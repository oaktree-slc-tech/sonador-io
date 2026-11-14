'''	Sonador-SR 0.3: quantitative measurements (Length, Angle, Cobb angle, Circle, Bidirectional) exchanged
	with the Sonador Viewer, specified in oak-tree/medical-imaging/sonador-client#37 (the shared model),
	oak-tree/medical-imaging/sonador-client#41 (Circle) and oak-tree/medical-imaging/sonador-client#43
	(Bidirectional).

	A measurement is one TID 1501 group holding a TID 300 NUM item whose tracking identifier names
	the Cornerstone tool. The geometry is a child of the NUM: `INFERRED FROM` SCOORD3D with a Frame
	of Reference (patient domain) or SCOORD with `SELECTED FROM` IMAGE (image domain). The per-item
	properties are children of the NUM item as well: flat `HAS PROPERTIES` TEXT meta items, one
	`Sonador-Measurement.View` per view, and the display container. Documents are written as
	Comprehensive 3D SR with the display container nested under the NUM; the flat form of Enhanced
	SR documents is read and no longer written. Group-level properties are a read-only legacy
	layout, with a `warning`.

	View values are registered (`SONADOR_MEASUREMENT_VIEWS`, each with a meaning and a Sonador-SR
	0.3 `Code` whose value is the text written); the encoding stays TEXT. A patient-domain NUM also
	carries where it was made: `Sonador-Measurement.Source-Series` (UIDREF, the imaging series) and
	`Sonador-Measurement.Drawing-Plane` (TEXT, the orientation of the drawing view as six decimals
	in the layout of Image Orientation (Patient), width direction then height direction). Neither is
	written for image-domain geometry, whose image reference already says where it was made.

	Quantitative concepts are written with their SCT codes and read in their SRT forms as well.

	A circle (`CircleROI`) is three NUM items, Area, Perimeter and Radius, each `INFERRED FROM` its own
	copy of the geometry: `CIRCLE` (center and circumference point) in the image domain, `ELLIPSE` (two
	perpendicular diameters of equal length) in the patient domain, where DICOM defines no 3D circle. The
	Area NUM carries the per-measurement properties and, for a circle derived from three points,
	`Sonador-Measurement.ConstructionPoints`. Readers also accept the undefined `CIRCLE` SCOORD3D a stock
	Cornerstone3D adapter writes, as a circle with no plane. The pixel statistics a circle may carry (a NUM
	per statistic after the Radius NUM) are read and written back while the circle is unchanged; this
	client never computes them.

	A bidirectional measurement (`Bidirectional`) is two NUM items, Long Axis then Short Axis, each `INFERRED
	FROM` its own two-point `POLYLINE`, with the per-measurement properties under the Long Axis NUM. The axes
	are kept as drawn, never swapped and never constrained to cross or be perpendicular; each stored length is
	authoritative while its own axis is unchanged.

	`Measurement.values` is keyed by the NUM item's concept, with its Derivation modifier when it has one.

	The identifier decides the type, the geometry never does: a group with an unknown identifier or
	an unusable geometry reads as a generic `Measurement` with every NUM item preserved. Children
	that are not modelled stay under their own item as `extra_items`.

	Contents: the concepts and accepted codes, `image_frame_geometry`, the drawing-plane codec, the content
	item encoders (`srencode_measurement_*`, `srencode_circle`, `srencode_bidirectional`) and the parsers
	(`srparse_measurement`, `srparse_circle`, `srparse_bidirectional`, `srparse_group_measurement`).
	The model classes are in `sonador.apisettings.base.sr`; the group router is
	`sonador.apisettings.sr.seriestag.srparse_report_group`.
'''
import copy, math, re
from collections import OrderedDict

import numpy as np

from pydicom.dataset import Dataset
from pydicom.sr.codedict import codes as dcmcodes
from pydicom.valuerep import DS

from highdicom.sr.content import CoordinatesForMeasurement, CoordinatesForMeasurement3D, SourceImageForRegion, FindingSite
from highdicom.sr.templates import Code, CodedConcept
from highdicom.sr.value_types import ContentItem, ContentSequence, NumContentItem, TextContentItem, UIDRefContentItem, \
	ScoordContentItem, Scoord3DContentItem
from pydicom.sequence import Sequence

from client.utils.object import pick
from highdicom.sr.enum import RelationshipTypeValues, GraphicTypeValues, GraphicTypeValues3D

from ..base import SONADOR_MANUFACTURER, SONADOR_SR, SONADOR_SCHEME_VERSION_03, Measurement, MeasurementValue, Geometry, \
	GeometricMeasurement, ImageFrameGeometry, measurement_class_for_type, DCMSR_GEOMETRY_DOMAIN_IMAGE, DCMSR_GEOMETRY_DOMAIN_PATIENT, \
	DCMSR_UNITS_PIXEL, DCMSR_CONCEPT_LENGTH, DCMSR_CONCEPT_COBB_ANGLE, DCMHEADER_SR_REF_REFERENCE_FRAME, \
	SONADOR_MEASUREMENT_TYPE_LENGTH, SONADOR_MEASUREMENT_TYPE_ANGLE, SONADOR_MEASUREMENT_TYPE_COBB_ANGLE, \
	SONADOR_MEASUREMENT_TYPE_GENERIC, SONADOR_MEASUREMENT_TYPE_CIRCLE, DCMHEADER_CODING_SCHEME_DESIGNATOR, DCMHEADER_CODE_MEANING, \
	drawing_plane_vectors, drawing_plane_normal, normals_parallel, CircleMeasurement, circle_from_ellipse, DCMSR_GRAPHIC_CIRCLE, \
	DCMSR_GRAPHIC_ELLIPSE, DCMSR_CONCEPT_AREA, DCMSR_CONCEPT_PERIMETER, DCMSR_CONCEPT_RADIUS, DCMSR_CIRCLE_LENGTH_TOLERANCE, \
	DCMSR_CIRCLE_AREA_TOLERANCE, DCMSR_CIRCLE_AREA_RELATIVE_TOLERANCE, DCMSR_CIRCLE_PARALLEL_DEGREES, DCMSR_UNITS_MM, \
	CIRCLE_VALUE_AREA, CIRCLE_VALUE_PERIMETER, CIRCLE_VALUE_RADIUS, CIRCLE_VALUE_ROLES, circle_unit_scale, BidirectionalMeasurement, \
	SONADOR_MEASUREMENT_TYPE_BIDIRECTIONAL, DCMSR_CONCEPT_LONG_AXIS, DCMSR_CONCEPT_SHORT_AXIS, DCMSR_BIDIRECTIONAL_LENGTH_TOLERANCE, \
	BIDIRECTIONAL_VALUE_LONG_AXIS, BIDIRECTIONAL_VALUE_SHORT_AXIS, BIDIRECTIONAL_VALUE_ROLES, DCMSR_GRAPHIC_POLYLINE, DCMSR_FREE_TEXT_VALUE
from . import dcm_code_value, dcm_code_matches, srcontent_is_named, srcontent_extra_items, srcontent_from_dataset, sr_diagnostic, \
	SR_DIAGNOSTIC_INFO, SR_DIAGNOSTIC_WARNING, SR_DIAGNOSTIC_ERROR, SR_CONDITION_MEASUREMENT_VALUE_MISMATCH, \
	SR_CONDITION_MEASUREMENT_VALUE_UNVERIFIED, SR_CONDITION_MEASUREMENT_UNCALIBRATED, SR_CONDITION_MEASUREMENT_WITHOUT_VALUE, \
	SR_CONDITION_MEASUREMENT_READ_AS_GENERIC, SR_CONDITION_FOREIGN_MEASUREMENT_LABEL, SR_CONDITION_DEGENERATE_GEOMETRY, \
	SR_CONDITION_INVALID_GEOMETRY, SR_CONDITION_SCOORD3D_WITHOUT_FRAME, SR_CONDITION_ANGLE_MIDDLE_POINTS_DIFFER
from .seriestag import srparse_item_properties, srencode_meta_items, srencode_display_items, srcontent_is_valid, \
	image_reference_from_dataset, measurement_type_for_tracking_id, DCMSR_LAYOUT_NESTED, DCMSR_LAYOUTS_WRITTEN, \
	_children, _value_type, _text_value, _relationship, _path, _note_by_reference, _note_missing_relationships, _UNNAMED_MARKER, DCMSR_IMAGE_REFERENCE_NAME
from . import srcontent_name, SR_CONDITION_UNREGISTERED_VIEW, SR_CONDITION_INVALID_DRAWING_PLANE, SR_CONDITION_PROVENANCE_ON_IMAGE_DOMAIN, \
	SR_CONDITION_LEGACY_CIRCLE, SR_CONDITION_QUALIFIER_DROPPED, SR_CONDITION_STATISTICS_DROPPED, SR_CONDITION_CONSTRUCTION_POINTS_DROPPED, \
	SR_CONDITION_AXES_NOT_ORDERED, SR_CONDITION_AXES_UNITS_DIFFER
from ..base import CircleStatistics
from .seriestag import DCMSR_MEASUREMENT_META, DCMSR_DISPLAY_ATTRIBUTES, DCMSR_DISPLAY_COLOR_ROLES

# Concepts

SONADOR_MEASUREMENT_VIEW = '%s-Measurement.View' % SONADOR_MANUFACTURER
SONADOR_MEASUREMENT_VIEW_MEANING = 'View a measurement was created in. Descriptive measurement attribute.'
DCMSR_MEASUREMENT_VIEW = Code(SONADOR_MEASUREMENT_VIEW, SONADOR_SR, SONADOR_MEASUREMENT_VIEW_MEANING,
	scheme_version=SONADOR_SCHEME_VERSION_03)

# View values. The string is the text written into the View item and the value of its code; the
# codes are the formal registry of the values and are not written into documents as CODE items.
# The Sonador Viewer declares the same table.
SONADOR_MEASUREMENT_VIEW_AXIAL = 'axial'
SONADOR_MEASUREMENT_VIEW_AXIAL_MEANING = 'Axial view. Cross-sections, dividing head from feet.'
DCMSR_MEASUREMENT_VIEW_AXIAL = Code(SONADOR_MEASUREMENT_VIEW_AXIAL, SONADOR_SR, SONADOR_MEASUREMENT_VIEW_AXIAL_MEANING,
	scheme_version=SONADOR_SCHEME_VERSION_03)

SONADOR_MEASUREMENT_VIEW_SAGITTAL = 'sagittal'
SONADOR_MEASUREMENT_VIEW_SAGITTAL_MEANING = 'Sagittal view. Side-on slices, dividing left from right.'
DCMSR_MEASUREMENT_VIEW_SAGITTAL = Code(SONADOR_MEASUREMENT_VIEW_SAGITTAL, SONADOR_SR, SONADOR_MEASUREMENT_VIEW_SAGITTAL_MEANING,
	scheme_version=SONADOR_SCHEME_VERSION_03)

SONADOR_MEASUREMENT_VIEW_CORONAL = 'coronal'
SONADOR_MEASUREMENT_VIEW_CORONAL_MEANING = 'Coronal view. Front-on slices, dividing front from back.'
DCMSR_MEASUREMENT_VIEW_CORONAL = Code(SONADOR_MEASUREMENT_VIEW_CORONAL, SONADOR_SR, SONADOR_MEASUREMENT_VIEW_CORONAL_MEANING,
	scheme_version=SONADOR_SCHEME_VERSION_03)

SONADOR_MEASUREMENT_VIEW_OBLIQUE = 'oblique'
SONADOR_MEASUREMENT_VIEW_OBLIQUE_MEANING = 'Oblique view. A slice tilted away from the standard views.'
DCMSR_MEASUREMENT_VIEW_OBLIQUE = Code(SONADOR_MEASUREMENT_VIEW_OBLIQUE, SONADOR_SR, SONADOR_MEASUREMENT_VIEW_OBLIQUE_MEANING,
	scheme_version=SONADOR_SCHEME_VERSION_03)

SONADOR_MEASUREMENT_VIEW_3D = '3d'
SONADOR_MEASUREMENT_VIEW_3D_MEANING = '3D view. The whole volume, seen from any direction.'
DCMSR_MEASUREMENT_VIEW_3D = Code(SONADOR_MEASUREMENT_VIEW_3D, SONADOR_SR, SONADOR_MEASUREMENT_VIEW_3D_MEANING,
	scheme_version=SONADOR_SCHEME_VERSION_03)

DCMSR_MEASUREMENT_VIEW_CODES = OrderedDict((code.value, code) for code in (DCMSR_MEASUREMENT_VIEW_AXIAL,
	DCMSR_MEASUREMENT_VIEW_SAGITTAL, DCMSR_MEASUREMENT_VIEW_CORONAL, DCMSR_MEASUREMENT_VIEW_OBLIQUE, DCMSR_MEASUREMENT_VIEW_3D))
SONADOR_MEASUREMENT_VIEWS = (SONADOR_MEASUREMENT_VIEW_AXIAL, SONADOR_MEASUREMENT_VIEW_SAGITTAL, SONADOR_MEASUREMENT_VIEW_CORONAL,
	SONADOR_MEASUREMENT_VIEW_OBLIQUE, SONADOR_MEASUREMENT_VIEW_3D)

# Where a patient-domain measurement was made: the imaging series, and the orientation of the view
SONADOR_MEASUREMENT_SOURCE_SERIES = '%s-Measurement.Source-Series' % SONADOR_MANUFACTURER
SONADOR_MEASUREMENT_SOURCE_SERIES_MEANING = 'Imaging series a patient-domain measurement was made on.'
DCMSR_MEASUREMENT_SOURCE_SERIES = Code(SONADOR_MEASUREMENT_SOURCE_SERIES, SONADOR_SR, SONADOR_MEASUREMENT_SOURCE_SERIES_MEANING,
	scheme_version=SONADOR_SCHEME_VERSION_03)

SONADOR_MEASUREMENT_DRAWING_PLANE = '%s-Measurement.Drawing-Plane' % SONADOR_MANUFACTURER
SONADOR_MEASUREMENT_DRAWING_PLANE_MEANING = 'Orientation of the view a measurement was drawn in.'
DCMSR_MEASUREMENT_DRAWING_PLANE = Code(SONADOR_MEASUREMENT_DRAWING_PLANE, SONADOR_SR, SONADOR_MEASUREMENT_DRAWING_PLANE_MEANING,
	scheme_version=SONADOR_SCHEME_VERSION_03)

# Points a measurement was derived from (a circle's three circumference points): auxiliary data, the
# primary geometry is complete without them
SONADOR_MEASUREMENT_CONSTRUCTION_POINTS = '%s-Measurement.ConstructionPoints' % SONADOR_MANUFACTURER
SONADOR_MEASUREMENT_CONSTRUCTION_POINTS_MEANING = 'Points a measurement was constructed from.'
DCMSR_MEASUREMENT_CONSTRUCTION_POINTS = Code(SONADOR_MEASUREMENT_CONSTRUCTION_POINTS, SONADOR_SR,
	SONADOR_MEASUREMENT_CONSTRUCTION_POINTS_MEANING, scheme_version=SONADOR_SCHEME_VERSION_03)

