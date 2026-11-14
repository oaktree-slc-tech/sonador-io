'''	Sonador-SR 0.3: arrow annotations (the Cornerstone `ArrowAnnotate` tool) exchanged with the Sonador
	Viewer, specified in oak-tree/medical-imaging/sonador-client#39.

	An arrow is a text annotation anchored on the image by an arrow: a qualitative finding with geometry,
	never a measurement. It is one TID 1501 measurement group with the tracking identifier
	`cornerstoneTools@^4.0.0:ArrowAnnotate` (`Cornerstone3DTools@^0.1.0:ArrowAnnotate` is read and kept),
	a tracking UID and one Finding CODE item `(121071, DCM, "Finding")`, and no NUM item:

	* the Finding's value is the label, `(CORNERSTONEFREETEXT, CST4, <text>)`, the text verbatim at any
		length as the Code Meaning; a Finding of any other code is a coded finding, carried verbatim;
	* `INFERRED FROM` the Finding: the geometry, a SCOORD `POLYLINE` of the tip then the tail `SELECTED
		FROM` the image (image domain) or a SCOORD3D `POLYLINE` with the Frame of Reference (patient domain);
		an arrow without a tail is a `POINT` of its tip;
	* `HAS PROPERTIES` the Finding: `Sonador-Measurement.Description`, `.Text`, `.Location`, `.View`, the
		`Sonador-DisplayAttributes` container with the `Line` and `Text` colour roles, and in the patient
		domain `Sonador-Measurement.Source-Series` and `.Drawing-Plane`.

	Readers also accept the two stock forms, whose geometry sits under a NUM carrier `(111010, DCM)` with a
	zero value: the Viewer's current form (the classic adapter's TID300 Point: NUM "Center" and a one-point
	`POINT`) and the stock Cornerstone3D form (NUM "label" and a two-point `POLYLINE`). The carrier has no
	content of its own; a re-save writes the form above without it, with the `info` `arrow-legacy-geometry`.
	A `CORNERSTONEFREETEXT` Finding Site, else a group-level Comment `(121106, DCM)` (what the stock Cornerstone3D
	writer writes for a labelled annotation), gives the label of a group without a Finding. The properties are
	read in the nested, flat and legacy group-level layouts.

	Contents: the encoder (`srencode_arrow`) and the parser (`srparse_arrow`). The model is
	`sonador.apisettings.base.ArrowAnnotation`; the group router is
	`sonador.apisettings.sr.seriestag.srparse_report_group`.
'''
import copy

from highdicom.sr import QualitativeEvaluation
from highdicom.sr.templates import CodedConcept
from highdicom.sr.value_types import ContentSequence

from ..base import ArrowAnnotation, Finding, DCMSR_FINDING, DCMSR_GRAPHIC_POLYLINE, DCMSR_GRAPHIC_POINT, DCMSR_FREE_TEXT_VALUE, \
	DCMSR_FREE_TEXT_SCHEME, DCMSR_ARROW_LENGTH_TOLERANCE, DCMSR_UNITS_PIXEL, SONADOR_MEASUREMENT_TYPE_ARROW, DCMHEADER_CODE_MEANING, \
	DCMHEADER_CODING_SCHEME_DESIGNATOR, is_free_text_finding, free_text_finding
from . import dcm_code_value, srcontent_is_named, srcontent_extra_items, sr_diagnostic, SRDiagnostic, SRDiagnosticError, \
	DCM_CODE_MEANING_MAX_LENGTH, SR_DIAGNOSTIC_INFO, SR_DIAGNOSTIC_WARNING, SR_DIAGNOSTIC_ERROR, SR_CONDITION_OVERLENGTH_CODE_MEANING, \
	SR_CONDITION_ARROW_LEGACY_GEOMETRY, SR_CONDITION_ARROW_LABEL_FROM_FINDING_SITE, SR_CONDITION_ARROW_WITHOUT_LABEL, \
	SR_CONDITION_DEGENERATE_ARROW, SR_CONDITION_ARROW_READ_AS_FINDING, SR_CONDITION_MEASUREMENT_UNCALIBRATED, SR_CONDITION_ARROW_LABEL_FROM_COMMENT
