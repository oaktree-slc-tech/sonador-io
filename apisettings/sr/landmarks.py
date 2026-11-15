'''	Sonador-SR 0.3: landmark sets, named groups of points with attributes of their own, specified in
	oak-tree/medical-imaging/sonador-client#47.

	A planning application keeps landmarks: points on bone and on devices that its plan is built from. A landmark
	set writes them into the structured report of the imaging series, where they can be read back, edited, re-saved
	and drawn, and where each point keeps a stable identity.

	A set is one TID 1501 measurement group in the "Imaging Measurements" container, with the tracking identifier
	`cornerstoneTools@^4.0.0:SonadorLandmarkSet` and the set's tracking UID:

	* one Finding CODE item `(121071, DCM, "Finding")`, TID 1501 row 3b: the set's name, and `HAS PROPERTIES` its
		description, its label (beside a concept), its Source-Series and its display attributes;
	* one CODE item `(Sonador-Landmark, Sonador-SR, "Landmark")` per landmark, row 11, in the order of the set (the
		order is a Sonador guarantee: TID 1501 gives sibling order no meaning): the landmark's name, `INFERRED FROM`
		its one `POINT` coordinate (SCOORD3D in a frame, or SCOORD `SELECTED FROM` one image frame), whose concept
		name is the landmark type and whose Fiducial UID is the landmark's identity, and `HAS PROPERTIES` its
		description, label and display attributes.

	The value of a set or landmark item is its concept; a label beside a concept is a `Sonador-Measurement.Text`
	child; a label without a concept is the meaning of the free-text code `(CORNERSTONEFREETEXT, CST4)`, a Cornerstone
	exchange convention rather than standard Code Meaning usage, limited by DICOM to 64 characters (a longer one is
	written verbatim, with `overlength-code-meaning`); a landmark with neither has the value `Sonador-Landmark.Point`.
	The free-text code and, for a landmark, the Point code are reserved and refused as concepts. A label is stored
	exactly as given or refused where it is written (`landmark-label-unrepresentable`); see
	`sonador.apisettings.base.landmark_label_problem`.

	A set read from a document and not changed is written back exactly as read, whatever layout or form it was
	read in. An edited set is written in the form above: unmodelled content stays where it was (an opaque child of
	the group after the sibling it followed, opaque children of the Finding and of each landmark under their item,
	the unread attributes of a coordinate item in the retained item, patched with every modelled field that
	changed). A by-reference item refuses the write.

	Legacy `Sonador-SR.1002` point groups under `Clinical-Report.Volume-Measurements` read as legacy sets
	(`srdoc_legacy_landmark_sets`). They are written back unchanged until edited; an edit migrates the one group to
	the form above when nothing would be lost (a POINT item is retained and patched); otherwise the set is read-only
	and an edit raises `landmark-legacy-not-migratable` when it is attempted.

	Image-domain landmarks are checked against what is known of their image (`ImageMetadata`, from cached instances or
	the caller's `images`): a multiframe image needs the one frame number, a tiled image needs a frame-relative pixel
	origin, and a point lies within the frame's columns and rows.

	The `Sonador-SR` scheme designator has no `99` prefix, a convention of this client since 0.2: the landmark
	concepts are not private codes in the sense of PS3.16 chapter 8.

	Contents: the reader (`srparse_landmark_set`, `srdoc_legacy_landmark_sets`), the writer
	(`srencode_landmark_set`) and the checks across the sets of a report (`check_landmark_identities`). The model is
	`sonador.apisettings.base.LandmarkSet`.
'''
import copy, math
from collections import OrderedDict, namedtuple

import numpy as np

from pydicom import Dataset as DcmDataset
from pydicom.sr.codedict import codes as dcmcodes
from pydicom.uid import generate_uid

from highdicom.sr.content import SourceImageForRegion
from highdicom.sr.enum import RelationshipTypeValues
from highdicom.sr.templates import CodedConcept, MeasurementsAndQualitativeEvaluations, TrackingIdentifier
from highdicom.sr.value_types import CodeContentItem, ContentSequence, TextContentItem, UIDRefContentItem, ScoordContentItem, \
	Scoord3DContentItem

from ..base import Landmark, LandmarkSet, DCMSR_FINDING, DCMSR_LANDMARK, DCMSR_LANDMARK_POINT, DCMSR_LANDMARK_ANATOMIC, \
	DCMSR_LANDMARK_REGISTRATION, DCMSR_LANDMARK_TYPES, DCMSR_LANDMARK_POINT_KEY, DCMSR_FREE_TEXT_KEY, ImageReference, \
	code_key, free_text_finding, LANDMARK_LABEL_TEXT, LANDMARK_LABEL_CODE_MEANING, landmark_label_problem, SONADOR_SR, \
	SONADOR_MEASUREMENT_TYPE_LANDMARK_SET
from . import dcm_code_value, srcontent_is_named, srcontent_extra_items, srcontent_from_dataset, sr_diagnostic, SRDiagnostic, \
	SRDiagnosticError, DCM_CODE_MEANING_MAX_LENGTH, DCMSR_SCOORD3D, DCMSR_SCOORD3D_ANATOMIC, DCMSR_SCOORD3D_LANDMARK, \
	DCMSR_SCOORD3D_REGISTRATION, DCMSR_REPORT_VOLUME_MEAUREMENTS, SR_DIAGNOSTIC_INFO, SR_DIAGNOSTIC_WARNING, SR_DIAGNOSTIC_ERROR, \
	SR_CONDITION_OVERLENGTH_CODE_MEANING, SR_CONDITION_LANDMARK_LABEL_UNREPRESENTABLE, SR_CONDITION_LANDMARK_LABEL_DUPLICATE, \
	SR_CONDITION_LANDMARK_LABEL_DUPLICATE_DROPPED, SR_CONDITION_LANDMARK_TYPE_ABSENT, SR_CONDITION_LANDMARK_TYPE_UNKNOWN, \
	SR_CONDITION_LANDMARK_PIXEL_ORIGIN_UNSUPPORTED, SR_CONDITION_LANDMARK_SET_EMPTY, SR_CONDITION_LANDMARK_SET_READ_AS_GROUP, \
	SR_CONDITION_LANDMARK_SET_WITHOUT_NAME, SR_CONDITION_LANDMARK_WITHOUT_POINT, SR_CONDITION_LANDMARK_POINT_AMBIGUOUS, \
	SR_CONDITION_LANDMARK_POINT_INVALID, SR_CONDITION_LANDMARK_IMAGE_REFERENCE_MISSING, SR_CONDITION_LANDMARK_IMAGE_REFERENCE_AMBIGUOUS, \
	SR_CONDITION_LANDMARK_POINT_OUT_OF_BOUNDS, SR_CONDITION_LANDMARK_SET_MIXED_FRAMES, SR_CONDITION_DUPLICATE_FIDUCIAL_UID, \
	SR_CONDITION_LANDMARK_IDENTITY_PROVISIONAL, SR_CONDITION_LANDMARK_LEGACY_COLLECTION, SR_CONDITION_LANDMARK_LEGACY_MIGRATED, \
	SR_CONDITION_LANDMARK_LEGACY_NOT_MIGRATABLE, SR_CONDITION_LANDMARK_SET_INVALID
from .seriestag import image_reference_from_dataset, srparse_item_properties, srparse_finding_code, srencode_display_items, \
	DCMSR_MEASUREMENT_DESCRIPTION, DCMSR_MEASUREMENT_TEXT, DCMSR_MEASUREMENT_LOCATION, _children, _value_type, _relationship, \
	_text_value, _path
from .measurements import DCMSR_MEASUREMENT_SOURCE_SERIES, _mark_unnamed


