'''	Sonador-SR 0.3: the Series Tag exchange profile shared with the Sonador Viewer
	(oak-tree/medical-imaging/sonador-client#36).

	A Series Tag is a qualitative finding (`Finding`) that applies to a whole imaging series. In a
	DICOM-SR document it is one TID 1501 measurement group holding a `(121071, DCM, "Finding")` CODE
	item whose value is the tag code, carried verbatim. The per-finding properties are children of
	the Finding item:

	* `INFERRED FROM` IMAGE: one referenced SOP instance, representative of the series;
	* `HAS PROPERTIES` TEXT: the extended meta attributes (`Sonador-Measurement.Description`, `.Text`,
		`.Location`), flat in every document class;
	* the display colours, one TEXT item per colour role present with a CIELab `L\\a\\b` value
		(0-65535). The registry holds six roles: the Series Tag roles `Background`, `Border`, `Value`,
		`Meaning`, and a measurement's `Line` and `Text`; any role is read and written on any item:
		- nested form (Comprehensive 3D SR, the only form written): a `HAS PROPERTIES` CONTAINER
			`Sonador-DisplayAttributes` holding a `CONTAINS` CONTAINER `Sonador-DisplayAttributes.Color`
			holding the role items;
		- flat form (Enhanced SR, which permits no CONTAINER under a CODE item; read only): the role
			items are `HAS PROPERTIES` children of the Finding, named by their dotted concepts.

	Readers accept three layouts: the nested and flat forms above and the legacy layout of the
	pre-0.3 Viewer documents, in which the meta and display items are group-level siblings of the
	Finding. The legacy layout is repaired on read with a `warning` diagnostic and rewritten in the
	writer's own form. Readers match concepts by code value and scheme designator: a display-attribute
	concept whose scheme version is present and is not 0.3 (an undefined scheme version) is read as
	the registered concept, with the `undefined-scheme-version` warning, and rewritten at version 0.3.

	The quantitative tools (Length, Angle, Cobb angle) are in `sonador.apisettings.sr.measurements`;
	their tracking identifiers are registered here so that a group routes by one table.

	Contents: the concept registry (`DCMSR_MEASUREMENT_META`, `DCMSR_DISPLAY_ATTRIBUTES`,
	`DCMSR_DISPLAY_COLOR_ROLES`), the tracking identifier map (`SONADOR_MEASUREMENT_TYPES`), the root
	templates, document classes and layouts, the colour codec (`color2dcmlab`, `dcmlab2color`), the
	per-finding encoder (`srencode_finding_children`) and the per-finding and per-group parsers
	(`srparse_finding`, `srparse_report_group`). The document-level reader and writers are in
	`sonador.imaging.helpers.sr`.
'''
import copy, logging
from collections import OrderedDict

from pydicom.dataset import Dataset as DcmDataset
from pydicom.sr.codedict import codes as dcmcodes

from highdicom.sr.templates import Code, CodedConcept, TrackingIdentifier
from highdicom.sr.value_types import ContentSequence, ContainerContentItem, TextContentItem, ImageContentItem
from highdicom.sr.enum import RelationshipTypeValues, ValueTypeValues

from ..base import SONADOR_MANUFACTURER, SONADOR_SR, SONADOR_SCHEME_VERSION_03, Finding, FindingCollection, \
	MeasurementCollection, SONADOR_MEASUREMENT_TYPE_SERIES_TAG, SONADOR_MEASUREMENT_TYPE_LENGTH, SONADOR_MEASUREMENT_TYPE_ANGLE, \
	SONADOR_MEASUREMENT_TYPE_COBB_ANGLE, SONADOR_MEASUREMENT_TYPE_CIRCLE, SONADOR_MEASUREMENT_TYPE_ARROW, SONADOR_MEASUREMENT_TYPE_GENERIC, ReportGroup, ImageReference, DCMSR_FINDING, RGBColor, \
	DCMHEADER_SR_VALUE_TYPE, DCMHEADER_CONCEPT_CODE_SEQUENCE, DCMHEADER_CODING_SCHEME_VERSION, \
	DCMHEADER_CODING_SCHEME_DESIGNATOR, DCMHEADER_CODE_MEANING, DCMHEADER_CODE_VALUE, \
	DCMHEADER_SR_REF_SOP_SEQ, DCMHEADER_SR_SOP_CLASS_UID, DCMHEADER_SR_REF_INSTANCE_UID, DCMHEADER_SERIES_INSTANCE_UID
from . import dcm_code_value, dcm_code_matches, srcontent_name, srcontent_is_named, srcontent_is_by_reference, srcontent_holds_by_reference, \
	sr_diagnostic, SRDiagnosticError, DCM_CODE_VALUE_MAX_LENGTH, DCM_CODE_MEANING_MAX_LENGTH, \
	DCMSR_VALUE_TYPES_OPTIONAL_NAME, \
	SR_DIAGNOSTIC_INFO, SR_DIAGNOSTIC_WARNING, SR_DIAGNOSTIC_ERROR, \
	SR_CONDITION_MISSING_RELATIONSHIP, SR_CONDITION_SELECTED_FROM_IMAGE, SR_CONDITION_VERSION_EQUALS_DESIGNATOR, \
	SR_CONDITION_UNDEFINED_SCHEME_VERSION, SR_CONDITION_GROUP_LEVEL_PROPERTIES, SR_CONDITION_OVERLENGTH_CODE_VALUE, \
	SR_CONDITION_OVERLENGTH_CODE_MEANING, SR_CONDITION_INVALID_DISPLAY_COLOR, SR_CONDITION_ITEM_DROPPED, \
	SR_CONDITION_GROUP_WITHOUT_FINDING, SR_CONDITION_BY_REFERENCE_ITEM, SR_CONDITION_UNKNOWN_TRACKING_ID
from ..display_attrs import DISPLAY_COLOR_ROLES, DisplayAttributes

logger = logging.getLogger(__name__)


# Sonador-SR 0.3 concept registry