# Concepts of a circle's NUM items by role, in writing order: the SCT code written, then the SRT equivalent read
DCMSR_CIRCLE_CONCEPTS = OrderedDict((
	(CIRCLE_VALUE_AREA, (DCMSR_CONCEPT_AREA, Code('G-A166', 'SRT', 'Area'))),
	(CIRCLE_VALUE_PERIMETER, (DCMSR_CONCEPT_PERIMETER, Code('G-A197', 'SRT', 'Perimeter'))),
	(CIRCLE_VALUE_RADIUS, (DCMSR_CONCEPT_RADIUS, Code('G-A196', 'SRT', 'Radius'))),
))

# Concepts of a bidirectional measurement's NUM items by role, in writing order: the SCT code written, then the SRT
# equivalent read
DCMSR_BIDIRECTIONAL_CONCEPTS = OrderedDict((
	(BIDIRECTIONAL_VALUE_LONG_AXIS, (DCMSR_CONCEPT_LONG_AXIS, Code('G-A185', 'SRT', 'Long Axis'))),
	(BIDIRECTIONAL_VALUE_SHORT_AXIS, (DCMSR_CONCEPT_SHORT_AXIS, Code('G-A186', 'SRT', 'Short Axis'))),
))

# Derivation modifier of a NUM item (`HAS CONCEPT MOD` CODE): the statistic a value is
DCMSR_DERIVATION = Code('121401', 'DCM', 'Derivation')

# Pixel statistics of a circle (CID 7464) by `CircleStatistics` field: the SCT code, then the SRT equivalent read
DCMSR_CIRCLE_STATISTICS = OrderedDict((
	('mean', (Code('373098007', 'SCT', 'Mean'), Code('R-00317', 'SRT', 'Mean'))),
	('std_dev', (Code('386136009', 'SCT', 'Standard Deviation'), Code('R-10047', 'SRT', 'Standard Deviation'))),
	('max', (Code('56851009', 'SCT', 'Maximum'), Code('G-A437', 'SRT', 'Maximum'))),
))

# Quantities a circle's statistics are read in, by the modality of the pixels: the concept naming a
# statistic NUM, its UCUM unit, and the modality unit the Viewer reads it as
DCMSR_CIRCLE_STATISTIC_QUANTITIES = OrderedDict((
	('CT', (Code('112031', 'DCM', 'Attenuation Coefficient'), Code("[hnsf'U]", 'UCUM', 'Hounsfield Unit'), 'HU')),
	('PT', (Code('126401', 'DCM', 'SUVbw'), Code('g/ml{SUVbw}', 'UCUM', 'Standardized Uptake Value body weight'), 'SUV')),
	('MR', (Code('110852', 'DCM', 'MR signal intensity'), Code('1', 'UCUM', 'no units'), '')),
))

# Units of body-weight SUV in a stock Cornerstone3D statistic (upper case); another SUV normalization is not read
DCMSR_BODY_WEIGHT_SUV_UNITS = ('SUV', 'SUVBW', 'G/ML{SUVBW}')

# Drawing-plane validation: unit length and perpendicularity of the directions, distance of the points
# from the plane through the first point (millimetres)
DCMSR_DRAWING_PLANE_DIRECTION_TOLERANCE = 1e-4
DCMSR_DRAWING_PLANE_DISTANCE_TOLERANCE = 1.0
DCMSR_DRAWING_PLANE_DECIMALS = 6
DCMSR_DRAWING_PLANE_NUMBER_RE = re.compile(r'^[+-]?(\d+(\.\d*)?|\.\d+)$')

# Concepts accepted for the primary NUM item of each tool; the first is the one written
DCMSR_MEASUREMENT_CONCEPTS = {
	SONADOR_MEASUREMENT_TYPE_LENGTH: (DCMSR_CONCEPT_LENGTH, Code('G-D7FE', 'SRT', 'Length')),
	SONADOR_MEASUREMENT_TYPE_ANGLE: (DCMSR_CONCEPT_COBB_ANGLE, Code('1483009', 'SCT', 'Angle')),
	SONADOR_MEASUREMENT_TYPE_COBB_ANGLE: (DCMSR_CONCEPT_COBB_ANGLE, Code('1483009', 'SCT', 'Angle')),
}

DCMSR_COMMENT = Code('121106', 'DCM', 'Comment')
DCMSR_FINDING_SITE_CONCEPTS = (Code('363698007', 'SCT', 'Finding Site'), Code('G-C0E3', 'SRT', 'Finding Site'))
DCMSR_CORNERSTONE_FREE_TEXT = DCMSR_FREE_TEXT_VALUE

# Allowed difference between a stored value and the value computed from the points
DCMSR_VALUE_TOLERANCE = 0.01

DCMSR_GRAPHIC_TYPES = {'SCOORD': GraphicTypeValues, 'SCOORD3D': GraphicTypeValues3D}


def _is_measurement_geometry(item):
	'''	Whether a child of a NUM item is the coordinates the measurement was made from: a SCOORD or
		SCOORD3D item that is not a named auxiliary property (`Sonador-Measurement.ConstructionPoints`) and
		is not a `HAS PROPERTIES` child.

		@input item (pydicom.Dataset): a child of a NUM item
		@returns bool
	'''
	if _value_type(item) not in DCMSR_GRAPHIC_TYPES or srcontent_is_named(item, DCMSR_MEASUREMENT_CONSTRUCTION_POINTS):
		return False
	return _relationship(item) != RelationshipTypeValues.HAS_PROPERTIES.value


# Calibration

def _numbers(value, count):
	'''	Tuple of `count` floats from a header value.

		@input value: multi-valued header value, or None
		@input count (int): number of values expected

		@returns
			- tuple of float
			- None when the value is absent or has another length
	'''
	if value is None:
		return None
	try:
		numbers = tuple(float(v) for v in value)
	except (TypeError, ValueError):
		return None
	return numbers if len(numbers) == count else None


def image_frame_geometry(ds, frame_number=None):
	'''	Spatial calibration of one frame of an image, read from its header. Makes no request. A
		multiframe image is read from the per-frame functional groups of the frame, then the shared
		functional groups, then the top-level attributes. Imager Pixel Spacing alone is not a
		calibration: without Pixel Spacing the frame is uncalibrated, and Pixel Aspect Ratio, Imager
		Pixel Spacing or Nominal Scanned Pixel Spacing only say whether its pixels are square.

		@input ds (pydicom.Dataset): header of the image
		@input frame_number (int, default=1): one-based frame of a multiframe image

		@returns ImageFrameGeometry
		@raises ValueError for a frame number outside the per-frame functional groups
	'''
	sources = []
	per_frame = ds.get('PerFrameFunctionalGroupsSequence')
	if per_frame:
		index = (frame_number or 1) - 1
		if index < 0 or index >= len(per_frame):
			raise ValueError('Frame %s is outside the %d frames of the image' % (frame_number, len(per_frame)))
		sources.append(per_frame[index])
	shared = ds.get('SharedFunctionalGroupsSequence')
	if shared:
		sources.append(shared[0])

	def functional(sequence, attribute):
		for source in sources:
			items = source.get(sequence)
			if items and attribute in items[0]:
				return items[0].get(attribute)
		return ds.get(attribute)

	position = _numbers(functional('PlanePositionSequence', 'ImagePositionPatient'), 3)
	orientation = _numbers(functional('PlaneOrientationSequence', 'ImageOrientationPatient'), 6)
	spacing = _numbers(functional('PixelMeasuresSequence', 'PixelSpacing'), 2)

	aspect = None
	if spacing is None:
		ratio = _numbers(ds.get('PixelAspectRatio'), 2)
		imager = _numbers(ds.get('ImagerPixelSpacing'), 2) or _numbers(ds.get('NominalScannedPixelSpacing'), 2)
		if ratio is not None and ratio[1]:
			aspect = ratio[0] / ratio[1]
		elif imager is not None and imager[1]:
			aspect = imager[0] / imager[1]
	return ImageFrameGeometry(position, orientation, spacing, aspect)


# Drawing plane

def drawing_plane_to_text(plane):
	'''	Text of a drawing plane: six decimals separated by commas, width x, y, z then height x, y, z,
		with no exponent, at most six decimal places and no spaces (`0,1,0,0,0,-1`).

		@input plane (pair of 3-vectors): the drawing plane
		@returns str
		@raises ValueError for a value of another shape or not finite
	'''
	width, height = drawing_plane_vectors(plane)
	numbers = []
	for value in tuple(width) + tuple(height):
		text = ('%.*f' % (DCMSR_DRAWING_PLANE_DECIMALS, value)).rstrip('0').rstrip('.')
		numbers.append('0' if text in ('-0', '') else text)
	return ','.join(numbers)


def drawing_plane_from_text(text):
	'''	Drawing plane of a Drawing-Plane text: six decimal numbers separated by commas, with any spaces
		around them trimmed. Only the form is checked here (`drawing_plane_failed_check` checks the rest).

		@input text (str): the text read
		@returns
			- tuple (width, height) of two `ImageCoord`
			- None when the text is not six finite decimal numbers
	'''
	parts = [part.strip() for part in str(text or '').split(',')]
	if len(parts) != 6 or not all(DCMSR_DRAWING_PLANE_NUMBER_RE.match(part) for part in parts):
		return None
	try:
		return drawing_plane_vectors([float(part) for part in parts])
	except ValueError:
		return None


def drawing_plane_failed_check(plane, points, normal=None):
	'''	The first validation check a drawing plane fails for the points of a measurement: each
		direction of unit length within 1e-4, the two directions perpendicular within 1e-4 (their dot
		product), the plane's normal parallel to `normal` within 1 degree when one is given (a circle's
		plane), every point within 1 mm of the plane through the first point. Makes no request.

		@input plane (pair of 3-vectors, or None): the drawing plane
		@input points (sequence of 3-sequences): patient-domain points of the measurement
		@input normal (sequence of 3 numbers, default=None): normal of the measurement's own plane

		@returns
			- None when the plane is valid
			- str naming the failed check: `six-finite-numbers`, `unit-width`, `unit-height`,
				`perpendicular`, `normal-parallel` or `points-within-1mm`
	'''
	try:
		width, height = (np.asarray(v, dtype=float) for v in drawing_plane_vectors(plane))
	except (TypeError, ValueError):
		return 'six-finite-numbers'
	if abs(np.linalg.norm(width) - 1.0) > DCMSR_DRAWING_PLANE_DIRECTION_TOLERANCE:
		return 'unit-width'
	if abs(np.linalg.norm(height) - 1.0) > DCMSR_DRAWING_PLANE_DIRECTION_TOLERANCE:
		return 'unit-height'
	if abs(float(np.dot(width, height))) > DCMSR_DRAWING_PLANE_DIRECTION_TOLERANCE:
		return 'perpendicular'
	plane_normal = np.cross(width, height)
	plane_normal = plane_normal / np.linalg.norm(plane_normal)
	if normal is not None and not normals_parallel(plane_normal, normal, DCMSR_CIRCLE_PARALLEL_DEGREES):
		return 'normal-parallel'
	if points:
		origin = np.asarray(points[0], dtype=float)
		for point in points[1:]:
			if abs(float(np.dot(np.asarray(point, dtype=float) - origin, plane_normal))) > DCMSR_DRAWING_PLANE_DISTANCE_TOLERANCE:
				return 'points-within-1mm'
	return None


# Encoder

def _mark_unnamed(item):
	'''	Mark a content item whose placeholder concept name is removed once the document is built, so
		that the tree matches the dcmjs layout, which names neither the SCOORD nor its IMAGE.

		@input item (content item)
		@returns `item`
	'''
	setattr(item, _UNNAMED_MARKER, True)
	return item


def _mark_source_unnamed(item):
	'''	Give a coordinate item read from a document, and its unnamed children, the placeholder name
		highdicom requires, marking each so that it is removed once the document is built: the
		re-emitted item stays as it was read. Modifies the item in place.

		@input item (pydicom.Dataset): root of the subtree
	'''
	if 'ConceptNameCodeSequence' not in item:
		item.ConceptNameCodeSequence = [CodedConcept.from_code(DCMSR_IMAGE_REFERENCE_NAME)]
		setattr(item, _UNNAMED_MARKER, True)
	for child in item.get('ContentSequence') or []:
		_mark_source_unnamed(child)


def _set_image_reference(item, reference):
	'''	Point the source IMAGE child (`SELECTED FROM`) of a coordinate item at another SOP instance and frame,
		adding the child when the item has none. Other children are left alone. Modifies the item in place.

		@input item (pydicom.Dataset): the SCOORD item
		@input reference (ImageReference): the new reference

		@raises ValueError for a reference without a SOP Instance UID
	'''
	if reference is None or not reference.sop_instance_uid:
		raise ValueError('An image-domain measurement needs an image reference with a SOP Instance UID to be written')
	images = [child for child in item.get('ContentSequence') or [] if child.get('ValueType') == 'IMAGE']
	# The source image is the SELECTED FROM child; an IMAGE of another relationship is left alone
	images = [child for child in images if child.get('RelationshipType') in (None, 'SELECTED FROM')]
	if not images:
		frames = [reference.frame_number] if reference.frame_number else None
		source_image = _mark_unnamed(SourceImageForRegion(reference.sop_class_uid or '', reference.sop_instance_uid, referenced_frame_numbers=frames))
		item.ContentSequence = list(item.get('ContentSequence') or []) + [source_image]
		return
	sop = images[0].ReferencedSOPSequence[0]
	sop.ReferencedSOPInstanceUID = reference.sop_instance_uid
	if reference.sop_class_uid:
		sop.ReferencedSOPClassUID = reference.sop_class_uid
	if reference.frame_number:
		sop.ReferencedFrameNumber = reference.frame_number
	elif 'ReferencedFrameNumber' in sop:
		del sop.ReferencedFrameNumber