# Sonador-SR 0.2 coordinate concepts of a legacy point group, and the 0.3 landmark type each reads as
DCMSR_LEGACY_LANDMARK_TYPES = OrderedDict((
	(code_key(DCMSR_SCOORD3D), DCMSR_LANDMARK_POINT),
	(code_key(DCMSR_SCOORD3D_LANDMARK), DCMSR_LANDMARK_POINT),
	(code_key(DCMSR_SCOORD3D_ANATOMIC), DCMSR_LANDMARK_ANATOMIC),
	(code_key(DCMSR_SCOORD3D_REGISTRATION), DCMSR_LANDMARK_REGISTRATION),
))
DCMSR_LEGACY_POINT_GROUP_TEMPLATE = 'Sonador-SR.1002'
DCMSR_KNOWN_LANDMARK_TYPES = set(code_key(code) for code in DCMSR_LANDMARK_TYPES)

# Pixel Origin Interpretation of a SCOORD: frame-relative coordinates only
DCMSR_PIXEL_ORIGIN_FRAME = 'FRAME'
DCMSR_PIXEL_ORIGIN_VOLUME = 'VOLUME'


class _Unreadable(Exception):
	'''	A landmark item that cannot be read: the condition and its message.
	'''
	def __init__(self, condition, message, geometry=True):
		super().__init__(message)
		self.condition = condition
		self.geometry = geometry


def _named(item, code):
	return srcontent_is_named(item, code)


def _code_dataset(item, attr='ConceptCodeSequence'):
	sequence = item.get(attr) or []
	return sequence[0] if len(sequence) else None


def _concept_of(dataset):
	'''	CodedConcept of a code dataset, carried verbatim.
	'''
	return CodedConcept.from_dataset(dataset, copy=True)


def _by_reference(item):
	return 'ReferencedContentItemIdentifier' in item


# ---- reading ----


def _read_name(item, diagnostics, strict, path, landmark):
	'''	Concept and label of a set's Finding or of a landmark item, by the read rule: the free-text code gives the
		label (a Text child beside it is kept opaque, `landmark-label-duplicate`); the Point code in a landmark gives no
		concept (a Text child beside it is the label); any other code is the concept (a Text child is the label).

		@input item (pydicom.Dataset): the CODE item
		@input landmark (bool): a landmark item (the Point code is reserved there)
		@returns tuple (concept or None, label or None, index of the Text child consumed or None, index of a duplicate
			Text child or None)
	'''
	children = _children(item)
	_, value = srparse_finding_code(item, diagnostics, strict, path)
	key = code_key(value)
	text_idx = next((idx for idx, child in enumerate(children) if _named(child, DCMSR_MEASUREMENT_TEXT) and _value_type(child) == 'TEXT'), None)
	text = _text_value(children[text_idx]) if text_idx is not None else None
	if key == DCMSR_FREE_TEXT_KEY:
		label = str(value.meaning or '')
		if text_idx is not None:
			sr_diagnostic(diagnostics, SR_DIAGNOSTIC_INFO, SR_CONDITION_LANDMARK_LABEL_DUPLICATE, 'free-text value beside a Text item; '
				'the value is the label and the Text item is kept as opaque content', path=path, value=text)
		return None, label or None, None, text_idx
	if landmark and key == DCMSR_LANDMARK_POINT_KEY:
		return None, text or None, text_idx, None
	return value, text or None, text_idx, None


def _note_label(concept, label, diagnostics, path):
	if label is None:
		return
	problem = landmark_label_problem(label, LANDMARK_LABEL_TEXT if concept is not None else LANDMARK_LABEL_CODE_MEANING)
	if problem:
		sr_diagnostic(diagnostics, SR_DIAGNOSTIC_INFO, SR_CONDITION_LANDMARK_LABEL_UNREPRESENTABLE, 'label read as stored (%s); '
			'written back as read while unchanged' % problem, path=path, value=label)


def _properties(children, consumed, group_children, group_consumed, diagnostics, strict, path, owner, keep_text=False):
	'''	Description and display attributes of an item, in the nested, flat and legacy layouts. Only the occurrences the
		model holds are consumed: a Location item, and a Text item the caller does not use as the label, stay with the
		item's preserved content in their place.

		@input keep_text (bool, default=False): whether the caller uses a `Sonador-Measurement.Text` found here as the label
		@returns tuple (dict of `description` and `text`, DisplayAttributes or None)
	'''
	unmodelled = (DCMSR_MEASUREMENT_LOCATION,) if keep_text else (DCMSR_MEASUREMENT_LOCATION, DCMSR_MEASUREMENT_TEXT)
	before, group_before = set(consumed), set(group_consumed)
	values, display = srparse_item_properties(children, group_children, consumed, group_consumed, diagnostics, strict, path, {}, owner=owner)
	for items, taken, previous in ((children, consumed, before), (group_children, group_consumed, group_before)):
		for idx in list(taken - previous):
			if any(_named(items[idx], concept) for concept in unmodelled):
				taken.discard(idx)
	if not keep_text:
		values.pop('text', None)
	return values, display


def _coordinate(children, diagnostics, strict, path, images=None):
	'''	The one coordinate of a landmark item, read: its point, domain, frame or image, type and identity.

		@returns dict, with `index` (of the child), `point`, `frame_uid` or `reference`, `type`, `type_absent`, `fiducial_uid`
		@raises _Unreadable with the condition of the failure
	'''
	coordinates = [(idx, child) for idx, child in enumerate(children) if _value_type(child) in ('SCOORD', 'SCOORD3D')
		and _relationship(child) in (None, 'INFERRED FROM')]
	if any(_by_reference(child) and _relationship(child) == 'INFERRED FROM' for child in children) and not coordinates:
		raise _Unreadable(SR_CONDITION_LANDMARK_WITHOUT_POINT, 'landmark whose coordinate is a by-reference item')
	if not coordinates:
		raise _Unreadable(SR_CONDITION_LANDMARK_WITHOUT_POINT, 'landmark without a coordinate child', geometry=False)
	if len(coordinates) > 1:
		raise _Unreadable(SR_CONDITION_LANDMARK_POINT_AMBIGUOUS, 'landmark with %d coordinate children' % len(coordinates))
	idx, item = coordinates[0]
	value_type = _value_type(item)
	if str(item.get('GraphicType') or '') != 'POINT':
		raise _Unreadable(SR_CONDITION_LANDMARK_WITHOUT_POINT, 'landmark coordinate of graphic type %s, not POINT' % item.get('GraphicType'))
	try:
		data = [float(v) for v in (item.get('GraphicData') or [])]
	except (TypeError, ValueError):
		data = []
	expected = 3 if value_type == 'SCOORD3D' else 2
	if len(data) != expected or not all(math.isfinite(v) for v in data):
		raise _Unreadable(SR_CONDITION_LANDMARK_POINT_INVALID, '%s GraphicData of %d values; a landmark point has %d finite values'
			% (value_type, len(data), expected))
	found = {'index': idx, 'point': tuple(data), 'frame_uid': None, 'reference': None}
	if value_type == 'SCOORD3D':
		frame_uid = str(item.get('ReferencedFrameOfReferenceUID') or '') or None
		if not frame_uid:
			raise _Unreadable(SR_CONDITION_LANDMARK_POINT_INVALID, 'SCOORD3D without a Referenced Frame of Reference UID')
		found['frame_uid'] = frame_uid
	else:
		origin = str(item.get('PixelOriginInterpretation') or '') or None
		if origin == DCMSR_PIXEL_ORIGIN_VOLUME:
			raise _Unreadable(SR_CONDITION_LANDMARK_PIXEL_ORIGIN_UNSUPPORTED, 'SCOORD in the Total Pixel Matrix (VOLUME); only '
				'frame-relative coordinates are supported')
		image_items = [child for child in _children(item) if _value_type(child) == 'IMAGE' and _relationship(child) in (None, 'SELECTED FROM')]
		if not image_items:
			raise _Unreadable(SR_CONDITION_LANDMARK_IMAGE_REFERENCE_MISSING, 'SCOORD without a SELECTED FROM image')
		if len(image_items) > 1:
			raise _Unreadable(SR_CONDITION_LANDMARK_IMAGE_REFERENCE_AMBIGUOUS, 'SCOORD with %d SELECTED FROM images' % len(image_items))
		sop = _code_dataset(image_items[0], 'ReferencedSOPSequence')
		frames = sop.get('ReferencedFrameNumber') if sop is not None else None
		if frames is not None and not isinstance(frames, (int, str)) and len(frames) > 1:
			raise _Unreadable(SR_CONDITION_LANDMARK_IMAGE_REFERENCE_AMBIGUOUS, 'SCOORD image with %d frame numbers' % len(frames))
		empty = frames is None or frames == '' or (hasattr(frames, '__len__') and not isinstance(frames, str) and len(frames) == 0)
		if not empty and not _positive_frame(frames):
			raise _Unreadable(SR_CONDITION_LANDMARK_IMAGE_REFERENCE_AMBIGUOUS, 'Referenced Frame Number %r is not a positive integer'
				% (frames,))
		reference = image_reference_from_dataset(image_items[0])
		if not reference.sop_instance_uid:
			raise _Unreadable(SR_CONDITION_LANDMARK_IMAGE_REFERENCE_MISSING, 'SCOORD image without a SOP Instance UID')
		problem = _image_problem(reference, image_metadata(images, reference), origin)
		if problem is not None:
			raise _Unreadable(*problem)
		found['reference'] = reference
	found['name'] = _code_dataset(item, 'ConceptNameCodeSequence')
	fiducial = str(item.get('FiducialUID') or '') or None
	found['fiducial_uid'] = fiducial
	return found


