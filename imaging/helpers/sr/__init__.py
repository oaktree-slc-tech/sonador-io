'''	Report models that write and read Sonador DICOM-SR documents.

	A DICOM-SR (Structured Report) document is a tree of coded content items rather than pixels. In
	Sonador it carries findings (qualitative annotations such as Series Tags) and measurements
	(quantitative annotations), and it names the DICOM instances they were made on as evidence. One
	document can cover several series and many instances of a study.

	The report models (`SonadorBaseSR`, `SonadorComprehensiveSR3D`, `SonadorImagingMeasurementReport`)
	hold the local data model of a document (`sonador.apisettings.base.sr`: findings, measurements,
	report groups, reference frames) together with the document-level state a round trip needs: the
	parsed document, the root template, the observers, the preserved root items and the diagnostics.

	Entry points:
	* `SonadorBaseSR.from_sr(document)` builds the model of one document given as a file, bytes, a
		dataset or an imaging server instance.
	* `SonadorBaseSR.parse(documents)` parses one or more documents, or an imaging server series or
		instance, and returns a `DcmSRSeries` whose instances carry the models.
	* `create_sr()` encodes the model as a DICOM-SR document (highdicom) with the equipment profile
		and Device Observer of the class, a document class and the per-finding layout that class
		permits. A new document is a Comprehensive 3D SR. On a re-save the class is selected from the
		parsed document's class and the content: an Enhanced SR stays Enhanced SR unless the content
		needs Comprehensive 3D SR, a Comprehensive SR is written as Comprehensive 3D SR, a
		Comprehensive 3D SR stays as it is (the transitions are stated on `SonadorComprehensiveSR3D`
		and `SonadorComprehensiveSR3D.create_sr`). A re-save names the parsed document as its
		predecessor.

	The documents follow the Sonador-SR 0.3 exchange profile shared with the Sonador Viewer, which is
	specified in oak-tree/medical-imaging/sonador-client#36. The coded concepts and the per-finding
	encoder and parser are in `sonador.apisettings.sr.seriestag`; the document reader is in `.parse`.
'''
import os, functools, abc, json, datetime, itertools, copy, logging
from collections import OrderedDict
from typing import Optional, Sequence, Union

from pydicom.sequence import Sequence as DcmSequence
from pydicom.dataset import Dataset as DcmDataset
from pydicom.uid import generate_uid
from pydicom.sr.codedict import codes as dcmcodes

from highdicom.sr import Comprehensive3DSR, EnhancedSR, ComprehensiveSR, MeasurementReport as SRMeasurementReport
from highdicom.sr.templates import TrackingIdentifier
from highdicom.sr.value_types import ContainerContentItem, ContentSequence
from highdicom.sr.enum import RelationshipTypeValues

from client.errors import ConfigurationError
from client.utils.object import pick, omit

from ....apisettings.base import ImageTransformMatrix, ImageTransformMatrixCollection, \
	ImageCoord, ImageCoordCollection, Finding, FindingCollection, Measurement, MeasurementCollection, \
	ReportGroup, VolumePointCollectionGroup, ReportMetaGroup, ReportGroupCollection, ImageReference, \
	SONADOR_SR, SONADOR_MANUFACTURER, SONADOR_IO, sonador_software_version, DCMTS_SERIES, \
	DCMHEADER_SERIES_DESCRIPTION, DCMHEADER_SERIES_NUMBER, DCMHEADER_REGISTRATION_SEQUENCE, \
	DCMHEADER_MANUFACTER_MODEL_NAME, DCMHEADER_SOFTWARE_VERSIONS, DCMHEADER_SOP_CLASS_UID, DCMHEADER_SOP_INSTANCE_UID, \
	DCMHEADER_SERIES_INSTANCE_UID, DCMHEADER_STUDY_INSTANCE_UID
from ....apisettings.sr import dcm_encode_reference_frame_transform_matrix, srencode_observation_context, \
	DCMSR_SONADOR_DATA_REPORT, sr_diagnostic, SRDiagnostic, SRDiagnosticError, srcontent_extra_items, \
	srcontent_is_by_reference, srcontent_holds_by_reference, SR_DIAGNOSTIC_WARNING, SR_DIAGNOSTIC_ERROR, \
	SR_CONDITION_DOCUMENT_CLASS_UPGRADED, SR_CONDITION_BY_REFERENCE_ITEM, SR_CONDITION_EMPTY_REPORT_CONTENT
from ....apisettings.sr.seriestag import DCMSR_SUPPORTED_TEMPLATE_IDS, SONADOR_SR_TEMPLATE_ID, DCMR_TEMPLATE_ID, \
	mapping_resource_for_template, layout_for_sop_class, DCMSR_LAYOUT_NESTED, DCMSR_LAYOUT_FLAT, \
	DCMSR_SOP_CLASS_ENHANCED, srcontent_strip_unnamed, srencode_image_reference
from ....serialization import ISO8601_DATETIME_FORMAT, DATETIME_FORMAT3, sonador_encode2str
from ...orthanc import ImagingSeries
from ...orthanc.base import ImagingResourceBaseCollection
from ...orthanc.sr import DcmSRSeries, DcmSRInstance

from .encoding import SonadorSRMeasurementReport
from .parse import srdoc_parse, srdoc_read, srdoc_document_class, srdoc_evidence_headers, srdoc_synthetic_evidence, srdoc_set_study, \
	srdoc_predecessor_items, srdoc_predecessors, SRPredecessor, SRParsedDocument, DCMSR_WRITER_CLASSES
from ....dcmcompat import require_oaktree_highdicom

# The SR document templates below pass `record_instances`, which only exists in the Oak-Tree
# highdicom fork. Fail at import, with an actionable message, rather than inside document creation.
require_oaktree_highdicom()

logger = logging.getLogger(__name__)


# Multiframe image SOP classes: a Series Tag reference to an instance of one of these carries a
# frame number (the first frame when the finding is created in Python), as the Viewer does.
DCM_MULTIFRAME_SOP_CLASSES = frozenset((
	'1.2.840.10008.5.1.4.1.1.2.1',		# Enhanced CT
	'1.2.840.10008.5.1.4.1.1.2.2',		# Legacy Converted Enhanced CT
	'1.2.840.10008.5.1.4.1.1.4.1',		# Enhanced MR
	'1.2.840.10008.5.1.4.1.1.4.3',		# Enhanced MR Color
	'1.2.840.10008.5.1.4.1.1.4.4',		# Legacy Converted Enhanced MR
	'1.2.840.10008.5.1.4.1.1.6.2',		# Enhanced US Volume
	'1.2.840.10008.5.1.4.1.1.3.1',		# Ultrasound Multi-frame
	'1.2.840.10008.5.1.4.1.1.7.1',		# Multi-frame Single Bit Secondary Capture
	'1.2.840.10008.5.1.4.1.1.7.2',		# Multi-frame Grayscale Byte Secondary Capture
	'1.2.840.10008.5.1.4.1.1.7.3',		# Multi-frame Grayscale Word Secondary Capture
	'1.2.840.10008.5.1.4.1.1.7.4',		# Multi-frame True Color Secondary Capture
	'1.2.840.10008.5.1.4.1.1.12.1',		# X-Ray Angiographic
	'1.2.840.10008.5.1.4.1.1.12.1.1',	# Enhanced XA
	'1.2.840.10008.5.1.4.1.1.12.2',		# X-Ray Radiofluoroscopic
	'1.2.840.10008.5.1.4.1.1.12.2.1',	# Enhanced XRF
	'1.2.840.10008.5.1.4.1.1.13.1.3',	# Breast Tomosynthesis
	'1.2.840.10008.5.1.4.1.1.20',		# Nuclear Medicine
	'1.2.840.10008.5.1.4.1.1.66.4',		# Segmentation
	'1.2.840.10008.5.1.4.1.1.77.1.5.4',	# Ophthalmic Tomography
	'1.2.840.10008.5.1.4.1.1.77.1.6',	# VL Whole Slide Microscopy
	'1.2.840.10008.5.1.4.1.1.128.1',	# Enhanced PET
	'1.2.840.10008.5.1.4.1.1.128.2',	# Legacy Converted Enhanced PET
	'1.2.840.10008.5.1.4.1.1.481.2',	# RT Dose
))

