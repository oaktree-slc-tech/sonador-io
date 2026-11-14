'''	Model classes for DICOM-SR (Structured Report) documents: the SR series and instances of a
	study, the parsing of their reports, and the findings API that hands the parsed annotations
	back to the imaging series and instances they describe.

	A DICOM-SR document is a DICOM object whose content is a tree of coded items rather than
	pixels. It carries structured data and annotations about other DICOM objects: its evidence
	sequences name the studies, series and instances it refers to, and its content tree holds the
	coded observations about them. A document is an instance of an SR series, which sits in the
	study next to the image series it annotates.

	Sonador uses DICOM-SR to annotate imaging resources. A document encodes findings (qualitative
	annotations, such as a Series Tag: a coded concept applied to a series, with optional
	description, text, location and display colours) and measurements (quantitative annotations
	of an instance, series, study or patient). One document can hold findings and measurements
	for several series and many instances of a study. Saving a document again adds an SR instance
	that names the one it replaces (Predecessor Documents Sequence), so an SR series carries a
	history: the most recent instance is the current document and is authoritative, the older
	instances are superseded revisions. The Sonador-SR 0.3 exchange profile is specified in
	oak-tree/medical-imaging/sonador-client#36; where the specification and this implementation
	differ, the implementation is authoritative.

	Classes in this module:
	* `DcmSRSeries`: one SR series of a study; `parse`, `findings` and `measurements` act on its
		current document.
	* `DcmSRSeriesCollection`: the SR series of a study (`ImagingStudy.sr_collection`).
	* `DcmStructuredInstance`, `DcmStructuredInstanceCollection`: base classes shared with
		DICOM-SEG that read the series and instance references from the evidence sequences.
	* `DcmSRInstance`: one document; `report` parses it on first access (one `file` request,
		cached), `findings` and `measurements` expose its annotations.
	* `DcmSRInstanceCollection`: the documents of one SR series; selects the current document.
	* `dcmsr_local_series`: a `DcmSRSeries` built from local documents, for the same API without
		an imaging server.

	Findings and measurements flow upwards from the document. `DcmSRInstance.findings` and
	`DcmSRInstance.measurements` link each item of the parsed report to this SR instance and to the
	imaging series and instance it references (from the cached collections of the parent study) and
	key it by imaging-series UID and measurement type. The collections merge these mappings, so
	`ImagingStudy.findings` returns `{series_uid: {measurement_type: FindingCollection}}` for the
	whole study and `ImagingSeries.findings` the `{measurement_type: FindingCollection}` of one series;
	`measurements` has the same shape with `MeasurementCollection`. A patient-domain measurement has
	no image reference: it is keyed by its source series (`Sonador-Measurement.Source-Series`) when
	it names one the study holds, else by the document's own series, and in both cases is also
	reachable with `measurements(frame_of_reference_uid=...)`, which the series and study accessors
	resolve from what is cached, without a request. Request budget of the default current-document retrieval: after
	`bulkpopulate_related(child_instances=True)` on the study collection, the first `findings` or
	`measurements` access costs one `file` GET per SR series and later accesses none, since the
	instance JSON, the files and the parsed reports are cached and shared by both. Without the child
	instances, the instance collection of each SR series is fetched first; `all_revisions=True` also
	fetches the superseded revisions, one `file` GET per revision whose file is not cached.
'''
import functools, datetime, logging, posixpath
from abc import ABCMeta, abstractmethod
from collections import OrderedDict

from pydicom.dataset import Dataset
from pydicom.sequence import Sequence

from ...apisettings import IMAGING_SERVER_RESOURCE_REPORT, DCMHEADER_SERIES_INSTANCE_UID, \
	DCMHEADER_SR_PERTINENT_OTHER_EVIDENCE_SEQUENCE, DCMHEADER_SR_EVIDENCE_SEQUENCES, DCMHEADER_SR_REF_SERIES_SEQ, \
	DCMHEADER_SR_REF_SOP_SEQ, DCMHEADER_SR_REF_INSTANCE_UID, DCMHEADER_SOP_INSTANCE_UID, DCMHEADER_SOP_CLASS_UID, \
	DCMHEADER_CONTENT_DATE, DCMHEADER_CONTENT_TIME, DCMHEADER_INSTANCE_NUMBER, DCMHEADER_INSTANCE_CREATION_DATE, \
	DCMHEADER_INSTANCE_CREATION_TIME, DCMHEADER_MODALITY, DCM_MODALITY_SR, DCMHEADER_SERIES_NUMBER, \
	DCMHEADER_SERIES_DESCRIPTION, DCMHEADER_SERIES_DATE, DCMHEADER_SERIES_TIME, DCMHEADER_STUDY_INSTANCE_UID, \
	IMAGING_SERVER_PARENT_SERIES, IMAGING_SERVER_SERIES_INDEX, IMAGING_SERVER_MAINDICOM, \
	DicomDatetimePair, DCMTS_CONTENT, FindingCollection, MeasurementCollection, DCMHEADER_FRAME_OF_REFERENCE_UID, \
	SONADOR_MEASUREMENT_TYPE_GENERIC
from ...apisettings.sr import sr_diagnostic, SR_DIAGNOSTIC_INFO, SR_DIAGNOSTIC_WARNING, \
	SR_CONDITION_PREDECESSOR_CHAIN_UNVERIFIED, SR_CONDITION_UID_ORDER_TIE_BREAK, SR_CONDITION_SHARED_TRACKING_UID, \
	SR_CONDITION_UNKNOWN_SOURCE_SERIES