def srencode_measurement_coordinates(measurement):
	'''	Coordinate content item of a measurement, `INFERRED FROM` its NUM item. A parsed item is
		copied with its points, Frame of Reference and image reference updated, so that everything else
		on it survives (concept name, fiducial UID, unmodelled children). Otherwise, or when the
		geometry is no longer of the item's type, the item is built from the geometry.

		@input measurement (Measurement)

		@returns
			- highdicom CoordinatesForMeasurement3D (patient domain) or CoordinatesForMeasurement
				(image domain)
			- None for a measurement without geometry
		@raises ValueError for a patient-domain measurement without a Frame of Reference, or an
			image-domain measurement without a SOP Instance UID
	'''
	geometry = measurement.geometry
	if geometry is None:
		return None
	data = np.asarray(geometry.points, dtype=float)

	source = measurement.source_coordinates
	if source is not None:
		item = copy.deepcopy(source)
		item.GraphicData = [float(v) for v in data.flatten()]
		if geometry.is_patient:
			if not measurement.frame_of_reference_uid:
				raise ValueError('A patient-domain measurement needs a Frame of Reference UID to be written')
			item.ReferencedFrameOfReferenceUID = measurement.frame_of_reference_uid
		elif measurement.reference_changed:
			_set_image_reference(item, geometry.reference)
		_mark_source_unnamed(item)
		if geometry.is_patient:
			return CoordinatesForMeasurement3D.from_dataset(item, copy=False)
		return CoordinatesForMeasurement.from_dataset(item, copy=False)

	if geometry.is_patient:
		if not measurement.frame_of_reference_uid:
			raise ValueError('A patient-domain measurement needs a Frame of Reference UID to be written')
		return _mark_unnamed(CoordinatesForMeasurement3D(geometry.graphic_type, data, measurement.frame_of_reference_uid))

	reference = geometry.reference
	if reference is None or not reference.sop_instance_uid:
		raise ValueError('An image-domain measurement needs an image reference with a SOP Instance UID to be written')
	frames = [reference.frame_number] if reference.frame_number else None
	source_image = _mark_unnamed(SourceImageForRegion(reference.sop_class_uid or '', reference.sop_instance_uid, referenced_frame_numbers=frames))
	return _mark_unnamed(CoordinatesForMeasurement(geometry.graphic_type, data, source_image))


def srencode_measurement_views(measurement, diagnostics=None, strict=False):
	'''	View items of a measurement, one `HAS PROPERTIES` TEXT item per view, written as given. A value
		outside the registry (`SONADOR_MEASUREMENT_VIEWS`) that was not read from the document is
		recorded as the `warning` `unregistered-view-value`; values read are written back unchanged
		without a diagnostic.

		@input measurement (Measurement)
		@input diagnostics (list, default=None): list the diagnostics are appended to
		@input strict (bool, default=False): refuse an unregistered value instead of writing it

		@returns list of highdicom TextContentItem
		@raises SRDiagnosticError under `strict` for an unregistered value
	'''
	parsed = set(measurement.parsed_views)
	items = []
	for view in measurement.views:
		view = str(view)
		if view not in DCMSR_MEASUREMENT_VIEW_CODES and view not in parsed:
			sr_diagnostic(diagnostics, SR_DIAGNOSTIC_WARNING, SR_CONDITION_UNREGISTERED_VIEW,
				'View value "%s" is not a registered value; written as given' % view, path=_path(measurement.tracking_uid),
				value=view, strict=strict)
		items.append(TextContentItem(name=DCMSR_MEASUREMENT_VIEW, value=view, relationship_type=RelationshipTypeValues.HAS_PROPERTIES))
	return items


def srencode_measurement_provenance(measurement):
	'''	Source-Series and Drawing-Plane items of a patient-domain measurement; none for any other.
		Source-Series is written from `series_uid`. Drawing-Plane is written from the text read from
		the document while the value is untouched, else formatted from `drawing_plane`. A plane that
		is new or set by the caller, or a valid plane read from the document whose measurement points
		have changed since, is validated first. Makes no request.

		@input measurement (Measurement)

		@returns list of highdicom content items (UIDRefContentItem, TextContentItem)
		@raises ValueError naming the failed check for a drawing plane that is not valid for the points
	'''
	geometry = measurement.geometry
	if geometry is None or not geometry.is_patient:
		return []
	items = []
	if measurement.series_uid:
		items.append(UIDRefContentItem(name=DCMSR_MEASUREMENT_SOURCE_SERIES, value=measurement.series_uid,
			relationship_type=RelationshipTypeValues.HAS_PROPERTIES))

	text, plane = measurement.drawing_plane_text, measurement.drawing_plane
	points_changed = getattr(measurement, 'points_changed', False)
	if plane is not None and (text is None or points_changed):
		failed = drawing_plane_failed_check(plane, geometry.points, normal=getattr(measurement, 'plane_normal', None))
		if failed is not None:
			raise ValueError('Drawing plane of measurement %s fails the check "%s" and cannot be written'
				% (measurement.tracking_uid, failed))
	if text is not None:
		items.append(TextContentItem(name=DCMSR_MEASUREMENT_DRAWING_PLANE, value=text, relationship_type=RelationshipTypeValues.HAS_PROPERTIES))
	elif plane is not None:
		items.append(TextContentItem(name=DCMSR_MEASUREMENT_DRAWING_PLANE, value=drawing_plane_to_text(plane),
			relationship_type=RelationshipTypeValues.HAS_PROPERTIES))
	return items


def srencode_measurement_children(measurement, layout=None, diagnostics=None, strict=False):
	'''	Children of the NUM item after the coordinates, in writing order: the extended meta items, one
		View item per view, Source-Series and Drawing-Plane (patient domain), the nested display
		container, the preserved extra items.

		@input measurement (Measurement)
		@input layout (str, default=`DCMSR_LAYOUT_NESTED`): the only layout written
		@input diagnostics (list, default=None): list the write diagnostics are appended to
		@input strict (bool, default=False): refuse an unregistered View value

		@returns list of highdicom content items: empty for a measurement without properties
		@raises ValueError for any other layout, or a drawing plane that fails validation
		@raises SRDiagnosticError under `strict` for an unregistered View value
	'''
	layout = layout or DCMSR_LAYOUT_NESTED
	if layout not in DCMSR_LAYOUTS_WRITTEN:
		raise ValueError('Unsupported per-item property layout: %s' % layout)
	children = srencode_meta_items(measurement)
	children.extend(srencode_measurement_views(measurement, diagnostics=diagnostics, strict=strict))
	children.extend(srencode_measurement_provenance(measurement))
	children.extend(srencode_display_items(measurement.display, layout=layout))
	children.extend(srcontent_extra_items(measurement.extra_items))
	return children


def _coded(code):
	'''	Coded concept of a `Code` or `CodedConcept`.

		@returns CodedConcept
	'''
	return code if isinstance(code, CodedConcept) else CodedConcept.from_code(code)


def _update_value_item(item, entry):
	'''	Apply a `MeasurementValue` to the NUM content item copied from its source: the concept, the
		number (with its alternative numeric representations) and the unit, each only when it differs,
		so that an untouched item stays as it was read. The children are left alone.

		@input item (highdicom content item): the copied NUM item, modified in place
		@input entry (MeasurementValue): the value written
	'''
	if entry.name is not None and _concept_key(entry.name) != _concept_key(item.ConceptNameCodeSequence[0]):
		item.ConceptNameCodeSequence = [_coded(entry.name)]

	value, unit, measured = _measured_value(item)
	if entry.value == value and (entry.unit is None or unit is None or _concept_key(entry.unit) == _concept_key(unit)):
		return
	if entry.value is None:
		item.MeasuredValueSequence = []
		return
	if measured is None:
		measured = Dataset()
		item.MeasuredValueSequence = [measured]
	if entry.value != value:
		measured.NumericValue = DS(entry.value, auto_format=True)
		for attribute in ('RationalNumeratorValue', 'RationalDenominatorValue'):
			if attribute in measured:
				delattr(measured, attribute)
		if isinstance(entry.value, float):
			measured.FloatingPointValue = entry.value
		elif 'FloatingPointValue' in measured:
			del measured.FloatingPointValue
	if entry.unit is not None:
		measured.MeasurementUnitsCodeSequence = [_coded(entry.unit)]


def srencode_measurement_values(measurement):
	'''	NUM items of the other values of a measurement group, in order. An item read from a document
		is re-emitted with its children; an edited concept, number or unit is written into a copy.

		@input measurement (Measurement)

		@returns list of highdicom content items
		@raises ValueError for a value that is not finite
		@raises SRDiagnosticError when a preserved item holds a by-reference relationship
	'''
	encoded = []
	for entry in measurement.values.values():
		if entry.value is not None and not math.isfinite(entry.value):
			raise ValueError('Measurement value %s of %s is not finite and cannot be written' % (entry.value, entry.name))
		if entry.item is None:
			encoded.append(NumContentItem(name=entry.name, value=entry.value, unit=entry.unit, relationship_type=RelationshipTypeValues.CONTAINS))
			continue
		item = srcontent_from_dataset(entry.item)
		_update_value_item(item, entry)
		encoded.append(item)
	return encoded


# Parser

def _measured_value(item):
	'''	Value, unit and Measured Value Sequence item of a NUM content item. A stored number that is not
		finite reads as no value.

		@input item (pydicom.Dataset): the NUM content item

		@returns tuple (float or None, CodedConcept or None, pydicom.Dataset or None)
	'''
	sequence = item.get('MeasuredValueSequence')
	if not sequence:
		return None, None, None
	measured = sequence[0]
	value = measured.get('FloatingPointValue')
	if value is None:
		value = measured.get('NumericValue')
	if value is not None and not math.isfinite(float(value)):
		value = None
	units = measured.get('MeasurementUnitsCodeSequence')
	unit = CodedConcept.from_dataset(units[0], copy=True) if units else None
	return (None if value is None else float(value)), unit, measured


def _preserved(item, parent_value_type='NUM'):
	'''	Deep copy of a preserved content item that can be re-emitted: a missing Relationship Type,
		already reported by the repair pass, is filled here and below it.

		@input item (pydicom.Dataset): the item
		@input parent_value_type (str, default='NUM'): Value Type of its parent

		@returns pydicom.Dataset
	'''
	copied = copy.deepcopy(item)
	def fill(node, parent):
		if node.get('RelationshipType') is None:
			if parent == 'CONTAINER':
				node.RelationshipType = RelationshipTypeValues.CONTAINS.value
			elif node.get('ValueType') == 'IMAGE':
				node.RelationshipType = RelationshipTypeValues.INFERRED_FROM.value
			else:
				node.RelationshipType = RelationshipTypeValues.HAS_PROPERTIES.value
		for child in node.get('ContentSequence') or []:
			fill(child, node.get('ValueType'))
	fill(copied, parent_value_type)
	return copied


def _concept_key(code):
	'''	Key of a concept in `Measurement.values`.

		@input code (pydicom.Dataset, Code or CodedConcept): the concept

		@returns tuple (str, str): (code value, scheme designator)
	'''
	if hasattr(code, 'scheme_designator'):
		return (code.value, code.scheme_designator)
	return (dcm_code_value(code), str(code.get(DCMHEADER_CODING_SCHEME_DESIGNATOR, '') or ''))


def _derivation_of(item):
	'''	Derivation of a NUM content item: the value of its first `HAS CONCEPT MOD` Derivation
		`(121401, DCM)` child.

		@input item (pydicom.Dataset): the NUM content item

		@returns pydicom.Dataset (the coded concept), or None for an item without one
	'''
	for child in _children(item):
		if _relationship(child) == RelationshipTypeValues.HAS_CONCEPT_MOD.value and srcontent_is_named(child, DCMSR_DERIVATION):
			concepts = child.get('ConceptCodeSequence')
			return concepts[0] if concepts else None
	return None


def _value_key(item):
	'''	Key of a NUM content item in `Measurement.values`: its concept, followed by its Derivation when
		it has one, so that NUM items of one concept with different derivations (a mean and a maximum)
		are kept apart.

		@input item (pydicom.Dataset): the NUM content item

		@returns tuple
			- (code value, scheme designator) for an item without a Derivation
			- (code value, scheme designator, derivation code value, derivation scheme designator)
	'''
	key = _concept_key(item.ConceptNameCodeSequence[0])
	derivation = _derivation_of(item)
	return key + _concept_key(derivation) if derivation is not None else key


def _is_finding_site(item):
	return _value_type(item) == 'CODE' and any(srcontent_is_named(item, code) for code in DCMSR_FINDING_SITE_CONCEPTS)


def _free_text_label(item):
	'''	Label held by a `CORNERSTONEFREETEXT` finding site.

		@input item (pydicom.Dataset): a content item

		@returns str, or None for any other item
	'''
	if not _is_finding_site(item) or not item.get('ConceptCodeSequence'):
		return None
	code = item.ConceptCodeSequence[0]
	if dcm_code_value(code) != DCMSR_CORNERSTONE_FREE_TEXT:
		return None
	return str(code.get(DCMHEADER_CODE_MEANING, '') or '') or None


def _foreign_label(item):
	'''	Label held by a dcmjs Comment item or `CORNERSTONEFREETEXT` finding site.

		@input item (pydicom.Dataset): a content item

		@returns str, or None for any other item
	'''
	if srcontent_is_named(item, DCMSR_COMMENT) and _value_type(item) == 'TEXT':
		return _text_value(item)
	return _free_text_label(item)


def _parse_coordinates(item, diagnostics, strict, path):
	'''	Geometry of a SCOORD or SCOORD3D item.

		Diagnostics raised here: `scoord3d-without-frame-of-reference` (error) and
		`invalid-measurement-geometry` (warning, malformed data or a graphic type the domain does not
		define or points that are not finite). In either case the item is left to be preserved unmodelled.

		@input item (pydicom.Dataset): the coordinate item
		@input diagnostics (list): list the diagnostics are appended to
		@input strict (bool): raise SRDiagnosticError on `warning` and `error`
		@input path (str): content tree location, for the diagnostics

		@returns tuple (Geometry or None, Frame of Reference UID or None)
	'''
	from ...imaging.helpers.headers import dcm_header_uid
	value_type = _value_type(item)
	frame_uid = None
	if value_type == 'SCOORD3D':
		frame_uid = dcm_header_uid(item, DCMHEADER_SR_REF_REFERENCE_FRAME)
		if not frame_uid:
			sr_diagnostic(diagnostics, SR_DIAGNOSTIC_ERROR, SR_CONDITION_SCOORD3D_WITHOUT_FRAME,
				'SCOORD3D without a Referenced Frame of Reference UID preserved unmodelled', path=path, strict=strict)
			return None, None

	graphic_type = str(item.get('GraphicType', '') or '')
	dimension = 3 if value_type == 'SCOORD3D' else 2
	data = [float(v) for v in (item.get('GraphicData') or [])]
	try:
		DCMSR_GRAPHIC_TYPES[value_type](graphic_type)
	except ValueError:
		sr_diagnostic(diagnostics, SR_DIAGNOSTIC_WARNING, SR_CONDITION_INVALID_GEOMETRY,
			'%s graphic type "%s" is not defined; item preserved unmodelled' % (value_type, graphic_type), path=path, value=graphic_type, strict=strict)
		return None, None
	if not data or len(data) % dimension or not all(math.isfinite(v) for v in data):
		sr_diagnostic(diagnostics, SR_DIAGNOSTIC_WARNING, SR_CONDITION_INVALID_GEOMETRY,
			'%s holds %d coordinate values that do not form finite points; item preserved unmodelled' % (value_type, len(data)), path=path, strict=strict)
		return None, None

	points = [data[i:i + dimension] for i in range(0, len(data), dimension)]
	if value_type == 'SCOORD3D':
		return Geometry(DCMSR_GEOMETRY_DOMAIN_PATIENT, graphic_type, points), frame_uid
	images = [child for child in _children(item) if _value_type(child) == 'IMAGE']
	reference = image_reference_from_dataset(images[0]) if images else None
	return Geometry(DCMSR_GEOMETRY_DOMAIN_IMAGE, graphic_type, points, reference), None