SONADOR_MEASUREMENT_DESCRIPTION = '%s-Measurement.Description' % SONADOR_MANUFACTURER
SONADOR_MEASUREMENT_DESCRIPTION_MEANING = 'Free-text description of a measurement. Primary measurement attribute.'
DCMSR_MEASUREMENT_DESCRIPTION = Code(SONADOR_MEASUREMENT_DESCRIPTION, SONADOR_SR, SONADOR_MEASUREMENT_DESCRIPTION_MEANING,
	scheme_version=SONADOR_SCHEME_VERSION_03)

SONADOR_MEASUREMENT_TEXT = '%s-Measurement.Text' % SONADOR_MANUFACTURER
SONADOR_MEASUREMENT_TEXT_MEANING = 'Free-text description of a measurement. Secondary measurement attribute.'
DCMSR_MEASUREMENT_TEXT = Code(SONADOR_MEASUREMENT_TEXT, SONADOR_SR, SONADOR_MEASUREMENT_TEXT_MEANING,
	scheme_version=SONADOR_SCHEME_VERSION_03)

SONADOR_MEASUREMENT_LOCATION = '%s-Measurement.Location' % SONADOR_MANUFACTURER
SONADOR_MEASUREMENT_LOCATION_MEANING = 'Free-text field describing the "location" of the measurement. Secondary measurement attribute.'
DCMSR_MEASUREMENT_LOCATION = Code(SONADOR_MEASUREMENT_LOCATION, SONADOR_SR, SONADOR_MEASUREMENT_LOCATION_MEANING,
	scheme_version=SONADOR_SCHEME_VERSION_03)

# Extended meta attributes of a finding, in writing order: (model attribute, concept)
DCMSR_MEASUREMENT_META = OrderedDict((
	('description', DCMSR_MEASUREMENT_DESCRIPTION),
	('text', DCMSR_MEASUREMENT_TEXT),
	('location', DCMSR_MEASUREMENT_LOCATION),
))

SONADOR_DISPLAY_ATTRIBUTES = '%s-DisplayAttributes' % SONADOR_MANUFACTURER
DCMSR_DISPLAY_ATTRIBUTES = Code(SONADOR_DISPLAY_ATTRIBUTES, SONADOR_SR, 'Display attributes',
	scheme_version=SONADOR_SCHEME_VERSION_03)
SONADOR_DISPLAY_COLOR = '%s.Color' % SONADOR_DISPLAY_ATTRIBUTES
DCMSR_DISPLAY_COLOR = Code(SONADOR_DISPLAY_COLOR, SONADOR_SR, 'Display color', scheme_version=SONADOR_SCHEME_VERSION_03)
DCMSR_DISPLAY_COLOR_ROLES = OrderedDict(
	(role, Code('%s.%s' % (SONADOR_DISPLAY_COLOR, role), SONADOR_SR, '%s (CIELab)' % meaning, scheme_version=SONADOR_SCHEME_VERSION_03))
	for role, meaning in (('Background', 'Background color'), ('Border', 'Border color'),
		('Value', 'Coded value color'), ('Meaning', 'Code meaning color'), ('Line', 'Line color'), ('Text', 'Text color')))


# Tracking identifiers and measurement types

# dcmjs prefix followed by the Cornerstone tool type name; shared with the Viewer so that documents
# route to the same tool on both sides.
CORNERSTONE_TOOLS_SR_TAG = 'cornerstoneTools@^4.0.0'
CORNERSTONE3D_TOOLS_SR_TAG = 'Cornerstone3DTools@^0.1.0'			# written by the Cornerstone3D adapters; read only
CORNERSTONE_TOOL_SERIES_TAG = 'DICOMSRSeriesTagTool'
CORNERSTONE_TOOL_LENGTH = 'Length'
CORNERSTONE_TOOL_ANGLE = 'Angle'
CORNERSTONE_TOOL_COBB_ANGLE = 'CobbAngle'
CORNERSTONE_TOOL_CIRCLE = 'CircleROI'
CORNERSTONE_TOOL_ARROW = 'ArrowAnnotate'


def cornerstone_tracking_id(tool):
	'''	Tracking identifier string of a Cornerstone tool (`cornerstoneTools@^4.0.0:<tool>`).

		@input tool (str): Cornerstone tool type name
		@returns str
	'''
	return '%s:%s' % (CORNERSTONE_TOOLS_SR_TAG, tool)


DCMSR_TRACKING_ID_SERIES_TAG = cornerstone_tracking_id(CORNERSTONE_TOOL_SERIES_TAG)

# Tracking identifier -> measurement type. A group whose identifier is not listed is a generic group.
# The `cornerstoneTools` form of a tool comes first: it is the one written for a new measurement.
SONADOR_MEASUREMENT_TYPES = OrderedDict((
	(DCMSR_TRACKING_ID_SERIES_TAG, SONADOR_MEASUREMENT_TYPE_SERIES_TAG),
	(cornerstone_tracking_id(CORNERSTONE_TOOL_LENGTH), SONADOR_MEASUREMENT_TYPE_LENGTH),
	(cornerstone_tracking_id(CORNERSTONE_TOOL_ANGLE), SONADOR_MEASUREMENT_TYPE_ANGLE),
	(cornerstone_tracking_id(CORNERSTONE_TOOL_COBB_ANGLE), SONADOR_MEASUREMENT_TYPE_COBB_ANGLE),
	(cornerstone_tracking_id(CORNERSTONE_TOOL_CIRCLE), SONADOR_MEASUREMENT_TYPE_CIRCLE),
	(cornerstone_tracking_id(CORNERSTONE_TOOL_ARROW), SONADOR_MEASUREMENT_TYPE_ARROW),
	('%s:%s' % (CORNERSTONE3D_TOOLS_SR_TAG, CORNERSTONE_TOOL_LENGTH), SONADOR_MEASUREMENT_TYPE_LENGTH),
	('%s:%s' % (CORNERSTONE3D_TOOLS_SR_TAG, CORNERSTONE_TOOL_ANGLE), SONADOR_MEASUREMENT_TYPE_ANGLE),
	('%s:%s' % (CORNERSTONE3D_TOOLS_SR_TAG, CORNERSTONE_TOOL_COBB_ANGLE), SONADOR_MEASUREMENT_TYPE_COBB_ANGLE),
	('%s:%s' % (CORNERSTONE3D_TOOLS_SR_TAG, CORNERSTONE_TOOL_CIRCLE), SONADOR_MEASUREMENT_TYPE_CIRCLE),
))
SONADOR_FINDING_TYPES = (SONADOR_MEASUREMENT_TYPE_SERIES_TAG,)		# qualitative (findings)
# Quantitative tools a measurement group is parsed to a typed measurement for
SONADOR_QUANTITATIVE_TYPES = (SONADOR_MEASUREMENT_TYPE_LENGTH, SONADOR_MEASUREMENT_TYPE_ANGLE, SONADOR_MEASUREMENT_TYPE_COBB_ANGLE,
	SONADOR_MEASUREMENT_TYPE_CIRCLE)


