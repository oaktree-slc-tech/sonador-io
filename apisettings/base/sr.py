'''	SR data classes of the Sonador client: the local data model that DICOM-SR documents are parsed
	to and written from.

	* `SRDataObject` / `SRDataCollectionMember` / `SRDataObjectCollection`: base classes. Every object
		encodes itself with `create_sr` and exposes the result as `.sr`, built on every access.
	* Spatial data: `ImageTransformMatrix`, `ImageCoord` and their collections.
	* Qualitative and quantitative data: `Finding`, `Measurement` and their collections. A `Finding`
		carries the Sonador-SR 0.3 per-finding properties of a Series Tag (`ImageReference`, tracking
		identifiers, extended meta, display attributes, preserved `extra_items`) and is read from a
		content item with `from_sr`. A `Measurement` carries the same attributes for a NUM item, plus
		its other NUM values and its coordinates (`Geometry`).
	* Typed quantitative tools: `LengthMeasurement`, `AngleMeasurement`, `CobbAngleMeasurement`, on
		`GeometricMeasurement`; `measurement_class_for_type` selects the class a document reads to.
	* Report groups: `ReportGroup` (TID 1501, also read with `from_sr`), `ReportMetaGroup`,
		`VolumePointCollectionGroup` and `ReportGroupCollection`.

	The constants these classes depend on are in `sonador.apisettings.base.constants`; the package
	`sonador.apisettings.base` re-exports both modules.
'''
import abc, functools, logging, math, numbers
from collections import namedtuple, OrderedDict
from typing import Sequence, Union

import numpy as np

from pydicom import Sequence as DcmSequence
from pydicom.sr.codedict import codes as dcmcodes
from pydicom.uid import generate_uid

from highdicom.sr.templates import Code, CodedConcept, MeasurementsAndQualitativeEvaluations
from highdicom.sr.value_types import ContainerContentItem, CodeContentItem, TextContentItem, NumContentItem

from client.utils.object import pick, omit
from client.utils.microservices import JsonObjectCollection

from .constants import DCMSR_UNITS_MM, DCMSR_UNITS_ANGLE_DEGREE, SONADOR_MEASUREMENT_TYPE_LENGTH, SONADOR_MEASUREMENT_TYPE_ANGLE, \
	SONADOR_MEASUREMENT_TYPE_COBB_ANGLE

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


DCMSR_TRANSFORM_TOLERANCE = 1e-6
DCMSR_TRANSFORM_UNIT_SCALES = {'mm': 1.0, 'm': 1000.0}


class ImageTransformMatrix(SRDataCollectionMember):
	''' Helper class for representing DICOM frame of reference frame transform matrices. The matrix is
		a 4x4 in the DICOM Frame of Reference Transformation Matrix layout (row-major, column-vector
		convention: `p_target = M . [p_source, 1]`).

		`convert` and `invert` move points between frames. They accept rigid transforms only and
		refuse any other with an error naming the check that failed. The translation of the matrix
		is in millimetres; `source_unit` declares the unit of the source points.

		@property source_frame (str or None): label or UID of the frame the points are given in
		@property target_frame (str or None): Frame of Reference UID the points are converted to
		@property source_unit (str): unit of the source points, `mm` or `m`
	'''
	def __init__(self, txmatrix, uid=None, transform_type=None, comment=None,
			fiducials=None, ref_images=None, source_frame=None, target_frame=None, source_unit='mm', **kwargs):
		'''	Initialize transformation matrix

			@input txmatrix (array-like): transform matrix (any layout `mtx4x4` accepts)
			@input source_frame (str, default=None): label or UID of the source frame
			@input target_frame (str, default=None): Frame of Reference UID of the target frame
			@input source_unit (str, default='mm'): unit of the source points, `mm` or `m`

			@raises ValueError for any other source unit
		'''
		super().__init__(object_type=transform_type, **kwargs)

		if source_unit not in DCMSR_TRANSFORM_UNIT_SCALES:
			raise ValueError('Unsupported source unit: %s (supported: mm, m)' % source_unit)

		self._matrix = txmatrix
		self._uid = uid
		self._comment = comment
		self._fiducials = fiducials
		self._img_refs = ref_images
		self.source_frame = source_frame
		self.target_frame = target_frame
		self.source_unit = source_unit

	def rigid_matrix(self):
		'''	The matrix as a 4x4 array, after checking that it is rigid: bottom row `0 0 0 1`, an
			orthonormal rotation part, determinant +1.

			@returns numpy.ndarray of shape (4, 4)
			@raises ValueError naming the failed check (`finite values`, `homogeneous row`, `orthonormal
				rotation` for scale or shear, `determinant +1` for a reflection)
		'''
		from ..sr import mtx4x4
		matrix = mtx4x4(np.asarray(self._matrix, dtype=float))
		if not np.all(np.isfinite(matrix)):
			raise ValueError('Transform refused: it fails the finite values check')
		if not np.allclose(matrix[3], (0, 0, 0, 1), atol=DCMSR_TRANSFORM_TOLERANCE):
			raise ValueError('Transform refused: it fails the homogeneous row check (bottom row must be 0 0 0 1)')
		rotation = matrix[:3, :3]
		if not np.allclose(rotation.dot(rotation.T), np.identity(3), atol=DCMSR_TRANSFORM_TOLERANCE):
			raise ValueError('Transform refused: it fails the orthonormal rotation check (scale or shear is not rigid)')
		if abs(np.linalg.det(rotation) - 1.0) > DCMSR_TRANSFORM_TOLERANCE:
			raise ValueError('Transform refused: it fails the determinant +1 check (a reflection is not rigid)')
		return matrix

	def convert(self, points):
		'''	Convert points from the source frame to the target frame. Points in metres are scaled to
			millimetres before the matrix is applied.

			@input points (sequence of (x, y, z)): points in the source frame, in `source_unit`
			@returns tuple of tuple of 3 float: points in the target frame, in millimetres
			@raises ValueError for a transform that is not rigid
		'''
		matrix = self.rigid_matrix()
		scale = DCMSR_TRANSFORM_UNIT_SCALES[self.source_unit]
		source = np.asarray(points, dtype=float) * scale
		homogeneous = np.column_stack((source, np.ones(len(source))))
		return tuple(tuple(float(c) for c in p[:3]) for p in matrix.dot(homogeneous.T).T)

	def invert(self, points):
		'''	Convert points from the target frame back to the source frame with the matrix inverse.

			@input points (sequence of (x, y, z)): points in the target frame, in millimetres
			@returns tuple of tuple of 3 float: points in the source frame, in `source_unit`
			@raises ValueError for a transform that is not rigid
		'''
		inverse = np.linalg.inv(self.rigid_matrix())
		scale = DCMSR_TRANSFORM_UNIT_SCALES[self.source_unit]
		target = np.asarray(points, dtype=float)
		homogeneous = np.column_stack((target, np.ones(len(target))))
		return tuple(tuple(float(c) / scale for c in p[:3]) for p in inverse.dot(homogeneous.T).T)

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