# Headers the writing program regenerates on every save: not carried through from a parsed document
DCMSR_HEADERS_REGENERATED = frozenset((
	'SOPInstanceUID', 'SOPClassUID', 'InstanceCreationDate', 'InstanceCreationTime', 'InstanceCreatorUID',
	'ContentDate', 'ContentTime', 'SeriesDate', 'SeriesTime', 'Manufacturer', 'ManufacturerModelName',
	'SoftwareVersions', 'DeviceSerialNumber', 'ContentSequence', 'ConceptNameCodeSequence', 'ContentTemplateSequence',
	'ValueType', 'ContinuityOfContent', 'CurrentRequestedProcedureEvidenceSequence', 'PertinentOtherEvidenceSequence',
	'PredecessorDocumentsSequence', 'VerifyingObserverSequence', 'ReferencedRequestSequence',
	'PerformedProcedureCodeSequence', 'ReferencedPerformedProcedureStepSequence', 'Modality', 'InstanceNumber',
	'SpecificCharacterSet', 'PixelData',
))

# Sentinel: `create_sr(previous=...)` links the parsed document by default
_PARSED_PREDECESSOR = object()


def dcm_header_text(ds, name, default=None):
	'''	Header value of a dataset as text.

		@input ds (pydicom.Dataset): dataset read
		@input name (str): header keyword
		@input default (default=None): value returned when the header is absent or empty

		@returns str: the value as a string, or `default`
	'''
	from ..headers import dcm_header_value
	value = dcm_header_value(ds, name)
	return default if value is None else str(value)


