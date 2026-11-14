'''	Offline tests of the arrow annotation (Cornerstone `ArrowAnnotate`) at Sonador-SR 0.3, and of the
	character-set rule of the writers.

	An arrow is a Finding with geometry: one TID 1501 group holding one Finding CODE item whose value is the
	label `(CORNERSTONEFREETEXT, CST4, <text>)` (or a coded finding, carried verbatim), `INFERRED FROM` a
	`POLYLINE` of the tip then the tail (a `POINT` of the tip for an arrow without a tail), SCOORD `SELECTED
	FROM` the image or SCOORD3D with the Frame of Reference, and the per-annotation properties as `HAS
	PROPERTIES` children of the Finding. No NUM item is written. Readers accept the Viewer's current form
	(NUM "Center" and a one-point `POINT`) and the stock Cornerstone3D form (NUM "label" and a two-point
	`POLYLINE`) as well.

	Fixtures (`ftests/test_sr/fixtures`, synthetic study): `sonador-io.arrow-image`, `arrow-axial` and
	`arrow-oblique`, each `.2026-1114.dcm`, written by this suite (`UPDATE_SR_FIXTURES=1`).
	`cornerstone-adapters.arrow-image.2026-1114.dcm` and `cornerstone-adapters.arrow-patient.2026-1114.dcm` are
	written by the stock `@cornerstonejs/adapters` 4.22.13 Cornerstone3D `ArrowAnnotate` writer and
	`cornerstone-adapters.arrow-point.2026-1114.dcm` by its classic `ArrowAnnotate` writer (TID300 Point, the form
	the Viewer writes today), from synthetic annotation state, by the jest harness in
	`fixtures/generators/cornerstone-adapters`. The Viewer-written `sonador-viewer.arrow-image` and
	`sonador-viewer.arrow-oblique` fixtures are tested once they are added.

	Run from the directory containing the `sonador` package: python -m unittest sonador.ftests.test_sr.sr_arrow
'''
import copy, glob, logging, os, unittest
from collections import OrderedDict

import numpy as np

from pydicom import Dataset
from pydicom.sequence import Sequence

from highdicom.sr.templates import Code

from ...apisettings.base import ArrowAnnotation, Finding, Geometry, ImageTransformMatrix, free_text_finding, SONADOR_MEASUREMENT_TYPE_ARROW, \
	SONADOR_MEASUREMENT_TYPE_SERIES_TAG, DCMSR_FINDING, DCMSR_FREE_TEXT_VALUE, DCMSR_FREE_TEXT_SCHEME
from ...apisettings.display_attrs import DisplayAttributes
from ...apisettings.sr import dcm_code_value, SRDiagnosticError, sr_diagnostics_with, SR_DIAGNOSTIC_INFO, SR_DIAGNOSTIC_WARNING, \
	SR_CONDITION_ARROW_LEGACY_GEOMETRY, SR_CONDITION_ARROW_LABEL_FROM_FINDING_SITE, SR_CONDITION_ARROW_WITHOUT_LABEL, \
	SR_CONDITION_DEGENERATE_ARROW, SR_CONDITION_ARROW_READ_AS_FINDING, SR_CONDITION_OVERLENGTH_CODE_MEANING, \
	SR_CONDITION_INVALID_DRAWING_PLANE, SR_CONDITION_GROUP_LEVEL_PROPERTIES, SR_CONDITION_PROVENANCE_ON_IMAGE_DOMAIN, \
	SR_CONDITION_MEASUREMENT_UNCALIBRATED, SR_CONDITION_ARROW_LABEL_FROM_COMMENT
from ...apisettings.sr.seriestag import measurement_type_for_tracking_id, tracking_id_for_measurement_type, DCMSR_MEASUREMENT_DESCRIPTION, \
	DCMSR_MEASUREMENT_TEXT, DCMSR_MEASUREMENT_LOCATION, DCMSR_DISPLAY_ATTRIBUTES, DCMSR_DISPLAY_COLOR_ROLES, DCMSR_SOP_CLASS_COMPREHENSIVE_3D
from ...apisettings.sr.measurements import DCMSR_MEASUREMENT_VIEW, DCMSR_MEASUREMENT_SOURCE_SERIES, DCMSR_MEASUREMENT_DRAWING_PLANE
from ...imaging.helpers.sr import SonadorImagingMeasurementReport, srdoc_apply_character_set, srdoc_has_non_ascii_text, \
	DCM_CHARACTER_SET_UTF8
from ...imaging.helpers.sr.parse import srdoc_children, srdoc_measurement_groups
from ...imaging.orthanc.sr import dcmsr_local_series
from .sr_seriestag import read, roundtrip, tobytes, new_model, synthetic_reference, named, io_findings, finding_item, SYNTHETIC, FIXTURES
from .sr_measurements import FOR_UID, CALIBRATION, UNCALIBRATED, reparse, measurements_of, group_of, text_item, length_patient, \
	length_image, angle_patient, OBLIQUE_WIDTH, OBLIQUE_HEIGHT, OBLIQUE_ORIGIN, OBLIQUE_TEXT
from .sr_circle import OracleMixin, coordinates_of, circle_axial, AXIAL, LINE_TEXT
from .sr_bidirectional import bidirectional_axial, bidirectional_image, VENDOR_NOTE, property_item


__all__ = ['ArrowRegistryTestCase', 'ArrowModelTestCase', 'ArrowWriterTestCase', 'ArrowReaderTestCase', 'ArrowLabelTestCase',
	'ArrowLegacyFormTestCase', 'ArrowFallbackTestCase', 'ArrowCarrierTestCase', 'ArrowEditTestCase', 'ArrowOutOfPlaneTestCase', 'ArrowFixtureTestCase',
	'ArrowAdapterFixtureTestCase', 'ArrowCommentLabelTestCase', 'ArrowViewerFixtureTestCase', 'ArrowDocumentTestCase', 'CharacterSetTestCase']


TRACKING = '2.25.9981'
ARROW_ID = 'cornerstoneTools@^4.0.0:ArrowAnnotate'
ARROW_ID_3D = 'Cornerstone3DTools@^0.1.0:ArrowAnnotate'
FRAMES = {SYNTHETIC['SOPInstanceUID']: CALIBRATION}

# Image-domain arrow on the synthetic frame (0.5 mm between columns, 2 mm between rows): tip, then tail
IMAGE_TIP, IMAGE_TAIL = (30.0, 20.0), (10.0, 40.0)
# Patient-domain arrow in the axial plane z = 30
AXIAL_TIP, AXIAL_TAIL = (10.0, 20.0, 30.0), (25.0, 35.0, 30.0)
NON_ASCII_LABEL = 'Flèche → lésion étendue'
NON_ASCII_DESCRIPTION = 'Lésion hypoéchogène, contrôle à 6 mois'
LESION = Code('52988006', 'SCT', 'Lesion')


def setUpModule():
	logging.getLogger('sonador').setLevel(logging.CRITICAL)


def tearDownModule():
	logging.getLogger('sonador').setLevel(logging.NOTSET)


def arrow_image(tip=IMAGE_TIP, tail=IMAGE_TAIL, label='Synthetic arrow', **kwargs):
	kwargs.setdefault('calibration', CALIBRATION)
	kwargs.setdefault('tracking_uid', TRACKING)
	return ArrowAnnotation.from_points(tip, tail, label, reference=synthetic_reference(), **kwargs)


def arrow_axial(tip=AXIAL_TIP, tail=AXIAL_TAIL, label='Synthetic arrow', **kwargs):
	kwargs.setdefault('frame_of_reference_uid', FOR_UID)
	kwargs.setdefault('tracking_uid', TRACKING)
	return ArrowAnnotation.from_points(tip, tail, label, **kwargs)


def written(findings, measurements=None, **kwargs):
	'''	Serialised document of a new report holding the findings (what the other side sees)
	'''
	return roundtrip(new_model(list(findings), measurements=measurements).create_sr(record_instances=True, **kwargs))


def arrows_of(model):
	return [f for g in (model.groups or []) for f in (g.findings or []) if f.type == SONADOR_MEASUREMENT_TYPE_ARROW]


def finding_of(group):
	return finding_item(group)


def children_named(item, concept):
	return named(srdoc_children(item), concept.value)


def graphic(item):
	'''	(value type, graphic type, points) of a coordinate item
	'''
	dimension = 3 if item.ValueType == 'SCOORD3D' else 2
	data = [float(v) for v in item.GraphicData]
	return item.ValueType, str(item.GraphicType), tuple(tuple(data[i:i + dimension]) for i in range(0, len(data), dimension))


def free_text_site(label):
	'''	A group-level CORNERSTONEFREETEXT Finding Site item, as dcmjs writes one
	'''
	item = Dataset()
	item.RelationshipType = 'HAS CONCEPT MOD'
	item.ValueType = 'CODE'
	name, value = Dataset(), Dataset()
	name.CodeValue, name.CodingSchemeDesignator, name.CodeMeaning = '363698007', 'SCT', 'Finding Site'
	value.CodeValue, value.CodingSchemeDesignator, value.CodeMeaning = DCMSR_FREE_TEXT_VALUE, DCMSR_FREE_TEXT_SCHEME, label
	item.ConceptNameCodeSequence = Sequence([name])
	item.ConceptCodeSequence = Sequence([value])
	return item


def legacy_expected(source):
	'''	The source document after the rewrite a re-save makes of a stock arrow: the NUM carrier removed, its
		geometry moved under the Finding
	'''
	expected = copy.deepcopy(source)
	for group in srdoc_measurement_groups(expected):
		children = list(group.ContentSequence)
		carrier = next(c for c in children if c.get('ValueType') == 'NUM')
		geometry = coordinates_of(carrier)[0]
		finding = finding_of(group)
		finding.ContentSequence = Sequence(list(finding.get('ContentSequence') or []) + [geometry])
		group.ContentSequence = Sequence([c for c in children if c is not carrier])
	return expected