class _DisplayAttributesMixin:
	'''	`display` attribute shared by findings and measurements.
	'''
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


class Finding(_DisplayAttributesMixin, SRDataCollectionMember):
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


# Quantitative measurements

DCMSR_GEOMETRY_DOMAIN_IMAGE = 'image'
DCMSR_GEOMETRY_DOMAIN_PATIENT = 'patient'
DCMSR_GRAPHIC_POLYLINE = 'POLYLINE'

# Uncalibrated image measurements are stored in pixels, under the unit code dcmjs writes
DCMSR_UNITS_PIXEL = Code('1', 'UCUM', 'px')

# Concept names written for the quantitative tools
DCMSR_CONCEPT_LENGTH = Code('G-D7FE', 'SRT', 'Length')
DCMSR_CONCEPT_COBB_ANGLE = Code('285285000', 'SCT', 'Cobb angle')

# A coordinate difference below this is treated as coincident points
DCMSR_GEOMETRY_COINCIDENT_TOLERANCE = 1e-6


class Geometry(namedtuple('Geometry', ('domain', 'graphic_type', 'points', 'reference'))):
	'''	Coordinates a measurement was made from. The domain read from a document is the domain written
		back: image coordinates are never promoted to patient coordinates.

		@property domain (str): `DCMSR_GEOMETRY_DOMAIN_IMAGE` (SCOORD) or `DCMSR_GEOMETRY_DOMAIN_PATIENT`
			(SCOORD3D)
		@property graphic_type (str): DICOM graphic type (`POLYLINE`, `CIRCLE`, `ELLIPSE`, ...)
		@property points (tuple of tuple of float): ordered points as drawn: `(column, row)` pixel pairs
			or `(x, y, z)` patient triples in millimetres
		@property reference (ImageReference or None): referenced SOP instance and frame of the image
			domain; None in the patient domain
	'''
	__slots__ = ()

	def __new__(cls, domain, graphic_type, points, reference=None):
		'''	Validate and normalise the coordinates.

			@input domain (str): `DCMSR_GEOMETRY_DOMAIN_IMAGE` or `DCMSR_GEOMETRY_DOMAIN_PATIENT`
			@input graphic_type (str): DICOM graphic type
			@input points (iterable of sequences of numbers): ordered points
			@input reference (ImageReference, default=None): image the points were drawn on

			@returns Geometry
			@raises ValueError for an unknown domain, points of the wrong dimension for the domain, or a
				coordinate that is not finite
		'''
		if domain not in (DCMSR_GEOMETRY_DOMAIN_IMAGE, DCMSR_GEOMETRY_DOMAIN_PATIENT):
			raise ValueError('Unknown geometry domain: %s' % domain)
		dimension = 2 if domain == DCMSR_GEOMETRY_DOMAIN_IMAGE else 3
		points = tuple(tuple(float(c) for c in point) for point in points)
		if any(len(point) != dimension for point in points):
			raise ValueError('%s-domain points must have %d coordinates' % (domain, dimension))
		if not all(math.isfinite(c) for point in points for c in point):
			raise ValueError('Measurement coordinates must be finite: %s' % (points,))
		return super().__new__(cls, domain, str(graphic_type), points, reference)

	@property
	def is_image(self):
		return self.domain == DCMSR_GEOMETRY_DOMAIN_IMAGE

	@property
	def is_patient(self):
		return self.domain == DCMSR_GEOMETRY_DOMAIN_PATIENT

	def with_points(self, points):
		'''	Same geometry with other points.

			@input points (iterable of sequences of numbers): the new points
			@returns Geometry
		'''
		return type(self)(self.domain, self.graphic_type, points, self.reference)


