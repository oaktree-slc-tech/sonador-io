'''	Offline tests of the bidirectional measurement (Cornerstone `Bidirectional`) at Sonador-SR 0.3.

	A bidirectional measurement is written as two NUM items, Long Axis `(103339001, SCT)` then Short Axis
	`(103340004, SCT)`, each `INFERRED FROM` its own two-point `POLYLINE`: SCOORD `SELECTED FROM` the image in
	the image domain, SCOORD3D with the Frame of Reference in the patient domain. The per-measurement
	properties are children of the Long Axis NUM. The axes are held as drawn and never swapped.

	Fixtures (`ftests/test_sr/fixtures`, synthetic study): `sonador-io.bidirectional-image`,
	`bidirectional-axial` and `bidirectional-oblique`, each `.2026-1113.dcm`, written by this suite
	(`UPDATE_SR_FIXTURES=1`). `cornerstone-adapters.bidirectional-image.2026-1113.dcm` and
	`cornerstone-adapters.bidirectional-patient.2026-1113.dcm` are written by the stock `@cornerstonejs/adapters`
	4.22.13 `Bidirectional` writer (the Sonador Viewer's locked version) from synthetic annotation state, by the
	jest harness in `fixtures/generators/cornerstone-adapters`. `sonador-viewer.bidirectional-image.2026-1113.dcm`
	(image domain) and `sonador-viewer.bidirectional-oblique.2026-1113.dcm` (patient domain, on a plane turned 45
	degrees between sagittal and axial) are written by the Sonador Viewer over its synthetic CT, copied unchanged
	from oak-tree/medical-imaging/ohif-viewers `platform/core/src/DICOMSR/__fixtures__`.

	Run from the directory containing the `sonador` package: python -m unittest sonador.ftests.test_sr.sr_bidirectional
'''
import copy, logging, math, os, unittest
from collections import OrderedDict

import numpy as np

from pydicom.sequence import Sequence

from highdicom.sr.templates import Code

from ...apisettings.base import BidirectionalMeasurement, CircleMeasurement, LengthMeasurement, Measurement, ImageTransformMatrix, \
	measurement_class_for_type, SONADOR_MEASUREMENT_TYPE_BIDIRECTIONAL, DCMSR_CONCEPT_LONG_AXIS, DCMSR_CONCEPT_SHORT_AXIS
from ...apisettings.display_attrs import DisplayAttributes
from ...apisettings.sr import dcm_code_value, SRDiagnosticError, sr_diagnostics_with, SR_DIAGNOSTIC_INFO, SR_DIAGNOSTIC_WARNING, \
	SR_CONDITION_AXES_NOT_ORDERED, SR_CONDITION_AXES_UNITS_DIFFER, SR_CONDITION_DEGENERATE_GEOMETRY, SR_CONDITION_MEASUREMENT_READ_AS_GENERIC, \
	SR_CONDITION_MEASUREMENT_VALUE_MISMATCH, SR_CONDITION_MEASUREMENT_UNCALIBRATED, SR_CONDITION_INVALID_DRAWING_PLANE, \
	SR_CONDITION_MEASUREMENT_WITHOUT_VALUE, SR_CONDITION_GROUP_LEVEL_PROPERTIES, SR_CONDITION_QUALIFIER_DROPPED, \
	SR_CONDITION_MEASUREMENT_NOT_COMPUTABLE
from ...apisettings.sr.seriestag import measurement_type_for_tracking_id, tracking_id_for_measurement_type, DCMSR_MEASUREMENT_DESCRIPTION, \
	DCMSR_MEASUREMENT_TEXT, DCMSR_DISPLAY_ATTRIBUTES, DCMSR_SOP_CLASS_COMPREHENSIVE_3D
from ...apisettings.sr.measurements import DCMSR_BIDIRECTIONAL_CONCEPTS, DCMSR_MEASUREMENT_VIEW, DCMSR_MEASUREMENT_SOURCE_SERIES, \
	DCMSR_MEASUREMENT_DRAWING_PLANE
from ...imaging.helpers.sr import SonadorImagingMeasurementReport
from ...imaging.helpers.sr.parse import srdoc_children
from ...imaging.orthanc.sr import dcmsr_local_series
from .sr_seriestag import read, roundtrip, new_model, synthetic_reference, named, io_findings, SYNTHETIC, FIXTURES
from .sr_measurements import FOR_UID, CALIBRATION, UNCALIBRATED, written, reparse, measurements_of, group_of, conditions, text_item, \
	length_patient, angle_patient, OBLIQUE_WIDTH, OBLIQUE_HEIGHT, OBLIQUE_ORIGIN, SAGITTAL
from .sr_circle import OracleMixin, nums_of, num_value, coordinates_of, set_ucum, set_number, qualifier_item, circle_axial, AXIAL, LINE_TEXT, \
	VIEWER_CT_CALIBRATION, VIEWER_CT_SERIES, VIEWER_CT_FRAME, VIEWER_CT_SLICE


__all__ = ['BidirectionalRegistryTestCase', 'BidirectionalModelTestCase', 'BidirectionalWriterTestCase', 'BidirectionalReaderTestCase',
	'BidirectionalValueRuleTestCase', 'BidirectionalMixedUnitEditTestCase', 'BidirectionalValueAssignmentTestCase', 'BidirectionalOutOfPlaneTestCase', 'BidirectionalFixtureTestCase', 'BidirectionalAdapterFixtureTestCase',
	'BidirectionalViewerFixtureTestCase', 'BidirectionalDocumentTestCase']


TRACKING = '2.25.9951'
BIDIRECTIONAL_ID = 'cornerstoneTools@^4.0.0:Bidirectional'
BIDIRECTIONAL_ID_3D = 'Cornerstone3DTools@^0.1.0:Bidirectional'
LONG_CODE, SHORT_CODE = '103339001', '103340004'
FRAMES = {SYNTHETIC['SOPInstanceUID']: CALIBRATION}

# Axial patient-domain axes: a 20 mm long axis along x, a 10 mm short axis along y crossing its midpoint
AXIAL_LONG = ((-10.0, 20.0, 30.0), (10.0, 20.0, 30.0))
AXIAL_SHORT = ((0.0, 15.0, 30.0), (0.0, 25.0, 30.0))
# Image-domain axes on the synthetic frame (0.5 mm between columns, 2 mm between rows): 20 mm along the
# rows, 5 mm along the columns
IMAGE_LONG = ((20.0, 10.0), (20.0, 20.0))
IMAGE_SHORT = ((15.0, 15.0), (25.0, 15.0))
# A child no tool models
VENDOR_NOTE = Code('Vendor-Note', '99VENDOR', 'Vendor note')


def setUpModule():
	logging.getLogger('sonador').setLevel(logging.CRITICAL)


def tearDownModule():
	logging.getLogger('sonador').setLevel(logging.NOTSET)


def bidirectional_axial(long_axis=AXIAL_LONG, short_axis=AXIAL_SHORT, **kwargs):
	kwargs.setdefault('frame_of_reference_uid', FOR_UID)
	kwargs.setdefault('tracking_uid', TRACKING)
	return BidirectionalMeasurement.from_axes(long_axis, short_axis, **kwargs)


def bidirectional_image(long_axis=IMAGE_LONG, short_axis=IMAGE_SHORT, **kwargs):
	kwargs.setdefault('calibration', CALIBRATION)
	kwargs.setdefault('tracking_uid', TRACKING)
	return BidirectionalMeasurement.from_axes(long_axis, short_axis, reference=synthetic_reference(), **kwargs)