class ArrowRegistryTestCase(unittest.TestCase):
	def test_identifiers_route_to_the_arrow(self):
		self.assertEqual(tracking_id_for_measurement_type(SONADOR_MEASUREMENT_TYPE_ARROW), ARROW_ID)
		for identifier in (ARROW_ID, ARROW_ID_3D):
			self.assertEqual(measurement_type_for_tracking_id(identifier), SONADOR_MEASUREMENT_TYPE_ARROW)

	def test_free_text_code(self):
		code = free_text_finding('Note')
		self.assertEqual((code.value, code.scheme_designator, code.meaning), ('CORNERSTONEFREETEXT', 'CST4', 'Note'))
		self.assertEqual((DCMSR_FINDING.value, DCMSR_FINDING.scheme_designator), ('121071', 'DCM'))


class ArrowModelTestCase(unittest.TestCase):
	def test_image_domain(self):
		a = arrow_image()
		self.assertIsInstance(a, Finding)
		self.assertEqual((a.type, a.tracking_id, a.label, a.is_coded), (SONADOR_MEASUREMENT_TYPE_ARROW, ARROW_ID, 'Synthetic arrow', False))
		self.assertEqual((a.tip, a.tail), (IMAGE_TIP, IMAGE_TAIL))
		self.assertEqual((a.geometry.domain, a.geometry.graphic_type), ('image', 'POLYLINE'))
		self.assertEqual((a.reference.sop_instance_uid, a.series_uid), (SYNTHETIC['SOPInstanceUID'], SYNTHETIC['SeriesInstanceUID']))
		self.assertEqual((a.finding.value, a.finding.scheme_designator, a.finding.meaning), ('CORNERSTONEFREETEXT', 'CST4', 'Synthetic arrow'))
		# 20 columns of 0.5 mm and 20 rows of 2 mm
		length, unit = a.length
		self.assertAlmostEqual(length, float(np.hypot(10.0, 40.0)))
		self.assertEqual(unit.value, 'mm')
		self.assertTrue(a.has_group_identity)
		self.assertFalse(a.is_series_tag)

	def test_patient_domain(self):
		a = arrow_axial(series_uid=SYNTHETIC['SeriesInstanceUID'], drawing_plane=AXIAL, views=['axial'])
		self.assertEqual((a.geometry.domain, a.frame_of_reference_uid, a.series_uid, a.reference), ('patient', FOR_UID, SYNTHETIC['SeriesInstanceUID'], None))
		self.assertEqual((a.tip, a.tail, a.views), (AXIAL_TIP, AXIAL_TAIL, ('axial',)))
		self.assertEqual(a.drawing_plane, AXIAL)
		self.assertAlmostEqual(a.length[0], float(np.hypot(15.0, 15.0)))

	def test_tip_only(self):
		a = arrow_axial(tail=None)
		self.assertEqual((a.tail, a.geometry.graphic_type, len(a.points), a.length, a.is_degenerate), (None, 'POINT', 1, None, False))

	def test_coded_finding(self):
		a = arrow_axial(label=LESION)
		self.assertEqual((a.label, a.is_coded, a.finding.value), (None, True, '52988006'))
		a.label = 'Now free text'
		self.assertEqual((a.finding.value, a.finding.meaning, a.label), ('CORNERSTONEFREETEXT', 'Now free text', 'Now free text'))
		with self.assertRaises(ValueError):
			a.label = None

	def test_uncalibrated_image_length_in_pixels(self):
		a = arrow_image(calibration=UNCALIBRATED)
		length, unit = a.length
		self.assertAlmostEqual(length, float(np.hypot(20.0, 20.0)))
		self.assertEqual(unit.value, '1')

	def test_construction_refusals(self):
		with self.assertRaises(ValueError):
			ArrowAnnotation.from_points(IMAGE_TIP, AXIAL_TAIL, 'Mixed', frame_of_reference_uid=FOR_UID)
		with self.assertRaises(ValueError):
			arrow_image(drawing_plane=AXIAL)
		with self.assertRaises(ValueError):
			ArrowAnnotation(DCMSR_FINDING, free_text_finding('x'), geometry=Geometry('image', 'POLYGON', [(0, 0), (1, 0), (1, 1)]))
		with self.assertRaises(ValueError):
			ArrowAnnotation(DCMSR_FINDING, None, geometry=Geometry('image', 'POINT', [(0, 0)]))

	def test_rigid_transform_converts_both_points_and_rotates_the_plane(self):
		# A rotation of 90 degrees about z, then a translation of (5, 0, 0)
		matrix = [[0.0, -1.0, 0.0, 5.0], [1.0, 0.0, 0.0, 0.0], [0.0, 0.0, 1.0, 0.0], [0.0, 0.0, 0.0, 1.0]]
		transform = ImageTransformMatrix(matrix, source_frame='planning', target_frame=FOR_UID)
		a = ArrowAnnotation.from_points((0.0, 0.0, 0.0), (10.0, 0.0, 0.0), 'Moved', transform=transform, drawing_plane=AXIAL)
		np.testing.assert_allclose(a.tip, (5.0, 0.0, 0.0), atol=1e-12)
		np.testing.assert_allclose(a.tail, (5.0, 10.0, 0.0), atol=1e-12)
		np.testing.assert_allclose(a.drawing_plane, ((0.0, 1.0, 0.0), (-1.0, 0.0, 0.0)), atol=1e-12)
		self.assertEqual(a.frame_of_reference_uid, FOR_UID)

	def test_non_rigid_transform_refused(self):
		scaled = ImageTransformMatrix([[2.0, 0, 0, 0], [0, 1.0, 0, 0], [0, 0, 1.0, 0], [0, 0, 0, 1.0]], source_frame='planning', target_frame=FOR_UID)
		with self.assertRaises(ValueError) as ctx:
			ArrowAnnotation.from_points(AXIAL_TIP, AXIAL_TAIL, 'Scaled', transform=scaled)
		self.assertIn('orthonormal rotation', str(ctx.exception))
		with self.assertRaises(ValueError):
			ArrowAnnotation.from_points(IMAGE_TIP, IMAGE_TAIL, 'Image', transform=ImageTransformMatrix(np.identity(4), source_frame='planning'))

	def test_degenerate(self):
		a = arrow_axial(tail=(10.005, 20.0, 30.0))
		self.assertTrue(a.is_degenerate)
		self.assertFalse(arrow_axial(tail=(10.02, 20.0, 30.0)).is_degenerate)
		# Uncalibrated frame: 0.01 px
		self.assertTrue(arrow_image(tail=(30.005, 20.0), calibration=None).is_degenerate)


class ArrowWriterTestCase(unittest.TestCase):
	def test_image_domain_layout(self):
		doc = written([arrow_image(description='Desc', text='Note', location='Liver', views=['axial'], display=LINE_TEXT)])
		self.assertEqual(str(doc.SOPClassUID), DCMSR_SOP_CLASS_COMPREHENSIVE_3D)
		group = group_of(doc)
		self.assertEqual([c.ValueType for c in srdoc_children(group)], ['TEXT', 'UIDREF', 'CODE'])
		self.assertEqual((srdoc_children(group)[0].TextValue, srdoc_children(group)[1].UID), (ARROW_ID, TRACKING))
		finding = finding_of(group)
		self.assertEqual(finding.RelationshipType, 'CONTAINS')
		value = finding.ConceptCodeSequence[0]
		self.assertEqual((dcm_code_value(value), value.CodingSchemeDesignator, value.CodeMeaning), ('CORNERSTONEFREETEXT', 'CST4', 'Synthetic arrow'))
		(scoord,) = coordinates_of(finding)
		self.assertEqual((scoord.RelationshipType, scoord.ValueType, scoord.GraphicType), ('INFERRED FROM', 'SCOORD', 'POLYLINE'))
		self.assertEqual(graphic(scoord)[2], (IMAGE_TIP, IMAGE_TAIL))
		self.assertNotIn('ConceptNameCodeSequence', scoord)
		(image,) = srdoc_children(scoord)
		self.assertEqual((image.RelationshipType, image.ValueType, image.ReferencedSOPSequence[0].ReferencedSOPInstanceUID),
			('SELECTED FROM', 'IMAGE', SYNTHETIC['SOPInstanceUID']))
		properties = [c for c in srdoc_children(finding) if c.RelationshipType == 'HAS PROPERTIES']
		self.assertEqual([dcm_code_value(c.ConceptNameCodeSequence[0]) for c in properties], [DCMSR_MEASUREMENT_DESCRIPTION.value,
			DCMSR_MEASUREMENT_TEXT.value, DCMSR_MEASUREMENT_LOCATION.value, DCMSR_MEASUREMENT_VIEW.value, DCMSR_DISPLAY_ATTRIBUTES.value])
		self.assertEqual([c.TextValue for c in properties[:4]], ['Desc', 'Note', 'Liver', 'axial'])
		roles = [dcm_code_value(c.ConceptNameCodeSequence[0]) for c in srdoc_children(srdoc_children(properties[4])[0])]
		self.assertEqual(roles, [DCMSR_DISPLAY_COLOR_ROLES['Line'].value, DCMSR_DISPLAY_COLOR_ROLES['Text'].value])
		# No NUM item anywhere in the group; the instance is in the evidence and the Image Library
		self.assertFalse([c for c in srdoc_children(group) if c.ValueType == 'NUM'])
		self.assertEqual(doc.CurrentRequestedProcedureEvidenceSequence[0].ReferencedSeriesSequence[0].ReferencedSOPSequence[0].ReferencedSOPInstanceUID,
			SYNTHETIC['SOPInstanceUID'])

	def test_patient_domain_layout(self):
		doc = written([arrow_axial(views=['axial'], series_uid=SYNTHETIC['SeriesInstanceUID'], drawing_plane=AXIAL)])
		finding = finding_of(group_of(doc))
		(scoord,) = coordinates_of(finding)
		self.assertEqual((scoord.RelationshipType, scoord.ValueType, scoord.GraphicType, scoord.ReferencedFrameOfReferenceUID),
			('INFERRED FROM', 'SCOORD3D', 'POLYLINE', FOR_UID))
		self.assertEqual(graphic(scoord)[2], (AXIAL_TIP, AXIAL_TAIL))
		self.assertEqual([c.UID for c in children_named(finding, DCMSR_MEASUREMENT_SOURCE_SERIES)], [SYNTHETIC['SeriesInstanceUID']])
		self.assertEqual([c.TextValue for c in children_named(finding, DCMSR_MEASUREMENT_DRAWING_PLANE)], ['1,0,0,0,1,0'])
		self.assertEqual([c.TextValue for c in children_named(finding, DCMSR_MEASUREMENT_VIEW)], ['axial'])

	def test_tracking_uid_generated_when_absent(self):
		a = arrow_axial(tracking_uid=None)
		doc = written([a])
		self.assertTrue(a.tracking_uid)
		self.assertEqual(srdoc_children(group_of(doc))[1].UID, a.tracking_uid)

	def test_image_domain_provenance_not_written(self):
		finding = finding_of(group_of(written([arrow_image(series_uid=SYNTHETIC['SeriesInstanceUID'])])))
		self.assertFalse(children_named(finding, DCMSR_MEASUREMENT_SOURCE_SERIES) + children_named(finding, DCMSR_MEASUREMENT_DRAWING_PLANE))

	def test_text_independent_of_label(self):
		finding = finding_of(group_of(written([arrow_image(label='Label', text='Text', description='Description', location='Location')])))
		self.assertEqual(finding.ConceptCodeSequence[0].CodeMeaning, 'Label')
		self.assertEqual([c.TextValue for c in children_named(finding, DCMSR_MEASUREMENT_TEXT)], ['Text'])
		without = finding_of(group_of(written([arrow_image(label='Label')])))
		self.assertFalse(children_named(without, DCMSR_MEASUREMENT_TEXT))

	def test_degenerate_refused(self):
		for arrow in (arrow_axial(tail=(10.005, 20.0, 30.0)), arrow_image(tail=(30.005, 20.0), calibration=None)):
			with self.assertRaises(SRDiagnosticError) as ctx:
				written([arrow])
			self.assertEqual(ctx.exception.diagnostic.condition, SR_CONDITION_DEGENERATE_ARROW)

	def test_tip_only_written_as_point(self):
		(scoord,) = coordinates_of(finding_of(group_of(written([arrow_axial(tail=None)]))))
		self.assertEqual(graphic(scoord), ('SCOORD3D', 'POINT', (AXIAL_TIP,)))