def _landmark_type(name, diagnostics, path, legacy=False):
	'''	Landmark type of a coordinate item's concept name.

		@returns tuple (code, bool absent)
	'''
	if name is None:
		sr_diagnostic(diagnostics, SR_DIAGNOSTIC_INFO, SR_CONDITION_LANDMARK_TYPE_ABSENT, 'coordinate without a concept name; read as '
			'a Point landmark, written back as read while unchanged', path=path)
		return DCMSR_LANDMARK_POINT, True
	key = code_key(name)
	if legacy and key in DCMSR_LEGACY_LANDMARK_TYPES:
		return DCMSR_LEGACY_LANDMARK_TYPES[key], False
	if key in DCMSR_KNOWN_LANDMARK_TYPES:
		return next(code for code in DCMSR_LANDMARK_TYPES if code_key(code) == key), False
	concept = _concept_of(name)
	sr_diagnostic(diagnostics, SR_DIAGNOSTIC_INFO, SR_CONDITION_LANDMARK_TYPE_UNKNOWN, 'landmark type %s/%s is not a type this client '
		'knows; kept as given' % (concept.scheme_designator, concept.value), path=path, value=concept.value)
	return concept, False


# ---- image metadata ----

class ImageMetadata(namedtuple('ImageMetadata', ('columns', 'rows', 'frames', 'tiled'))):
	'''	What the client knows of a referenced image, from what is cached or what the caller supplies: the size of one
		frame, the number of frames and whether the image is tiled. Each field is None when unknown.

		@property columns (int or None), rows (int or None): size of one frame, in pixels (a point is (column, row))
		@property frames (int or None): Number of Frames
		@property tiled (bool or None): a tiled image (Whole Slide Microscopy), whose coordinates may be in the Total
			Pixel Matrix
	'''
	__slots__ = ()

	def __new__(cls, columns=None, rows=None, frames=None, tiled=None):
		return super().__new__(cls, columns, rows, frames, tiled)


# SOP classes whose instances hold several frames, and the tiled one; used when the frame count is not cached
DCMSR_TILED_SOP_CLASSES = frozenset(('1.2.840.10008.5.1.4.1.1.77.1.6',))
DCMSR_MULTIFRAME_SOP_CLASSES = frozenset((
	'1.2.840.10008.5.1.4.1.1.2.1', '1.2.840.10008.5.1.4.1.1.2.2',				# Enhanced CT, Legacy Converted Enhanced CT
	'1.2.840.10008.5.1.4.1.1.4.1', '1.2.840.10008.5.1.4.1.1.4.3', '1.2.840.10008.5.1.4.1.1.4.4',	# Enhanced MR, MR Color, Legacy Converted
	'1.2.840.10008.5.1.4.1.1.130', '1.2.840.10008.5.1.4.1.1.128.1',			# Enhanced PET, Legacy Converted Enhanced PET
	'1.2.840.10008.5.1.4.1.1.3.1', '1.2.840.10008.5.1.4.1.1.6.2',				# US Multi-frame, Enhanced US Volume
	'1.2.840.10008.5.1.4.1.1.7.1', '1.2.840.10008.5.1.4.1.1.7.2', '1.2.840.10008.5.1.4.1.1.7.3', '1.2.840.10008.5.1.4.1.1.7.4',
	'1.2.840.10008.5.1.4.1.1.12.1', '1.2.840.10008.5.1.4.1.1.12.1.1',			# XA, Enhanced XA
	'1.2.840.10008.5.1.4.1.1.12.2', '1.2.840.10008.5.1.4.1.1.12.2.1',			# XRF, Enhanced XRF
	'1.2.840.10008.5.1.4.1.1.13.1.3', '1.2.840.10008.5.1.4.1.1.20',			# Breast Tomosynthesis, NM
	'1.2.840.10008.5.1.4.1.1.77.1.5.4', '1.2.840.10008.5.1.4.1.1.77.1.6',		# Ophthalmic Tomography, Whole Slide Microscopy
))


def image_metadata(images, reference):
	'''	What is known of the image a reference names: the entry of `images` for its instance, completed by its SOP class
		(a multiframe or tiled class) where the entry does not say.

		@input images (mapping or None): `ImageMetadata`, or a `(columns, rows)` tuple, by SOP Instance UID
		@input reference (ImageReference)
		@returns ImageMetadata
	'''
	entry = (images or {}).get(reference.sop_instance_uid) if reference is not None else None
	if entry is not None and not isinstance(entry, ImageMetadata):
		columns, rows = entry
		entry = ImageMetadata(columns=columns, rows=rows)
	entry = entry or ImageMetadata()
	sop_class = getattr(reference, 'sop_class_uid', None)
	tiled = entry.tiled if entry.tiled is not None else (sop_class in DCMSR_TILED_SOP_CLASSES if sop_class else None)
	return entry._replace(tiled=tiled)


def image_is_multiframe(meta, reference):
	'''	Whether a referenced image holds several frames: its cached frame count, else its SOP class.

		@returns bool
	'''
	if meta.frames is not None:
		return int(meta.frames) > 1
	return getattr(reference, 'sop_class_uid', None) in DCMSR_MULTIFRAME_SOP_CLASSES


def _image_problem(reference, meta, origin):
	'''	Why a SCOORD on a referenced image cannot be read as a frame-relative landmark, or None.

		@returns tuple (condition, message) or None
	'''
	if meta.tiled and origin is None:
		return (SR_CONDITION_LANDMARK_PIXEL_ORIGIN_UNSUPPORTED, 'SCOORD on a tiled image without Pixel Origin Interpretation; its '
			'coordinates may be in the Total Pixel Matrix, which is not supported')
	if image_is_multiframe(meta, reference) and not reference.frame_number:
		return (SR_CONDITION_LANDMARK_IMAGE_REFERENCE_AMBIGUOUS, 'SCOORD on a multiframe image without a Referenced Frame Number')
	if meta.frames is not None and reference.frame_number and int(reference.frame_number) > int(meta.frames):
		return (SR_CONDITION_LANDMARK_IMAGE_REFERENCE_AMBIGUOUS, 'Referenced Frame Number %s of an image of %s frames'
			% (reference.frame_number, meta.frames))
	return None