def oblique_axes(origin=OBLIQUE_ORIGIN, half_long=10.0, half_short=4.0):
	'''	Axes in the oblique drawing plane through `origin`: the long axis along its width direction, the short
		axis along its height direction, crossing at `origin`
	'''
	o, w, h = np.asarray(origin), np.asarray(OBLIQUE_WIDTH), np.asarray(OBLIQUE_HEIGHT)
	as_points = lambda a, b: (tuple(float(c) for c in a), tuple(float(c) for c in b))
	return as_points(o - half_long * w, o + half_long * w), as_points(o - half_short * h, o + half_short * h)


def property_item(concept, value):
	'''	A Sonador-SR 0.3 TEXT property item, as this client writes it
	'''
	item = text_item(concept, value)
	item.ConceptNameCodeSequence[0].CodingSchemeVersion = '0.3'
	return item


def concepts_of(group):
	return [dcm_code_value(n.ConceptNameCodeSequence[0]) for n in nums_of(group)]


def stored_numbers(group):
	return [n.MeasuredValueSequence[0].NumericValue for n in nums_of(group)]


def remove_float(item):
	measured = item.MeasuredValueSequence[0]
	if 'FloatingPointValue' in measured:
		del measured.FloatingPointValue


class BidirectionalRegistryTestCase(unittest.TestCase):
	def test_identifiers_route_to_the_bidirectional(self):
		self.assertEqual(tracking_id_for_measurement_type(SONADOR_MEASUREMENT_TYPE_BIDIRECTIONAL), BIDIRECTIONAL_ID)
		for identifier in (BIDIRECTIONAL_ID, BIDIRECTIONAL_ID_3D):
			self.assertEqual(measurement_type_for_tracking_id(identifier), SONADOR_MEASUREMENT_TYPE_BIDIRECTIONAL)
		self.assertIs(measurement_class_for_type(SONADOR_MEASUREMENT_TYPE_BIDIRECTIONAL), BidirectionalMeasurement)

	def test_concepts(self):
		self.assertEqual([(c[0].value, c[0].scheme_designator, c[0].meaning) for c in DCMSR_BIDIRECTIONAL_CONCEPTS.values()],
			[(LONG_CODE, 'SCT', 'Long Axis'), (SHORT_CODE, 'SCT', 'Short Axis')])
		self.assertEqual([(c[1].value, c[1].scheme_designator) for c in DCMSR_BIDIRECTIONAL_CONCEPTS.values()], [('G-A185', 'SRT'), ('G-A186', 'SRT')])
		self.assertEqual((DCMSR_CONCEPT_LONG_AXIS.value, DCMSR_CONCEPT_SHORT_AXIS.value), (LONG_CODE, SHORT_CODE))


class BidirectionalModelTestCase(unittest.TestCase):
	def test_axes_and_lengths_patient(self):
		m = bidirectional_axial()
		self.assertEqual((m.long_axis, m.short_axis), (AXIAL_LONG, AXIAL_SHORT))
		self.assertEqual((m.length, m.width, m.value), (20.0, 10.0, 20.0))
		self.assertEqual((m.unit.value, m.width_unit.value), ('mm', 'mm'))
		self.assertEqual(m.geometry.domain, 'patient')
		self.assertEqual(m.tracking_id, BIDIRECTIONAL_ID)
		self.assertEqual([(key, v.value, v.unit.value) for key, v in m.values.items()], [((SHORT_CODE, 'SCT'), 10.0, 'mm')])
		with self.assertRaises(ValueError):
			m.values = {('x', 'y'): None}

	def test_image_domain_lengths_honour_anisotropic_spacing(self):
		m = bidirectional_image()
		self.assertEqual(m.geometry.domain, 'image')
		self.assertEqual(m.reference.sop_instance_uid, SYNTHETIC['SOPInstanceUID'])
		self.assertAlmostEqual(m.length, 20.0)
		self.assertAlmostEqual(m.width, 5.0)

	def test_uncalibrated_frame_in_pixels(self):
		m = bidirectional_image(calibration=UNCALIBRATED)
		self.assertEqual((m.length, m.width, m.unit.value, m.width_unit.value), (10.0, 10.0, '1', '1'))
		model = new_model([], measurements=[m])
		doc = roundtrip(model.create_sr(record_instances=True))
		self.assertEqual([d.severity for d in sr_diagnostics_with(model.diagnostics, condition=SR_CONDITION_MEASUREMENT_UNCALIBRATED)], ['info'])
		self.assertEqual([num_value(n)[1] for n in nums_of(group_of(doc))], ['1', '1'])

	def test_image_domain_without_calibration_cannot_be_written(self):
		m = bidirectional_image(calibration=None)
		self.assertIsNone(m.length)
		with self.assertRaises(SRDiagnosticError):
			written([m])

	def test_construction_refusals(self):
		with self.assertRaises(ValueError):
			BidirectionalMeasurement.from_axes(((0, 0, 0),), AXIAL_SHORT, frame_of_reference_uid=FOR_UID)
		with self.assertRaises(ValueError):
			BidirectionalMeasurement.from_axes(IMAGE_LONG, AXIAL_SHORT, frame_of_reference_uid=FOR_UID)
		m = bidirectional_axial()
		with self.assertRaises(ValueError):
			m.long_axis = AXIAL_LONG + AXIAL_SHORT

	def test_axes_never_swapped_and_no_perpendicularity_constraint(self):
		m = bidirectional_axial(long_axis=AXIAL_SHORT, short_axis=AXIAL_LONG)
		self.assertEqual((m.long_axis, m.length, m.width), (AXIAL_SHORT, 10.0, 20.0))
		self.assertFalse(m.axes_ordered)
		# Axes that neither cross nor are perpendicular
		skew = bidirectional_axial(short_axis=((50.0, 50.0, 30.0), (53.0, 54.0, 30.0)))
		self.assertEqual((skew.length, skew.width), (20.0, 5.0))

	def test_edit_recomputes_the_axis_edited(self):
		m = bidirectional_axial()
		m.short_axis = ((0.0, 10.0, 30.0), (0.0, 30.0, 30.0))
		self.assertEqual((m.length, m.width), (20.0, 20.0))
		m.long_axis = ((-15.0, 20.0, 30.0), (15.0, 20.0, 30.0))
		self.assertEqual((m.length, m.width), (30.0, 20.0))

	def test_rigid_transform_converts_both_axes_and_rotates_the_plane(self):
		# A rotation of 90 degrees about z, then a translation of (5, 0, 0)
		matrix = [[0.0, -1.0, 0.0, 5.0], [1.0, 0.0, 0.0, 0.0], [0.0, 0.0, 1.0, 0.0], [0.0, 0.0, 0.0, 1.0]]
		transform = ImageTransformMatrix(matrix, source_frame='planning', target_frame=FOR_UID)
		m = BidirectionalMeasurement.from_axes(((0.0, 0.0, 0.0), (10.0, 0.0, 0.0)), ((5.0, -2.0, 0.0), (5.0, 2.0, 0.0)), transform=transform,
			drawing_plane=AXIAL)
		np.testing.assert_allclose(m.long_axis, ((5.0, 0.0, 0.0), (5.0, 10.0, 0.0)), atol=1e-12)
		np.testing.assert_allclose(m.short_axis, ((7.0, 5.0, 0.0), (3.0, 5.0, 0.0)), atol=1e-12)
		np.testing.assert_allclose(m.drawing_plane, ((0.0, 1.0, 0.0), (-1.0, 0.0, 0.0)), atol=1e-12)
		self.assertEqual(m.frame_of_reference_uid, FOR_UID)
		self.assertEqual((m.length, m.width), (10.0, 4.0))

	def test_non_rigid_transform_refused(self):
		scaled = ImageTransformMatrix([[2.0, 0, 0, 0], [0, 1.0, 0, 0], [0, 0, 1.0, 0], [0, 0, 0, 1.0]], source_frame='planning', target_frame=FOR_UID)
		with self.assertRaises(ValueError) as ctx:
			BidirectionalMeasurement.from_axes(AXIAL_LONG, AXIAL_SHORT, transform=scaled)
		self.assertIn('orthonormal rotation', str(ctx.exception))

	def test_degenerate_axis_refused_on_write(self):
		for long_axis, short_axis in ((((0.0, 0.0, 0.0), (0.005, 0.0, 0.0)), AXIAL_SHORT), (AXIAL_LONG, ((1.0, 1.0, 30.0), (1.0, 1.0, 30.0)))):
			m = bidirectional_axial(long_axis=long_axis, short_axis=short_axis)
			self.assertTrue(m.is_degenerate)
			with self.assertRaises(SRDiagnosticError) as ctx:
				written([m])
			self.assertEqual(ctx.exception.diagnostic.condition, SR_CONDITION_DEGENERATE_GEOMETRY)


