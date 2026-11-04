'''	SR data classes of the Sonador client: the local data model that DICOM-SR documents are parsed
	to and written from.

	* `SRDataObject` / `SRDataCollectionMember` / `SRDataObjectCollection`: base classes. Every object
		encodes itself with `create_sr` and exposes the result as `.sr`, built on every access.
	* Spatial data: `ImageTransformMatrix`, `ImageCoord` and their collections.
	* Qualitative and quantitative data: `Finding`, `Measurement` and their collections. A `Finding`
		carries the Sonador-SR 0.3 per-finding properties of a Series Tag (`ImageReference`, tracking
		identifiers, extended meta, display attributes, preserved `extra_items`) and is read from a
		content item with `from_sr`.
	* Report groups: `ReportGroup` (TID 1501, also read with `from_sr`), `ReportMetaGroup`,
		`VolumePointCollectionGroup` and `ReportGroupCollection`.

	The constants these classes depend on are in `sonador.apisettings.base.constants`; the package
	`sonador.apisettings.base` re-exports both modules.
'''
import abc, functools, logging, numbers
from collections import namedtuple, OrderedDict
from typing import Sequence, Union

from pydicom import Sequence as DcmSequence
from pydicom.sr.codedict import codes as dcmcodes
from pydicom.uid import generate_uid

from highdicom.sr.templates import Code, CodedConcept, MeasurementsAndQualitativeEvaluations
from highdicom.sr.value_types import ContainerContentItem, CodeContentItem, TextContentItem, NumContentItem

from client.utils.object import pick, omit
from client.utils.microservices import JsonObjectCollection

from .constants import DCMSR_UNITS_MM

logger = logging.getLogger(__name__)


# SR Data Structures


class SRDataObject:
	'''	Structured reporting data object. Provides methods and properties for working with
		local data within Sonador.

		@type (str, default=None): string which describes the type of SR object.
			(Return value depends on the object type, and may not be defined.)
	'''
	def _init_srdata(self, object_type=None, codes:Sequence[Union[Code,CodedConcept]]=None):
		self._type = object_type
		self._codes = codes

	@property
	def type(self):
		return self._type

	@property
	def codes(self):
		return self._codes


class SRDataCollectionMember(SRDataObject):
	'''	Structured reporting data object which can be associated with a collection.
	'''
	def __init__(self, collection=None, 
			object_type=None, codes:Sequence[Union[Code,CodedConcept]]=None, **kwargs):
		self._init_srdata(object_type=object_type, codes=codes)
		self._init_collection(collection=collection)

	def _init_collection(self, collection=None):
		self.collection = collection

	@abc.abstractmethod
	def create_sr(self):
		'''	Encode a DICOM-SR representation of the collection
		'''	

	@property
	def sr(self):
		'''	SR encoded representation of the data object. Built on every access, so a model that was
			parsed and then edited re-emits its current state.

			@returns the result of `create_sr` (a highdicom content item, template or sequence, by class)
		'''
		return self.create_sr()