def _generic(diagnostics, strict, path, message, value=None):
	sr_diagnostic(diagnostics, SR_DIAGNOSTIC_WARNING, SR_CONDITION_MEASUREMENT_READ_AS_GENERIC,
		message + '; read as a generic measurement', path=path, value=value, strict=strict)
	return Measurement, SONADOR_MEASUREMENT_TYPE_GENERIC


def _calibration_for(calibrations, reference):
	'''	Calibration of a referenced frame.

		@input calibrations (mapping or None): `ImageFrameGeometry` by SOP Instance UID or
			`(SOP Instance UID, frame number)`
		@input reference (ImageReference or None): the referenced frame

		@returns ImageFrameGeometry, or None when unknown
	'''
	if not calibrations or reference is None:
		return None
	return calibrations.get((reference.sop_instance_uid, reference.frame_number)) or calibrations.get(reference.sop_instance_uid)


def _note_value_checks(measurement, stored_unit, diagnostics, strict, path):
	'''	Compare the stored value of a parsed typed measurement with the value computed from its
		points. The stored value stays authoritative; only diagnostics are recorded.

		Diagnostics raised here: `degenerate-geometry` and `measurement-without-value` (warning),
		`measurement-uncalibrated`, `measurement-value-mismatch` and `measurement-value-unverified`
		(info).

		@input measurement (GeometricMeasurement): the measurement
		@input stored_unit (CodedConcept or None): unit read from the document
		@input diagnostics (list): list the diagnostics are appended to
		@input strict (bool): raise SRDiagnosticError on a `warning`
		@input path (str): content tree location, for the diagnostics
	'''
	if measurement.is_degenerate:
		sr_diagnostic(diagnostics, SR_DIAGNOSTIC_WARNING, SR_CONDITION_DEGENERATE_GEOMETRY,
			'coincident points; kept as read, but refused on write', path=path, strict=strict)
		return
	stored = measurement._val
	if stored is None:
		sr_diagnostic(diagnostics, SR_DIAGNOSTIC_WARNING, SR_CONDITION_MEASUREMENT_WITHOUT_VALUE,
			'NUM item without a value; the value is computed on write', path=path, strict=strict)
		return
	_note_stored_value(stored, stored_unit, measurement.computed_value(), diagnostics, path)


def _note_stored_value(stored, stored_unit, computed, diagnostics, path, label=None):
	'''	Compare one stored value with the value computed from the points: within 0.01 when both are in
		the same unit; a stored unit that differs from the computed one is reported, not converted. The
		stored value stays authoritative; only diagnostics are recorded.

		Diagnostics raised here (info): `measurement-uncalibrated` (a value stored in pixels),
		`measurement-value-unverified` (nothing computed) and `measurement-value-mismatch`.

		@input stored (float): the stored value
		@input stored_unit (CodedConcept or None): its unit as read
		@input computed (tuple (float, unit) or None): the value computed from the points
		@input diagnostics (list), path (str): diagnostics list and content tree location
		@input label (str, default=None): which value of a multi-value measurement, prefixed to the messages
	'''
	prefix = '%s ' % label if label else ''
	if getattr(stored_unit, 'value', None) == DCMSR_UNITS_PIXEL.value:
		sr_diagnostic(diagnostics, SR_DIAGNOSTIC_INFO, SR_CONDITION_MEASUREMENT_UNCALIBRATED,
			'%svalue stored in pixels: the measurement is uncalibrated' % prefix, path=path, value=stored)
	if computed is None:
		sr_diagnostic(diagnostics, SR_DIAGNOSTIC_INFO, SR_CONDITION_MEASUREMENT_VALUE_UNVERIFIED,
			'%sno usable calibration for the image frame; the stored value is kept and not verified' % prefix, path=path, value=stored)
		return
	value, unit = computed
	if getattr(unit, 'value', None) != getattr(stored_unit, 'value', None):
		sr_diagnostic(diagnostics, SR_DIAGNOSTIC_INFO, SR_CONDITION_MEASUREMENT_VALUE_MISMATCH,
			'%sstored unit %s differs from computed unit %s; the stored value is kept' % (prefix, getattr(stored_unit, 'value', None),
			getattr(unit, 'value', None)), path=path, value=stored)
	elif abs(value - stored) > DCMSR_VALUE_TOLERANCE:
		sr_diagnostic(diagnostics, SR_DIAGNOSTIC_INFO, SR_CONDITION_MEASUREMENT_VALUE_MISMATCH,
			'%sstored value %s differs from computed value %s by more than %s; the stored value is kept' % (prefix, stored, value,
			DCMSR_VALUE_TOLERANCE), path=path, value=stored)


def _group_tracking(group):
	'''	Tracking identifier and UID of a measurement group.

		@input group (pydicom.Dataset or None): the measurement group

		@returns tuple (str or None, str or None)
	'''
	from ...imaging.helpers.headers import dcm_header_uid
	identifier, uid = None, None
	for child in (_children(group) if group is not None else []):
		if srcontent_is_named(child, dcmcodes.DCM.TrackingIdentifier) and identifier is None:
			identifier = _text_value(child)
		elif srcontent_is_named(child, dcmcodes.DCM.TrackingUniqueIdentifier) and uid is None:
			uid = dcm_header_uid(child, 'UID')
	return identifier, uid


def _parse_num_properties(children, consumed, group_items, group_consumed, diagnostics, strict, path, geometry, tracking_uid,
		normal=None, foreign_labels=True, owner='NUM item'):
	'''	Read the per-measurement properties under a NUM item: the extended meta items and display
		attributes (with the legacy group-level layout), the View items, Source-Series and Drawing-Plane
		(patient domain only), a foreign label, coded finding sites, and every other child preserved.

		Diagnostics raised here: those of `srparse_item_properties`, `patient-provenance-on-image-domain`
		(info), `invalid-drawing-plane` (warning), `foreign-measurement-label` (info), and those of the
		validity checks of preserved items.

		@input children (list of pydicom.Dataset): children of the NUM item
		@input consumed (set): indices of `children` already read; those read here are added
		@input group_items (list of pydicom.Dataset): group-level siblings read as the legacy layout
		@input group_consumed (set): indices of `group_items` already read; those read here are added
		@input diagnostics (list), strict (bool), path (str): diagnostics list, strict mode, location
		@input geometry (Geometry or None): geometry of the measurement
		@input tracking_uid (str or None): tracking UID, the value of the drawing-plane diagnostic
		@input normal (sequence of 3 numbers, default=None): plane normal a drawing plane must be
			parallel to within 1 degree (circles)
		@input foreign_labels (bool, default=True): read a dcmjs label as the description and the coded finding
			sites; when False they stay with the preserved children
		@input owner (str, default='NUM item'): name of the item in the diagnostic messages

		@returns tuple (dict, DisplayAttributes or None, list of str, str or None, tuple or None, str or None,
			list of FindingSite, list of pydicom.Dataset): meta attributes, display, views, source series,
			drawing plane, drawing-plane text, finding sites, preserved children
	'''
	# Properties of the NUM item, then the legacy group level
	meta, display = srparse_item_properties(children, group_items, consumed, group_consumed, diagnostics, strict, path, {}, owner=owner)

	views = []
	for idx, child in enumerate(children):
		if idx not in consumed and srcontent_is_named(child, DCMSR_MEASUREMENT_VIEW) and _value_type(child) == 'TEXT':
			consumed.add(idx)
			if (_text_value(child) or '').strip():
				views.append(_text_value(child).strip())
	if not views:
		for idx, sibling in enumerate(group_items):
			if idx not in group_consumed and srcontent_is_named(sibling, DCMSR_MEASUREMENT_VIEW) and _value_type(sibling) == 'TEXT':
				group_consumed.add(idx)
				if (_text_value(sibling) or '').strip():
					views.append(_text_value(sibling).strip())

	# Where a patient-domain measurement was made; under an image-domain NUM the items are reported and
	# left in place, so that they are preserved with the other unmodelled children
	source_series, plane, plane_text = None, None, None
	for idx, child in enumerate(children):
		if idx in consumed:
			continue
		is_series = srcontent_is_named(child, DCMSR_MEASUREMENT_SOURCE_SERIES) and _value_type(child) == 'UIDREF'
		is_plane = srcontent_is_named(child, DCMSR_MEASUREMENT_DRAWING_PLANE) and _value_type(child) == 'TEXT'
		if not (is_series or is_plane):
			continue
		if geometry is None or not geometry.is_patient:
			sr_diagnostic(diagnostics, SR_DIAGNOSTIC_INFO, SR_CONDITION_PROVENANCE_ON_IMAGE_DOMAIN,
				'%s under a %s without patient-domain geometry; ignored and preserved' % (dcm_code_value(srcontent_name(child)), owner), path=path)
		elif is_series and source_series is None:
			consumed.add(idx)
			source_series = str(child.get('UID') or '').strip() or None
		elif is_plane and plane_text is None:
			consumed.add(idx)
			plane_text = _text_value(child) or ''
			plane = drawing_plane_from_text(plane_text)
			failed = 'six-finite-numbers' if plane is None else drawing_plane_failed_check(plane, geometry.points, normal=normal)
			if failed is not None:
				plane = None
				sr_diagnostic(diagnostics, SR_DIAGNOSTIC_WARNING, SR_CONDITION_INVALID_DRAWING_PLANE,
					'Drawing-Plane "%s" fails the check "%s"; read as absent and written back as read' % (plane_text, failed),
					path=path, value=tracking_uid, strict=strict)

	# A dcmjs label becomes the description; it is written back only in that form
	if foreign_labels and meta.get('description') is None:
		label = None
		for idx, child in enumerate(children):
			if idx not in consumed and _foreign_label(child):
				label = _foreign_label(child)
				consumed.add(idx)
				break
		if label is None:
			for idx, sibling in enumerate(group_items):
				if idx not in group_consumed and _foreign_label(sibling):
					label = _foreign_label(sibling)
					group_consumed.add(idx)
					break
		if label is not None:
			meta['description'] = label
			sr_diagnostic(diagnostics, SR_DIAGNOSTIC_INFO, SR_CONDITION_FOREIGN_MEASUREMENT_LABEL,
				'foreign label read as the description; written back as the Sonador description', path=path, value=label)

	# Coded finding sites under the NUM item
	finding_sites = []
	for idx, child in enumerate(children):
		if foreign_labels and idx not in consumed and _is_finding_site(child) and _relationship(child) == RelationshipTypeValues.HAS_CONCEPT_MOD.value:
			consumed.add(idx)
			finding_sites.append(FindingSite.from_dataset(child, copy=True))

	# Everything else under the NUM item is preserved there
	extra_items = []
	for idx, child in enumerate(children):
		if idx in consumed:
			continue
		if _note_by_reference(child, diagnostics, path):
			pass
		elif not srcontent_is_valid(child, diagnostics=diagnostics, strict=strict, path=path):
			continue
		extra_items.append(_preserved(child))

	return meta, display, views, source_series, plane, plane_text, finding_sites, extra_items