class BidirectionalWriterTestCase(unittest.TestCase):
	def test_patient_domain_layout(self):
		m = bidirectional_axial(description='Target', text='Note', location='Liver', views=['axial'], series_uid=SYNTHETIC['SeriesInstanceUID'],
			drawing_plane=AXIAL, display=DisplayAttributes.from_color(LINE_TEXT))
		doc = written([m])
		self.assertEqual(str(doc.SOPClassUID), DCMSR_SOP_CLASS_COMPREHENSIVE_3D)
		group = group_of(doc)
		self.assertEqual(srdoc_children(group)[0].TextValue, BIDIRECTIONAL_ID)
		self.assertEqual(srdoc_children(group)[1].UID, TRACKING)
		long_num, short_num = nums_of(group)
		self.assertEqual(concepts_of(group), [LONG_CODE, SHORT_CODE])
		self.assertEqual([num_value(n) for n in (long_num, short_num)], [(20.0, 'mm'), (10.0, 'mm')])
		for num, axis in ((long_num, AXIAL_LONG), (short_num, AXIAL_SHORT)):
			(scoord,) = coordinates_of(num)
			self.assertEqual((scoord.ValueType, scoord.GraphicType, scoord.RelationshipType), ('SCOORD3D', 'POLYLINE', 'INFERRED FROM'))
			self.assertEqual(scoord.ReferencedFrameOfReferenceUID, FOR_UID)
			np.testing.assert_allclose(np.reshape([float(v) for v in scoord.GraphicData], (2, 3)), axis)
			self.assertNotIn('ConceptNameCodeSequence', scoord)
		# The properties are children of the Long Axis NUM only
		concepts = [dcm_code_value(c.ConceptNameCodeSequence[0]) for c in srdoc_children(long_num) if c.get('ConceptNameCodeSequence')]
		for concept in (DCMSR_MEASUREMENT_DESCRIPTION, DCMSR_MEASUREMENT_TEXT, DCMSR_MEASUREMENT_VIEW, DCMSR_MEASUREMENT_SOURCE_SERIES,
				DCMSR_MEASUREMENT_DRAWING_PLANE, DCMSR_DISPLAY_ATTRIBUTES):
			self.assertIn(concept.value, concepts)
		self.assertEqual([c.ValueType for c in srdoc_children(short_num)], ['SCOORD3D'])

	def test_image_domain_layout(self):
		doc = written([bidirectional_image()])
		for num, axis in zip(nums_of(group_of(doc)), (IMAGE_LONG, IMAGE_SHORT)):
			(scoord,) = coordinates_of(num)
			self.assertEqual((scoord.ValueType, scoord.GraphicType), ('SCOORD', 'POLYLINE'))
			np.testing.assert_allclose(np.reshape([float(v) for v in scoord.GraphicData], (2, 2)), axis)
			(image,) = srdoc_children(scoord)
			self.assertEqual((image.ValueType, image.RelationshipType), ('IMAGE', 'SELECTED FROM'))
			self.assertEqual(image.ReferencedSOPSequence[0].ReferencedSOPInstanceUID, SYNTHETIC['SOPInstanceUID'])
		self.assertEqual([num_value(n)[1] for n in nums_of(group_of(doc))], ['mm', 'mm'])
		self.assertNotIn(DCMSR_MEASUREMENT_SOURCE_SERIES.value, [dcm_code_value(c.ConceptNameCodeSequence[0])
			for c in srdoc_children(nums_of(group_of(doc))[0]) if c.get('ConceptNameCodeSequence')])

	def test_short_axis_longer_written_as_given(self):
		m = bidirectional_axial(long_axis=AXIAL_SHORT, short_axis=AXIAL_LONG)
		model = new_model([], measurements=[m])
		doc = roundtrip(model.create_sr(record_instances=True))
		self.assertEqual([num_value(n)[0] for n in nums_of(group_of(doc))], [10.0, 20.0])
		np.testing.assert_allclose(np.reshape([float(v) for v in coordinates_of(nums_of(group_of(doc))[0])[0].GraphicData], (2, 3)), AXIAL_SHORT)
		self.assertEqual([(d.severity, d.value) for d in sr_diagnostics_with(model.diagnostics, condition=SR_CONDITION_AXES_NOT_ORDERED)], [('info', 20.0)])

	def test_tracking_uid_generated_when_absent(self):
		m = bidirectional_axial(tracking_uid=None)
		doc = written([m])
		self.assertTrue(m.tracking_uid)
		self.assertEqual(srdoc_children(group_of(doc))[1].UID, m.tracking_uid)


