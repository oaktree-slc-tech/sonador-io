'''	Offline tests of the circle measurement (Cornerstone `CircleROI`) at Sonador-SR 0.3, and of the
	measurement display colour roles `Line` and `Text`.

	A circle is written as three NUM items, Area (`mm2`), Perimeter (`mm`) and Radius (`mm`), each
	`INFERRED FROM` its own copy of the geometry: an image-domain `CIRCLE` (center, circumference point)
	or a patient-domain `ELLIPSE` (two perpendicular diameters of equal length). The per-measurement
	properties are children of the Area NUM; a three-point circle carries its construction points there.

	Fixtures (`ftests/test_sr/fixtures`, synthetic study, Python-written ones regenerated with
	`UPDATE_SR_FIXTURES=1`): `sonador-io.circle-image`, `circle-axial`, `circle-oblique`, `circle-three-point`,
	`display-line-text`, `display-line` and `display-mixed`, each `.2026-1111.dcm`.
	`cornerstone-adapters.circle-image.2026-1111.dcm` and `cornerstone-adapters.circle-patient.2026-1111.dcm`
	are written by the stock `@cornerstonejs/adapters` 4.22.13 `CircleROI` writer (the Sonador Viewer's locked
	version) from synthetic annotation state, by the jest harness in `fixtures/generators/cornerstone-adapters`:
	an image-domain `CIRCLE` and the undefined `CIRCLE` SCOORD3D the stock writer produces in the patient domain.
	`sonador-viewer.circle-image.2026-1111.dcm` and `sonador-viewer.circle-oblique.2026-1111.dcm` are written by
	the Sonador Viewer (copied unchanged from its `platform/core/src/DICOMSR/__fixtures__`) over a synthetic CT.

	Pixel statistics: `sonador-io.circle-statistics-ct.2026-1112.dcm` (quantity form, patient domain) and
	`sonador-io.circle-statistics-stock.2026-1112.dcm` (stock Cornerstone3D form, image domain) hold a circle of
	this client with three statistic NUM items added the way a Viewer writes them.
	`sonador-viewer.circle-statistics.2026-1113.dcm` is written by the Sonador Viewer (copied unchanged from its
	`platform/core/src/DICOMSR/__fixtures__`): an image-domain circle of radius 6 mm inside the 400 HU cylinder of
	its synthetic CT, with Mean, Std dev and Max in the quantity form.

	Run from the directory containing the `sonador` package: python -m unittest sonador.ftests.test_sr.sr_circle
'''
import copy, logging, math, os, unittest
from collections import OrderedDict

import numpy as np

from pydicom.dataset import Dataset
from pydicom.sequence import Sequence
from pydicom.valuerep import DS

from highdicom.sr.templates import Code

from ...apisettings.base import CircleMeasurement, LengthMeasurement, AngleMeasurement, Measurement, Geometry, ImageFrameGeometry, \
	ImageTransformMatrix, Finding, measurement_class_for_type, SONADOR_MEASUREMENT_TYPE_CIRCLE, DCMSR_CONCEPT_AREA, DCMSR_CONCEPT_PERIMETER, \
	DCMSR_CONCEPT_RADIUS, CircleStatistics
from ...apisettings.display_attrs import DisplayAttributes, DISPLAY_COLOR_ROLES
from ...apisettings.sr import dcm_code_value, SRDiagnosticError, sr_diagnostics_with, SR_DIAGNOSTIC_INFO, SR_DIAGNOSTIC_WARNING, \
	SR_CONDITION_LEGACY_CIRCLE, SR_CONDITION_DEGENERATE_GEOMETRY, SR_CONDITION_MEASUREMENT_VALUE_MISMATCH, SR_CONDITION_MEASUREMENT_READ_AS_GENERIC, \
	SR_CONDITION_INVALID_DRAWING_PLANE, SR_CONDITION_MEASUREMENT_VALUE_UNVERIFIED, SR_CONDITION_GROUP_LEVEL_PROPERTIES, \
	SR_CONDITION_INVALID_GEOMETRY, SR_CONDITION_QUALIFIER_DROPPED, SR_CONDITION_STATISTICS_DROPPED, SR_CONDITION_CONSTRUCTION_POINTS_DROPPED
from ...apisettings.sr.seriestag import measurement_type_for_tracking_id, tracking_id_for_measurement_type, DCMSR_MEASUREMENT_DESCRIPTION, \
	DCMSR_MEASUREMENT_TEXT, DCMSR_DISPLAY_ATTRIBUTES, DCMSR_DISPLAY_COLOR, DCMSR_DISPLAY_COLOR_ROLES, DCMSR_SOP_CLASS_COMPREHENSIVE_3D
from ...apisettings.sr.measurements import DCMSR_MEASUREMENT_CONSTRUCTION_POINTS, DCMSR_CIRCLE_CONCEPTS, DCMSR_MEASUREMENT_VIEW, \
	DCMSR_MEASUREMENT_SOURCE_SERIES, DCMSR_MEASUREMENT_DRAWING_PLANE, drawing_plane_failed_check, DCMSR_DERIVATION, DCMSR_CIRCLE_STATISTICS
from ...imaging.helpers.sr import SonadorImagingMeasurementReport
from ...imaging.helpers.sr.parse import srdoc_measurement_groups, srdoc_children
from ...imaging.orthanc.sr import dcmsr_local_series
from .sr_seriestag import read, roundtrip, new_model, synthetic_reference, named, Node, compare_nodes, io_findings, SYNTHETIC, FIXTURES, \
	evidence_dataset, PROCEDURE
from .sr_measurements import FOR_UID, CALIBRATION, COLORS, model_of, written, reparse, measurements_of, group_of, conditions, text_item, \
	length_patient, length_image, angle_patient, sagittal_length, OBLIQUE_WIDTH, OBLIQUE_HEIGHT, SECOND_SERIES_UID, two_series_evidence, \
	children_named


__all__ = ['CircleRegistryTestCase', 'CircleModelTestCase', 'CircleWriterTestCase', 'CircleReaderTestCase', 'CircleAdapterFixtureTestCase',
	'CircleFixtureTestCase', 'CircleDocumentTestCase', 'DisplayColorRoleTestCase', 'CircleUnitsAndEditsTestCase', 'CircleReadFidelityTestCase',
	'CircleViewerFixtureTestCase', 'CircleStatisticsTestCase', 'CircleViewerStatisticsTestCase', 'MeasurementValueKeyTestCase',
	'CircleConstructionPointsDroppedTestCase']


TRACKING = '2.25.9901'
CIRCLE_ID = 'cornerstoneTools@^4.0.0:CircleROI'
CIRCLE_ID_3D = 'Cornerstone3DTools@^0.1.0:CircleROI'
AXIAL = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0))
LINE_TEXT = {'Line': (200, 30, 40), 'Text': (250, 250, 10)}
SERIES_TAG_ROLES = OrderedDict((('Background', '#FFFFFF'), ('Border', '#FDF1F0'), ('Value', '#DC3545'), ('Meaning', '#FDF1F0')))


def setUpModule():
	logging.getLogger('sonador').setLevel(logging.CRITICAL)


def tearDownModule():
	logging.getLogger('sonador').setLevel(logging.NOTSET)


def circle_axial(**kwargs):
	kwargs.setdefault('frame_of_reference_uid', FOR_UID)
	kwargs.setdefault('tracking_uid', TRACKING)
	return CircleMeasurement.from_center_radius((10.0, 20.0, 30.0), 5.0, normal=(0.0, 0.0, 1.0), **kwargs)


def circle_image(**kwargs):
	kwargs.setdefault('calibration', CALIBRATION)
	kwargs.setdefault('tracking_uid', TRACKING)
	return CircleMeasurement.from_center_point((20.0, 20.0), (30.0, 20.0), reference=synthetic_reference(), **kwargs)


def nums_of(group):
	return [c for c in srdoc_children(group) if c.get('ValueType') == 'NUM']


def num_value(item):
	measured = item.MeasuredValueSequence[0]
	return float(measured.NumericValue), measured.MeasurementUnitsCodeSequence[0].CodeValue


def coordinates_of(item):
	return [c for c in srdoc_children(item) if c.get('ValueType') in ('SCOORD', 'SCOORD3D')]


def geometry_trace(item, path='root'):
	'''	Every coordinate item below a content item, in document order: (path, value type, graphic type, data)
	'''
	found = []
	for idx, child in enumerate(srdoc_children(item)):
		here = '%s/%d' % (path, idx)
		if child.get('ValueType') in ('SCOORD', 'SCOORD3D'):
			found.append((here, child.ValueType, str(child.GraphicType), tuple(float(v) for v in child.GraphicData)))
		found.extend(geometry_trace(child, here))
	return found


class OracleMixin:
	def assert_same_content(self, source, resaved):
		'''	Content-tree oracle on the measurement groups (values, codes, colours within 1 per channel,
			unknown children under their parents), plus the coordinates of every geometry item.
		'''
		src_groups, out_groups = srdoc_measurement_groups(source), srdoc_measurement_groups(resaved)
		self.assertEqual(len(src_groups), len(out_groups))
		for src, out in zip(src_groups, out_groups):
			compare_nodes(self, Node(src), Node(out), 'group')
			# Coordinates compared as a multiset: a re-save may write children in its own order
			expected, actual = sorted(e[1:] for e in geometry_trace(src)), sorted(a[1:] for a in geometry_trace(out))
			self.assertEqual([e[:2] for e in expected], [a[:2] for a in actual])
			for e, a in zip(expected, actual):
				np.testing.assert_allclose(e[2], a[2], atol=1e-4)


class CircleRegistryTestCase(unittest.TestCase):
	def test_identifier_routes_to_the_circle(self):
		self.assertEqual(tracking_id_for_measurement_type(SONADOR_MEASUREMENT_TYPE_CIRCLE), CIRCLE_ID)
		for identifier in (CIRCLE_ID, CIRCLE_ID_3D):
			self.assertEqual(measurement_type_for_tracking_id(identifier), SONADOR_MEASUREMENT_TYPE_CIRCLE)
		self.assertIs(measurement_class_for_type(SONADOR_MEASUREMENT_TYPE_CIRCLE), CircleMeasurement)

	def test_concepts(self):
		self.assertEqual([(c[0].value, c[0].scheme_designator, c[0].meaning) for c in DCMSR_CIRCLE_CONCEPTS.values()],
			[('42798000', 'SCT', 'Area'), ('131191004', 'SCT', 'Perimeter'), ('131190003', 'SCT', 'Radius')])
		self.assertEqual([(c[1].value, c[1].scheme_designator) for c in DCMSR_CIRCLE_CONCEPTS.values()],
			[('G-A166', 'SRT'), ('G-A197', 'SRT'), ('G-A196', 'SRT')])
		code = DCMSR_MEASUREMENT_CONSTRUCTION_POINTS
		self.assertEqual((code.value, code.scheme_designator, code.scheme_version), ('Sonador-Measurement.ConstructionPoints', 'Sonador-SR', '0.3'))
		self.assertTrue(code.meaning)