def measurement_type_for_tracking_id(tracking_id):
	'''	Measurement type a tracking identifier routes to.

		@input tracking_id (str): tracking identifier of a measurement group
		@returns str (`SONADOR_MEASUREMENT_TYPE_*`), or None for an unknown or empty identifier
	'''
	return SONADOR_MEASUREMENT_TYPES.get(tracking_id) if tracking_id else None


def tracking_id_for_measurement_type(measurement_type):
	'''	Tracking identifier written for a measurement type.

		@input measurement_type (str): one of `SONADOR_MEASUREMENT_TYPE_*`
		@returns str, or None for an unknown type
	'''
	for tracking_id, mtype in SONADOR_MEASUREMENT_TYPES.items():
		if mtype == measurement_type:
			return tracking_id
	return None


def tracking_identifier_parts(tracking):
	'''	Parts of a tracking identifier given as a string, a highdicom `TrackingIdentifier` or None.

		@returns tuple (str, str): (identifier, UID); the UID is None for a string, both are None
			for None
	'''
	if tracking is None:
		return (None, None)
	if isinstance(tracking, str):
		return (tracking, None)
	identifier, uid = None, None
	for item in tracking:
		if srcontent_is_named(item, dcmcodes.DCM.TrackingIdentifier):
			identifier = str(item.TextValue)
		elif srcontent_is_named(item, dcmcodes.DCM.TrackingUniqueIdentifier):
			uid = str(item.UID)
	return (identifier, uid)


# Root templates, document classes and layouts

SONADOR_SR_TEMPLATE_ID = '%s.1500' % SONADOR_SR
SONADOR_SR_MAPPING_RESOURCE = SONADOR_SR
DCMR_TEMPLATE_ID = '1500'
DCMR_MAPPING_RESOURCE = 'DCMR'
DCMSR_SUPPORTED_TEMPLATE_IDS = (DCMR_TEMPLATE_ID, SONADOR_SR_TEMPLATE_ID)


def mapping_resource_for_template(template_id):
	'''	Mapping resource paired with a root template identifier: `DCMR` with `1500`, `Sonador-SR` with
		any other identifier (`Sonador-SR.1500`).

		@returns str
	'''
	return DCMR_MAPPING_RESOURCE if template_id == DCMR_TEMPLATE_ID else SONADOR_SR_MAPPING_RESOURCE


DCMSR_SOP_CLASS_ENHANCED = '1.2.840.10008.5.1.4.1.1.88.22'
DCMSR_SOP_CLASS_COMPREHENSIVE = '1.2.840.10008.5.1.4.1.1.88.33'
DCMSR_SOP_CLASS_COMPREHENSIVE_3D = '1.2.840.10008.5.1.4.1.1.88.34'
DCMSR_SOP_CLASSES_SUPPORTED = (DCMSR_SOP_CLASS_ENHANCED, DCMSR_SOP_CLASS_COMPREHENSIVE, DCMSR_SOP_CLASS_COMPREHENSIVE_3D)

DCMSR_LAYOUT_NESTED = 'nested'		# display container under the item (Comprehensive / Comprehensive 3D SR; written)
DCMSR_LAYOUT_FLAT = 'flat'			# dotted role items under the item (Enhanced SR; read only)
DCMSR_LAYOUT_GROUP = 'group'		# legacy: meta and display items as group-level siblings (read only)

# Documents are written as Comprehensive 3D SR with the nested layout; the others are read
DCMSR_LAYOUTS_WRITTEN = (DCMSR_LAYOUT_NESTED,)


def layout_for_sop_class(sop_class_uid):
	'''	Layout of the per-item properties a document of a class carries when written by its own class:
		flat for Enhanced SR, nested otherwise. Readers accept both; this client writes the nested one.

		@input sop_class_uid (str): SOP Class UID of the document class
		@returns str: `DCMSR_LAYOUT_FLAT` or `DCMSR_LAYOUT_NESTED`
	'''
	return DCMSR_LAYOUT_FLAT if str(sop_class_uid) == DCMSR_SOP_CLASS_ENHANCED else DCMSR_LAYOUT_NESTED


# Colour codec: RGB in the model, DICOM CIELab (PS3.3 C.10.7.1.1, 0-65535) in the document. The
# formulas mirror dcmjs (`Colors.rgb2DICOMLAB` / `dicomlab2RGB`), rounding as the Viewer does.

DCMLAB_SEPARATOR = '\\'
DCMLAB_MAX = 65535


def color2dcmlab(rgb):
	'''	DICOM CIELab value of an RGB colour: RGB -> XYZ -> CIELab -> DICOM encoding, each component
		rounded and clamped to 0-65535.

		@input rgb: 8-bit RGB tuple, `#RRGGBB` string or `RGBColor`
		@returns tuple of 3 int: (L, a, b)
	'''
	from ...helpers.color import rgb2xyz, xyz2cielab, cielab2dcm
	rgb = DisplayAttributes._rgb(rgb)
	lab = cielab2dcm(xyz2cielab(rgb2xyz(RGBColor(*rgb), is_upscaled=True)))
	return tuple(min(DCMLAB_MAX, max(0, int(round(v)))) for v in lab)