class ImageFrameGeometry(namedtuple('ImageFrameGeometry', ('position', 'orientation', 'pixel_spacing', 'pixel_aspect'))):
	'''	Spatial calibration of one image frame, used to compute the physical size of image-domain
		measurements. `sonador.apisettings.sr.measurements.image_frame_geometry` reads it from an image
		header.

		@property position (tuple of 3 float or None): Image Position (Patient)
		@property orientation (tuple of 6 float or None): Image Orientation (Patient), row direction then
			column direction
		@property pixel_spacing (tuple of 2 float or None): Pixel Spacing, the spacing between rows then
			the spacing between columns; None for an uncalibrated frame
		@property pixel_aspect (float or None): ratio of row to column spacing of an uncalibrated frame
			when its header declares one; None when unknown
	'''
	__slots__ = ()

	def __new__(cls, position=None, orientation=None, pixel_spacing=None, pixel_aspect=None):
		return super().__new__(cls, position, orientation, pixel_spacing, pixel_aspect)

	@property
	def is_usable(self):
		'''	Whether the frame's values can be calculated with: every value finite, spacing and aspect
			positive, and the orientation two perpendicular unit vectors (to 1 part in 100).

			@returns bool
		'''
		aspect = () if self.pixel_aspect is None else (self.pixel_aspect,)
		values = tuple(self.position or ()) + tuple(self.orientation or ()) + tuple(self.pixel_spacing or ()) + aspect
		if not all(math.isfinite(v) for v in values):
			return False
		if any(v <= 0 for v in tuple(self.pixel_spacing or ()) + aspect):
			return False
		if self.orientation is not None:
			row, column = np.asarray(self.orientation[:3]), np.asarray(self.orientation[3:])
			if abs(np.linalg.norm(row) - 1.0) > 0.01 or abs(np.linalg.norm(column) - 1.0) > 0.01 or abs(row.dot(column)) > 0.01:
				return False
		return True

	@property
	def is_calibrated(self):
		'''	Whether the frame gives physical sizes: usable values with a Pixel Spacing.

			@returns bool
		'''
		return self.pixel_spacing is not None and self.is_usable

	@property
	def is_isotropic(self):
		'''	Whether an uncalibrated frame is known to have square pixels.

			@returns bool: False when the aspect is anisotropic or unknown, or the frame is not usable
		'''
		return self.is_usable and self.pixel_aspect is not None and abs(self.pixel_aspect - 1.0) < 1e-6

	def patient_point(self, column, row):
		'''	Patient coordinates of a pixel position. Row spacing applies along the column direction and
			column spacing along the row direction, so anisotropic spacing is honoured. The SCOORD
			origin is the outer corner of the first pixel, half a pixel from Image Position (Patient).
			A frame without position or orientation uses the patient origin and axes, which leaves
			distances and angles unchanged.

			@input column, row (float): pixel position
			@returns tuple of 3 float
			@raises ValueError for a frame without a usable Pixel Spacing
		'''
		if not self.is_calibrated:
			raise ValueError('Image frame without a usable Pixel Spacing has no patient coordinates')
		position = self.position or (0.0, 0.0, 0.0)
		orientation = self.orientation or (1.0, 0.0, 0.0, 0.0, 1.0, 0.0)
		row_spacing, column_spacing = self.pixel_spacing
		return tuple(position[i] + (column - 0.5) * column_spacing * orientation[i]
			+ (row - 0.5) * row_spacing * orientation[i + 3] for i in range(3))


# Another NUM item of a measurement group, beside the primary value: its concept, value and unit,
# and the content item it was read from (children included), None for one created in Python
MeasurementValue = namedtuple('MeasurementValue', ('name', 'value', 'unit', 'item'))
MeasurementValue.__new__.__defaults__ = (None,)