class CircleModelTestCase(unittest.TestCase):
	def test_center_radius_axial(self):
		m = circle_axial()
		self.assertEqual(m.center, (10.0, 20.0, 30.0))
		self.assertAlmostEqual(m.radius, 5.0)
		self.assertEqual(m.normal, (0.0, 0.0, 1.0))
		self.assertEqual(m.u, (1.0, 0.0, 0.0))
		self.assertEqual(m.edge, (15.0, 20.0, 30.0))
		self.assertAlmostEqual(m.area, math.pi * 25.0)
		self.assertAlmostEqual(m.perimeter, 10.0 * math.pi)
		self.assertEqual(m.geometry.graphic_type, 'ELLIPSE')
		np.testing.assert_allclose(m.points, ((5, 20, 30), (15, 20, 30), (10, 15, 30), (10, 25, 30)))
		self.assertIsNone(m.construction_points)

	def test_first_axis_falls_back_to_y_near_the_x_axis(self):
		m = CircleMeasurement.from_center_radius((0, 0, 0), 2.0, normal=(1.0, 0.0, 0.01), frame_of_reference_uid=FOR_UID)
		u = np.asarray(m.u)
		self.assertAlmostEqual(abs(float(np.dot(u, (0, 1, 0)))), 1.0, places=6)
		self.assertAlmostEqual(float(np.dot(u, m.normal)), 0.0, places=9)

	def test_normal_from_the_drawing_plane_and_the_conflict_check(self):
		m = CircleMeasurement.from_center_radius((0, 0, 0), 3.0, drawing_plane=((0, 1, 0), (0, 0, -1)), frame_of_reference_uid=FOR_UID)
		self.assertAlmostEqual(abs(m.normal[0]), 1.0)
		self.assertEqual(m.drawing_plane, ((0.0, 1.0, 0.0), (0.0, 0.0, -1.0)))
		with self.assertRaises(ValueError):
			CircleMeasurement.from_center_radius((0, 0, 0), 3.0, normal=(0, 0, 1), drawing_plane=((0, 1, 0), (0, 0, -1)), frame_of_reference_uid=FOR_UID)
		# Within 1 degree: the normal wins
		tilt = math.radians(0.5)
		m = CircleMeasurement.from_center_radius((0, 0, 0), 3.0, normal=(0, math.sin(tilt), math.cos(tilt)), drawing_plane=AXIAL, frame_of_reference_uid=FOR_UID)
		self.assertAlmostEqual(m.normal[1], math.sin(tilt))
		with self.assertRaises(ValueError):
			CircleMeasurement.from_center_point((0, 0, 0), (3, 0, 0), frame_of_reference_uid=FOR_UID)

	def test_center_point(self):
		m = CircleMeasurement.from_center_point((0, 0, 0), (3, 4, 2), normal=(0, 0, 1), frame_of_reference_uid=FOR_UID)
		self.assertAlmostEqual(m.radius, 5.0)
		np.testing.assert_allclose(m.u, (0.6, 0.8, 0.0))
		np.testing.assert_allclose(m.edge, (3, 4, 0), atol=1e-12)
		image = circle_image()
		self.assertEqual((image.center, image.edge, image.normal), ((20.0, 20.0), (30.0, 20.0), None))
		self.assertAlmostEqual(image.radius, 5.0)

	def test_three_points(self):
		m = CircleMeasurement.from_points((0, 0, 0), (10, 0, 0), (0, 10, 0), frame_of_reference_uid=FOR_UID)
		np.testing.assert_allclose(m.center, (5, 5, 0))
		self.assertAlmostEqual(m.radius, math.sqrt(50.0))
		self.assertEqual(m.normal, (0.0, 0.0, 1.0))
		np.testing.assert_allclose(m.u, np.array((-5.0, -5.0, 0.0)) / math.sqrt(50.0))
		self.assertEqual(m.construction_points, ((0.0, 0.0, 0.0), (10.0, 0.0, 0.0), (0.0, 10.0, 0.0)))
		flat = CircleMeasurement.from_points((0, 0), (10, 0), (0, 10), reference=synthetic_reference(), calibration=CALIBRATION)
		self.assertEqual(flat.center, (5.0, 5.0))

	def test_degenerate_inputs_refused(self):
		with self.assertRaises(ValueError):
			CircleMeasurement.from_center_radius((0, 0, 0), 0.005, normal=(0, 0, 1), frame_of_reference_uid=FOR_UID)
		with self.assertRaises(ValueError):
			CircleMeasurement.from_points((0, 0, 0), (5, 0.005, 0), (10, 0, 0), frame_of_reference_uid=FOR_UID)
		with self.assertRaises(ValueError):
			CircleMeasurement.from_center_point((0, 0), (0.001, 0))

	def test_rigid_transform_moves_center_normal_and_plane(self):
		rotation = [[0, -1, 0, 10], [1, 0, 0, 20], [0, 0, 1, 30], [0, 0, 0, 1]]
		transform = ImageTransformMatrix(rotation, source_frame='planning', target_frame=FOR_UID)
		m = CircleMeasurement.from_center_radius((1, 0, 0), 2.0, normal=(1, 0, 0), drawing_plane=((0, 1, 0), (0, 0, 1)), transform=transform)
		np.testing.assert_allclose(m.center, (10, 21, 30), atol=1e-9)
		np.testing.assert_allclose(m.normal, (0, 1, 0), atol=1e-9)
		np.testing.assert_allclose(m.drawing_plane, ((-1, 0, 0), (0, 0, 1)), atol=1e-9)
		self.assertEqual(m.frame_of_reference_uid, FOR_UID)

	def test_non_rigid_transform_refused(self):
		scale = ImageTransformMatrix([[2, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]], source_frame='planning', target_frame=FOR_UID)
		for build in (lambda: CircleMeasurement.from_center_radius((0, 0, 0), 2.0, normal=(0, 0, 1), transform=scale),
				lambda: CircleMeasurement.from_points((0, 0, 0), (2, 0, 0), (0, 2, 0), transform=scale)):
			with self.assertRaises(ValueError) as ctx:
				build()
			self.assertIn('orthonormal rotation', str(ctx.exception))

	def test_edits_recompute(self):
		m = circle_axial()
		m.radius = 7.0
		self.assertAlmostEqual(m.area, 49.0 * math.pi)
		self.assertEqual(m.center, (10.0, 20.0, 30.0))
		m.center = (0.0, 0.0, 0.0)
		np.testing.assert_allclose(m.edge, (7, 0, 0))
		m.normal = (0, 1, 0)
		np.testing.assert_allclose(m.normal, (0, 1, 0), atol=1e-12)
		self.assertAlmostEqual(m.radius, 7.0)
		with self.assertRaises(ValueError):
			circle_image().normal = (0, 0, 1)
		with self.assertRaises(ValueError):
			circle_image(drawing_plane=AXIAL)


class CircleWriterTestCase(unittest.TestCase):
	def test_patient_domain_tree(self):
		doc = written([circle_axial(description='Head', views=['axial'], display=DisplayAttributes.from_color(LINE_TEXT))])
		self.assertEqual(str(doc.SOPClassUID), DCMSR_SOP_CLASS_COMPREHENSIVE_3D)
		group = group_of(doc)
		self.assertEqual(srdoc_children(group)[0].TextValue, CIRCLE_ID)
		nums = nums_of(group)
		self.assertEqual([(dcm_code_value(n.ConceptNameCodeSequence[0]), n.ConceptNameCodeSequence[0].CodingSchemeDesignator) for n in nums],
			[('42798000', 'SCT'), ('131191004', 'SCT'), ('131190003', 'SCT')])
		values = [num_value(n) for n in nums]
		self.assertAlmostEqual(values[0][0], 25 * math.pi, places=6)
		self.assertAlmostEqual(values[1][0], 10 * math.pi, places=6)
		self.assertAlmostEqual(values[2][0], 5.0, places=6)
		self.assertEqual([v[1] for v in values], ['mm2', 'mm', 'mm'])
		for n in nums:
			coordinates = coordinates_of(n)
			self.assertEqual(len(coordinates), 1)
			c = coordinates[0]
			self.assertEqual((c.ValueType, c.RelationshipType, c.GraphicType, c.ReferencedFrameOfReferenceUID), ('SCOORD3D', 'INFERRED FROM', 'ELLIPSE', FOR_UID))
			np.testing.assert_allclose(list(c.GraphicData), [5, 20, 30, 15, 20, 30, 10, 15, 30, 10, 25, 30])
		# Properties under the Area NUM only
		self.assertEqual(len(named(srdoc_children(nums[0]), DCMSR_MEASUREMENT_DESCRIPTION.value)), 1)
		self.assertEqual(len(named(srdoc_children(nums[0]), DCMSR_DISPLAY_ATTRIBUTES.value)), 1)
		for n in nums[1:]:
			self.assertEqual([c.ValueType for c in srdoc_children(n)], ['SCOORD3D'])

	def test_image_domain_tree(self):
		doc = written([circle_image()])
		for n in nums_of(group_of(doc)):
			c = coordinates_of(n)[0]
			self.assertEqual((c.ValueType, c.GraphicType, list(c.GraphicData)), ('SCOORD', 'CIRCLE', [20.0, 20.0, 30.0, 20.0]))
			image = srdoc_children(c)[0]
			self.assertEqual((image.ValueType, image.RelationshipType), ('IMAGE', 'SELECTED FROM'))
			self.assertEqual(image.ReferencedSOPSequence[0].ReferencedSOPInstanceUID, SYNTHETIC['SOPInstanceUID'])

	def test_construction_points_under_the_area_num(self):
		m = CircleMeasurement.from_points((0, 0, 0), (10, 0, 0), (0, 10, 0), frame_of_reference_uid=FOR_UID, tracking_uid=TRACKING)
		area = nums_of(group_of(written([m])))[0]
		item = named(srdoc_children(area), DCMSR_MEASUREMENT_CONSTRUCTION_POINTS.value)[0]
		self.assertEqual((item.ValueType, item.RelationshipType, item.GraphicType, item.ReferencedFrameOfReferenceUID),
			('SCOORD3D', 'HAS PROPERTIES', 'POLYLINE', FOR_UID))
		self.assertEqual(list(item.GraphicData), [0, 0, 0, 10, 0, 0, 0, 10, 0])
		flat = CircleMeasurement.from_points((0, 0), (10, 0), (0, 10), reference=synthetic_reference(), calibration=CALIBRATION, tracking_uid=TRACKING)
		item = named(srdoc_children(nums_of(group_of(written([flat])))[0]), DCMSR_MEASUREMENT_CONSTRUCTION_POINTS.value)[0]
		self.assertEqual((item.ValueType, item.GraphicType, srdoc_children(item)[0].ValueType), ('SCOORD', 'POLYLINE', 'IMAGE'))
		self.assertEqual(measurements_of(reparse(written([flat])))[0].construction_points, ((0.0, 0.0), (10.0, 0.0), (0.0, 10.0)))

	def test_provenance_under_the_area_num(self):
		doc = roundtrip(new_model([], measurements=[circle_axial(series_uid=SECOND_SERIES_UID, drawing_plane=AXIAL, views=['axial'])],
			dcm_evidence=two_series_evidence()).create_sr(record_instances=True))
		area = nums_of(group_of(doc))[0]
		self.assertEqual([i.UID for i in children_named(area, DCMSR_MEASUREMENT_SOURCE_SERIES)], [SECOND_SERIES_UID])
		self.assertEqual([i.TextValue for i in children_named(area, DCMSR_MEASUREMENT_DRAWING_PLANE)], ['1,0,0,0,1,0'])

	def test_drawing_plane_must_hold_the_circle_plane(self):
		tilted = ((1.0, 0.0, 0.0), (0.0, math.cos(math.radians(3)), math.sin(math.radians(3))))
		m = circle_axial()
		m.drawing_plane = tilted
		self.assertEqual(drawing_plane_failed_check(tilted, m.points, normal=m.normal), 'normal-parallel')
		with self.assertRaises(ValueError) as ctx:
			written([m])
		self.assertIn('normal-parallel', str(ctx.exception))
		# On read: malformed, absent, written back as read
		doc = written([circle_axial(drawing_plane=AXIAL)])
		children_named(nums_of(group_of(doc))[0], DCMSR_MEASUREMENT_DRAWING_PLANE)[0].TextValue = '1,0,0,0,0.998630,0.052336'
		model = reparse(doc)
		self.assertIsNone(measurements_of(model)[0].drawing_plane)
		self.assertIn(SR_CONDITION_INVALID_DRAWING_PLANE, conditions(model.diagnostics))
		again = roundtrip(model.create_sr())
		self.assertEqual([i.TextValue for i in children_named(nums_of(group_of(again))[0], DCMSR_MEASUREMENT_DRAWING_PLANE)], ['1,0,0,0,0.998630,0.052336'])

	def test_degenerate_and_uncalibrated_refused(self):
		m = circle_axial()
		m.points = [(10.0, 20.0, 30.0)] * 4
		with self.assertRaises(SRDiagnosticError):
			written([m])
		with self.assertRaises(SRDiagnosticError):
			written([circle_image(calibration=None)])