class SRDataObjectCollection(SRDataObject, JsonObjectCollection):
	'''	Collection of SR objects
	'''
	model = SRDataObject
	object_type_attr = 'object_type'

	def __init__(self, *args, object_type=None, codes:Sequence[Union[Code,CodedConcept]]=None, **kwargs):
		self._init_srdata(object_type=object_type, codes=codes)
		super().__init__(*args, **kwargs)

	def _init_collection_modelinstance(self, *args, **kwargs):
		'''	Initialize collection model instance
		'''
		return self.model(*args, **kwargs)

	def _init_collection_models(self, **kwargs):
		'''	Initialize collection models
		'''
		# Add object type and codes to model instance
		if not kwargs.get(self.object_type_attr) and self.type:
			kwargs[self.object_type_attr] = self.type
		if not kwargs.get('codes') and self.codes:
			kwargs['codes'] = self.codes

		# Add a reference to the collection
		if not kwargs.get('collection'):
			kwargs['collection'] = self

		def _init_model(data):
			'''	Initialize model instance from data
				1. If data is already a model, return instance
				2. If a dict (or structured dict), initialize data object
			'''
			if isinstance(data, self.model) \
				or (getattr(self, 'basemodel', None) and isinstance(data, getattr(self, 'basemodel', None))):
				return data

			# Tuple instance
			elif isinstance(data, (tuple, list)):
				return self._init_collection_modelinstance(*data, **kwargs)

			# Dictionary instance
			elif isinstance(data, (dict, OrderedDict)):

				# Determine if any keys need to be omitted.
				# Keys defined in the data dictionary should be used
				# over collection attributes.
				_okw = []
				if data.get('codes'): _okw.append('codes')

				# Ad collection keyword arguments to the data dict, omit any
				# keywords which are duplicated between collection and intance
				data.update(omit(pick(kwargs, kwargs.keys()), _okw))
				return self._init_collection_modelinstance(**data)

			raise NotImplementedError(
				'Unsupported data type "%s". Model data must be a dict or a model instance.' % type(data))

		return map(_init_model, self._objectdata)

	@abc.abstractmethod
	def create_sr(self):
		'''	Encode a DICOM-SR representation of the collection
		'''

	@property
	def sr(self):
		'''	SR encoded representation of the collection, built on every access.

			@returns the result of `create_sr` (a sequence or tuple of encoded members, by class)
		'''
		return self.create_sr()


class ImageTransformMatrix(SRDataCollectionMember):
	''' Helper class for representing DICOM frame of reference frame transform matrices.		
	'''
	def __init__(self, txmatrix, uid=None, transform_type=None, comment=None, 
			fiducials=None, ref_images=None, **kwargs):
		'''	Initialize transformation matrix
		'''
		super().__init__(object_type=transform_type, **kwargs)

		self._matrix = txmatrix
		self._uid = uid
		self._comment = comment
		self._fiducials = fiducials
		self._img_refs = ref_images

	@property
	def matrix(self):
		return self._matrix

	@property
	def uid(self):
		return self._uid

	@property
	def comment(self):
		return self._comment

	@property
	def fiducials(self):
		return self._fiducials

	@property
	def ref_images(self):
		return self._img_refs

	def create_sr(self, **kwargs):
		from ..sr import dcm_encode_reference_frame_transform_matrix
		return dcm_encode_reference_frame_transform_matrix(self.matrix, 
			reference_uid=self.uid, transform_type=self.type, transform_comment=self.comment,
			fiducials=self.fiducials, ref_images=self.ref_images, codes=self.codes, **kwargs)


class ImageTransformMatrixCollection(SRDataObjectCollection):
	''' Collection of reference frame transform matrices
	'''
	model = ImageTransformMatrix
	object_type_attr = 'transform_type'

	def create_sr(self):
		''' Create a DICOM representation of the collection

			@returns pydicom.Sequence
		'''
		return DcmSequence(m.sr for m in self)


class ImageCoord(SRDataCollectionMember):
	'''	Helper class for working with DICOM (spatial) coordinates
	'''
	def __init__(self, x, y, z, reference_frame=None, name=None, point_type=None, **kwargs):
		'''	Initialize image coordinate
		'''
		super().__init__(object_type=point_type, **kwargs)
		self._name = name

		# Coordinate values
		self.x = x
		self.y = y
		self.z = z

		# Coordinate reference frame
		self._ref_frame = reference_frame

	@property
	def _pts(self):
		return (self.x, self.y, self.z)

	@property
	def name(self):
		return self._name

	@property
	def reference_frame(self):
		return self._ref_frame

	def __iter__(self):
		yield from self._pts

	def __str__(self):
		return '(x=%s,y=%s,z=%s)' % self._pts

	def create_sr(self, **kwargs):
		from ..sr import srencode_coord3d
		return srencode_coord3d(
			self.reference_frame.uid if self.reference_frame else generate_uid(), self, 
			name=self.name, point_type=self.type, **kwargs)


