'''	Sonador-SR 0.3: quantitative measurements (Length, Angle, Cobb angle) exchanged with the Sonador
	Viewer, specified in oak-tree/medical-imaging/sonador-client#37.

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

	The identifier decides the type, the geometry never does: a group with an unknown identifier or
	an unusable geometry reads as a generic `Measurement` with every NUM item preserved. Children
	that are not modelled stay under their own item as `extra_items`.

	Contents: the concepts and accepted codes, `image_frame_geometry`, the content item encoders
	(`srencode_measurement_*`) and the parsers (`srparse_measurement`, `srparse_group_measurement`).
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
from highdicom.sr.value_types import ContentItem, NumContentItem, TextContentItem, UIDRefContentItem
from highdicom.sr.enum import RelationshipTypeValues, GraphicTypeValues, GraphicTypeValues3D

from ..base import SONADOR_MANUFACTURER, SONADOR_SR, SONADOR_SCHEME_VERSION_03, Measurement, MeasurementValue, Geometry, \
	GeometricMeasurement, ImageFrameGeometry, measurement_class_for_type, DCMSR_GEOMETRY_DOMAIN_IMAGE, DCMSR_GEOMETRY_DOMAIN_PATIENT, \
	DCMSR_UNITS_PIXEL, DCMSR_CONCEPT_LENGTH, DCMSR_CONCEPT_COBB_ANGLE, DCMHEADER_SR_REF_REFERENCE_FRAME, \
	SONADOR_MEASUREMENT_TYPE_LENGTH, SONADOR_MEASUREMENT_TYPE_ANGLE, SONADOR_MEASUREMENT_TYPE_COBB_ANGLE, \
	SONADOR_MEASUREMENT_TYPE_GENERIC, DCMHEADER_CODING_SCHEME_DESIGNATOR, DCMHEADER_CODE_MEANING, drawing_plane_vectors
from . import dcm_code_value, srcontent_is_named, srcontent_extra_items, srcontent_from_dataset, sr_diagnostic, \
	SR_DIAGNOSTIC_INFO, SR_DIAGNOSTIC_WARNING, SR_DIAGNOSTIC_ERROR, SR_CONDITION_MEASUREMENT_VALUE_MISMATCH, \
	SR_CONDITION_MEASUREMENT_VALUE_UNVERIFIED, SR_CONDITION_MEASUREMENT_UNCALIBRATED, SR_CONDITION_MEASUREMENT_WITHOUT_VALUE, \
	SR_CONDITION_MEASUREMENT_READ_AS_GENERIC, SR_CONDITION_FOREIGN_MEASUREMENT_LABEL, SR_CONDITION_DEGENERATE_GEOMETRY, \
	SR_CONDITION_INVALID_GEOMETRY, SR_CONDITION_SCOORD3D_WITHOUT_FRAME, SR_CONDITION_ANGLE_MIDDLE_POINTS_DIFFER
from .seriestag import srparse_item_properties, srencode_meta_items, srencode_display_items, srcontent_is_valid, \
	image_reference_from_dataset, measurement_type_for_tracking_id, DCMSR_LAYOUT_NESTED, DCMSR_LAYOUTS_WRITTEN, \
	_children, _value_type, _text_value, _relationship, _path, _note_by_reference, _note_missing_relationships, _UNNAMED_MARKER, DCMSR_IMAGE_REFERENCE_NAME
from . import srcontent_name, SR_CONDITION_UNREGISTERED_VIEW, SR_CONDITION_INVALID_DRAWING_PLANE, SR_CONDITION_PROVENANCE_ON_IMAGE_DOMAIN

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
DCMSR_CORNERSTONE_FREE_TEXT = 'CORNERSTONEFREETEXT'

# Allowed difference between a stored value and the value computed from the points
DCMSR_VALUE_TOLERANCE = 0.01

DCMSR_GRAPHIC_TYPES = {'SCOORD': GraphicTypeValues, 'SCOORD3D': GraphicTypeValues3D}


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
	for value in width + height:
		text = ('%.*f' % (DCMSR_DRAWING_PLANE_DECIMALS, value)).rstrip('0').rstrip('.')
		numbers.append('0' if text in ('-0', '') else text)
	return ','.join(numbers)


def drawing_plane_from_text(text):
	'''	Drawing plane of a Drawing-Plane text: six decimal numbers separated by commas, with any spaces
		around them trimmed. Only the form is checked here (`drawing_plane_failed_check` checks the rest).

		@input text (str): the text read
		@returns
			- tuple (width, height) of two 3-tuples of float
			- None when the text is not six finite decimal numbers
	'''
	parts = [part.strip() for part in str(text or '').split(',')]
	if len(parts) != 6 or not all(DCMSR_DRAWING_PLANE_NUMBER_RE.match(part) for part in parts):
		return None
	try:
		return drawing_plane_vectors([float(part) for part in parts])
	except ValueError:
		return None


def drawing_plane_failed_check(plane, points):
	'''	The first validation check a drawing plane fails for the points of a measurement: each
		direction of unit length within 1e-4, the two directions perpendicular within 1e-4 (their dot
		product), every point within 1 mm of the plane through the first point. Makes no request.

		@input plane (pair of 3-vectors, or None): the drawing plane
		@input points (sequence of 3-sequences): patient-domain points of the measurement

		@returns
			- None when the plane is valid
			- str naming the failed check: `six-finite-numbers`, `unit-width`, `unit-height`,
				`perpendicular` or `points-within-1mm`
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
	normal = np.cross(width, height)
	normal = normal / np.linalg.norm(normal)
	if points:
		origin = np.asarray(points[0], dtype=float)
		for point in points[1:]:
			if abs(float(np.dot(np.asarray(point, dtype=float) - origin, normal))) > DCMSR_DRAWING_PLANE_DISTANCE_TOLERANCE:
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
	'''	Point the IMAGE child of a coordinate item at another SOP instance and frame, adding the child
		when the item has none. Other children are left alone. Modifies the item in place.

		@input item (pydicom.Dataset): the SCOORD item
		@input reference (ImageReference): the new reference

		@raises ValueError for a reference without a SOP Instance UID
	'''
	if reference is None or not reference.sop_instance_uid:
		raise ValueError('An image-domain measurement needs an image reference with a SOP Instance UID to be written')
	images = [child for child in item.get('ContentSequence') or [] if child.get('ValueType') == 'IMAGE']
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
		failed = drawing_plane_failed_check(plane, geometry.points)
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
	if getattr(stored_unit, 'value', None) == DCMSR_UNITS_PIXEL.value:
		sr_diagnostic(diagnostics, SR_DIAGNOSTIC_INFO, SR_CONDITION_MEASUREMENT_UNCALIBRATED,
			'value stored in pixels: the measurement is uncalibrated', path=path, value=stored)

	computed = measurement.computed_value()
	if computed is None:
		sr_diagnostic(diagnostics, SR_DIAGNOSTIC_INFO, SR_CONDITION_MEASUREMENT_VALUE_UNVERIFIED,
			'no usable calibration for the image frame; the stored value is kept and not verified', path=path, value=stored)
		return
	value, unit = computed
	if getattr(unit, 'value', None) != getattr(stored_unit, 'value', None):
		sr_diagnostic(diagnostics, SR_DIAGNOSTIC_INFO, SR_CONDITION_MEASUREMENT_VALUE_MISMATCH,
			'stored unit %s differs from computed unit %s; the stored value is kept' % (getattr(stored_unit, 'value', None), getattr(unit, 'value', None)),
			path=path, value=stored)
	elif abs(value - stored) > DCMSR_VALUE_TOLERANCE:
		sr_diagnostic(diagnostics, SR_DIAGNOSTIC_INFO, SR_CONDITION_MEASUREMENT_VALUE_MISMATCH,
			'stored value %s differs from computed value %s by more than %s; the stored value is kept' % (stored, value, DCMSR_VALUE_TOLERANCE),
			path=path, value=stored)


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
		if _value_type(child) in DCMSR_GRAPHIC_TYPES:
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

	# Properties of the NUM item, then the legacy group level
	meta, display = srparse_item_properties(children, group_items, consumed, group_consumed, diagnostics, strict, path, {}, owner='NUM item')

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
				'%s under a NUM item without patient-domain geometry; ignored and preserved' % dcm_code_value(srcontent_name(child)), path=path)
		elif is_series and source_series is None:
			consumed.add(idx)
			source_series = str(child.get('UID') or '').strip() or None
		elif is_plane and plane_text is None:
			consumed.add(idx)
			plane_text = _text_value(child) or ''
			plane = drawing_plane_from_text(plane_text)
			failed = 'six-finite-numbers' if plane is None else drawing_plane_failed_check(plane, geometry.points)
			if failed is not None:
				plane = None
				sr_diagnostic(diagnostics, SR_DIAGNOSTIC_WARNING, SR_CONDITION_INVALID_DRAWING_PLANE,
					'Drawing-Plane "%s" fails the check "%s"; read as absent and written back as read' % (plane_text, failed),
					path=path, value=tracking_uid, strict=strict)

	# A dcmjs label becomes the description; it is written back only in that form
	if meta.get('description') is None:
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
		if idx not in consumed and _is_finding_site(child) and _relationship(child) == RelationshipTypeValues.HAS_CONCEPT_MOD.value:
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

	# The other NUM items of the group; a repeated concept stays in the group's preserved items
	values, group_sites = OrderedDict(), []
	own_key = _concept_key(item.ConceptNameCodeSequence[0])
	for idx, sibling in enumerate(group_children):
		if sibling is item:
			group_consumed.add(idx)
		elif idx not in group_consumed and _value_type(sibling) == 'NUM' and sibling.get('ConceptNameCodeSequence'):
			key = _concept_key(sibling.ConceptNameCodeSequence[0])
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
		item, the other ones becoming its `values`. Used by the group router.

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
	numbers = [child for idx, child in enumerate(_children(group))
		if idx not in consumed and _value_type(child) == 'NUM' and child.get('ConceptNameCodeSequence')]
	if not numbers:
		return None
	accepted = DCMSR_MEASUREMENT_CONCEPTS.get(measurement_type, ())
	primary = next((n for n in numbers if any(srcontent_is_named(n, code) for code in accepted)), numbers[0])
	return srparse_measurement(primary, group=group, strict=strict, diagnostics=diagnostics, tracking_id=tracking_id, tracking_uid=tracking_uid,
		group_consumed=consumed, allow_group_level=allow_group_level, calibrations=calibrations, repaired=True)