from .seriestag import image_reference_from_dataset, srparse_finding, srparse_finding_code, srencode_meta_items, srencode_display_items, DCMSR_LAYOUT_NESTED, \
	DCMSR_LAYOUTS_WRITTEN, _children, _value_type, _text_value, _relationship, _path
from .measurements import srencode_measurement_views, srencode_measurement_provenance, _coordinates_item, _is_measurement_geometry, \
	_is_finding_site, _parse_coordinates, _parse_num_properties, _preserved_children, _calibration_for, _measured_value, DCMSR_COMMENT


# Geometry an arrow holds: (graphic type, number of points)
DCMSR_ARROW_GEOMETRIES = ((DCMSR_GRAPHIC_POLYLINE, 2), (DCMSR_GRAPHIC_POINT, 1))

# Concept of the NUM carrier the stock writers wrap an arrow's geometry in, with a zero value in the UCUM unit `1`
DCMSR_ARROW_CARRIER_CONCEPT = ('111010', 'DCM')
DCMSR_ARROW_CARRIER_UNIT = ('1', 'UCUM')


def _note_pixel_distance(arrow, diagnostics, path):
	'''	Record, as the `info` `measurement-uncalibrated`, that the tip-to-tail distance of an image-domain arrow
		with a tail is in pixels (its frame has no calibration). Nothing for a tip-only or calibrated arrow.

		@input arrow (ArrowAnnotation)
		@input diagnostics (list), path (str): diagnostics list and content tree location
	'''
	length = arrow.length
	if arrow.geometry.is_image and length is not None and getattr(length[1], 'value', None) == DCMSR_UNITS_PIXEL.value:
		sr_diagnostic(diagnostics, SR_DIAGNOSTIC_INFO, SR_CONDITION_MEASUREMENT_UNCALIBRATED,
			'image frame without a calibration; the tip-to-tail distance is in pixels', path=path, value=arrow.tracking_uid)


# Encoder