class ImageCoordCollection(SRDataObjectCollection):
	'''	Collection of coordinates
	'''
	model = ImageCoord
	object_type_attr = 'point_type'

	def __init__(self, *args, reference_frame=None, name=None, point_type=None, **kwargs):
		self._ref_frame = reference_frame
		self._name = name
		super().__init__(*args, object_type=point_type, **kwargs)

	def _init_collection_models(self, *args, **kwargs):
		if not kwargs.get('reference_frame') and self.reference_frame:
			kwargs['reference_frame'] = self.reference_frame

		return super()._init_collection_models(*args, **kwargs)

	def create_sr(self, **kwargs):
		'''	Create a DICOM-SR encoded representation of the collection
		'''
		from ..sr import srencode_coord3d
		return srencode_coord3d(
			self.reference_frame.uid if self.reference_frame else generate_uid(), self, 
			name=self.name, point_type=self.type, **kwargs)

	@property
	@functools.lru_cache()
	def array(self):
		from ..sr import points2array
		return points2array(self)

	@property
	def reference_frame(self):
		return self._ref_frame

	@property
	def name(self):
		return self._name


# Image referenced by a finding: one SOP instance (frame number only for multiframe classes) and the
# series it belongs to, resolved from the document evidence when read from a document.
# `study_uid` is the study the referenced instance belongs to, as recorded in a document's evidence
# sequences (None when unknown: the document's own study is assumed when writing evidence)
ImageReference = namedtuple('ImageReference', ('sop_class_uid', 'sop_instance_uid', 'frame_number', 'series_uid', 'study_uid'))
ImageReference.__new__.__defaults__ = (None, None, None)

DCMSR_FINDING = dcmcodes.DCM.Finding