def _positive_frame(value):
	'''	Whether a Referenced Frame Number value (one value, as read or as given) is a positive integer; frames are
		numbered from 1 (PS3.3 C.18.4).
	'''
	if hasattr(value, '__len__') and not isinstance(value, str):
		if len(value) != 1:
			return False
		value = value[0]
	try:
		number = int(value)
	except (TypeError, ValueError):
		return False
	return number >= 1 and float(value) == number


def _out_of_bounds(point, meta):
	if meta.columns is None or meta.rows is None:
		return False
	column, row = point
	return not (0 <= column <= int(meta.columns) and 0 <= row <= int(meta.rows))


def _read_landmark(item, diagnostics, strict, path, images=None):
	'''	One landmark item, read.

		@returns Landmark (with `_parsed`), and the coordinate it was read from (dict)
		@raises _Unreadable
	'''
	children = _children(item)
	if not item.get('ConceptCodeSequence'):
		raise _Unreadable(SR_CONDITION_LANDMARK_SET_INVALID, 'landmark item without a value', geometry=False)
	coordinate = _coordinate(children, diagnostics, strict, path, images=images)
	consumed = {coordinate['index']}
	concept, label, text_idx, duplicate_idx = _read_name(item, diagnostics, strict, path, landmark=True)
	if text_idx is not None:
		consumed.add(text_idx)
	if duplicate_idx is not None:
		consumed.add(duplicate_idx)
	_note_label(concept, label, diagnostics, path)
	values, display = _properties(children, consumed, [], set(), diagnostics, strict, path, 'landmark')
	description = values.get('description')
	landmark_type, absent = _landmark_type(coordinate['name'], diagnostics, path)
	provisional = coordinate['fiducial_uid'] is None
	if provisional:
		sr_diagnostic(diagnostics, SR_DIAGNOSTIC_INFO, SR_CONDITION_LANDMARK_IDENTITY_PROVISIONAL, 'coordinate without a Fiducial UID; '
			'a provisional identity is written on the first edited save', path=path)
	extra = [copy.deepcopy(child) for idx, child in enumerate(children) if idx not in consumed]
	landmark = Landmark.__new__(Landmark)
	landmark._point = None
	landmark.point = coordinate['point']
	landmark._concept, landmark._label = concept, label
	landmark.landmark_type = landmark_type
	landmark.description = description
	landmark.display = display
	landmark.fiducial_uid = coordinate['fiducial_uid'] or generate_uid()
	landmark.extra_items = tuple(extra)
	landmark._parsed = {'raw': copy.deepcopy(item), 'coordinate': copy.deepcopy(children[coordinate['index']]),
		'coordinate_index': coordinate['index'], 'type_absent': absent, 'provisional': provisional,
		'duplicate_text': copy.deepcopy(children[duplicate_idx]) if duplicate_idx is not None else None}
	landmark._parsed['state'] = landmark._state()
	if coordinate['reference'] is not None:
		meta = image_metadata(images, coordinate['reference'])
		if _out_of_bounds(landmark.point, meta):
			column, row = landmark.point
			sr_diagnostic(diagnostics, SR_DIAGNOSTIC_WARNING, SR_CONDITION_LANDMARK_POINT_OUT_OF_BOUNDS, 'point (%s, %s) lies outside '
				'the image of %d columns and %d rows; kept, refused on an edited save' % (column, row, meta.columns, meta.rows),
				path=path, value=landmark.fiducial_uid, strict=strict)
	return landmark, coordinate


def _place(coordinate):
	'''	Frame or image a landmark is on: what all landmarks of one set share.
	'''
	if coordinate['frame_uid'] is not None:
		return ('patient', coordinate['frame_uid'])
	reference = coordinate['reference']
	return ('image', reference.sop_instance_uid, reference.frame_number)