def srparse_measurement(item, group=None, strict=False, diagnostics=None, measurement_class=None, tracking_id=None, tracking_uid=None,
		group_consumed=None, allow_group_level=True, calibrations=None, repaired=False):
	'''	Build a measurement from a NUM content item: the value and unit, the geometry, the per-item
		properties in the nested, flat and legacy layouts, the other NUM items of the group (`values`)
		and everything else preserved under the item. The stored value is authoritative: a difference
		from the value computed from the points is only reported. Makes no request.

		The class follows from the tracking identifier: `measurement_class` overrides it, and a group
		with an unknown identifier, a concept the tool does not accept, or geometry the tool cannot use
		reads as a generic `Measurement`.

		A patient-domain NUM's `Sonador-Measurement.Source-Series` is read into `series_uid` and its
		`Sonador-Measurement.Drawing-Plane` into `drawing_plane`, keeping the text for write-back.

		Diagnostics raised here: `foreign-measurement-label` (info, a dcmjs Comment or
		`CORNERSTONEFREETEXT` finding site became the description), `patient-provenance-on-image-domain`
		(info, Source-Series or Drawing-Plane under a NUM without patient-domain geometry; the item is
		preserved, not read), `invalid-drawing-plane` (warning, the plane reads as None and its text is
		written back), `measurement-read-as-generic`,
		`angle-middle-points-differ` and those of `srparse_item_properties` (warning),
		`measurement-without-value` (warning, or `error` when there is no geometry either), those of
		`_note_value_checks` and `content-item-dropped` (error). `strict` raises on `warning` and `error`.

		@input item (pydicom.Dataset): the NUM content item
		@input group (pydicom.Dataset, default=None): measurement group holding the item
		@input strict (bool, default=False): raise SRDiagnosticError on `warning` and `error` diagnostics
		@input diagnostics (list, default=None): list the diagnostics are appended to
		@input measurement_class (type, default=the class of the tracking identifier): class instantiated
		@input tracking_id, tracking_uid (str, default=those of `group`): tracking identifier and UID
		@input group_consumed (set, default=new set): indices into the group's content sequence already
			read; those read here are added
		@input allow_group_level (bool, default=True): read the legacy group-level properties
		@input calibrations (mapping, default=None): `ImageFrameGeometry` by SOP Instance UID or
			`(SOP Instance UID, frame number)`
		@input repaired (bool, default=False): the document repair pass already recorded the missing
			Relationship Types

		@returns
			- Measurement (the class selected)
			- None for a NUM item the model cannot hold: no value and no usable geometry on a tool
				group (`error`), or no value in a group with an unknown identifier. The item is left to
				be preserved in the group.
	'''
	if group is not None and tracking_id is None and tracking_uid is None:
		tracking_id, tracking_uid = _group_tracking(group)
	path = _path(tracking_uid)
	if not repaired:
		_note_missing_relationships(group if group is not None else item, diagnostics, strict, path)

	group_children = _children(group) if group is not None else []
	group_consumed = group_consumed if group_consumed is not None else set()
	group_items = group_children if allow_group_level else []

	name = CodedConcept.from_dataset(item.ConceptNameCodeSequence[0], copy=True)
	value, unit, stored_item = _measured_value(item)
	qualifiers = item.get('NumericValueQualifierCodeSequence')
	qualifier = CodedConcept.from_dataset(qualifiers[0], copy=True) if qualifiers else None

	# Geometry: the first coordinate child; further ones are preserved
	children = _children(item)
	consumed = set()
	geometry, frame_uid, source_coordinates = None, None, None
	for idx, child in enumerate(children):
		if _is_measurement_geometry(child):
			geometry, frame_uid = _parse_coordinates(child, diagnostics, strict, path)
			if geometry is not None:
				consumed.add(idx)
				source_coordinates = copy.deepcopy(child)
			break

	# The class: the identifier decides, and what the document holds can only make it generic
	measurement_type = measurement_type_for_tracking_id(tracking_id)
	if measurement_class is None:
		measurement_class = measurement_class_for_type(measurement_type)
	else:
		measurement_type = getattr(measurement_class, 'default_type', measurement_type)
	typed = issubclass(measurement_class, GeometricMeasurement)

	if value is None and geometry is None:
		if typed:
			sr_diagnostic(diagnostics, SR_DIAGNOSTIC_ERROR, SR_CONDITION_MEASUREMENT_WITHOUT_VALUE,
				'NUM item with neither a value nor geometry preserved in the group', path=path, strict=strict)
		return None
	if not typed and value is None:
		return None
	if typed:
		accepted = DCMSR_MEASUREMENT_CONCEPTS.get(measurement_type, ())
		if accepted and not any(srcontent_is_named(item, code) for code in accepted):
			measurement_class, measurement_type = _generic(diagnostics, strict, path, 'NUM concept is not one the tool accepts', str(name.value))
		elif geometry is None:
			measurement_class, measurement_type = _generic(diagnostics, strict, path, 'tool group without usable geometry')
		elif geometry.graphic_type != measurement_class.graphic_type or len(geometry.points) != measurement_class.point_count:
			measurement_class, measurement_type = _generic(diagnostics, strict, path,
				'%s of %d points is not the geometry of the tool' % (geometry.graphic_type, len(geometry.points)))
		typed = issubclass(measurement_class, GeometricMeasurement)
		if not typed and value is None:
			return None

	# Properties of the NUM item (then the legacy group level), its provenance, label, finding sites and
	# the unmodelled children preserved under it
	meta, display, views, source_series, plane, plane_text, finding_sites, extra_items = _parse_num_properties(
		children, consumed, group_items, group_consumed, diagnostics, strict, path, geometry, tracking_uid)

	# The other NUM items of the group, keyed by concept and Derivation; a repeated key stays in the
	# group's preserved items
	values, group_sites = OrderedDict(), []
	own_key = _value_key(item)
	for idx, sibling in enumerate(group_children):
		if sibling is item:
			group_consumed.add(idx)
		elif idx not in group_consumed and _value_type(sibling) == 'NUM' and sibling.get('ConceptNameCodeSequence'):
			key = _value_key(sibling)
			if key == own_key or key in values:
				continue
			group_consumed.add(idx)
			_note_by_reference(sibling, diagnostics, path)
			sibling_value, sibling_unit, _ = _measured_value(sibling)
			values[key] = MeasurementValue(CodedConcept.from_dataset(sibling.ConceptNameCodeSequence[0], copy=True),
				sibling_value, sibling_unit, _preserved(sibling, 'CONTAINER'))
		elif idx not in group_consumed and _is_finding_site(sibling) and sibling.get('ConceptCodeSequence'):
			group_sites.append(CodedConcept.from_dataset(sibling.ConceptCodeSequence[0], copy=True))

	fields = dict(tracking_id=tracking_id, tracking_uid=tracking_uid, frame_of_reference_uid=frame_uid, display=display, views=views,
		series_uid=source_series,
		extra_items=extra_items, values=values, group_finding_sites=group_sites, finding_sites=finding_sites or None, qualifier=qualifier, **meta)
	if typed:
		calibration = _calibration_for(calibrations, geometry.reference)
		measurement = measurement_class(geometry=geometry, name=name, unit=unit, value=value, calibration=calibration, **fields)
		if plane_text is not None:
			measurement._set_parsed_drawing_plane(plane, plane_text)
		measurement._mark_parsed(copy.deepcopy(stored_item) if stored_item is not None else None, source_coordinates)
		if measurement_type == SONADOR_MEASUREMENT_TYPE_ANGLE and geometry.points[1] != geometry.points[2]:
			sr_diagnostic(diagnostics, SR_DIAGNOSTIC_WARNING, SR_CONDITION_ANGLE_MIDDLE_POINTS_DIFFER,
				'Angle polyline with distinct middle points; read from points 0, 1 and 3', path=path, strict=strict)
		_note_value_checks(measurement, unit, diagnostics, strict, path)
	else:
		measurement = Measurement(value, unit=unit, name=name, measurement_type=measurement_type or SONADOR_MEASUREMENT_TYPE_GENERIC,
			geometry=geometry, **fields)
		if plane_text is not None:
			measurement._set_parsed_drawing_plane(plane, plane_text)
		measurement._mark_parsed(copy.deepcopy(stored_item) if stored_item is not None else None, source_coordinates)
	return measurement


def srparse_group_measurement(group, measurement_type, tracking_id, tracking_uid, consumed, strict=False, diagnostics=None,
		calibrations=None, allow_group_level=True):
	'''	Measurement of a measurement group: the NUM item the tool's concept names, else the first NUM
		item, the other ones becoming its `values`; a circle group is read by `srparse_circle` and a bidirectional
		group by `srparse_bidirectional`. Used by
		the group router.

		@input group (pydicom.Dataset): Measurement Group content item
		@input measurement_type (str or None): type the tracking identifier routes to
		@input tracking_id, tracking_uid (str or None): tracking identifier and UID of the group
		@input consumed (set): indices into the group's content sequence already read; those read here
			are added
		@input strict (bool, default=False): raise SRDiagnosticError on `warning` and `error` diagnostics
		@input diagnostics (list, default=None): list the diagnostics are appended to
		@input calibrations (mapping, default=None): see `srparse_measurement`
		@input allow_group_level (bool, default=True): read the legacy group-level properties

		@returns Measurement, or None for a group without a NUM item the model can hold
	'''
	if measurement_type == SONADOR_MEASUREMENT_TYPE_CIRCLE:
		return srparse_circle(group, tracking_id, tracking_uid, consumed, strict=strict, diagnostics=diagnostics,
			calibrations=calibrations, allow_group_level=allow_group_level)
	if measurement_type == SONADOR_MEASUREMENT_TYPE_BIDIRECTIONAL:
		return srparse_bidirectional(group, tracking_id, tracking_uid, consumed, strict=strict, diagnostics=diagnostics,
			calibrations=calibrations, allow_group_level=allow_group_level)
	numbers = [child for idx, child in enumerate(_children(group))
		if idx not in consumed and _value_type(child) == 'NUM' and child.get('ConceptNameCodeSequence')]
	if not numbers:
		return None
	accepted = DCMSR_MEASUREMENT_CONCEPTS.get(measurement_type, ())
	primary = next((n for n in numbers if any(srcontent_is_named(n, code) for code in accepted)), numbers[0])
	return srparse_measurement(primary, group=group, strict=strict, diagnostics=diagnostics, tracking_id=tracking_id, tracking_uid=tracking_uid,
		group_consumed=consumed, allow_group_level=allow_group_level, calibrations=calibrations, repaired=True)


# Circles: Area, Perimeter and Radius NUM items sharing one geometry, properties under the Area NUM

def _circle_role(item):
	'''	Role of a NUM item in a circle group: matched by code (SCT, or the SRT equivalent), else by
		code meaning, as the Cornerstone3D reader matches it.

		@input item (pydicom.Dataset): a NUM content item
		@returns str (`area`, `perimeter`, `radius`), or None for any other concept
	'''
	for role, concepts in DCMSR_CIRCLE_CONCEPTS.items():
		if any(srcontent_is_named(item, code) for code in concepts):
			return role
	name = srcontent_name(item)
	meaning = str((name.get(DCMHEADER_CODE_MEANING) if name is not None else '') or '').strip().lower()
	return meaning if meaning in DCMSR_CIRCLE_CONCEPTS else None


def _carries_typed_properties(children):
	'''	Whether a NUM item carries any of the per-measurement properties of the model.

		@input children (list of pydicom.Dataset): the children of the NUM item
		@returns bool
	'''
	concepts = tuple(DCMSR_MEASUREMENT_META.values()) + (DCMSR_MEASUREMENT_VIEW, DCMSR_DISPLAY_ATTRIBUTES, DCMSR_MEASUREMENT_SOURCE_SERIES,
		DCMSR_MEASUREMENT_DRAWING_PLANE, DCMSR_MEASUREMENT_CONSTRUCTION_POINTS) + tuple(DCMSR_DISPLAY_COLOR_ROLES.values())
	return any(srcontent_is_named(child, code) for child in children for code in concepts)


def _preserved_children(children, consumed, diagnostics, strict, path):
	'''	Children of a NUM item kept unmodelled under it: every child not yet read, each checked by the
		validity checks of preserved items (an invalid one is dropped with their diagnostic).

		@input children (list of pydicom.Dataset): the children of the NUM item
		@input consumed (set): indices of `children` already read
		@input diagnostics (list), strict (bool), path (str): diagnostics list, strict mode, location

		@returns list of pydicom.Dataset
	'''
	found = []
	for idx, child in enumerate(children):
		if idx in consumed:
			continue
		if _note_by_reference(child, diagnostics, path):
			pass
		elif not srcontent_is_valid(child, diagnostics=diagnostics, strict=strict, path=path):
			continue
		found.append(_preserved(child))
	return found


def _circle_coordinates(measurement, source):
	'''	Coordinate item of one NUM item of a circle (`_coordinates_item`): a new item is an `ELLIPSE`
		(patient domain) or `CIRCLE` (image domain).

		@input measurement (CircleMeasurement)
		@input source (pydicom.Dataset or None): coordinate item of the NUM item as read

		@returns highdicom CoordinatesForMeasurement3D or CoordinatesForMeasurement
		@raises ValueError as `_coordinates_item`
	'''
	geometry = measurement.geometry
	return _coordinates_item(measurement, source, geometry.points, DCMSR_GRAPHIC_ELLIPSE if geometry.is_patient else DCMSR_GRAPHIC_CIRCLE)


def _coordinates_item(measurement, source, points, graphic_type):
	'''	Coordinate item of one NUM item of a multi-NUM measurement: the item that NUM was read with, copied
		with its points, Frame of Reference and image reference updated, while it is of the same value type
		and graphic type; else a new item of `graphic_type`, a SCOORD3D with the Frame of Reference (patient
		domain) or a SCOORD `SELECTED FROM` the referenced image (image domain).

		@input measurement (GeometricMeasurement): gives the domain, Frame of Reference and image reference
		@input source (pydicom.Dataset or None): coordinate item of the NUM item as read
		@input points (sequence of points): the points of this NUM item
		@input graphic_type (str): graphic type of the item

		@returns highdicom CoordinatesForMeasurement3D or CoordinatesForMeasurement
		@raises ValueError for a patient-domain measurement without a Frame of Reference, or an image-domain
			measurement without a SOP Instance UID
	'''
	geometry = measurement.geometry
	data = np.asarray(points, dtype=float)
	value_type = 'SCOORD3D' if geometry.is_patient else 'SCOORD'
	if geometry.is_patient and not measurement.frame_of_reference_uid:
		raise ValueError('A patient-domain measurement needs a Frame of Reference UID to be written')
	if source is not None and source.get('ValueType') == value_type and source.get('GraphicType') == graphic_type:
		item = copy.deepcopy(source)
		item.GraphicData = [float(v) for v in data.flatten()]
		if geometry.is_patient:
			item.ReferencedFrameOfReferenceUID = measurement.frame_of_reference_uid
		elif measurement.reference_changed:
			_set_image_reference(item, geometry.reference)
		_mark_source_unnamed(item)
		return CoordinatesForMeasurement3D.from_dataset(item, copy=False) if geometry.is_patient else CoordinatesForMeasurement.from_dataset(item, copy=False)
	if geometry.is_patient:
		return _mark_unnamed(CoordinatesForMeasurement3D(GraphicTypeValues3D(graphic_type), data, measurement.frame_of_reference_uid))
	reference = geometry.reference
	if reference is None or not reference.sop_instance_uid:
		raise ValueError('An image-domain measurement needs an image reference with a SOP Instance UID to be written')
	frames = [reference.frame_number] if reference.frame_number else None
	source_image = _mark_unnamed(SourceImageForRegion(reference.sop_class_uid or '', reference.sop_instance_uid, referenced_frame_numbers=frames))
	return _mark_unnamed(CoordinatesForMeasurement(GraphicTypeValues(graphic_type), data, source_image))


def srencode_construction_points(measurement):
	'''	`Sonador-Measurement.ConstructionPoints` item of a circle derived from three points: a
		`HAS PROPERTIES` SCOORD3D `POLYLINE` with the Frame of Reference (patient domain), or a SCOORD
		`POLYLINE` `SELECTED FROM` its image (image domain). None for a circle without construction points.

		@input measurement (CircleMeasurement)
		@returns list of highdicom content items: empty or one item
	'''
	points = measurement.construction_points
	if points is None:
		return []
	data = np.asarray(points, dtype=float)
	geometry = measurement.geometry
	if geometry.is_patient:
		return [Scoord3DContentItem(name=DCMSR_MEASUREMENT_CONSTRUCTION_POINTS, graphic_type=GraphicTypeValues3D.POLYLINE, graphic_data=data,
			frame_of_reference_uid=measurement.frame_of_reference_uid, relationship_type=RelationshipTypeValues.HAS_PROPERTIES)]
	item = ScoordContentItem(name=DCMSR_MEASUREMENT_CONSTRUCTION_POINTS, graphic_type=GraphicTypeValues.POLYLINE, graphic_data=data,
		relationship_type=RelationshipTypeValues.HAS_PROPERTIES)
	reference = geometry.reference
	frames = [reference.frame_number] if reference.frame_number else None
	item.ContentSequence = ContentSequence([_mark_unnamed(SourceImageForRegion(reference.sop_class_uid or '', reference.sop_instance_uid,
		referenced_frame_numbers=frames))])
	return [item]