class CircleReaderTestCase(OracleMixin, unittest.TestCase):
	def doc(self, measurement=None):
		return written([measurement or circle_axial(description='Head', text='Note', views=['axial'], display=DisplayAttributes.from_color(LINE_TEXT))])

	def test_values_read_independently(self):
		m = measurements_of(reparse(self.doc(), strict=True))[0]
		self.assertIsInstance(m, CircleMeasurement)
		self.assertEqual((m.center, m.normal, m.frame_of_reference_uid, m.tracking_uid), ((10.0, 20.0, 30.0), (0.0, 0.0, 1.0), FOR_UID, TRACKING))
		self.assertAlmostEqual(m.radius, 5.0)
		self.assertAlmostEqual(m.area, 25 * math.pi)
		self.assertAlmostEqual(m.perimeter, 10 * math.pi)
		self.assertEqual(m.unit.value, 'mm')
		self.assertEqual([v.unit.value for v in m.values.values()], ['mm2', 'mm'])
		self.assertEqual((m.description, m.text, m.views), ('Head', 'Note', ('axial',)))
		self.assertEqual(tuple(m.display.color['Line']), LINE_TEXT['Line'])

	def test_unchanged_resave_keeps_everything(self):
		doc = self.doc()
		again = roundtrip(reparse(doc).create_sr())
		self.assert_same_content(doc, again)
		for src, out in zip(nums_of(group_of(doc)), nums_of(group_of(again))):
			self.assertEqual(out.MeasuredValueSequence[0].NumericValue, src.MeasuredValueSequence[0].NumericValue)

	def test_srt_codes_and_meaning_only_concepts_read(self):
		doc = self.doc()
		nums = nums_of(group_of(doc))
		for n, (code, scheme) in zip(nums, (('G-A166', 'SRT'), ('G-A197', 'SRT'), ('X-RAD', 'Vendor'))):
			name = n.ConceptNameCodeSequence[0]
			name.CodeValue, name.CodingSchemeDesignator = code, scheme
		m = measurements_of(reparse(doc))[0]
		self.assertIsInstance(m, CircleMeasurement)
		self.assertAlmostEqual(m.radius, 5.0)
		again = nums_of(group_of(roundtrip(reparse(doc).create_sr())))
		self.assertEqual([dcm_code_value(n.ConceptNameCodeSequence[0]) for n in again], ['G-A166', 'G-A197', 'X-RAD'])

	def test_property_precedence_and_unknown_children_stay_under_their_num(self):
		doc = self.doc(circle_axial())
		area, perimeter, radius = nums_of(group_of(doc))
		perimeter.ContentSequence = Sequence(srdoc_children(perimeter) + [text_item(DCMSR_MEASUREMENT_DESCRIPTION, 'From perimeter'),
			text_item(DCMSR_MEASUREMENT_TEXT, 'Perimeter note')])
		radius.ContentSequence = Sequence(srdoc_children(radius) + [text_item(DCMSR_MEASUREMENT_DESCRIPTION, 'From radius'),
			text_item(DCMSR_MEASUREMENT_VIEW, 'unknown-child-of-radius')])
		view = text_item(DCMSR_MEASUREMENT_VIEW, 'axial')
		view.ConceptNameCodeSequence[0].CodingSchemeVersion = '0.3'
		area.ContentSequence = Sequence(srdoc_children(area) + [view])
		# The Area NUM is first in document order and carries a property: it is the source
		m = measurements_of(reparse(doc))[0]
		self.assertEqual((m.description, m.views), (None, ('axial',)))
		self.assertEqual([i.TextValue for i in m.perimeter_extra_items], ['From perimeter', 'Perimeter note'])
		self.assertEqual([i.TextValue for i in m.radius_extra_items], ['From radius', 'unknown-child-of-radius'])
		again = roundtrip(reparse(doc).create_sr())
		self.assert_same_content(doc, again)

		# Without properties on the Area NUM, the first NUM carrying any is the source
		area.ContentSequence = Sequence([c for c in srdoc_children(area) if c.get('ValueType') == 'SCOORD3D'])
		m = measurements_of(reparse(doc))[0]
		self.assertEqual((m.description, m.text), ('From perimeter', 'Perimeter note'))
		self.assertEqual([i.TextValue for i in m.radius_extra_items], ['From radius', 'unknown-child-of-radius'])
		out = nums_of(group_of(roundtrip(reparse(doc).create_sr())))
		self.assertEqual([i.TextValue for i in named(srdoc_children(out[0]), DCMSR_MEASUREMENT_DESCRIPTION.value)], ['From perimeter'])
		self.assertEqual([i.TextValue for i in named(srdoc_children(out[2]), DCMSR_MEASUREMENT_DESCRIPTION.value)], ['From radius'])

	def test_group_level_properties_read(self):
		doc = self.doc(circle_axial())
		group = group_of(doc)
		group.ContentSequence = Sequence(srdoc_children(group) + [text_item(DCMSR_MEASUREMENT_DESCRIPTION, 'Legacy label', 'HAS CONCEPT MOD')])
		model = reparse(doc)
		self.assertEqual(measurements_of(model)[0].description, 'Legacy label')
		self.assertIn(SR_CONDITION_GROUP_LEVEL_PROPERTIES, conditions(model.diagnostics))

	def test_stored_values_authoritative_with_tolerances(self):
		doc = self.doc(circle_axial())
		area, perimeter, radius = nums_of(group_of(doc))
		area.MeasuredValueSequence[0].NumericValue = 25 * math.pi + 0.04
		del area.MeasuredValueSequence[0].FloatingPointValue
		model = reparse(doc)
		self.assertNotIn(SR_CONDITION_MEASUREMENT_VALUE_MISMATCH, conditions(model.diagnostics))
		area.MeasuredValueSequence[0].NumericValue = 25 * math.pi + 0.06
		radius.MeasuredValueSequence[0].NumericValue = 5.02
		if 'FloatingPointValue' in radius.MeasuredValueSequence[0]:
			del radius.MeasuredValueSequence[0].FloatingPointValue
		model = reparse(doc)
		mismatches = sr_diagnostics_with(model.diagnostics, condition=SR_CONDITION_MEASUREMENT_VALUE_MISMATCH)
		self.assertEqual(sorted(d.severity for d in mismatches), ['info', 'info'])
		m = measurements_of(model)[0]
		self.assertAlmostEqual(m.radius, 5.02)
		self.assertAlmostEqual(m.area, 25 * math.pi + 0.06, places=6)
		# Written back verbatim while the geometry is unchanged
		out = nums_of(group_of(roundtrip(model.create_sr())))
		self.assertAlmostEqual(num_value(out[2])[0], 5.02)
		# Area tolerance is relative for a large circle: 1 part in 10 000
		big = written([CircleMeasurement.from_center_radius((0, 0, 0), 100.0, normal=(0, 0, 1), frame_of_reference_uid=FOR_UID)])
		nums_of(group_of(big))[0].MeasuredValueSequence[0].NumericValue = math.pi * 10000 + 2.0
		del nums_of(group_of(big))[0].MeasuredValueSequence[0].FloatingPointValue
		self.assertNotIn(SR_CONDITION_MEASUREMENT_VALUE_MISMATCH, conditions(reparse(big).diagnostics))

	def test_edit_recomputes_all_three(self):
		model = reparse(self.doc(circle_axial()))
		m = measurements_of(model)[0]
		m.radius = 6.0
		values = [num_value(n)[0] for n in nums_of(group_of(roundtrip(model.create_sr())))]
		np.testing.assert_allclose(values, [36 * math.pi, 12 * math.pi, 6.0])

	def test_missing_radius_num_written_back_computed(self):
		doc = self.doc(circle_axial())
		group = group_of(doc)
		radius = nums_of(group)[2]
		group.ContentSequence = Sequence([c for c in srdoc_children(group) if c is not radius])
		area_value = nums_of(group)[0].MeasuredValueSequence[0].NumericValue
		model = reparse(doc)
		m = measurements_of(model)[0]
		self.assertAlmostEqual(m.radius, 5.0)
		out = nums_of(group_of(roundtrip(model.create_sr())))
		self.assertEqual([dcm_code_value(n.ConceptNameCodeSequence[0]) for n in out], ['42798000', '131191004', '131190003'])
		self.assertEqual(out[0].MeasuredValueSequence[0].NumericValue, area_value)
		self.assertAlmostEqual(num_value(out[2])[0], 5.0)

	def test_degenerate_read_with_a_warning(self):
		doc = self.doc(circle_axial())
		for n in nums_of(group_of(doc)):
			coordinates_of(n)[0].GraphicData = [10, 20, 30] * 4
		model = reparse(doc)
		self.assertNotIsInstance(measurements_of(model)[0], CircleMeasurement)
		self.assertIn(SR_CONDITION_MEASUREMENT_READ_AS_GENERIC, conditions(model.diagnostics))
		doc = self.doc(circle_axial())
		for n in nums_of(group_of(doc)):
			coordinates_of(n)[0].GraphicData = [9.996, 20, 30, 10.004, 20, 30, 10, 19.996, 30, 10, 20.004, 30]
		model = reparse(doc)
		m = measurements_of(model)[0]
		self.assertIsInstance(m, CircleMeasurement)
		self.assertIn(SR_CONDITION_DEGENERATE_GEOMETRY, conditions(sr_diagnostics_with(model.diagnostics, severity=SR_DIAGNOSTIC_WARNING)))
		with self.assertRaises(SRDiagnosticError):
			model.create_sr()

	def test_ellipse_that_is_not_a_circle_is_generic(self):
		doc = self.doc(circle_axial())
		for n in nums_of(group_of(doc)):
			coordinates_of(n)[0].GraphicData = [5, 20, 30, 15, 20, 30, 10, 17, 30, 10, 23, 30]
		model = reparse(doc)
		m = measurements_of(model)[0]
		self.assertNotIsInstance(m, CircleMeasurement)
		self.assertIn(SR_CONDITION_MEASUREMENT_READ_AS_GENERIC, conditions(model.diagnostics))
		self.assert_same_content(doc, roundtrip(model.create_sr()))

	def test_collinear_construction_points_warned(self):
		m = CircleMeasurement.from_points((0, 0, 0), (10, 0, 0), (0, 10, 0), frame_of_reference_uid=FOR_UID, tracking_uid=TRACKING)
		doc = written([m])
		named(srdoc_children(nums_of(group_of(doc))[0]), DCMSR_MEASUREMENT_CONSTRUCTION_POINTS.value)[0].GraphicData = [0, 0, 0, 5, 0, 0, 10, 0, 0]
		model = reparse(doc)
		self.assertIn(SR_CONDITION_DEGENERATE_GEOMETRY, conditions(model.diagnostics))
		with self.assertRaises(SRDiagnosticError):
			model.create_sr()

	def test_deletion_and_repeated_saves(self):
		doc = written([circle_axial(), length_patient(tracking_uid='2.25.9902')])
		first = roundtrip(reparse(doc).create_sr())
		second = roundtrip(reparse(first).create_sr())
		self.assert_same_content(first, second)
		model = reparse(second)
		model.groups = [g for g in model.groups if not isinstance((g.measurements or [None])[0], CircleMeasurement)]
		third = roundtrip(model.create_sr())
		self.assertEqual([type(m).__name__ for m in measurements_of(reparse(third))], ['LengthMeasurement'])