def color2dcmlab_text(rgb):
	'''	Text value of a colour as written in a display-attribute TEXT item: the DICOM CIELab triple
		joined with backslashes (`L\\a\\b`).

		@input rgb: 8-bit RGB tuple, `#RRGGBB` string or `RGBColor`
		@returns str
	'''
	return DCMLAB_SEPARATOR.join(str(v) for v in color2dcmlab(rgb))


def dcmlab2color(value):
	'''	RGB colour of a DICOM CIELab triple: DICOM encoding -> CIELab -> RGB, each component clamped
		to 0-1 and rounded to 8 bits.

		@input value: `L\\a\\b` text or a sequence of three numbers

		@returns
			- RGBColor (8-bit components)
			- None when the value is not three finite numbers in 0-65535
	'''
	from ...helpers.color import dcm2cielab, cielab2rgb
	try:
		parts = value.split(DCMLAB_SEPARATOR) if isinstance(value, str) else list(value)
		lab = [float(p) for p in parts]
	except (TypeError, ValueError, AttributeError):
		return None
	if len(lab) != 3 or any(v != v or v < 0 or v > DCMLAB_MAX for v in lab):
		return None

	rgb = cielab2rgb(dcm2cielab(lab), upscale=False)
	return RGBColor(*tuple(int(round(min(1.0, max(0.0, v)) * 255)) for v in rgb))


# Image references

def image_reference_from_dataset(item, series_uid=None):
	'''	Image reference of a Referenced SOP Sequence item, of an IMAGE content item (its first
		Referenced SOP Sequence item) or of a dictionary with the same keys. An `ImageReference` is
		returned as it is. A multi-valued Referenced Frame Number contributes its first frame.

		@input item: the item
		@input series_uid (str, default=the item's Series Instance UID): series of the referenced instance

		@returns ImageReference
	'''
	from ...imaging.helpers.headers import dcm_header_value
	if isinstance(item, ImageReference):
		return item
	refs = dcm_header_value(item, DCMHEADER_SR_REF_SOP_SEQ)
	if refs:
		item = refs[0]
	frame = dcm_header_value(item, 'ReferencedFrameNumber')
	if frame is not None and not isinstance(frame, int):
		try:
			frame = int(frame[0]) if hasattr(frame, '__len__') and not isinstance(frame, str) else int(frame)
		except (TypeError, ValueError):
			frame = None
	sop_class = dcm_header_value(item, DCMHEADER_SR_SOP_CLASS_UID)
	sop_instance = dcm_header_value(item, DCMHEADER_SR_REF_INSTANCE_UID)
	return ImageReference(
		None if sop_class is None else str(sop_class), None if sop_instance is None else str(sop_instance),
		frame, series_uid or dcm_header_value(item, DCMHEADER_SERIES_INSTANCE_UID))


# Concept name the IMAGE reference carries while the content tree is assembled. highdicom requires a
# name on every item it holds; the Viewer writes none, and the name is Type 1C for IMAGE items, so
# the writers strip it before the document is built (`srcontent_strip_unnamed`).
DCMSR_IMAGE_REFERENCE_NAME = Code('260753009', 'SCT', 'Source')
_UNNAMED_MARKER = '_sonador_unnamed'


def srencode_image_reference(reference, relationship_type=RelationshipTypeValues.INFERRED_FROM):
	'''	IMAGE content item of an `ImageReference`; the frame number is written only when the reference
		carries one. The item carries the placeholder concept name `DCMSR_IMAGE_REFERENCE_NAME`, which
		`srcontent_strip_unnamed` removes once the document is built.

		@input reference (ImageReference): referenced instance; a SOP Instance UID is required
		@input relationship_type (RelationshipTypeValues, default=INFERRED FROM)

		@returns highdicom.sr.value_types.ImageContentItem
		@raises ValueError for a reference without a SOP Instance UID
	'''
	if not reference or not reference.sop_instance_uid:
		raise ValueError('An image reference with a SOP Instance UID is required to encode a finding')
	item = ImageContentItem(name=DCMSR_IMAGE_REFERENCE_NAME, referenced_sop_class_uid=reference.sop_class_uid or '',
		referenced_sop_instance_uid=reference.sop_instance_uid,
		referenced_frame_numbers=reference.frame_number, relationship_type=relationship_type)
	setattr(item, _UNNAMED_MARKER, True)
	return item


def srcontent_strip_unnamed(item):
	'''	Remove the placeholder concept name from the IMAGE items `srencode_image_reference` built, in
		the whole subtree of `item`, in place. Called by the writers once the content tree is complete.

		@input item (content item or highdicom SR document): root of the subtree
		@returns `item`
	'''
	if getattr(item, _UNNAMED_MARKER, False) and DCMHEADER_CONCEPT_CODE_SEQUENCE in item:
		delattr(item, DCMHEADER_CONCEPT_CODE_SEQUENCE)
	for child in item.get('ContentSequence', None) or []:
		srcontent_strip_unnamed(child)
	return item


# Per-finding encoder

def srencode_meta_items(finding, relationship_type=RelationshipTypeValues.HAS_PROPERTIES):
	'''	TEXT items of the extended meta attributes a finding carries (description, text, location),
		in registry order; empty attributes are left out.

		@input finding (Finding)
		@input relationship_type (RelationshipTypeValues, default=HAS PROPERTIES)

		@returns list of highdicom.sr.value_types.TextContentItem
	'''
	items = []
	for attr, concept in DCMSR_MEASUREMENT_META.items():
		value = getattr(finding, attr, None)
		if value is None or value == '':
			continue
		items.append(TextContentItem(name=concept, value=str(value), relationship_type=relationship_type))
	return items