def srparse_landmark_set(group, tracking_id, tracking_uid, consumed, strict=False, diagnostics=None, images=None):
	'''	Landmark set of a measurement group routed by its tracking identifier.

		| Group | Returned | Diagnostics |
		|---|---|---|
		| no landmark item | None (the group is kept raw) | `landmark-set-empty`, `landmark-set-read-as-group` (warnings) |
		| landmark items, none readable | None | one warning per item, then `landmark-set-read-as-group` |
		| some readable | a LandmarkSet of the readable ones; the others are opaque slots | one warning per unreadable item |
		| no concept and no label | a LandmarkSet with name `''` | `landmark-set-without-name` (warning) |

		Landmarks on another frame or image than the first readable one are opaque slots, with one
		`landmark-set-mixed-frames` warning. The set's properties are read from its Finding in the nested and flat layouts,
		and from the group level in the legacy layout. Every child of the group that is not modelled is an opaque slot,
		anchored after the sibling it followed. `strict` raises on the first warning. Makes no request.

		@input group (pydicom.Dataset): the measurement group
		@input tracking_id, tracking_uid (str or None)
		@input consumed (set): indices of the group's children already read (the tracking pair)
		@input strict (bool, default=False), diagnostics (list, default=None)
		@input images (mapping, default=None): `ImageMetadata` (or a `(columns, rows)` tuple) by SOP Instance UID, from
			what is cached: checks image-domain points against the frame count, tiling and size of their image

		@returns LandmarkSet, or None for a group read as a plain group
	'''
	path = _path(tracking_uid)
	children = _children(group)
	consumed = set(consumed)
	pair = set(consumed)
	landmark_indices = [idx for idx, child in enumerate(children) if idx not in consumed and _value_type(child) == 'CODE'
		and _named(child, DCMSR_LANDMARK)]
	if not landmark_indices:
		sr_diagnostic(diagnostics, SR_DIAGNOSTIC_WARNING, SR_CONDITION_LANDMARK_SET_EMPTY, 'landmark-set group without a landmark item',
			path=path, value=tracking_uid, strict=strict)
		sr_diagnostic(diagnostics, SR_DIAGNOSTIC_WARNING, SR_CONDITION_LANDMARK_SET_READ_AS_GROUP, 'group kept as read, not listed as a '
			'landmark set', path=path, value=tracking_uid, strict=strict)
		return None

	landmarks, unreadable, place, mixed = OrderedDict(), OrderedDict(), None, False
	opaque_geometry = False
	for idx in landmark_indices:
		item_path = '%s, landmark %d' % (path, landmark_indices.index(idx) + 1)
		try:
			landmark, coordinate = _read_landmark(children[idx], diagnostics, strict, item_path, images=images)
		except _Unreadable as failed:
			sr_diagnostic(diagnostics, SR_DIAGNOSTIC_WARNING, failed.condition, '%s; kept as an opaque item of the group' % failed,
				path=item_path, value=tracking_uid, strict=strict)
			unreadable[idx] = failed
			opaque_geometry = opaque_geometry or failed.geometry
			continue
		if place is None:
			place = _place(coordinate)
		elif _place(coordinate) != place:
			mixed = True
			unreadable[idx] = None
			opaque_geometry = True
			continue
		landmarks[idx] = (landmark, coordinate)
	if mixed:
		sr_diagnostic(diagnostics, SR_DIAGNOSTIC_WARNING, SR_CONDITION_LANDMARK_SET_MIXED_FRAMES, 'landmarks on more than one frame or '
			'image; the set takes those of the first readable landmark, the others are opaque items', path=path, value=tracking_uid,
			strict=strict)
	if not landmarks:
		sr_diagnostic(diagnostics, SR_DIAGNOSTIC_WARNING, SR_CONDITION_LANDMARK_SET_READ_AS_GROUP, 'no landmark item is readable; the group '
			'is kept as read, not listed as a landmark set', path=path, value=tracking_uid, strict=strict)
		return None
	consumed.update(landmarks)

	# The set's Finding: name and properties (nested and flat under it, legacy at group level)
	finding_idx = next((idx for idx, child in enumerate(children) if idx not in consumed and _value_type(child) == 'CODE'
		and _named(child, DCMSR_FINDING) and child.get('ConceptCodeSequence')), None)
	concept, label, description, display, series_uid, extra, duplicate = None, None, None, None, None, [], None
	group_consumed = set(consumed) | set(unreadable)
	if finding_idx is not None:
		consumed.add(finding_idx)
		group_consumed.add(finding_idx)
		finding = children[finding_idx]
		finding_children = _children(finding)
		concept, label, text_idx, duplicate_idx = _read_name(finding, diagnostics, strict, path, landmark=False)
		finding_consumed = set(i for i in (text_idx, duplicate_idx) if i is not None)
		values, display = _properties(finding_children, finding_consumed, children, group_consumed, diagnostics, strict, path, 'Finding',
			keep_text=label is None and concept is not None)
		description = values.get('description')
		if label is None and concept is not None and values.get('text'):
			label = values['text']
		series_idx = next((i for i, child in enumerate(finding_children) if i not in finding_consumed
			and _named(child, DCMSR_MEASUREMENT_SOURCE_SERIES) and _value_type(child) == 'UIDREF'), None)
		if series_idx is not None:
			finding_consumed.add(series_idx)
			series_uid = str(finding_children[series_idx].get('UID') or '') or None
		duplicate = copy.deepcopy(finding_children[duplicate_idx]) if duplicate_idx is not None else None
		extra = [copy.deepcopy(child) for i, child in enumerate(finding_children) if i not in finding_consumed]
	if series_uid is None:
		series_idx = next((i for i, child in enumerate(children) if i not in group_consumed and _named(child, DCMSR_MEASUREMENT_SOURCE_SERIES)
			and _value_type(child) == 'UIDREF'), None)
		if series_idx is not None:
			group_consumed.add(series_idx)
			series_uid = str(children[series_idx].get('UID') or '') or None
	_note_label(concept, label, diagnostics, path)
	if concept is None and label is None:
		sr_diagnostic(diagnostics, SR_DIAGNOSTIC_WARNING, SR_CONDITION_LANDMARK_SET_WITHOUT_NAME, 'landmark set without a concept or a '
			'label; read with the name "" and refused on an edited save until it is named', path=path, value=tracking_uid, strict=strict)

	# Opaque slots of the group, each anchored after the modelled sibling it followed
	order, anchor, opaque = ['start'], 'start', []
	for idx, child in enumerate(children):
		if idx in pair:
			anchor = 'start'
		elif idx == finding_idx:
			anchor = 'finding'
			order.append(anchor)
		elif idx in landmarks:
			anchor = landmarks[idx][0]
			order.append(anchor)
		elif idx in group_consumed and idx not in unreadable:
			continue
		else:
			opaque.append((anchor, copy.deepcopy(child)))
	first = next(iter(landmarks.values()))[1]
	lset = LandmarkSet.__new__(LandmarkSet)
	LandmarkSet.__init__(lset, [landmark for landmark, _ in landmarks.values()], description=description, display=display,
		tracking_id=tracking_id, tracking_uid=tracking_uid, series_uid=series_uid, frame_of_reference_uid=first['frame_uid'],
		reference=first['reference'], extra_items=extra)
	lset._concept, lset._label = concept, label
	# `read_token` names this read of the group: copies of the set share it, so only one of them is the occurrence read
	lset._parsed = {'raw': copy.deepcopy(group), 'read_token': generate_uid(), 'opaque': opaque, 'order': order, 'duplicate_text': duplicate,
		'opaque_geometry': opaque_geometry, 'has_finding': finding_idx is not None}
	lset._parsed['state'] = lset._state()
	return lset


def srparse_landmark_set_group(group, strict=False, diagnostics=None, **kwargs):
	'''	Landmark set of a standalone measurement group (the tracking pair read here).

		@returns LandmarkSet, or None for a group read as a plain group
	'''
	from .seriestag import srparse_report_group
	parsed = srparse_report_group(group, strict=strict, diagnostics=diagnostics, **kwargs)
	return getattr(parsed, 'landmark_set', None)


def note_duplicate_identities(sets, diagnostics=None, strict=False):
	'''	`duplicate-fiducial-uid` for each Fiducial UID held by more than one landmark of the sets read from a document,
		within a set or across sets. Both landmarks are kept.

		@input sets (list of LandmarkSet)
		@input diagnostics (list or None): receives the warnings
		@input strict (bool): raise `SRDiagnosticError` instead
	'''
	seen = OrderedDict()
	for lset in sets:
		for landmark in lset.landmarks:
			seen.setdefault(landmark.fiducial_uid, []).append(lset)
	for uid, owners in seen.items():
		if len(owners) > 1:
			sr_diagnostic(diagnostics, SR_DIAGNOSTIC_WARNING, SR_CONDITION_DUPLICATE_FIDUCIAL_UID, 'Fiducial UID held by %d landmarks; both '
				'are kept' % len(owners), path=_path(owners[0].tracking_uid), value=uid, strict=strict)


# ---- legacy point groups ----


def _template_id(item):
	sequence = item.get('ContentTemplateSequence') or []
	return str(sequence[0].get('TemplateIdentifier') or '') if len(sequence) else None


def srdoc_legacy_landmark_sets(root_items, diagnostics=None, strict=False):
	'''	Legacy sets of a document's `Sonador-SR.1002` point groups: the child groups of the root container
		`Clinical-Report.Volume-Measurements` with that template identifier. The identifier under any other parent (a
		metadata group) is never a point group. Each group reads on its own as a `LandmarkSet` with `legacy` set and the
		`info` `landmark-legacy-collection`: one landmark per `POINT` item and per point of a `MULTIPOINT` item, the
		tracking identifier as the label, the tracking UID kept, the types mapped from the 0.2 concepts.

		Points in another frame than the first readable item's are not members (`landmark-set-mixed-frames`). The reason a
		group cannot be migrated losslessly is recorded on the set, which is then read-only: an edit of it, of one of its
		landmarks or of its membership raises `landmark-legacy-not-migratable` when it is attempted.

		@input root_items (list of pydicom.Dataset): the document's preserved root items
		@returns list of LandmarkSet
	'''
	found = []
	for container_index, container in enumerate(root_items):
		if not _named(container, DCMSR_REPORT_VOLUME_MEAUREMENTS):
			continue
		for position, group in enumerate(_children(container)):
			if _template_id(group) != DCMSR_LEGACY_POINT_GROUP_TEMPLATE:
				continue
			lset = _read_legacy_group(group, (container_index, position), diagnostics, strict)
			if lset is not None:
				found.append(lset)
	return found


# Attributes a legacy item may carry and still be migrated: the model writes each of them again
DCMSR_LEGACY_GROUP_ATTRIBUTES = frozenset(('RelationshipType', 'ValueType', 'ConceptNameCodeSequence', 'ContinuityOfContent',
	'ContentTemplateSequence', 'ContentSequence'))
DCMSR_LEGACY_TRACKING_ATTRIBUTES = frozenset(('RelationshipType', 'ValueType', 'ConceptNameCodeSequence', 'TextValue', 'UID'))
DCMSR_LEGACY_MULTIPOINT_ATTRIBUTES = frozenset(('RelationshipType', 'ValueType', 'ConceptNameCodeSequence', 'GraphicType', 'GraphicData',
	'ReferencedFrameOfReferenceUID'))