from ..helpers.headers import dcm_header_value, dcm_header_uid
from .base import ImagingSeriesCoreResource, ImagingSeriesDcm0Mixin, ImagingSeriesCollection, ImagingServerChildCollection, \
	DcmInstanceCoreResource, DcmInstanceCoreCollection, ImagingSeriesBulkPopulateMixin

logger = logging.getLogger(__name__)


# Headers copied from a local document into the instance JSON of a local series (`dcmsr_local_series`)
DCMSR_LOCAL_INSTANCE_HEADERS = (DCMHEADER_SOP_INSTANCE_UID, DCMHEADER_SOP_CLASS_UID, DCMHEADER_CONTENT_DATE,
	DCMHEADER_CONTENT_TIME, DCMHEADER_INSTANCE_NUMBER, DCMHEADER_INSTANCE_CREATION_DATE, DCMHEADER_INSTANCE_CREATION_TIME)
DCMSR_LOCAL_SERIES_HEADERS = (DCMHEADER_SERIES_INSTANCE_UID, DCMHEADER_MODALITY, DCMHEADER_SERIES_NUMBER,
	DCMHEADER_SERIES_DESCRIPTION, DCMHEADER_SERIES_DATE, DCMHEADER_SERIES_TIME)


def dcmsr_series_frame_uid(series):
	'''	Frame of Reference UID of an imaging series, from what is cached: its main tags, the main tags
		of its cached instances, then its cached instance files. Makes no request.

		@input series (ImagingSeries): the series
		@returns str, or None when nothing cached carries it
	'''
	uid = dcm_header_uid(series.dicomdata, DCMHEADER_FRAME_OF_REFERENCE_UID)
	if uid:
		return uid
	for instance in series.__dict__.get('_slices') or []:
		uid = dcm_header_uid(instance.dicomdata, DCMHEADER_FRAME_OF_REFERENCE_UID)
		if not uid and instance.__dict__.get('_dcmfile') is not None:
			uid = dcm_header_uid(instance._dcmfile, DCMHEADER_FRAME_OF_REFERENCE_UID)
		if uid:
			return uid
	return None


def _measurement_frame_uid(measurement):
	'''	Frame of Reference UID of a measurement: its own, else that of the imaging series it is linked
		to, from what is cached. Makes no request.

		@input measurement (Measurement): the measurement
		@returns str, or None when unknown
	'''
	if measurement.frame_of_reference_uid:
		return measurement.frame_of_reference_uid
	return dcmsr_series_frame_uid(measurement.series) if measurement.series is not None else None


def _filter_frame(results, frame_of_reference_uid, collection_class=MeasurementCollection):
	'''	Keep the measurements of a `{series_uid: {measurement_type: collection}}` mapping that lie in a
		Frame of Reference. Series and types left empty are removed. Makes no request.

		@input results (OrderedDict): the mapping
		@input frame_of_reference_uid (str): the Frame of Reference UID
		@input collection_class (default=MeasurementCollection): collection type of the results

		@returns OrderedDict: the narrowed mapping
	'''
	narrowed = OrderedDict()
	for uid, by_type in results.items():
		for mtype, collection in by_type.items():
			kept = [m for m in collection if _measurement_frame_uid(m) == frame_of_reference_uid]
			if kept:
				narrowed.setdefault(uid, OrderedDict())[mtype] = collection_class(kept)
	return narrowed


def _shape_results(results, series_uid=None, measurement_type=None, collection_class=FindingCollection, frame_of_reference_uid=None):
	'''	Narrow a `{series_uid: {measurement_type: collection}}` mapping by the arguments: both given
		-> one collection (empty when absent); series only -> `{measurement_type: collection}`; type
		only -> `{series_uid: collection}`; neither -> the full mapping.

		@input collection_class (default=FindingCollection): type of the empty collection returned when
			both arguments are given and nothing matches
		@input frame_of_reference_uid (str, default=None): keep only the measurements in this Frame of
			Reference (see `_filter_frame`)
	'''
	if frame_of_reference_uid is not None:
		results = _filter_frame(results, frame_of_reference_uid, collection_class=collection_class)
	if series_uid is not None and measurement_type is not None:
		return results.get(series_uid, {}).get(measurement_type) or collection_class([])
	if series_uid is not None:
		return results.get(series_uid, OrderedDict())
	if measurement_type is not None:
		return OrderedDict((uid, by_type[measurement_type]) for uid, by_type in results.items() if measurement_type in by_type)
	return results


def _merge_results(target, source, collection_class=FindingCollection):
	'''	Merge `source` into `target`, both `{series_uid: {measurement_type: collection}}` mappings;
		collections under the same keys are concatenated into a new `collection_class`.

		@returns the `target` mapping
	'''
	for uid, by_type in source.items():
		bucket = target.setdefault(uid, OrderedDict())
		for mtype, collection in by_type.items():
			if mtype in bucket:
				bucket[mtype] = collection_class(list(bucket[mtype]) + list(collection))
			else:
				bucket[mtype] = collection_class(list(collection))
	return target