def srencode_display_items(display, layout=DCMSR_LAYOUT_NESTED):
	'''	Content items of a finding's or measurement's display attributes: one `HAS PROPERTIES` display
		container holding a `CONTAINS` colour container holding the role items (the nested layout, the
		only one written). Preserved items are re-emitted under the container they were read from: the
		colour container's (`color_extra_items`) after the role items, the display container's
		(`extra_items`) after the colour container. A display read in the flat layout is written in
		this form.

		@input display (DisplayAttributes, colour-role dict or None): the display attributes
		@input layout (str, default=`DCMSR_LAYOUT_NESTED`): `DCMSR_LAYOUT_NESTED`

		@returns list of highdicom content items: empty for no display attributes
		@raises ValueError for any other layout
	'''
	from . import srcontent_extra_items
	if layout not in DCMSR_LAYOUTS_WRITTEN:
		raise ValueError('Unsupported display-attribute layout: %s' % layout)
	if not display:
		return []
	display = DisplayAttributes.from_color(display)
	preserved = srcontent_extra_items(display.extra_items)
	color_preserved = srcontent_extra_items(display.color_extra_items)

	color_container = ContainerContentItem(name=DCMSR_DISPLAY_COLOR, relationship_type=RelationshipTypeValues.CONTAINS)
	color_container.ContentSequence = ContentSequence([
		TextContentItem(name=DCMSR_DISPLAY_COLOR_ROLES[role], value=color2dcmlab_text(rgb),
			relationship_type=RelationshipTypeValues.CONTAINS)
		for role, rgb in display.color.items()] + color_preserved)
	display_container = ContainerContentItem(name=DCMSR_DISPLAY_ATTRIBUTES, relationship_type=RelationshipTypeValues.HAS_PROPERTIES)
	display_container.ContentSequence = ContentSequence([color_container] + preserved)
	return [display_container]


def srencode_finding_children(finding, layout=None):
	'''	Children of the Finding item of a Series Tag, in writing order: the image reference, the
		extended meta items, the nested display container, then the preserved extra items.

		@input finding (Finding)
		@input layout (str, default=`DCMSR_LAYOUT_NESTED`): `DCMSR_LAYOUT_NESTED`, the only layout written

		@returns list of highdicom content items: empty for a finding without properties
		@raises ValueError for any other layout
	'''
	from . import srcontent_extra_items
	layout = layout or DCMSR_LAYOUT_NESTED
	if layout not in DCMSR_LAYOUTS_WRITTEN:
		raise ValueError('Unsupported per-finding property layout: %s' % layout)

	children = []
	reference = getattr(finding, 'reference', None)
	if reference is not None and reference.sop_instance_uid:
		children.append(srencode_image_reference(reference))
	children.extend(srencode_meta_items(finding))
	children.extend(srencode_display_items(getattr(finding, 'display', None), layout=layout))
	children.extend(srcontent_extra_items(getattr(finding, 'extra_items', None)))
	return children


# Per-finding parser

def _path(tracking_uid, concept=None):
	'''	Content tree location written into a diagnostic: the group (by tracking UID) and, when given,
		the concept.

		@returns str
	'''
	parts = ['group %s' % tracking_uid if tracking_uid else 'group']
	if concept is not None:
		parts.append(str(concept))
	return ' / '.join(parts)


def _value_type(item):
	from ...imaging.helpers.headers import dcm_header_value
	return dcm_header_value(item, DCMHEADER_SR_VALUE_TYPE)


def _relationship(item):
	from ...imaging.helpers.headers import dcm_header_value
	return dcm_header_value(item, 'RelationshipType')


def _text_value(item):
	from ...imaging.helpers.headers import dcm_header_value
	value = dcm_header_value(item, 'TextValue')
	return None if value is None else str(value)


def _scheme_version(code):
	from ...imaging.helpers.headers import dcm_header_value
	value = dcm_header_value(code, DCMHEADER_CODING_SCHEME_VERSION)
	return None if value is None else str(value)


def srcontent_is_valid(item, diagnostics=None, strict=False, path=None):
	'''	Whether a content item can be modelled or preserved: it has a Value Type and, when the value
		type requires one, a concept name. A by-reference item is valid (it is preserved, not
		modelled). Anything else is dropped with an `error` diagnostic (`content-item-dropped`), the
		only case in which content is dropped.

		@input item (pydicom.Dataset): the content item
		@input diagnostics (list, default=None): list the diagnostic is appended to
		@input strict (bool, default=False): raise SRDiagnosticError on the `error`
		@input path (str, default=None): content tree location for the diagnostic

		@returns bool
	'''
	if srcontent_is_by_reference(item):
		return True
	value_type = _value_type(item)
	if not value_type:
		sr_diagnostic(diagnostics, SR_DIAGNOSTIC_ERROR, SR_CONDITION_ITEM_DROPPED,
			'content item without a Value Type dropped', path=path, value=item, strict=strict)
		return False
	if srcontent_name(item) is None and value_type not in DCMSR_VALUE_TYPES_OPTIONAL_NAME:
		sr_diagnostic(diagnostics, SR_DIAGNOSTIC_ERROR, SR_CONDITION_ITEM_DROPPED,
			'%s content item without a concept name dropped' % value_type, path=path, value=item, strict=strict)
		return False
	return True


def _children(item):
	'''	Content items directly under a content item.

		@returns list of pydicom.Dataset: empty when there are none
	'''
	from ...imaging.helpers.headers import dcm_header_value
	return list(dcm_header_value(item, 'ContentSequence') or [])


def _display_concept_version_check(code, diagnostics, strict, path, seen):
	'''	Record, once per finding, a display-attribute concept whose scheme version is not 0.3 (the
		`undefined-scheme-version` warning). The concept is matched by code value and designator, so
		it is read all the same, and the rewrite carries version 0.3.

		@input code (pydicom.Dataset): concept name of the item
		@input seen (dict): per-finding record of the diagnostics already raised
	'''
	version = _scheme_version(code)
	if version and version != SONADOR_SCHEME_VERSION_03 and not seen.get('scheme-version'):
		seen['scheme-version'] = True
		sr_diagnostic(diagnostics, SR_DIAGNOSTIC_WARNING, SR_CONDITION_UNDEFINED_SCHEME_VERSION,
			'display-attribute concepts carry Sonador-SR scheme version %s, which is not a defined revision; '
			'read as the registered concepts and rewritten at version %s' % (version, SONADOR_SCHEME_VERSION_03),
			path=path, value=version, strict=strict)