class SonadorBaseSR(metaclass=abc.ABCMeta):
	'''	Base class of the report models: the local data model of a DICOM-SR document together with
		the document-level state needed to read it and write it again.

		@attr supported_templates (tuple of str): root template identifiers the class parses
		@attr dcmsr_template_id (str): root template identifier written for new documents
		@attr diagnostics (list of SRDiagnostic): conditions met while parsing or writing
		@attr parsed (SRParsedDocument or None): result of the document reader when the model was built
			from a document (`from_sr`); None for a model built in Python
		@attr template_id, mapping_resource (str): root template of the document the model writes:
			the parsed document's on a re-save, the class default otherwise
		@attr dcmsr_document_template (highdicom SR class): document class written. On a re-save the
			parsed document's class, with Comprehensive SR mapped to Comprehensive 3D SR
			(`DCMSR_WRITER_CLASSES`)
		@attr person_observer (str or None): Person Observer Name written at the root: the parsed
			document's name on a re-save, the caller's otherwise
		@attr extra_items (tuple of pydicom.Dataset): root-level content items preserved from the parsed
			document and re-emitted after the TID 1500 items
	'''
	dcmsr_report_template = None
	dcmsr_document_template = None
	dcmsr_document_manufacturer = SONADOR_SR
	dcmsr_document_model_name = None
	dcmsr_document_software_version = None
	dcmsr_template_id = SONADOR_SR_TEMPLATE_ID
	supported_templates = DCMSR_SUPPORTED_TEMPLATE_IDS
	report_procedure = None

	def __init__(self, sonador_ref_series=None, sonador_series=None, dcm_evidence=None,
			observation_uid=None, report_procedure=None, parsed=None, **kwargs):
		'''	Initialize the SR model

			@input sonador_ref_series (sonador.imaging.orthanc.ImagingSeries, or a list, tuple or
				imaging server collection of ImagingSeriesCoreResource objects; any other container is
				rejected with TypeError): imaging series associated with the DICOM-SR
				report. IMPORTANT: for existing DICOM-SR resources (where sonador_series is provided)
				the reference series will be populated dynamically. For new series instances,
				the reference series (or multiple reference series) will be used in the creation
				of the DICOM-SR report.
			@input sonador_series (sonador.imaging.orthanc.DcmSRSeries, default=None):
				DcmSRSeries associated with the report content. When provided in the
				constructor, the SR model class can be used to retrieve and parse SR data
				stored in Sonador/Orthanc to local SR data object.
			@input parsed (SRParsedDocument, default=None): result of the document reader when the
				model is built from a document (see `from_sr`). It carries the root template, the
				observers, the preserved items, the evidence and the diagnostics of that document.

			@input dcmsr_report_template (highdicom.sr.Template): template instance to be used
				for rendering the report section of the document.
			@input dcmsr_document_template (highdicom.sr._SR subclass): template
				to be used for rending the DICOM-SR document container.
			@input template_id (str, default=the parsed document's, else the class default): root template
				identifier written
			@input person_observer (str, default=the parsed document's name): Person Observer Name written
				at the root

			@input observation_context_uid (str, default=new UID per document): Device Observer UID
			@input observation_context_device_name (str, default=class profile): Device Observer Name
			@input observation_context_manufacturer (str, default=class profile): Device Observer Manufacturer
			@input observation_context_model (str, default=class profile): Device Observer Model Name
			@input observation_context_serial_number (str, default=None): device serial number
			@input observation_context_device_role (highdicom.CodedConcept, default=None): device role
			@input observation_context_device_location (highdicom.CodedConcept, default=None): device location
		'''
		self.parsed = parsed
		self.diagnostics = list(parsed.diagnostics) if parsed is not None else list(kwargs.pop('diagnostics', None) or [])

		self.series = sonador_series
		self.ref_series = sonador_ref_series
		self._evidence = dcm_evidence
		self.record_instances = kwargs.get('record_instances', True)

		# DICOM-SR templates (report and document)
		self.dcmsr_report_template = kwargs.get('dcmsr_report_template', self.dcmsr_report_template)
		self.dcmsr_document_manufacturer = kwargs.get('dcmsr_document_manufacturer', self.dcmsr_document_manufacturer)
		self.dcmsr_document_template = kwargs.get('dcmsr_document_template') \
			or (srdoc_document_class(parsed.dataset) if parsed is not None else None) or self.dcmsr_document_template
		self.template_id = kwargs.get('template_id') or (parsed.template_id if parsed is not None else None) or self.dcmsr_template_id
		self.mapping_resource = kwargs.get('mapping_resource') \
			or (parsed.mapping_resource if (parsed is not None and parsed.template_id == self.template_id) else None) \
			or mapping_resource_for_template(self.template_id)
		self.person_observer = kwargs.get('person_observer', parsed.person_observer if parsed is not None else None)
		self.extra_items = tuple(kwargs.get('extra_items', parsed.extra_items if parsed is not None else ()) or ())

		procedures = report_procedure or (parsed.procedures if parsed is not None else None) or self.report_procedure
		if isinstance(procedures, (list, tuple)) and len(procedures) == 1:
			procedures = procedures[0]
		self.report_procedure = procedures or None

		# Observation context UID
		self._observation_context = kwargs.get('observeration_context')
		self.observation_context_uid = observation_uid
		self.observation_context_device_name = kwargs.get('observation_context_device_name') \
			or getattr(self, 'observation_context_device_name', None)
		self.observation_context_manufacturer = kwargs.get('observation_context_manufacturer') \
			or getattr(self, 'observation_context_manufacturer', None)
		self.observation_context_model = kwargs.get('observation_context_model') \
			or getattr(self, 'observation_context_model', None)
		self.observation_context_serial_number = kwargs.get('observation_context_serial_number') \
			or getattr(self, 'observation_context_serial_number', None)
		self.observation_context_device_role = kwargs.get('observation_context_device_role') \
			or getattr(self, 'observation_context_device_role', None)
		self.observation_context_device_location = kwargs.get('observation_context_device_location') \
			or getattr(self, 'observation_context_device_location', None)

		self._sr = None

		if not self.dcmsr_report_template:
			raise ConfigurationError(
				'Unable to initialize Sonador DICOM-SR model %s. No report template specified.' % type(self))
		if not self.dcmsr_document_template:
			raise ConfigurationError(
				'Unable to initialize Sonador DICOM-SR model %s. No document template specified.' % type(self))
		if not self.report_procedure and parsed is None:
			raise ConfigurationError(
				'Unable to initialize Sonador DICOM-SR model %s. No "report_procedure" code or sequence provided.' % type(self))

	# ---- read ----

	@classmethod
	def from_sr(cls, dcmsr, strict=False, diagnostics=None, **kwargs):
		'''	Build the report model of one DICOM-SR document. The reader accepts the nested, flat and
			legacy layouts and records every repair as a diagnostic; see `sonador.imaging.helpers.sr.parse`.

			@input dcmsr (pydicom.Dataset, highdicom SR object, bytes, file-like object, path or
				DcmSRInstance): the document. An imaging server instance is read through its cached
				file (one `file` request when the file is not cached yet).
			@input strict (bool, default=False): raise SRDiagnosticError on the first `warning` or `error`
				diagnostic
			@input diagnostics (list, default=new list): list the diagnostics are appended to
			@input kwargs: constructor arguments of `cls`

			@returns `cls` instance: the model, with `parsed`, `diagnostics`, `groups`, `report_title`,
				`report_procedure`, `person_observer`, `template_id` and `extra_items` populated
		'''
		return srdoc_parse(dcmsr, cls, strict=strict, diagnostics=diagnostics, **kwargs)

	@classmethod
	def _from_parsed(cls, parsed, **kwargs):
		'''	Model instance for the result of the document reader. Subclasses add their own data.

			@input parsed (SRParsedDocument): result of the document reader
			@returns `cls` instance
		'''
		return cls(parsed=parsed, **kwargs)

	@classmethod
	def parse(cls, dcmsr, server=None, strict=False, **kwargs):
		'''	Parse one or more DICOM-SR documents into a `DcmSRSeries`. Each `DcmSRInstance` of its
			`instances_collection` carries `report` (the parsed model, an instance of `cls`), `highdicom`
			(the highdicom object the model was parsed from) and `diagnostics`.

			@input dcmsr: one document (pydicom.Dataset, highdicom SR object, bytes, file-like object or
				path), a list or tuple of documents of one series (any other container is treated as one
				document and rejected by the reader), or an imaging server `DcmSRSeries` or
				`DcmSRInstance`, whose instances are parsed in place from their cached files
			@input server (SonadorServer, default=None): connection the series built for local documents
				is bound to; None for files
			@input strict (bool, default=False): raise SRDiagnosticError on the first `warning` or `error`
				diagnostic
			@input kwargs: constructor arguments of `cls`

			@returns sonador.imaging.orthanc.sr.DcmSRSeries: the series the documents belong to, with every
				instance parsed
		'''
		from ...orthanc.sr import dcmsr_local_series
		if isinstance(dcmsr, DcmSRSeries):
			series = dcmsr
		elif isinstance(dcmsr, DcmSRInstance):
			series = dcmsr.parent
		else:
			documents = list(dcmsr) if isinstance(dcmsr, (list, tuple)) else [dcmsr]
			series = dcmsr_local_series([srdoc_read(d) for d in documents], server=server)

		for instance in series.instances_collection:
			instance.parse(report_class=cls, strict=strict, **kwargs)
		return series

	# ---- write ----

	@abc.abstractmethod
	def create_sr(self):
		'''	Create an SR document from the data associated with the model using the document template

			@returns highdicom SR document (EnhancedSR, ComprehensiveSR or Comprehensive3DSR instance)
		'''

	@abc.abstractmethod
	def create_report_sr(self):
		'''	Create the SR report from the data associated with the model using the report template

			@returns highdicom.sr.templates.Template: the report content, a sequence holding the root
				CONTAINER item
		'''

	def dcmsr_series_headers(self, srdoc, ts:datetime.datetime, description=None, **kwargs):
		'''	Add identifying headers to the SR document: series date and time, Series Description, the
			equipment profile of the class (Manufacturer Model Name and Software Versions) and, on a
			re-save, the headers carried through from the parsed document (`_carry_headers`).

			@input srdoc (highdicom SR document): document the headers are set on
			@input ts (datetime.datetime): series date and time
			@input description (str, default=the parsed document's Series Description): Series Description

			@returns highdicom SR document: `srdoc`
		'''
		setattr(srdoc, DCMTS_SERIES.date_tag, ts.date())
		setattr(srdoc, DCMTS_SERIES.time_tag, ts.time())

		if description is None and self.parsed is not None:
			description = dcm_header_text(self.parsed.dataset, DCMHEADER_SERIES_DESCRIPTION)
		if description:
			setattr(srdoc, DCMHEADER_SERIES_DESCRIPTION, description)

		# Equipment profile: class attributes, overridden per call through the constructor
		if self.dcmsr_document_model_name:
			setattr(srdoc, DCMHEADER_MANUFACTER_MODEL_NAME, self.dcmsr_document_model_name)
			setattr(srdoc, DCMHEADER_SOFTWARE_VERSIONS, self.dcmsr_document_software_version or sonador_software_version())

		if self.parsed is not None:
			self._carry_headers(srdoc, self.parsed.dataset)

		return srdoc

	def _carry_headers(self, srdoc, parsed):
		'''	Copy to the new document the top-level headers of the parsed document that the new document
			does not set and the writing program does not regenerate (`DCMSR_HEADERS_REGENERATED`):
			unknown public tags, private tags and flags such as Content Qualification, verbatim.

			@input srdoc (highdicom SR document): document written
			@input parsed (pydicom.Dataset): document the model was built from

			@returns highdicom SR document: `srdoc`
		'''
		for elem in parsed:
			# Command set and file meta groups, and group length elements, never belong in the data set
			# (dcmjs-written documents may carry file meta values in group 0000)
			if elem.tag.group < 0x0008 or elem.tag.element == 0x0000 or elem.tag in srdoc:
				continue
			if elem.keyword in DCMSR_HEADERS_REGENERATED:
				continue
			srdoc[elem.tag] = copy.deepcopy(elem)
		return srdoc

	@property
	@abc.abstractmethod
	def evidence(self):
		'''	DICOM dataset to be evidenced in the intance.
		'''
	@property
	@abc.abstractmethod
	def sr(self):
		'''	SR report instance. For existing Sonador series, pulls the DICOM-SR instance from Sonador/Orthanc.
			For new reports, creates a cached copy of the report by calling create_sr method for the class.
		'''

	def observation_context(self, person_observer=None, device_observer_uid=None, **kwargs):
		'''	Create the DICOM-SR observation context: the Person Observer (the caller's, or the parsed
			document's on a re-save) followed by the Device Observer of the class profile.

			@input person_observer (str, default=self.person_observer): Person Observer Name; an empty
				string omits the item
			@input device_observer_uid (str, default=new UID): Device Observer UID
			@input kwargs: Device Observer attributes (`observation_context_*`), overriding the class profile

			@returns highdicom.sr.templates.ObservationContext
		'''
		_kwargs = {
			'device_name': kwargs.get('observation_context_device_name') or self.observation_context_device_name,
			'manufacturer_name': kwargs.get('observation_context_manufacturer') or self.observation_context_manufacturer,
			'model_name': kwargs.get('observation_context_model') or self.observation_context_model,
			'serial_number': kwargs.get('observation_context_serial_number') or self.observation_context_serial_number,
			'role_in_procedure': kwargs.get('observation_context_device_role') or self.observation_context_device_role,
		}
		_kwargs.update(omit(kwargs, [k for k in kwargs.keys() if 'observation_context' in k]))

		if person_observer is None:
			person_observer = self.person_observer

		return srencode_observation_context(
			context_uuid=device_observer_uid or self.observation_context_uid, person_name=person_observer or None, **_kwargs)

	@property
	def series(self):
		return self._series

	@series.setter
	def series(self, val):
		if val is not None and not isinstance(val, DcmSRSeries):
			raise TypeError('Sonador series reference must be a DcmSRSeries instance or None')

		self._series = val

	@property
	def ref_series(self):
		'''	Reference series of the report: the imaging series the findings and measurements apply to.
			For a model bound to an imaging server series, the series its documents reference, resolved
			from the parent study's cached series collection on first access.

			@returns
				- None when the report has no reference series
				- ImagingSeries for one reference series
				- list of ImagingSeries for several
		'''
		# Dynamically populate reference series
		if self._ref_series is None and self.series is not None and getattr(self.series, 'pacs', None) is not None:
			self.ref_series = list(self.series.reference_series_collection)

		return self._ref_series

	@ref_series.setter
	def ref_series(self, val):
		if val and not isinstance(val, (tuple, list, ImagingSeries, ImagingResourceBaseCollection)):
			raise TypeError('Invalid DICOM-SR reference series. Must be None, tuple, list, or ImagingSeries instance.')

		# Unpack reference series tuple/list
		if isinstance(val, (tuple, list, ImagingResourceBaseCollection)):
			val = list(val)

			# Set reference series according to what is linked against it.
			# * None for no links.
			# * ImagingSeries intance for one link.
			# * Iterable of ImagingSeries for more than one link.
			if len(val) == 0: self._ref_series = None
			elif len(val) == 1: self._ref_series = val[0]
			elif len(val) > 1: self._ref_series = val

		# Single value (ImagingSeries or None)
		elif val is None or isinstance(val, ImagingSeries):
			self._ref_series = val

		# Unsupported value
		else:
			raise TypeError('Unsupported DICOM-SR reference series. Type: "%s"' % type(val))

	@property
	def ref_series_list(self):
		'''	Reference series of the report as a list.

			@returns list of ImagingSeries: empty when the report has none
		'''
		ref = self.ref_series
		if ref is None:
			return []
		return list(ref) if isinstance(ref, (list, tuple)) else [ref]

	# ---- parsed document headers ----

	@property
	def parsed_dataset(self):
		'''	Document the model was built from.

			@returns pydicom.Dataset, or None for a model built in Python
		'''
		return self.parsed.dataset if self.parsed is not None else None

	@property
	def series_instance_uid(self):
		'''	Series Instance UID of the SR series the model writes to: the parsed document's (a re-save
			adds an instance to the same series), else that of the series the model is bound to.

			@returns str, or None for a model with neither
		'''
		if self.parsed is not None:
			return dcm_header_text(self.parsed.dataset, DCMHEADER_SERIES_INSTANCE_UID)
		if self.series is not None:
			return self.series.series_uid
		return None

	@property
	def sop_instance_uid(self):
		'''	SOP Instance UID of the parsed document.

			@returns str, or None for a model built in Python
		'''
		return dcm_header_text(self.parsed.dataset, DCMHEADER_SOP_INSTANCE_UID) if self.parsed is not None else None

	@property
	def predecessors(self):
		'''	Instances the next document written from the model names in its Predecessor Documents
			Sequence: the parsed document's own predecessors followed by the parsed document itself.

			@returns list of SRPredecessor: empty for a model built in Python
		'''
		if self.parsed is None:
			return []
		ds = self.parsed.dataset
		own = SRPredecessor(dcm_header_text(ds, DCMHEADER_STUDY_INSTANCE_UID), dcm_header_text(ds, DCMHEADER_SERIES_INSTANCE_UID),
			dcm_header_text(ds, DCMHEADER_SOP_CLASS_UID), dcm_header_text(ds, DCMHEADER_SOP_INSTANCE_UID))
		return list(self.parsed.predecessors) + [own]

	def has_by_reference_items(self):
		'''	Whether the model holds a preserved by-reference content item (Referenced Content Item
			Identifier) anywhere: at the root, in a group, under a finding or in its display attributes,
			at any depth of a preserved subtree. Such a model is never rewritten (`create_sr` raises).

			@returns bool
		'''
		return any(srcontent_holds_by_reference(item) for item in self._all_extra_items())

	def _all_extra_items(self):
		'''	Every preserved content item the model holds: the root items, then those of each group, of
			each finding and of both display containers of the finding.

			@returns iterator of pydicom.Dataset
		'''
		yield from self.extra_items
		for group in getattr(self, 'groups', None) or []:
			yield from getattr(group, 'extra_items', ())
			for finding in getattr(group, 'findings', None) or []:
				yield from getattr(finding, 'extra_items', ())
				display = getattr(finding, 'display', None)
				if display is not None:
					yield from getattr(display, 'extra_items', ())
					yield from getattr(display, 'color_extra_items', ())