class Measurement(_DisplayAttributesMixin, SRDataCollectionMember):
	'''	Numerical measurement associated with a resource: a simplified implementation of DICOM-SR
		TID 300, extended with the identity, property and link attributes a `Finding` carries.

		Only `value` and `unit` are required within Sonador; persisting to DICOM-SR also needs `name`. A
		measurement with a string `tracking_id` is written as its own TID 1501 measurement group; one
		without is written to the report's primary group.

		@property value (number): measured value
		@property unit (highdicom.Code or highdicom.CodedConcept): measurement unit
		@property values (OrderedDict): the other NUM items of the measurement group, keyed by concept
			`(code value, scheme designator)`, as `MeasurementValue`. Re-emitted as read unless replaced.
		@property uid: tracking identifier as given (a string or a highdicom `TrackingIdentifier`)
		@property tracking_id (str or None): human readable tracking identifier
		@property tracking_uid (str or None): tracking unique identifier, stable across revisions
		@property type (str or None): measurement type (`SONADOR_MEASUREMENT_TYPE_*`)
		@property geometry (Geometry or None): coordinates of the measurement
		@property reference (ImageReference or None): image the measurement was made on. Required in
			the image domain, optional in the patient domain, where it is not written to the document.
		@property frame_of_reference_uid (str or None): Frame of Reference of the imaging series
		@property series_uid (str or None): imaging series the measurement belongs to
		@property description, text, location (str or None): extended meta attributes; `description`
			is the label
		@property views (tuple of str): views the measurement was created in
		@property display (DisplayAttributes or None): display attributes
		@property extra_items (tuple of pydicom.Dataset): unmodelled children of the NUM item, re-emitted
			under it
		@property finding_sites (tuple or None): finding sites under the NUM item
		@property group_finding_sites (tuple of CodedConcept): coded finding sites read from the group
			level of a document (read only: the items stay in the group's `extra_items`)
		@property series, instance, sr_instance: model objects the measurement was aligned with when
			retrieved from an imaging server (`ImagingSeries`, `DcmInstance`, `DcmSRInstance`)
	'''
	def __init__(self, val:Union[int,float,numbers.Number], unit=DCMSR_UNITS_MM, name=None, measurement_type=None,
			qualifier=None, tracking_id=None, algorithm=None, derivation=None,  method=None,
			finding_sites=None, properties=None, ref_images=None, ref_value=None, tracking_uid=None, reference=None,
			geometry=None, frame_of_reference_uid=None, series_uid=None, description=None, text=None, location=None,
			views=None, display=None, extra_items=None, values=None, group_finding_sites=None, **kwargs):
		'''	Initialize measurement

			@input val (numbers.Number): numeric measurement value
			@input unit (highdicom.Code or highdicom.CodedConcept, default='mm'): Unit of measurement
			@input name (highdicom.Code or highdicom.CodedConcept, default=None): Name of the measurement
			@input measurement_type (str, default=the type `tracking_id` routes to): measurement type
			@input qualifier (highdicom.Code or highdicom.CodedConcept, default=None):
				Qualification of numeric measurement value or qualitative description.
			@input tracking_id (str or highdicom.sr.TrackingIdentifier, default=None): a string is the
				tracking identifier of the measurement's own group; a `TrackingIdentifier` is written
				under the NUM item
			@input tracking_uid (str, default=None): tracking unique identifier paired with a string
				`tracking_id` (generated on write when None)
			@input algorithm (default=None): description of the algoithm used for making the measurement
			@input derivation (dault=None): how the value was computed
			@input method (default=None): measurement method
			@input finding_sites (default=None): coded description of one or more anatomic locations
				associated with the measurement
			@input properties (default=None): meaurement properties, including evaluations of significance,
				relationship to a reference population, and its range.
			@input ref_images (default=None): referenced images which were used as source for the meaurement
			@input ref_value (default=None): reference real world value map
			@input reference (ImageReference or pydicom.Dataset, default=None): referenced image
			@input geometry (Geometry, default=None): coordinates of the measurement
			@input frame_of_reference_uid (str, default=None): Frame of Reference of the imaging series
			@input series_uid (str, default=None): series the measurement belongs to when `reference`
				carries none
			@input description, text, location (str, default=None): extended meta attributes
			@input views (iterable of str, default=None): views the measurement was created in
			@input display (DisplayAttributes or colour-role dict, default=None): display attributes
			@input extra_items (iterable of pydicom.Dataset, default=None): preserved children of the NUM item
			@input values (mapping, default=None): the other NUM items of the group (`MeasurementValue`)
			@input group_finding_sites (iterable of CodedConcept, default=None): group-level finding sites read
		'''
		if measurement_type is None and isinstance(tracking_id, str):
			from ..sr.seriestag import measurement_type_for_tracking_id
			measurement_type = measurement_type_for_tracking_id(tracking_id)

		super().__init__(object_type=measurement_type, **kwargs)
		self._val = val
		self._unit = unit
		self._name = name
		self._qualifier = qualifier
		self._uid = tracking_id
		self._tracking_uid = tracking_uid
		self._algorithm = algorithm
		self._derivation = derivation
		self._method = method
		self._finding_sites = finding_sites
		self._properties = properties
		self._img_refs = ref_images
		self._val_ref = ref_value

		self._geometry = geometry
		self._reference = None
		if reference is not None:
			self.reference = reference
		self.frame_of_reference_uid = frame_of_reference_uid
		self._series_uid = series_uid
		self.description = description
		self.text = text
		self.location = location
		self.views = tuple(views or ())
		self.display = display
		self.extra_items = tuple(extra_items or ())
		self.values = OrderedDict(values or ())
		self.group_finding_sites = tuple(group_finding_sites or ())

		# Parsed state: what the document held, so that an unchanged measurement is written verbatim
		self._parsed_value = None
		self._parsed_points = None
		self._parsed_reference = None
		self._stored_value_item = None
		self._source_coordinates = None

		# Links to the model objects of an imaging server (set by the retrieval layer)
		self.series = None
		self.instance = None
		self.sr_instance = None

	@property
	def value(self):
		return self._val

	@value.setter
	def value(self, val):
		self._val = val

	@property
	def unit(self):
		return self._unit

	@property
	def uid(self):
		return self._uid

	@property
	def tracking_id(self):
		'''	Human readable tracking identifier.

			@returns str, or None
		'''
		from ..sr.seriestag import tracking_identifier_parts
		return tracking_identifier_parts(self._uid)[0]

	@tracking_id.setter
	def tracking_id(self, val):
		self._uid = val

	@property
	def tracking_uid(self):
		'''	Tracking unique identifier.

			@returns str, or None until written when none was assigned
		'''
		from ..sr.seriestag import tracking_identifier_parts
		return tracking_identifier_parts(self._uid)[1] or self._tracking_uid

	@tracking_uid.setter
	def tracking_uid(self, val):
		self._tracking_uid = val

	@property
	def has_group_identity(self):
		'''	Whether the measurement is written as its own TID 1501 group.

			@returns bool: True for a string tracking identifier
		'''
		return isinstance(self._uid, str)

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

	@property
	def geometry(self):
		return self._geometry

	@geometry.setter
	def geometry(self, val):
		self._check_geometry(val)
		self._geometry = val

	def _check_geometry(self, geometry):
		'''	Hook for a class that constrains its geometry. The base class accepts any.

			@input geometry (Geometry or None): the geometry being assigned
		'''

	@property
	def reference(self):
		'''	Referenced image: that of the geometry in the image domain, else the one given.

			@returns ImageReference, or None
		'''
		if self._geometry is not None and self._geometry.is_image:
			return self._geometry.reference
		return self._reference

	@reference.setter
	def reference(self, val):
		'''	Set the image reference. A Referenced SOP Sequence item (dataset or dict) is converted to an
			`ImageReference`.
		'''
		if val is not None and not isinstance(val, ImageReference):
			from ..sr.seriestag import image_reference_from_dataset
			val = image_reference_from_dataset(val)
		if self._geometry is not None and self._geometry.is_image:
			self._geometry = self._geometry._replace(reference=val)
		else:
			self._reference = val

	@property
	def series_uid(self):
		'''	Series Instance UID the measurement belongs to: that of the image reference when it carries
			one, else the `series_uid` given.

			@returns str, or None
		'''
		reference = self.reference
		if reference is not None and reference.series_uid:
			return reference.series_uid
		return self._series_uid

	@series_uid.setter
	def series_uid(self, val):
		if self.reference is not None:
			self.reference = self.reference._replace(series_uid=val)
		self._series_uid = val

	def _mark_parsed(self, stored_value_item=None, source_coordinates=None):
		'''	Record the state read from a document, against which later edits are detected.

			@input stored_value_item (pydicom.Dataset, default=None): the Measured Value Sequence item
			@input source_coordinates (pydicom.Dataset, default=None): the SCOORD or SCOORD3D item
		'''
		self._parsed_value = self._val
		self._stored_value_item = stored_value_item
		self._source_coordinates = source_coordinates
		if self._geometry is not None:
			self._parsed_points = self._geometry.points
			self._parsed_reference = self._reference_key()

	def _reference_key(self):
		reference = self.reference
		return None if reference is None else (reference.sop_instance_uid, reference.frame_number)

	@property
	def is_parsed(self):
		'''	Whether the measurement holds the state of a document it was read from.

			@returns bool
		'''
		return self._stored_value_item is not None or self._parsed_points is not None

	@property
	def keeps_stored_value(self):
		'''	Whether a write re-emits the value read from the document verbatim.

			@returns bool: True for a parsed value that was not replaced
		'''
		return self._stored_value_item is not None and self._val == self._parsed_value

	@property
	def source_coordinates(self):
		'''	Coordinate item the measurement was read from, while it can still hold the geometry (same
			value type and graphic type). A write updates the points, the Frame of Reference and the
			image reference of a copy, so that the rest of the item survives.

			@returns pydicom.Dataset, or None
		'''
		source, geometry = self._source_coordinates, self._geometry
		if source is None or geometry is None:
			return None
		if source.get('ValueType') != ('SCOORD3D' if geometry.is_patient else 'SCOORD') or source.get('GraphicType') != geometry.graphic_type:
			return None
		return source

	@property
	def reference_changed(self):
		'''	Whether the referenced SOP instance or frame differs from the one read from the document.

			@returns bool: False for a measurement created in Python
		'''
		return self._parsed_points is not None and self._reference_key() != self._parsed_reference

	@property
	def stored_value_item(self):
		'''	Measured Value Sequence item read from the document, while the value has not been replaced.

			@returns pydicom.Dataset, or None
		'''
		return self._stored_value_item if self._val == self._parsed_value else None

	def resolve_value(self, diagnostics=None):
		'''	Value and unit written for the measurement.

			@input diagnostics (list, default=None): list the diagnostics are appended to

			@returns tuple (number, unit)
			@raises SRDiagnosticError for a value that is not finite (`measurement-not-computable`)
		'''
		if isinstance(self._val, numbers.Number) and not math.isfinite(self._val):
			from ..sr import SRDiagnostic, SRDiagnosticError, SR_DIAGNOSTIC_ERROR, SR_CONDITION_MEASUREMENT_NOT_COMPUTABLE
			raise SRDiagnosticError(SRDiagnostic(SR_DIAGNOSTIC_ERROR, SR_CONDITION_MEASUREMENT_NOT_COMPUTABLE, None, self.tracking_uid,
				'Measurement value %s is not finite and cannot be written' % self._val))
		return self._val, self._unit

	@classmethod
	def from_sr(cls, item, group=None, strict=False, diagnostics=None, calibrations=None, **kwargs):
		'''	Build a measurement from a NUM content item. Called on `Measurement`, the class comes from
			the tracking identifier and a NUM group with an unknown identifier is a generic `Measurement`.
			Called on a subclass, that subclass is requested: the parser still falls back to a generic
			`Measurement` when the concept or the geometry is not one the tool can use. See
			`sonador.apisettings.sr.measurements.srparse_measurement`.

			@input item (pydicom.Dataset): the NUM content item
			@input group (pydicom.Dataset, default=None): the measurement group holding the item; gives
				the tracking identifiers, the other NUM items and the legacy group-level properties
			@input strict (bool, default=False): raise SRDiagnosticError on `warning` and `error` diagnostics
			@input diagnostics (list, default=None): list the diagnostics are appended to
			@input calibrations (mapping, default=None): `ImageFrameGeometry` by SOP Instance UID or
				`(SOP Instance UID, frame number)`, used to verify image-domain stored values

			@returns
				- the class selected, or a generic Measurement after the fallback above
				- None for a typed NUM item with neither a value nor usable coordinates, or a generic NUM
					item without a value: such an item stays preserved in its group
		'''
		from ..sr.measurements import srparse_measurement
		return srparse_measurement(item, group=group, strict=strict, diagnostics=diagnostics, calibrations=calibrations,
			measurement_class=None if cls is Measurement else cls, **kwargs)

	def create_sr(self, **kwargs):
		from ..sr import srencode_measurement
		return srencode_measurement(self, **kwargs)

	def __repr__(self):
		return '%s(%s, tracking_uid=%s)' % (type(self).__name__, self.value, self.tracking_uid)