def _parse_color_items(items, diagnostics, strict, path, seen):
	'''	Read the colour role TEXT items among a sequence of candidate items. The first item of each
		role wins; a value that does not decode is skipped with the `invalid-display-color` warning.

		@returns tuple (OrderedDict, set, list): the colours by role (`RGBColor`), the indices of the
			items consumed, and the items that are not colour roles
	'''
	color, consumed, extra = OrderedDict(), set(), []
	for idx, item in enumerate(items):
		matched = False
		for role, concept in DCMSR_DISPLAY_COLOR_ROLES.items():
			if srcontent_is_named(item, concept) and _value_type(item) == 'TEXT':
				matched = True
				consumed.add(idx)
				_display_concept_version_check(srcontent_name(item), diagnostics, strict, path, seen)
				rgb = dcmlab2color(_text_value(item))
				if rgb is None:
					sr_diagnostic(diagnostics, SR_DIAGNOSTIC_WARNING, SR_CONDITION_INVALID_DISPLAY_COLOR,
						'invalid %s colour value skipped' % role, path=path, value=_text_value(item), strict=strict)
				elif role not in color:
					color[role] = rgb
				break
		if not matched:
			extra.append(item)
	return color, consumed, extra


def _note_missing_relationships(item, diagnostics, strict, path):
	'''	Record one `warning` (`missing-relationship-type`) per content item below `item` that lacks a
		Relationship Type, at any depth. Used by the standalone entry points (`Finding.from_sr`,
		`ReportGroup.from_sr`, `FindingCollection.from_sr`); a document parse records the same repairs
		once in its repair pass (`sonador.imaging.helpers.sr.parse.srdoc_repair`) and passes
		`repaired=True`.
	'''
	for idx, child in enumerate(_children(item)):
		if _relationship(child) is None:
			sr_diagnostic(diagnostics, SR_DIAGNOSTIC_WARNING, SR_CONDITION_MISSING_RELATIONSHIP,
				'content item without a Relationship Type; read with the relationship its parent implies',
				path='%s[%d]' % (path, idx), value=_value_type(child), strict=strict)
		_note_missing_relationships(child, diagnostics, strict, '%s[%d]' % (path, idx))


def _note_by_reference(item, diagnostics, path):
	'''	Record, as `info` (`by-reference-item`), that a preserved item is or holds a by-reference
		relationship, which prevents a rewrite of the document.

		@returns bool: whether the item is or holds one
	'''
	if srcontent_holds_by_reference(item):
		sr_diagnostic(diagnostics, SR_DIAGNOSTIC_INFO, SR_CONDITION_BY_REFERENCE_ITEM,
			'by-reference content item preserved; the document cannot be rewritten', path=path)
		return True
	return False


def _parse_display_container(container, diagnostics, strict, path, seen):
	'''	Display attributes of a `Sonador-DisplayAttributes` container: the colour roles of its `.Color`
		child. Unknown children are preserved under the container they were read from: the display
		container's as `extra_items`, the colour container's as `color_extra_items`.

		@returns DisplayAttributes
	'''
	_display_concept_version_check(srcontent_name(container), diagnostics, strict, path, seen)
	color, extra, color_extra = OrderedDict(), [], []
	for child in _children(container):
		if srcontent_is_named(child, DCMSR_DISPLAY_COLOR) and _value_type(child) == 'CONTAINER':
			_display_concept_version_check(srcontent_name(child), diagnostics, strict, path, seen)
			role_items = _children(child)
			color, _, found = _parse_color_items(role_items, diagnostics, strict, path, seen)
			for item in found:
				_note_by_reference(item, diagnostics, path)
				color_extra.append(copy.deepcopy(item))
		else:
			_note_by_reference(child, diagnostics, path)
			extra.append(copy.deepcopy(child))
	return DisplayAttributes(color=color, extra_items=extra, color_extra_items=color_extra)


def srparse_item_properties(children, group_items, consumed, group_consumed, diagnostics, strict, path, seen, owner='Finding'):
	'''	Read the extended meta and display attributes of a Finding or NUM item from the three accepted
		layouts: the item's children (nested display container or flat role items), then the legacy
		group-level siblings, which raise the `group-level-properties` warning.

		@input children (list of pydicom.Dataset): children of the item
		@input group_items (list of pydicom.Dataset): children of the group; empty to skip the legacy layout
		@input consumed (set): indices into `children` already read; those read here are added
		@input group_consumed (set): indices into `group_items` already read; those read here are added
		@input diagnostics (list): list the diagnostics are appended to
		@input strict (bool): raise SRDiagnosticError on a `warning`
		@input path (str): content tree location, for the diagnostics
		@input seen (dict): per-item record of the diagnostics already raised
		@input owner (str, default='Finding'): name of the item in the diagnostic message

		@returns tuple (dict, DisplayAttributes or None): the meta values by attribute, the display attributes
	'''
	values, display = {}, None
	group_level_used = False

	# Extended meta: children first, then the legacy group-level siblings
	for attr, concept_name in DCMSR_MEASUREMENT_META.items():
		for idx, child in enumerate(children):
			if idx not in consumed and srcontent_is_named(child, concept_name) and _value_type(child) == 'TEXT':
				consumed.add(idx)
				values[attr] = _text_value(child)
				break
		else:
			for idx, sibling in enumerate(group_items):
				if idx not in group_consumed and srcontent_is_named(sibling, concept_name) and _value_type(sibling) == 'TEXT':
					group_consumed.add(idx)
					group_level_used = True
					values[attr] = _text_value(sibling)
					break

	# Display attributes: nested container under the item, flat role items, or the legacy group-level container
	for idx, child in enumerate(children):
		if idx not in consumed and srcontent_is_named(child, DCMSR_DISPLAY_ATTRIBUTES) and _value_type(child) == 'CONTAINER':
			consumed.add(idx)
			display = _parse_display_container(child, diagnostics, strict, path, seen)
			break
	if display is None:
		candidates = [(idx, child) for idx, child in enumerate(children) if idx not in consumed]
		color, color_consumed, _ = _parse_color_items([c for _, c in candidates], diagnostics, strict, path, seen)
		if color_consumed:
			consumed.update(candidates[i][0] for i in color_consumed)
			display = DisplayAttributes(color=color)
	if display is None:
		for idx, sibling in enumerate(group_items):
			if idx not in group_consumed and srcontent_is_named(sibling, DCMSR_DISPLAY_ATTRIBUTES) and _value_type(sibling) == 'CONTAINER':
				group_consumed.add(idx)
				group_level_used = True
				display = _parse_display_container(sibling, diagnostics, strict, path, seen)
				break

	if group_level_used:
		sr_diagnostic(diagnostics, SR_DIAGNOSTIC_WARNING, SR_CONDITION_GROUP_LEVEL_PROPERTIES,
			'per-item properties read from group-level siblings (legacy layout); rewritten under the %s' % owner,
			path=path, strict=strict)
	return values, display