def srencode_circle(measurement, sr_template=None, layout=None, diagnostics=None, strict=False):
	'''	The three NUM items of a circle, in the order Area (`mm2`), Perimeter (`mm`), Radius (`mm`),
		each `INFERRED FROM` its own copy of the geometry. The Area NUM carries the per-measurement
		children: description, text, location, View items, Source-Series and Drawing-Plane (patient
		domain), the display container, the construction points, then its preserved children; each other
		NUM carries its own preserved children. While the geometry is unchanged, each stored value is
		re-emitted verbatim with its unit and its numeric value qualifier; a circle read without a Radius
		NUM gains one, derived. After an edit the values are recomputed in mm and mm2, and a qualifier of a
		replaced value is not written (the `info` `measurement-qualifier-dropped`). Concepts are written
		as read, else with their SCT codes. Makes no request.

		The pixel statistic NUM items read with the circle follow the Radius NUM, verbatim (their
		Derivation, their own copy of the geometry and every other child) while the circle is unchanged. A
		circle whose points, image or Frame of Reference changed is written without them (the `info`
		`statistics-dropped`). Construction points read with the circle that it no longer carries (a new
		center, radius or normal clears them) record the `info` `construction-points-dropped`.

		@input measurement (CircleMeasurement)
		@input sr_template (class, default=highdicom Measurement): template class instantiated per NUM item
		@input layout (str, default=`DCMSR_LAYOUT_NESTED`): the only layout written
		@input diagnostics (list, default=None): list the write diagnostics are appended to
		@input strict (bool, default=False): refuse an unregistered View value

		@returns highdicom.sr.Measurement: the Area NUM item, followed by the Perimeter and Radius items and
			the statistic items
		@raises ValueError for a legacy circle without a normal, a missing Frame of Reference or image
			reference, or a drawing plane that fails validation
		@raises SRDiagnosticError for degenerate geometry, a radius that cannot be computed, or under
			`strict` an unregistered View value
	'''
	from highdicom.sr import Measurement as DcmMeasurement
	sr_template = sr_template or DcmMeasurement
	layout = layout or DCMSR_LAYOUT_NESTED
	if layout not in DCMSR_LAYOUTS_WRITTEN:
		raise ValueError('Unsupported per-item property layout: %s' % layout)
	resolved = measurement.resolve_circle_values(diagnostics=diagnostics)
	extras = {CIRCLE_VALUE_AREA: measurement.extra_items, CIRCLE_VALUE_PERIMETER: measurement.perimeter_extra_items,
		CIRCLE_VALUE_RADIUS: measurement.radius_extra_items}

	encoded = None
	for role in CIRCLE_VALUE_ROLES:
		value, unit, stored, qualifier = resolved[role]
		if value is not None and not math.isfinite(value):
			raise ValueError('Circle %s value %s is not finite and cannot be written' % (role, value))
		name = _coded(measurement.value_names.get(role) or DCMSR_CIRCLE_CONCEPTS[role][0])
		options = pick(measurement, ('qualifier', 'derivation', 'finding_sites', 'method', 'properties')) if role == CIRCLE_VALUE_AREA else {}
		coordinates = _circle_coordinates(measurement, measurement._num_coordinates.get(role))
		template = sr_template(name=name, value=value, unit=unit, algorithm_id=measurement.algorithm if role == CIRCLE_VALUE_AREA else None,
			referenced_coordinates=[coordinates], **options)
		item = template[0]
		if stored is not None:
			item.MeasuredValueSequence = Sequence([copy.deepcopy(stored)])
		if qualifier is not None:
			item.NumericValueQualifierCodeSequence = copy.deepcopy(qualifier)
		elif role in measurement._stored_qualifiers:
			sr_diagnostic(diagnostics, SR_DIAGNOSTIC_INFO, SR_CONDITION_QUALIFIER_DROPPED,
				'numeric value qualifier of the %s qualified the stored value, which the edited geometry replaces; not written' % role,
				path=_path(measurement.tracking_uid), value=dcm_code_value(measurement._stored_qualifiers[role][0]))
		children = []
		if role == CIRCLE_VALUE_AREA:
			children = srencode_meta_items(measurement)
			children.extend(srencode_measurement_views(measurement, diagnostics=diagnostics, strict=strict))
			children.extend(srencode_measurement_provenance(measurement))
			children.extend(srencode_display_items(measurement.display, layout=layout))
			children.extend(srencode_construction_points(measurement))
		children.extend(srcontent_extra_items(extras[role]))
		if children:
			if not hasattr(item, 'ContentSequence'):
				item.ContentSequence = ContentSequence()
			for child in children:
				item.ContentSequence.append(child)
		if encoded is None:
			encoded = template
		else:
			encoded.append(item)

	path = _path(measurement.tracking_uid)
	if measurement.statistics_dropped:
		sr_diagnostic(diagnostics, SR_DIAGNOSTIC_INFO, SR_CONDITION_STATISTICS_DROPPED,
			'pixel statistics read with the circle no longer describe it after the edit; not written', path=path,
			value=len(measurement._statistic_items))
	if measurement.construction_points_dropped:
		sr_diagnostic(diagnostics, SR_DIAGNOSTIC_INFO, SR_CONDITION_CONSTRUCTION_POINTS_DROPPED,
			'construction points read with the circle no longer describe it after the edit; not written', path=path)
	for item in measurement.statistic_items:
		encoded.append(srcontent_from_dataset(item))
	return encoded


def _parse_circle_coordinates(item, diagnostics, strict, path):
	'''	Geometry of a circle's coordinate item. The undefined `CIRCLE` SCOORD3D a stock Cornerstone3D
		adapter writes is accepted with the `warning` `legacy-circle-scoord3d`: center and circumference
		point, no plane. Every other item is read as `_parse_coordinates` reads it.

		@returns tuple (Geometry or None, Frame of Reference UID or None)
	'''
	from ...imaging.helpers.headers import dcm_header_uid
	if _value_type(item) != 'SCOORD3D' or str(item.get('GraphicType', '') or '') != DCMSR_GRAPHIC_CIRCLE:
		return _parse_coordinates(item, diagnostics, strict, path)
	frame_uid = dcm_header_uid(item, DCMHEADER_SR_REF_REFERENCE_FRAME)
	if not frame_uid:
		sr_diagnostic(diagnostics, SR_DIAGNOSTIC_ERROR, SR_CONDITION_SCOORD3D_WITHOUT_FRAME,
			'SCOORD3D without a Referenced Frame of Reference UID preserved unmodelled', path=path, strict=strict)
		return None, None
	data = [float(v) for v in (item.get('GraphicData') or [])]
	if len(data) != 6 or not all(math.isfinite(v) for v in data):
		sr_diagnostic(diagnostics, SR_DIAGNOSTIC_WARNING, SR_CONDITION_INVALID_GEOMETRY,
			'CIRCLE SCOORD3D holds %d coordinate values, not the two points of a center and a circumference point' % len(data),
			path=path, strict=strict)
		return None, None
	sr_diagnostic(diagnostics, SR_DIAGNOSTIC_WARNING, SR_CONDITION_LEGACY_CIRCLE,
		'CIRCLE SCOORD3D is not a defined graphic type; read as center and circumference point with no plane, '
		'written again only once a normal is set', path=path, strict=strict)
	return Geometry(DCMSR_GEOMETRY_DOMAIN_PATIENT, DCMSR_GRAPHIC_CIRCLE, [data[:3], data[3:]]), frame_uid


def _circle_shape_failure(geometry):
	'''	Why a geometry cannot be held by `CircleMeasurement`.

		@returns str, or None when it is a circle form (and an `ELLIPSE` is a circle)
	'''
	forms = {(DCMSR_GEOMETRY_DOMAIN_IMAGE, DCMSR_GRAPHIC_CIRCLE): 2, (DCMSR_GEOMETRY_DOMAIN_PATIENT, DCMSR_GRAPHIC_ELLIPSE): 4,
		(DCMSR_GEOMETRY_DOMAIN_PATIENT, DCMSR_GRAPHIC_CIRCLE): 2}
	if forms.get((geometry.domain, geometry.graphic_type)) != len(geometry.points):
		return '%s of %d points is not a circle form' % (geometry.graphic_type, len(geometry.points))
	if geometry.graphic_type == DCMSR_GRAPHIC_ELLIPSE:
		failed = circle_from_ellipse(geometry.points)
		if isinstance(failed, str):
			return 'ELLIPSE is not a circle (check "%s")' % failed
	return None


def _construction_points_reference_matches(item, geometry):
	'''	Whether image-domain construction points name the circle's own image: their `SELECTED FROM`
		IMAGE child has the circle's SOP Instance UID and frame (no frame on both, or the same frame).

		@input item (pydicom.Dataset): the construction-point SCOORD item
		@input geometry (Geometry): the circle's image-domain geometry
		@returns bool
	'''
	images = [child for child in _children(item) if _value_type(child) == 'IMAGE']
	if len(images) != 1 or geometry.reference is None:
		return False
	own = image_reference_from_dataset(images[0])
	return own is not None and own.sop_instance_uid == geometry.reference.sop_instance_uid \
		and (own.frame_number or None) == (geometry.reference.frame_number or None)


def _parse_construction_points(children, consumed, geometry, frame_uid, diagnostics, strict, path):
	'''	Construction points under a circle's NUM item: the first `Sonador-Measurement.ConstructionPoints`
		POLYLINE of three points in the measurement's domain, in its Frame of Reference (patient domain)
		or on its image and frame (image domain). Another form, or points on another image, frame or Frame
		of Reference, are left in place, preserved unmodelled under the NUM item, with the `warning`
		`invalid-measurement-geometry`; the circle then reads as having no construction points.

		@returns tuple of 3 points, or None
	'''
	from ...imaging.helpers.headers import dcm_header_uid
	value_type = 'SCOORD3D' if geometry.is_patient else 'SCOORD'
	dimension = 3 if geometry.is_patient else 2
	for idx, child in enumerate(children):
		if idx in consumed or not srcontent_is_named(child, DCMSR_MEASUREMENT_CONSTRUCTION_POINTS):
			continue
		data = [float(v) for v in (child.get('GraphicData') or [])]
		if geometry.is_patient:
			reference_ok = dcm_header_uid(child, DCMHEADER_SR_REF_REFERENCE_FRAME) == frame_uid
		else:
			reference_ok = _construction_points_reference_matches(child, geometry)
		if _value_type(child) != value_type or str(child.get('GraphicType', '')) != 'POLYLINE' or len(data) != 3 * dimension \
				or not all(math.isfinite(v) for v in data):
			sr_diagnostic(diagnostics, SR_DIAGNOSTIC_WARNING, SR_CONDITION_INVALID_GEOMETRY,
				'construction points that are not a %s POLYLINE of three points; preserved unmodelled' % value_type,
				path=path, strict=strict)
			return None
		if not reference_ok:
			sr_diagnostic(diagnostics, SR_DIAGNOSTIC_WARNING, SR_CONDITION_INVALID_GEOMETRY,
				'construction points on another %s than the circle; preserved unmodelled'
				% ('Frame of Reference' if geometry.is_patient else 'image or frame'), path=path, strict=strict)
			return None
		consumed.add(idx)
		return tuple(tuple(data[i:i + dimension]) for i in range(0, len(data), dimension))
	return None


def _note_circle_value_checks(measurement, stored, diagnostics, strict, path):
	'''	Compare each stored circle value with the value computed from the geometry, after converting
		its UCUM unit to mm or mm2: radius and perimeter within 0.01 mm, area within 0.05 mm2 or 1 part in
		10 000 of the value, whichever is larger. The stored values stay authoritative; a difference is an
		`info`, and so is a unit that does not convert (the value is then not verified).

		Diagnostics raised here: `degenerate-geometry` (warning), `measurement-value-unverified` and
		`measurement-value-mismatch` (info).

		@input measurement (CircleMeasurement)
		@input stored (dict): role -> (value or None, unit or None)
	'''
	if measurement.is_degenerate:
		sr_diagnostic(diagnostics, SR_DIAGNOSTIC_WARNING, SR_CONDITION_DEGENERATE_GEOMETRY,
			'circle with a radius below %s or collinear construction points; kept as read, but refused on write' % DCMSR_CIRCLE_LENGTH_TOLERANCE,
			path=path, strict=strict)
		return
	computed = measurement.computed_value()
	if computed is None:
		if any(value is not None for value, _ in stored.values()):
			sr_diagnostic(diagnostics, SR_DIAGNOSTIC_INFO, SR_CONDITION_MEASUREMENT_VALUE_UNVERIFIED,
				'no usable calibration for the image frame; the stored circle values are kept and not verified', path=path)
		return
	radius = computed[0]
	expected = {CIRCLE_VALUE_AREA: (math.pi * radius ** 2, 'area'), CIRCLE_VALUE_PERIMETER: (2.0 * math.pi * radius, 'length'),
		CIRCLE_VALUE_RADIUS: (radius, 'length')}
	for role, (value, unit) in stored.items():
		if value is None:
			continue
		target, kind = expected[role]
		scale = circle_unit_scale(unit, kind)
		tolerance = max(DCMSR_CIRCLE_AREA_TOLERANCE, DCMSR_CIRCLE_AREA_RELATIVE_TOLERANCE * abs(target)) if role == CIRCLE_VALUE_AREA \
			else DCMSR_CIRCLE_LENGTH_TOLERANCE
		if scale is None:
			sr_diagnostic(diagnostics, SR_DIAGNOSTIC_INFO, SR_CONDITION_MEASUREMENT_VALUE_UNVERIFIED,
				'stored %s unit %s does not convert to %s; the stored value is kept and not verified'
				% (role, getattr(unit, 'value', None), 'mm2' if kind == 'area' else 'mm'), path=path, value=value)
		elif abs(value * scale - target) > tolerance:
			sr_diagnostic(diagnostics, SR_DIAGNOSTIC_INFO, SR_CONDITION_MEASUREMENT_VALUE_MISMATCH,
				'stored %s %s (%s %s) differs from computed %s by more than %s; the stored value is kept'
				% (role, value, value * scale, 'mm2' if kind == 'area' else 'mm', target, tolerance),
				path=path, value=value)


def _statistic_quantity_of_unit(unit):
	'''	Quantity of a stock Cornerstone3D statistic NUM (one named by the statistic's own code), from its
		unit. No unit, or the UCUM unit `1`, names no quantity; nor does a UCUM unit of no known
		quantity, which is still read. `HU` and `[hnsf'U]` are the attenuation coefficient of CT, and
		body-weight SUV is SUVbw of PT.

		@input unit (pydicom.Dataset or None): the unit coded concept

		@returns
			- tuple (modality unit, quantity key or None): the identity statistics of one circle share
			- None for a unit that is not UCUM, or an SUV normalization other than body weight
	'''
	from ...imaging.helpers.headers import dcm_header_value
	value = dcm_code_value(unit) if unit is not None else None
	scheme = dcm_header_value(unit, DCMHEADER_CODING_SCHEME_DESIGNATOR) if unit is not None else None
	if not value or value == '1':
		return ('', None) if unit is None or scheme == 'UCUM' else None
	if scheme != 'UCUM':
		return None
	for key, (_, quantity_unit, modality_unit) in DCMSR_CIRCLE_STATISTIC_QUANTITIES.items():
		if value == quantity_unit.value:
			return modality_unit, key
	upper = value.upper()
	if upper == 'HU':
		return 'HU', 'CT'
	if upper in DCMSR_BODY_WEIGHT_SUV_UNITS:
		return 'SUV', 'PT'
	if upper.startswith('SUV') or '{SUV' in upper:
		return None
	return value, None