class BidirectionalReaderTestCase(OracleMixin, unittest.TestCase):
	def doc(self, measurement=None):
		return written([measurement or bidirectional_axial(description='Target', views=['axial'], display=DisplayAttributes.from_color(LINE_TEXT))])

	def test_read_both_axes(self):
		model = reparse(self.doc(), strict=True)
		m = measurements_of(model)[0]
		self.assertIsInstance(m, BidirectionalMeasurement)
		self.assertEqual((m.long_axis, m.short_axis, m.length, m.width), (AXIAL_LONG, AXIAL_SHORT, 20.0, 10.0))
		self.assertEqual((m.frame_of_reference_uid, m.tracking_id, m.tracking_uid), (FOR_UID, BIDIRECTIONAL_ID, TRACKING))
		self.assertEqual((m.description, m.views, tuple(m.display.color['Line'])), ('Target', ('axial',), LINE_TEXT['Line']))
		self.assertEqual(m.short_axis_extra_items, ())
		self.assertFalse(model.diagnostics)

	def test_image_domain_read(self):
		model = reparse(self.doc(bidirectional_image()), strict=True, calibrations=FRAMES)
		m = measurements_of(model)[0]
		self.assertEqual((m.geometry.domain, m.long_axis, m.short_axis), ('image', IMAGE_LONG, IMAGE_SHORT))
		self.assertAlmostEqual(m.length, 20.0)
		self.assertAlmostEqual(m.width, 5.0)
		self.assertEqual(m.reference.sop_instance_uid, SYNTHETIC['SOPInstanceUID'])
		self.assertFalse(model.diagnostics)

	def test_cornerstone3d_identifier_read(self):
		doc = self.doc()
		srdoc_children(group_of(doc))[0].TextValue = BIDIRECTIONAL_ID_3D
		m = measurements_of(reparse(doc, strict=True))[0]
		self.assertIsInstance(m, BidirectionalMeasurement)
		self.assertEqual(srdoc_children(group_of(roundtrip(reparse(doc).create_sr())))[0].TextValue, BIDIRECTIONAL_ID_3D)

	def test_srt_codes_read_and_kept(self):
		doc = self.doc()
		for num, code in zip(nums_of(group_of(doc)), ('G-A185', 'G-A186')):
			name = num.ConceptNameCodeSequence[0]
			name.CodeValue, name.CodingSchemeDesignator = code, 'SRT'
		m = measurements_of(reparse(doc, strict=True))[0]
		self.assertIsInstance(m, BidirectionalMeasurement)
		self.assertEqual(list(m.values), [('G-A186', 'SRT')])
		again = roundtrip(reparse(doc).create_sr())
		self.assertEqual([(dcm_code_value(n.ConceptNameCodeSequence[0]), n.ConceptNameCodeSequence[0].CodingSchemeDesignator)
			for n in nums_of(group_of(again))], [('G-A185', 'SRT'), ('G-A186', 'SRT')])
		self.assert_same_content(doc, again)

	def test_meaning_only_and_any_order(self):
		doc = self.doc()
		group = group_of(doc)
		long_num, short_num = nums_of(group)
		for num in (long_num, short_num):
			name = num.ConceptNameCodeSequence[0]
			name.CodeValue, name.CodingSchemeDesignator = 'X-%s' % name.CodeMeaning.split()[0].upper(), 'Vendor'
		# Short Axis first in document order
		children = srdoc_children(group)
		group.ContentSequence = Sequence([c for c in children if c is not long_num and c is not short_num] + [short_num, long_num])
		m = measurements_of(reparse(doc))[0]
		self.assertIsInstance(m, BidirectionalMeasurement)
		self.assertEqual((m.long_axis, m.short_axis, m.length, m.width), (AXIAL_LONG, AXIAL_SHORT, 20.0, 10.0))
		self.assertEqual(m.description, 'Target')
		again = roundtrip(reparse(doc).create_sr())
		self.assertEqual(concepts_of(group_of(again)), ['X-LONG', 'X-SHORT'])
		self.assert_same_content(doc, again)

	def test_property_precedence_and_unknown_children_stay_under_their_num(self):
		doc = self.doc(bidirectional_axial())
		long_num, short_num = nums_of(group_of(doc))
		long_num.ContentSequence = Sequence(srdoc_children(long_num) + [property_item(DCMSR_MEASUREMENT_DESCRIPTION, 'From long axis'),
			text_item(VENDOR_NOTE, 'unknown-child-of-long')])
		short_num.ContentSequence = Sequence(srdoc_children(short_num) + [property_item(DCMSR_MEASUREMENT_DESCRIPTION, 'From short axis'),
			property_item(DCMSR_MEASUREMENT_TEXT, 'Short axis note')])
		# Both NUMs carry properties: the first in document order (Long Axis) is the source, nothing is relocated
		m = measurements_of(reparse(doc))[0]
		self.assertEqual((m.description, m.text), ('From long axis', None))
		self.assertEqual([i.TextValue for i in m.extra_items], ['unknown-child-of-long'])
		self.assertEqual([i.TextValue for i in m.short_axis_extra_items], ['From short axis', 'Short axis note'])
		again = roundtrip(reparse(doc).create_sr())
		self.assert_same_content(doc, again)
		out_long, out_short = nums_of(group_of(again))
		self.assertEqual([i.TextValue for i in named(srdoc_children(out_short), DCMSR_MEASUREMENT_DESCRIPTION.value)], ['From short axis'])

		# Without properties on the Long Axis NUM, the Short Axis NUM is the source; they are written under the Long Axis NUM
		long_num.ContentSequence = Sequence([c for c in srdoc_children(long_num) if c.get('ValueType') == 'SCOORD3D'])
		m = measurements_of(reparse(doc))[0]
		self.assertEqual((m.description, m.text, m.short_axis_extra_items), ('From short axis', 'Short axis note', ()))
		out_long, out_short = nums_of(group_of(roundtrip(reparse(doc).create_sr())))
		self.assertEqual([i.TextValue for i in named(srdoc_children(out_long), DCMSR_MEASUREMENT_DESCRIPTION.value)], ['From short axis'])
		self.assertEqual([c.ValueType for c in srdoc_children(out_short)], ['SCOORD3D'])

	def test_group_level_properties_read(self):
		doc = self.doc(bidirectional_axial())
		group = group_of(doc)
		group.ContentSequence = Sequence(srdoc_children(group) + [text_item(DCMSR_MEASUREMENT_DESCRIPTION, 'Legacy label', 'HAS CONCEPT MOD')])
		model = reparse(doc)
		self.assertEqual(measurements_of(model)[0].description, 'Legacy label')
		self.assertIn(SR_CONDITION_GROUP_LEVEL_PROPERTIES, conditions(model.diagnostics))

	def test_short_axis_longer_read_unswapped(self):
		model = reparse(self.doc(bidirectional_axial(long_axis=AXIAL_SHORT, short_axis=AXIAL_LONG)))
		m = measurements_of(model)[0]
		self.assertEqual((m.long_axis, m.length, m.width), (AXIAL_SHORT, 10.0, 20.0))
		self.assertEqual([d.severity for d in sr_diagnostics_with(model.diagnostics, condition=SR_CONDITION_AXES_NOT_ORDERED)], ['info'])
		reparse(self.doc(bidirectional_axial(long_axis=AXIAL_SHORT, short_axis=AXIAL_LONG)), strict=True)

	def test_axes_in_different_units_read_as_stored(self):
		doc = self.doc(bidirectional_axial())
		short_num = nums_of(group_of(doc))[1]
		set_ucum(short_num, 'cm', 'centimeter')
		set_number(short_num, 1.0)
		model = reparse(doc)
		m = measurements_of(model)[0]
		self.assertEqual((m.length, m.unit.value, m.width, m.width_unit.value), (20.0, 'mm', 1.0, 'cm'))
		self.assertEqual([d.severity for d in sr_diagnostics_with(model.diagnostics, condition=SR_CONDITION_AXES_UNITS_DIFFER)], ['warning'])
		self.assertNotIn(SR_CONDITION_AXES_NOT_ORDERED, conditions(model.diagnostics))
		with self.assertRaises(SRDiagnosticError):
			reparse(doc, strict=True)
		again = roundtrip(model.create_sr())
		self.assertEqual([num_value(n) for n in nums_of(group_of(again))], [(20.0, 'mm'), (1.0, 'cm')])
		self.assert_same_content(doc, again)

	def test_degenerate_axis_read_with_a_warning(self):
		doc = self.doc(bidirectional_axial())
		coordinates_of(nums_of(group_of(doc))[1])[0].GraphicData = [0.0, 20.0, 30.0, 0.0, 20.004, 30.0]
		model = reparse(doc)
		self.assertIsInstance(measurements_of(model)[0], BidirectionalMeasurement)
		self.assertIn(SR_CONDITION_DEGENERATE_GEOMETRY, conditions(sr_diagnostics_with(model.diagnostics, severity=SR_DIAGNOSTIC_WARNING)))
		with self.assertRaises(SRDiagnosticError):
			model.create_sr()

	def test_groups_the_model_cannot_hold_read_as_generic(self):
		def one_axis(doc):
			group = group_of(doc)
			short_num = nums_of(group)[1]
			group.ContentSequence = Sequence([c for c in srdoc_children(group) if c is not short_num])

		def without_geometry(doc):
			short_num = nums_of(group_of(doc))[1]
			short_num.ContentSequence = Sequence([c for c in srdoc_children(short_num) if c.get('ValueType') != 'SCOORD3D'])

		def shared_geometry(doc):
			long_num, short_num = nums_of(group_of(doc))
			short_num.ContentSequence = Sequence([copy.deepcopy(coordinates_of(long_num)[0])])

		def by_reference(doc):
			short_num = nums_of(group_of(doc))[1]
			reference = copy.deepcopy(coordinates_of(short_num)[0])
			for attribute in ('ValueType', 'GraphicType', 'GraphicData', 'ReferencedFrameOfReferenceUID'):
				delattr(reference, attribute)
			reference.ReferencedContentItemIdentifier = [1, 1, 3, 1]
			short_num.ContentSequence = Sequence([reference])

		def three_points(doc):
			coordinates_of(nums_of(group_of(doc))[0])[0].GraphicData = [-10.0, 20.0, 30.0, 0.0, 20.0, 30.0, 10.0, 20.0, 30.0]

		def other_frame(doc):
			coordinates_of(nums_of(group_of(doc))[1])[0].ReferencedFrameOfReferenceUID = '2.25.7999'

		for edit in (one_axis, without_geometry, shared_geometry, by_reference, three_points, other_frame):
			doc = self.doc(bidirectional_axial())
			edit(doc)
			model = reparse(doc)
			m = measurements_of(model)[0]
			self.assertIs(type(m), Measurement, edit.__name__)
			self.assertIn(SR_CONDITION_MEASUREMENT_READ_AS_GENERIC, conditions(model.diagnostics), edit.__name__)
			if edit is not by_reference:
				self.assert_same_content(doc, roundtrip(model.create_sr()))

	def test_stored_values_authoritative_within_tolerance(self):
		doc = self.doc(bidirectional_axial())
		long_num, short_num = nums_of(group_of(doc))
		set_number(long_num, 20.008)
		set_number(short_num, 10.02)
		model = reparse(doc)
		mismatches = sr_diagnostics_with(model.diagnostics, condition=SR_CONDITION_MEASUREMENT_VALUE_MISMATCH)
		self.assertEqual([(d.severity, d.value) for d in mismatches], [('info', 10.02)])
		m = measurements_of(model)[0]
		self.assertEqual((m.length, m.width), (20.008, 10.02))
		self.assertEqual(stored_numbers(group_of(roundtrip(model.create_sr()))), stored_numbers(group_of(doc)))

	def test_from_sr_on_the_group(self):
		group = group_of(written([bidirectional_axial()]))
		m = BidirectionalMeasurement.from_sr(group)
		self.assertIsInstance(m, BidirectionalMeasurement)
		self.assertEqual((m.length, m.width), (20.0, 10.0))
		self.assertIsNone(BidirectionalMeasurement.from_sr(nums_of(group)[0]))
		self.assertIsInstance(BidirectionalMeasurement.from_sr(nums_of(group)[0], group=group), BidirectionalMeasurement)