class CircleAdapterFixtureTestCase(unittest.TestCase):
	'''	Documents written by the stock Cornerstone3D `CircleROI` adapter (the Viewer's locked version)
	'''
	IMAGE = os.path.join(FIXTURES, 'cornerstone-adapters.circle-image.2026-1111.dcm')
	PATIENT = os.path.join(FIXTURES, 'cornerstone-adapters.circle-patient.2026-1111.dcm')
	# Calibration of the synthetic frame the adapter fixture was written for: origin, axis-aligned, 1 mm pixels
	FRAME = {SYNTHETIC['SOPInstanceUID']: ImageFrameGeometry((0.0, 0.0, 0.0), (1.0, 0.0, 0.0, 0.0, 1.0, 0.0), (1.0, 1.0))}

	def test_image_domain(self):
		doc = read(self.IMAGE)
		# The stock writer: Perimeter first, then Area and Radius, geometry under the Perimeter NUM only
		nums = nums_of(group_of(doc))
		self.assertEqual([dcm_code_value(n.ConceptNameCodeSequence[0]) for n in nums], ['131191004', '42798000', '131190003'])
		self.assertEqual([len(coordinates_of(n)) for n in nums], [1, 0, 0])
		model = SonadorImagingMeasurementReport.from_sr(doc, calibrations=self.FRAME, strict=True)
		m = measurements_of(model)[0]
		self.assertIsInstance(m, CircleMeasurement)
		self.assertEqual((m.tracking_id, m.center, m.edge, m.normal), (CIRCLE_ID_3D, (20.0, 20.0), (25.0, 20.0), None))
		self.assertAlmostEqual(m.radius, 5.0)
		self.assertAlmostEqual(m.area, 25 * math.pi)
		self.assertAlmostEqual(m.perimeter, 10 * math.pi)
		self.assertEqual(m.reference.sop_instance_uid, SYNTHETIC['SOPInstanceUID'])
		self.assertFalse(sr_diagnostics_with(model.diagnostics, condition=SR_CONDITION_MEASUREMENT_VALUE_MISMATCH))
		# Rewritten in this client's form: Area, Perimeter, Radius, each with the geometry; values kept
		again = roundtrip(model.create_sr())
		out = nums_of(group_of(again))
		self.assertEqual([dcm_code_value(n.ConceptNameCodeSequence[0]) for n in out], ['42798000', '131191004', '131190003'])
		self.assertEqual([len(coordinates_of(n)) for n in out], [1, 1, 1])
		self.assertEqual([n.MeasuredValueSequence[0].NumericValue for n in out],
			[nums[1].MeasuredValueSequence[0].NumericValue, nums[0].MeasuredValueSequence[0].NumericValue, nums[2].MeasuredValueSequence[0].NumericValue])
		self.assertEqual(srdoc_children(group_of(again))[0].TextValue, CIRCLE_ID_3D)

	def test_patient_domain_legacy_circle(self):
		doc = read(self.PATIENT)
		model = SonadorImagingMeasurementReport.from_sr(doc)
		m = measurements_of(model)[0]
		self.assertIsInstance(m, CircleMeasurement)
		self.assertTrue(m.is_legacy)
		self.assertEqual((m.center, m.normal, m.frame_of_reference_uid), ((10.0, 20.0, 30.0), None, FOR_UID))
		self.assertAlmostEqual(m.radius, 5.0)
		self.assertEqual([d.severity for d in sr_diagnostics_with(model.diagnostics, condition=SR_CONDITION_LEGACY_CIRCLE)], ['warning'])
		with self.assertRaises(SRDiagnosticError):
			SonadorImagingMeasurementReport.from_sr(doc, strict=True)
		with self.assertRaises(ValueError) as ctx:
			model.create_sr()
		self.assertIn('normal', str(ctx.exception))
		m.normal = (0.0, 0.0, 1.0)
		again = SonadorImagingMeasurementReport.from_sr(roundtrip(model.create_sr()), strict=True)
		circle = measurements_of(again)[0]
		self.assertEqual(circle.geometry.graphic_type, 'ELLIPSE')
		self.assertEqual((circle.center, circle.normal, circle.u), ((10.0, 20.0, 30.0), (0.0, 0.0, 1.0), (1.0, 0.0, 0.0)))
		self.assertAlmostEqual(circle.radius, 5.0)
		self.assertAlmostEqual(circle.area, 25 * math.pi)


FIXTURE_DATE = '2026-1111'


def fixture_path(topic):
	return os.path.join(FIXTURES, 'sonador-io.%s.%s.dcm' % (topic, FIXTURE_DATE))


def fixture_properties(topic, view, **kwargs):
	values = dict(description='%s measurement' % topic, text='Synthetic', location='Synthetic location', views=[view],
		display=DisplayAttributes.from_color(LINE_TEXT), tracking_uid='2.25.8300')
	values.update(kwargs)
	return values


def fixture_measurements(topic):
	'''	The content of each Python-written fixture
	'''
	if topic == 'circle-image':
		return [circle_image(**fixture_properties(topic, 'axial'))], []
	if topic == 'circle-axial':
		return [circle_axial(**fixture_properties(topic, 'axial', series_uid=SYNTHETIC['SeriesInstanceUID'], drawing_plane=AXIAL))], []
	if topic == 'circle-oblique':
		return [CircleMeasurement.from_center_radius((10.0, 20.0, 30.0), 8.0, normal=(1.0, 1.0, 1.0), frame_of_reference_uid=FOR_UID,
			**fixture_properties(topic, 'oblique', series_uid=SYNTHETIC['SeriesInstanceUID'], drawing_plane=(OBLIQUE_WIDTH, OBLIQUE_HEIGHT)))], []
	if topic == 'circle-three-point':
		return [CircleMeasurement.from_points((0.0, 0.0, 0.0), (10.0, 0.0, 0.0), (0.0, 10.0, 0.0), frame_of_reference_uid=FOR_UID,
			**fixture_properties(topic, 'axial', series_uid=SYNTHETIC['SeriesInstanceUID'], drawing_plane=AXIAL))], []
	if topic == 'display-line-text':
		return [length_image(**fixture_properties(topic, 'axial'))], []
	if topic == 'display-line':
		return [length_image(**fixture_properties(topic, 'axial', display=DisplayAttributes.from_color({'Line': LINE_TEXT['Line']})))], []
	if topic == 'display-mixed':
		tag = io_findings()[0]
		tag.display = dict(SERIES_TAG_ROLES)
		return [length_image(**fixture_properties(topic, 'axial'))], [tag]
	raise KeyError(topic)


def write_fixture(topic):
	measurements, findings = fixture_measurements(topic)
	doc = new_model(findings, measurements=measurements).create_sr(record_instances=True, dcmsr_series_uid='2.25.8000', dcmsr_instance_uid='2.25.8001')
	doc.save_as(fixture_path(topic))


class CircleFixtureTestCase(OracleMixin, unittest.TestCase):
	# topic -> (domain, center, radius, normal, area, perimeter, Drawing-Plane text, view, construction points)
	CIRCLES = OrderedDict((
		('circle-image', ('image', (20.0, 20.0), 5.0, None, 25 * math.pi, 10 * math.pi, None, 'axial', None)),
		('circle-axial', ('patient', (10.0, 20.0, 30.0), 5.0, (0.0, 0.0, 1.0), 25 * math.pi, 10 * math.pi, '1,0,0,0,1,0', 'axial', None)),
		('circle-oblique', ('patient', (10.0, 20.0, 30.0), 8.0, tuple(np.ones(3) / math.sqrt(3)), 64 * math.pi, 16 * math.pi,
			'0.707107,-0.707107,0,0.408248,0.408248,-0.816497', 'oblique', None)),
		('circle-three-point', ('patient', (5.0, 5.0, 0.0), math.sqrt(50.0), (0.0, 0.0, 1.0), 50 * math.pi, 2 * math.sqrt(50.0) * math.pi,
			'1,0,0,0,1,0', 'axial', ((0.0, 0.0, 0.0), (10.0, 0.0, 0.0), (0.0, 10.0, 0.0)))),
	))
	DISPLAY = OrderedDict((('display-line-text', ['Line', 'Text']), ('display-line', ['Line']), ('display-mixed', ['Line', 'Text'])))

	def setUp(self):
		if os.environ.get('UPDATE_SR_FIXTURES'):
			for topic in list(self.CIRCLES) + list(self.DISPLAY):
				write_fixture(topic)

	def read_fixture(self, topic):
		doc = read(fixture_path(topic))
		self.assertEqual((doc.PatientID, str(doc.StudyInstanceUID), str(doc.SOPClassUID)), ('ANONYMOUS', '2.25.1001', DCMSR_SOP_CLASS_COMPREHENSIVE_3D))
		return doc

	def test_circle_fixtures(self):
		for topic, (domain, center, radius, normal, area, perimeter, plane, view, construction) in self.CIRCLES.items():
			doc = self.read_fixture(topic)
			model = SonadorImagingMeasurementReport.from_sr(doc, strict=True, calibrations={SYNTHETIC['SOPInstanceUID']: CALIBRATION})
			m = measurements_of(model)[0]
			self.assertIsInstance(m, CircleMeasurement, topic)
			self.assertEqual(m.geometry.domain, domain, topic)
			np.testing.assert_allclose(m.center, center, atol=1e-4, err_msg=topic)
			self.assertAlmostEqual(m.radius, radius, places=4, msg=topic)
			if normal is None:
				self.assertIsNone(m.normal, topic)
			else:
				np.testing.assert_allclose(m.normal, normal, atol=1e-5, err_msg=topic)
				self.assertEqual(m.frame_of_reference_uid, FOR_UID)
				self.assertEqual(m.series_uid, SYNTHETIC['SeriesInstanceUID'])
			self.assertAlmostEqual(m.area, area, places=4, msg=topic)
			self.assertAlmostEqual(m.perimeter, perimeter, places=4, msg=topic)
			self.assertEqual([v.unit.value for v in m.values.values()], ['mm2', 'mm'])
			self.assertEqual((m.drawing_plane_text, m.views, m.construction_points), (plane, (view,), construction), topic)
			self.assertEqual(m.description, '%s measurement' % topic)
			self.assertEqual(list(m.display.color), ['Line', 'Text'])
			self.assertFalse(sr_diagnostics_with(model.diagnostics, condition=SR_CONDITION_MEASUREMENT_VALUE_MISMATCH), topic)
			self.assert_same_content(doc, roundtrip(model.create_sr()))

	def test_display_fixtures(self):
		for topic, roles in self.DISPLAY.items():
			doc = self.read_fixture(topic)
			model = reparse(doc, strict=True)
			m = measurements_of(model)[0]
			self.assertEqual(list(m.display.color), roles, topic)
			self.assertEqual(m.display.color_extra_items, ())
			self.assertEqual(dict(m.display.color), {role: LINE_TEXT[role] for role in roles})
			color = named(srdoc_children(named(srdoc_children(nums_of(group_of(doc, -1))[0]), DCMSR_DISPLAY_ATTRIBUTES.value)[0]), DCMSR_DISPLAY_COLOR.value)[0]
			self.assertEqual([dcm_code_value(c.ConceptNameCodeSequence[0]) for c in srdoc_children(color)],
				['Sonador-DisplayAttributes.Color.%s' % role for role in roles])
			if topic == 'display-mixed':
				finding = [f for g in model.groups for f in (g.findings or [])][0]
				self.assertEqual(dict(finding.display.hex), dict(SERIES_TAG_ROLES))
				tag_color = named(srdoc_children(named(srdoc_children(group_of(doc, 0)), '121071')[0]), DCMSR_DISPLAY_ATTRIBUTES.value)[0]
				self.assertEqual([dcm_code_value(c.ConceptNameCodeSequence[0]).rsplit('.', 1)[1]
					for c in srdoc_children(named(srdoc_children(tag_color), DCMSR_DISPLAY_COLOR.value)[0])], list(SERIES_TAG_ROLES))
			self.assert_same_content(doc, roundtrip(model.create_sr()))