def srparse_finding(item, group=None, strict=False, diagnostics=None, finding_class=Finding,
		tracking_id=None, tracking_uid=None, group_consumed=None, allow_group_level=True, repaired=False, **kwargs):
	'''	Build a finding from a Finding content item (CODE) of a measurement group. The finding code
		is carried verbatim. The per-finding properties (image reference, extended meta, display
		attributes) are read from the three accepted layouts: the children of the Finding (nested
		display container or flat role items), then, when `allow_group_level` is set, the group-level
		siblings of the legacy layout, which are reported with the `group-level-properties` warning.
		Every other child is preserved as `extra_items`.

		Diagnostics raised here: `coding-scheme-version-equals-designator` (warning, the version is
		dropped), `overlength-code-value` and `overlength-code-meaning` (info, carried verbatim),
		`selected-from-image-reference` (warning, read as INFERRED FROM), `undefined-scheme-version`
		and `invalid-display-color` (warning), `by-reference-item` (info) and `content-item-dropped`
		(error).

		@input item (pydicom.Dataset): the Finding content item
		@input group (pydicom.Dataset, default=None): the measurement group holding the item; needed
			for the legacy layout
		@input strict (bool, default=False): raise SRDiagnosticError on `warning` and `error` diagnostics
		@input diagnostics (list, default=None): list the diagnostics are appended to
		@input finding_class (type, default=Finding): class instantiated
		@input tracking_id, tracking_uid (str, default=None): tracking identifier and UID of the group
		@input group_consumed (set, default=new set): indices into the group's content sequence of the
			items already consumed; the group-level siblings read here are added to it
		@input allow_group_level (bool, default=True): read the legacy group-level siblings
		@input repaired (bool, default=False): the document repair pass has already recorded the
			missing Relationship Types below the item. When False (a standalone parse of the item)
			they are recorded here, one `warning` each, before anything is read.
		@input kwargs: further constructor arguments of `finding_class`; `finding_type` overrides the
			type derived from `tracking_id`

		@returns Finding (an instance of `finding_class`)
	'''
	path = _path(tracking_uid)
	seen = {}
	group_consumed = group_consumed if group_consumed is not None else set()
	group_items = _children(group) if (group is not None and allow_group_level) else []

	# Finding code, verbatim. A version equal to the designator is a legacy defect: dropped with a
	# warning, not substituted. An empty version counts as absent.
	code = item.ConceptCodeSequence[0]
	designator = str(code.get(DCMHEADER_CODING_SCHEME_DESIGNATOR, '') or '')
	version = _scheme_version(code)
	if version and version == designator:
		sr_diagnostic(diagnostics, SR_DIAGNOSTIC_WARNING, SR_CONDITION_VERSION_EQUALS_DESIGNATOR,
			'Coding Scheme Version equal to the scheme designator "%s" dropped' % designator, path=path, value=version, strict=strict)
		version = None
	value = dcm_code_value(code)
	meaning = str(code.get(DCMHEADER_CODE_MEANING, '') or '')
	if DCMHEADER_CODE_VALUE in code and len(value or '') > DCM_CODE_VALUE_MAX_LENGTH:
		sr_diagnostic(diagnostics, SR_DIAGNOSTIC_INFO, SR_CONDITION_OVERLENGTH_CODE_VALUE,
			'CodeValue of %d characters exceeds the SH limit of %d; carried verbatim' % (len(value), DCM_CODE_VALUE_MAX_LENGTH),
			path=path, value=value)
	if len(meaning) > DCM_CODE_MEANING_MAX_LENGTH:
		sr_diagnostic(diagnostics, SR_DIAGNOSTIC_INFO, SR_CONDITION_OVERLENGTH_CODE_MEANING,
			'CodeMeaning of %d characters exceeds the LO limit of %d; carried verbatim' % (len(meaning), DCM_CODE_MEANING_MAX_LENGTH),
			path=path, value=meaning)
	concept = CodedConcept(value, designator, meaning, scheme_version=version or None)
	name = CodedConcept.from_dataset(item.ConceptNameCodeSequence[0], copy=True)

	# Children of the finding
	if not repaired:
		_note_missing_relationships(item, diagnostics, strict, path)
	children = _children(item)
	consumed = set()
	reference = None

	# Image reference: the IMAGE child; INFERRED FROM is the profile's relationship
	for idx, child in enumerate(children):
		if _value_type(child) != 'IMAGE' or reference is not None:
			continue
		consumed.add(idx)
		# A missing Relationship Type was recorded by the repair pass or by `_note_missing_relationships`
		relationship = _relationship(child)
		if relationship == RelationshipTypeValues.SELECTED_FROM.value:
			sr_diagnostic(diagnostics, SR_DIAGNOSTIC_WARNING, SR_CONDITION_SELECTED_FROM_IMAGE,
				'image reference with SELECTED FROM; read as INFERRED FROM', path=path, value=relationship, strict=strict)
		reference = image_reference_from_dataset(child)

	description_values, display = srparse_item_properties(children, group_items, consumed, group_consumed, diagnostics, strict, path, seen, owner='Finding')

	# Everything else under the finding is preserved
	extra_items = []
	for idx, child in enumerate(children):
		if idx in consumed:
			continue
		if _note_by_reference(child, diagnostics, path):
			pass
		elif not srcontent_is_valid(child, diagnostics=diagnostics, strict=strict, path=path):
			continue
		extra_items.append(copy.deepcopy(child))

	finding_type = kwargs.pop('finding_type', None) or measurement_type_for_tracking_id(tracking_id)
	return finding_class(name, concept, finding_type=finding_type, tracking_id=tracking_id, tracking_uid=tracking_uid,
		reference=reference, display=display, extra_items=extra_items, **description_values, **kwargs)