def _unmodelled_attributes(item, allowed):
	return sorted(element.keyword or str(element.tag) for element in item if (element.keyword or str(element.tag)) not in allowed)


def _read_legacy_group(group, origin, diagnostics, strict):
	'''	One legacy point group, read as a legacy set. `origin` is (index of its container among the root items, index
		of the group in the container), where the group was read; the group's encoded content is recorded with it, and a
		migration removes only a child of the writing report that holds exactly that content.
	'''
	children = _children(group)
	tracking_id = tracking_uid = None
	others, coordinates, pair = [], [], []
	for child in children:
		if tracking_id is None and _named(child, dcmcodes.DCM.TrackingIdentifier) and _value_type(child) == 'TEXT':
			tracking_id = _text_value(child)
			pair.append(child)
		elif tracking_uid is None and _named(child, dcmcodes.DCM.TrackingUniqueIdentifier) and _value_type(child) == 'UIDREF':
			tracking_uid = str(child.get('UID') or '') or None
			pair.append(child)
		elif _value_type(child) in ('SCOORD', 'SCOORD3D'):
			coordinates.append(child)
		else:
			others.append(child)
	path = _path(tracking_uid)
	reasons = []
	# The tracking pair is written again from its two values only: anything else on it would be lost
	for item in pair:
		extra = _unmodelled_attributes(item, DCMSR_LEGACY_TRACKING_ATTRIBUTES)
		if extra:
			reasons.append('a tracking item carries content the landmark-set form does not hold (%s)' % ', '.join(extra))
	unmodelled = _unmodelled_attributes(group, DCMSR_LEGACY_GROUP_ATTRIBUTES)
	if unmodelled:
		reasons.append('the group item carries attributes the landmark-set form does not hold (%s)' % ', '.join(unmodelled))
	frame_uid, mixed, unplaced, landmarks = None, False, False, []
	for item in coordinates:
		graphic = str(item.get('GraphicType') or '')
		if _value_type(item) != 'SCOORD3D' or graphic not in ('POINT', 'MULTIPOINT'):
			reasons.append('a coordinate item is a %s %s' % (_value_type(item), graphic))
			continue
		item_frame = str(item.get('ReferencedFrameOfReferenceUID') or '') or None
		if item_frame is None:
			# Never given another item's frame: the item stays in the group as read, and the set cannot be edited
			reasons.append('a coordinate item names no Frame of Reference')
			unplaced = True
			continue
		try:
			data = [float(v) for v in (item.get('GraphicData') or [])]
		except (TypeError, ValueError):
			data = []
		if len(data) % 3 or not data or not all(math.isfinite(v) for v in data) or (graphic == 'POINT' and len(data) != 3):
			reasons.append('a coordinate item has unusable GraphicData')
			unplaced = True
			continue
		if frame_uid is None:
			frame_uid = item_frame
		if item_frame != frame_uid:
			mixed = True
			continue
		name = _code_dataset(item, 'ConceptNameCodeSequence')
		landmark_type, absent = _landmark_type(name, diagnostics, path, legacy=True)
		fiducial = str(item.get('FiducialUID') or '') or None
		if graphic == 'MULTIPOINT' and fiducial:
			reasons.append('a MULTIPOINT item carries a Fiducial UID, which cannot be divided among its points')
		if graphic == 'MULTIPOINT':
			extra = _unmodelled_attributes(item, DCMSR_LEGACY_MULTIPOINT_ATTRIBUTES | set(('FiducialUID',)))
			if extra:
				reasons.append('a MULTIPOINT item carries content that cannot be divided among its points (%s)' % ', '.join(extra))
		points = [tuple(data[i:i + 3]) for i in range(0, len(data), 3)]
		for point in points:
			landmark = Landmark(point, landmark_type=landmark_type,
				fiducial_uid=fiducial if (graphic == 'POINT' and fiducial) else None)
			# A POINT item is retained and patched when the group is migrated, so its other attributes and children survive
			landmark._parsed = {'raw': None, 'coordinate': copy.deepcopy(item) if graphic == 'POINT' else None, 'type_absent': absent,
				'legacy_type': copy.deepcopy(name), 'provisional': not (graphic == 'POINT' and fiducial), 'duplicate_text': None}
			landmark._parsed['state'] = landmark._state()
			landmarks.append(landmark)
	if mixed:
		reasons.append('its items name different frames')
		sr_diagnostic(diagnostics, SR_DIAGNOSTIC_WARNING, SR_CONDITION_LANDMARK_SET_MIXED_FRAMES, 'legacy point group whose items name '
			'different frames; the set holds the points of the first frame only', path=path, value=tracking_uid, strict=strict)
	if others:
		reasons.append('the group holds content other than its points (%d items)' % len(others))
	if not landmarks:
		return None
	sr_diagnostic(diagnostics, SR_DIAGNOSTIC_INFO, SR_CONDITION_LANDMARK_LEGACY_COLLECTION, 'Sonador-SR.1002 point group read as a legacy '
		'landmark set; written back unchanged until edited', path=path, value=tracking_uid)
	lset = LandmarkSet(landmarks, label=tracking_id if tracking_id and not landmark_label_problem(tracking_id, LANDMARK_LABEL_CODE_MEANING)
		else None, tracking_uid=tracking_uid, frame_of_reference_uid=frame_uid, legacy=True)
	if lset.label is None and tracking_id:
		lset._label = tracking_id
	from ..base.sr import _items_state
	lset._parsed = {'raw': None, 'opaque': [], 'duplicate_text': None, 'opaque_geometry': mixed or unplaced, 'legacy_origin': tuple(origin),
		'legacy_content': _items_state([group])[0], 'legacy_item': group, 'legacy_tracking_id': tracking_id,
		'legacy_tracking_uid': tracking_uid,
		'read_token': generate_uid(),
		'legacy_reason': '; '.join(reasons) if reasons else None}
	lset._parsed['state'] = lset._state()
	if reasons:
		lset._freeze('; '.join(reasons))
	return lset


# ---- writing ----


def raw_group_template(dataset, tracking_id, tracking_uid):
	'''	A measurement-group template whose group item is `dataset`, written back exactly as read.

		@returns highdicom MeasurementsAndQualitativeEvaluations
		@raises SRDiagnosticError for a group holding a by-reference item
	'''
	template = MeasurementsAndQualitativeEvaluations(tracking_identifier=TrackingIdentifier(uid=tracking_uid or generate_uid(),
		identifier=tracking_id or 'group'))
	template[0:1] = [srcontent_from_dataset(dataset)]
	return template


def _value_code(concept, label, landmark):
	if concept is not None:
		return concept
	if label is not None:
		return free_text_finding(label)
	return DCMSR_LANDMARK_POINT if landmark else None


def _refuse(condition, message, path, value=None):
	raise SRDiagnosticError(SRDiagnostic(SR_DIAGNOSTIC_ERROR, condition, path, value, message))


def _check_name(owner, path, landmark):
	from . import SR_CONDITION_LANDMARK_RESERVED_CONCEPT
	if owner.concept is not None and code_key(owner.concept) in owner.reserved_concepts:
		_refuse(SR_CONDITION_LANDMARK_RESERVED_CONCEPT, 'a reserved code is given as the concept', path, getattr(owner.concept, 'value', None))
	problem = owner.label_problem
	if problem:
		_refuse(SR_CONDITION_LANDMARK_LABEL_UNREPRESENTABLE, 'label %r cannot be stored where it is written: %s' % (owner.label, problem),
			path, owner.label)