class Finding(SRDataCollectionMember):
	'''	Qualitative finding associated with a resource: a simplified implementation of the DICOM-SR
		TID 1501 "Finding" item, extended with the Sonador-SR 0.3 per-finding properties of a Series
		Tag (a coded concept applied to a whole imaging series, with optional description, text,
		location and display colours).

		@property name (Code or CodedConcept): concept name of the finding item (`DCMSR_FINDING` for
			Series Tags)
		@property finding (Code or CodedConcept): coded value, carried verbatim (any designator, version,
			value length or meaning length)
		@property type (str): measurement type the finding routes to (the `finding_type` argument;
			`SONADOR_MEASUREMENT_TYPE_SERIES_TAG` for Series Tags)
		@property tracking_id (str): human readable tracking identifier of the measurement group the
			finding is written to (the Cornerstone tool string for Series Tags)
		@property tracking_uid (str): tracking unique identifier, stable across revisions of the finding
		@property reference (ImageReference): image the finding was made on; a Series Tag applies to the
			whole series of that image
		@property description, text, location (str): Sonador-SR 0.3 extended meta attributes
		@property display (sonador.apisettings.display_attrs.DisplayAttributes): display attributes
		@property extra_items (tuple of pydicom.Dataset): unmodelled content items read from under the
			finding, re-emitted after the modelled children on write
		@property series, instance, sr_instance: model objects the finding was aligned with when it was
			retrieved from an imaging server (`ImagingSeries`, `DcmInstance`, `DcmSRInstance`); None for
			findings created locally. Not part of the JSON form or of equality.
	'''
	def __init__(self, name, finding, finding_type=None, tracking_id=None, tracking_uid=None, reference=None,
			description=None, text=None, location=None, display=None, extra_items=None, series_uid=None, **kwargs):
		'''	Initialize finding

			@input name (highdicom.Code or highdicom.CodedConcept): name of the finding
			@input finding (highdicom.Code or highdicom.CodedConept): finding value
			@input finding_type (str, default=None): measurement type; derived from `tracking_id` when the
				identifier is a known Cornerstone tool string
			@input tracking_id (str, default=None): tracking identifier; the Series Tag tool string when the
				finding type is `series-tag` and none is given
			@input tracking_uid (str, default=None): tracking unique identifier (generated on write when None)
			@input reference (ImageReference or pydicom.Dataset, default=None): referenced image (a
				Referenced SOP Sequence item is accepted)
			@input series_uid (str, default=None): series the finding applies to when `reference` carries none
		'''
		from ..sr.seriestag import measurement_type_for_tracking_id, tracking_id_for_measurement_type
		if finding_type is None and tracking_id:
			finding_type = measurement_type_for_tracking_id(tracking_id)
		if tracking_id is None and finding_type:
			tracking_id = tracking_id_for_measurement_type(finding_type)

		super().__init__(object_type=finding_type, **kwargs)
		self._name = name
		self._finding = finding
		self.tracking_id = tracking_id
		self.tracking_uid = tracking_uid
		self.reference = reference
		self.description = description
		self.text = text
		self.location = location
		self.display = display
		self.extra_items = tuple(extra_items or ())

		if series_uid and self.reference is not None and not self.reference.series_uid:
			self.reference = self.reference._replace(series_uid=series_uid)
		self._series_uid = series_uid

		# Links to the model objects of an imaging server (set by the retrieval layer)
		self.series = None
		self.instance = None
		self.sr_instance = None

	@property
	def name(self):
		return self._name

	@property
	def finding(self):
		return self._finding

	@property
	def value(self):
		'''	Code value of the finding.

			@returns str, or None for a value without one
		'''
		return getattr(self.finding, 'value', None)

	@property
	def meaning(self):
		'''	Code meaning of the finding, verbatim.

			@returns str, or None for a value without one
		'''
		return getattr(self.finding, 'meaning', None)

	@property
	def reference(self):
		return self._reference

	@reference.setter
	def reference(self, val):
		'''	Set the image reference. A Referenced SOP Sequence item (dataset or dict) is converted to an
			`ImageReference`.
		'''
		if val is not None and not isinstance(val, ImageReference):
			from ..sr.seriestag import image_reference_from_dataset
			val = image_reference_from_dataset(val)
		self._reference = val

	@property
	def series_uid(self):
		'''	Series Instance UID the finding applies to: that of the image reference when it carries one,
			else the `series_uid` given.

			@returns str, or None
		'''
		if self.reference is not None and self.reference.series_uid:
			return self.reference.series_uid
		return self._series_uid

	@series_uid.setter
	def series_uid(self, val):
		if self.reference is not None:
			self.reference = self.reference._replace(series_uid=val)
		self._series_uid = val

	@property
	def display(self):
		return self._display

	@display.setter
	def display(self, val):
		'''	Set the display attributes. A colour-role dictionary (hexadecimal or RGB values) is converted
			to a `DisplayAttributes` instance.
		'''
		from ..display_attrs import DisplayAttributes
		if val is not None and not isinstance(val, DisplayAttributes):
			val = DisplayAttributes.from_color(val)
		self._display = val

	@property
	def is_series_tag(self):
		'''	Whether the finding is a Series Tag.

			@returns bool
		'''
		from ..sr.seriestag import SONADOR_MEASUREMENT_TYPE_SERIES_TAG
		return self.type == SONADOR_MEASUREMENT_TYPE_SERIES_TAG

	@classmethod
	def series_tag(cls, tag, reference=None, **kwargs):
		'''	Series Tag finding for a tag (a `sonador.imaging.orthanc.tags.Tag`, a Code or a CodedConcept).

			@input tag: tag whose concept is the finding value. A `Tag` model also provides the default
				display colours of the finding from its `Meta` document.
			@input reference (ImageReference, default=None): image the tag was applied on; when None the
				writer references the first instance (`dcm0`) of the reference series.
			@input kwargs: further constructor arguments (`description`, `text`, `location`, `display`,
				`series_uid`, `tracking_uid`)

			@returns Finding (an instance of `cls`)
		'''
		from ..sr.seriestag import SONADOR_MEASUREMENT_TYPE_SERIES_TAG
		concept = getattr(tag, 'concept', tag)
		if kwargs.get('display') is None and getattr(tag, 'display_color', None):
			kwargs['display'] = tag.display_color
		return cls(DCMSR_FINDING, concept, finding_type=SONADOR_MEASUREMENT_TYPE_SERIES_TAG,
			reference=reference, **kwargs)

	@classmethod
	def from_sr(cls, item, group=None, strict=False, diagnostics=None, **kwargs):
		'''	Build a finding from a Finding content item (CODE) of a measurement group. The nested, flat
			and legacy layouts of the per-finding properties are accepted; repairs are recorded as
			diagnostics (see `sonador.apisettings.sr.seriestag.srparse_finding`).

			@input item (pydicom.Dataset): the Finding content item
			@input group (pydicom.Dataset, default=None): the measurement group holding the item; needed
				to read the legacy layout, where the per-finding properties are group-level siblings
			@input strict (bool, default=False): raise SRDiagnosticError on `warning` and `error`
				diagnostics
			@input diagnostics (list, default=None): list the diagnostics are appended to

			@returns Finding (an instance of `cls`)
		'''
		from ..sr.seriestag import srparse_finding
		return srparse_finding(item, group=group, strict=strict, diagnostics=diagnostics, finding_class=cls, **kwargs)

	def create_sr(self, **kwargs):
		from ..sr import srencode_finding
		return srencode_finding(self, **kwargs)

	def __repr__(self):
		return '%s(%s, tracking_uid=%s)' % (type(self).__name__, self.finding, self.tracking_uid)