class BidirectionalValueRuleTestCase(OracleMixin, unittest.TestCase):
	def parsed(self, measurement=None):
		doc = written([measurement or bidirectional_axial()])
		long_num, short_num = nums_of(group_of(doc))
		# Stored values that differ slightly from the points: only an unchanged axis keeps its stored value
		set_number(long_num, 20.004)
		set_number(short_num, 10.004)
		return doc, reparse(doc)

	def test_unchanged_points_keep_both_stored_values(self):
		doc, model = self.parsed()
		again = roundtrip(model.create_sr())
		self.assertEqual(stored_numbers(group_of(again)), stored_numbers(group_of(doc)))
		self.assert_same_content(doc, again)

	def test_edit_recomputes_that_axis_only(self):
		doc, model = self.parsed()
		m = measurements_of(model)[0]
		m.short_axis = ((0.0, 10.0, 30.0), (0.0, 30.0, 30.0))
		self.assertEqual((m.length, m.width), (20.004, 20.0))
		out = nums_of(group_of(roundtrip(model.create_sr())))
		self.assertEqual(out[0].MeasuredValueSequence[0].NumericValue, nums_of(group_of(doc))[0].MeasuredValueSequence[0].NumericValue)
		self.assertEqual(num_value(out[1]), (20.0, 'mm'))

		doc, model = self.parsed()
		m = measurements_of(model)[0]
		m.long_axis = ((-15.0, 20.0, 30.0), (15.0, 20.0, 30.0))
		out = nums_of(group_of(roundtrip(model.create_sr())))
		self.assertEqual(num_value(out[0]), (30.0, 'mm'))
		self.assertEqual(out[1].MeasuredValueSequence[0].NumericValue, nums_of(group_of(doc))[1].MeasuredValueSequence[0].NumericValue)

	def test_reference_change_recomputes_both(self):
		doc = written([bidirectional_image()])
		for num in nums_of(group_of(doc)):
			set_number(num, 7.0)
		model = reparse(doc, calibrations=FRAMES)
		m = measurements_of(model)[0]
		self.assertEqual((m.length, m.width), (7.0, 7.0))
		m.reference = synthetic_reference()._replace(frame_number=2)
		self.assertAlmostEqual(m.length, 20.0)
		self.assertAlmostEqual(m.width, 5.0)

	def test_qualifier_kept_while_unchanged_and_dropped_with_the_edited_value(self):
		doc = written([bidirectional_axial()])
		short_num = nums_of(group_of(doc))[1]
		short_num.NumericValueQualifierCodeSequence = Sequence([qualifier_item('114006', 'Measurement failure')])
		again = nums_of(group_of(roundtrip(reparse(doc).create_sr())))
		self.assertEqual(dcm_code_value(again[1].NumericValueQualifierCodeSequence[0]), '114006')
		model = reparse(doc)
		measurements_of(model)[0].short_axis = ((0.0, 10.0, 30.0), (0.0, 30.0, 30.0))
		out = nums_of(group_of(roundtrip(model.create_sr())))
		self.assertNotIn('NumericValueQualifierCodeSequence', out[1])
		self.assertEqual([d.severity for d in sr_diagnostics_with(model.diagnostics, condition=SR_CONDITION_QUALIFIER_DROPPED)], ['info'])

	def test_deletion_and_repeated_saves(self):
		doc = written([bidirectional_axial(), length_patient(tracking_uid='2.25.9952')])
		first = roundtrip(reparse(doc).create_sr())
		second = roundtrip(reparse(first).create_sr())
		third = roundtrip(reparse(second).create_sr())
		self.assert_same_content(first, second)
		self.assert_same_content(second, third)
		model = reparse(third)
		model.groups = [g for g in model.groups if not isinstance((g.measurements or [None])[0], BidirectionalMeasurement)]
		fourth = roundtrip(model.create_sr())
		self.assertEqual([type(m).__name__ for m in measurements_of(reparse(fourth))], ['LengthMeasurement'])


# Edits that change each image-domain axis of `bidirectional_image` from 10 px to 15 px (long) and 20 px (short)
PIXEL_EDITS = OrderedDict((('long_axis', ((20.0, 10.0), (20.0, 25.0))), ('short_axis', ((10.0, 15.0), (30.0, 15.0)))))