class CircleDocumentTestCase(OracleMixin, unittest.TestCase):
	def test_mixed_document(self):
		measurements = [length_patient(tracking_uid='2.25.9911'), angle_patient(tracking_uid='2.25.9912'), circle_axial(tracking_uid='2.25.9913'),
			circle_image(tracking_uid='2.25.9914')]
		doc = roundtrip(new_model(io_findings(), measurements=measurements).create_sr(record_instances=True))
		model = reparse(doc, strict=True, calibrations={SYNTHETIC['SOPInstanceUID']: CALIBRATION})
		self.assertEqual([type(m).__name__ for m in measurements_of(model)],
			['LengthMeasurement', 'AngleMeasurement', 'CircleMeasurement', 'CircleMeasurement'])
		self.assertEqual(len([f for g in model.groups for f in (g.findings or [])]), 2)
		self.assert_same_content(doc, roundtrip(model.create_sr()))

	def test_retrieval_keys_and_frame_accessor(self):
		doc = written([circle_axial(), circle_image(tracking_uid='2.25.9921')])
		series = dcmsr_local_series([doc])
		by_series = series.measurements(measurement_type=SONADOR_MEASUREMENT_TYPE_CIRCLE)
		self.assertEqual(sorted(by_series.keys()), sorted([SYNTHETIC['SeriesInstanceUID'], series.pk]))
		found = series.measurements(frame_of_reference_uid=FOR_UID, measurement_type=SONADOR_MEASUREMENT_TYPE_CIRCLE)
		self.assertEqual([m.tracking_uid for c in found.values() for m in c], [TRACKING])

	def test_unnamed_preserved_coordinates_stay_unnamed(self):
		'''	An unnamed coordinate item kept as unmodelled content under a NUM is written back unnamed
		'''
		doc = written([length_patient()])
		num = [c for c in srdoc_children(group_of(doc)) if c.get('ValueType') == 'NUM'][0]
		extra = copy.deepcopy(coordinates_of(num)[0])
		extra.RelationshipType = 'HAS PROPERTIES'
		self.assertNotIn('ConceptNameCodeSequence', extra)
		num.ContentSequence = Sequence(srdoc_children(num) + [extra])
		again = roundtrip(reparse(doc).create_sr())
		out = [c for c in srdoc_children(group_of(again)) if c.get('ValueType') == 'NUM'][0]
		self.assertEqual([('ConceptNameCodeSequence' in c) for c in coordinates_of(out)], [False, False])
		self.assert_same_content(doc, again)

	def test_from_sr_on_the_group(self):
		group = group_of(written([circle_axial()]))
		m = CircleMeasurement.from_sr(group)
		self.assertIsInstance(m, CircleMeasurement)
		self.assertAlmostEqual(m.radius, 5.0)


class DisplayColorRoleTestCase(OracleMixin, unittest.TestCase):
	def test_six_roles(self):
		self.assertEqual(DISPLAY_COLOR_ROLES, ('Background', 'Border', 'Value', 'Meaning', 'Line', 'Text'))
		display = DisplayAttributes.from_color({'Line': '#C81E28', 'Text': '#fafa0a', 'Border': (1, 2, 3)})
		self.assertEqual(list(display.color), ['Border', 'Line', 'Text'])
		self.assertEqual(display.hex['Line'], '#C81E28')
		self.assertEqual(display.to_meta(), {'DisplayAttributes': {'Color': {'Border': '#010203', 'Line': '#C81E28', 'Text': '#FAFA0A'}}})

	def test_written_roles_are_the_roles_present(self):
		for roles in (['Line', 'Text'], ['Line'], ['Background', 'Border', 'Value', 'Meaning', 'Line', 'Text'], []):
			display = DisplayAttributes.from_color({role: (10, 20, 30) for role in roles}) if roles else None
			num = nums_of(group_of(written([length_patient(display=display)])))[0] if True else None
			containers = named(srdoc_children(num), DCMSR_DISPLAY_ATTRIBUTES.value)
			if not roles:
				self.assertEqual(containers, [])
				continue
			color = named(srdoc_children(containers[0]), DCMSR_DISPLAY_COLOR.value)[0]
			self.assertEqual([dcm_code_value(c.ConceptNameCodeSequence[0]).rsplit('.', 1)[1] for c in srdoc_children(color)], roles)

	def test_line_and_text_read_into_the_model(self):
		'''	A Line or Text item read into `color`, not kept as an unmodelled colour-container item; the
			Series Tag roles still read on a measurement, and Line on a Series Tag
		'''
		doc = written([length_patient(display=DisplayAttributes.from_color({'Line': (200, 30, 40), 'Border': (5, 6, 7)}))])
		m = measurements_of(reparse(doc, strict=True))[0]
		self.assertEqual(dict(m.display.color), {'Border': (5, 6, 7), 'Line': (200, 30, 40)})
		self.assertEqual(m.display.color_extra_items, ())
		tag = Finding.series_tag(io_findings()[0].finding, reference=synthetic_reference(), tracking_uid='2.25.9931',
			display={'Border': '#FDF1F0', 'Line': '#00FF00', 'Text': '#0000FF'})
		model = reparse(written([], ) if False else roundtrip(new_model([tag]).create_sr(record_instances=True)), strict=True)
		finding = [f for g in model.groups for f in (g.findings or [])][0]
		self.assertEqual(list(finding.display.color), ['Border', 'Line', 'Text'])

	def test_flat_layout_roles_read(self):
		doc = written([length_patient(display=DisplayAttributes.from_color(LINE_TEXT))])
		num = nums_of(group_of(doc))[0]
		container = named(srdoc_children(num), DCMSR_DISPLAY_ATTRIBUTES.value)[0]
		roles = srdoc_children(srdoc_children(container)[0])
		for role in roles:
			role.RelationshipType = 'HAS PROPERTIES'
		num.ContentSequence = Sequence([c for c in srdoc_children(num) if c is not container] + list(roles))
		m = measurements_of(reparse(doc))[0]
		self.assertEqual(dict(m.display.color), LINE_TEXT)

	def test_absent_role_not_written_and_removed_role_dropped(self):
		model = reparse(written([length_patient(display=DisplayAttributes.from_color(LINE_TEXT))]))
		m = measurements_of(model)[0]
		m.display = DisplayAttributes.from_color({'Line': LINE_TEXT['Line']})
		num = nums_of(group_of(roundtrip(model.create_sr())))[0]
		color = named(srdoc_children(named(srdoc_children(num), DCMSR_DISPLAY_ATTRIBUTES.value)[0]), DCMSR_DISPLAY_COLOR.value)[0]
		self.assertEqual([dcm_code_value(c.ConceptNameCodeSequence[0]) for c in srdoc_children(color)], ['Sonador-DisplayAttributes.Color.Line'])


def set_ucum(item, value, meaning=None):
	unit = item.MeasuredValueSequence[0].MeasurementUnitsCodeSequence[0]
	unit.CodeValue, unit.CodingSchemeDesignator, unit.CodeMeaning = value, 'UCUM', meaning or value


def set_number(item, value):
	measured = item.MeasuredValueSequence[0]
	measured.NumericValue = value
	if 'FloatingPointValue' in measured:
		del measured.FloatingPointValue


def qualifier_item(value, meaning):
	code = Dataset()
	code.CodeValue, code.CodingSchemeDesignator, code.CodeMeaning = value, 'DCM', meaning
	return code


class CircleUnitsAndEditsTestCase(unittest.TestCase):
	'''	The radius in millimetres on read and on assignment, stored units, and refused edits
	'''
	def calibrated_doc(self, calibration, center=(20.0, 20.0), edge=(30.0, 20.0)):
		m = CircleMeasurement.from_center_point(center, edge, reference=synthetic_reference(), calibration=calibration, tracking_uid=TRACKING)
		return m, written([m])

	def test_radius_round_trip_in_millimetres(self):
		for calibration in (CALIBRATION, ImageFrameGeometry((0.0, 0.0, 0.0), (1.0, 0.0, 0.0, 0.0, 1.0, 0.0), (0.8, 0.8))):
			for edge in ((30.0, 20.0), (26.0, 28.0)):
				m, doc = self.calibrated_doc(calibration, edge=edge)
				before = m.radius
				m.radius = m.radius
				self.assertAlmostEqual(m.radius, before, places=9)
				np.testing.assert_allclose(m.edge, edge, atol=1e-9)
				model = reparse(doc, calibrations={SYNTHETIC['SOPInstanceUID']: calibration})
				parsed = measurements_of(model)[0]
				parsed.radius = parsed.radius
				out = nums_of(group_of(roundtrip(model.create_sr())))
				self.assertAlmostEqual(num_value(out[2])[0], before, places=6)
				self.assertAlmostEqual(num_value(out[0])[0], math.pi * before ** 2, places=5)
				# An explicit edit in millimetres, read back through a serialised document
				parsed.radius = 7.0
				again = measurements_of(reparse(roundtrip(model.create_sr()), calibrations={SYNTHETIC['SOPInstanceUID']: calibration}))[0]
				self.assertAlmostEqual(again.radius, 7.0, places=6)
				direction = np.subtract(again.edge, again.center)
				np.testing.assert_allclose(direction / np.linalg.norm(direction), np.subtract(edge, (20, 20)) / np.linalg.norm(np.subtract(edge, (20, 20))),
					atol=1e-6)

	def test_pixel_radius_and_image_constructor(self):
		m = circle_image()
		self.assertAlmostEqual(m.pixel_radius, 10.0)
		m.pixel_radius = 4.0
		self.assertEqual(m.edge, (24.0, 20.0))
		self.assertAlmostEqual(m.radius, 2.0)
		built = CircleMeasurement.from_center_radius((20.0, 20.0), 5.0, reference=synthetic_reference(), calibration=CALIBRATION)
		self.assertEqual(built.edge, (30.0, 20.0))
		with self.assertRaises(ValueError):
			CircleMeasurement.from_center_radius((20.0, 20.0), 5.0, reference=synthetic_reference())
		uncalibrated = circle_image(calibration=None)
		with self.assertRaises(ValueError):
			uncalibrated.radius = 5.0
		with self.assertRaises(ValueError):
			circle_axial().pixel_radius = 3.0

	def test_invalid_radius_edits_refused_without_change(self):
		for m in (circle_axial(), circle_image()):
			points = m.points
			for bad in (-5, 0.0, 0.005, float('nan'), float('inf'), 'x'):
				with self.assertRaises(ValueError):
					m.radius = bad
				self.assertEqual(m.points, points)
			if m.geometry.is_image:
				with self.assertRaises(ValueError):
					m.pixel_radius = -2
				self.assertEqual(m.points, points)

	def cm_doc(self, measurement=None):
		doc = written([measurement or circle_axial()])
		area, perimeter, radius = nums_of(group_of(doc))
		for item, value, unit in ((area, 0.25 * math.pi, 'cm2'), (perimeter, math.pi, 'cm'), (radius, 0.5, 'cm')):
			set_number(item, value)
			set_ucum(item, unit)
		return doc

	def test_radius_in_millimetres_from_stored_centimetres(self):
		calibrations = {SYNTHETIC['SOPInstanceUID']: CALIBRATION}
		for measurement in (circle_axial(), circle_image()):
			doc = self.cm_doc(measurement)
			model = reparse(doc, calibrations=calibrations)
			m = measurements_of(model)[0]
			self.assertAlmostEqual(m.radius, 5.0)
			self.assertEqual((m.value, m.unit.value), (0.5, 'cm'))
			# Unchanged: the stored pairs are written back
			out = nums_of(group_of(roundtrip(model.create_sr())))
			self.assertEqual([num_value(n) for n in out], [(0.25 * math.pi, 'cm2'), (math.pi, 'cm'), (0.5, 'cm')])
			# Self-assignment keeps the circle a 5 mm circle
			m.radius = m.radius
			self.assertAlmostEqual(m.radius, 5.0)
			again = measurements_of(reparse(roundtrip(model.create_sr()), calibrations=calibrations))[0]
			self.assertAlmostEqual(again.radius, 5.0, places=5)
			self.assertAlmostEqual(again.area if again.area_unit.value == 'mm2' else again.area * 100, 25 * math.pi, places=4)
			# An explicit edit in millimetres
			m.radius = 6.0
			again = measurements_of(reparse(roundtrip(model.create_sr()), calibrations=calibrations))[0]
			self.assertAlmostEqual(again.radius, 6.0, places=5)
			self.assertEqual(again.unit.value, 'mm')

	def test_unconvertible_units_kept_on_an_unchanged_save(self):
		doc = written([circle_image()])
		units = (('dm2', math.pi / 400), ('dm', math.pi / 10), ('dm', 0.05))
		for item, (unit, value), (code, meaning) in zip(nums_of(group_of(doc)), units,
				(('114009', 'Value out of range'), ('114008', 'Value unknown'), ('114006', 'Measurement failure'))):
			set_number(item, value)
			set_ucum(item, unit)
			item.NumericValueQualifierCodeSequence = Sequence([qualifier_item(code, meaning)])
		model = reparse(doc)
		m = measurements_of(model)[0]
		self.assertIsNone(m.radius)
		self.assertEqual((m.value, m.unit.value), (0.05, 'dm'))
		out = nums_of(group_of(roundtrip(model.create_sr())))
		self.assertEqual([num_value(n) for n in out], [(u[1], u[0]) for u in units])
		self.assertEqual([n.NumericValueQualifierCodeSequence[0].CodeValue for n in out], ['114009', '114008', '114006'])

	def test_stored_units_kept_in_the_model_and_the_document(self):
		doc = self.cm_doc()
		model = reparse(doc, strict=True)
		m = measurements_of(model)[0]
		self.assertEqual((m.value, m.unit.value), (0.5, 'cm'))
		self.assertAlmostEqual(m.radius, 5.0)
		self.assertEqual([(v.value, v.unit.value) for v in m.values.values()], [(0.25 * math.pi, 'cm2'), (math.pi, 'cm')])
		self.assertEqual((m.area_unit.value, m.perimeter_unit.value), ('cm2', 'cm'))
		self.assertNotIn(SR_CONDITION_MEASUREMENT_VALUE_MISMATCH, conditions(model.diagnostics))
		out = nums_of(group_of(roundtrip(model.create_sr())))
		self.assertEqual([num_value(n)[1] for n in out], ['cm2', 'cm', 'cm'])
		np.testing.assert_allclose([num_value(n)[0] for n in out], [0.25 * math.pi, math.pi, 0.5])
		# A value off by more than the tolerance after conversion is reported
		set_number(nums_of(group_of(doc))[2], 0.6)
		self.assertIn(SR_CONDITION_MEASUREMENT_VALUE_MISMATCH, conditions(reparse(doc).diagnostics))

	def test_missing_radius_derived_after_unit_conversion(self):
		doc = written([circle_image()])
		group = group_of(doc)
		area, perimeter, radius = nums_of(group)
		set_number(area, 0.25 * math.pi)
		set_ucum(area, 'cm2')
		set_number(perimeter, math.pi)
		set_ucum(perimeter, 'cm')
		group.ContentSequence = Sequence([c for c in srdoc_children(group) if c is not radius])
		model = reparse(doc)
		m = measurements_of(model)[0]
		self.assertAlmostEqual(m.radius, 5.0)
		out = nums_of(group_of(roundtrip(model.create_sr())))
		self.assertEqual(num_value(out[2]), (5.0, 'mm'))
		self.assertEqual([num_value(n)[1] for n in out[:2]], ['cm2', 'cm'])
		# A unit that does not convert, and no calibration: refused, not relabelled
		for item in (area, perimeter):
			set_ucum(item, '{px}')
		with self.assertRaises(SRDiagnosticError):
			reparse(doc).create_sr()