class FindingCollection(SRDataObjectCollection):
	'''	Collection of qualitative findings
	'''
	model = Finding
	object_type_attr = 'finding_type'

	@classmethod
	def from_sr(cls, group, strict=False, diagnostics=None, **kwargs):
		'''	Findings of a measurement group (TID 1501): one per Finding content item, with the per-finding
			properties read from the nested, flat or legacy layout.

			@input group (pydicom.Dataset): Measurement Group content item
			@input strict (bool, default=False): raise SRDiagnosticError on `warning` and `error`
				diagnostics
			@input diagnostics (list, default=None): list the diagnostics are appended to

			@returns FindingCollection (an instance of `cls`): empty for a group without findings
		'''
		from ..sr.seriestag import srparse_findings
		return srparse_findings(group, strict=strict, diagnostics=diagnostics, collection_class=cls, **kwargs)

	def create_sr(self, **kwargs):
		'''	Return a DICOM-SR encoded iterator for all findings in the collection
		'''
		return tuple(f.create_sr(**kwargs) for f in self)


class Measurement(SRDataCollectionMember):
	'''	Numerical measurement associated with a resource. (Provides a simplified 
		implementation DICOM-SR TID:3000.) 

		Note: Within the Sonador library, only two fields are required: value and unit. 
		To persist to measuremets to DICOM-SR, an additional parameter `name`, must also be provided.
		(Refer to __init__ arguments list for all available parameters and types.)

		@property value (number): measured value
		@property unit (highdicom.Code or highdicom.CodedConcept): measurement unit
		@property uid: user assigned ID for tracking the measurement
	'''
	def __init__(self, val:Union[int,float,numbers.Number], unit=DCMSR_UNITS_MM, name=None, measurement_type=None, 
			qualifier=None, tracking_id=None, algorithm=None, derivation=None,  method=None, 
			finding_sites=None, properties=None, ref_images=None, ref_value=None, **kwargs):
		'''	Initialize measurement

			@input val (numbers.Number): numeric measurement value
			@input unit (highdicom.Code or highdicom.CodedConcept, default='mm'): Unit of measurement
			@input name (highdicom.Code or highdicom.CodedConcept, default=None): Name of the measurement
			@input qualifier (highdicom.Code or highdicom.CodedConcept, default=None): 
				Qualification of numeric measurement value or qualitative description.
			@input tracking_id (default=None): user assigned ID for tracking the value in reports
			@input algorithm (default=None): description of the algoithm used for making the measurement
			@input derivation (dault=None): how the value was computed
			@input method (default=None): measurement method
			@input finding_sites (default=None): coded description of one or more anatomic locations 
				associated with the measurement
			@input properties (default=None): meaurement properties, including evaluations of significance,
				relationship to a reference population, and its range.
			@input ref_images (default=None): referenced images which were used as source for the meaurement
			@input ref_value (default=None): reference real world value map
		'''
		super().__init__(object_type=measurement_type, **kwargs)
		self._val = val
		self._unit = unit
		self._name = name
		self._qualifier = qualifier
		self._uid = tracking_id
		self._algorithm = algorithm
		self._derivation = derivation
		self._method = method
		self._finding_sites = finding_sites
		self._properties = properties
		self._img_refs = ref_images
		self._val_ref = ref_value

	@property
	def value(self):
		return self._val

	@property
	def unit(self):
		return self._unit

	@property
	def uid(self):
		return self._uid

	@property
	def qualifier(self):
		return self._qualifier

	@property
	def name(self):
		return self._name

	@property
	def algorithm(self):
		return self._algorithm

	@property
	def derivation(self):
		return self._derivation

	@property
	def method(self):
		return self._method

	@property
	def finding_sites(self):
		return self._finding_sites

	@property
	def properties(self):
		return self._properties

	@property
	def ref_images(self):
		return self._img_refs

	@property
	def ref_value(self):
		return self._val_ref

	def create_sr(self, **kwargs):
		from ..sr import srencode_measurement
		return srencode_measurement(self, **kwargs)