class ArrowReaderTestCase(OracleMixin, unittest.TestCase):
	def test_round_trip_both_domains(self):
		doc = written([arrow_image(description='Image', display=LINE_TEXT, views=['axial'], tracking_uid='2.25.9982'),
			arrow_axial(series_uid=SYNTHETIC['SeriesInstanceUID'], drawing_plane=AXIAL, text='Patient', tracking_uid='2.25.9983')])
		model = reparse(doc, strict=True, calibrations=FRAMES)
		self.assertFalse(model.diagnostics)
		image, patient = arrows_of(model)
		self.assertIsInstance(image, ArrowAnnotation)
		self.assertEqual((image.tip, image.tail, image.description, image.views, dict(image.display.color)), (IMAGE_TIP, IMAGE_TAIL, 'Image', ('axial',), LINE_TEXT))
		self.assertEqual((image.reference.sop_instance_uid, image.series_uid, image.tracking_uid), (SYNTHETIC['SOPInstanceUID'], SYNTHETIC['SeriesInstanceUID'], '2.25.9982'))
		self.assertEqual(image.calibration, CALIBRATION)
		self.assertEqual((patient.tip, patient.tail, patient.frame_of_reference_uid, patient.series_uid, patient.drawing_plane_text, patient.text),
			(AXIAL_TIP, AXIAL_TAIL, FOR_UID, SYNTHETIC['SeriesInstanceUID'], '1,0,0,0,1,0', 'Patient'))
		self.assertFalse(image.legacy_geometry or patient.legacy_geometry)
		self.assertFalse(measurements_of(model))
		self.assert_same_content(doc, roundtrip(model.create_sr()))

	def test_tip_and_tail_order(self):
		model = reparse(written([arrow_axial(tip=AXIAL_TAIL, tail=AXIAL_TIP)]))
		(a,) = arrows_of(model)
		self.assertEqual((a.tip, a.tail), (AXIAL_TAIL, AXIAL_TIP))
		self.assertEqual(graphic(coordinates_of(finding_of(group_of(roundtrip(model.create_sr()))))[0])[2], (AXIAL_TAIL, AXIAL_TIP))

	def test_unknown_children_preserved(self):
		doc = written([arrow_image()])
		finding = finding_of(group_of(doc))
		finding.ContentSequence.append(property_item(VENDOR_NOTE, 'kept'))
		model = reparse(doc)
		(a,) = arrows_of(model)
		self.assertEqual([dcm_code_value(i.ConceptNameCodeSequence[0]) for i in a.extra_items], [VENDOR_NOTE.value])
		again = roundtrip(model.create_sr())
		self.assertEqual([c.TextValue for c in named(srdoc_children(finding_of(group_of(again))), VENDOR_NOTE.value)], ['kept'])
		self.assert_same_content(doc, again)

	def test_flat_layout(self):
		doc = written([arrow_image(display=LINE_TEXT)])
		finding = finding_of(group_of(doc))
		container = children_named(finding, DCMSR_DISPLAY_ATTRIBUTES)[0]
		roles = list(srdoc_children(srdoc_children(container)[0]))
		for role in roles:
			role.RelationshipType = 'HAS PROPERTIES'
		finding.ContentSequence = Sequence([c for c in finding.ContentSequence if c is not container] + roles)
		model = reparse(doc, strict=True)
		self.assertEqual(dict(arrows_of(model)[0].display.color), LINE_TEXT)
		again = finding_of(group_of(roundtrip(model.create_sr())))
		self.assertEqual(len(children_named(again, DCMSR_DISPLAY_ATTRIBUTES)), 1)

	def test_legacy_group_level_layout(self):
		doc = written([arrow_image(description='Group level', views=['axial'])])
		group = group_of(doc)
		finding = finding_of(group)
		moved = children_named(finding, DCMSR_MEASUREMENT_DESCRIPTION) + children_named(finding, DCMSR_MEASUREMENT_VIEW)
		finding.ContentSequence = Sequence([c for c in finding.ContentSequence if all(c is not m for m in moved)])
		for item in moved:
			item.RelationshipType = 'CONTAINS'
		group.ContentSequence = Sequence(list(group.ContentSequence) + moved)
		model = reparse(doc)
		(a,) = arrows_of(model)
		self.assertEqual((a.description, a.views), ('Group level', ('axial',)))
		self.assertEqual([d.severity for d in sr_diagnostics_with(model.diagnostics, condition=SR_CONDITION_GROUP_LEVEL_PROPERTIES)], ['warning'])
		again = group_of(roundtrip(model.create_sr()))
		self.assertEqual([c.TextValue for c in children_named(finding_of(again), DCMSR_MEASUREMENT_DESCRIPTION)], ['Group level'])
		self.assertFalse(named(srdoc_children(again), DCMSR_MEASUREMENT_DESCRIPTION.value))

	def test_provenance_under_an_image_domain_finding_preserved(self):
		doc = written([arrow_image()])
		finding = finding_of(group_of(doc))
		series = Dataset()
		series.RelationshipType, series.ValueType, series.UID = 'HAS PROPERTIES', 'UIDREF', SYNTHETIC['SeriesInstanceUID']
		name = Dataset()
		name.CodeValue, name.CodingSchemeDesignator, name.CodeMeaning = DCMSR_MEASUREMENT_SOURCE_SERIES.value, 'Sonador-SR', 'Source series'
		series.ConceptNameCodeSequence = Sequence([name])
		finding.ContentSequence.append(series)
		model = reparse(doc)
		self.assertEqual([d.severity for d in sr_diagnostics_with(model.diagnostics, condition=SR_CONDITION_PROVENANCE_ON_IMAGE_DOMAIN)], ['info'])
		self.assertEqual(len(arrows_of(model)[0].extra_items), 1)
		self.assert_same_content(doc, roundtrip(model.create_sr()))

	def test_degenerate_read_with_warning(self):
		doc = written([arrow_axial()])
		coordinates_of(finding_of(group_of(doc)))[0].GraphicData = list(AXIAL_TIP) + [AXIAL_TIP[0] + 0.001, AXIAL_TIP[1], AXIAL_TIP[2]]
		model = reparse(doc)
		self.assertTrue(arrows_of(model)[0].is_degenerate)
		self.assertEqual([d.severity for d in sr_diagnostics_with(model.diagnostics, condition=SR_CONDITION_DEGENERATE_ARROW)], ['warning'])
		with self.assertRaises(SRDiagnosticError):
			reparse(doc, strict=True)
		with self.assertRaises(SRDiagnosticError) as ctx:
			model.create_sr()
		self.assertEqual(ctx.exception.diagnostic.condition, SR_CONDITION_DEGENERATE_ARROW)

	def test_pixel_distance_reported(self):
		doc = written([arrow_image(tracking_uid='2.25.9971'), arrow_image(tail=None, tracking_uid='2.25.9972'), arrow_axial(tracking_uid='2.25.9973')])
		uncalibrated = sr_diagnostics_with(reparse(doc).diagnostics, condition=SR_CONDITION_MEASUREMENT_UNCALIBRATED)
		self.assertEqual([(d.severity, d.value) for d in uncalibrated], [('info', '2.25.9971')])
		self.assertFalse(sr_diagnostics_with(reparse(doc, calibrations=FRAMES).diagnostics, condition=SR_CONDITION_MEASUREMENT_UNCALIBRATED))
		model = new_model([arrow_image(calibration=UNCALIBRATED, tracking_uid='2.25.9974')])
		model.create_sr(record_instances=True)
		self.assertEqual([d.severity for d in sr_diagnostics_with(model.diagnostics, condition=SR_CONDITION_MEASUREMENT_UNCALIBRATED)], ['info'])
		model = new_model([arrow_image()])
		model.create_sr(record_instances=True)
		self.assertFalse(sr_diagnostics_with(model.diagnostics, condition=SR_CONDITION_MEASUREMENT_UNCALIBRATED))

	def test_from_sr(self):
		group = group_of(written([arrow_axial(label='Group')]))
		a = ArrowAnnotation.from_sr(group)
		self.assertEqual((a.label, a.tip, a.tracking_id, a.tracking_uid), ('Group', AXIAL_TIP, ARROW_ID, TRACKING))
		self.assertIsNone(ArrowAnnotation.from_sr(finding_of(group)))
		self.assertEqual(ArrowAnnotation.from_sr(finding_of(group), group=group).label, 'Group')