def _note_overlength(concept, label, diagnostics, path):
	if concept is None and label is not None and len(label) > DCM_CODE_MEANING_MAX_LENGTH:
		sr_diagnostic(diagnostics, SR_DIAGNOSTIC_INFO, SR_CONDITION_OVERLENGTH_CODE_MEANING, 'label of %d characters written as a CodeMeaning '
			'(LO limit %d); written verbatim, not conformant' % (len(label), DCM_CODE_MEANING_MAX_LENGTH), path=path, value=label)


def _name_items(owner, diagnostics, path, landmark):
	'''	The value code of an item and its label TEXT child (beside a concept), with the duplicate Text item kept while the
		name is unchanged.

		@returns tuple (value code, list of children)
	'''
	children = []
	value = _value_code(owner.concept, owner.label, landmark)
	if owner.concept is not None and owner.label is not None:
		children.append(TextContentItem(name=DCMSR_MEASUREMENT_TEXT, value=owner.label, relationship_type=RelationshipTypeValues.HAS_PROPERTIES))
	duplicate = (owner._parsed or {}).get('duplicate_text')
	if duplicate is not None:
		if owner.name_edited:
			sr_diagnostic(diagnostics, SR_DIAGNOSTIC_INFO, SR_CONDITION_LANDMARK_LABEL_DUPLICATE_DROPPED, 'Text item read beside a free-text '
				'value dropped once the name was edited', path=path)
		else:
			children.extend(srcontent_extra_items([duplicate]))
	_note_overlength(owner.concept, owner.label, diagnostics, path)
	return value, children


def _patched_coordinate(landmark, lset, edited):
	'''	The coordinate item of a landmark: the retained item with every modelled field that changed patched in, or a new
		item.

		@returns highdicom content item
	'''
	parsed = landmark._parsed or {}
	source = parsed.get('coordinate')
	image = lset.is_image
	value_type = 'SCOORD' if image else 'SCOORD3D'
	type_written = not (parsed.get('type_absent') and not edited)
	if source is not None and source.get('ValueType') == value_type:
		item = copy.deepcopy(source)
		item.RelationshipType = 'INFERRED FROM'
		item.GraphicData = [float(v) for v in landmark.point]
		item.FiducialUID = landmark.fiducial_uid
		if type_written:
			# A CodedConcept, so that the item is valid whether it was read from a file (a pydicom Dataset) or from a
			# document built in this session (a highdicom content item, kept as it is)
			from pydicom import Sequence as DcmSequence
			from . import srcode2dataset
			item.ConceptNameCodeSequence = DcmSequence([_concept_of(srcode2dataset(landmark.landmark_type))])
		if image:
			item.PixelOriginInterpretation = DCMSR_PIXEL_ORIGIN_FRAME
			from .measurements import _set_image_reference
			_set_image_reference(item, lset.reference)
		else:
			item.ReferencedFrameOfReferenceUID = lset.frame_of_reference_uid
		return srcontent_from_dataset(item)
	if image:
		reference = lset.reference
		frames = [reference.frame_number] if reference.frame_number else None
		source_image = _mark_unnamed(SourceImageForRegion(reference.sop_class_uid or '', reference.sop_instance_uid,
			referenced_frame_numbers=frames))
		item = ScoordContentItem(name=landmark.landmark_type, graphic_type='POINT', graphic_data=np.array([list(landmark.point)], dtype=float),
			pixel_origin_interpretation=DCMSR_PIXEL_ORIGIN_FRAME, fiducial_uid=landmark.fiducial_uid,
			relationship_type=RelationshipTypeValues.INFERRED_FROM)
		item.ContentSequence = ContentSequence([source_image])
		return item
	return Scoord3DContentItem(name=landmark.landmark_type, graphic_type='POINT', graphic_data=np.array([list(landmark.point)], dtype=float),
		frame_of_reference_uid=lset.frame_of_reference_uid, fiducial_uid=landmark.fiducial_uid,
		relationship_type=RelationshipTypeValues.INFERRED_FROM)


def _landmark_item(landmark, lset, diagnostics, path):
	'''	Landmark item of an edited set: the item as read (with a provisional identity written) when the landmark and its
		place are unchanged, else the item of the 0.3 form.
	'''
	parsed = landmark._parsed or {}
	raw = parsed.get('raw')
	if raw is not None and not landmark.edited and not lset.structure_edited:
		item = copy.deepcopy(raw)
		if parsed.get('provisional'):
			item.ContentSequence[parsed['coordinate_index']].FiducialUID = landmark.fiducial_uid
		return srcontent_from_dataset(item)
	_check_name(landmark, path, landmark=True)
	value, name_children = _name_items(landmark, diagnostics, path, landmark=True)
	children = [_patched_coordinate(landmark, lset, edited=True)]
	if landmark.description:
		children.append(TextContentItem(name=DCMSR_MEASUREMENT_DESCRIPTION, value=landmark.description,
			relationship_type=RelationshipTypeValues.HAS_PROPERTIES))
	children.extend(name_children)
	children.extend(srencode_display_items(landmark.display))
	children.extend(srcontent_extra_items(landmark.extra_items))
	item = CodeContentItem(name=DCMSR_LANDMARK, value=value, relationship_type=RelationshipTypeValues.CONTAINS)
	item.ContentSequence = ContentSequence(children)
	return item


def _finding_item(lset, diagnostics, path):
	value, name_children = _name_items(lset, diagnostics, path, landmark=False)
	children = []
	if lset.description:
		children.append(TextContentItem(name=DCMSR_MEASUREMENT_DESCRIPTION, value=lset.description,
			relationship_type=RelationshipTypeValues.HAS_PROPERTIES))
	children.extend(name_children)
	if lset.is_patient and lset.series_uid:
		children.append(UIDRefContentItem(name=DCMSR_MEASUREMENT_SOURCE_SERIES, value=lset.series_uid,
			relationship_type=RelationshipTypeValues.HAS_PROPERTIES))
	children.extend(srencode_display_items(lset.display))
	children.extend(srcontent_extra_items(lset.extra_items))
	item = CodeContentItem(name=DCMSR_FINDING, value=value, relationship_type=RelationshipTypeValues.CONTAINS)
	item.ContentSequence = ContentSequence(children)
	return item