class MeasurementCollection(SRDataObjectCollection):
	'''	Collection of measurements.
	'''
	model = Measurement
	object_type_attr = 'measurement_type'

	def create_sr(self, **kwargs):
		'''	Return a DICOM-SR encoded iterator for all measurements in the collection
		'''
		return tuple(m.create_sr(**kwargs) for m in self)


class ReportBaseGroup(SRDataCollectionMember):
	'''	Provides a general structure for grouping data together into a DICOM-SR report section.
	'''
	sr_template = None

	def __init__(self, *args, tracking_id=None, tracking_uid=None, extra_items=None, **kwargs):
		''' Initialize report group

			@input tracking_id (str or highdicom.sr.TrackingIdentifier, default=None): user assigned ID
				for tracking the value in reports
			@input tracking_uid (str, default=None): tracking unique identifier paired with a string
				`tracking_id` (generated on write when None)
			@input extra_items (iterable of pydicom.Dataset, default=None): unmodelled content items read
				from the group, re-emitted after the modelled items on write

			@input sr_template (subclass of highdicom.sr.templates._MeasurementsAndQualitativeEvaluations):
				SR template that should be used to encode the report group. By default, TID-1501 which provide
				support for qualitative findings and quantitative measurements is used.
		'''
		self.sr_template = kwargs.pop('sr_template', self.sr_template)
		self._uid = tracking_id
		self._tracking_uid = tracking_uid
		self.extra_items = tuple(extra_items or ())

		super().__init__(*args, **kwargs)

		if not self.sr_template:
			raise ValueError('Unable to initialize %s, invalid SR template class' % type(self))

	@property
	def uid(self):
		return self._uid

	@property
	def tracking_id(self):
		'''	Human readable tracking identifier of the group.

			@returns str, or None
		'''
		from ..sr.seriestag import tracking_identifier_parts
		return tracking_identifier_parts(self._uid)[0]

	@property
	def tracking_uid(self):
		'''	Tracking unique identifier of the group.

			@returns str, or None until the group is written when none was assigned
		'''
		from ..sr.seriestag import tracking_identifier_parts
		return tracking_identifier_parts(self._uid)[1] or self._tracking_uid

	def create_sr(self, *args, **kwargs):
		from ..sr import srencode_report_group
		if not self.uid:
			raise ValueError('A user defined tracking ID is required to create a DICOM-SR instance.')

		return srencode_report_group(self.uid, sr_template=self.sr_template, tracking_uid=self._tracking_uid,
			extra_items=self.extra_items, **kwargs)