class SonadorComprehensiveSR3D(SonadorBaseSR):
	'''	Report model for writing and reading measurements, findings and other structured data to and
		from DICOM-SR documents.

		A new document is a Comprehensive 3D SR with the Sonador root template (`Sonador-SR.1500`,
		mapping resource `Sonador-SR`). A model built from a document (`from_sr`) re-saves with the
		parsed document's template and in its document class: an Enhanced SR stays Enhanced SR, in the
		flat per-finding layout, unless its content needs Comprehensive 3D SR, in which case the
		document is upgraded with a `warning` diagnostic; a Comprehensive SR is written as
		Comprehensive 3D SR; a Comprehensive 3D SR stays Comprehensive 3D SR.

		Equipment profile and Device Observer: Manufacturer `Sonador`, model name `Sonador-IO`,
		software version the installed client version (`dev` from a source checkout), mirrored by a
		Device Observer context item. A Person Observer comes from the caller or, on a re-save, from
		the parsed document. The profile is held in class attributes, inherited by subclasses and
		overridden per call through the constructor arguments.

		@property reference_frames (ImageTransformMatrixCollection): reference frames
			(encoded as transform matrixes) associated with the report.
		@property measurements (MeasurementCollection): primary measurement collection for the report.
		@property findings (FindingCollection): primary findings collection for the report. A Series
			Tag finding is written as its own TID 1501 measurement group; the other findings go to the
			primary group.
		@property groups (ReportGroupCollection): secondary report groups that contain their own set
			of measurements and qualitative findings. For a model built from a document, the parsed
			groups, re-emitted as they were read.
	'''
	report_title = DCMSR_SONADOR_DATA_REPORT
	dcmsr_report_template = SonadorSRMeasurementReport
	dcmsr_document_template = Comprehensive3DSR

	# Equipment profile and Device Observer
	dcmsr_document_manufacturer = SONADOR_MANUFACTURER
	dcmsr_document_model_name = SONADOR_IO
	dcmsr_document_software_version = None			# `sonador_software_version()` at write time
	observation_context_device_name = SONADOR_IO
	observation_context_manufacturer = SONADOR_MANUFACTURER
	observation_context_model = SONADOR_IO

	primary_group_identifier = DCMSR_SONADOR_DATA_REPORT.value

	def __init__(self, reference_frames:Union[Sequence[ImageTransformMatrix],ImageTransformMatrixCollection]=None,
			points:Union[Sequence[ImageCoord],ImageCoordCollection]=None,
			measurements:Union[Sequence[Measurement],MeasurementCollection]=None,
			findings:Union[Sequence[Finding],FindingCollection]=None,
			groups:Union[Sequence[ReportGroup],ReportGroupCollection]=None,
			**kwargs):
		'''	Initialize comprehensive SR instance

			@input reference_frames (iterable of ImageTransformMatrix): transform matrices (representing DICOM
				reference frames) associated with the report. Added to the `RegistrationSequence` DICOM header
				of the encoded SR document. Refer to:
				https://dicom.nema.org/medical/Dicom/2016c/output/chtml/part03/sect_C.20.2.html
			@input measurements (iterable of Measurement): measurements of the report, written to the
				primary group
			@input findings (iterable of Finding): findings of the report; a Series Tag finding
				(`Finding.series_tag`) is written as its own measurement group
			@input groups (iterable of ReportGroup, default=the parsed document's groups): report groups,
				written as given
		'''
		# Report components
		parsed = kwargs.get('parsed')
		self.report_title = kwargs.get('report_title') or (parsed.title if parsed is not None else None) or self.report_title

		# Report data
		self.reference_frames = reference_frames
		self.measurements = measurements
		self.findings = findings
		self.groups = groups if groups is not None else (parsed.groups if parsed is not None else None)

		# Primary measurement group attributes
		self.primary_group_identifier = kwargs.get('primary_group_identifier') or self.primary_group_identifier
		self.primary_group_uid = kwargs.get('primary_group_uid') \
			or getattr(self, 'primary_group_uid', None)
		self.primary_group_method = kwargs.get('primary_group_method') \
			or getattr(self, 'primary_group_method', None)
		self.primary_group_finding_sites = kwargs.get('primary_group_finding_site') \
			or getattr(self, 'primary_group_finding_site', None)
		self.primary_group_algorithm = kwargs.get('primary_group_algorithm') \
			or getattr(self, 'primary_group_algorithm', None)
		self.primary_group_finding_category = kwargs.get('primary_group_finding_category') \
			or getattr(self, 'primary_group_finding_category', None)

		# Initialize base class
		super().__init__(**omit(kwargs, ('report_title',)))

	@property
	def reference_frames(self):
		return self._reference_frames

	@reference_frames.setter
	def reference_frames(self, val):
		''' Add reference frame (transform matrix instnces) to report model
		'''
		# Convert to collection
		if val and not isinstance(val, ImageTransformMatrixCollection):
			val = ImageTransformMatrixCollection([tx for tx in val])

		self._reference_frames = val

	@property
	def measurements(self):
		'''	Add measurements to report model
		'''
		return self._measurements

	@measurements.setter
	def measurements(self, val):
		'''	Add measurements to report model
		'''
		# Convert to collection
		if val and not isinstance(val, MeasurementCollection):
			val = MeasurementCollection([m for m in val])

		self._measurements = val

	@property
	def findings(self):
		return self._findings

	@findings.setter
	def findings(self, val):
		'''	Add findings to report model
		'''
		# Convert to collection
		if val and not isinstance(val, FindingCollection):
			val = FindingCollection([m for m in val])

		self._findings = val

	@property
	def groups(self):
		return self._groups

	@groups.setter
	def groups(self, val):
		'''	Add groups to report model
		'''
		# Convert to collection
		if val and not isinstance(val, ReportGroupCollection):
			val = ReportGroupCollection([g for g in val])

		self._groups = val

	@property
	def series_tag_findings(self):
		'''	Series Tag findings of the model: those of `findings` first, then those of every group, in
			order.

			@returns list of Finding
		'''
		found = [f for f in (self.findings or []) if f.is_series_tag]
		for group in (self.groups or []):
			found.extend(f for f in (getattr(group, 'findings', None) or []) if f.is_series_tag)
		return found

	def _imaging_measurement_groups(self, **kwargs):
		'''	Measurement groups of the report, in writing order. The measurements and the findings of
			`findings` that are not Series Tags form the "primary" group, placed first. Each Series Tag
			finding of `findings` becomes one TID 1501 group with the tracking identifier of the Series
			Tag tool and the finding's tracking UID (generated when the finding has none). The report
			groups of `groups` follow as they are; the meta groups and the volume point groups among
			them are written separately (`_meta_groups`, `_volume_measurement_groups`).

			@input kwargs: options of the primary group, prefixed `primary_group_*`

			@returns list of ReportGroup
		'''
		groups = kwargs.get('groups', [])
		findings = list(self.findings or [])
		primary_findings = [f for f in findings if not f.is_series_tag]
		tag_findings = [f for f in findings if f.is_series_tag]

		if primary_findings or self.measurements:
			_pgroup_kwargs = {
				'tracking_id': kwargs.get('primary_group_tacking_id') or TrackingIdentifier(
					uid=kwargs.get('primary_group_uid') or self.primary_group_uid,
					identifier=kwargs.get('primary_group_identifier') or self.primary_group_identifier),
				'findings': FindingCollection(primary_findings) if primary_findings else None, 'measurements': self.measurements,
				'finding_sites': kwargs.get('primary_site_finding_sites') or self.primary_group_finding_sites,
				'method': kwargs.get('primary_group_method') or self.primary_group_method,
				'algorithm': kwargs.get('primary_group_algorithm') or self.primary_group_algorithm,
				'finding_category': kwargs.get('primary_group_finding_category') or self.primary_group_finding_category,
				'finding_type': kwargs.get('primary_group_finding_category') or self.primary_group_finding_category,
			}
			_pgroup_kwargs.update(omit(kwargs, [k for k in kwargs.keys() if 'primary_group' in k]))

			# Filter out null or empty arguments
			groups.append(ReportGroup(**pick(_pgroup_kwargs, _pgroup_kwargs.keys())))

		for finding in tag_findings:
			if not finding.tracking_uid:
				finding.tracking_uid = generate_uid()
			groups.append(ReportGroup(findings=FindingCollection([finding]),
				tracking_id=finding.tracking_id, tracking_uid=finding.tracking_uid))

		groups.extend(g for g in (self.groups or []) if (isinstance(g, ReportGroup) and not isinstance(g, VolumePointCollectionGroup)))
		return groups

	def _meta_groups(self, **kwargs):
		'''	Iterable of all meta groups associated with the report
		'''
		return [g for g in (self.groups or []) if isinstance(g, ReportMetaGroup)]

	def _volume_measurement_groups(self, **kwargs):
		'''	Iterable of all volume groups associated with the report
		'''
		return [g for g in (self.groups or []) if isinstance(g, VolumePointCollectionGroup)]

	def _image_references(self):
		'''	Distinct image references of the Series Tag findings, in order, for the Image Library.

			@returns list of ImageReference: one per referenced SOP instance
		'''
		references = OrderedDict()
		for finding in self.series_tag_findings:
			ref = finding.reference
			if ref is not None and ref.sop_instance_uid and ref.sop_instance_uid not in references:
				references[ref.sop_instance_uid] = ref
		return list(references.values())

	def _image_library(self, references):
		'''	TID 1600 Image Library container holding one IMAGE item per referenced instance, without
			entry descriptors, in the form the Sonador Viewer writes.

			@input references (iterable of ImageReference): the referenced instances

			@returns
				- highdicom.sr.value_types.ContainerContentItem: the Image Library
				- None when there are no references
		'''
		if not references:
			return None
		group = ContainerContentItem(name=dcmcodes.DCM.ImageLibraryGroup, relationship_type=RelationshipTypeValues.CONTAINS)
		group.ContentSequence = ContentSequence([
			srencode_image_reference(ref._replace(frame_number=None), relationship_type=RelationshipTypeValues.CONTAINS)
			for ref in references])
		library = ContainerContentItem(name=dcmcodes.DCM.ImageLibrary, relationship_type=RelationshipTypeValues.CONTAINS)
		library.ContentSequence = ContentSequence([group])
		return library

	def create_report_sr(self, layout=None, person_observer=None, device_observer_uid=None, strict=False, **kwargs):
		'''	Create the DICOM-SR report content (TID 1500) from the model data: the observation context,
			the Image Library of the Series Tag references, the measurement groups, then the preserved
			root items of the parsed document.

			A model with no measurement group, meta group, point collection or preserved root item still
			yields a report: its root holds the observation context and the procedure reported, and the
			document built from it carries its evidence, series headers and any encapsulated payload. The
			`warning` `empty-report-content` is recorded on `diagnostics`, so that a caller for whom an
			empty content tree is a mistake can refuse it with `strict=True`.

			@input layout (str, default=`DCMSR_LAYOUT_NESTED`): per-finding property layout the document
				class permits (`DCMSR_LAYOUT_FLAT` for Enhanced SR)
			@input person_observer (str, default=self.person_observer): Person Observer Name
			@input device_observer_uid (str, default=new UID): Device Observer UID
			@input strict (bool, default=False): raise SRDiagnosticError on the `empty-report-content`
				warning instead of writing the report
			@input kwargs: report template arguments (a `report_` prefix is stripped), observation context
				arguments (`observation_context_*`) and primary group options (`primary_group_*`)

			@returns highdicom.sr.templates.Template: the report content, a sequence holding the root
				CONTAINER item
			@raises SRDiagnosticError under `strict` for a report without content
		'''
		layout = layout or DCMSR_LAYOUT_NESTED

		# Replace report_ in report key names.
		for kw in list(kwargs.keys()):
			if 'report_' in kw:
				kwargs[kw.replace('report_', '')] = kwargs.pop(kw)

		observation_context = self._observation_context or self.observation_context(
			person_observer=person_observer, device_observer_uid=device_observer_uid,
			**pick(kwargs, tuple(k for k in kwargs.keys() if 'observation_context' in k)))
		imaging_groups = self._imaging_measurement_groups(**pick(kwargs, tuple(k for k in kwargs.keys() if 'primary_group' in k)))
		meta_groups = self._meta_groups()
		volume_groups = self._volume_measurement_groups()
		if not (imaging_groups or meta_groups or volume_groups or self.extra_items):
			sr_diagnostic(self.diagnostics, SR_DIAGNOSTIC_WARNING, SR_CONDITION_EMPTY_REPORT_CONTENT,
				'the report has no measurement group, meta group, point collection or preserved root item; '
				+ 'the document carries its header items, evidence and series headers only', strict=strict)

		report = self.dcmsr_report_template(
			title=self.report_title, procedure_reported=self.report_procedure,
			observation_context=observation_context,
			imaging_measurements=[g.create_sr(layout=layout) for g in imaging_groups],
			meta_groups=[g.sr for g in meta_groups],
			volume_measurements=[g.sr for g in volume_groups],
			**omit(kwargs, tuple(k for k in kwargs.keys() if 'observation_context' in k or 'primary_group' in k)))

		# Image Library before the Imaging Measurements container; preserved root items last
		root = report[0]
		library = self._image_library(self._image_references())
		if library is not None:
			position = next((i for i, item in enumerate(root.ContentSequence)
				if item.name == dcmcodes.DCM.ImagingMeasurements), len(root.ContentSequence))
			root.ContentSequence.insert(position, library)
		for item in srcontent_extra_items(self.extra_items):
			root.ContentSequence.append(item)

		return report

	@staticmethod
	def _requires_comprehensive(item, parent=None):
		'''	Whether a content tree holds an SCOORD3D item, or a CONTAINER under a CODE or NUM item,
			neither of which an Enhanced SR document can hold (PS3.3 Table A.35.2-2).

			@input item (content item): root of the tree checked
			@input parent (content item, default=None): parent of `item`

			@returns bool
		'''
		value_type = item.get('ValueType')
		if value_type == 'SCOORD3D':
			return True
		if value_type == 'CONTAINER' and parent is not None and parent.get('ValueType') in ('CODE', 'NUM'):
			return True
		return any(SonadorComprehensiveSR3D._requires_comprehensive(child, item) for child in (item.get('ContentSequence') or []))

	# ---- evidence ----

	@staticmethod
	def _instance_reference(series, instance):
		'''	Image reference to an imaging server instance, built from its cached JSON (SOP Class and SOP
			Instance UID from the main tags). When the cached tags lack either UID the instance file is
			read instead (`dcmfile(cache=True)`: one `file` request, cached afterwards). The reference
			carries frame number 1 for a multiframe SOP class (`DCM_MULTIFRAME_SOP_CLASSES`), else none.

			@input series (ImagingSeries): series the instance belongs to
			@input instance (DcmInstance): the instance

			@returns ImageReference
		'''
		sop_class = instance.sop_class_uid
		sop_instance = instance.sop_instance_uid
		if not sop_class or not sop_instance:
			ds = instance.dcmfile(cache=True)
			sop_class, sop_instance = str(ds.SOPClassUID), str(ds.SOPInstanceUID)
		frame = 1 if str(sop_class) in DCM_MULTIFRAME_SOP_CLASSES else None
		return ImageReference(str(sop_class), str(sop_instance), frame, series.series_uid)

	def series_reference(self, series):
		'''	Image reference a Series Tag on `series` carries: the first instance of the series (`dcm0`).

			@input series (ImagingSeries): the tagged series
			@returns ImageReference
		'''
		return self._instance_reference(series, series.dcm0)

	def _series_for_finding(self, finding):
		'''	Reference series a finding applies to: the one matching the finding's `series_uid`, else the
			only reference series of the report.

			@input finding (Finding)
			@returns ImagingSeries
			@raises ValueError when the finding names a series that is not a reference series, or when
				it names none and the report has several
		'''
		candidates = self.ref_series_list
		if finding.series_uid:
			for sx in candidates:
				if sx.series_uid == finding.series_uid:
					return sx
			raise ValueError('Finding %r references series %s, which is not a reference series of the report'
				% (finding, finding.series_uid))
		if len(candidates) == 1:
			return candidates[0]
		raise ValueError('Finding %r carries no image reference and the report has %d reference series; '
			'set `reference` or `series_uid` on the finding' % (finding, len(candidates)))

	def _assign_references(self):
		'''	Set the image reference of every Series Tag finding that has none to the reference of its
			series (`series_reference`). Modifies the findings in place.
		'''
		for finding in self.series_tag_findings:
			if finding.reference is None or not finding.reference.sop_instance_uid:
				finding.reference = self.series_reference(self._series_for_finding(finding))

	def _evidence_headers_from_series(self, series):
		'''	Patient and study attributes for synthesised evidence, read from the cached JSON of the
			series' parent study: the attributes highdicom requires (empty when the server has no value)
			plus every other patient and study tag the JSON carries, which highdicom copies into the
			document. No request is made.

			@input series (ImagingSeries): a reference series

			@returns tuple (OrderedDict, str): the headers by keyword and the Study Instance UID
		'''
		study = series.parent
		headers = OrderedDict()
		headers.update(srdoc_evidence_headers(study.patientdata))
		for name, value in srdoc_evidence_headers(study.dicomdata).items():
			if value is not None:
				headers[name] = value
		for source in (study.patientdata, study.dicomdata):
			for name, value in (source or {}).items():
				if name not in headers and value is not None and name != DCMHEADER_STUDY_INSTANCE_UID:
					headers[name] = value
		return headers, study.study_uid

	@property
	def evidence(self):
		'''	Evidence datasets highdicom records in the document (Current Requested Procedure Evidence
			Sequence for the document's study, Pertinent Other Evidence Sequence for other studies).
			Built on first access and cached.

			For reference series of an imaging server with `record_instances=True`: the instance files
			of every reference series, downloaded (one `file` request per instance). With
			`record_instances=False`, the exchange profile: datasets synthesised from the cached patient,
			study, series and instance JSON for the first instance of each reference series and the
			instances the Series Tags reference. That costs no request when the cached instance JSON
			carries the SOP Class and SOP Instance UIDs (a study populated with
			`bulkpopulate_related(child_instances=True)` on the development Orthanc build); an instance
			whose cached tags lack them is fetched once (`dcmfile(cache=True)`).

			For a model built from a document and bound to no reference series: datasets synthesised
			from the parsed document's own evidence and from the Series Tag references, each under the
			study it was recorded in.

			@returns list of pydicom.Dataset, or None for a model with neither reference series nor a
				parsed document
		'''
		if getattr(self, '_evidence', None) is None and self.ref_series:
			series_list = self.ref_series_list

			if self.record_instances:
				self._evidence = list(itertools.chain(*tuple(tuple(dcm for dcm in sx.instances_collection.dcmfiles) for sx in series_list)))
			else:
				evidence = []
				for sx in series_list:
					headers, study_uid = self._evidence_headers_from_series(sx)
					references = [self.series_reference(sx)]
					references.extend(f.reference for f in self.series_tag_findings
						if f.reference is not None and f.reference.series_uid == sx.series_uid
						and f.reference.sop_instance_uid != references[0].sop_instance_uid)
					evidence.extend(srdoc_synthetic_evidence(headers, study_uid, references))
				self._evidence = evidence

		elif getattr(self, '_evidence', None) is None and self.parsed is not None:
			ds = self.parsed.dataset
			headers = srdoc_evidence_headers(ds)
			study_uid = dcm_header_text(ds, DCMHEADER_STUDY_INSTANCE_UID)
			references = [ref for ref in self.parsed.evidence.values() if ref.sop_instance_uid]
			known = set(ref.sop_instance_uid for ref in references)
			for finding in self.series_tag_findings:
				ref = finding.reference
				if ref is not None and ref.sop_instance_uid and ref.sop_instance_uid not in known:
					references.append(ref)
					known.add(ref.sop_instance_uid)
			self._evidence = srdoc_synthetic_evidence(headers, study_uid, references)

		return getattr(self, '_evidence', None)

	# ---- document ----

	def create_sr(self, dcmsr_series_uid=None, dcmsr_instance_uid=None,
			dcmsr_series_number=None, dcmsr_instance_number=1, ts=None, is_complete=None,
			is_final=None, is_verified=None, series_headers_kwargs=None, record_instances=None,
			person_observer=None, device_observer_uid=None, template_id=None, previous=_PARSED_PREDECESSOR,
			document_template=None, strict=False, **kwargs):
		'''	Create the DICOM-SR document from the model data: the report content (`create_report_sr`),
			the evidence, the document flags, the predecessor links, the root template and the series
			headers. A re-save (a model built from a document) keeps the parsed document's study,
			series, flags, template and headers, and names the parsed document as a predecessor.

			@input dcmsr_series_uid (str, default=the parsed document's series, else a new UID): Series
				Instance UID
			@input dcmsr_instance_uid (str, default=new UID): SOP Instance UID; new for every save
			@input dcmsr_series_number (int, default=the parsed document's, else 1): Series Number
			@input dcmsr_instance_number (int, default=1): Instance Number
			@input ts (datetime.datetime, default=now, UTC): series date and time
			@input is_complete, is_final, is_verified (bool, default=the parsed document's flags, else
				True, False, False): Completion, Preliminary and Verification flags; a verified parsed
				document carries its first verifying observer through
			@input series_headers_kwargs (dict, default=None): arguments of `dcmsr_series_headers`
			@input record_instances (bool, default=self.record_instances): download every instance of the
				reference series as evidence and record them all (one `file` request per instance). When
				False the evidence is synthesised from the cached JSON (the first instance of each
				reference series and the instances the Series Tags reference), which costs no request
				when the cached instance JSON carries the SOP identifiers and one `file` request per
				instance whose tags lack them (see `evidence`). The referenced instances are always
				recorded in the evidence when the document holds Series Tags, because readers resolve the
				series of a tag from them.
			@input person_observer (str, default=self.person_observer): Person Observer Name
			@input device_observer_uid (str, default=new UID): Device Observer UID
			@input template_id (str, default=self.template_id): root template identifier; `1500` pairs
				with mapping resource `DCMR`, `Sonador-SR.1500` with `Sonador-SR`
			@input previous (default=the parsed document): instance(s) the document supersedes, as a
				dataset, a `DcmSRInstance` or a list of those; their own predecessors are carried through.
				None writes no predecessor link.
			@input document_template (highdicom SR class, default=self.dcmsr_document_template): document
				class. An Enhanced SR whose content Enhanced SR cannot hold is written as Comprehensive
				3D SR with a `warning` diagnostic (`document-class-upgraded`).
			@input strict (bool, default=False): refuse a report without content (`create_report_sr`)
				instead of writing it with the `empty-report-content` warning
			@input kwargs: further document class, report template, observation context and primary
				group arguments (see `create_report_sr`)

			@returns highdicom SR document (EnhancedSR or Comprehensive3DSR instance), also kept as the
				model's last document (`sr`)

			@raises SRDiagnosticError when the model holds a by-reference content item (such a document is
				never rewritten), or under `strict` for a report without content
			@raises ConfigurationError when no procedure reported is set
			@raises ValueError when a Series Tag finding has no image reference and no reference series
				to take one from, or when the model has no evidence source at all
		'''
		if self.has_by_reference_items():
			raise SRDiagnosticError(SRDiagnostic(SR_DIAGNOSTIC_ERROR, SR_CONDITION_BY_REFERENCE_ITEM, None, None,
				'The document holds a by-reference content item (Referenced Content Item Identifier); rewriting '
				+ 'the content tree would invalidate its target, so the document is not rewritten.'))
		if not self.report_procedure:
			raise ConfigurationError('Unable to create DICOM-SR instance. No "report_procedure" code provided.')

		if record_instances is None:
			record_instances = self.record_instances
		if self.ref_series:
			self._assign_references()
		for finding in self.series_tag_findings:
			if finding.reference is None or not finding.reference.sop_instance_uid:
				raise ValueError('Series Tag finding %r carries no image reference and the report has no reference '
					'series to take one from; set `reference` on the finding or bind reference series' % (finding,))
		if not getattr(self, '_evidence', None) and not self.ref_series and self.parsed is None:
			raise ValueError('Unable to create DICOM-SR instance. No `evidence` avaialble, '
				+ 'and no reference series for the model has been defined.')

		ts = ts or datetime.datetime.utcnow()
		parsed = self.parsed.dataset if self.parsed is not None else None

		# Document class and the layout it permits; an Enhanced SR save whose content needs
		# Comprehensive 3D SR is upgraded with a warning
		document_class = document_template or self.dcmsr_document_template or Comprehensive3DSR
		layout = DCMSR_LAYOUT_FLAT if document_class is EnhancedSR else DCMSR_LAYOUT_NESTED
		report_kwargs = pick(kwargs, tuple(k for k in kwargs.keys() if 'report' in k or 'observation_context' in k or 'primary_group' in k))
		content = self.create_report_sr(layout=layout, person_observer=person_observer,
			device_observer_uid=device_observer_uid, strict=strict, **report_kwargs)
		if document_class is EnhancedSR and self._requires_comprehensive(content[0]):
			sr_diagnostic(self.diagnostics, SR_DIAGNOSTIC_WARNING, SR_CONDITION_DOCUMENT_CLASS_UPGRADED,
				'the content holds items an Enhanced SR document cannot carry; written as Comprehensive 3D SR',
				value=EnhancedSR.__name__)
			document_class = Comprehensive3DSR
			content = self.create_report_sr(layout=DCMSR_LAYOUT_NESTED, person_observer=person_observer,
				device_observer_uid=device_observer_uid, **report_kwargs)

		# Flags: the parsed document's, unless given
		if is_complete is None:
			is_complete = (dcm_header_text(parsed, 'CompletionFlag') == 'COMPLETE') if parsed is not None else True
		if is_final is None:
			is_final = (dcm_header_text(parsed, 'PreliminaryFlag') == 'FINAL') if parsed is not None else False
		verifying = {}
		if is_verified is None:
			is_verified = False
			if parsed is not None and dcm_header_text(parsed, 'VerificationFlag') == 'VERIFIED':
				observers = parsed.get('VerifyingObserverSequence') or []
				if observers:
					is_verified = True
					verifying = {
						'verifying_observer_name': observers[0].get('VerifyingObserverName'),
						'verifying_organization': observers[0].get('VerifyingOrganization'),
					}

		# Predecessors: the parsed document (and its own predecessors) by default
		predecessors = []
		if previous is _PARSED_PREDECESSOR:
			predecessors = self.predecessors
		elif previous:
			for item in (previous if isinstance(previous, (list, tuple)) else [previous]):
				ds = srdoc_read(item)
				predecessors.extend(srdoc_predecessors(ds))
				predecessors.append(SRPredecessor(dcm_header_text(ds, DCMHEADER_STUDY_INSTANCE_UID),
					dcm_header_text(ds, DCMHEADER_SERIES_INSTANCE_UID), dcm_header_text(ds, DCMHEADER_SOP_CLASS_UID),
					dcm_header_text(ds, DCMHEADER_SOP_INSTANCE_UID)))

		series_uid = dcmsr_series_uid or self.series_instance_uid or generate_uid()
		if dcmsr_series_number is None:
			dcmsr_series_number = int(dcm_header_text(parsed, DCMHEADER_SERIES_NUMBER, 1) or 1) if parsed is not None else 1

		# Initialize SR document instance
		_srdoc = document_class(
			evidence=self.evidence, content=content,
			series_instance_uid=series_uid, sop_instance_uid=dcmsr_instance_uid or generate_uid(),
			series_number=dcmsr_series_number, instance_number=dcmsr_instance_number,
			manufacturer=self.dcmsr_document_manufacturer, is_complete=is_complete, is_final=is_final, is_verified=is_verified,
			record_instances=record_instances or bool(self.series_tag_findings), **verifying,
			**omit(kwargs, tuple(k for k in kwargs.keys() if 'report' in k or 'observation_context' in k or 'primary_group' in k)))

		# The image references are written without a concept name, as the Viewer does (the placeholder
		# name is needed while highdicom collects the evidence; the document shares the item objects)
		srcontent_strip_unnamed(_srdoc)

		# A re-save stays in the parsed document's study: highdicom took the study from the first
		# evidence dataset, which belongs to another study when the parent recorded no evidence of
		# its own; the evidence is filed again against the document's study
		if parsed is not None:
			srdoc_set_study(_srdoc, dcm_header_text(parsed, DCMHEADER_STUDY_INSTANCE_UID))

		# Root template and mapping resource (highdicom hard-codes `1500` / `DCMR`)
		template_id = template_id or self.template_id
		_srdoc.ContentTemplateSequence[0].TemplateIdentifier = template_id
		_srdoc.ContentTemplateSequence[0].MappingResource = self.mapping_resource if template_id == self.template_id \
			else mapping_resource_for_template(template_id)

		if predecessors:
			_srdoc.PredecessorDocumentsSequence = DcmSequence(srdoc_predecessor_items(predecessors))

		# Apply series headers to report instance
		self.dcmsr_series_headers(_srdoc, ts, **(series_headers_kwargs or {}))
		self._sr = _srdoc
		return _srdoc

	def dcmsr_series_headers(self, srdoc, *args, **kwargs):
		'''	Add the series headers of the base class and the reference frames of the model (Registration
			Sequence) to the SR document. A model without reference frames keeps the parsed document's,
			carried through by the base class.

			@returns highdicom SR document: `srdoc`
		'''
		# Add headers from parent instance
		srdoc = super().dcmsr_series_headers(srdoc, *args, **kwargs)

		# Add reference frames (the parsed document's are carried by the base class when the model has none)
		if self.reference_frames:
			setattr(srdoc, DCMHEADER_REGISTRATION_SEQUENCE, DcmSequence([rf.sr for rf in self.reference_frames]))

		return srdoc

	@property
	def sr(self):
		'''	SR representation of the model.

			@returns
				- DcmSRSeries: the imaging server series the model is bound to
				- highdicom SR document: otherwise, the document the model last created (`create_sr` is
					called on first access)
		'''
		if self.series is not None:
			return self.series
		if self._sr is None:
			self._sr = self.create_sr()
		return self._sr

	@classmethod
	def _from_parsed(cls, parsed, **kwargs):
		'''	Model instance for the result of the document reader: the parsed groups become `groups`;
			the title, procedures, observers, template and preserved items are taken from `parsed`.

			@input parsed (SRParsedDocument): result of the document reader
			@returns `cls` instance
		'''
		return cls(parsed=parsed, groups=parsed.groups, **kwargs)


class SonadorImagingMeasurementReport(SonadorComprehensiveSR3D):
	'''	Report model of the TID 1500 "Imaging Measurement Report" documents the Sonador Viewer
		exchanges: the report title is `(126000, DCM, "Imaging Measurement Report")`, which the
		Viewer's report detection matches. Series Tag findings are written one measurement group
		each; the procedure reported is supplied by the caller.
	'''
	report_title = dcmcodes.DCM.ImagingMeasurementReport