def dcmsr_local_series(datasets, server=None):
	'''	Build a `DcmSRSeries` from DICOM-SR documents held locally (files read into pydicom datasets).
		The series and instance JSON are derived from the document headers and each dataset is
		attached as the cached file of its instance, so the findings API works without an imaging
		server and makes no requests.

		@input datasets (iterable of pydicom.Dataset): the documents of one SR series
		@input server (default=None): Sonador connection the series is bound to
		@returns DcmSRSeries
		@raises ValueError: no datasets, or datasets from more than one series
	'''
	datasets = list(datasets)
	if not datasets:
		raise ValueError('At least one DICOM-SR document is required')
	series_uids = set(dcm_header_uid(ds, DCMHEADER_SERIES_INSTANCE_UID) for ds in datasets)
	if len(series_uids) != 1:
		raise ValueError('The documents belong to %d series; parse one series at a time' % len(series_uids))
	series_uid = series_uids.pop()

	first = datasets[0]
	series_tags = OrderedDict((name, dcm_header_value(first, name)) for name in DCMSR_LOCAL_SERIES_HEADERS)
	series_tags = OrderedDict((k, str(v)) for k, v in series_tags.items() if v is not None)
	series_tags.setdefault(DCMHEADER_MODALITY, DCM_MODALITY_SR)
	instance_pks = [dcm_header_uid(ds, DCMHEADER_SOP_INSTANCE_UID) or ('instance-%d' % i) for i, ds in enumerate(datasets, 1)]
	series = DcmSRSeries(server, {
		'ID': series_uid, 'Type': 'Series', IMAGING_SERVER_MAINDICOM: series_tags, 'Instances': instance_pks,
		'ParentStudy': dcm_header_uid(first, DCMHEADER_STUDY_INSTANCE_UID),
	})

	instances_json = []
	for i, (pk, ds) in enumerate(zip(instance_pks, datasets), 1):
		tags = OrderedDict((name, dcm_header_value(ds, name)) for name in DCMSR_LOCAL_INSTANCE_HEADERS)
		instances_json.append({
			'ID': pk, 'Type': 'Instance', IMAGING_SERVER_PARENT_SERIES: series_uid, IMAGING_SERVER_SERIES_INDEX: i,
			IMAGING_SERVER_MAINDICOM: OrderedDict((k, str(v)) for k, v in tags.items() if v is not None),
		})
	instances = DcmSRInstanceCollection(server, instances_json, series=series)
	for instance in instances:
		instance._dcmfile = datasets[instance_pks.index(instance.pk)]
	series.instances_collection = instances
	return series


class DcmSRSeries(ImagingSeriesDcm0Mixin, ImagingSeriesCoreResource):
	'''	A DICOM-SR series of a study. Its instances (`DcmSRInstance`) are the documents of the series,
		in Sonador the revisions of one structured report; `parse`, `findings` and `measurements` act
		on the current document.
	'''
	@property
	def dcminstance_modelcollection_class(self): return DcmSRInstanceCollection

	@property
	def instances(self):
		'''	Identifiers of the instances of the series, from the series JSON

			@returns list of str
		'''
		return self._objectdata.get('Instances')

	@property
	def instances_collection(self):
		'''	DICOM-SR instances of the series, fetched on first access and cached

			@returns DcmSRInstanceCollection
		'''
		if getattr(self, '_instances', None) is None:
			setattr(self, '_instances', self.fetch_dcminstances())

		return self._instances

	@instances_collection.setter
	def instances_collection(self, val):
		'''	Set the instances collection of the series
		'''
		self._instances = val

	@property
	def series_reference_uids(self):
		'''	Series Instance UIDs of the image series the documents of this series refer to, from the
			evidence sequences. Read from the cached files or parsed reports when present, else from
			the instance tags.

			@returns set of str
		'''
		return self.instances_collection.series_reference_uids

	@property
	def imaging_series_collection(self):
		'''	(DEPRECATED) Imaging series the documents of this series refer to, most recent first; alias
			of `reference_series_collection`

			@returns list of ImagingSeries
		'''
		return self.reference_series_collection

	@property
	def reference_series_collection(self):
		'''	Imaging series of the parent study that the documents of this series refer to, most recent
			(by `ts`) first. Not cached, so a repopulated study is seen on the next access.

			@returns list of ImagingSeries
		'''
		references = self.series_reference_uids
		return sorted(
			[s for s in self.parent.series_collection if s.series_uid in references],
			key=lambda s: s.ts if s.ts else datetime.datetime(year=1900, month=1, day=1),
			reverse=True)

	@property
	def kafka_url(self):
		return posixpath.join(self.resource_url, 'kafka')

	def parse(self, report_class=None, strict=False, all_revisions=False, **kwargs):
		'''	Parse the current document of the series, or every document with `all_revisions=True`
			(`DcmSRInstanceCollection.parse`). The report is cached on each instance; the parse costs one
			`file` request per instance parsed unless its file is cached.

			@input report_class (subclass of SonadorBaseSR, default=SonadorComprehensiveSR3D)
			@input strict (bool, default=False): raise SRDiagnosticError on a `warning` or `error`
				diagnostic, in the selection of the current document and in the parse
			@input all_revisions (bool, default=False): parse the superseded revisions as well
			@returns DcmSRSeries: the series
		'''
		self.instances_collection.parse(report_class=report_class, strict=strict, all_revisions=all_revisions, **kwargs)
		return self

	def invalidate(self):
		'''	Drop the parsed reports of the cached instances; the cached files are kept. The next
			`report`, `findings` or `measurements` access parses again.
		'''
		if self.__dict__.get('_instances') is not None:
			for instance in self._instances:
				instance.invalidate()

	def findings(self, series_uid=None, measurement_type=None, **kwargs):
		'''	Qualitative findings of the current document of the series, keyed by imaging-series UID and
			measurement type. Arguments as `DcmSRInstanceCollection.findings`.

			@returns
			  - dict keyed to imaging-series UID: `{series_uid: {measurement_type: FindingCollection}}`
			  - dict keyed to measurement type for a `series_uid`: `{measurement_type: FindingCollection}`
			  - dict keyed to imaging-series UID for a `measurement_type`: `{series_uid: FindingCollection}`
			  - FindingCollection for both
		'''
		return self.instances_collection.findings(series_uid=series_uid, measurement_type=measurement_type, **kwargs)

	def measurements(self, series_uid=None, measurement_type=None, **kwargs):
		'''	Quantitative measurements of the current document of the series, keyed by imaging-series UID
			and measurement type. Arguments as `DcmSRInstanceCollection.measurements`. Costs one `file`
			request on first access, shared with `findings`; later accesses cost none.

			@returns
			  - dict keyed to imaging-series UID: `{series_uid: {measurement_type: MeasurementCollection}}`
			  - dict keyed to measurement type for a `series_uid`: `{measurement_type: MeasurementCollection}`
			  - dict keyed to imaging-series UID for a `measurement_type`: `{series_uid: MeasurementCollection}`
			  - MeasurementCollection for both (empty when nothing matches)
		'''
		return self.instances_collection.measurements(series_uid=series_uid, measurement_type=measurement_type, **kwargs)