class ArrowLabelTestCase(OracleMixin, unittest.TestCase):
	def test_coded_finding_carried_verbatim(self):
		doc = written([arrow_axial(label=LESION, text='Coded')])
		value = finding_of(group_of(doc)).ConceptCodeSequence[0]
		self.assertEqual((value.CodeValue, value.CodingSchemeDesignator, value.CodeMeaning), ('52988006', 'SCT', 'Lesion'))
		model = reparse(doc, strict=True)
		(a,) = arrows_of(model)
		self.assertEqual((a.label, a.is_coded, a.finding.meaning, a.text), (None, True, 'Lesion', 'Coded'))
		self.assert_same_content(doc, roundtrip(model.create_sr()))

	def test_empty_label_refused(self):
		for label in ('', '   '):
			with self.assertRaises(ValueError):
				written([arrow_axial(label=label)])

	def test_empty_meaning_read_with_warning_and_written_back(self):
		doc = written([arrow_axial()])
		finding_of(group_of(doc)).ConceptCodeSequence[0].CodeMeaning = ''
		model = reparse(doc)
		(a,) = arrows_of(model)
		self.assertEqual(a.label, '')
		self.assertEqual([d.severity for d in sr_diagnostics_with(model.diagnostics, condition=SR_CONDITION_ARROW_WITHOUT_LABEL)], ['warning'])
		again = roundtrip(model.create_sr())
		self.assertEqual(finding_of(group_of(again)).ConceptCodeSequence[0].CodeMeaning, '')
		# A label read and then emptied is refused
		model = reparse(written([arrow_axial()]))
		arrows_of(model)[0].label = ''
		with self.assertRaises(ValueError):
			model.create_sr()

	def test_over_length_label_verbatim(self):
		label = 'An arrow label written well past the sixty-four characters of a Code Meaning (LO)'
		self.assertGreater(len(label), 64)
		model = new_model([arrow_axial(label=label)])
		doc = roundtrip(model.create_sr(record_instances=True))
		self.assertEqual([d.severity for d in sr_diagnostics_with(model.diagnostics, condition=SR_CONDITION_OVERLENGTH_CODE_MEANING)], ['info'])
		self.assertEqual(finding_of(group_of(doc)).ConceptCodeSequence[0].CodeMeaning, label)
		read_back = reparse(doc)
		self.assertEqual(arrows_of(read_back)[0].label, label)
		self.assertEqual([d.severity for d in sr_diagnostics_with(read_back.diagnostics, condition=SR_CONDITION_OVERLENGTH_CODE_MEANING)], ['info'])

	def test_non_ascii_label_and_description(self):
		doc = written([arrow_image(label=NON_ASCII_LABEL, description=NON_ASCII_DESCRIPTION)])
		self.assertEqual(doc.SpecificCharacterSet, DCM_CHARACTER_SET_UTF8)
		model = reparse(doc, strict=True)
		(a,) = arrows_of(model)
		self.assertEqual((a.label, a.description), (NON_ASCII_LABEL, NON_ASCII_DESCRIPTION))
		again = roundtrip(model.create_sr())
		self.assertEqual(again.SpecificCharacterSet, DCM_CHARACTER_SET_UTF8)
		self.assert_same_content(doc, again)

	def test_foreign_scheme_free_text_value_is_coded(self):
		foreign = Code(DCMSR_FREE_TEXT_VALUE, '99OTHER', 'Foreign coded meaning')
		doc = written([arrow_axial(label=foreign)])
		model = reparse(doc, strict=True)
		(a,) = arrows_of(model)
		self.assertEqual((a.label, a.is_coded, a.finding.meaning, a.finding.scheme_designator), (None, True, 'Foreign coded meaning', '99OTHER'))
		# An empty meaning in a foreign scheme is not an empty label
		finding_of(group_of(doc)).ConceptCodeSequence[0].CodeMeaning = ''
		self.assertFalse(sr_diagnostics_with(reparse(doc).diagnostics, condition=SR_CONDITION_ARROW_WITHOUT_LABEL))

	def test_foreign_scheme_finding_site_gives_no_label(self):
		doc = written([arrow_axial()])
		group = group_of(doc)
		finding = finding_of(group)
		site = free_text_site('Not user text')
		site.ConceptCodeSequence[0].CodingSchemeDesignator = '99OTHER'
		group.ContentSequence = Sequence([c for c in group.ContentSequence if c is not finding] + [site])
		model = reparse(doc)
		(f,) = [f for g in model.groups for f in g.findings]
		self.assertNotIsInstance(f, ArrowAnnotation)
		self.assertIsNone(f.finding)
		self.assertFalse(sr_diagnostics_with(model.diagnostics, condition=SR_CONDITION_ARROW_LABEL_FROM_FINDING_SITE))
		self.assert_same_content(doc, roundtrip(model.create_sr()))

	def test_label_from_finding_site(self):
		doc = written([arrow_axial()])
		group = group_of(doc)
		finding = finding_of(group)
		# The stock layout of a label held by a Finding Site: the Finding replaced by a group-level site, the
		# geometry under a NUM carrier
		carrier = Dataset()
		carrier.RelationshipType, carrier.ValueType = 'CONTAINS', 'NUM'
		name = Dataset()
		name.CodeValue, name.CodingSchemeDesignator, name.CodeMeaning = '111010', 'DCM', 'label'
		carrier.ConceptNameCodeSequence = Sequence([name])
		carrier.MeasuredValueSequence = Sequence()
		carrier.ContentSequence = Sequence(coordinates_of(finding))
		group.ContentSequence = Sequence([c for c in group.ContentSequence if c is not finding] + [free_text_site('From site'), carrier])
		model = reparse(doc)
		(a,) = arrows_of(model)
		self.assertEqual((a.label, a.tip, a.tail, a.legacy_geometry), ('From site', AXIAL_TIP, AXIAL_TAIL, True))
		self.assertEqual([d.severity for d in sr_diagnostics_with(model.diagnostics, condition=SR_CONDITION_ARROW_LABEL_FROM_FINDING_SITE)], ['info'])
		again = group_of(roundtrip(model.create_sr()))
		self.assertEqual([c.ValueType for c in srdoc_children(again)], ['TEXT', 'UIDREF', 'CODE'])
		self.assertEqual(finding_of(again).ConceptCodeSequence[0].CodeMeaning, 'From site')