class ReportMetaGroup(ReportBaseGroup):
	'''	Provides a general structure for metadata, coded concepts, and other information
		to be added to a report template.
	'''
	def __init__(self, meta:Sequence[Union[TextContentItem,NumContentItem,CodeContentItem]], *args, **kwargs):
		'''	Initialize report group

			@input meta (iterable of highdicom.ContentItem instances): content items to be added to
				the report group.
		'''
		self._meta = meta
		super().__init__(*args, **kwargs)

	@property
	def meta(self):
		return self._meta

	@property
	def sr_template(self):
		'''	Dynamic property for retrieving the SR template. Required by Sonaor in order
			to prevent circular imports. Uses sonador.imaging.sr.helpers.encoding.ReportMetaGroup
			as the default SR template.
		'''
		if getattr(self, '_sr_template', None):
			return self._sr_template

		from ...imaging.helpers.sr.encoding import ReportMetaGroup as DcmReportMetaGroup
		return DcmReportMetaGroup

	@sr_template.setter
	def sr_template(self, val):
		self._sr_template = val

	def create_sr(self, **kwargs):
		return super().create_sr(meta=self.meta, **kwargs)


class ReportGroup(ReportBaseGroup):
	'''	Groups quantitative measurements and qualitative findings together into a report section.
		(Provides a simplified implementation of DICOM-SR TID:1501.)
	'''
	sr_template = MeasurementsAndQualitativeEvaluations

	def __init__(self, *args, findings:FindingCollection=None, measurements:MeasurementCollection=None,
			finding_type=None, finding_sites=None, finding_category=None, algorithm=None, method=None, ref_value=None, **kwargs):
		'''	Initialize report group	
	
			@input tracking_id (default=None): user assigned ID for tracking the value in reports
			@input finding_type (highdicom.Code or highdicom.CodedConcept, default=None): type of observed
				finding associated with the group
			@input finding_category (highdicom.Code or highdicom.CodedConcept, default=None): category
				of the observed findings (eg, anatomic structure or morphologically abnormal structure)
			@input measurements (MeasurementCollection, default=None): measurements to be added to the group
			@input findings (FindingCollection, default=None): findings to be added to the group
			@input algorithm (default=None): description of the algorithm used for making the measurement or findings.
				(Findings and measurement may futher have their own algorithm descriptions embedded.)
			@input finding_sites (default=None): coded description of one or more anatomic locations 
				associated with the measurement and findings
			@input ref_value (default=None): reference real world value map
		'''
		# Data properties
		self._finding_category = finding_category
		self._measurements = measurements
		self._findings = findings
		self._algorithm = algorithm
		self._method = method
		self._finding_sites = finding_sites
		self._val_ref = ref_value

		super().__init__(*args, object_type=finding_type, **kwargs)

	@property
	def finding_category(self):
		return self._finding_category

	@property
	def measurements(self):
		return self._measurements

	@property
	def findings(self):
		return self._findings

	@property
	def algorithm(self):
		return self._algorithm

	@property
	def method(self):
		return self._method

	@property
	def finding_sites(self):
		return self._finding_sites

	@property
	def ref_value(self):
		return self._val_ref

	@property
	def measurement_type(self):
		'''	Measurement type the group routes to by its tracking identifier.

			@returns str (`SONADOR_MEASUREMENT_TYPE_*`), or None for a generic group
		'''
		from ..sr.seriestag import measurement_type_for_tracking_id
		return measurement_type_for_tracking_id(self.tracking_id)

	@classmethod
	def from_sr(cls, group, strict=False, diagnostics=None, **kwargs):
		'''	Build a report group from a Measurement Group content item (TID 1501): the tracking identifier
			and UID, the findings (`Finding.from_sr`), and every other child preserved as `extra_items`.

			@input group (pydicom.Dataset): Measurement Group content item
			@input strict (bool, default=False): raise SRDiagnosticError on `warning` and `error`
				diagnostics
			@input diagnostics (list, default=None): list the diagnostics are appended to

			@returns ReportGroup (an instance of `cls`)
		'''
		from ..sr.seriestag import srparse_report_group
		return srparse_report_group(group, strict=strict, diagnostics=diagnostics, group_class=cls, **kwargs)

	def create_sr(self, **kwargs):
		# A parsed group carries its finding type on the Finding items themselves; a group created in
		# Python with `finding_type` keeps the TID 1501 "Finding" item highdicom writes for it.
		finding_type = self.type if isinstance(self.type, (Code, CodedConcept)) else None
		return super().create_sr(
			measurements=self.measurements, findings=self.findings, referenced_real_world_value_map=self.ref_value,
			method=self.method, algorithm_id=self.algorithm, finding_type=finding_type, finding_category=self.finding_category,
			finding_sites=self.finding_sites, **kwargs)