class DcmSRSeriesCollection(ImagingSeriesBulkPopulateMixin, ImagingServerChildCollection):
	'''	The DICOM-SR series of a study (`ImagingStudy.sr_collection`). `findings` and `measurements`
		aggregate the current document of every series.

		@attr diagnostics (list of SRDiagnostic): conditions met while aggregating across the series
			(a tracking UID shared by two series)
	'''
	model = DcmSRSeries

	def __init__(self, *args, **kwargs):
		self.parent = kwargs.pop('study', None)
		self.diagnostics = []
		super(DcmSRSeriesCollection, self).__init__(*args, **kwargs)

	def _init_collection_models(self, **kwargs):
		if self.parent:
			kwargs['study'] = self.parent

		return super(DcmSRSeriesCollection, self)._init_collection_models(**kwargs)

	def parse(self, report_class=None, strict=False, all_revisions=False, **kwargs):
		'''	Parse the current document of every series of the collection (one `file` request per series
			unless its file is cached), or every document with `all_revisions=True`. Arguments as
			`DcmSRSeries.parse`.

			@returns DcmSRSeriesCollection: the collection
		'''
		for series in self:
			series.parse(report_class=report_class, strict=strict, all_revisions=all_revisions, **kwargs)
		return self

	def invalidate(self):
		'''	Drop the parsed reports of every series (`DcmSRSeries.invalidate`)
		'''
		for series in self:
			series.invalidate()

	def _aggregate(self, attr, series_uid=None, measurement_type=None, collection_class=FindingCollection, frame_of_reference_uid=None, **kwargs):
		'''	Merge the results of `attr` (`findings` or `measurements`) of every SR series. The current
			document is selected per series; the results of different series are independent and are
			combined under the identity (SR series, tracking UID). A tracking UID that two series share
			yields both results and a `warning` diagnostic in `diagnostics` (recorded, never raised).

			@input attr (str): `findings` or `measurements`
			@input series_uid, measurement_type: narrowing of the result, as `_shape_results`
			@input collection_class: collection type the merged results are built with
			@input frame_of_reference_uid (str, default=None): keep only the measurements in this Frame of
				Reference, resolved from what is cached
			@input kwargs: passed to the series method (`all_revisions`, `diagnostics`, `strict`)
			@returns the narrowed mapping
		'''
		results = OrderedDict()
		seen = {}
		for series in self:
			per_series = getattr(series, attr)(collection_class=collection_class, **kwargs)
			for uid, by_type in per_series.items():
				for mtype, collection in by_type.items():
					for item in collection:
						tracking_uid = getattr(item, 'tracking_uid', None)
						if tracking_uid:
							other = seen.setdefault(tracking_uid, series.pk)
							if other != series.pk:
								sr_diagnostic(self.diagnostics, SR_DIAGNOSTIC_WARNING, SR_CONDITION_SHARED_TRACKING_UID,
									'tracking UID shared by two SR series; both results are returned',
									path='series %s and %s' % (other, series.pk), value=tracking_uid)
			_merge_results(results, per_series, collection_class=collection_class)
		return _shape_results(results, series_uid=series_uid, measurement_type=measurement_type, collection_class=collection_class,
			frame_of_reference_uid=frame_of_reference_uid)

	def findings(self, series_uid=None, measurement_type=None, **kwargs):
		'''	Qualitative findings of the current document of every SR series of the study, keyed by
			imaging-series UID and measurement type.

			@input series_uid (str, default=None): imaging series to narrow to
			@input measurement_type (str, default=None): measurement type to narrow to (`series-tag`, `arrow`)
			@input kwargs: passed to `DcmSRInstanceCollection.findings` (`all_revisions`, `diagnostics`,
				`strict`)
			@returns
			  - dict keyed to imaging-series UID: `{series_uid: {measurement_type: FindingCollection}}`
			  - dict keyed to measurement type for a `series_uid`: `{measurement_type: FindingCollection}`
			  - dict keyed to imaging-series UID for a `measurement_type`: `{series_uid: FindingCollection}`
			  - FindingCollection for both
		'''
		return self._aggregate('findings', series_uid=series_uid, measurement_type=measurement_type,
			collection_class=FindingCollection, **kwargs)

	def measurements(self, series_uid=None, measurement_type=None, frame_of_reference_uid=None, **kwargs):
		'''	Quantitative measurements of the current document of every SR series of the study, keyed by
			imaging-series UID and measurement type. Arguments as `findings`. Costs one `file` request per
			SR series on first access, shared with `findings`.

			@input frame_of_reference_uid (str, default=None): keep only the measurements in this Frame of
				Reference, resolved from what is cached, with no request

			@returns
			  - dict keyed to imaging-series UID: `{series_uid: {measurement_type: MeasurementCollection}}`
			  - dict keyed to measurement type for a `series_uid`: `{measurement_type: MeasurementCollection}`
			  - dict keyed to imaging-series UID for a `measurement_type`: `{series_uid: MeasurementCollection}`
			  - MeasurementCollection for both (empty when nothing matches)
		'''
		return self._aggregate('measurements', series_uid=series_uid, measurement_type=measurement_type,
			collection_class=MeasurementCollection, frame_of_reference_uid=frame_of_reference_uid, **kwargs)