def _circle_statistic(item):
	'''	The pixel statistic a NUM item of a circle group holds, in either form: named by the quantity
		(`DCMSR_CIRCLE_STATISTIC_QUANTITIES`) in its UCUM unit, with a Derivation of CID 7464; or, as stock
		Cornerstone3D writes it, named by the statistic's code in a UCUM unit. Codes match with their
		scheme, SCT or the SRT equivalent. A value that is missing or not finite is no statistic.

		@input item (pydicom.Dataset): a NUM content item

		@returns
			- tuple (field, value, identity, quantity, unit): the `CircleStatistics` field, the number, the
				quantity identity (`_statistic_quantity_of_unit`), the quantity concept (CodedConcept or None)
				and the unit (CodedConcept or None)
			- None for an item that is no statistic this client reads
	'''
	value, unit, measured = _measured_value(item)
	if value is None:
		return None
	units = measured.get('MeasurementUnitsCodeSequence')
	unit_item = units[0] if units else None
	derivation = _derivation_of(item)
	# A Derivation modifier, even one without a value, makes it the quantity form
	if any(_relationship(child) == RelationshipTypeValues.HAS_CONCEPT_MOD.value and srcontent_is_named(child, DCMSR_DERIVATION)
			for child in _children(item)):
		field = next((name for name, codes in DCMSR_CIRCLE_STATISTICS.items() if any(dcm_code_matches(derivation, code) for code in codes)), None)
		quantity = next((key for key, (concept, quantity_unit, _) in DCMSR_CIRCLE_STATISTIC_QUANTITIES.items()
			if srcontent_is_named(item, concept) and dcm_code_matches(unit_item, quantity_unit)), None)
		if field is None or quantity is None:
			return None
		identity = (DCMSR_CIRCLE_STATISTIC_QUANTITIES[quantity][2], quantity)
		return field, value, identity, CodedConcept.from_dataset(item.ConceptNameCodeSequence[0], copy=True), unit
	field = next((name for name, codes in DCMSR_CIRCLE_STATISTICS.items() if any(srcontent_is_named(item, code) for code in codes)), None)
	identity = _statistic_quantity_of_unit(unit_item) if field is not None else None
	if identity is None:
		return None
	concept = DCMSR_CIRCLE_STATISTIC_QUANTITIES[identity[1]][0] if identity[1] is not None else None
	return field, value, identity, (_coded(concept) if concept is not None else None), unit


def _parse_circle_statistics(group_children, consumed, diagnostics, path):
	'''	Pixel statistics of a circle group: the first NUM item of each statistic (`_circle_statistic`)
		among the group's items not yet read. Statistics of different quantities are not read at all, so
		that one quantity is never reported as another; unread items stay unmodelled in the group. Once
		read, the statistics own every statistic NUM item of the group, a repeated one included, so that
		all of them are written back while the circle is unchanged and none survives an edit.

		The quantity is the concept naming the first statistic in the quantity form, else the quantity
		the unit of a stock statistic identifies (None for a unit of no known quantity); the unit is that
		of the first statistic in document order.

		@input group_children (list of pydicom.Dataset): the children of the measurement group
		@input consumed (set): indices into `group_children` already read; those read here are added

		@returns tuple (CircleStatistics or None, list of pydicom.Dataset): the statistics and every
			statistic NUM item of the group, in document order, copied for write-back
	'''
	found, owned = OrderedDict(), []
	for idx, child in enumerate(group_children):
		if idx in consumed or _value_type(child) != 'NUM' or not child.get('ConceptNameCodeSequence'):
			continue
		statistic = _circle_statistic(child)
		if statistic is None:
			continue
		owned.append(idx)
		if statistic[0] not in found:
			found[statistic[0]] = (idx,) + statistic
	if not found or len({entry[3] for entry in found.values()}) > 1:
		return None, []
	entries = sorted(found.values(), key=lambda entry: entry[0])
	items = []
	for idx in owned:
		consumed.add(idx)
		_note_by_reference(group_children[idx], diagnostics, path)
		items.append(_preserved(group_children[idx], 'CONTAINER'))
	quantity = next((entry[4] for entry in entries if entry[4] is not None), None)
	values = {name: (found[name][2] if name in found else None) for name in DCMSR_CIRCLE_STATISTICS}
	return CircleStatistics(quantity=quantity, unit=entries[0][5], **values), items


def srparse_circle(group, tracking_id, tracking_uid, consumed, strict=False, diagnostics=None, calibrations=None, allow_group_level=True):
	'''	Circle of a `CircleROI` measurement group: the Area, Perimeter and Radius NUM items (by code,
		SCT or SRT, else by meaning), the geometry from the first coordinate item among them in document
		order (`CIRCLE` image domain, `ELLIPSE` patient domain, the legacy `CIRCLE` SCOORD3D with a
		`warning`), the typed properties from the first of those NUM items in document order that carries
		any (with the legacy group-level layout), and every other child kept under its own NUM item. A
		group whose NUM items or geometry the circle model cannot hold reads as a generic `Measurement`
		with the `warning` `measurement-read-as-generic`. Makes no request.

		The geometry is the measurement's own coordinate item (`_is_measurement_geometry`), whatever the
		order of the NUM's children; construction points count only on the circle's own image and frame
		(image domain) or Frame of Reference (patient domain), and are otherwise preserved unmodelled.
		Each NUM's stored value keeps its unit and numeric value qualifier. The pixel statistics of the
		group are read into `statistics` (`_parse_circle_statistics`) and kept out of `values`.

		Diagnostics raised here: `legacy-circle-scoord3d`, `degenerate-geometry`, `measurement-read-as-generic`,
		`invalid-measurement-geometry` (construction points in another form or on another image, frame or
		Frame of Reference) and `invalid-drawing-plane` (warning, a drawing plane whose normal is not
		parallel to the circle's within 1 degree included), the value checks of `_note_circle_value_checks`
		(info) and those of `_parse_num_properties`. `strict` raises on `warning` and `error`.

		@input group (pydicom.Dataset): the measurement group
		@input tracking_id, tracking_uid (str or None): tracking identifier and UID of the group
		@input consumed (set): indices into the group's content sequence already read; those read here are added
		@input strict (bool, default=False): raise SRDiagnosticError on `warning` and `error` diagnostics
		@input diagnostics (list, default=None): list the diagnostics are appended to
		@input calibrations (mapping, default=None): see `srparse_measurement`
		@input allow_group_level (bool, default=True): read the legacy group-level properties

		@returns
			- CircleMeasurement
			- Measurement (generic) for a group the circle model cannot hold
			- None for a group without a NUM item the model can hold
	'''
	path = _path(tracking_uid)
	group_children = _children(group)
	nums = OrderedDict()
	for idx, child in enumerate(group_children):
		if idx in consumed or _value_type(child) != 'NUM' or not child.get('ConceptNameCodeSequence'):
			continue
		role = _circle_role(child)
		if role is not None and role not in nums:
			nums[role] = (idx, child)

	def generic(message):
		'''	The group read as a generic `Measurement` (its Radius NUM, else its first NUM, as the primary
			item), with the `measurement-read-as-generic` warning.

			@returns Measurement, or None for a group without a NUM item
		'''
		numbers = [child for idx, child in enumerate(group_children)
			if idx not in consumed and _value_type(child) == 'NUM' and child.get('ConceptNameCodeSequence')]
		if not numbers:
			return None
		_generic(diagnostics, strict, path, message)
		primary = nums[CIRCLE_VALUE_RADIUS][1] if CIRCLE_VALUE_RADIUS in nums else numbers[0]
		return srparse_measurement(primary, group=group, strict=strict, diagnostics=diagnostics, measurement_class=Measurement,
			tracking_id=tracking_id, tracking_uid=tracking_uid, group_consumed=consumed, allow_group_level=allow_group_level,
			calibrations=calibrations, repaired=True)

	if not nums:
		return generic('circle group without an Area, Perimeter or Radius NUM item')

	ordered = sorted(nums.items(), key=lambda entry: entry[1][0])
	geometry, frame_uid = None, None
	for role, (idx, item) in ordered:
		coordinate = next((child for child in _children(item) if _is_measurement_geometry(child)), None)
		if coordinate is not None:
			geometry, frame_uid = _parse_circle_coordinates(coordinate, diagnostics, strict, path)
			break
	if geometry is None:
		return generic('circle group without usable geometry')
	failure = _circle_shape_failure(geometry)
	if failure is not None:
		return generic(failure)

	# Each NUM item: its own coordinate item (when it holds the geometry) and its children
	per_num = OrderedDict()
	for role, (idx, item) in ordered:
		children = _children(item)
		own, source = set(), None
		for cidx, child in enumerate(children):
			if _is_measurement_geometry(child):
				if _value_type(child) == ('SCOORD3D' if geometry.is_patient else 'SCOORD') and str(child.get('GraphicType', '')) == geometry.graphic_type:
					own.add(cidx)
					source = copy.deepcopy(child)
				break
		per_num[role] = {'item': item, 'children': children, 'consumed': own, 'source': source}

	# The typed properties: the first NUM in document order carrying any, else the Area NUM
	source_role = next((role for role, entry in per_num.items()
		if _carries_typed_properties([c for i, c in enumerate(entry['children']) if i not in entry['consumed']])), None)
	source_role = source_role or (CIRCLE_VALUE_AREA if CIRCLE_VALUE_AREA in per_num else next(iter(per_num)))
	group_items = group_children if allow_group_level else []

	# A provisional circle gives the normal the drawing plane is checked against
	normal = circle_from_ellipse(geometry.points)['normal'] if geometry.graphic_type == DCMSR_GRAPHIC_ELLIPSE else None
	extras, properties = {}, None
	for role, entry in per_num.items():
		if role == source_role:
			construction = _parse_construction_points(entry['children'], entry['consumed'], geometry, frame_uid, diagnostics, strict, path)
			properties = _parse_num_properties(entry['children'], entry['consumed'], group_items, consumed, diagnostics, strict, path,
				geometry, tracking_uid, normal=normal) + (construction,)
			extras[role] = properties[7]
		else:
			extras[role] = _preserved_children(entry['children'], entry['consumed'], diagnostics, strict, path)
	meta, display, views, source_series, plane, plane_text, finding_sites, _, construction = properties

	stored, names, stored_items, qualifiers = {}, {}, {}, {}
	for role, entry in per_num.items():
		_note_by_reference(entry['item'], diagnostics, path)
		value, unit, measured = _measured_value(entry['item'])
		stored[role] = (value, unit)
		names[role] = CodedConcept.from_dataset(entry['item'].ConceptNameCodeSequence[0], copy=True)
		stored_items[role] = copy.deepcopy(measured) if measured is not None and value is not None else None
		if entry['item'].get('NumericValueQualifierCodeSequence'):
			qualifiers[role] = copy.deepcopy(entry['item'].NumericValueQualifierCodeSequence)
	for role, (idx, item) in nums.items():
		consumed.add(idx)
	statistics, statistic_items = _parse_circle_statistics(group_children, consumed, diagnostics, path)

	group_sites = [CodedConcept.from_dataset(sibling.ConceptCodeSequence[0], copy=True) for idx, sibling in enumerate(group_children)
		if idx not in consumed and _is_finding_site(sibling) and sibling.get('ConceptCodeSequence')]

	radius_value, radius_unit = stored.get(CIRCLE_VALUE_RADIUS, (None, None))
	measurement = CircleMeasurement(geometry=geometry, value=radius_value, unit=radius_unit or DCMSR_UNITS_MM,
		name=names.get(CIRCLE_VALUE_RADIUS), area=stored.get(CIRCLE_VALUE_AREA, (None, None))[0],
		perimeter=stored.get(CIRCLE_VALUE_PERIMETER, (None, None))[0], value_names=names,
		area_unit=stored.get(CIRCLE_VALUE_AREA, (None, None))[1], perimeter_unit=stored.get(CIRCLE_VALUE_PERIMETER, (None, None))[1],
		calibration=_calibration_for(calibrations, geometry.reference), tracking_id=tracking_id, tracking_uid=tracking_uid,
		frame_of_reference_uid=frame_uid, display=display, views=views, series_uid=source_series,
		extra_items=extras.get(CIRCLE_VALUE_AREA, ()), perimeter_extra_items=extras.get(CIRCLE_VALUE_PERIMETER, ()),
		radius_extra_items=extras.get(CIRCLE_VALUE_RADIUS, ()), group_finding_sites=group_sites,
		finding_sites=finding_sites or None, construction_points=construction, **meta)
	if plane_text is not None:
		measurement._set_parsed_drawing_plane(plane, plane_text)
	measurement._stored_items = stored_items
	measurement._stored_qualifiers = qualifiers
	measurement._num_coordinates = {role: entry['source'] for role, entry in per_num.items()}
	if statistics is not None:
		measurement._set_parsed_statistics(statistics, statistic_items)
	measurement._mark_parsed(stored_items.get(CIRCLE_VALUE_RADIUS), per_num[source_role]['source'])
	_note_circle_value_checks(measurement, stored, diagnostics, strict, path)
	return measurement


# Bidirectional: a Long Axis and a Short Axis NUM item, each with its own POLYLINE, properties under the Long Axis NUM

def _bidirectional_role(item):
	'''	Role of a NUM item in a bidirectional group: matched by code (SCT, or the SRT equivalent), else by
		code meaning, as the Cornerstone3D reader matches it.

		@input item (pydicom.Dataset): a NUM content item
		@returns str (`long_axis`, `short_axis`), or None for any other concept
	'''
	for role, concepts in DCMSR_BIDIRECTIONAL_CONCEPTS.items():
		if any(srcontent_is_named(item, code) for code in concepts):
			return role
	name = srcontent_name(item)
	meaning = str((name.get(DCMHEADER_CODE_MEANING) if name is not None else '') or '').strip().lower()
	return next((role for role, concepts in DCMSR_BIDIRECTIONAL_CONCEPTS.items() if concepts[0].meaning.lower() == meaning), None)