class BidirectionalMixedUnitEditTestCase(OracleMixin, unittest.TestCase):
	'''	An image-domain measurement with one axis stored in mm and the other in pixels: without a calibration,
		an edited axis is recomputed in pixels only when its own stored length is in pixels
	'''
	def mixed(self, pixel_role):
		doc = written([bidirectional_image()])
		long_num, short_num = nums_of(group_of(doc))
		pixel_num = long_num if pixel_role == 'long_axis' else short_num
		set_ucum(pixel_num, '1', 'px')
		set_number(pixel_num, 10.0)
		return doc

	def test_unchanged_resave_keeps_both_units(self):
		for pixel_role in PIXEL_EDITS:
			doc = self.mixed(pixel_role)
			model = reparse(doc)
			self.assertIn(SR_CONDITION_AXES_UNITS_DIFFER, conditions(model.diagnostics), pixel_role)
			again = roundtrip(model.create_sr())
			self.assertEqual([num_value(n) for n in nums_of(group_of(again))], [num_value(n) for n in nums_of(group_of(doc))], pixel_role)
			self.assert_same_content(doc, again)

	def test_edit_of_the_mm_axis_without_calibration_is_refused(self):
		for pixel_role in PIXEL_EDITS:
			mm_role = 'short_axis' if pixel_role == 'long_axis' else 'long_axis'
			model = reparse(self.mixed(pixel_role))
			m = measurements_of(model)[0]
			setattr(m, mm_role, PIXEL_EDITS[mm_role])
			self.assertIsNone(m.axis_value(mm_role)[0], mm_role)
			self.assertEqual(m.axis_value(pixel_role), (10.0, m.axis_value(pixel_role)[1]), pixel_role)
			with self.assertRaises(SRDiagnosticError) as ctx:
				model.create_sr()
			self.assertEqual(ctx.exception.diagnostic.condition, SR_CONDITION_MEASUREMENT_NOT_COMPUTABLE, mm_role)

	def test_edit_of_the_pixel_axis_without_calibration_stays_in_pixels(self):
		expected = {'long_axis': 15.0, 'short_axis': 20.0}
		for pixel_role in PIXEL_EDITS:
			doc = self.mixed(pixel_role)
			model = reparse(doc)
			setattr(measurements_of(model)[0], pixel_role, PIXEL_EDITS[pixel_role])
			source, out = nums_of(group_of(doc)), nums_of(group_of(roundtrip(model.create_sr())))
			index = 0 if pixel_role == 'long_axis' else 1
			self.assertEqual(num_value(out[index]), (expected[pixel_role], '1'), pixel_role)
			self.assertEqual(num_value(out[1 - index]), num_value(source[1 - index]), pixel_role)

	def test_calibrated_edit_recomputes_in_mm_and_keeps_the_other_axis(self):
		expected = {'long_axis': 30.0, 'short_axis': 10.0}
		for pixel_role in PIXEL_EDITS:
			for role in PIXEL_EDITS:
				doc = self.mixed(pixel_role)
				model = reparse(doc, calibrations=FRAMES)
				setattr(measurements_of(model)[0], role, PIXEL_EDITS[role])
				source, out = nums_of(group_of(doc)), nums_of(group_of(roundtrip(model.create_sr())))
				index = 0 if role == 'long_axis' else 1
				value, unit = num_value(out[index])
				self.assertEqual(unit, 'mm', (pixel_role, role))
				self.assertAlmostEqual(value, expected[role], msg=(pixel_role, role))
				self.assertEqual(num_value(out[1 - index]), num_value(source[1 - index]), (pixel_role, role))

	def test_uncalibrated_frame_and_pixel_axes_still_computed(self):
		doc = written([bidirectional_image(calibration=UNCALIBRATED)])
		model = reparse(doc)
		measurements_of(model)[0].long_axis = PIXEL_EDITS['long_axis']
		self.assertEqual([num_value(n) for n in nums_of(group_of(roundtrip(model.create_sr())))], [(15.0, '1'), (10.0, '1')])


class BidirectionalValueAssignmentTestCase(unittest.TestCase):
	'''	Assigning `value` replaces the stored long axis length: it is written, not the item it replaced
	'''
	def test_assigned_value_saved_and_reparsed(self):
		doc = written([bidirectional_axial()])
		long_num, short_num = nums_of(group_of(doc))
		long_num.NumericValueQualifierCodeSequence = Sequence([qualifier_item('114006', 'Measurement failure')])
		model = reparse(doc)
		m = measurements_of(model)[0]
		m.value = 25.0
		self.assertEqual((m.value, m.length, m.width), (25.0, 25.0, 10.0))
		again = roundtrip(model.create_sr())
		out_long, out_short = nums_of(group_of(again))
		self.assertEqual(num_value(out_long), (25.0, 'mm'))
		self.assertNotIn('NumericValueQualifierCodeSequence', out_long)
		self.assertEqual([d.severity for d in sr_diagnostics_with(model.diagnostics, condition=SR_CONDITION_QUALIFIER_DROPPED)], ['info'])
		self.assertEqual(out_short.MeasuredValueSequence[0].NumericValue, short_num.MeasuredValueSequence[0].NumericValue)
		read_back = measurements_of(reparse(again))[0]
		self.assertEqual((read_back.length, read_back.width, read_back.long_axis), (25.0, 10.0, AXIAL_LONG))

	def test_assigning_the_same_value_keeps_the_stored_item(self):
		doc = written([bidirectional_axial()])
		model = reparse(doc)
		m = measurements_of(model)[0]
		m.value = m.value
		self.assertEqual(stored_numbers(group_of(roundtrip(model.create_sr()))), stored_numbers(group_of(doc)))

	def test_axis_edit_after_an_assignment_recomputes(self):
		model = reparse(written([bidirectional_axial()]))
		m = measurements_of(model)[0]
		m.value = 25.0
		m.long_axis = ((-15.0, 20.0, 30.0), (15.0, 20.0, 30.0))
		self.assertEqual([num_value(n) for n in nums_of(group_of(roundtrip(model.create_sr())))], [(30.0, 'mm'), (10.0, 'mm')])


class BidirectionalOutOfPlaneTestCase(OracleMixin, unittest.TestCase):
	def test_drawing_plane_holds_all_four_points(self):
		long_axis, short_axis = oblique_axes()
		m = bidirectional_axial(long_axis=long_axis, short_axis=short_axis, drawing_plane=(OBLIQUE_WIDTH, OBLIQUE_HEIGHT),
			series_uid=SYNTHETIC['SeriesInstanceUID'])
		model = reparse(written([m]), strict=True)
		self.assertEqual(measurements_of(model)[0].drawing_plane_text, '0.707107,-0.707107,0,0.408248,0.408248,-0.816497')
		# A short axis 2 mm out of the plane fails it, on write and on read
		normal = np.cross(OBLIQUE_WIDTH, OBLIQUE_HEIGHT)
		lifted = tuple(tuple(float(c) for c in np.asarray(p) + 2.0 * normal) for p in short_axis)
		with self.assertRaises(ValueError) as ctx:
			written([bidirectional_axial(long_axis=long_axis, short_axis=lifted, drawing_plane=(OBLIQUE_WIDTH, OBLIQUE_HEIGHT))])
		self.assertIn('points-within-1mm', str(ctx.exception))
		doc = written([m])
		coordinates_of(nums_of(group_of(doc))[1])[0].GraphicData = [c for p in lifted for c in p]
		model = reparse(doc)
		read_back = measurements_of(model)[0]
		self.assertIsNone(read_back.drawing_plane)
		self.assertEqual([d.severity for d in sr_diagnostics_with(model.diagnostics, condition=SR_CONDITION_INVALID_DRAWING_PLANE)], ['warning'])
		again = roundtrip(model.create_sr())
		self.assertEqual(named(srdoc_children(nums_of(group_of(again))[0]), DCMSR_MEASUREMENT_DRAWING_PLANE.value)[0].TextValue,
			'0.707107,-0.707107,0,0.408248,0.408248,-0.816497')
		self.assert_same_content(doc, again)

	def test_axes_in_no_common_plane_are_legal(self):
		m = bidirectional_axial(short_axis=((0.0, 20.0, 25.0), (0.0, 20.0, 35.0)), series_uid=SYNTHETIC['SeriesInstanceUID'])
		doc = written([m])
		model = reparse(doc, strict=True)
		read_back = measurements_of(model)[0]
		self.assertEqual((read_back.length, read_back.width, read_back.drawing_plane), (20.0, 10.0, None))
		self.assert_same_content(doc, roundtrip(model.create_sr()))

	def test_one_source_series(self):
		doc = written([bidirectional_axial(series_uid=SYNTHETIC['SeriesInstanceUID'])])
		long_num, short_num = nums_of(group_of(doc))
		self.assertEqual([c.UID for c in named(srdoc_children(long_num), DCMSR_MEASUREMENT_SOURCE_SERIES.value)], [SYNTHETIC['SeriesInstanceUID']])
		self.assertEqual(named(srdoc_children(short_num), DCMSR_MEASUREMENT_SOURCE_SERIES.value), [])
		self.assertEqual(measurements_of(reparse(doc))[0].series_uid, SYNTHETIC['SeriesInstanceUID'])