class DcmStructuredInstance(DcmInstanceCoreResource):
	'''	Instance base class shared by DICOM-SR and DICOM-SEG documents: a structured DICOM object
		that refers to image series and instances as evidence
	'''
	@property
	@abstractmethod
	def instance_reference_uids(self):
		'''	SOP Instance UIDs of the image instances the structured document refers to

			@returns set of str
		'''	

	@property
	@abstractmethod
	def series_reference_uids(self):
		'''	Series Instance UIDs of the image series the structured document refers to

			@returns set of str
		'''


class DcmStructuredInstanceCollection(DcmInstanceCoreCollection):
	'''	Collection base class shared by DICOM-SR and DICOM-SEG instances
	'''
	def _instances_with_cached_headers(self):
		'''	Instances whose evidence can be read without a request (cached file or parsed report).
			Every instance when none is cached, so that the union falls back to the instance tags.

			@returns list of DcmStructuredInstance
		'''
		cached = [dcm for dcm in self if dcm.__dict__.get('_dcmfile') is not None or dcm.__dict__.get('_report') is not None]
		return cached or list(self)

	@property
	def series_reference_uids(self):
		'''	Series Instance UIDs of the image series the instances of the collection refer to. Read
			from the instances whose file or report is cached (the current document once it has been
			parsed); the instance tags are requested only when nothing is cached.

			@returns set of str: empty for an empty collection
		'''
		return functools.reduce(lambda a,b: a.union(b), (dcm.series_reference_uids for dcm in self._instances_with_cached_headers()), set())

	@property
	def instance_reference_uids(self):
		'''	SOP Instance UIDs of the image instances the instances of the collection refer to, read
			from the same instances as `series_reference_uids`

			@returns set of str
		'''
		return functools.reduce(lambda a,b: a.union(b), (dcm.instance_reference_uids for dcm in self._instances_with_cached_headers()), set())


def dcmsr_iter_series_references(tags):
	'''	Iterate over the Referenced Series Sequence items recorded as evidence in a DICOM-SR document.
		Both evidence sequences are read: the Current Requested Procedure Evidence Sequence
		(same-study evidence, where highdicom 0.23 and later record it) and the Pertinent Other
		Evidence Sequence (other-study evidence; documents written by earlier Sonador releases
		record all their evidence there). A missing sequence yields nothing.

		@input tags (dict or pydicom.Dataset): simplified DICOM tags of the SR instance (sequences as
			lists of dicts) or the document dataset
		@yields dict or pydicom.Dataset: Referenced Series Sequence item
	'''
	for evidence_seq in DCMHEADER_SR_EVIDENCE_SEQUENCES:
		for refset in dcm_header_value(tags, evidence_seq) or []:
			for ref in dcm_header_value(refset, DCMHEADER_SR_REF_SERIES_SEQ) or []:
				yield ref