def srparse_report_group(group, strict=False, diagnostics=None, group_class=ReportGroup, finding_class=Finding,
		collection_class=FindingCollection, repaired=False, calibrations=None, **kwargs):
	'''	Build a report group from a Measurement Group content item (TID 1501): the tracking identifier
		and UID, one finding per Finding item (`srparse_finding`), the measurement of a group that is
		routed to a quantitative tool or holds NUM items under an unknown identifier
		(`sonador.apisettings.sr.measurements.srparse_measurement`), and every other child preserved
		as `extra_items`. A group with exactly one finding and no tool reads the legacy group-level
		properties as that finding's; a group with several keeps them at group level as preserved
		items; a quantitative tool's group reads them as the measurement's.

		Diagnostics raised here: `unknown-tracking-identifier` (info, the group stays generic),
		`series-tag-group-without-finding` (error, a Series Tag group without a Finding code stays
		generic), `missing-relationship-type` (warning, standalone parse only), `by-reference-item`
		(info) and `content-item-dropped` (error).

		@input group (pydicom.Dataset): Measurement Group content item
		@input strict (bool, default=False): raise SRDiagnosticError on `warning` and `error` diagnostics
		@input diagnostics (list, default=None): list the diagnostics are appended to
		@input group_class (type, default=ReportGroup): class instantiated
		@input finding_class (type, default=Finding): class of the findings
		@input collection_class (type, default=FindingCollection): class of the findings collection
		@input repaired (bool, default=False): the document repair pass has already recorded the
			missing Relationship Types below the group (see `srparse_finding`)
		@input calibrations (mapping, default=None): `ImageFrameGeometry` by SOP Instance UID or
			`(SOP Instance UID, frame number)`, used to verify image-domain stored values
		@input kwargs: further constructor arguments of `group_class`

		@returns ReportGroup (an instance of `group_class`)
	'''
	children = _children(group)
	consumed = set()
	tracking_id, tracking_uid = None, None
	for idx, child in enumerate(children):
		if srcontent_is_named(child, dcmcodes.DCM.TrackingIdentifier) and _value_type(child) == 'TEXT' and tracking_id is None:
			tracking_id = _text_value(child)
			consumed.add(idx)
		elif srcontent_is_named(child, dcmcodes.DCM.TrackingUniqueIdentifier) and _value_type(child) == 'UIDREF' and tracking_uid is None:
			from ...imaging.helpers.headers import dcm_header_value
			tracking_uid = str(dcm_header_value(child, 'UID'))
			consumed.add(idx)

	path = _path(tracking_uid)
	# A standalone parse (no document repair pass) records the missing Relationship Types of the
	# whole group once here; the findings below are then parsed as repaired
	if not repaired:
		_note_missing_relationships(group, diagnostics, strict, path)
	measurement_type = measurement_type_for_tracking_id(tracking_id)
	if tracking_id and measurement_type is None:
		sr_diagnostic(diagnostics, SR_DIAGNOSTIC_INFO, SR_CONDITION_UNKNOWN_TRACKING_ID,
			'measurement group with an unknown tracking identifier kept as a generic report group',
			path=path, value=tracking_id)

	finding_indices = [idx for idx, child in enumerate(children)
		if idx not in consumed and _value_type(child) == 'CODE' and srcontent_is_named(child, DCMSR_FINDING)
		and child.get('ConceptCodeSequence')]
	if measurement_type == SONADOR_MEASUREMENT_TYPE_SERIES_TAG and not finding_indices:
		sr_diagnostic(diagnostics, SR_DIAGNOSTIC_ERROR, SR_CONDITION_GROUP_WITHOUT_FINDING,
			'Series Tag measurement group without a Finding code kept as a generic report group', path=path, strict=strict)

	# Quantitative content: a group routed to a tool, or any NUM group with an unknown identifier. A
	# tool group owns its legacy group-level properties; otherwise a single Finding keeps them.
	measurements = []
	typed = measurement_type in SONADOR_QUANTITATIVE_TYPES
	if typed or measurement_type is None:
		from .measurements import srparse_group_measurement
		measurement = srparse_group_measurement(group, measurement_type, tracking_id, tracking_uid, consumed, strict=strict,
			diagnostics=diagnostics, calibrations=calibrations, allow_group_level=(typed or not finding_indices))
		if measurement is not None:
			measurements.append(measurement)

	findings = []
	for idx in finding_indices:
		consumed.add(idx)
		findings.append(srparse_finding(children[idx], group=group, strict=strict, diagnostics=diagnostics,
			finding_class=finding_class, tracking_id=tracking_id, tracking_uid=tracking_uid,
			group_consumed=consumed, allow_group_level=(len(finding_indices) == 1 and not (typed and measurements)), repaired=True,
			finding_type=measurement_type if measurement_type in SONADOR_FINDING_TYPES else None))

	extra_items = []
	for idx, child in enumerate(children):
		if idx in consumed:
			continue
		if _note_by_reference(child, diagnostics, path):
			pass
		elif not srcontent_is_valid(child, diagnostics=diagnostics, strict=strict, path=path):
			continue
		extra_items.append(copy.deepcopy(child))

	return group_class(findings=collection_class(findings) if findings else None,
		measurements=MeasurementCollection(measurements) if measurements else None,
		tracking_id=tracking_id, tracking_uid=tracking_uid, extra_items=extra_items, **kwargs)


def srparse_findings(group, strict=False, diagnostics=None, collection_class=FindingCollection, **kwargs):
	'''	Findings of a Measurement Group content item, parsed with `srparse_report_group`.

		@returns FindingCollection (an instance of `collection_class`): empty for a group without findings
	'''
	parsed = srparse_report_group(group, strict=strict, diagnostics=diagnostics, collection_class=collection_class, **kwargs)
	return parsed.findings if parsed.findings is not None else collection_class([])