class GeometricMeasurement(Measurement):
	'''	Measurement whose value follows from its coordinates. The stored NUM value of a document is
		authoritative; a new measurement computes its value at write; a measurement whose points
		were edited since it was read recomputes at write; degenerate geometry is refused at write.
		Subclasses state the tool (`default_type`), the concept and the number of encoded points.

		Patient-domain points are in millimetres in the imaging series' Frame of Reference. Points
		supplied in another frame are converted once, at construction, with `transform`: the document
		never carries geometry in any other frame.

		@property points (tuple of tuple of float): the ordered encoded points; settable
		@property calibration (ImageFrameGeometry or None): spatial calibration of the referenced frame,
			used to compute a new or edited image-domain value. Assigning it enables that computation and
			does not change or check a stored value; `calibrations` passed to `from_sr` verify at parse.
	'''
	default_type = None
	default_name = None
	default_unit = DCMSR_UNITS_MM
	point_count = None
	graphic_type = DCMSR_GRAPHIC_POLYLINE

	def __init__(self, points=None, geometry=None, transform=None, reference=None, frame_of_reference_uid=None, calibration=None,
			name=None, unit=None, tracking_id=None, value=None, **kwargs):
		'''	Initialize the measurement from its encoded points or from a `Geometry`.

			@input points (sequence of sequences of numbers, default=None): `(column, row)` pairs for the
				image domain, `(x, y, z)` triples for the patient domain
			@input geometry (Geometry, default=None): used instead of `points`
			@input transform (ImageTransformMatrix, default=None): conversion of patient-domain `points`
				from another frame to the imaging series' frame; it must be rigid and name its source frame
			@input reference (ImageReference, default=None): image the points were drawn on
			@input frame_of_reference_uid (str, default=the transform's target frame): Frame of Reference
				of the imaging series
			@input calibration (ImageFrameGeometry, default=None): spatial calibration of the frame
			@input name (Code or CodedConcept, default=the tool's concept): concept name of the NUM item
			@input unit (Code or CodedConcept, default=the tool's unit): unit of a stored value
			@input tracking_id (str, default=the identifier written for the tool): tracking identifier
			@input value (number, default=None): stored value; the document reader sets it
			@input kwargs: further `Measurement` arguments

			@raises ValueError for a wrong number of points, a transform applied to image coordinates, an
				unnamed source frame, or a target frame other than `frame_of_reference_uid`
		'''
		if geometry is None:
			if points is None:
				raise ValueError('%s needs points or a geometry' % type(self).__name__)
			points = tuple(tuple(point) for point in points)
			if len(points) != self.point_count:
				raise ValueError('%s holds %d encoded points, got %d' % (type(self).__name__, self.point_count, len(points)))
			if transform is not None:
				if any(len(point) != 3 for point in points):
					raise ValueError('A frame conversion applies to patient-domain points only')
				if not (transform.source_frame or transform.uid):
					raise ValueError('A frame conversion must name its source frame')
				if frame_of_reference_uid and transform.target_frame and frame_of_reference_uid != transform.target_frame:
					raise ValueError('The transform targets frame %s, not the measurement frame %s'
						% (transform.target_frame, frame_of_reference_uid))
				points = transform.convert(points)
				frame_of_reference_uid = frame_of_reference_uid or transform.target_frame
			domain = DCMSR_GEOMETRY_DOMAIN_IMAGE if len(points[0]) == 2 else DCMSR_GEOMETRY_DOMAIN_PATIENT
			geometry = Geometry(domain, self.graphic_type, points, reference if domain == DCMSR_GEOMETRY_DOMAIN_IMAGE else None)

		if tracking_id is None:
			from ..sr.seriestag import tracking_id_for_measurement_type
			tracking_id = tracking_id_for_measurement_type(self.default_type)
		super().__init__(value, unit=unit or self.default_unit, name=name or self.default_name, measurement_type=self.default_type,
			tracking_id=tracking_id, geometry=geometry, reference=None if geometry.is_image else reference,
			frame_of_reference_uid=frame_of_reference_uid, **kwargs)
		self.calibration = calibration

	@property
	def points(self):
		return self._geometry.points

	@points.setter
	def points(self, points):
		if len(points) != self.point_count:
			raise ValueError('%s holds %d encoded points, got %d' % (type(self).__name__, self.point_count, len(points)))
		self._geometry = self._geometry.with_points(points)

	def _check_geometry(self, geometry):
		'''	Refuse a geometry the tool cannot hold.

			@input geometry (Geometry or None): the geometry being assigned
			@raises ValueError for no geometry, another graphic type or another number of points
		'''
		if geometry is None or geometry.graphic_type != self.graphic_type or len(geometry.points) != self.point_count:
			raise ValueError('%s holds a %s of %d encoded points' % (type(self).__name__, self.graphic_type, self.point_count))

	def _calculation_points(self, points):
		'''	Points in the space the value is calculated in: patient coordinates when the image frame is
			calibrated, the points as they are otherwise.

			@input points (sequence of points): points of the measurement
			@returns sequence of points
		'''
		if self._geometry.is_image and self.calibration is not None and self.calibration.is_calibrated:
			return [self.calibration.patient_point(*p) for p in points]
		return points

	@property
	def _calibration_unusable(self):
		return self._geometry.is_image and self.calibration is not None and not self.calibration.is_usable

	@property
	def points_changed(self):
		'''	Whether the points differ from those read from the document.

			@returns bool: True for a measurement created in Python
		'''
		return self._parsed_points is None or self._geometry.points != self._parsed_points

	@property
	def keeps_stored_value(self):
		'''	Whether a write re-emits the stored value: a parsed value whose points and referenced image
			are unchanged. Supplying a calibration does not change this.

			@returns bool
		'''
		return self._val is not None and not self.points_changed and not self.reference_changed

	@property
	def value(self):
		'''	Measured value: the stored value while the points are unchanged, else the value computed
			from the points.

			@returns float, or None when it cannot be computed
		'''
		if self.keeps_stored_value:
			return self._val
		computed = self.computed_value()
		return computed[0] if computed is not None else None

	@value.setter
	def value(self, val):
		self._val = val

	@property
	def unit(self):
		'''	Unit of `value`: the stored unit while the points are unchanged, else the computed unit.

			@returns Code or CodedConcept
		'''
		if not self.keeps_stored_value:
			computed = self.computed_value()
			if computed is not None:
				return computed[1]
		return self._unit

	def _is_stored_in_pixels(self):
		return getattr(self._unit, 'value', None) == DCMSR_UNITS_PIXEL.value

	def computed_value(self):
		'''	Value computed from the points. Makes no request.

			@returns
				- tuple (float, unit)
				- None when the geometry is degenerate or the calibration needed is unavailable
		'''
		raise NotImplementedError

	@property
	def is_degenerate(self):
		'''	Whether the geometry defines no measurement (coincident points).

			@returns bool
		'''
		raise NotImplementedError

	def resolve_value(self, diagnostics=None):
		'''	Value and unit written: the stored value of an unchanged parsed measurement, else the value
			computed from the points. An uncalibrated image-domain value is recorded as an `info`
			diagnostic (`measurement-uncalibrated`); pixels are never reported as millimetres.

			@input diagnostics (list, default=None): list the diagnostics are appended to

			@returns tuple (number, unit)
			@raises SRDiagnosticError for degenerate geometry (`degenerate-geometry`), or a value that
				cannot be computed from what the measurement holds (`measurement-not-computable`)
		'''
		from ..sr import SRDiagnostic, SRDiagnosticError, sr_diagnostic, SR_DIAGNOSTIC_ERROR, SR_DIAGNOSTIC_INFO, \
			SR_CONDITION_DEGENERATE_GEOMETRY, SR_CONDITION_MEASUREMENT_NOT_COMPUTABLE, SR_CONDITION_MEASUREMENT_UNCALIBRATED
		if self.is_degenerate:
			raise SRDiagnosticError(SRDiagnostic(SR_DIAGNOSTIC_ERROR, SR_CONDITION_DEGENERATE_GEOMETRY, None, self.tracking_uid,
				'%s has degenerate geometry (coincident points) and cannot be written' % type(self).__name__))
		if self.keeps_stored_value:
			return self._val, self._unit
		computed = self.computed_value()
		if computed is None:
			raise SRDiagnosticError(SRDiagnostic(SR_DIAGNOSTIC_ERROR, SR_CONDITION_MEASUREMENT_NOT_COMPUTABLE, None, self.tracking_uid,
				'%s cannot be computed: its image frame has no usable calibration or its result is not finite; '
				'set `calibration` to the ImageFrameGeometry of the referenced frame' % type(self).__name__))
		if getattr(computed[1], 'value', None) == DCMSR_UNITS_PIXEL.value:
			sr_diagnostic(diagnostics, SR_DIAGNOSTIC_INFO, SR_CONDITION_MEASUREMENT_UNCALIBRATED,
				'image frame without Pixel Spacing; value written in pixels', value=self.tracking_uid)
		return computed