def srencode_arrow(arrow, sr_template=None, layout=None, diagnostics=None, strict=False, **kwargs):
	'''	Finding content item of an arrow, with its children in writing order: the geometry (`INFERRED FROM`),
		then the description, text, location, View items, Source-Series and Drawing-Plane (patient domain),
		the display container and the preserved children (`HAS PROPERTIES`). No NUM item is written: the NUM
		carrier of an arrow read in a stock form is dropped, with the `info` `arrow-legacy-geometry`. A coordinate
		item read from the document is copied with its points, Frame of Reference and image reference updated
		while it holds the same value type and graphic type. A label longer than 64 characters is written
		verbatim with the `info` `overlength-code-meaning`, and an image-domain arrow whose tip-to-tail distance is in
		pixels with the `info` `measurement-uncalibrated`. Makes no request.

		@input arrow (ArrowAnnotation)
		@input sr_template (class, default=highdicom QualitativeEvaluation): template class instantiated
		@input layout (str, default=`DCMSR_LAYOUT_NESTED`): the only layout written
		@input diagnostics (list, default=None): list the write diagnostics are appended to
		@input strict (bool, default=False): refuse an unregistered View value
		@input kwargs: other write options of the document, ignored

		@returns highdicom.sr.QualitativeEvaluation: a one-item content sequence
		@raises ValueError for an empty label (new or edited), a missing Frame of Reference or image reference,
			or a drawing plane that fails validation
		@raises SRDiagnosticError for a tip and tail closer than 0.01 mm (`degenerate-arrow`), or under `strict`
			an unregistered View value
	'''
	sr_template = sr_template or QualitativeEvaluation
	layout = layout or DCMSR_LAYOUT_NESTED
	if layout not in DCMSR_LAYOUTS_WRITTEN:
		raise ValueError('Unsupported per-item property layout: %s' % layout)
	path = _path(arrow.tracking_uid)

	label = arrow.label
	if label is not None and not label.strip() and arrow.finding_changed:
		raise ValueError('Arrow %s has an empty label; an arrow is written with a non-empty label' % arrow.tracking_uid)
	if arrow.is_degenerate:
		raise SRDiagnosticError(SRDiagnostic(SR_DIAGNOSTIC_ERROR, SR_CONDITION_DEGENERATE_ARROW, path, arrow.tracking_uid,
			'tip and tail of the arrow are closer than %s and the arrow cannot be written' % DCMSR_ARROW_LENGTH_TOLERANCE))
	meaning = str(getattr(arrow.finding, 'meaning', None) or '')
	if len(meaning) > DCM_CODE_MEANING_MAX_LENGTH:
		sr_diagnostic(diagnostics, SR_DIAGNOSTIC_INFO, SR_CONDITION_OVERLENGTH_CODE_MEANING,
			'CodeMeaning of %d characters exceeds the LO limit of %d; written verbatim' % (len(meaning), DCM_CODE_MEANING_MAX_LENGTH),
			path=path, value=meaning)
	if arrow.legacy_geometry:
		sr_diagnostic(diagnostics, SR_DIAGNOSTIC_INFO, SR_CONDITION_ARROW_LEGACY_GEOMETRY,
			'NUM carrier of the arrow geometry not written; the geometry is written under the Finding', path=path, value=arrow.tracking_uid)
	_note_pixel_distance(arrow, diagnostics, path)

	coordinates = _coordinates_item(arrow, arrow.source_coordinates, arrow.points, arrow.geometry.graphic_type)
	children = [coordinates]
	children.extend(srencode_meta_items(arrow))
	children.extend(srencode_measurement_views(arrow, diagnostics=diagnostics, strict=strict))
	children.extend(srencode_measurement_provenance(arrow))
	children.extend(srencode_display_items(arrow.display, layout=layout))
	children.extend(srcontent_extra_items(arrow.extra_items))

	evaluation = sr_template(arrow.name, arrow.finding)
	evaluation[0].ContentSequence = ContentSequence(children)
	return evaluation


# Parser

def _free_text_site(item):
	'''	Whether a content item is a free-text Finding Site: value `CORNERSTONEFREETEXT` in the `CST4` scheme.

		@input item (pydicom.Dataset): a content item
		@returns bool
	'''
	if not _is_finding_site(item) or not item.get('ConceptCodeSequence'):
		return False
	code = item.ConceptCodeSequence[0]
	return dcm_code_value(code) == DCMSR_FREE_TEXT_VALUE and code.get(DCMHEADER_CODING_SCHEME_DESIGNATOR) == DCMSR_FREE_TEXT_SCHEME


def _is_stock_carrier(item):
	'''	Whether a group child is the NUM carrier a stock writer wraps an arrow's geometry in: concept
		`(111010, DCM)` (meaning "Center" or "label"), a zero value in the UCUM unit `1` (or no Measured Value
		Sequence at all), no Numeric Value Qualifier, and a coordinate child. Any other NUM item, a qualified one
		included, has numeric meaning of its own and is preserved where it is.

		@input item (pydicom.Dataset): a child of the group
		@returns bool
	'''
	if _value_type(item) != 'NUM' or not item.get('ConceptNameCodeSequence'):
		return False
	name = item.ConceptNameCodeSequence[0]
	if (dcm_code_value(name), name.get(DCMHEADER_CODING_SCHEME_DESIGNATOR)) != DCMSR_ARROW_CARRIER_CONCEPT:
		return False
	if item.get('NumericValueQualifierCodeSequence'):
		return False
	value, unit, measured = _measured_value(item)
	if measured is not None:
		if value != 0 or unit is None or (unit.value, unit.scheme_designator) != DCMSR_ARROW_CARRIER_UNIT:
			return False
	return _first_geometry(_children(item)) is not None


def _first_geometry(children):
	'''	Index of the first coordinate child that is the geometry of its parent (`_is_measurement_geometry`).

		@input children (list of pydicom.Dataset)
		@returns int, or None
	'''
	return next((idx for idx, child in enumerate(children) if _is_measurement_geometry(child)), None)