class ArrowLegacyFormTestCase(OracleMixin, unittest.TestCase):
	'''	The Viewer's current form (NUM "Center", one-point POINT) and the stock Cornerstone3D form (NUM "label",
		two-point POLYLINE), built from a Sonador-form document
	'''
	def legacy(self, arrow, meaning, tip_only=False):
		doc = written([arrow])
		group = group_of(doc)
		finding = finding_of(group)
		geometry = coordinates_of(finding)[0]
		if tip_only:
			geometry.GraphicType = 'POINT'
			geometry.GraphicData = list(geometry.GraphicData)[:len(geometry.GraphicData) // 2]
		finding.ContentSequence = Sequence([c for c in finding.ContentSequence if c is not geometry])
		carrier = Dataset()
		carrier.RelationshipType, carrier.ValueType = 'CONTAINS', 'NUM'
		name, unit, value = Dataset(), Dataset(), Dataset()
		name.CodeValue, name.CodingSchemeDesignator, name.CodeMeaning = '111010', 'DCM', meaning
		unit.CodeValue, unit.CodingSchemeDesignator, unit.CodeMeaning = '1', 'UCUM', 'no units'
		value.NumericValue, value.MeasurementUnitsCodeSequence = 0, Sequence([unit])
		carrier.ConceptNameCodeSequence, carrier.MeasuredValueSequence = Sequence([name]), Sequence([value])
		carrier.ContentSequence = Sequence([geometry])
		group.ContentSequence.append(carrier)
		return doc

	def test_viewer_current_form(self):
		doc = self.legacy(arrow_image(description='Viewer', display=LINE_TEXT), 'Center', tip_only=True)
		model = reparse(doc, calibrations=FRAMES)
		(a,) = arrows_of(model)
		self.assertEqual((a.tip, a.tail, a.geometry.graphic_type, a.description, a.legacy_geometry), (IMAGE_TIP, None, 'POINT', 'Viewer', True))
		self.assertEqual([d.severity for d in sr_diagnostics_with(model.diagnostics, condition=SR_CONDITION_ARROW_LEGACY_GEOMETRY)], ['info'])
		again = roundtrip(model.create_sr())
		group = group_of(again)
		self.assertFalse([c for c in srdoc_children(group) if c.ValueType == 'NUM'])
		self.assertEqual(graphic(coordinates_of(finding_of(group))[0]), ('SCOORD', 'POINT', (IMAGE_TIP,)))
		self.assertEqual([d.severity for d in sr_diagnostics_with(model.diagnostics, condition=SR_CONDITION_ARROW_LEGACY_GEOMETRY)], ['info', 'info'])
		self.assert_same_content(legacy_expected(doc), again)
		# Written back in the Sonador form, it reads without the legacy diagnostic
		self.assertFalse(sr_diagnostics_with(reparse(again, strict=True).diagnostics, condition=SR_CONDITION_ARROW_LEGACY_GEOMETRY))

	def test_stock_cornerstone3d_form(self):
		doc = self.legacy(arrow_axial(), 'label')
		model = reparse(doc)
		(a,) = arrows_of(model)
		self.assertEqual((a.tip, a.tail, a.frame_of_reference_uid, a.legacy_geometry), (AXIAL_TIP, AXIAL_TAIL, FOR_UID, True))
		self.assert_same_content(legacy_expected(doc), roundtrip(model.create_sr()))

	def test_other_carrier_children_move_under_the_finding(self):
		doc = self.legacy(arrow_axial(), 'label')
		carrier = [c for c in srdoc_children(group_of(doc)) if c.ValueType == 'NUM'][0]
		carrier.ContentSequence.append(property_item(VENDOR_NOTE, 'carried'))
		model = reparse(doc)
		self.assertEqual([i.TextValue for i in arrows_of(model)[0].extra_items], ['carried'])
		finding = finding_of(group_of(roundtrip(model.create_sr())))
		self.assertEqual([c.TextValue for c in named(srdoc_children(finding), VENDOR_NOTE.value)], ['carried'])


class ArrowFallbackTestCase(OracleMixin, unittest.TestCase):
	def test_geometry_of_another_type_reads_as_a_plain_finding(self):
		doc = written([arrow_image()])
		scoord = coordinates_of(finding_of(group_of(doc)))[0]
		scoord.GraphicType = 'POLYGON'
		scoord.GraphicData = [10.0, 10.0, 20.0, 10.0, 20.0, 20.0, 10.0, 10.0]
		model = reparse(doc)
		(f,) = [f for g in model.groups for f in g.findings]
		self.assertNotIsInstance(f, ArrowAnnotation)
		self.assertEqual((type(f), f.type, f.meaning), (Finding, SONADOR_MEASUREMENT_TYPE_ARROW, 'Synthetic arrow'))
		self.assertEqual([i.ValueType for i in f.extra_items], ['SCOORD'])
		self.assertEqual([d.severity for d in sr_diagnostics_with(model.diagnostics, condition=SR_CONDITION_ARROW_READ_AS_FINDING)], ['warning'])
		with self.assertRaises(SRDiagnosticError):
			reparse(doc, strict=True)
		self.assertEqual(len(dcmsr_local_series([doc]).findings(measurement_type=SONADOR_MEASUREMENT_TYPE_ARROW)), 1)
		self.assert_same_content(doc, roundtrip(model.create_sr()))

	def test_legacy_carrier_of_another_type_stays_in_the_group(self):
		doc = written([arrow_axial()])
		group = group_of(doc)
		finding = finding_of(group)
		geometry = coordinates_of(finding)[0]
		geometry.GraphicType = 'MULTIPOINT'
		finding.ContentSequence = Sequence([c for c in finding.ContentSequence if c is not geometry])
		carrier = Dataset()
		carrier.RelationshipType, carrier.ValueType = 'CONTAINS', 'NUM'
		name = Dataset()
		name.CodeValue, name.CodingSchemeDesignator, name.CodeMeaning = '111010', 'DCM', 'label'
		unit, value = Dataset(), Dataset()
		unit.CodeValue, unit.CodingSchemeDesignator, unit.CodeMeaning = '1', 'UCUM', 'no units'
		value.NumericValue, value.MeasurementUnitsCodeSequence = 0, Sequence([unit])
		carrier.ConceptNameCodeSequence, carrier.MeasuredValueSequence, carrier.ContentSequence = Sequence([name]), Sequence([value]), Sequence([geometry])
		group.ContentSequence.append(carrier)
		model = reparse(doc)
		(g,) = model.groups
		self.assertNotIsInstance(g.findings[0], ArrowAnnotation)
		self.assertEqual([i.ValueType for i in g.extra_items], ['NUM'])
		self.assert_same_content(doc, roundtrip(model.create_sr()))

	def test_group_without_a_finding_listed_as_a_plain_finding(self):
		doc = written([arrow_image(tracking_uid='2.25.9979')])
		group = group_of(doc)
		finding = finding_of(group)
		group.ContentSequence = Sequence([c for c in group.ContentSequence if c is not finding] + list(coordinates_of(finding)))
		model = reparse(doc)
		(g,) = model.groups
		(f,) = g.findings
		self.assertEqual((type(f), f.type, f.finding, f.tracking_id, f.tracking_uid), (Finding, SONADOR_MEASUREMENT_TYPE_ARROW, None, ARROW_ID, '2.25.9979'))
		self.assertTrue(f.is_group_placeholder)
		self.assertEqual((f.reference.sop_instance_uid, f.series_uid), (SYNTHETIC['SOPInstanceUID'], SYNTHETIC['SeriesInstanceUID']))
		self.assertEqual([i.ValueType for i in g.extra_items], ['SCOORD'])
		self.assertEqual([d.severity for d in sr_diagnostics_with(model.diagnostics, condition=SR_CONDITION_ARROW_READ_AS_FINDING)], ['warning'])
		# Through the public accessors: listed by findings under arrow, never by measurements
		series = dcmsr_local_series([doc])
		(listed,) = series.findings(SYNTHETIC['SeriesInstanceUID'], SONADOR_MEASUREMENT_TYPE_ARROW)
		self.assertEqual((listed.tracking_uid, listed.finding), ('2.25.9979', None))
		self.assertFalse(series.measurements())
		# Re-saved as read: the group, its tracking identity and its content, and no Finding added
		again = roundtrip(model.create_sr())
		out = group_of(again)
		self.assertEqual([c.ValueType for c in srdoc_children(out)], ['TEXT', 'UIDREF', 'SCOORD'])
		self.assertEqual((srdoc_children(out)[0].TextValue, srdoc_children(out)[1].UID), (ARROW_ID, '2.25.9979'))
		self.assert_same_content(doc, again)

	def test_new_finding_without_a_value_is_refused(self):
		entered = Finding(DCMSR_FINDING, None, description='Entered observation')
		self.assertFalse(entered.is_group_placeholder)
		with self.assertRaises(ValueError):
			new_model([entered, arrow_axial()]).create_sr(record_instances=True)
		# A Finding-less group read from a document still writes back as read beside it
		doc = written([arrow_image(tracking_uid='2.25.9978')])
		group = group_of(doc)
		finding = finding_of(group)
		group.ContentSequence = Sequence([c for c in group.ContentSequence if c is not finding] + list(coordinates_of(finding)))
		model = reparse(doc)
		self.assert_same_content(doc, roundtrip(model.create_sr()))
		model.findings = [Finding(DCMSR_FINDING, None, description='Entered observation')]
		with self.assertRaises(ValueError):
			model.create_sr()

	def test_image_geometry_without_its_image_reads_as_a_plain_finding(self):
		doc = written([arrow_image()])
		scoord = coordinates_of(finding_of(group_of(doc)))[0]
		del scoord.ContentSequence
		model = reparse(doc)
		(f,) = [f for g in model.groups for f in g.findings]
		self.assertNotIsInstance(f, ArrowAnnotation)
		self.assertEqual((f.type, f.meaning, [i.ValueType for i in f.extra_items]), (SONADOR_MEASUREMENT_TYPE_ARROW, 'Synthetic arrow', ['SCOORD']))
		self.assertEqual([d.severity for d in sr_diagnostics_with(model.diagnostics, condition=SR_CONDITION_ARROW_READ_AS_FINDING)], ['warning'])
		self.assert_same_content(doc, roundtrip(model.create_sr()))

	def test_image_of_another_relationship_is_not_the_source(self):
		doc = written([arrow_image()])
		scoord = coordinates_of(finding_of(group_of(doc)))[0]
		srdoc_children(scoord)[0].RelationshipType = 'INFERRED FROM'
		model = reparse(doc)
		(f,) = [f for g in model.groups for f in g.findings]
		self.assertNotIsInstance(f, ArrowAnnotation)
		self.assertEqual([d.severity for d in sr_diagnostics_with(model.diagnostics, condition=SR_CONDITION_ARROW_READ_AS_FINDING)], ['warning'])
		again = roundtrip(model.create_sr())
		self.assertEqual(srdoc_children(coordinates_of(finding_of(group_of(again)))[0])[0].RelationshipType, 'INFERRED FROM')
		self.assert_same_content(doc, again)

	def test_unrelated_image_cannot_take_the_source_link(self):
		doc = written([arrow_image()])
		scoord = coordinates_of(finding_of(group_of(doc)))[0]
		source = srdoc_children(scoord)[0]
		# An unrelated IMAGE first, on another frame of the same instance (so that the evidence records it)
		unrelated = copy.deepcopy(source)
		unrelated.RelationshipType = 'INFERRED FROM'
		unrelated.ReferencedSOPSequence[0].ReferencedFrameNumber = 7
		scoord.ContentSequence = Sequence([unrelated, source])
		model = reparse(doc)
		(a,) = arrows_of(model)
		self.assertEqual((a.reference.sop_instance_uid, a.reference.frame_number), (SYNTHETIC['SOPInstanceUID'], None))
		self.assert_same_content(doc, roundtrip(model.create_sr()))
		# An edit of the reference moves the SELECTED FROM image only
		model = reparse(doc)
		arrows_of(model)[0].reference = synthetic_reference(sop_instance_uid='2.25.1004')
		images = srdoc_children(coordinates_of(finding_of(group_of(roundtrip(model.create_sr()))))[0])
		self.assertEqual([(i.RelationshipType, i.ReferencedSOPSequence[0].ReferencedSOPInstanceUID, i.ReferencedSOPSequence[0].get('ReferencedFrameNumber'))
			for i in images], [('INFERRED FROM', SYNTHETIC['SOPInstanceUID'], 7), ('SELECTED FROM', '2.25.1004', None)])

	def test_frame_reference_kept(self):
		doc = written([ArrowAnnotation.from_points(IMAGE_TIP, IMAGE_TAIL, 'Framed', reference=synthetic_reference(frame_number=3),
			tracking_uid=TRACKING)])
		image = srdoc_children(coordinates_of(finding_of(group_of(doc)))[0])[0]
		self.assertEqual(int(image.ReferencedSOPSequence[0].ReferencedFrameNumber), 3)
		model = reparse(doc, strict=True)
		self.assertEqual(arrows_of(model)[0].reference.frame_number, 3)
		self.assert_same_content(doc, roundtrip(model.create_sr()))


class ArrowCarrierTestCase(OracleMixin, unittest.TestCase):
	'''	Only the stock NUM carrier is dropped: concept (111010, DCM), a zero value in the UCUM unit 1, and geometry
	'''
	def num(self, geometry, code=('111010', 'DCM', 'Center'), value=0, unit=('1', 'UCUM', 'no units')):
		item = Dataset()
		item.RelationshipType, item.ValueType = 'CONTAINS', 'NUM'
		name, units, measured = Dataset(), Dataset(), Dataset()
		name.CodeValue, name.CodingSchemeDesignator, name.CodeMeaning = code
		units.CodeValue, units.CodingSchemeDesignator, units.CodeMeaning = unit
		measured.NumericValue, measured.MeasurementUnitsCodeSequence = value, Sequence([units])
		item.ConceptNameCodeSequence, item.MeasuredValueSequence = Sequence([name]), Sequence([measured])
		item.ContentSequence = Sequence([copy.deepcopy(geometry)])
		return item

	def stock(self, *nums):
		'''	A Viewer-form arrow (Finding with the label, geometry under the NUM items given)
		'''
		doc = written([arrow_axial()])
		group = group_of(doc)
		finding = finding_of(group)
		geometry = coordinates_of(finding)[0]
		finding.ContentSequence = Sequence([c for c in finding.ContentSequence if c is not geometry])
		group.ContentSequence = Sequence(list(group.ContentSequence) + [n(geometry) for n in nums])
		return doc

	def test_unrelated_num_before_the_carrier_preserved(self):
		unrelated = lambda g: self.num(g, code=('410668003', 'SCT', 'Length'), value=17, unit=('mm', 'UCUM', 'mm'))
		marker_like = lambda g: self.num(g, value=17)
		carrier = lambda g: self.num(g)
		doc = self.stock(unrelated, marker_like, carrier)
		model = reparse(doc)
		(a,) = arrows_of(model)
		self.assertEqual((a.tip, a.tail, a.legacy_geometry), (AXIAL_TIP, AXIAL_TAIL, True))
		(g,) = model.groups
		self.assertEqual([float(i.MeasuredValueSequence[0].NumericValue) for i in g.extra_items], [17.0, 17.0])
		again = group_of(roundtrip(model.create_sr()))
		nums = [c for c in srdoc_children(again) if c.ValueType == 'NUM']
		self.assertEqual([(dcm_code_value(n.ConceptNameCodeSequence[0]), float(n.MeasuredValueSequence[0].NumericValue)) for n in nums],
			[('410668003', 17.0), ('111010', 17.0)])
		self.assertTrue(all(coordinates_of(n) for n in nums))

	def test_qualified_num_without_a_value_preserved(self):
		def qualified(g):
			item = self.num(g)
			del item.MeasuredValueSequence
			qualifier, name = Dataset(), Dataset()
			qualifier.CodeValue, qualifier.CodingSchemeDesignator, qualifier.CodeMeaning = '114006', 'DCM', 'Measurement failure'
			item.NumericValueQualifierCodeSequence = Sequence([qualifier])
			return item
		# Alone: no carrier, the group reads as a plain Finding and re-saves as read
		doc = self.stock(qualified)
		model = reparse(doc)
		(g,) = model.groups
		self.assertNotIsInstance(g.findings[0], ArrowAnnotation)
		again = group_of(roundtrip(model.create_sr()))
		(num,) = [c for c in srdoc_children(again) if c.ValueType == 'NUM']
		self.assertEqual((num.NumericValueQualifierCodeSequence[0].CodeValue, len(num.MeasuredValueSequence)), ('114006', 0))
		self.assertTrue(coordinates_of(num))
		# Before a real carrier: kept, the arrow read from the carrier
		doc = self.stock(qualified, lambda g: self.num(g))
		model = reparse(doc)
		self.assertTrue(arrows_of(model)[0].legacy_geometry)
		again = group_of(roundtrip(model.create_sr()))
		(num,) = [c for c in srdoc_children(again) if c.ValueType == 'NUM']
		self.assertEqual((num.NumericValueQualifierCodeSequence[0].CodeValue, len(coordinates_of(num))), ('114006', 1))

	def test_group_with_only_an_unrelated_num(self):
		doc = self.stock(lambda g: self.num(g, value=17))
		model = reparse(doc)
		(g,) = model.groups
		(f,) = g.findings
		self.assertNotIsInstance(f, ArrowAnnotation)
		self.assertEqual((f.type, f.meaning), (SONADOR_MEASUREMENT_TYPE_ARROW, 'Synthetic arrow'))
		self.assertEqual([float(i.MeasuredValueSequence[0].NumericValue) for i in g.extra_items], [17.0])
		self.assertEqual([d.severity for d in sr_diagnostics_with(model.diagnostics, condition=SR_CONDITION_ARROW_READ_AS_FINDING)], ['warning'])
		self.assert_same_content(doc, roundtrip(model.create_sr()))


class ArrowEditTestCase(OracleMixin, unittest.TestCase):
	def edited(self, arrow, edit, **kwargs):
		model = reparse(written([arrow]), **kwargs)
		edit(arrows_of(model)[0])
		return model, roundtrip(model.create_sr())

	def test_edit_tip_and_tail(self):
		model, doc = self.edited(arrow_axial(), lambda a: setattr(a, 'tip', (0.0, 0.0, 30.0)))
		self.assertEqual(graphic(coordinates_of(finding_of(group_of(doc)))[0])[2], ((0.0, 0.0, 30.0), AXIAL_TAIL))
		model, doc = self.edited(arrow_image(), lambda a: setattr(a, 'tail', (5.0, 5.0)), calibrations=FRAMES)
		self.assertEqual(graphic(coordinates_of(finding_of(group_of(doc)))[0])[2], (IMAGE_TIP, (5.0, 5.0)))
		self.assertEqual(arrows_of(reparse(doc))[0].tail, (5.0, 5.0))

	def test_removing_and_adding_the_tail(self):
		model, doc = self.edited(arrow_image(), lambda a: setattr(a, 'tail', None))
		self.assertEqual(graphic(coordinates_of(finding_of(group_of(doc)))[0]), ('SCOORD', 'POINT', (IMAGE_TIP,)))
		model = reparse(doc)
		arrows_of(model)[0].tail = IMAGE_TAIL
		again = roundtrip(model.create_sr())
		scoord = coordinates_of(finding_of(group_of(again)))[0]
		self.assertEqual(graphic(scoord), ('SCOORD', 'POLYLINE', (IMAGE_TIP, IMAGE_TAIL)))
		self.assertEqual(srdoc_children(scoord)[0].ReferencedSOPSequence[0].ReferencedSOPInstanceUID, SYNTHETIC['SOPInstanceUID'])

	def test_coordinate_identity_kept_across_point_and_polyline(self):
		doc = written([ArrowAnnotation.from_points(IMAGE_TIP, IMAGE_TAIL, 'Fiducial', reference=synthetic_reference(frame_number=2),
			tracking_uid=TRACKING)])
		coordinates_of(finding_of(group_of(doc)))[0].FiducialUID = '2.25.9977'
		for tail, expected in ((None, ('SCOORD', 'POINT', (IMAGE_TIP,))), (IMAGE_TAIL, ('SCOORD', 'POLYLINE', (IMAGE_TIP, IMAGE_TAIL)))):
			model = reparse(doc)
			arrows_of(model)[0].tail = tail
			doc = roundtrip(model.create_sr())
			scoord = coordinates_of(finding_of(group_of(doc)))[0]
			self.assertEqual(graphic(scoord), expected)
			self.assertEqual(scoord.FiducialUID, '2.25.9977')
			image = srdoc_children(scoord)[0].ReferencedSOPSequence[0]
			self.assertEqual((image.ReferencedSOPInstanceUID, int(image.ReferencedFrameNumber)), (SYNTHETIC['SOPInstanceUID'], 2))
		# The patient domain likewise
		doc = written([arrow_axial()])
		coordinates_of(finding_of(group_of(doc)))[0].FiducialUID = '2.25.9976'
		model = reparse(doc)
		arrows_of(model)[0].tail = None
		scoord = coordinates_of(finding_of(group_of(roundtrip(model.create_sr()))))[0]
		self.assertEqual((graphic(scoord), scoord.FiducialUID, scoord.ReferencedFrameOfReferenceUID), (('SCOORD3D', 'POINT', (AXIAL_TIP,)), '2.25.9976', FOR_UID))

	def test_edit_label(self):
		model, doc = self.edited(arrow_axial(text='Kept'), lambda a: setattr(a, 'label', 'Renamed'))
		finding = finding_of(group_of(doc))
		self.assertEqual(finding.ConceptCodeSequence[0].CodeMeaning, 'Renamed')
		self.assertEqual([c.TextValue for c in children_named(finding, DCMSR_MEASUREMENT_TEXT)], ['Kept'])
		model, doc = self.edited(arrow_axial(label=LESION), lambda a: setattr(a, 'label', 'Free'))
		self.assertEqual(dcm_code_value(finding_of(group_of(doc)).ConceptCodeSequence[0]), 'CORNERSTONEFREETEXT')

	def test_edit_reference(self):
		model, doc = self.edited(arrow_image(), lambda a: setattr(a, 'reference', synthetic_reference(sop_instance_uid='2.25.1004')))
		scoord = coordinates_of(finding_of(group_of(doc)))[0]
		self.assertEqual(srdoc_children(scoord)[0].ReferencedSOPSequence[0].ReferencedSOPInstanceUID, '2.25.1004')

	def test_deletion(self):
		doc = written([arrow_axial(tracking_uid='2.25.9984'), arrow_image(tracking_uid='2.25.9985')])
		model = reparse(doc)
		model.groups = [g for g in model.groups if g.tracking_uid != '2.25.9984']
		again = roundtrip(model.create_sr())
		self.assertEqual([a.tracking_uid for a in arrows_of(reparse(again))], ['2.25.9985'])
		self.assertEqual(len(srdoc_measurement_groups(again)), 1)

	def test_repeated_saves(self):
		doc = written([arrow_image(label=NON_ASCII_LABEL, description='Repeated', display=LINE_TEXT, views=['axial']),
			arrow_axial(series_uid=SYNTHETIC['SeriesInstanceUID'], drawing_plane=AXIAL, tracking_uid='2.25.9986')])
		current = doc
		for _ in range(3):
			model = reparse(current, strict=True)
			current = roundtrip(model.create_sr())
			self.assert_same_content(doc, current)
		self.assertEqual(len(current.PredecessorDocumentsSequence[0].ReferencedSeriesSequence[0].ReferencedSOPSequence), 3)


class ArrowOutOfPlaneTestCase(OracleMixin, unittest.TestCase):
	def test_drawing_plane_holds_both_points(self):
		tail = tuple(float(c) for c in np.asarray(OBLIQUE_ORIGIN) + 10.0 * np.asarray(OBLIQUE_WIDTH))
		a = arrow_axial(tip=OBLIQUE_ORIGIN, tail=tail, drawing_plane=(OBLIQUE_WIDTH, OBLIQUE_HEIGHT), series_uid=SYNTHETIC['SeriesInstanceUID'])
		doc = written([a])
		self.assertEqual(arrows_of(reparse(doc, strict=True))[0].drawing_plane_text, OBLIQUE_TEXT)
		# A tail 2 mm out of the plane fails it, on write and on read
		normal = np.cross(OBLIQUE_WIDTH, OBLIQUE_HEIGHT)
		lifted = tuple(float(c) for c in np.asarray(tail) + 2.0 * normal)
		with self.assertRaises(ValueError) as ctx:
			written([arrow_axial(tip=OBLIQUE_ORIGIN, tail=lifted, drawing_plane=(OBLIQUE_WIDTH, OBLIQUE_HEIGHT))])
		self.assertIn('points-within-1mm', str(ctx.exception))
		coordinates_of(finding_of(group_of(doc)))[0].GraphicData = list(OBLIQUE_ORIGIN) + list(lifted)
		model = reparse(doc)
		self.assertIsNone(arrows_of(model)[0].drawing_plane)
		self.assertEqual([d.severity for d in sr_diagnostics_with(model.diagnostics, condition=SR_CONDITION_INVALID_DRAWING_PLANE)], ['warning'])
		again = roundtrip(model.create_sr())
		self.assertEqual([c.TextValue for c in children_named(finding_of(group_of(again)), DCMSR_MEASUREMENT_DRAWING_PLANE)], [OBLIQUE_TEXT])
		self.assert_same_content(doc, again)

	def test_source_series_in_the_evidence(self):
		doc = written([arrow_axial(series_uid=SYNTHETIC['SeriesInstanceUID'])])
		evidence = doc.CurrentRequestedProcedureEvidenceSequence[0].ReferencedSeriesSequence
		self.assertEqual([str(s.SeriesInstanceUID) for s in evidence], [SYNTHETIC['SeriesInstanceUID']])
		with self.assertRaises(ValueError):
			written([arrow_axial(series_uid='2.25.404')])


FIXTURE_DATE = '2026-1114'
FIXTURE_TRACKING = '2.25.8500'
OBLIQUE_VIEW = ((OBLIQUE_WIDTH, OBLIQUE_HEIGHT))


def fixture_path(topic):
	return os.path.join(FIXTURES, 'sonador-io.%s.%s.dcm' % (topic, FIXTURE_DATE))


def fixture_arrows(topic):
	'''	The content of each Python-written fixture
	'''
	common = dict(text='Synthetic', location='Synthetic location', display=DisplayAttributes.from_color(LINE_TEXT), tracking_uid=FIXTURE_TRACKING)
	if topic == 'arrow-image':
		return [arrow_image(label=NON_ASCII_LABEL, description=NON_ASCII_DESCRIPTION, views=['axial'], **common)]
	if topic == 'arrow-axial':
		return [arrow_axial(label='Axial arrow', description='arrow-axial annotation', views=['axial'], series_uid=SYNTHETIC['SeriesInstanceUID'],
			drawing_plane=AXIAL, **common)]
	if topic == 'arrow-oblique':
		return [arrow_axial(tip=OBLIQUE_ORIGIN, tail=None, label='Oblique arrow', description='arrow-oblique annotation', views=['oblique'],
			series_uid=SYNTHETIC['SeriesInstanceUID'], drawing_plane=OBLIQUE_VIEW, **common)]
	raise KeyError(topic)


def write_fixture(topic):
	doc = new_model(fixture_arrows(topic)).create_sr(record_instances=True, dcmsr_series_uid='2.25.8000', dcmsr_instance_uid='2.25.8001')
	doc.save_as(fixture_path(topic))


class ArrowFixtureTestCase(OracleMixin, unittest.TestCase):
	# topic -> (domain, graphic type, tip, tail, label, description, Drawing-Plane text, view)
	FIXTURES = OrderedDict((
		('arrow-image', ('image', 'POLYLINE', IMAGE_TIP, IMAGE_TAIL, NON_ASCII_LABEL, NON_ASCII_DESCRIPTION, None, 'axial')),
		('arrow-axial', ('patient', 'POLYLINE', AXIAL_TIP, AXIAL_TAIL, 'Axial arrow', 'arrow-axial annotation', '1,0,0,0,1,0', 'axial')),
		('arrow-oblique', ('patient', 'POINT', OBLIQUE_ORIGIN, None, 'Oblique arrow', 'arrow-oblique annotation', OBLIQUE_TEXT, 'oblique')),
	))

	def setUp(self):
		if os.environ.get('UPDATE_SR_FIXTURES'):
			for topic in self.FIXTURES:
				write_fixture(topic)

	def test_fixtures(self):
		for topic, (domain, graphic_type, tip, tail, label, description, plane, view) in self.FIXTURES.items():
			doc = read(fixture_path(topic))
			self.assertEqual((doc.PatientID, str(doc.StudyInstanceUID), str(doc.SOPClassUID)), ('ANONYMOUS', '2.25.1001', DCMSR_SOP_CLASS_COMPREHENSIVE_3D))
			self.assertEqual(doc.get('SpecificCharacterSet') == DCM_CHARACTER_SET_UTF8, topic == 'arrow-image', topic)
			model = reparse(doc, strict=True, calibrations=FRAMES)
			self.assertFalse(model.diagnostics, topic)
			(a,) = arrows_of(model)
			self.assertIsInstance(a, ArrowAnnotation, topic)
			self.assertEqual((a.geometry.domain, a.geometry.graphic_type, a.tracking_id, a.tracking_uid), (domain, graphic_type, ARROW_ID, FIXTURE_TRACKING), topic)
			np.testing.assert_allclose(a.tip, tip, atol=1e-4, err_msg=topic)
			if tail is None:
				self.assertIsNone(a.tail, topic)
			else:
				np.testing.assert_allclose(a.tail, tail, atol=1e-4, err_msg=topic)
			self.assertEqual((a.label, a.description, a.text, a.location, a.views, a.drawing_plane_text),
				(label, description, 'Synthetic', 'Synthetic location', (view,), plane), topic)
			self.assertEqual(dict(a.display.color), LINE_TEXT, topic)
			if domain == 'patient':
				self.assertEqual((a.frame_of_reference_uid, a.series_uid), (FOR_UID, SYNTHETIC['SeriesInstanceUID']), topic)
			else:
				self.assertEqual(a.reference.sop_instance_uid, SYNTHETIC['SOPInstanceUID'], topic)
			self.assertFalse([c for c in srdoc_children(group_of(doc)) if c.ValueType == 'NUM'], topic)
			self.assert_same_content(doc, roundtrip(model.create_sr()))


ADAPTER_FIXTURES = {topic: os.path.join(FIXTURES, 'cornerstone-adapters.%s.2026-1114.dcm' % topic)
	for topic in ('arrow-image', 'arrow-patient', 'arrow-point')}


class ArrowAdapterFixtureTestCase(OracleMixin, unittest.TestCase):
	'''	Documents written by the stock @cornerstonejs/adapters 4.22.13 writers: the Cornerstone3D ArrowAnnotate
		adapter (NUM "label", two-point POLYLINE) and the classic ArrowAnnotate adapter (NUM "Center", one-point POINT)
	'''
	# topic -> (tracking identifier, NUM meaning, value type, graphic type, tip, tail)
	EXPECTED = OrderedDict((
		('arrow-image', (ARROW_ID_3D, 'label', 'SCOORD', 'POLYLINE', (30.0, 20.0), (10.0, 40.0))),
		('arrow-patient', (ARROW_ID_3D, 'label', 'SCOORD3D', 'POLYLINE', (10.0, 20.0, 30.0), (25.0, 35.0, 30.0))),
		('arrow-point', (ARROW_ID, 'Center', 'SCOORD', 'POINT', (30.0, 20.0), None)),
	))

	def test_stock_forms(self):
		for topic, (tracking_id, meaning, value_type, graphic_type, tip, tail) in self.EXPECTED.items():
			doc = read(ADAPTER_FIXTURES[topic])
			(num,) = [c for c in srdoc_children(group_of(doc)) if c.ValueType == 'NUM']
			self.assertEqual(num.ConceptNameCodeSequence[0].CodeMeaning, meaning, topic)
			self.assertEqual(graphic(coordinates_of(num)[0])[:2], (value_type, graphic_type), topic)
			model = SonadorImagingMeasurementReport.from_sr(doc)
			(a,) = arrows_of(model)
			self.assertIsInstance(a, ArrowAnnotation, topic)
			self.assertEqual((a.tracking_id, a.label, a.tip, a.tail, a.legacy_geometry), (tracking_id, 'Synthetic arrow', tip, tail, True), topic)
			if value_type == 'SCOORD3D':
				self.assertEqual(a.frame_of_reference_uid, FOR_UID, topic)
			else:
				self.assertEqual((a.reference.sop_instance_uid, a.series_uid), (SYNTHETIC['SOPInstanceUID'], SYNTHETIC['SeriesInstanceUID']), topic)
			self.assertEqual([d.severity for d in sr_diagnostics_with(model.diagnostics, condition=SR_CONDITION_ARROW_LEGACY_GEOMETRY)], ['info'], topic)
			self.assertFalse(sr_diagnostics_with(model.diagnostics, severity=SR_DIAGNOSTIC_WARNING), topic)

			# Re-saved in the Sonador form: no NUM, the identifier read kept, a tip-only arrow as a POINT
			again = roundtrip(model.create_sr())
			group = group_of(again)
			self.assertFalse([c for c in srdoc_children(group) if c.ValueType == 'NUM'], topic)
			self.assertEqual(srdoc_children(group)[0].TextValue, tracking_id, topic)
			self.assertEqual(graphic(coordinates_of(finding_of(group))[0])[:2], (value_type, graphic_type), topic)
			self.assert_same_content(legacy_expected(doc), again)
			read_back = reparse(again, strict=True)
			self.assertEqual((arrows_of(read_back)[0].tip, arrows_of(read_back)[0].tail), (tip, tail), topic)


def comment_form(doc, label='Commented arrow', text=None):
	'''	The labelled stock Cornerstone3D form built from a stock fixture: the Finding removed, the label in a group-level
		Comment (121106, DCM), and optionally a group-level Sonador text property
	'''
	group = group_of(doc)
	finding = finding_of(group)
	comment = text_item(Code('121106', 'DCM', 'Comment'), label, relationship='CONTAINS')
	extra = [property_item(DCMSR_MEASUREMENT_TEXT, text)] if text is not None else []
	for item in extra:
		item.RelationshipType = 'CONTAINS'
	group.ContentSequence = Sequence([c for c in group.ContentSequence if c is not finding] + [comment] + extra)
	return doc


class ArrowCommentLabelTestCase(OracleMixin, unittest.TestCase):
	'''	The stock Cornerstone3D writer's form for a labelled annotation: no Finding, the label in a group-level Comment
	'''
	def test_comment_read_as_the_label(self):
		for topic in ('arrow-image', 'arrow-patient'):
			doc = comment_form(read(ADAPTER_FIXTURES[topic]), label=NON_ASCII_LABEL, text='Independent text')
			model = SonadorImagingMeasurementReport.from_sr(doc)
			(a,) = arrows_of(model)
			self.assertIsInstance(a, ArrowAnnotation, topic)
			self.assertEqual((a.label, a.text, a.tracking_id, a.legacy_geometry), (NON_ASCII_LABEL, 'Independent text', ARROW_ID_3D, True), topic)
			self.assertEqual([d.severity for d in sr_diagnostics_with(model.diagnostics, condition=SR_CONDITION_ARROW_LABEL_FROM_COMMENT)],
				['info'], topic)
			# Written back as a CST4 Finding (LongCodeValue), the text a property of its own, no Comment
			again = roundtrip(model.create_sr())
			self.assertEqual(again.SpecificCharacterSet, DCM_CHARACTER_SET_UTF8, topic)
			group = group_of(again)
			value = finding_of(group).ConceptCodeSequence[0]
			self.assertEqual((value.LongCodeValue, value.CodingSchemeDesignator, value.CodeMeaning), ('CORNERSTONEFREETEXT', 'CST4', NON_ASCII_LABEL), topic)
			self.assertNotIn('CodeValue', value)
			self.assertFalse(named(srdoc_children(group), '121106'), topic)
			self.assertEqual([c.TextValue for c in children_named(finding_of(group), DCMSR_MEASUREMENT_TEXT)], ['Independent text'], topic)
			read_back = reparse(again, strict=True)
			self.assertEqual((arrows_of(read_back)[0].label, arrows_of(read_back)[0].text), (NON_ASCII_LABEL, 'Independent text'), topic)

	def test_both_code_value_encodings_give_the_same_label(self):
		labels = []
		for attribute in ('CodeValue', 'LongCodeValue'):
			doc = written([arrow_axial(label='Encoded')])
			value = finding_of(group_of(doc)).ConceptCodeSequence[0]
			for name in ('CodeValue', 'LongCodeValue'):
				if name in value:
					delattr(value, name)
			setattr(value, attribute, 'CORNERSTONEFREETEXT')
			(a,) = arrows_of(reparse(roundtrip(doc)))
			labels.append((a.label, a.is_coded))
		self.assertEqual(labels, [('Encoded', False), ('Encoded', False)])
		# The stock fixtures carry the code in CodeValue
		self.assertIn('CodeValue', finding_of(group_of(read(ADAPTER_FIXTURES['arrow-point']))).ConceptCodeSequence[0])

	def test_coded_finding_kept_beside_a_comment(self):
		doc = written([arrow_axial(label=LESION)])
		group_of(doc).ContentSequence.append(text_item(Code('121106', 'DCM', 'Comment'), 'Not the label', relationship='CONTAINS'))
		model = reparse(doc)
		(a,) = arrows_of(model)
		self.assertEqual((a.label, a.finding.meaning), (None, 'Lesion'))
		self.assertFalse(sr_diagnostics_with(model.diagnostics, condition=SR_CONDITION_ARROW_LABEL_FROM_COMMENT))
		self.assert_same_content(doc, roundtrip(model.create_sr()))


def viewer_fixture(topic):
	found = sorted(glob.glob(os.path.join(FIXTURES, 'sonador-viewer.%s.*.dcm' % topic)))
	return found[-1] if found else None


class ArrowViewerFixtureTestCase(OracleMixin, unittest.TestCase):
	'''	Documents written by the Sonador Viewer's arrow adapter over its synthetic CT, once they are added to the
		fixtures: read without warnings as Sonador-form arrows, and re-saved with the same content tree
	'''
	def check(self, topic, domain):
		path = viewer_fixture(topic)
		if path is None:
			self.skipTest('%s fixture not added yet' % topic)
		doc = read(path)
		model = reparse(doc, strict=True)
		self.assertFalse(sr_diagnostics_with(model.diagnostics, severity=SR_DIAGNOSTIC_WARNING))
		arrows = arrows_of(model)
		self.assertTrue(arrows)
		for a in arrows:
			self.assertIsInstance(a, ArrowAnnotation)
			self.assertEqual((a.geometry.domain, a.legacy_geometry), (domain, False))
			self.assertTrue(a.label)
		self.assert_same_content(doc, roundtrip(model.create_sr()))

	def test_image_domain(self):
		self.check('arrow-image', 'image')

	def test_oblique_patient_domain(self):
		self.check('arrow-oblique', 'patient')


class ArrowDocumentTestCase(OracleMixin, unittest.TestCase):
	def test_mixed_document(self):
		measurements = [length_patient(tracking_uid='2.25.9991'), angle_patient(tracking_uid='2.25.9992'), circle_axial(tracking_uid='2.25.9993'),
			bidirectional_axial(tracking_uid='2.25.9994'), bidirectional_image(tracking_uid='2.25.9995')]
		arrows = [arrow_image(tracking_uid='2.25.9996', label=NON_ASCII_LABEL), arrow_axial(tracking_uid='2.25.9997', series_uid=SYNTHETIC['SeriesInstanceUID'])]
		doc = written(io_findings() + arrows, measurements=measurements)
		model = reparse(doc, strict=True, calibrations=FRAMES)
		self.assertEqual([type(m).__name__ for m in measurements_of(model)],
			['LengthMeasurement', 'AngleMeasurement', 'CircleMeasurement', 'BidirectionalMeasurement', 'BidirectionalMeasurement'])
		findings = [f for g in model.groups for f in (g.findings or [])]
		self.assertEqual([(type(f).__name__, f.type) for f in findings], [('Finding', SONADOR_MEASUREMENT_TYPE_SERIES_TAG)] * 2
			+ [('ArrowAnnotation', SONADOR_MEASUREMENT_TYPE_ARROW)] * 2)
		self.assertEqual([a.tracking_uid for a in model.arrow_findings], ['2.25.9996', '2.25.9997'])
		self.assert_same_content(doc, roundtrip(model.create_sr()))

	def test_retrieval_keys_and_accessor_split(self):
		doc = written([arrow_image(tracking_uid='2.25.9998'), arrow_axial(tracking_uid='2.25.9999', series_uid=SYNTHETIC['SeriesInstanceUID']),
			arrow_axial(tracking_uid='2.25.9990')], measurements=[length_patient(tracking_uid='2.25.9989')])
		series = dcmsr_local_series([doc])
		by_series = series.findings(measurement_type=SONADOR_MEASUREMENT_TYPE_ARROW)
		self.assertEqual(sorted(by_series.keys()), sorted([SYNTHETIC['SeriesInstanceUID'], series.pk]))
		self.assertEqual([a.tracking_uid for a in by_series[SYNTHETIC['SeriesInstanceUID']]], ['2.25.9998', '2.25.9999'])
		self.assertEqual([a.tracking_uid for a in by_series[series.pk]], ['2.25.9990'])
		self.assertEqual(sorted(type(a).__name__ for c in by_series.values() for a in c), ['ArrowAnnotation'] * 3)
		# Arrows are findings, never measurements
		measured = series.measurements()
		self.assertEqual([m.tracking_uid for by_type in measured.values() for c in by_type.values() for m in c], ['2.25.9989'])
		self.assertFalse([t for by_type in measured.values() for t in by_type if t == SONADOR_MEASUREMENT_TYPE_ARROW])
		self.assertEqual(len(series.findings(SYNTHETIC['SeriesInstanceUID'], SONADOR_MEASUREMENT_TYPE_ARROW)), 2)


class CharacterSetTestCase(OracleMixin, unittest.TestCase):
	'''	Specific Character Set `ISO_IR 192` whenever a text value of the document is not ASCII, for every tool;
		an all-ASCII document is written as before
	'''
	def test_non_ascii_text_of_any_tool(self):
		docs = [written([], measurements=[length_image(description=NON_ASCII_DESCRIPTION)]),
			written([Finding.series_tag(io_findings()[0].finding, reference=synthetic_reference(), text=NON_ASCII_DESCRIPTION)]),
			written([arrow_axial(label=NON_ASCII_LABEL)]),
			written([arrow_axial(location=NON_ASCII_DESCRIPTION)])]
		for doc in docs:
			self.assertEqual(doc.SpecificCharacterSet, DCM_CHARACTER_SET_UTF8)
		self.assertIn(NON_ASCII_DESCRIPTION.encode('utf-8'), tobytes(docs[0]))
		self.assertEqual(measurements_of(reparse(docs[0]))[0].description, NON_ASCII_DESCRIPTION)
		self.assertIn(NON_ASCII_LABEL.encode('utf-8'), tobytes(docs[2]))

	def test_non_ascii_header_text(self):
		model = new_model([arrow_axial()])
		doc = roundtrip(model.create_sr(record_instances=True, series_headers_kwargs={'description': 'Série'}))
		self.assertEqual(doc.SpecificCharacterSet, DCM_CHARACTER_SET_UTF8)

	def test_all_ascii_document_unchanged(self):
		doc = written([arrow_axial(description='Plain')], measurements=[length_patient()])
		self.assertNotIn('SpecificCharacterSet', doc)
		self.assertFalse(srdoc_has_non_ascii_text(doc))
		model = reparse(doc, strict=True)
		again = roundtrip(model.create_sr())
		self.assertNotIn('SpecificCharacterSet', again)
		self.assert_same_content(doc, again)

	def test_existing_fixture_byte_identical(self):
		path = os.path.join(FIXTURES, 'sonador-io.length.2026-1111.dcm')
		with open(path, 'rb') as f:
			original = f.read()
		doc = read(path)
		self.assertFalse(srdoc_has_non_ascii_text(doc))
		self.assertEqual(tobytes(srdoc_apply_character_set(read(path))), tobytes(doc))
		self.assertEqual(tobytes(srdoc_apply_character_set(read(path))), original)
		# Re-saved through the model, the content tree and the character set are unchanged
		again = roundtrip(reparse(doc).create_sr())
		self.assertEqual(again.get('SpecificCharacterSet'), doc.get('SpecificCharacterSet'))
		self.assert_same_content(doc, again)