def _distance(a, b):
	return float(np.linalg.norm(np.subtract(a, b)))


class LengthMeasurement(GeometricMeasurement):
	'''	Length between two ordered points (Cornerstone `Length` tool), written as a NUM item
		`INFERRED FROM` a two-point `POLYLINE`.

		The image-domain value maps both points to patient coordinates with the frame's calibration
		and is in millimetres. On a frame without Pixel Spacing it is the pixel distance, with unit
		`(1, UCUM, "px")`.

		@property p1, p2 (tuple of float): the end points as drawn; settable
		@property length (float or None): the measured value
	'''
	default_type = SONADOR_MEASUREMENT_TYPE_LENGTH
	default_name = DCMSR_CONCEPT_LENGTH
	point_count = 2

	@property
	def length(self):
		return self.value

	@property
	def p1(self):
		return self.points[0]

	@p1.setter
	def p1(self, point):
		self.points = (point, self.points[1])

	@property
	def p2(self):
		return self.points[1]

	@p2.setter
	def p2(self, point):
		self.points = (self.points[0], point)

	@property
	def is_degenerate(self):
		return _distance(*self._calculation_points(self.points)) <= DCMSR_GEOMETRY_COINCIDENT_TOLERANCE

	def computed_value(self):
		'''	Length computed from the points.

			@returns
				- tuple (float, unit): millimetres, or pixels on an uncalibrated frame
				- None for an image-domain length in millimetres without a calibration, an unusable
					calibration, or a result that is not finite
		'''
		geometry = self._geometry
		if self._calibration_unusable:
			return None
		if geometry.is_patient or (self.calibration is not None and self.calibration.is_calibrated):
			value, unit = _distance(*self._calculation_points(geometry.points)), DCMSR_UNITS_MM
		elif self.calibration is not None or self._is_stored_in_pixels():
			value, unit = _distance(*geometry.points), DCMSR_UNITS_PIXEL
		else:
			return None
		return (value, unit) if math.isfinite(value) else None