FIXTURE_DATE = '2026-1113'


def fixture_path(topic):
	return os.path.join(FIXTURES, 'sonador-io.%s.%s.dcm' % (topic, FIXTURE_DATE))


def fixture_properties(topic, view, **kwargs):
	values = dict(description='%s measurement' % topic, text='Synthetic', location='Synthetic location', views=[view],
		display=DisplayAttributes.from_color(LINE_TEXT), tracking_uid='2.25.8400')
	values.update(kwargs)
	return values


def fixture_measurements(topic):
	'''	The content of each Python-written fixture
	'''
	if topic == 'bidirectional-image':
		return [bidirectional_image(**fixture_properties(topic, 'axial'))]
	if topic == 'bidirectional-axial':
		return [bidirectional_axial(**fixture_properties(topic, 'axial', series_uid=SYNTHETIC['SeriesInstanceUID'], drawing_plane=AXIAL))]
	if topic == 'bidirectional-oblique':
		long_axis, short_axis = oblique_axes()
		return [bidirectional_axial(long_axis=long_axis, short_axis=short_axis, **fixture_properties(topic, 'oblique',
			series_uid=SYNTHETIC['SeriesInstanceUID'], drawing_plane=(OBLIQUE_WIDTH, OBLIQUE_HEIGHT)))]
	raise KeyError(topic)


def write_fixture(topic):
	doc = new_model([], measurements=fixture_measurements(topic)).create_sr(record_instances=True, dcmsr_series_uid='2.25.8000',
		dcmsr_instance_uid='2.25.8001')
	doc.save_as(fixture_path(topic))


class BidirectionalFixtureTestCase(OracleMixin, unittest.TestCase):
	# topic -> (domain, long axis, short axis, length, width, Drawing-Plane text, view)
	OBLIQUE_LONG, OBLIQUE_SHORT = oblique_axes()
	FIXTURES = OrderedDict((
		('bidirectional-image', ('image', IMAGE_LONG, IMAGE_SHORT, 20.0, 5.0, None, 'axial')),
		('bidirectional-axial', ('patient', AXIAL_LONG, AXIAL_SHORT, 20.0, 10.0, '1,0,0,0,1,0', 'axial')),
		('bidirectional-oblique', ('patient', OBLIQUE_LONG, OBLIQUE_SHORT, 20.0, 8.0, '0.707107,-0.707107,0,0.408248,0.408248,-0.816497', 'oblique')),
	))

	def setUp(self):
		if os.environ.get('UPDATE_SR_FIXTURES'):
			for topic in self.FIXTURES:
				write_fixture(topic)

	def test_fixtures(self):
		for topic, (domain, long_axis, short_axis, length, width, plane, view) in self.FIXTURES.items():
			doc = read(fixture_path(topic))
			self.assertEqual((doc.PatientID, str(doc.StudyInstanceUID), str(doc.SOPClassUID)), ('ANONYMOUS', '2.25.1001', DCMSR_SOP_CLASS_COMPREHENSIVE_3D))
			model = reparse(doc, strict=True, calibrations=FRAMES)
			m = measurements_of(model)[0]
			self.assertIsInstance(m, BidirectionalMeasurement, topic)
			self.assertEqual((m.geometry.domain, m.tracking_id, m.tracking_uid), (domain, BIDIRECTIONAL_ID, '2.25.8400'), topic)
			np.testing.assert_allclose(m.long_axis, long_axis, atol=1e-4, err_msg=topic)
			np.testing.assert_allclose(m.short_axis, short_axis, atol=1e-4, err_msg=topic)
			self.assertAlmostEqual(m.length, length, places=4, msg=topic)
			self.assertAlmostEqual(m.width, width, places=4, msg=topic)
			self.assertEqual((m.unit.value, m.width_unit.value), ('mm', 'mm'), topic)
			if domain == 'patient':
				self.assertEqual((m.frame_of_reference_uid, m.series_uid), (FOR_UID, SYNTHETIC['SeriesInstanceUID']), topic)
			else:
				self.assertEqual(m.reference.sop_instance_uid, SYNTHETIC['SOPInstanceUID'], topic)
			self.assertEqual((m.drawing_plane_text, m.views, m.description, m.text, m.location),
				(plane, (view,), '%s measurement' % topic, 'Synthetic', 'Synthetic location'), topic)
			self.assertEqual(dict(m.display.color), LINE_TEXT, topic)
			self.assertEqual(concepts_of(group_of(doc)), [LONG_CODE, SHORT_CODE], topic)
			self.assertFalse(model.diagnostics, topic)
			self.assert_same_content(doc, roundtrip(model.create_sr()))


class BidirectionalAdapterFixtureTestCase(OracleMixin, unittest.TestCase):
	'''	Documents written by the stock Cornerstone3D `Bidirectional` adapter (the Viewer's locked version), from
		handles given short axis first: the stock writer puts the longer pair under the Long Axis NUM
	'''
	IMAGE = os.path.join(FIXTURES, 'cornerstone-adapters.bidirectional-image.2026-1113.dcm')
	PATIENT = os.path.join(FIXTURES, 'cornerstone-adapters.bidirectional-patient.2026-1113.dcm')
	# Calibration of the synthetic frame the adapter fixture was written for: origin, axis-aligned, 1 mm pixels
	FRAME = {SYNTHETIC['SOPInstanceUID']: CALIBRATION._replace(pixel_spacing=(1.0, 1.0))}

	def test_image_domain(self):
		doc = read(self.IMAGE)
		self.assertEqual(concepts_of(group_of(doc)), [LONG_CODE, SHORT_CODE])
		model = SonadorImagingMeasurementReport.from_sr(doc, calibrations=self.FRAME)
		m = measurements_of(model)[0]
		self.assertIsInstance(m, BidirectionalMeasurement)
		self.assertEqual((m.tracking_id, m.long_axis, m.short_axis), (BIDIRECTIONAL_ID_3D, ((10.0, 20.0), (30.0, 20.0)), ((20.0, 15.0), (20.0, 25.0))))
		self.assertEqual((m.length, m.width, m.unit.value, m.width_unit.value), (20.0, 10.0, 'mm', 'mm'))
		self.assertEqual(m.reference.sop_instance_uid, SYNTHETIC['SOPInstanceUID'])
		self.assertFalse(sr_diagnostics_with(model.diagnostics, condition=SR_CONDITION_MEASUREMENT_VALUE_MISMATCH))
		again = roundtrip(model.create_sr())
		self.assertEqual(stored_numbers(group_of(again)), stored_numbers(group_of(doc)))
		self.assert_same_content(doc, again)

	def test_patient_domain_without_stored_values(self):
		doc = read(self.PATIENT)
		model = SonadorImagingMeasurementReport.from_sr(doc)
		m = measurements_of(model)[0]
		self.assertIsInstance(m, BidirectionalMeasurement)
		self.assertEqual((m.long_axis, m.short_axis, m.frame_of_reference_uid),
			(((0.0, 20.0, 30.0), (20.0, 20.0, 30.0)), ((10.0, 15.0, 30.0), (10.0, 25.0, 30.0)), FOR_UID))
		self.assertEqual((m.length, m.width, m.unit.value, m.width_unit.value), (20.0, 10.0, 'mm', 'mm'))
		self.assertEqual([d.severity for d in sr_diagnostics_with(model.diagnostics, condition=SR_CONDITION_MEASUREMENT_WITHOUT_VALUE)],
			['warning', 'warning'])
		# Written with the lengths computed in mm
		again = roundtrip(model.create_sr())
		self.assertEqual([num_value(n) for n in nums_of(group_of(again))], [(20.0, 'mm'), (10.0, 'mm')])
		self.assertIsInstance(measurements_of(reparse(again, strict=True))[0], BidirectionalMeasurement)