class VolumePointCollectionGroup(ReportGroup):
	'''	Specialized report group (container) which is able to encode spatial points and associated measurements/findings.
		Extension of DICOM-SR TID:1501. (Uses TID:Sonador-1001 DICOM-SR template by default.)
	'''
	def __init__(self, *args, points:Sequence[Union[ImageCoord,ImageCoordCollection]]=None, **kwargs):
		'''	Initialize report group.
			(Refer to sonador.apisettings.ReportGroup for full list of init arguments.)

			@input points (iterable of ImageCoordCollection or ImageCoord instances)
		'''
		super().__init__(*args, **kwargs)
		self._points = points

	@property
	def points(self):
		return self._points

	@property
	def sr_template(self):
		'''	Dynamic property for retrieving the SR template. Required by Sonaor in order
			to prevent circular imports. Uses sonador.imaging.sr.helpers.encoding.VolumetricPointCollection
			as the default SR template.
		'''
		if getattr(self, '_sr_template', None):
			return self._sr_template

		from ...imaging.helpers.sr.encoding import VolumetricPointCollection
		return VolumetricPointCollection

	@sr_template.setter
	def sr_template(self, val):
		self._sr_template = val		

	def create_sr(self, **kwargs):
		return super().create_sr(points=[p.sr for p in self.points], **kwargs)


class ReportGroupCollection(SRDataObjectCollection):
	'''	Collection of report groups
	'''
	basemodel = ReportBaseGroup
	model = ReportGroup
	object_type_attr = 'finding_type'

	def create_sr(self, **kwargs):
		'''	Create a DICOM-SR content iterable of all groups in the collection
		'''
		return tuple(g.create_sr(**kwargs) for g in self)

	def _init_collection_modelinstance(self, *args, **kwargs):
		'''	Initialize collection model instance. If `points` is in the list of
			arguments, return a VolumePointCollectionGroup instance rather than the
			default model type associated with the collection.
		'''
		if 'points' in kwargs:
			return VolumePointCollectionGroup(*args, **kwargs)
		elif 'meta' in kwargs:
			return ReportMetaGroup(*args, **kwargs)

		return super()._init_collection_modelinstance(*args, **kwargs)


EUCLID_COORD_ORIGIN = ImageCoord(0, 0, 0)