class _AngleBase(GeometricMeasurement):
	'''	Angle between two lines, encoded as a four-point `POLYLINE` in UCUM `deg`. With the points
		`p1, p2, p3, p4`, `v1 = p2 - p1`, `v2 = p3 - p4` and the value is
		`acos(v1 . v2 / (|v1| |v2|))` in degrees, 0 to 180. The result depends on endpoint order:
		reversing one line gives the supplement, and no normalisation to the acute angle is applied.
	'''
	default_name = DCMSR_CONCEPT_COBB_ANGLE
	default_unit = DCMSR_UNITS_ANGLE_DEGREE
	point_count = 4

	@property
	def angle(self):
		return self.value

	def _formula_points(self):
		return self.points

	@property
	def is_degenerate(self):
		p1, p2, p3, p4 = self._calculation_points(self._formula_points())
		return _distance(p1, p2) <= DCMSR_GEOMETRY_COINCIDENT_TOLERANCE or _distance(p3, p4) <= DCMSR_GEOMETRY_COINCIDENT_TOLERANCE

	def computed_value(self):
		'''	Angle computed from the points. Image-domain points are mapped to patient coordinates first,
			so that anisotropic spacing does not distort the angle; a frame without Pixel Spacing is
			computed in pixels only when its pixels are known to be square.

			@returns
				- tuple (float, unit): degrees
				- None for degenerate lines, an image frame without a usable calibration, or an intermediate
					result that is not finite
		'''
		if self.is_degenerate:
			return None
		if self._geometry.is_image and (self.calibration is None or not (self.calibration.is_calibrated or self.calibration.is_isotropic)):
			return None
		p1, p2, p3, p4 = (np.asarray(p, dtype=float) for p in self._calculation_points(self._formula_points()))
		v1, v2 = p2 - p1, p3 - p4
		cosine = float(np.dot(v1, v2) / (np.linalg.norm(v1) * np.linalg.norm(v2)))
		if not math.isfinite(cosine):
			return None
		return math.degrees(math.acos(min(1.0, max(-1.0, cosine)))), DCMSR_UNITS_ANGLE_DEGREE