class DcmSRInstance(DcmStructuredInstance):
	'''	One DICOM-SR document: an instance of an SR series.

		@property report: parsed report model (`SonadorComprehensiveSR3D` unless `report_class` is set),
			parsed on first access from the document file (one `file` request, cached)
		@property highdicom: highdicom SR object the report was parsed from
		@property diagnostics (list of SRDiagnostic): conditions met while parsing
		@property findings: qualitative findings of the document, keyed by imaging-series UID and
			measurement type
		@property measurements: quantitative measurements of the document, same shape
	'''
	report_class = None

	def _default_report_class(self):
		if self.report_class is not None:
			return self.report_class
		from ..helpers.sr import SonadorComprehensiveSR3D
		return SonadorComprehensiveSR3D

	def parse(self, report_class=None, strict=False, **kwargs):
		'''	Parse the document to its report model and cache it on the instance as `report`. The
			document is read with `dcmfile(cache=True)`: one `file` request unless the file is cached.
			The model is bound to the series (`report.series`) when the instance belongs to an
			imaging-server series.

			@input report_class (subclass of SonadorBaseSR, default=SonadorComprehensiveSR3D)
			@input strict (bool, default=False): raise SRDiagnosticError on the first `warning` or
				`error` diagnostic of the parse
			@input kwargs: passed to `report_class.from_sr` (for example `diagnostics`)
			@returns the report model
		'''
		report_class = report_class or self._default_report_class()
		report = report_class.from_sr(self.dcmfile(cache=True), strict=strict, **kwargs)
		if self.__dict__.get('_parent') is not None and self.__dict__.get('pacs') is not None:
			report.series = self._parent
		self._report = report
		return report

	def invalidate(self):
		'''	Drop the parsed report; the cached file is kept, so the next `report` access parses again
			without a request
		'''
		self._report = None

	@property
	def report(self):
		'''	Parsed report model, parsed on first access with `parse` and its defaults

			@returns the report model (`report_class`)
		'''
		if self.__dict__.get('_report') is None:
			self.parse()
		return self._report

	@report.setter
	def report(self, val):
		self._report = val

	@property
	def highdicom(self):
		'''	highdicom SR object the report was parsed from

			@returns highdicom SR document or None: None when the report holds no parsed document
		'''
		parsed = getattr(self.report, 'parsed', None)
		return parsed.highdicom if parsed is not None else None

	@property
	def diagnostics(self):
		'''	Diagnostics of the parse

			@returns list of SRDiagnostic: empty before the document is parsed
		'''
		report = self.__dict__.get('_report')
		return list(report.diagnostics) if report is not None else []

	def _evidence_headers(self):
		'''	Headers the evidence sequences are read from, cheapest first: the dataset of the parsed
			report, the cached file, else the instance tags (one `simplified-tags` request, cached)

			@returns pydicom.Dataset or dict
		'''
		report = self.__dict__.get('_report')
		if report is not None and getattr(report, 'parsed', None) is not None:
			return report.parsed.dataset
		if self.__dict__.get('_dcmfile') is not None:
			return self._dcmfile
		return self.tags

	@property
	def instance_reference_uids(self):
		'''	SOP Instance UIDs of the image instances the document refers to, read from the Referenced
			SOP Sequence of both evidence sequences (Current Requested Procedure Evidence Sequence and
			Pertinent Other Evidence Sequence). A series recorded without instance-level references
			(a document written with `record_instances=False`) contributes no instances.

			@returns set of str
		'''
		instance_references = set()

		# Iterate through all references in the evidence sequences, unpack reference UIDs
		for ref in dcmsr_iter_series_references(self._evidence_headers()):
			for dcm in dcm_header_value(ref, DCMHEADER_SR_REF_SOP_SEQ) or []:
				uid = dcm_header_uid(dcm, DCMHEADER_SR_REF_INSTANCE_UID)
				if uid:
					instance_references.add(uid)

		return instance_references

	@property
	def series_reference_uids(self):
		'''	Series Instance UIDs of the image series the document refers to, read from both evidence
			sequences (Current Requested Procedure Evidence Sequence and Pertinent Other Evidence
			Sequence)

			@returns set of str
		'''
		series_references = set()

		# Iterate through all series references in the evidence sequences, unpack UIDs
		for ref in dcmsr_iter_series_references(self._evidence_headers()):
			uid = dcm_header_uid(ref, DCMHEADER_SERIES_INSTANCE_UID)
			if uid:
				series_references.add(uid)

		return series_references

	@property
	def instance_creation_ts(self):
		'''	Instance Creation Date and Time of the document, from the instance JSON (no request)

			@returns datetime.datetime or None: None when absent or invalid (logged)
		'''
		pair = DicomDatetimePair(self.dicomdata.get(DCMHEADER_INSTANCE_CREATION_DATE),
			self.dicomdata.get(DCMHEADER_INSTANCE_CREATION_TIME), meta=DCMTS_CONTENT)
		try:
			return pair.ts
		except Exception as err:
			logger.error('Invalid instance creation timestamp. Error: "%s"' % err)
			return None

	def _study_series(self):
		'''	Cached imaging series collection of the study the document belongs to. Makes no request.

			@returns the series collection, or None when it is not cached
		'''
		series = self.__dict__.get('_parent')
		study = series.__dict__.get('_parent') if series is not None else None
		return study.__dict__.get('_series') if study is not None else None

	def _link_annotation(self, annotation):
		'''	Attach the model objects a finding or measurement describes: this SR instance
			(`sr_instance`), the imaging series of the parent study whose UID it references (`series`)
			and, from the cached instances of that series, the referenced instance (`instance`). A
			patient-domain measurement without a series UID, or naming a source series the study does
			not hold, links to the one cached imaging series in its Frame of Reference, when there is
			exactly one. Only cached collections are read; no request is made, and a level that is not
			cached leaves its attribute unset.

			@input annotation (Finding or Measurement): the item to link, modified in place
		'''
		annotation.sr_instance = self
		series_collection = self._study_series()
		if series_collection is None:
			return
		matches = []
		if annotation.series_uid:
			matches = [sx for sx in series_collection if sx.series_uid == annotation.series_uid]
		if not matches:
			frame_uid = getattr(annotation, 'frame_of_reference_uid', None)
			matches = [sx for sx in series_collection if frame_uid and dcmsr_series_frame_uid(sx) == frame_uid]
			if len(matches) != 1:
				return
		for sx in matches[:1]:
			annotation.series = sx
			instances = sx.__dict__.get('_slices')
			if instances is not None and annotation.reference is not None:
				for dcm in instances:
					if dcm.sop_instance_uid == annotation.reference.sop_instance_uid:
						annotation.instance = dcm
						break

	def _link_finding(self, finding):
		'''	Attach the model objects a finding describes (`_link_annotation`).

			@input finding (Finding): the finding to link, modified in place
		'''
		self._link_annotation(finding)

	@property
	def findings(self):
		'''	Qualitative findings of the document, parsed with `report` (one `file` request on first
			access): the findings of every group of the report that carry a measurement type, each
			linked to its models (`_link_finding`) and keyed by the imaging series its reference
			resolves to; a patient-domain arrow by its source series (`Sonador-Measurement.Source-Series`).
			A finding without a series is keyed by the identifier of the document's own series.

			@returns dict keyed to imaging-series UID: `{series_uid: {measurement_type: FindingCollection}}`
		'''
		results = OrderedDict()
		for group in (self.report.groups or []):
			for finding in (group.findings or []):
				if not finding.type:
					continue
				self._link_finding(finding)
				series_uid = finding.series_uid or self.series
				bucket = results.setdefault(series_uid, OrderedDict()).setdefault(finding.type, [])
				bucket.append(finding)
		return OrderedDict((uid, OrderedDict((mtype, FindingCollection(items)) for mtype, items in by_type.items()))
			for uid, by_type in results.items())

	@property
	def measurements(self):
		'''	Quantitative measurements of the document, parsed with `report` (one `file` request on first
			access, shared with `findings`, and none after that): the measurements of every group of the
			report, each linked to its models (`_link_annotation`) and keyed by the imaging series its
			image reference resolves to. A patient-domain measurement is keyed by its source series
			(`Sonador-Measurement.Source-Series`); without one, by the identifier of the document's own
			series. A source series the study does not hold, when the study's series collection is
			cached, keys the measurement by the document's series with the `warning`
			`unknown-source-series`, recorded once on the report's diagnostics (`diagnostics`) and never
			raised. A NUM group with an unknown tracking identifier is listed under
			`SONADOR_MEASUREMENT_TYPE_GENERIC`.

			@returns dict keyed to imaging-series UID: `{series_uid: {measurement_type: MeasurementCollection}}`
		'''
		results = OrderedDict()
		series_collection = self._study_series()
		held = set(sx.series_uid for sx in series_collection) if series_collection is not None else None
		for group in (self.report.groups or []):
			for measurement in (getattr(group, 'measurements', None) or []):
				self._link_annotation(measurement)
				series_uid = measurement.series_uid or self.series
				patient = measurement.geometry is not None and measurement.geometry.is_patient
				if patient and measurement.series_uid and held is not None and measurement.series_uid not in held:
					series_uid = self.series
					if not any(d.condition == SR_CONDITION_UNKNOWN_SOURCE_SERIES and d.value == measurement.series_uid
							and d.path == 'group %s' % measurement.tracking_uid for d in self.report.diagnostics):
						sr_diagnostic(self.report.diagnostics, SR_DIAGNOSTIC_WARNING, SR_CONDITION_UNKNOWN_SOURCE_SERIES,
							'source series is not a series of the study; keyed by the series of the document',
							path='group %s' % measurement.tracking_uid, value=measurement.series_uid)
				bucket = results.setdefault(series_uid, OrderedDict()).setdefault(measurement.type or SONADOR_MEASUREMENT_TYPE_GENERIC, [])
				bucket.append(measurement)
		return OrderedDict((uid, OrderedDict((mtype, MeasurementCollection(items)) for mtype, items in by_type.items()))
			for uid, by_type in results.items())