class CircleReadFidelityTestCase(OracleMixin, unittest.TestCase):
	'''	Construction-point identity and order, numeric value qualifiers, and the group context of from_sr
	'''
	OTHER_SOP = '2.25.1004'

	def image_three_point(self):
		m = CircleMeasurement.from_points((0, 0), (10, 0), (0, 10), reference=synthetic_reference(), calibration=CALIBRATION, tracking_uid=TRACKING)
		return roundtrip(new_model([], measurements=[m], dcm_evidence=[evidence_dataset(), evidence_dataset(SOPInstanceUID=self.OTHER_SOP)])
			.create_sr(record_instances=True))

	def test_construction_points_on_another_image_or_frame_preserved(self):
		doc = self.image_three_point()
		self.assertIsNotNone(measurements_of(reparse(doc, strict=True))[0].construction_points)
		for sop, frame in ((self.OTHER_SOP, None), (SYNTHETIC['SOPInstanceUID'], 2)):
			changed = copy.deepcopy(doc)
			item = named(srdoc_children(nums_of(group_of(changed))[0]), DCMSR_MEASUREMENT_CONSTRUCTION_POINTS.value)[0]
			image = srdoc_children(item)[0].ReferencedSOPSequence[0]
			image.ReferencedSOPInstanceUID = sop
			if frame:
				image.ReferencedFrameNumber = frame
			model = reparse(changed)
			m = measurements_of(model)[0]
			self.assertIsInstance(m, CircleMeasurement)
			self.assertIsNone(m.construction_points)
			self.assertIn(SR_CONDITION_INVALID_GEOMETRY, conditions(sr_diagnostics_with(model.diagnostics, severity=SR_DIAGNOSTIC_WARNING)))
			out = named(srdoc_children(nums_of(group_of(roundtrip(model.create_sr())))[0]), DCMSR_MEASUREMENT_CONSTRUCTION_POINTS.value)
			self.assertEqual(len(out), 1)
			ref = srdoc_children(out[0])[0].ReferencedSOPSequence[0]
			self.assertEqual((ref.ReferencedSOPInstanceUID, ref.get('ReferencedFrameNumber')), (sop, frame))

	def test_construction_points_before_the_geometry(self):
		patient = written([CircleMeasurement.from_points((0, 0, 0), (10, 0, 0), (0, 10, 0), frame_of_reference_uid=FOR_UID, tracking_uid=TRACKING)])
		for doc in (patient, self.image_three_point()):
			area = nums_of(group_of(doc))[0]
			children = srdoc_children(area)
			construction = named(children, DCMSR_MEASUREMENT_CONSTRUCTION_POINTS.value)[0]
			area.ContentSequence = Sequence([construction] + [c for c in children if c is not construction])
			model = reparse(doc, strict=True, calibrations={SYNTHETIC['SOPInstanceUID']: CALIBRATION})
			m = measurements_of(model)[0]
			self.assertIsInstance(m, CircleMeasurement)
			self.assertEqual(len(m.construction_points), 3)
			self.assert_same_content(doc, roundtrip(model.create_sr()))

	def test_numeric_value_qualifiers_kept_per_num(self):
		doc = written([circle_axial()])
		codes = (('114009', 'Value out of range'), ('114008', 'Value unknown'), ('114006', 'Measurement failure'))
		for item, (value, meaning) in zip(nums_of(group_of(doc)), codes):
			item.NumericValueQualifierCodeSequence = Sequence([qualifier_item(value, meaning)])
		model = reparse(doc)
		out = nums_of(group_of(roundtrip(model.create_sr())))
		self.assertEqual([n.NumericValueQualifierCodeSequence[0].CodeValue for n in out], [c[0] for c in codes])
		# An edit replaces the qualified values: the qualifiers are not written, with an info each
		m = measurements_of(model)[0]
		m.radius = 6.0
		out = nums_of(group_of(roundtrip(model.create_sr())))
		self.assertEqual([('NumericValueQualifierCodeSequence' in n) for n in out], [False, False, False])
		dropped = sr_diagnostics_with(model.diagnostics, condition=SR_CONDITION_QUALIFIER_DROPPED)
		self.assertEqual(sorted(d.value for d in dropped), sorted(c[0] for c in codes))
		self.assertEqual(set(d.severity for d in dropped), {'info'})

	def test_from_sr_needs_the_group_for_a_num(self):
		group = group_of(written([circle_axial()]))
		num = nums_of(group)[1]
		self.assertIsNone(CircleMeasurement.from_sr(num))
		self.assertAlmostEqual(CircleMeasurement.from_sr(num, group=group).radius, 5.0)


# Viewer-authored circles (oak-tree/medical-imaging/ohif-viewers, `platform/core/src/DICOMSR/__fixtures__`): the
# synthetic CT they reference is 64 x 48 pixels, 0.8 mm between rows and 1.2 mm between columns, axial, first
# pixel centre at (-38.4, -19.2, z), slices 3 mm apart from z = 0
VIEWER_FIXTURES = {topic: os.path.join(FIXTURES, 'sonador-viewer.%s.2026-1111.dcm' % topic) for topic in ('circle-image', 'circle-oblique')}
VIEWER_CT_SERIES = '2.25.316170002'
VIEWER_CT_FRAME = '2.25.316170003'
VIEWER_CT_SLICE = '2.25.316170113'
VIEWER_CT_CALIBRATION = {VIEWER_CT_SLICE: ImageFrameGeometry((-38.4, -19.2, 9.0), (1.0, 0.0, 0.0, 0.0, 1.0, 0.0), (0.8, 1.2))}


class CircleViewerFixtureTestCase(OracleMixin, unittest.TestCase):
	'''	Circles written by the Sonador Viewer, read with the calibration of the synthetic CT they reference
	'''
	def read_viewer(self, topic, **kwargs):
		doc = read(VIEWER_FIXTURES[topic])
		model = reparse(doc, strict=True, **kwargs)
		self.assertFalse(sr_diagnostics_with(model.diagnostics, condition=SR_CONDITION_MEASUREMENT_VALUE_MISMATCH), topic)
		self.assertFalse(sr_diagnostics_with(model.diagnostics, condition=SR_CONDITION_MEASUREMENT_VALUE_UNVERIFIED), topic)
		m = measurements_of(model)[0]
		self.assertIsInstance(m, CircleMeasurement, topic)
		self.assertEqual(m.tracking_id, CIRCLE_ID)
		self.assertIsNone(m.statistics)
		self.assertEqual([v.unit.value for v in m.values.values()], ['mm2', 'mm'])
		return doc, model, m

	def test_image_domain(self):
		doc, model, m = self.read_viewer('circle-image', calibrations=VIEWER_CT_CALIBRATION)
		self.assertEqual(m.geometry.domain, 'image')
		self.assertEqual(m.reference.sop_instance_uid, VIEWER_CT_SLICE)
		np.testing.assert_allclose(m.center, (24.1667, 18.25), atol=1e-4)
		calibration = VIEWER_CT_CALIBRATION[VIEWER_CT_SLICE]
		np.testing.assert_allclose(calibration.patient_point(*m.center), (-10.0, -5.0, 9.0), atol=1e-3)
		np.testing.assert_allclose(calibration.patient_point(*m.edge), (2.0, -5.0, 9.0), atol=1e-3)
		# The stored values: 10 columns of 1.2 mm; the radius is stored as read after the pixel round trip
		self.assertEqual(str(nums_of(group_of(doc))[2].MeasuredValueSequence[0].NumericValue), '11.9999999999999')
		self.assertAlmostEqual(m.radius, 12.0, places=6)
		self.assertAlmostEqual(m.computed_value()[0], 12.0, places=3)
		self.assertAlmostEqual(m.area, 452.3893, places=4)
		self.assertAlmostEqual(m.perimeter, 75.3982, places=4)
		self.assertIsNone(m.normal)
		self.assertEqual(m.description, 'Circle (image)')
		self.assert_same_content(doc, roundtrip(model.create_sr()))

	def test_oblique_patient_domain(self):
		doc, model, m = self.read_viewer('circle-oblique')
		self.assertEqual((m.geometry.domain, m.geometry.graphic_type, m.frame_of_reference_uid), ('patient', 'ELLIPSE', VIEWER_CT_FRAME))
		np.testing.assert_allclose(m.center, (0.0, 0.0, 15.0), atol=1e-4)
		self.assertAlmostEqual(m.radius, 10.0, places=4)
		self.assertAlmostEqual(m.area, 314.1593, places=4)
		self.assertAlmostEqual(m.perimeter, 62.8319, places=4)
		np.testing.assert_allclose(m.normal, np.array((-1.0, 0.0, -1.0)) / math.sqrt(2), atol=1e-5)
		np.testing.assert_allclose(m.u, np.array((1.0, 0.0, -1.0)) / math.sqrt(2), atol=1e-5)
		self.assertEqual((m.drawing_plane_text, m.views, m.series_uid), ('0.707107,0,-0.707107,0,-1,0', ('oblique',), VIEWER_CT_SERIES))
		self.assertIsNotNone(m.drawing_plane)
		self.assertEqual(m.description, 'Circle (oblique)')
		self.assert_same_content(doc, roundtrip(model.create_sr()))


# Pixel statistics

CT_QUANTITY = Code('112031', 'DCM', 'Attenuation Coefficient')
HU = Code("[hnsf'U]", 'UCUM', 'Hounsfield Unit')
NO_UNITS = Code('1', 'UCUM', 'no units')
STATISTIC_VALUES = OrderedDict((('mean', 40.5), ('std_dev', 12.25), ('max', 97.0)))
STATISTICS_FIXTURE_DATE = '2026-1112'
STATISTICS_FIXTURES = ('circle-statistics-ct', 'circle-statistics-stock')