class AngleMeasurement(_AngleBase):
	'''	Three-point angle with the vertex in the middle (Cornerstone `Angle` tool). The three handles
		and the four encoded points are distinct: the encoding is `p1, vertex, vertex, p3`, and a
		document is read from points 0, 1 and 3. The value is the interior angle at the vertex.

		@property p1, vertex, p3 (tuple of float): the handles; settable
		@property angle (float or None): the measured value
	'''
	default_type = SONADOR_MEASUREMENT_TYPE_ANGLE

	def __init__(self, p1=None, vertex=None, p3=None, **kwargs):
		'''	Initialize the angle from its handles, or from a `geometry` of four encoded points.

			@input p1, vertex, p3 (sequence of numbers, default=None): the handles
			@input kwargs: `GeometricMeasurement` arguments
		'''
		if kwargs.get('geometry') is None:
			kwargs['points'] = (p1, vertex, vertex, p3)
		super().__init__(**kwargs)

	def _formula_points(self):
		return (self.p1, self.vertex, self.vertex, self.p3)

	def _set_handles(self, p1, vertex, p3):
		self.points = (p1, vertex, vertex, p3)

	@property
	def p1(self):
		return self.points[0]

	@p1.setter
	def p1(self, point):
		self._set_handles(point, self.vertex, self.p3)

	@property
	def vertex(self):
		return self.points[1]

	@vertex.setter
	def vertex(self, point):
		self._set_handles(self.p1, point, self.p3)

	@property
	def p3(self):
		return self.points[3]

	@p3.setter
	def p3(self, point):
		self._set_handles(self.p1, self.vertex, point)


class CobbAngleMeasurement(_AngleBase):
	'''	Angle between two independent lines (Cornerstone `CobbAngle` tool). Coincident middle points
		do not change the type: the tracking identifier decides it.

		@property line1, line2 (tuple of 2 points): the lines as drawn; settable
		@property angle (float or None): the measured value
	'''
	default_type = SONADOR_MEASUREMENT_TYPE_COBB_ANGLE

	def __init__(self, line1=None, line2=None, **kwargs):
		'''	Initialize the angle from its lines, or from a `geometry` of four encoded points.

			@input line1, line2 (pair of points, default=None): the lines, in drawing order
			@input kwargs: `GeometricMeasurement` arguments
		'''
		if kwargs.get('geometry') is None:
			kwargs['points'] = tuple(line1) + tuple(line2)
		super().__init__(**kwargs)

	@property
	def line1(self):
		return self.points[:2]

	@line1.setter
	def line1(self, line):
		self.points = tuple(line) + self.points[2:]

	@property
	def line2(self):
		return self.points[2:]

	@line2.setter
	def line2(self, line):
		self.points = self.points[:2] + tuple(line)


# Measurement class by measurement type: the factory behind `Measurement.from_sr`
MEASUREMENT_CLASSES = {}


def register_measurement_class(cls):
	'''	Make a measurement class the one read for its `default_type`.

		@input cls (subclass of GeometricMeasurement)
		@returns `cls`
	'''
	MEASUREMENT_CLASSES[cls.default_type] = cls
	return cls


def measurement_class_for_type(measurement_type):
	'''	Class of a measurement type.

		@input measurement_type (str or None): one of `SONADOR_MEASUREMENT_TYPE_*`
		@returns type: the registered class, `Measurement` for any other type
	'''
	return MEASUREMENT_CLASSES.get(measurement_type, Measurement)


for _measurement_class in (LengthMeasurement, AngleMeasurement, CobbAngleMeasurement):
	register_measurement_class(_measurement_class)


class MeasurementCollection(SRDataObjectCollection):
	'''	Collection of measurements.
	'''
	model = Measurement
	object_type_attr = 'measurement_type'

	@classmethod
	def from_sr(cls, group, strict=False, diagnostics=None, **kwargs):
		'''	Measurements of a measurement group (TID 1501), parsed with the group reader: one per group
			routed to a quantitative tool or holding NUM items, none otherwise.

			@input group (pydicom.Dataset): Measurement Group content item
			@input strict (bool, default=False): raise SRDiagnosticError on `warning` and `error` diagnostics
			@input diagnostics (list, default=None): list the diagnostics are appended to

			@returns MeasurementCollection (an instance of `cls`): empty for a group without measurements
		'''
		from ..sr.seriestag import srparse_report_group
		parsed = srparse_report_group(group, strict=strict, diagnostics=diagnostics, **kwargs)
		return cls(list(parsed.measurements) if parsed.measurements is not None else [])

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
			and UID, the findings (`Finding.from_sr`), the measurement (`Measurement.from_sr`), and every
			other child preserved as `extra_items`.

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