class DcmSRInstanceCollection(DcmStructuredInstanceCollection):
	'''	The DICOM-SR instances of one SR series: the revisions of its document. `current_instance`
		selects the authoritative document; `parse`, `findings` and `measurements` act on it.
	'''
	model = DcmSRInstance

	def parse(self, report_class=None, strict=False, all_revisions=False, diagnostics=None, **kwargs):
		'''	Parse the current document of the series, or every document with `all_revisions=True`.
			The current document is selected with `current_instance` and parsed with
			`DcmSRInstance.parse`, which caches the report on the instance: one `file` request per
			instance parsed unless its file is cached. Superseded revisions are fetched only with
			`all_revisions=True`.

			@input report_class (subclass of SonadorBaseSR, default=SonadorComprehensiveSR3D)
			@input strict (bool, default=False): raise SRDiagnosticError on a `warning` or `error`
				diagnostic, in the selection of the current document and in the parse
			@input all_revisions (bool, default=False): parse the superseded revisions as well
			@input diagnostics (list, default=None): list the selection diagnostics are appended to
			@input kwargs: passed to `DcmSRInstance.parse`
			@returns DcmSRInstanceCollection: the collection
		'''
		if all_revisions:
			instances = list(self)
		else:
			current = self.current_instance(diagnostics=diagnostics, strict=strict)
			instances = [current] if current is not None else []
		for instance in instances:
			instance.parse(report_class=report_class, strict=strict, **kwargs)
		return self

	def current_instance(self, diagnostics=None, strict=False):
		'''	The current document of the series: the instance that is authoritative for its findings
			and measurements. When the file of every instance is cached, the current document is the
			instance no other document names in its Predecessor Documents Sequence (no request).
			Otherwise, or when the predecessor chain leaves more than one candidate, the newest
			instance by the header timestamps of the instance JSON is chosen: Content Date and Time,
			then Instance Creation Date and Time, then SOP Instance UID order.

			@input diagnostics (list, default=None): list the selection diagnostics are appended to:
				`info` when the predecessor chain could not be verified, `warning` when the SOP Instance
				UID order decided between instances with the same timestamps
			@input strict (bool, default=False): raise SRDiagnosticError on the `warning` diagnostic
			@returns DcmSRInstance or None: None for an empty collection
		'''
		instances = list(self)
		if not instances:
			return None
		if len(instances) == 1:
			return instances[0]

		# Predecessor chain, when every document is cached (no request)
		if all(dcm.__dict__.get('_dcmfile') is not None for dcm in instances):
			from ..helpers.sr.parse import srdoc_predecessors
			superseded = set(pre.sop_instance_uid for dcm in instances for pre in srdoc_predecessors(dcm._dcmfile))
			current = [dcm for dcm in instances if dcm.sop_instance_uid not in superseded]
			if len(current) == 1:
				return current[0]
			instances = current or instances
		else:
			sr_diagnostic(diagnostics, SR_DIAGNOSTIC_INFO, SR_CONDITION_PREDECESSOR_CHAIN_UNVERIFIED,
				'current document selected from the instance headers; the Predecessor Documents chain was not verified',
				path='series %s' % (self.parent.pk if self.parent is not None else '?'))

		floor = datetime.datetime(year=1900, month=1, day=1)
		def key(dcm):
			return (dcm.ts or floor, dcm.instance_creation_ts or floor, str(dcm.sop_instance_uid or ''))
		ordered = sorted(instances, key=key, reverse=True)
		first, second = ordered[0], ordered[1]
		if key(first)[:2] == key(second)[:2]:
			sr_diagnostic(diagnostics, SR_DIAGNOSTIC_WARNING, SR_CONDITION_UID_ORDER_TIE_BREAK,
				'documents share their timestamps; the current document was chosen by SOP Instance UID order',
				path='series %s' % (self.parent.pk if self.parent is not None else '?'), value=first.sop_instance_uid, strict=strict)
		return first

	def _aggregate(self, attr, series_uid=None, measurement_type=None, all_revisions=False, diagnostics=None,
			collection_class=FindingCollection, strict=False, frame_of_reference_uid=None):
		'''	Merge the results of `attr` (`findings` or `measurements`) of the current instance, or of
			every instance with `all_revisions=True`, and narrow them by the arguments (`_shape_results`)

			@returns the narrowed mapping
		'''
		if all_revisions:
			instances = list(self)
		else:
			current = self.current_instance(diagnostics=diagnostics, strict=strict)
			instances = [current] if current is not None else []
		results = OrderedDict()
		for instance in instances:
			_merge_results(results, getattr(instance, attr), collection_class=collection_class)
		return _shape_results(results, series_uid=series_uid, measurement_type=measurement_type, collection_class=collection_class,
			frame_of_reference_uid=frame_of_reference_uid)

	def findings(self, series_uid=None, measurement_type=None, all_revisions=False, diagnostics=None, strict=False,
			collection_class=FindingCollection):
		'''	Qualitative findings of the current document of the series, keyed by imaging-series UID
			and measurement type. Parsing the document costs one `file` request on first access; the
			report is cached on the instance.

			@input series_uid (str, default=None): imaging series to narrow to
			@input measurement_type (str, default=None): measurement type to narrow to (`series-tag`, `arrow`)
			@input all_revisions (bool, default=False): aggregate every document of the series, the
				superseded revisions included (one `file` request per revision whose file is not cached)
			@input diagnostics (list, default=None): list the current-document selection diagnostics
				are appended to (the parse diagnostics are on `DcmSRInstance.diagnostics`)
			@input strict (bool, default=False): raise SRDiagnosticError on a `warning` selection
				diagnostic. The parse itself runs with its defaults; call `parse(strict=True)` first
				for a strict parse.
			@input collection_class (default=FindingCollection): collection type of the results
			@returns
			  - dict keyed to imaging-series UID: `{series_uid: {measurement_type: FindingCollection}}`
			  - dict keyed to measurement type for a `series_uid`: `{measurement_type: FindingCollection}`
			  - dict keyed to imaging-series UID for a `measurement_type`: `{series_uid: FindingCollection}`
			  - FindingCollection for both (empty when nothing matches)
		'''
		return self._aggregate('findings', series_uid=series_uid, measurement_type=measurement_type,
			all_revisions=all_revisions, diagnostics=diagnostics, collection_class=collection_class, strict=strict)

	def measurements(self, series_uid=None, measurement_type=None, all_revisions=False, diagnostics=None, strict=False,
			collection_class=MeasurementCollection, frame_of_reference_uid=None):
		'''	Quantitative measurements of the current document of the series, keyed by imaging-series UID
			and measurement type. Arguments as `findings`, with `collection_class` defaulting to
			`MeasurementCollection`. Parsing the document costs one `file` request on first access,
			shared with `findings`; the report is cached on the instance.

			@input frame_of_reference_uid (str, default=None): keep only the measurements in this Frame of
				Reference, resolved from what is cached, with no request

			@returns
			  - dict keyed to imaging-series UID: `{series_uid: {measurement_type: MeasurementCollection}}`
			  - dict keyed to measurement type for a `series_uid`: `{measurement_type: MeasurementCollection}`
			  - dict keyed to imaging-series UID for a `measurement_type`: `{series_uid: MeasurementCollection}`
			  - MeasurementCollection for both (empty when nothing matches)
		'''
		return self._aggregate('measurements', series_uid=series_uid, measurement_type=measurement_type,
			all_revisions=all_revisions, diagnostics=diagnostics, collection_class=collection_class, strict=strict,
			frame_of_reference_uid=frame_of_reference_uid)