def coded_item(code):
	item = Dataset()
	item.CodeValue, item.CodingSchemeDesignator, item.CodeMeaning = code.value, code.scheme_designator, code.meaning
	return item


def statistic_num(name, value, unit, geometry, derivation=None):
	'''	A statistic NUM item as the Viewer writes it: CONTAINS NUM, the Derivation (when given), then its own
		copy of the circle's geometry
	'''
	item = Dataset()
	item.RelationshipType, item.ValueType = 'CONTAINS', 'NUM'
	item.ConceptNameCodeSequence = Sequence([coded_item(name)])
	measured = Dataset()
	if unit is not None:
		measured.MeasurementUnitsCodeSequence = Sequence([coded_item(unit)])
	# A value that is not finite is written as dcmjs writes it
	measured.NumericValue = DS(value, auto_format=True) if math.isfinite(value) else 'NaN'
	item.MeasuredValueSequence = Sequence([measured])
	children = []
	if derivation is not None:
		modifier = Dataset()
		modifier.RelationshipType, modifier.ValueType = 'HAS CONCEPT MOD', 'CODE'
		modifier.ConceptNameCodeSequence = Sequence([coded_item(DCMSR_DERIVATION)])
		modifier.ConceptCodeSequence = Sequence([coded_item(derivation)])
		children.append(modifier)
	children.append(copy.deepcopy(geometry))
	item.ContentSequence = Sequence(children)
	return item


def derivation_form(quantity=CT_QUANTITY, unit=HU, srt=False, values=STATISTIC_VALUES):
	'''	(name, value, unit, derivation) of each statistic in the quantity form
	'''
	return [(quantity, value, unit, DCMSR_CIRCLE_STATISTICS[field][1 if srt else 0]) for field, value in values.items()]


def stock_form(unit=HU, srt=False, values=STATISTIC_VALUES):
	'''	(name, value, unit, derivation) of each statistic in the stock Cornerstone3D form
	'''
	return [(DCMSR_CIRCLE_STATISTICS[field][1 if srt else 0], value, unit, None) for field, value in values.items()]


def with_statistics(doc, statistics, group_index=0):
	'''	Add statistic NUM items after the last NUM item of a measurement group, each with a copy of the
		geometry of the group's first NUM item
	'''
	group = group_of(doc, group_index)
	children = list(srdoc_children(group))
	last = max(idx for idx, child in enumerate(children) if child.get('ValueType') == 'NUM')
	geometry = coordinates_of(children[last])[0] if coordinates_of(children[last]) else coordinates_of(nums_of(group)[0])[0]
	added = [statistic_num(name, value, unit, geometry, derivation) for name, value, unit, derivation in statistics]
	group.ContentSequence = Sequence(children[:last + 1] + added + children[last + 1:])
	return doc


def statistics_fixture_doc(topic):
	'''	The Python-written statistics fixtures: a circle of this client, with the statistics a Viewer adds
		(this client never computes or creates them): the quantity form on a patient-domain circle, the stock
		Cornerstone3D form on an image-domain circle. The values are synthetic; nothing checks them against pixels.
	'''
	if topic == 'circle-statistics-ct':
		measurements, statistics = [circle_axial(**fixture_properties(topic, 'axial', series_uid=SYNTHETIC['SeriesInstanceUID'],
			drawing_plane=AXIAL))], derivation_form()
	elif topic == 'circle-statistics-stock':
		measurements, statistics = [circle_image(**fixture_properties(topic, 'axial'))], stock_form()
	else:
		raise KeyError(topic)
	doc = roundtrip(new_model([], measurements=measurements).create_sr(record_instances=True, dcmsr_series_uid='2.25.8000',
		dcmsr_instance_uid='2.25.8001'))
	return with_statistics(doc, statistics)


def statistics_fixture_path(topic):
	return os.path.join(FIXTURES, 'sonador-io.%s.%s.dcm' % (topic, STATISTICS_FIXTURE_DATE))


class CircleStatisticsTestCase(OracleMixin, unittest.TestCase):
	'''	Pixel statistics of a circle: read into `statistics` in the quantity and the stock form, written back
		verbatim while the circle is unchanged, dropped after an edit, never created by this client
	'''
	CALIBRATIONS = {SYNTHETIC['SOPInstanceUID']: CALIBRATION}

	def setUp(self):
		if os.environ.get('UPDATE_SR_FIXTURES'):
			for topic in STATISTICS_FIXTURES:
				statistics_fixture_doc(topic).save_as(statistics_fixture_path(topic))

	def read_fixture(self, topic):
		doc = read(statistics_fixture_path(topic))
		self.assertEqual((doc.PatientID, str(doc.StudyInstanceUID), str(doc.SOPClassUID)), ('ANONYMOUS', '2.25.1001', DCMSR_SOP_CLASS_COMPREHENSIVE_3D))
		model = reparse(doc, strict=True, calibrations=self.CALIBRATIONS)
		return doc, model, measurements_of(model)[0]

	def circle_with(self, statistics, circle=None):
		doc = with_statistics(written([circle or circle_axial()]), statistics)
		model = reparse(doc, calibrations=self.CALIBRATIONS)
		return doc, model, measurements_of(model)[0]

	def assert_statistics(self, m, quantity, unit, values=STATISTIC_VALUES):
		s = m.statistics
		self.assertIsInstance(s, CircleStatistics)
		self.assertEqual((s.mean, s.std_dev, s.max), tuple(values.get(field) for field in ('mean', 'std_dev', 'max')))
		self.assertEqual(None if s.quantity is None else (s.quantity.value, s.quantity.scheme_designator), quantity)
		self.assertEqual(None if s.unit is None else (s.unit.value, s.unit.scheme_designator), unit)

	def test_fixtures_read_in_both_forms(self):
		for topic in STATISTICS_FIXTURES:
			doc, model, m = self.read_fixture(topic)
			self.assertIsInstance(m, CircleMeasurement, topic)
			self.assert_statistics(m, ('112031', 'DCM'), ("[hnsf'U]", 'UCUM'))
			# Statistics are kept out of `values`, which holds the area and perimeter only
			self.assertEqual(list(m.values), [('42798000', 'SCT'), ('131191004', 'SCT')], topic)
			self.assertEqual(len(nums_of(group_of(doc))), 6, topic)

	def test_unchanged_resave_reemits_the_statistics_verbatim(self):
		for topic in STATISTICS_FIXTURES:
			doc, model, m = self.read_fixture(topic)
			again = roundtrip(model.create_sr())
			src, out = nums_of(group_of(doc)), nums_of(group_of(again))
			self.assertEqual(len(out), 6, topic)
			# After the Radius NUM, each with its Derivation (quantity form) and its own geometry copy
			for source, written_item in zip(src[3:], out[3:]):
				compare_nodes(self, Node(source), Node(written_item), topic)
				self.assertEqual(coordinates_of(written_item)[0].GraphicData, coordinates_of(source)[0].GraphicData)
			self.assertFalse(sr_diagnostics_with(model.diagnostics, condition=SR_CONDITION_STATISTICS_DROPPED), topic)
			self.assert_same_content(doc, again)
			self.assert_statistics(measurements_of(reparse(again, strict=True, calibrations=self.CALIBRATIONS))[0], ('112031', 'DCM'),
				("[hnsf'U]", 'UCUM'))

	def test_edit_drops_the_statistics(self):
		edits = {'circle-statistics-ct': lambda m: setattr(m, 'center', (11.0, 20.0, 30.0)),
			'circle-statistics-stock': lambda m: setattr(m, 'radius', 6.0)}
		for topic, edit in edits.items():
			doc, model, m = self.read_fixture(topic)
			edit(m)
			self.assertIsNone(m.statistics, topic)
			again = roundtrip(model.create_sr())
			out = nums_of(group_of(again))
			self.assertEqual([dcm_code_value(n.ConceptNameCodeSequence[0]) for n in out], ['42798000', '131191004', '131190003'], topic)
			dropped = sr_diagnostics_with(model.diagnostics, condition=SR_CONDITION_STATISTICS_DROPPED)
			self.assertEqual([(d.severity, d.value) for d in dropped], [('info', 3)], topic)
			self.assertIsNone(measurements_of(reparse(again, calibrations=self.CALIBRATIONS))[0].statistics, topic)

	def test_srt_codes_read(self):
		for statistics in (derivation_form(srt=True), stock_form(srt=True)):
			doc, model, m = self.circle_with(statistics)
			self.assert_statistics(m, ('112031', 'DCM'), ("[hnsf'U]", 'UCUM'))
			self.assert_same_content(doc, roundtrip(model.create_sr()))

	def test_quantities_and_units(self):
		cases = (
			(derivation_form(Code('126401', 'DCM', 'SUVbw'), Code('g/ml{SUVbw}', 'UCUM', 'SUVbw')), ('126401', 'DCM'), ('g/ml{SUVbw}', 'UCUM')),
			(derivation_form(Code('110852', 'DCM', 'MR signal intensity'), NO_UNITS), ('110852', 'DCM'), ('1', 'UCUM')),
			(stock_form(Code('HU', 'UCUM', 'HU')), ('112031', 'DCM'), ('HU', 'UCUM')),
			(stock_form(Code('SUV', 'UCUM', 'SUV')), ('126401', 'DCM'), ('SUV', 'UCUM')),
			# No unit, or a unit of no known quantity: read, with no quantity
			(stock_form(NO_UNITS), None, ('1', 'UCUM')),
			(stock_form(Code('mg/ml', 'UCUM', 'mg/ml')), None, ('mg/ml', 'UCUM')),
		)
		for statistics, quantity, unit in cases:
			doc, model, m = self.circle_with(statistics)
			self.assert_statistics(m, quantity, unit)

	def test_unsupported_statistics_stay_unmodelled(self):
		ct_mean = derivation_form(values={'mean': 40.5})
		cases = OrderedDict((
			('mixed quantities', ct_mean + stock_form(NO_UNITS, values={'max': 97.0})),
			('SUV normalization', stock_form(Code('g/ml{SUVlbm}', 'UCUM', 'SUVlbm'))),
			('unsupported quantity', derivation_form(Code('118565006', 'SCT', 'Volume'))),
			('quantity scheme', derivation_form(Code('112031', '99X', 'Attenuation Coefficient'))),
			('quantity unit', derivation_form(unit=Code('mm', 'UCUM', 'mm'))),
			('unit scheme', stock_form(Code('HU', '99CS', 'HU'))),
			('statistic scheme', [(CT_QUANTITY, 40.5, HU, Code('373098007', 'SRT', 'Mean'))]),
			('another derivation', [(CT_QUANTITY, 40.5, HU, Code('255605001', 'SCT', 'Minimum'))]),
			('not finite', derivation_form(values={'mean': float('nan')})),
		))
		for case, statistics in cases.items():
			doc, model, m = self.circle_with(statistics)
			self.assertIsInstance(m, CircleMeasurement, case)
			self.assertIsNone(m.statistics, case)
			self.assertEqual(list(m.values), [('42798000', 'SCT'), ('131191004', 'SCT')], case)
			again = roundtrip(model.create_sr())
			self.assertEqual(len(nums_of(group_of(again))), 3 + len(statistics), case)
			if case == 'not finite':
				# The oracle cannot compare NaN with itself
				self.assertEqual(str(nums_of(group_of(again))[3].MeasuredValueSequence[0].NumericValue), 'NaN')
			else:
				self.assert_same_content(doc, again)

	def test_unsupported_statistic_beside_supported_ones(self):
		doc, model, m = self.circle_with(derivation_form(values={'mean': 40.5}) + stock_form(Code('g/ml{SUVlbm}', 'UCUM', 'SUVlbm'), values={'max': 3.5}))
		self.assert_statistics(m, ('112031', 'DCM'), ("[hnsf'U]", 'UCUM'), values={'mean': 40.5})
		self.assert_same_content(doc, roundtrip(model.create_sr()))

	def test_repeated_statistic_reads_the_first(self):
		doc, model, m = self.circle_with(derivation_form() + derivation_form(values={'mean': 41.0}))
		self.assertEqual(m.statistics.mean, 40.5)
		self.assertEqual(len(nums_of(group_of(roundtrip(model.create_sr())))), 7)

	def test_repeated_statistics_follow_the_circle(self):
		repeats = {'circle-statistics-ct': (derivation_form(values={'mean': 41.0}), lambda m: setattr(m, 'center', (11.0, 20.0, 30.0))),
			'circle-statistics-stock': (stock_form(values={'mean': 41.0}), lambda m: setattr(m, 'radius', 6.0))}
		for topic, (repeat, edit) in repeats.items():
			source = with_statistics(read(statistics_fixture_path(topic)), repeat)
			# Unchanged: all four statistic NUMs written back verbatim, the first Mean read
			model = reparse(source, strict=True, calibrations=self.CALIBRATIONS)
			m = measurements_of(model)[0]
			self.assertEqual(m.statistics.mean, 40.5, topic)
			again = roundtrip(model.create_sr())
			self.assertEqual(len(nums_of(group_of(again))), 7, topic)
			self.assert_same_content(source, again)
			# Edited: no statistic NUM survives, and none comes back on later reads and saves
			model = reparse(source, strict=True, calibrations=self.CALIBRATIONS)
			edit(measurements_of(model)[0])
			edited = roundtrip(model.create_sr())
			self.assertEqual(len(nums_of(group_of(edited))), 3, topic)
			dropped = sr_diagnostics_with(model.diagnostics, condition=SR_CONDITION_STATISTICS_DROPPED)
			self.assertEqual([(d.severity, d.value) for d in dropped], [('info', 4)], topic)
			later = reparse(edited, calibrations=self.CALIBRATIONS)
			self.assertIsNone(measurements_of(later)[0].statistics, topic)
			self.assertEqual(len(nums_of(group_of(roundtrip(later.create_sr())))), 3, topic)

	def test_frame_change_drops_the_statistics(self):
		doc, model, m = self.read_fixture('circle-statistics-ct')
		# The same Frame of Reference assigned again keeps them
		m.frame_of_reference_uid = str(m.frame_of_reference_uid)
		self.assertIsNotNone(m.statistics)
		m.frame_of_reference_uid = '2.25.9999'
		self.assertIsNone(m.statistics)
		again = roundtrip(model.create_sr())
		out = nums_of(group_of(again))
		self.assertEqual(len(out), 3)
		self.assertEqual({str(coordinates_of(n)[0].ReferencedFrameOfReferenceUID) for n in out}, {'2.25.9999'})
		dropped = sr_diagnostics_with(model.diagnostics, condition=SR_CONDITION_STATISTICS_DROPPED)
		self.assertEqual([(d.severity, d.value) for d in dropped], [('info', 3)])

	def test_python_circle_writes_none(self):
		for m in (circle_axial(), circle_image(), CircleMeasurement.from_points((0.0, 0.0, 0.0), (10.0, 0.0, 0.0), (0.0, 10.0, 0.0),
				frame_of_reference_uid=FOR_UID, tracking_uid=TRACKING)):
			self.assertIsNone(m.statistics)
			with self.assertRaises(AttributeError):
				m.statistics = CircleStatistics(1.0, None, None, None, None)
			self.assertEqual(len(nums_of(group_of(written([m])))), 3)

	def test_circle_read_as_generic_keeps_statistics_as_values(self):
		'''	A circle group the circle model cannot hold reads as a generic measurement: its statistics are
			values, keyed by concept and Derivation
		'''
		doc = with_statistics(written([circle_axial()]), derivation_form())
		ellipse = coordinates_of(nums_of(group_of(doc))[0])[0]
		for num in nums_of(group_of(doc))[:3]:
			coordinates_of(num)[0].GraphicData = [5, 20, 30, 15, 20, 30, 10, 17, 30, 10, 23, 30]
		m = measurements_of(reparse(doc))[0]
		self.assertNotIsInstance(m, CircleMeasurement)
		self.assertEqual([k for k in m.values if len(k) == 4],
			[('112031', 'DCM', '373098007', 'SCT'), ('112031', 'DCM', '386136009', 'SCT'), ('112031', 'DCM', '56851009', 'SCT')])