VIEWER_FIXTURES = {topic: os.path.join(FIXTURES, 'sonador-viewer.%s.2026-1113.dcm' % topic)
	for topic in ('bidirectional-image', 'bidirectional-oblique')}


class BidirectionalViewerFixtureTestCase(OracleMixin, unittest.TestCase):
	'''	Documents written by the Sonador Viewer over its synthetic CT (0.8 mm rows, 1.2 mm columns), read with
		the calibration of the slice they reference
	'''
	# topic -> (domain, long axis, short axis, stored Long Axis and Short Axis text, description)
	H = math.sqrt(50.0)
	EXPECTED = OrderedDict((
		('bidirectional-image', ('image', ((27.5, 17.0), (37.5, 32.0)), ((34.5, 21.5), (30.5, 27.5)),
			['16.9705627484771', '6.78822509939085'], 'Bidirectional (image)')),
		('bidirectional-oblique', ('patient', ((-H, 0.0, 15.0 + H), (H, 0.0, 15.0 - H)), ((0.0, -6.0, 15.0), (0.0, 6.0, 15.0)),
			['20', '12'], 'Bidirectional (oblique)')),
	))

	def read_viewer(self, topic):
		doc = read(VIEWER_FIXTURES[topic])
		self.assertEqual(str(doc.SOPClassUID), DCMSR_SOP_CLASS_COMPREHENSIVE_3D)
		model = reparse(doc, strict=True, calibrations=VIEWER_CT_CALIBRATION)
		self.assertFalse(sr_diagnostics_with(model.diagnostics, severity=SR_DIAGNOSTIC_WARNING), topic)
		m = measurements_of(model)[0]
		domain, long_axis, short_axis, stored, description = self.EXPECTED[topic]
		self.assertIsInstance(m, BidirectionalMeasurement, topic)
		self.assertEqual((m.geometry.domain, m.tracking_id, m.description), (domain, BIDIRECTIONAL_ID, description), topic)
		np.testing.assert_allclose(m.long_axis, long_axis, atol=1e-4, err_msg=topic)
		np.testing.assert_allclose(m.short_axis, short_axis, atol=1e-4, err_msg=topic)

		# Long Axis then Short Axis; the stored values kept verbatim and within 0.01 mm of the points
		group = group_of(doc)
		self.assertEqual(concepts_of(group), [LONG_CODE, SHORT_CODE], topic)
		self.assertEqual([str(v) for v in stored_numbers(group)], stored, topic)
		self.assertEqual((m.length, m.width), tuple(float(v) for v in stored), topic)
		for role, value in zip(('long_axis', 'short_axis'), stored):
			computed, unit = m.computed_axis_value(role)
			self.assertEqual(unit.value, 'mm', topic)
			self.assertLess(abs(computed - float(value)), 0.01, '%s %s' % (topic, role))
		self.assertFalse(sr_diagnostics_with(model.diagnostics, condition=SR_CONDITION_MEASUREMENT_VALUE_MISMATCH), topic)

		# The properties are children of the Long Axis NUM only
		long_num, short_num = nums_of(group)
		self.assertEqual([c.ValueType for c in srdoc_children(group)], ['TEXT', 'UIDREF', 'NUM', 'NUM'], topic)
		self.assertEqual([c.TextValue for c in named(srdoc_children(long_num), DCMSR_MEASUREMENT_DESCRIPTION.value)], [description], topic)
		self.assertEqual([c.get('RelationshipType') for c in srdoc_children(short_num)], ['INFERRED FROM'], topic)

		# An unchanged re-save writes the same content tree
		again = roundtrip(model.create_sr())
		self.assertEqual(stored_numbers(group_of(again)), stored_numbers(group))
		self.assert_same_content(doc, again)
		return doc, m, long_num

	def test_image_domain(self):
		doc, m, long_num = self.read_viewer('bidirectional-image')
		self.assertEqual(m.reference.sop_instance_uid, VIEWER_CT_SLICE)
		self.assertEqual((m.drawing_plane_text, m.views), (None, ()))
		self.assertEqual((m.text, m.location), ('Synthetic', 'Synthetic location'))
		self.assertTrue(named(srdoc_children(long_num), DCMSR_DISPLAY_ATTRIBUTES.value))

	def test_oblique_patient_domain(self):
		doc, m, long_num = self.read_viewer('bidirectional-oblique')
		self.assertEqual((m.frame_of_reference_uid, m.series_uid), (VIEWER_CT_FRAME, VIEWER_CT_SERIES))
		self.assertEqual((m.drawing_plane_text, m.views), ('0.707107,0,-0.707107,0,-1,0', ('oblique',)))
		self.assertEqual((m.text, m.location, m.display), (None, None, None))
		self.assertIsNotNone(m.drawing_plane)
		self.assertEqual([c.UID for c in named(srdoc_children(long_num), DCMSR_MEASUREMENT_SOURCE_SERIES.value)], [VIEWER_CT_SERIES])


class BidirectionalDocumentTestCase(OracleMixin, unittest.TestCase):
	def test_mixed_document(self):
		measurements = [length_patient(tracking_uid='2.25.9961'), angle_patient(tracking_uid='2.25.9962'), circle_axial(tracking_uid='2.25.9963'),
			bidirectional_axial(tracking_uid='2.25.9964'), bidirectional_image(tracking_uid='2.25.9965')]
		doc = roundtrip(new_model(io_findings(), measurements=measurements).create_sr(record_instances=True))
		model = reparse(doc, strict=True, calibrations=FRAMES)
		self.assertEqual([type(m).__name__ for m in measurements_of(model)],
			['LengthMeasurement', 'AngleMeasurement', 'CircleMeasurement', 'BidirectionalMeasurement', 'BidirectionalMeasurement'])
		self.assertEqual(len([f for g in model.groups for f in (g.findings or [])]), 2)
		self.assert_same_content(doc, roundtrip(model.create_sr()))

	def test_retrieval_keys_and_frame_accessor(self):
		doc = written([bidirectional_axial(), bidirectional_image(tracking_uid='2.25.9971')])
		series = dcmsr_local_series([doc])
		by_series = series.measurements(measurement_type=SONADOR_MEASUREMENT_TYPE_BIDIRECTIONAL)
		self.assertEqual(sorted(by_series.keys()), sorted([SYNTHETIC['SeriesInstanceUID'], series.pk]))
		self.assertEqual(sorted(type(m).__name__ for c in by_series.values() for m in c), ['BidirectionalMeasurement'] * 2)
		found = series.measurements(frame_of_reference_uid=FOR_UID, measurement_type=SONADOR_MEASUREMENT_TYPE_BIDIRECTIONAL)
		self.assertEqual([m.tracking_uid for c in found.values() for m in c], [TRACKING])