def validate_landmark_set(lset, images=None):
	'''	Refuse a new or edited set that cannot be written: no name, no landmark, a reserved concept or unrepresentable
		label, points of both domains, a patient-domain set without a frame, an image-domain set without one image
		reference, an image reference without the one frame number a multiframe image needs, a point outside its image
		(when its size is known), or a legacy set that cannot be migrated.

		@input images (mapping, default=None): `ImageMetadata` (or a `(columns, rows)` tuple) by SOP Instance UID

		@raises SRDiagnosticError
	'''
	path = _path(lset.tracking_uid)
	if lset.legacy and (lset._parsed or {}).get('legacy_reason'):
		_refuse(SR_CONDITION_LANDMARK_LEGACY_NOT_MIGRATABLE, 'the legacy point group cannot be migrated without loss (%s); it stays as '
			'read. Build a new set with LandmarkSet.from_points and state its frame' % lset._parsed['legacy_reason'], path, lset.tracking_uid)
	if lset.concept is None and lset.label is None:
		_refuse(SR_CONDITION_LANDMARK_SET_WITHOUT_NAME, 'a landmark set needs a concept or a non-empty label', path, lset.tracking_uid)
	_check_name(lset, path, landmark=False)
	if not len(lset.landmarks):
		_refuse(SR_CONDITION_LANDMARK_SET_EMPTY, 'a landmark set needs at least one landmark', path, lset.tracking_uid)
	if lset.reference is not None and lset.frame_of_reference_uid:
		_refuse(SR_CONDITION_LANDMARK_SET_MIXED_FRAMES, 'the set names both an image and a Frame of Reference; one set, one domain',
			path, lset.tracking_uid)
	dims = set(len(item.point) for item in lset.landmarks)
	if len(dims) > 1:
		_refuse(SR_CONDITION_LANDMARK_SET_MIXED_FRAMES, 'landmarks in both domains; one set, one domain', path, lset.tracking_uid)
	if lset.is_image:
		reference = lset.reference
		if reference is None or not reference.sop_instance_uid:
			_refuse(SR_CONDITION_LANDMARK_IMAGE_REFERENCE_MISSING, 'an image-domain set needs the image it is on', path, lset.tracking_uid)
		if dims != set((2,)):
			_refuse(SR_CONDITION_LANDMARK_SET_MIXED_FRAMES, 'an image-domain set holds (column, row) points', path, lset.tracking_uid)
		if reference.frame_number is not None and not _positive_frame(reference.frame_number):
			_refuse(SR_CONDITION_LANDMARK_IMAGE_REFERENCE_AMBIGUOUS, 'frame number %r is not a positive integer' % (reference.frame_number,),
				path, reference.sop_instance_uid)
		meta = image_metadata(images, reference)
		if image_is_multiframe(meta, reference) and not reference.frame_number:
			_refuse(SR_CONDITION_LANDMARK_IMAGE_REFERENCE_AMBIGUOUS, 'the image is multiframe; the reference needs the one frame the '
				'points are on', path, reference.sop_instance_uid)
		if meta.frames is not None and reference.frame_number and int(reference.frame_number) > int(meta.frames):
			_refuse(SR_CONDITION_LANDMARK_IMAGE_REFERENCE_AMBIGUOUS, 'frame %s of an image of %s frames' % (reference.frame_number,
				meta.frames), path, reference.sop_instance_uid)
		for item in lset.landmarks:
			if _out_of_bounds(item.point, meta):
				_refuse(SR_CONDITION_LANDMARK_POINT_OUT_OF_BOUNDS, 'point %r lies outside the image of %d columns and %d rows'
					% (tuple(item.point), meta.columns, meta.rows), path, item.fiducial_uid)
	else:
		if not lset.frame_of_reference_uid:
			_refuse(SR_CONDITION_LANDMARK_POINT_INVALID, 'a patient-domain set needs a Frame of Reference UID', path, lset.tracking_uid)
		if dims != set((3,)):
			_refuse(SR_CONDITION_LANDMARK_SET_MIXED_FRAMES, 'a patient-domain set holds (x, y, z) points', path, lset.tracking_uid)


def srencode_landmark_set(lset, diagnostics=None, strict=False, images=None, **kwargs):
	'''	Measurement group of a landmark set. An unchanged set read from a document is written back exactly as read. A new
		or edited set is validated (`validate_landmark_set`) and written in the 0.3 form: the tracking pair, the Finding,
		the landmark items in set order, and the opaque slots of the group after the modelled sibling they followed (the
		nearest earlier surviving one when that sibling was removed). A legacy set is written in this form when it is
		migrated (`info` `landmark-legacy-migrated`). Makes no request.

		@input lset (LandmarkSet)
		@input diagnostics (list, default=None)
		@input images (mapping, default=None): `ImageMetadata` (or a `(columns, rows)` tuple) by SOP Instance UID, to check the
			image reference and points of an image-domain set

		@returns highdicom MeasurementsAndQualitativeEvaluations
		@raises SRDiagnosticError for a set that cannot be written, or a group holding a by-reference item
	'''
	parsed = lset._parsed or {}
	if parsed.get('raw') is not None and not lset.edited:
		return raw_group_template(parsed['raw'], lset.tracking_id, lset.tracking_uid)
	validate_landmark_set(lset, images=images or getattr(lset, '_write_images', None))
	path = _path(lset.tracking_uid)
	if not lset.tracking_uid:
		lset.tracking_uid = generate_uid()
	if lset.legacy:
		sr_diagnostic(diagnostics, SR_DIAGNOSTIC_INFO, SR_CONDITION_LANDMARK_LEGACY_MIGRATED, 'legacy point group migrated to a landmark-set '
			'group in Imaging Measurements', path=path, value=lset.tracking_uid)
	template = MeasurementsAndQualitativeEvaluations(tracking_identifier=TrackingIdentifier(uid=lset.tracking_uid, identifier=lset.tracking_id))
	content = template[0].ContentSequence

	# Opaque slots after their anchor; an anchor that was removed passes its slots to the nearest earlier survivor
	current = list(lset.landmarks)
	present = set(id(item) for item in current)
	slots = OrderedDict((('start', []), ('finding', [])))
	for item in current:
		slots[id(item)] = []
	for anchor, item in parsed.get('opaque', []):
		key = anchor if isinstance(anchor, str) else id(anchor)
		if not isinstance(anchor, str) and key not in present:
			key = _earlier_survivor(anchor, parsed, present)
		slots.setdefault(key, []).append(item)
	for item in srcontent_extra_items(slots['start']):
		content.append(item)
	content.append(_finding_item(lset, diagnostics, path))
	for item in srcontent_extra_items(slots['finding']):
		content.append(item)
	for index, landmark in enumerate(current):
		content.append(_landmark_item(landmark, lset, diagnostics, '%s, landmark %d' % (path, index + 1)))
		for item in srcontent_extra_items(slots.get(id(landmark), [])):
			content.append(item)
	return template


def _earlier_survivor(anchor, parsed, present):
	'''	The nearest sibling, before a removed anchor in the order read, that is still in the set.
	'''
	order = parsed.get('order', [])
	try:
		position = next(i for i, entry in enumerate(order) if entry is anchor)
	except StopIteration:
		return 'finding'
	for entry in reversed(order[:position]):
		if isinstance(entry, str):
			return entry
		if id(entry) in present:
			return id(entry)
	return 'start'


def check_landmark_identities(sets, diagnostics=None, report_token=None):
	'''	Refuse a write in which a Fiducial UID is held by two landmark occurrences and either is new or edited. An
		occurrence is new when its set did not hold that UID that many times when it was read: a landmark added,
		inserted (also into a second set) or appended again, and every landmark of a set created in Python, of a set
		read by another report, of a second copy of a set read from the document, or of a legacy set being migrated.
		Duplicates the writing report's document already held, in sets written back unchanged, are kept as read.

		@input sets (list of LandmarkSet): every set the report writes
		@input report_token (str, default=None): token of the writing report; the sets it read carry it
		@raises SRDiagnosticError `duplicate-fiducial-uid`
	'''
	from collections import Counter
	holders, tokens = OrderedDict(), set()
	for lset in sets:
		parsed = lset._parsed or {}
		# A copy of a set read from the document (same read token) is not the occurrence that was read: all of it is new
		token = parsed.get('read_token')
		copied = token is not None and token in tokens
		if token is not None:
			tokens.add(token)
		foreign = report_token is None or parsed.get('report_token') != report_token
		imported = Counter() if (not parsed or copied or foreign or (lset.legacy and lset.edited)) else Counter(parsed['state'][10])
		seen = Counter()
		for landmark in lset.landmarks:
			uid = landmark.fiducial_uid
			seen[uid] += 1
			new = seen[uid] > imported[uid] or landmark.edited
			holders.setdefault(uid, []).append((lset, new))
	for uid, found in holders.items():
		if len(found) > 1 and any(new for _, new in found):
			_refuse(SR_CONDITION_DUPLICATE_FIDUCIAL_UID, 'Fiducial UID held by %d landmarks among the sets being written; a new or '
				'edited landmark needs an identity of its own' % len(found), _path(found[0][0].tracking_uid), uid)