VIEWER_STATISTICS_FIXTURE = os.path.join(FIXTURES, 'sonador-viewer.circle-statistics.2026-1113.dcm')
VIEWER_STATISTICS = OrderedDict((('mean', 400.0), ('std_dev', 0.0), ('max', 400.0)))


class CircleViewerStatisticsTestCase(OracleMixin, unittest.TestCase):
	'''	Pixel statistics written by the Sonador Viewer: Mean, Std dev and Max of a circle on its synthetic CT,
		every pixel inside the circle 400 HU
	'''
	def read_viewer(self):
		doc = read(VIEWER_STATISTICS_FIXTURE)
		model = reparse(doc, strict=True, calibrations=VIEWER_CT_CALIBRATION)
		self.assertFalse(sr_diagnostics_with(model.diagnostics, severity=SR_DIAGNOSTIC_WARNING))
		return doc, model, measurements_of(model)[0]

	def test_hydrates_as_a_circle_with_statistics(self):
		doc, model, m = self.read_viewer()
		self.assertIsInstance(m, CircleMeasurement)
		self.assertEqual((m.tracking_id, m.geometry.domain, m.reference.sop_instance_uid), (CIRCLE_ID, 'image', VIEWER_CT_SLICE))
		self.assertEqual((tuple(m.center), tuple(m.edge)), ((32.5, 24.5), (37.5, 24.5)))
		calibration = VIEWER_CT_CALIBRATION[VIEWER_CT_SLICE]
		np.testing.assert_allclose(calibration.patient_point(*m.center), (0.0, 0.0, 9.0), atol=1e-6)
		np.testing.assert_allclose(calibration.patient_point(*m.edge), (6.0, 0.0, 9.0), atol=1e-6)
		self.assertAlmostEqual(m.radius, 6.0, places=6)
		self.assertEqual(m.description, 'Circle (statistics)')

		s = m.statistics
		self.assertIsInstance(s, CircleStatistics)
		self.assertEqual((s.mean, s.std_dev, s.max), tuple(VIEWER_STATISTICS.values()))
		self.assertEqual(((s.quantity.value, s.quantity.scheme_designator), (s.unit.value, s.unit.scheme_designator)),
			(('112031', 'DCM'), ("[hnsf'U]", 'UCUM')))
		self.assertEqual(list(m.values), [('42798000', 'SCT'), ('131191004', 'SCT')])

		# Area, Perimeter, Radius, then the statistics, each with the Derivation modifier and its own geometry copy
		nums = nums_of(group_of(doc))
		self.assertEqual([dcm_code_value(n.ConceptNameCodeSequence[0]) for n in nums],
			['42798000', '131191004', '131190003', '112031', '112031', '112031'])
		for num, derivation, value in zip(nums[3:], ('373098007', '386136009', '56851009'), VIEWER_STATISTICS.values()):
			self.assertEqual(num_value(num), (value, "[hnsf'U]"))
			modifiers = [c for c in srdoc_children(num) if c.RelationshipType == 'HAS CONCEPT MOD']
			self.assertEqual([(dcm_code_value(c.ConceptNameCodeSequence[0]), dcm_code_value(c.ConceptCodeSequence[0])) for c in modifiers],
				[('121401', derivation)])
		for num in nums:
			coordinates = coordinates_of(num)
			self.assertEqual([(c.RelationshipType, c.ValueType, c.GraphicType) for c in coordinates], [('INFERRED FROM', 'SCOORD', 'CIRCLE')])
			self.assertEqual(list(coordinates[0].GraphicData), [32.5, 24.5, 37.5, 24.5])

	def test_unchanged_resave_keeps_the_statistics_verbatim(self):
		doc, model, m = self.read_viewer()
		again = roundtrip(model.create_sr())
		src, out = nums_of(group_of(doc)), nums_of(group_of(again))
		self.assertEqual(len(out), 6)
		for source, written_item in zip(src[3:], out[3:]):
			compare_nodes(self, Node(source), Node(written_item), 'statistic')
			self.assertEqual(str(written_item.MeasuredValueSequence[0].NumericValue), str(source.MeasuredValueSequence[0].NumericValue))
		self.assertFalse(sr_diagnostics_with(model.diagnostics, condition=SR_CONDITION_STATISTICS_DROPPED))
		self.assert_same_content(doc, again)
		self.assertEqual(measurements_of(reparse(again, strict=True, calibrations=VIEWER_CT_CALIBRATION))[0].statistics.mean, 400.0)

	def test_edit_drops_the_statistics(self):
		edits = OrderedDict((('center', lambda m: setattr(m, 'center', (33.5, 24.5))), ('radius', lambda m: setattr(m, 'radius', 4.8))))
		for case, edit in edits.items():
			doc, model, m = self.read_viewer()
			edit(m)
			self.assertIsNone(m.statistics, case)
			again = roundtrip(model.create_sr())
			out = nums_of(group_of(again))
			self.assertEqual([dcm_code_value(n.ConceptNameCodeSequence[0]) for n in out], ['42798000', '131191004', '131190003'], case)
			dropped = sr_diagnostics_with(model.diagnostics, condition=SR_CONDITION_STATISTICS_DROPPED)
			self.assertEqual([(d.severity, d.value) for d in dropped], [('info', 3)], case)
			self.assertIsNone(measurements_of(reparse(again, calibrations=VIEWER_CT_CALIBRATION))[0].statistics, case)


class MeasurementValueKeyTestCase(unittest.TestCase):
	'''	`Measurement.values` keys: concept and Derivation together, for every tool
	'''
	def test_two_derivations_of_one_concept_are_both_kept(self):
		doc = written([length_patient()])
		group = group_of(doc)
		geometry = coordinates_of(nums_of(group)[0])[0]
		group.ContentSequence = Sequence(list(srdoc_children(group)) + [statistic_num(CT_QUANTITY, value, HU, geometry, DCMSR_CIRCLE_STATISTICS[field][0])
			for field, value in (('mean', 40.5), ('max', 97.0))])
		model = reparse(doc)
		m = measurements_of(model)[0]
		self.assertIsInstance(m, LengthMeasurement)
		self.assertEqual(list(m.values), [('112031', 'DCM', '373098007', 'SCT'), ('112031', 'DCM', '56851009', 'SCT')])
		self.assertEqual([v.value for v in m.values.values()], [40.5, 97.0])
		out = nums_of(group_of(roundtrip(model.create_sr())))
		self.assertEqual([dcm_code_value(srdoc_children(n)[0].ConceptCodeSequence[0]) for n in out[1:]], ['373098007', '56851009'])

	def test_key_without_derivation_unchanged(self):
		doc = written([length_patient()])
		group = group_of(doc)
		geometry = coordinates_of(nums_of(group)[0])[0]
		group.ContentSequence = Sequence(list(srdoc_children(group)) + [statistic_num(CT_QUANTITY, 40.5, HU, geometry)])
		self.assertEqual(list(measurements_of(reparse(doc))[0].values), [('112031', 'DCM')])


class CircleConstructionPointsDroppedTestCase(unittest.TestCase):
	def test_edit_drops_the_construction_points_with_an_info(self):
		doc = written([CircleMeasurement.from_points((0.0, 0.0, 0.0), (10.0, 0.0, 0.0), (0.0, 10.0, 0.0), frame_of_reference_uid=FOR_UID,
			tracking_uid=TRACKING)])
		model = reparse(doc, strict=True)
		roundtrip(model.create_sr())
		self.assertFalse(sr_diagnostics_with(model.diagnostics, condition=SR_CONDITION_CONSTRUCTION_POINTS_DROPPED))
		m = measurements_of(model)[0]
		m.radius = 9.0
		self.assertIsNone(m.construction_points)
		again = roundtrip(model.create_sr())
		self.assertEqual([d.severity for d in sr_diagnostics_with(model.diagnostics, condition=SR_CONDITION_CONSTRUCTION_POINTS_DROPPED)], ['info'])
		self.assertFalse(named(srdoc_children(nums_of(group_of(again))[0]), DCMSR_MEASUREMENT_CONSTRUCTION_POINTS.value))