def srencode_bidirectional(measurement, sr_template=None, layout=None, diagnostics=None, strict=False):
	'''	The two NUM items of a bidirectional measurement, Long Axis then Short Axis, each `INFERRED FROM` its
		own two-point `POLYLINE`. The Long Axis NUM carries the per-measurement children: description, text,
		location, View items, Source-Series and Drawing-Plane (patient domain), the display container, then
		its preserved children; the Short Axis NUM carries its own preserved children. A stored length whose
		axis is unchanged is re-emitted verbatim with its unit and numeric value qualifier; an edited axis is
		recomputed, and a qualifier of its replaced value is not written (the `info`
		`measurement-qualifier-dropped`). The axes are written as held, never swapped: a short axis longer
		than the long axis is recorded as the `info` `axes-not-ordered`. Concepts are written as read, else
		with their SCT codes. Makes no request.

		@input measurement (BidirectionalMeasurement)
		@input sr_template (class, default=highdicom Measurement): template class instantiated per NUM item
		@input layout (str, default=`DCMSR_LAYOUT_NESTED`): the only layout written
		@input diagnostics (list, default=None): list the write diagnostics are appended to
		@input strict (bool, default=False): refuse an unregistered View value

		@returns highdicom.sr.Measurement: the Long Axis NUM item, followed by the Short Axis NUM item
		@raises ValueError for a missing Frame of Reference or image reference, or a drawing plane that
			fails validation
		@raises SRDiagnosticError for degenerate geometry, a length that cannot be computed, or under
			`strict` an unregistered View value
	'''
	from highdicom.sr import Measurement as DcmMeasurement
	sr_template = sr_template or DcmMeasurement
	layout = layout or DCMSR_LAYOUT_NESTED
	if layout not in DCMSR_LAYOUTS_WRITTEN:
		raise ValueError('Unsupported per-item property layout: %s' % layout)
	resolved = measurement.resolve_axis_values(diagnostics=diagnostics)
	path = _path(measurement.tracking_uid)
	if not measurement.axes_ordered:
		sr_diagnostic(diagnostics, SR_DIAGNOSTIC_INFO, SR_CONDITION_AXES_NOT_ORDERED,
			'short axis longer than the long axis; written as given, the axes are not swapped', path=path,
			value=resolved[BIDIRECTIONAL_VALUE_SHORT_AXIS][0])
	extras = {BIDIRECTIONAL_VALUE_LONG_AXIS: measurement.extra_items, BIDIRECTIONAL_VALUE_SHORT_AXIS: measurement.short_axis_extra_items}

	encoded = None
	for role in BIDIRECTIONAL_VALUE_ROLES:
		value, unit, stored, qualifier = resolved[role]
		if value is not None and not math.isfinite(value):
			raise ValueError('Bidirectional %s value %s is not finite and cannot be written' % (role.replace('_', ' '), value))
		name = _coded(measurement.value_names.get(role) or DCMSR_BIDIRECTIONAL_CONCEPTS[role][0])
		is_long = role == BIDIRECTIONAL_VALUE_LONG_AXIS
		options = pick(measurement, ('qualifier', 'derivation', 'finding_sites', 'method', 'properties')) if is_long else {}
		coordinates = _coordinates_item(measurement, measurement._num_coordinates.get(role), measurement._axis_points(role),
			DCMSR_GRAPHIC_POLYLINE)
		template = sr_template(name=name, value=value, unit=unit, algorithm_id=measurement.algorithm if is_long else None,
			referenced_coordinates=[coordinates], **options)
		item = template[0]
		if stored is not None:
			item.MeasuredValueSequence = Sequence([copy.deepcopy(stored)])
		if qualifier is not None:
			item.NumericValueQualifierCodeSequence = copy.deepcopy(qualifier)
		elif role in measurement._stored_qualifiers:
			sr_diagnostic(diagnostics, SR_DIAGNOSTIC_INFO, SR_CONDITION_QUALIFIER_DROPPED,
				'numeric value qualifier of the %s qualified the stored value, which the edited axis replaces; not written' % role.replace('_', ' '),
				path=path, value=dcm_code_value(measurement._stored_qualifiers[role][0]))
		children = []
		if is_long:
			children = srencode_meta_items(measurement)
			children.extend(srencode_measurement_views(measurement, diagnostics=diagnostics, strict=strict))
			children.extend(srencode_measurement_provenance(measurement))
			children.extend(srencode_display_items(measurement.display, layout=layout))
		children.extend(srcontent_extra_items(extras[role]))
		if children:
			if not hasattr(item, 'ContentSequence'):
				item.ContentSequence = ContentSequence()
			for child in children:
				item.ContentSequence.append(child)
		if encoded is None:
			encoded = template
		else:
			encoded.append(item)
	return encoded


def _note_bidirectional_value_checks(measurement, stored, diagnostics, strict, path):
	'''	Diagnostics of a parsed bidirectional measurement: degenerate geometry, axes whose units differ,
		axes not ordered, and each stored length compared with the length computed from its axis
		(`_note_stored_value`). The stored lengths stay authoritative.

		Diagnostics raised here: `degenerate-geometry` and `axes-units-differ` (warning), `axes-not-ordered`
		and those of `_note_stored_value` (info).

		@input measurement (BidirectionalMeasurement)
		@input stored (dict): role -> (value or None, unit or None) as read
	'''
	if measurement.is_degenerate:
		sr_diagnostic(diagnostics, SR_DIAGNOSTIC_WARNING, SR_CONDITION_DEGENERATE_GEOMETRY,
			'an axis shorter than %s; kept as read, but refused on write' % DCMSR_BIDIRECTIONAL_LENGTH_TOLERANCE, path=path, strict=strict)
		return
	units = [getattr(unit, 'value', None) for value, unit in stored.values() if value is not None]
	if len(set(units)) > 1:
		sr_diagnostic(diagnostics, SR_DIAGNOSTIC_WARNING, SR_CONDITION_AXES_UNITS_DIFFER,
			'long and short axis lengths are stored in different units (%s); read as stored and written back as read' % ', '.join(units),
			path=path, value=measurement.tracking_uid, strict=strict)
	if not measurement.axes_ordered:
		sr_diagnostic(diagnostics, SR_DIAGNOSTIC_INFO, SR_CONDITION_AXES_NOT_ORDERED,
			'short axis longer than the long axis; read as labelled, the axes are not swapped', path=path, value=measurement.width)
	for role in BIDIRECTIONAL_VALUE_ROLES:
		value, unit = stored[role]
		if value is None:
			sr_diagnostic(diagnostics, SR_DIAGNOSTIC_WARNING, SR_CONDITION_MEASUREMENT_WITHOUT_VALUE,
				'%s NUM item without a value; the length is computed on write' % role.replace('_', ' '), path=path, strict=strict)
			continue
		_note_stored_value(value, unit, measurement.computed_axis_value(role), diagnostics, path, label=role.replace('_', ' '))


def srparse_bidirectional(group, tracking_id, tracking_uid, consumed, strict=False, diagnostics=None, calibrations=None, allow_group_level=True):
	'''	Bidirectional measurement of a `Bidirectional` measurement group: the Long Axis and Short Axis NUM
		items (by code, SCT or SRT, else by meaning, in any order), each with its own two-point `POLYLINE`
		(SCOORD on the same image, or SCOORD3D in the same Frame of Reference), the typed properties from
		the first of the two NUM items in document order that carries any (with the legacy group-level
		layout; on write they are placed under the Long Axis NUM), and every other child, the other NUM's
		property items included, kept under its own NUM item. A group with one axis only, a NUM item
		without its own geometry (none, by reference, or a copy of the other axis's), or axes of another
		form, domain, image or Frame of Reference reads as a generic `Measurement` with the `warning`
		`measurement-read-as-generic`. Makes no request.

		The axes are read as labelled: a short axis longer than the long axis is the `info`
		`axes-not-ordered`. The stored lengths are authoritative, each kept with its unit and numeric value
		qualifier; lengths stored in different units are the `warning` `axes-units-differ`. A Drawing-Plane is
		valid only when all four points lie within 1 mm of it.

		Diagnostics raised here: `degenerate-geometry`, `axes-units-differ`, `measurement-without-value`,
		`measurement-read-as-generic` and `invalid-drawing-plane` (warning), `axes-not-ordered` and the value
		checks of `_note_stored_value` (info), and those of `_parse_num_properties`. `strict` raises on
		`warning` and `error`.

		@input group (pydicom.Dataset): the measurement group
		@input tracking_id, tracking_uid (str or None): tracking identifier and UID of the group
		@input consumed (set): indices into the group's content sequence already read; those read here are added
		@input strict (bool, default=False): raise SRDiagnosticError on `warning` and `error` diagnostics
		@input diagnostics (list, default=None): list the diagnostics are appended to
		@input calibrations (mapping, default=None): see `srparse_measurement`
		@input allow_group_level (bool, default=True): read the legacy group-level properties

		@returns
			- BidirectionalMeasurement
			- Measurement (generic) for a group the bidirectional model cannot hold
			- None for a group without a NUM item the model can hold
	'''
	path = _path(tracking_uid)
	group_children = _children(group)
	nums = OrderedDict()
	for idx, child in enumerate(group_children):
		if idx in consumed or _value_type(child) != 'NUM' or not child.get('ConceptNameCodeSequence'):
			continue
		role = _bidirectional_role(child)
		if role is not None and role not in nums:
			nums[role] = (idx, child)

	def generic(message):
		'''	The group read as a generic `Measurement` (its Long Axis NUM, else its first NUM, as the primary
			item), with the `measurement-read-as-generic` warning.

			@returns Measurement, or None for a group without a NUM item
		'''
		numbers = [child for idx, child in enumerate(group_children)
			if idx not in consumed and _value_type(child) == 'NUM' and child.get('ConceptNameCodeSequence')]
		if not numbers:
			return None
		_generic(diagnostics, strict, path, message)
		primary = nums[BIDIRECTIONAL_VALUE_LONG_AXIS][1] if BIDIRECTIONAL_VALUE_LONG_AXIS in nums else numbers[0]
		return srparse_measurement(primary, group=group, strict=strict, diagnostics=diagnostics, measurement_class=Measurement,
			tracking_id=tracking_id, tracking_uid=tracking_uid, group_consumed=consumed, allow_group_level=allow_group_level,
			calibrations=calibrations, repaired=True)

	if len(nums) != 2:
		return generic('bidirectional group without both a Long Axis and a Short Axis NUM item')

	# Each NUM item's own geometry: its first coordinate child, a two-point POLYLINE
	per_num = OrderedDict()
	for role in BIDIRECTIONAL_VALUE_ROLES:
		idx, item = nums[role]
		children = _children(item)
		position = next((cidx for cidx, child in enumerate(children) if _is_measurement_geometry(child)), None)
		if position is None:
			return generic('%s NUM item without its own geometry' % role.replace('_', ' '))
		geometry, frame_uid = _parse_coordinates(children[position], diagnostics, strict, path)
		if geometry is None:
			return generic('%s NUM item without usable geometry' % role.replace('_', ' '))
		if geometry.graphic_type != DCMSR_GRAPHIC_POLYLINE or len(geometry.points) != 2:
			return generic('%s geometry %s of %d points is not a two-point POLYLINE' % (role.replace('_', ' '), geometry.graphic_type,
				len(geometry.points)))
		per_num[role] = {'idx': idx, 'item': item, 'children': children, 'consumed': {position}, 'geometry': geometry,
			'frame_uid': frame_uid, 'source': copy.deepcopy(children[position])}
	long_entry, short_entry = per_num[BIDIRECTIONAL_VALUE_LONG_AXIS], per_num[BIDIRECTIONAL_VALUE_SHORT_AXIS]
	long_geometry, short_geometry = long_entry['geometry'], short_entry['geometry']
	if long_geometry.domain != short_geometry.domain:
		return generic('the two axes are in different coordinate domains')
	if long_geometry.is_patient and long_entry['frame_uid'] != short_entry['frame_uid']:
		return generic('the two axes are in different Frames of Reference')
	if long_geometry.is_image and (long_geometry.reference is None or short_geometry.reference is None
			or (long_geometry.reference.sop_instance_uid, long_geometry.reference.frame_number)
			!= (short_geometry.reference.sop_instance_uid, short_geometry.reference.frame_number)):
		return generic('the two axes are on different images or frames')
	if [tuple(p) for p in long_geometry.points] == [tuple(p) for p in short_geometry.points]:
		return generic('the two NUM items share one geometry')
	geometry = long_geometry.with_points(long_geometry.points + short_geometry.points)
	frame_uid = long_entry['frame_uid']

	# The typed properties: the first NUM in document order carrying any, else the Long Axis NUM. They are written
	# under the Long Axis NUM; every other child stays under the NUM it was read from
	ordered = sorted(per_num.items(), key=lambda entry: entry[1]['idx'])
	source_role = next((role for role, entry in ordered
		if _carries_typed_properties([c for i, c in enumerate(entry['children']) if i not in entry['consumed']])), BIDIRECTIONAL_VALUE_LONG_AXIS)
	group_items = group_children if allow_group_level else []
	extras, properties = {}, None
	for role, entry in ordered:
		if role == source_role:
			properties = _parse_num_properties(entry['children'], entry['consumed'], group_items, consumed, diagnostics, strict, path,
				geometry, tracking_uid)
			extras[role] = properties[7]
		else:
			extras[role] = _preserved_children(entry['children'], entry['consumed'], diagnostics, strict, path)
	meta, display, views, source_series, plane, plane_text, finding_sites, _ = properties

	stored, names, stored_items, qualifiers = {}, {}, {}, {}
	for role, entry in per_num.items():
		_note_by_reference(entry['item'], diagnostics, path)
		value, unit, measured = _measured_value(entry['item'])
		stored[role] = (value, unit)
		names[role] = CodedConcept.from_dataset(entry['item'].ConceptNameCodeSequence[0], copy=True)
		stored_items[role] = copy.deepcopy(measured) if measured is not None and value is not None else None
		if entry['item'].get('NumericValueQualifierCodeSequence'):
			qualifiers[role] = copy.deepcopy(entry['item'].NumericValueQualifierCodeSequence)
		consumed.add(entry['idx'])

	group_sites = [CodedConcept.from_dataset(sibling.ConceptCodeSequence[0], copy=True) for idx, sibling in enumerate(group_children)
		if idx not in consumed and _is_finding_site(sibling) and sibling.get('ConceptCodeSequence')]

	long_value, long_unit = stored[BIDIRECTIONAL_VALUE_LONG_AXIS]
	short_value, short_unit = stored[BIDIRECTIONAL_VALUE_SHORT_AXIS]
	measurement = BidirectionalMeasurement(geometry=geometry, value=long_value, unit=long_unit if long_value is not None else None,
		name=names[BIDIRECTIONAL_VALUE_LONG_AXIS], width=short_value, width_unit=short_unit if short_value is not None else None,
		value_names=names, calibration=_calibration_for(calibrations, geometry.reference), tracking_id=tracking_id,
		tracking_uid=tracking_uid, frame_of_reference_uid=frame_uid, display=display, views=views, series_uid=source_series,
		extra_items=extras[BIDIRECTIONAL_VALUE_LONG_AXIS], short_axis_extra_items=extras[BIDIRECTIONAL_VALUE_SHORT_AXIS],
		group_finding_sites=group_sites, finding_sites=finding_sites or None, **meta)
	if plane_text is not None:
		measurement._set_parsed_drawing_plane(plane, plane_text)
	measurement._stored_items = stored_items
	measurement._stored_qualifiers = qualifiers
	measurement._num_coordinates = {role: entry['source'] for role, entry in per_num.items()}
	measurement._mark_parsed(stored_items.get(BIDIRECTIONAL_VALUE_LONG_AXIS), long_entry['source'])
	_note_bidirectional_value_checks(measurement, stored, diagnostics, strict, path)
	return measurement