def _source_image(item):
	'''	The image a SCOORD was drawn on: its first IMAGE child with the relationship `SELECTED FROM` (or none
		recorded, which the parent implies). An IMAGE child with any other relationship is not the source.

		@input item (pydicom.Dataset): the SCOORD item
		@returns pydicom.Dataset, or None
	'''
	return next((child for child in _children(item) if _value_type(child) == 'IMAGE'
		and _relationship(child) in (None, 'SELECTED FROM')), None)


def _group_image_reference(children, consumed):
	'''	Image reference of the first SCOORD of a group (directly in the group or under one of its NUM items), for
		the placeholder of a group without a Finding.

		@input children (list of pydicom.Dataset): children of the group
		@input consumed (set): indices already read
		@returns ImageReference, or None
	'''
	for idx, child in enumerate(children):
		if idx in consumed:
			continue
		for item in [child] + (_children(child) if _value_type(child) == 'NUM' else []):
			if _value_type(item) == 'SCOORD':
				image = _source_image(item)
				if image is not None:
					return image_reference_from_dataset(image)
	return None


def srparse_arrow(group, tracking_id, tracking_uid, consumed, strict=False, diagnostics=None, calibrations=None, allow_group_level=True):
	'''	Arrow of an `ArrowAnnotate` measurement group. The Finding is the first Finding CODE item of the group, its
		code carried verbatim (a free-text code in `CodeValue` or `LongCodeValue`); without one, a free-text Finding
		Site gives the label (`info` `arrow-label-from-finding-site`), else a group-level Comment `(121106, DCM)`, the
		stock Cornerstone3D writer's form (`info` `arrow-label-from-comment`); either is written back as a Finding,
		and a `Sonador-Measurement.Text` property stays a property of its own. The geometry is the first
		coordinate child of the Finding (the Sonador form), else that of a NUM carrier of the group (the stock
		forms, `info` `arrow-legacy-geometry`; the carrier's other children move under the Finding): a two-point
		`POLYLINE` (tip, tail) or a one-point `POINT` (tip only), SCOORD on an image or SCOORD3D in a Frame of
		Reference. The properties are read from the Finding's children in the nested and flat layouts and, when
		`allow_group_level` is set, from the legacy group-level siblings. Every other child of the Finding is
		preserved under it. Makes no request.

		The NUM carrier is recognised by concept `(111010, DCM)`, a zero value in the UCUM unit `1` and a coordinate
		child; any other NUM item is content of its own and stays in the group. A free-text label is matched by code
		value and the `CST4` scheme; a Finding of that value in another scheme is a coded finding, and a Finding Site
		in another scheme gives no label.

		A group whose Finding has no usable arrow geometry (none, another graphic type, or an image-domain item without
		a `SELECTED FROM` IMAGE naming the SOP Instance it was drawn on; an IMAGE child of another relationship is
		never taken as the source) reads as a plain `Finding` of type `arrow`, with its geometry preserved where
		it was and the `warning` `arrow-read-as-finding`. A group without a Finding, free-text Finding Site or Comment is
		listed as a plain `Finding` of type `arrow` without a value, with the same warning: it is not written, and the
		group's content is re-emitted from its preserved items.

		Diagnostics raised here: `arrow-legacy-geometry`, `arrow-label-from-finding-site`, `arrow-label-from-comment` and
		`measurement-uncalibrated`
		(info, the tip-to-tail distance of an image-domain arrow in pixels),
		`arrow-without-label` (warning, an empty or absent meaning of a free-text finding: label `''`, written
		back as read), `degenerate-arrow` (warning, tip and tail closer than 0.01 mm, or 0.01 px on an
		uncalibrated frame), `arrow-read-as-finding` (warning), and those of `srparse_finding_code`,
		`_parse_coordinates` and `_parse_num_properties`. `strict` raises on `warning` and `error`.

		@input group (pydicom.Dataset): the measurement group
		@input tracking_id, tracking_uid (str or None): tracking identifier and UID of the group
		@input consumed (set): indices into the group's content sequence already read; those read here are added
		@input strict (bool, default=False): raise SRDiagnosticError on `warning` and `error` diagnostics
		@input diagnostics (list, default=None): list the diagnostics are appended to
		@input calibrations (mapping, default=None): `ImageFrameGeometry` by SOP Instance UID or
			`(SOP Instance UID, frame number)`, used for the tip-to-tail distance of an image-domain arrow
		@input allow_group_level (bool, default=True): read the legacy group-level properties

		@returns
			- ArrowAnnotation
			- Finding (plain, type `arrow`) for a Finding without usable arrow geometry, or without a value for a
				group without a Finding, free-text Finding Site or Comment
	'''
	path = _path(tracking_uid)
	children = _children(group)
	finding_idx = next((idx for idx, child in enumerate(children) if idx not in consumed and _value_type(child) == 'CODE'
		and srcontent_is_named(child, DCMSR_FINDING) and child.get('ConceptCodeSequence')), None)
	site_idx, comment_idx = None, None
	if finding_idx is None:
		site_idx = next((idx for idx, child in enumerate(children) if idx not in consumed and _free_text_site(child)), None)
	if finding_idx is None and site_idx is None:
		comment_idx = next((idx for idx, child in enumerate(children) if idx not in consumed and _value_type(child) == 'TEXT'
			and srcontent_is_named(child, DCMSR_COMMENT)), None)

	def fallback(message):
		'''	The group read without the arrow model, with the `arrow-read-as-finding` warning: its Finding as a plain
			`Finding` of type `arrow`, or, for a group without one, a plain `Finding` of type `arrow` without a
			value, which stands for the group in `findings` and is not written (its content stays in the group's
			preserved items).

			@returns Finding
		'''
		sr_diagnostic(diagnostics, SR_DIAGNOSTIC_WARNING, SR_CONDITION_ARROW_READ_AS_FINDING,
			message + ('; read as a plain Finding, its geometry preserved' if finding_idx is not None
				else '; listed as a plain Finding without a value, its content kept in the group'),
			path=path, value=tracking_uid, strict=strict)
		if finding_idx is None:
			placeholder = Finding(CodedConcept.from_code(DCMSR_FINDING), None, finding_type=SONADOR_MEASUREMENT_TYPE_ARROW,
				tracking_id=tracking_id, tracking_uid=tracking_uid, reference=_group_image_reference(children, consumed))
			placeholder.is_group_placeholder = True
			return placeholder
		consumed.add(finding_idx)
		return srparse_finding(children[finding_idx], group=group, strict=strict, diagnostics=diagnostics, tracking_id=tracking_id,
			tracking_uid=tracking_uid, group_consumed=consumed, allow_group_level=allow_group_level, repaired=True,
			finding_type=SONADOR_MEASUREMENT_TYPE_ARROW)

	if finding_idx is None and site_idx is None and comment_idx is None:
		return fallback('arrow group without a Finding')

	# Geometry: under the Finding, else under a NUM carrier of the group
	finding_children = _children(children[finding_idx]) if finding_idx is not None else []
	position = _first_geometry(finding_children)
	carrier_idx, carrier_children, carrier_position = None, [], None
	if position is not None:
		source = finding_children[position]
	else:
		for idx, child in enumerate(children):
			if idx not in consumed and _is_stock_carrier(child):
				carrier_idx, carrier_children = idx, _children(child)
				carrier_position = _first_geometry(carrier_children)
				break
		if carrier_idx is None:
			return fallback('arrow group without geometry under its Finding or a NUM carrier')
		source = carrier_children[carrier_position]
	geometry, frame_uid = _parse_coordinates(source, diagnostics, strict, path)
	if geometry is None:
		return fallback('arrow geometry is not usable')
	if (geometry.graphic_type, len(geometry.points)) not in DCMSR_ARROW_GEOMETRIES:
		return fallback('%s of %d points is not the geometry of an arrow' % (geometry.graphic_type, len(geometry.points)))
	if geometry.is_image:
		image = _source_image(source)
		reference = image_reference_from_dataset(image) if image is not None else None
		if reference is None or not reference.sop_instance_uid:
			return fallback('image-domain arrow geometry without a SELECTED FROM image naming the SOP Instance it was drawn on')
		geometry = geometry._replace(reference=reference)

	# The Finding code (or the free-text Finding Site's)
	if finding_idx is not None:
		consumed.add(finding_idx)
		name, concept = srparse_finding_code(children[finding_idx], diagnostics, strict, path)
	elif site_idx is not None:
		consumed.add(site_idx)
		name = CodedConcept.from_code(DCMSR_FINDING)
		concept = CodedConcept.from_dataset(children[site_idx].ConceptCodeSequence[0], copy=True)
		sr_diagnostic(diagnostics, SR_DIAGNOSTIC_INFO, SR_CONDITION_ARROW_LABEL_FROM_FINDING_SITE,
			'label read from a CORNERSTONEFREETEXT Finding Site; written back as the Finding', path=path, value=concept.meaning)
	else:
		consumed.add(comment_idx)
		name = CodedConcept.from_code(DCMSR_FINDING)
		concept = CodedConcept.from_code(free_text_finding(_text_value(children[comment_idx]) or ''))
		sr_diagnostic(diagnostics, SR_DIAGNOSTIC_INFO, SR_CONDITION_ARROW_LABEL_FROM_COMMENT,
			'label read from a group-level Comment (the stock Cornerstone3D form); written back as the Finding', path=path,
			value=concept.meaning)
	if is_free_text_finding(concept) and not (concept.meaning or '').strip():
		sr_diagnostic(diagnostics, SR_DIAGNOSTIC_WARNING, SR_CONDITION_ARROW_WITHOUT_LABEL,
			'free-text Finding with an empty meaning; read with an empty label and written back as read', path=path, value=tracking_uid,
			strict=strict)

	# The Finding's properties, then the legacy group level; everything else under the Finding is preserved
	finding_consumed = {position} if position is not None else set()
	properties = _parse_num_properties(finding_children, finding_consumed, children if allow_group_level else [], consumed, diagnostics,
		strict, path, geometry, tracking_uid, foreign_labels=False, owner='Finding')
	meta, display, views, source_series, plane, plane_text, _, extra_items = properties

	legacy = carrier_idx is not None
	if legacy:
		consumed.add(carrier_idx)
		moved = _preserved_children(carrier_children, {carrier_position}, diagnostics, strict, path)
		extra_items = list(extra_items) + moved
		sr_diagnostic(diagnostics, SR_DIAGNOSTIC_INFO, SR_CONDITION_ARROW_LEGACY_GEOMETRY,
			'arrow geometry read from a NUM carrier (%s); written back under the Finding without the carrier%s'
			% (str(children[carrier_idx].ConceptNameCodeSequence[0].get(DCMHEADER_CODE_MEANING, '') or '')
				if children[carrier_idx].get('ConceptNameCodeSequence') else 'unnamed',
				', its %d other children moved under the Finding' % len(moved) if moved else ''),
			path=path, value=tracking_uid)

	arrow = ArrowAnnotation(name, concept, geometry=geometry, frame_of_reference_uid=frame_uid,
		calibration=_calibration_for(calibrations, geometry.reference), views=views, tracking_id=tracking_id, tracking_uid=tracking_uid,
		series_uid=source_series, display=display, extra_items=extra_items, **meta)
	if plane_text is not None:
		arrow._set_parsed_drawing_plane(plane, plane_text)
	arrow._mark_parsed(copy.deepcopy(source), legacy=legacy)
	_note_pixel_distance(arrow, diagnostics, path)
	if arrow.is_degenerate:
		sr_diagnostic(diagnostics, SR_DIAGNOSTIC_WARNING, SR_CONDITION_DEGENERATE_ARROW,
			'tip and tail closer than %s %s; kept as read, but refused on write' % (DCMSR_ARROW_LENGTH_TOLERANCE, arrow.length[1].meaning),
			path=path, value=tracking_uid, strict=strict)
	return arrow
