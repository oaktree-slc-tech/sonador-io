'''	Offline tests of the quantitative DICOM-SR measurements (Length, Angle, Cobb angle) at Sonador-SR
	0.3: the geometry and frame conversion, the value rules, the readers and writers per class and
	domain, property layouts and preservation, mixed documents, and the retrieval budget against a
	mocked imaging server.

	Fixtures (`ftests/test_sr/fixtures`, synthetic study, regenerated with `UPDATE_SR_FIXTURES=1`):
	`sonador-io.length.2026-1108.dcm` (patient domain), `sonador-io.length-image.2026-1108.dcm`
	(image domain), `sonador-io.angle.2026-1108.dcm` and `sonador-io.cobb-angle.2026-1108.dcm`.
	`sonador-viewer.length-image.2026-1108.dcm` is written by the other side: an image-domain Length
	produced by the Sonador Viewer's classic SR writer (oak-tree/medical-imaging/ohif-viewers `master`
	revision 0fae4bdc, version recorded in Software Versions) from a synthetic tool state, with the
	`@cornerstonejs/adapters` and dcmjs versions of that revision's lockfile. It was not drawn in a browser.

	Run from the directory containing the `sonador` package: python -m unittest sonador.ftests.test_sr.sr_measurements
'''
import copy, logging, math, os, unittest
from collections import OrderedDict

import numpy as np

from pydicom.dataset import Dataset
from pydicom.sequence import Sequence
from pydicom.sr.codedict import codes

from highdicom.sr import CodedConcept

from ...servers import SonadorServer
from ...apisettings.base import Measurement, MeasurementCollection, LengthMeasurement, AngleMeasurement, CobbAngleMeasurement, \
	Geometry, ImageFrameGeometry, ImageTransformMatrix, MeasurementValue, Finding, DCMSR_UNITS_PIXEL, \
	measurement_class_for_type, SONADOR_MEASUREMENT_TYPE_LENGTH, SONADOR_MEASUREMENT_TYPE_ANGLE, SONADOR_MEASUREMENT_TYPE_COBB_ANGLE, \
	SONADOR_MEASUREMENT_TYPE_GENERIC
from ...apisettings.display_attrs import DisplayAttributes
from ...apisettings.sr import dcm_code_value, SRDiagnosticError, sr_diagnostics_with, SR_DIAGNOSTIC_INFO, SR_DIAGNOSTIC_WARNING, SR_DIAGNOSTIC_ERROR, \
	SR_CONDITION_MEASUREMENT_VALUE_MISMATCH, SR_CONDITION_MEASUREMENT_VALUE_UNVERIFIED, SR_CONDITION_MEASUREMENT_UNCALIBRATED, \
	SR_CONDITION_MEASUREMENT_NOT_COMPUTABLE, SR_CONDITION_MEASUREMENT_READ_AS_GENERIC, SR_CONDITION_MEASUREMENT_WITHOUT_VALUE, \
	SR_CONDITION_FOREIGN_MEASUREMENT_LABEL, SR_CONDITION_DEGENERATE_GEOMETRY, SR_CONDITION_SCOORD3D_WITHOUT_FRAME, \
	SR_CONDITION_ANGLE_MIDDLE_POINTS_DIFFER, SR_CONDITION_GROUP_LEVEL_PROPERTIES, SR_CONDITION_DOCUMENT_CLASS_UPGRADED, SR_CONDITION_INVALID_GEOMETRY, \
	SR_CONDITION_BY_REFERENCE_ITEM
from ...apisettings.sr.seriestag import DCMSR_MEASUREMENT_DESCRIPTION, DCMSR_MEASUREMENT_TEXT, DCMSR_MEASUREMENT_LOCATION, DCMSR_DISPLAY_ATTRIBUTES, \
	DCMSR_SOP_CLASS_ENHANCED, DCMSR_SOP_CLASS_COMPREHENSIVE_3D, measurement_type_for_tracking_id, tracking_id_for_measurement_type
from ...apisettings.sr.measurements import DCMSR_MEASUREMENT_VIEW, image_frame_geometry
from ...imaging.helpers.sr import SonadorComprehensiveSR3D, SonadorImagingMeasurementReport
from ...imaging.helpers.sr.parse import srdoc_measurement_groups, srdoc_children
from ...imaging.orthanc import ImagingStudyCollection, ImagingSeries
from ...imaging.orthanc.sr import DcmSRInstance
from ...test.fakes import IMAGESERVER_JSON
from .sr_seriestag import read, roundtrip, tobytes, new_model, synthetic_reference, named, TAG_REJECT, SYNTHETIC, PROCEDURE, evidence_dataset, FIXTURES
from .sr_retrieval import MockPacs, CT_SERIES_UID


__all__ = ['ViewerWrittenLengthTestCase', 'InvalidGeometryTestCase', 'SpatialEditTestCase', 'SecondaryValueEditTestCase', 'RegistryAndModelTestCase', 'FrameConversionTestCase', 'CalibrationTestCase', 'LengthRoundTripTestCase', 'AngleTestCase',
	'PropertyLayoutTestCase', 'ValueRuleTestCase', 'DocumentTestCase', 'MeasurementRetrievalTestCase', 'MeasurementFixtureTestCase']


FOR_UID = '2.25.7001'
LENGTH_ID = 'cornerstoneTools@^4.0.0:Length'
ANGLE_ID = 'cornerstoneTools@^4.0.0:Angle'
COBB_ID = 'cornerstoneTools@^4.0.0:CobbAngle'
LENGTH_ID_3D = 'Cornerstone3DTools@^0.1.0:Length'
ANGLE_ID_3D = 'Cornerstone3DTools@^0.1.0:Angle'
COBB_ID_3D = 'Cornerstone3DTools@^0.1.0:CobbAngle'
TRACKING = '2.25.9001'

CALIBRATION = ImageFrameGeometry((0.0, 0.0, 0.0), (1.0, 0.0, 0.0, 0.0, 1.0, 0.0), (2.0, 0.5))
UNCALIBRATED = ImageFrameGeometry((0.0, 0.0, 0.0), (1.0, 0.0, 0.0, 0.0, 1.0, 0.0), None)
UNCALIBRATED_SQUARE = ImageFrameGeometry(None, None, None, 1.0)

CONSUMER_PROPERTY = CodedConcept('plan.side', 'Consumer-Plan', 'Plan side')
CONSUMER_VALUE = CodedConcept('left', 'Consumer-Plan', 'Left')
COLORS = {'Background': (10, 20, 30), 'Border': (200, 100, 50), 'Value': (0, 255, 0), 'Meaning': (5, 5, 250)}


def setUpModule():
	logging.getLogger('sonador').setLevel(logging.CRITICAL)


def tearDownModule():
	logging.getLogger('sonador').setLevel(logging.NOTSET)


def length_patient(**kwargs):
	kwargs.setdefault('frame_of_reference_uid', FOR_UID)
	kwargs.setdefault('tracking_uid', TRACKING)
	return LengthMeasurement([(0.0, 0.0, 0.0), (3.0, 4.0, 0.0)], **kwargs)


def length_image(**kwargs):
	kwargs.setdefault('calibration', CALIBRATION)
	kwargs.setdefault('tracking_uid', TRACKING)
	return LengthMeasurement([(10.0, 10.0), (14.0, 13.0)], reference=synthetic_reference(), **kwargs)


def angle_patient(**kwargs):
	kwargs.setdefault('frame_of_reference_uid', FOR_UID)
	kwargs.setdefault('tracking_uid', TRACKING)
	return AngleMeasurement((10.0, 0.0, 0.0), (0.0, 0.0, 0.0), (0.0, 10.0, 0.0), **kwargs)


def cobb_patient(line2=((0.0, 10.0, 0.0), (10.0, 20.0, 0.0)), **kwargs):
	kwargs.setdefault('frame_of_reference_uid', FOR_UID)
	kwargs.setdefault('tracking_uid', TRACKING)
	return CobbAngleMeasurement(((0.0, 0.0, 0.0), (10.0, 0.0, 0.0)), line2, **kwargs)


def model_of(measurements, **kwargs):
	return new_model([], measurements=list(measurements), **kwargs)


def written(measurements, **kwargs):
	'''	Serialised document of a new report (what the other side sees)
	'''
	return roundtrip(model_of(measurements).create_sr(record_instances=True, **kwargs))


def reparse(doc, cls=SonadorImagingMeasurementReport, **kwargs):
	return cls.from_sr(doc, **kwargs)


def measurements_of(model):
	return [m for g in (model.groups or []) for m in (g.measurements or [])]


def group_of(doc, index=0):
	return srdoc_measurement_groups(doc)[index]


def num_of(group):
	return next(c for c in srdoc_children(group) if c.get('ValueType') == 'NUM')


def conditions(diagnostics):
	return [d.condition for d in diagnostics]


def text_item(concept, value, relationship='HAS PROPERTIES'):
	item = Dataset()
	item.RelationshipType = relationship
	item.ValueType = 'TEXT'
	name = Dataset()
	name.CodeValue, name.CodingSchemeDesignator, name.CodeMeaning = concept.value, concept.scheme_designator, concept.meaning
	item.ConceptNameCodeSequence = Sequence([name])
	item.TextValue = value
	return item


def code_item(name_value, name_scheme, name_meaning, value, scheme, meaning, relationship='CONTAINS'):
	item = Dataset()
	item.RelationshipType = relationship
	item.ValueType = 'CODE'
	name = Dataset()
	name.CodeValue, name.CodingSchemeDesignator, name.CodeMeaning = name_value, name_scheme, name_meaning
	item.ConceptNameCodeSequence = Sequence([name])
	code = Dataset()
	code.CodeValue, code.CodingSchemeDesignator, code.CodeMeaning = value, scheme, meaning
	item.ConceptCodeSequence = Sequence([code])
	return item


def set_tracking_id(doc, tracking_id, index=0):
	next(c for c in srdoc_children(group_of(doc, index)) if c.get('ValueType') == 'TEXT' and dcm_code_value(c.ConceptNameCodeSequence[0]) == '112039').TextValue = tracking_id


def set_stored_value(doc, value, index=0):
	measured = num_of(group_of(doc, index)).MeasuredValueSequence[0]
	measured.NumericValue = value
	if 'FloatingPointValue' in measured:
		del measured.FloatingPointValue


def set_concept(doc, value, scheme, meaning, index=0):
	name = num_of(group_of(doc, index)).ConceptNameCodeSequence[0]
	name.CodeValue, name.CodingSchemeDesignator, name.CodeMeaning = value, scheme, meaning


class RegistryAndModelTestCase(unittest.TestCase):
	def test_tracking_identifiers(self):
		for form, mtype in ((LENGTH_ID, SONADOR_MEASUREMENT_TYPE_LENGTH), (LENGTH_ID_3D, SONADOR_MEASUREMENT_TYPE_LENGTH),
				(ANGLE_ID, SONADOR_MEASUREMENT_TYPE_ANGLE), (ANGLE_ID_3D, SONADOR_MEASUREMENT_TYPE_ANGLE),
				(COBB_ID, SONADOR_MEASUREMENT_TYPE_COBB_ANGLE), (COBB_ID_3D, SONADOR_MEASUREMENT_TYPE_COBB_ANGLE)):
			self.assertEqual(measurement_type_for_tracking_id(form), mtype)
		self.assertEqual(tracking_id_for_measurement_type(SONADOR_MEASUREMENT_TYPE_LENGTH), LENGTH_ID)
		self.assertEqual(tracking_id_for_measurement_type(SONADOR_MEASUREMENT_TYPE_ANGLE), ANGLE_ID)
		self.assertEqual(tracking_id_for_measurement_type(SONADOR_MEASUREMENT_TYPE_COBB_ANGLE), COBB_ID)

	def test_class_factory(self):
		self.assertIs(measurement_class_for_type(SONADOR_MEASUREMENT_TYPE_LENGTH), LengthMeasurement)
		self.assertIs(measurement_class_for_type(SONADOR_MEASUREMENT_TYPE_ANGLE), AngleMeasurement)
		self.assertIs(measurement_class_for_type(SONADOR_MEASUREMENT_TYPE_COBB_ANGLE), CobbAngleMeasurement)
		self.assertIs(measurement_class_for_type('other'), Measurement)

	def test_view_concept_is_at_0_3(self):
		self.assertEqual(DCMSR_MEASUREMENT_VIEW.value, 'Sonador-Measurement.View')
		self.assertEqual(DCMSR_MEASUREMENT_VIEW.scheme_version, '0.3')

	def test_geometry_validates_dimension_and_domain(self):
		with self.assertRaises(ValueError):
			Geometry('image', 'POLYLINE', [(1, 2, 3)])
		with self.assertRaises(ValueError):
			Geometry('patient', 'POLYLINE', [(1, 2)])
		with self.assertRaises(ValueError):
			Geometry('other', 'POLYLINE', [(1, 2)])
		self.assertTrue(Geometry('image', 'POLYLINE', [(1, 2)]).is_image)

	def test_measurement_parity_with_finding(self):
		display = DisplayAttributes.from_color(COLORS)
		m = length_patient(description='Leg', text='note', location='left', views=['coronal', '3d'], display=display, series_uid='2.25.5')
		self.assertEqual((m.description, m.text, m.location), ('Leg', 'note', 'left'))
		self.assertEqual(m.views, ('coronal', '3d'))
		self.assertIs(m.display, display)
		self.assertEqual(m.series_uid, '2.25.5')
		self.assertEqual(m.tracking_id, LENGTH_ID)
		self.assertEqual(m.tracking_uid, TRACKING)
		self.assertEqual(m.type, SONADOR_MEASUREMENT_TYPE_LENGTH)
		self.assertEqual(m.frame_of_reference_uid, FOR_UID)
		self.assertIsNone(m.series)
		self.assertIsNone(m.sr_instance)

	def test_scalar_measurement_keeps_its_interface(self):
		m = Measurement(12, name=codes.SCT.Length)
		self.assertEqual((m.value, m.uid, m.tracking_id), (12, None, None))
		self.assertFalse(m.has_group_identity)
		encoded = m.create_sr()
		self.assertEqual(encoded[0].ValueType, 'NUM')
		self.assertNotIn('ContentSequence', encoded[0])

	def test_wrong_point_count_refused(self):
		with self.assertRaises(ValueError):
			LengthMeasurement([(0, 0, 0)], frame_of_reference_uid=FOR_UID)

	def test_handles_and_encoded_points_are_distinct(self):
		m = angle_patient()
		self.assertEqual(len(m.points), 4)
		self.assertEqual(m.points[1], m.points[2])
		self.assertEqual(m.p1, (10.0, 0.0, 0.0))
		self.assertEqual(m.vertex, (0.0, 0.0, 0.0))
		self.assertEqual(m.p3, (0.0, 10.0, 0.0))
		m.vertex = (1.0, 1.0, 0.0)
		self.assertEqual(m.points, ((10.0, 0.0, 0.0), (1.0, 1.0, 0.0), (1.0, 1.0, 0.0), (0.0, 10.0, 0.0)))


class FrameConversionTestCase(unittest.TestCase):
	MATRIX = np.array([[0, -1, 0, 10], [1, 0, 0, 20], [0, 0, 1, 30], [0, 0, 0, 1]], dtype=float)

	def transform(self, **kwargs):
		kwargs.setdefault('source_frame', 'planning')
		kwargs.setdefault('target_frame', FOR_UID)
		return ImageTransformMatrix(kwargs.pop('matrix', self.MATRIX), **kwargs)

	def test_known_point_forward_and_inverse(self):
		tx = self.transform()
		self.assertEqual(tx.convert([(1, 0, 0)]), ((10.0, 21.0, 30.0),))
		np.testing.assert_allclose(tx.invert([(10, 21, 30)]), [(1, 0, 0)], atol=1e-9)

	def test_metres_scaled_before_the_matrix(self):
		tx = self.transform(source_unit='m')
		np.testing.assert_allclose(tx.convert([(0.001, 0, 0)]), [(10, 21, 30)], atol=1e-9)
		np.testing.assert_allclose(tx.invert([(10, 21, 30)]), [(0.001, 0, 0)], atol=1e-9)

	def test_unknown_unit_refused(self):
		with self.assertRaises(ValueError):
			self.transform(source_unit='cm')

	def test_scale_refused_naming_the_check(self):
		matrix = self.MATRIX.copy()
		matrix[:3, :3] *= 2
		with self.assertRaisesRegex(ValueError, 'orthonormal rotation'):
			self.transform(matrix=matrix).convert([(1, 0, 0)])

	def test_shear_refused_naming_the_check(self):
		matrix = np.identity(4)
		matrix[0, 1] = 0.5
		with self.assertRaisesRegex(ValueError, 'orthonormal rotation'):
			self.transform(matrix=matrix).convert([(1, 0, 0)])

	def test_reflection_refused_naming_the_check(self):
		matrix = np.identity(4)
		matrix[0, 0] = -1
		with self.assertRaisesRegex(ValueError, 'determinant'):
			self.transform(matrix=matrix).convert([(1, 0, 0)])

	def test_bottom_row_refused_naming_the_check(self):
		matrix = np.identity(4)
		matrix[3, 0] = 1
		with self.assertRaisesRegex(ValueError, 'homogeneous row'):
			self.transform(matrix=matrix).convert([(1, 0, 0)])

	def test_measurement_built_through_a_transform(self):
		m = LengthMeasurement([(1, 0, 0), (2, 0, 0)], transform=self.transform())
		self.assertEqual(m.points[0], (10.0, 21.0, 30.0))
		self.assertEqual(m.frame_of_reference_uid, FOR_UID)
		self.assertAlmostEqual(m.value, 1.0)

	def test_measurement_transform_requirements(self):
		with self.assertRaises(ValueError):
			LengthMeasurement([(1, 0), (2, 0)], transform=self.transform(), reference=synthetic_reference())
		with self.assertRaises(ValueError):
			LengthMeasurement([(1, 0, 0), (2, 0, 0)], transform=ImageTransformMatrix(self.MATRIX))
		with self.assertRaises(ValueError):
			LengthMeasurement([(1, 0, 0), (2, 0, 0)], transform=self.transform(), frame_of_reference_uid='2.25.99')

	def test_existing_transform_serialisation_unchanged(self):
		ds = ImageTransformMatrix(self.MATRIX, uid='2.25.7').sr
		self.assertEqual(ds.FrameOfReferenceUID, '2.25.7')


class CalibrationTestCase(unittest.TestCase):
	def test_anisotropic_spacing_honoured(self):
		# 4 columns at 0.5 mm, 3 rows at 2 mm: row spacing runs along the column direction
		self.assertAlmostEqual(length_image().value, math.hypot(4 * 0.5, 3 * 2.0))

	def test_oblique_plane_keeps_the_length(self):
		c = math.sqrt(0.5)
		oblique = ImageFrameGeometry((5.0, 6.0, 7.0), (c, c, 0.0, -c, c, 0.0), (2.0, 0.5))
		self.assertAlmostEqual(length_image(calibration=oblique).value, math.hypot(4 * 0.5, 3 * 2.0))

	def test_patient_point_uses_the_pixel_centre_origin(self):
		geometry = ImageFrameGeometry((1.0, 2.0, 3.0), (1.0, 0.0, 0.0, 0.0, 1.0, 0.0), (2.0, 1.0))
		self.assertEqual(geometry.patient_point(0.5, 0.5), (1.0, 2.0, 3.0))
		self.assertEqual(geometry.patient_point(1.5, 1.5), (2.0, 4.0, 3.0))

	def test_uncalibrated_frame_reports_pixels(self):
		m = length_image(calibration=UNCALIBRATED)
		self.assertAlmostEqual(m.value, 5.0)
		self.assertEqual(m.unit, DCMSR_UNITS_PIXEL)

	def test_image_header_single_frame(self):
		ds = Dataset()
		ds.ImagePositionPatient, ds.ImageOrientationPatient, ds.PixelSpacing = [1, 2, 3], [1, 0, 0, 0, 1, 0], [0.5, 0.25]
		self.assertEqual(image_frame_geometry(ds), ImageFrameGeometry((1.0, 2.0, 3.0), (1.0, 0.0, 0.0, 0.0, 1.0, 0.0), (0.5, 0.25)))

	def test_imager_spacing_alone_is_not_a_calibration(self):
		ds = Dataset()
		ds.ImagerPixelSpacing = [0.2, 0.2]
		geometry = image_frame_geometry(ds)
		self.assertFalse(geometry.is_calibrated)
		self.assertTrue(geometry.is_isotropic)
		ds.ImagerPixelSpacing = [0.2, 0.4]
		self.assertFalse(image_frame_geometry(ds).is_isotropic)
		self.assertFalse(image_frame_geometry(Dataset()).is_isotropic)

	def test_multiframe_uses_the_functional_groups(self):
		def frame(position, spacing):
			item, plane, measures = Dataset(), Dataset(), Dataset()
			plane.ImagePositionPatient = position
			measures.PixelSpacing = spacing
			item.PlanePositionSequence, item.PixelMeasuresSequence = Sequence([plane]), Sequence([measures])
			return item
		ds = Dataset()
		ds.PerFrameFunctionalGroupsSequence = Sequence([frame([0, 0, 0], [1, 1]), frame([0, 0, 5], [2, 3])])
		shared, orientation = Dataset(), Dataset()
		orientation.ImageOrientationPatient = [1, 0, 0, 0, 1, 0]
		shared.PlaneOrientationSequence = Sequence([orientation])
		ds.SharedFunctionalGroupsSequence = Sequence([shared])
		geometry = image_frame_geometry(ds, frame_number=2)
		self.assertEqual(geometry.position, (0.0, 0.0, 5.0))
		self.assertEqual(geometry.pixel_spacing, (2.0, 3.0))
		self.assertEqual(geometry.orientation, (1.0, 0.0, 0.0, 0.0, 1.0, 0.0))
		with self.assertRaises(ValueError):
			image_frame_geometry(ds, frame_number=3)


class LengthRoundTripTestCase(unittest.TestCase):
	def test_patient_domain_tree(self):
		doc = written([length_patient()])
		self.assertEqual(str(doc.SOPClassUID), DCMSR_SOP_CLASS_COMPREHENSIVE_3D)
		group = group_of(doc)
		self.assertEqual([(c.ValueType, c.RelationshipType) for c in srdoc_children(group) if c.ValueType in ('TEXT', 'UIDREF')],
			[('TEXT', 'HAS OBS CONTEXT'), ('UIDREF', 'HAS OBS CONTEXT')])
		self.assertEqual(srdoc_children(group)[0].TextValue, LENGTH_ID)
		self.assertEqual(srdoc_children(group)[1].UID, TRACKING)
		num = num_of(group)
		self.assertEqual((num.RelationshipType, num.ConceptNameCodeSequence[0].CodeValue, num.ConceptNameCodeSequence[0].CodingSchemeDesignator), ('CONTAINS', 'G-D7FE', 'SRT'))
		self.assertAlmostEqual(float(num.MeasuredValueSequence[0].NumericValue), 5.0)
		self.assertEqual(num.MeasuredValueSequence[0].MeasurementUnitsCodeSequence[0].CodeValue, 'mm')
		scoord = srdoc_children(num)[0]
		self.assertEqual((scoord.ValueType, scoord.RelationshipType, scoord.GraphicType), ('SCOORD3D', 'INFERRED FROM', 'POLYLINE'))
		self.assertEqual(scoord.ReferencedFrameOfReferenceUID, FOR_UID)
		self.assertNotIn('ConceptNameCodeSequence', scoord)
		self.assertEqual(list(scoord.GraphicData), [0.0, 0.0, 0.0, 3.0, 4.0, 0.0])

	def test_image_domain_tree(self):
		doc = written([length_image()])
		scoord = srdoc_children(num_of(group_of(doc)))[0]
		self.assertEqual((scoord.ValueType, scoord.RelationshipType, scoord.GraphicType), ('SCOORD', 'INFERRED FROM', 'POLYLINE'))
		self.assertNotIn('ConceptNameCodeSequence', scoord)
		image = srdoc_children(scoord)[0]
		self.assertEqual((image.ValueType, image.RelationshipType), ('IMAGE', 'SELECTED FROM'))
		self.assertNotIn('ConceptNameCodeSequence', image)
		self.assertEqual(image.ReferencedSOPSequence[0].ReferencedSOPInstanceUID, SYNTHETIC['SOPInstanceUID'])

	def assert_parsed_length(self, parsed, value, domain):
		self.assertIsInstance(parsed, LengthMeasurement)
		self.assertAlmostEqual(parsed.value, value, places=3)
		self.assertEqual(parsed.geometry.domain, domain)
		self.assertEqual(parsed.tracking_uid, TRACKING)

	def test_patient_domain_round_trip(self):
		doc = written([length_patient()])
		parsed = measurements_of(reparse(doc))
		self.assertEqual(len(parsed), 1)
		self.assert_parsed_length(parsed[0], 5.0, 'patient')
		self.assertEqual(parsed[0].frame_of_reference_uid, FOR_UID)
		self.assertEqual(parsed[0].points, ((0.0, 0.0, 0.0), (3.0, 4.0, 0.0)))
		self.assertIsNone(parsed[0].reference)

	def test_image_domain_round_trip_resolves_the_series(self):
		doc = written([length_image()])
		parsed = measurements_of(reparse(doc))[0]
		self.assert_parsed_length(parsed, math.hypot(2.0, 6.0), 'image')
		self.assertEqual(parsed.points, ((10.0, 10.0), (14.0, 13.0)))
		self.assertEqual(parsed.reference.sop_instance_uid, SYNTHETIC['SOPInstanceUID'])
		self.assertEqual(parsed.series_uid, SYNTHETIC['SeriesInstanceUID'])
		self.assertIsNone(parsed.frame_of_reference_uid)

	def test_domain_written_back_as_read(self):
		for build, value_type in ((length_patient, 'SCOORD3D'), (length_image, 'SCOORD')):
			again = roundtrip(reparse(written([build()])).create_sr())
			self.assertEqual(srdoc_children(num_of(group_of(again)))[0].ValueType, value_type)

	def test_repeated_saves_are_stable(self):
		first = written([length_patient(description='Leg', views=['axial'], display=DisplayAttributes.from_color(COLORS))])
		second = roundtrip(reparse(first).create_sr())
		third = roundtrip(reparse(second).create_sr())
		self.assertNotEqual(second.SOPInstanceUID, third.SOPInstanceUID)
		self.assertEqual(second.SeriesInstanceUID, third.SeriesInstanceUID)
		m = measurements_of(reparse(third))[0]
		self.assertEqual((m.tracking_uid, m.description, m.views), (TRACKING, 'Leg', ('axial',)))
		for a, b in ((second, third),):
			self.assertEqual(num_of(group_of(a)).MeasuredValueSequence[0].NumericValue, num_of(group_of(b)).MeasuredValueSequence[0].NumericValue)
		self.assertEqual(third.PredecessorDocumentsSequence[0].ReferencedSeriesSequence[0].ReferencedSOPSequence[-1].ReferencedSOPInstanceUID, second.SOPInstanceUID)

	def test_both_tracking_prefixes(self):
		doc = written([length_patient()])
		set_tracking_id(doc, LENGTH_ID_3D)
		m = measurements_of(reparse(doc))[0]
		self.assertIsInstance(m, LengthMeasurement)
		self.assertEqual(m.tracking_id, LENGTH_ID_3D)
		rewritten = roundtrip(reparse(doc).create_sr())
		self.assertEqual(srdoc_children(group_of(rewritten))[0].TextValue, LENGTH_ID_3D)

	def test_sct_length_concept_accepted(self):
		doc = written([length_patient()])
		set_concept(doc, '410668003', 'SCT', 'Length')
		self.assertIsInstance(measurements_of(reparse(doc))[0], LengthMeasurement)

	def test_tracking_uid_generated_when_absent(self):
		m = LengthMeasurement([(0, 0, 0), (1, 0, 0)], frame_of_reference_uid=FOR_UID)
		doc = written([m])
		self.assertTrue(srdoc_children(group_of(doc))[1].UID)
		self.assertEqual(m.tracking_uid, srdoc_children(group_of(doc))[1].UID)

	def test_deletion_by_removing_the_group(self):
		other = '2.25.9002'
		model = reparse(written([length_patient(), length_patient(tracking_uid=other)]))
		model.groups = [g for g in model.groups if g.tracking_uid != other]
		again = roundtrip(model.create_sr())
		self.assertEqual([m.tracking_uid for m in measurements_of(reparse(again))], [TRACKING])

	def test_new_measurement_added_to_a_parsed_document(self):
		model = reparse(written([length_patient()]))
		model.measurements = [cobb_patient(tracking_uid='2.25.9003')]
		parsed = measurements_of(reparse(roundtrip(model.create_sr())))
		self.assertEqual(sorted(type(m).__name__ for m in parsed), ['CobbAngleMeasurement', 'LengthMeasurement'])

	def test_verbatim_coordinates_keep_foreign_item_content(self):
		doc = written([length_image()])
		scoord = srdoc_children(num_of(group_of(doc)))[0]
		scoord.FiducialUID = '2.25.4242'
		again = roundtrip(reparse(doc).create_sr())
		self.assertEqual(srdoc_children(num_of(group_of(again)))[0].FiducialUID, '2.25.4242')


class AngleTestCase(unittest.TestCase):
	def angle_doc(self, m):
		return written([m])

	def round_trip(self, m):
		return measurements_of(reparse(self.angle_doc(m)))[0]

	def test_ordinary_angle(self):
		m = self.round_trip(angle_patient())
		self.assertIsInstance(m, AngleMeasurement)
		self.assertAlmostEqual(m.angle, 90.0)
		self.assertEqual(m.p1, (10.0, 0.0, 0.0))
		self.assertEqual(m.vertex, (0.0, 0.0, 0.0))
		self.assertEqual(m.p3, (0.0, 10.0, 0.0))

	def test_angle_encoding(self):
		doc = self.angle_doc(angle_patient())
		num = num_of(group_of(doc))
		self.assertEqual((num.ConceptNameCodeSequence[0].CodeValue, num.ConceptNameCodeSequence[0].CodingSchemeDesignator), ('285285000', 'SCT'))
		self.assertEqual(num.MeasuredValueSequence[0].MeasurementUnitsCodeSequence[0].CodeValue, 'deg')
		self.assertEqual(srdoc_children(group_of(doc))[0].TextValue, ANGLE_ID)
		self.assertEqual(list(srdoc_children(num)[0].GraphicData), [10.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 10.0, 0.0])

	def test_independent_line_cobb_angle_example(self):
		m = self.round_trip(cobb_patient())
		self.assertIsInstance(m, CobbAngleMeasurement)
		self.assertAlmostEqual(m.angle, 135.0)
		self.assertEqual(m.line1, ((0.0, 0.0, 0.0), (10.0, 0.0, 0.0)))
		self.assertEqual(m.line2, ((0.0, 10.0, 0.0), (10.0, 20.0, 0.0)))

	def test_reversed_endpoints_give_the_supplement(self):
		self.assertAlmostEqual(cobb_patient(line2=((10.0, 20.0, 0.0), (0.0, 10.0, 0.0))).value, 45.0)

	def test_shared_endpoint_keeps_the_cobb_type(self):
		m = self.round_trip(cobb_patient(line2=((10.0, 0.0, 0.0), (20.0, 10.0, 0.0))))
		self.assertIsInstance(m, CobbAngleMeasurement)
		self.assertEqual(m.points[1], m.points[2])

	def test_degenerate_lines_refused_on_write_warned_on_read(self):
		for m in (cobb_patient(line2=((1.0, 1.0, 0.0), (1.0, 1.0, 0.0))),
				AngleMeasurement((0.0, 0.0, 0.0), (0.0, 0.0, 0.0), (1.0, 0.0, 0.0), frame_of_reference_uid=FOR_UID, tracking_uid=TRACKING)):
			with self.assertRaises(SRDiagnosticError) as raised:
				model_of([m]).create_sr(record_instances=True)
			self.assertEqual(raised.exception.diagnostic.condition, SR_CONDITION_DEGENERATE_GEOMETRY)

		doc = written([cobb_patient()])
		scoord = srdoc_children(num_of(group_of(doc)))[0]
		scoord.GraphicData = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 10.0, 0.0, 10.0, 20.0, 0.0]
		model = reparse(doc)
		self.assertIn(SR_CONDITION_DEGENERATE_GEOMETRY, conditions(sr_diagnostics_with(model.diagnostics, severity=SR_DIAGNOSTIC_WARNING)))
		self.assertIsInstance(measurements_of(model)[0], CobbAngleMeasurement)
		with self.assertRaises(SRDiagnosticError):
			reparse(doc, strict=True)

	def test_angle_with_distinct_middle_points_reads_0_1_3(self):
		doc = self.angle_doc(angle_patient())
		scoord = srdoc_children(num_of(group_of(doc)))[0]
		data = list(scoord.GraphicData)
		data[6:9] = [50.0, 50.0, 50.0]
		scoord.GraphicData = data
		diagnostics = []
		model = reparse(doc, diagnostics=diagnostics)
		m = measurements_of(model)[0]
		self.assertIsInstance(m, AngleMeasurement)
		self.assertAlmostEqual(m.vertex[0], 0.0)
		self.assertEqual(m.p3, (0.0, 10.0, 0.0))
		self.assertAlmostEqual(m.computed_value()[0], 90.0)
		self.assertIn(SR_CONDITION_ANGLE_MIDDLE_POINTS_DIFFER, conditions(sr_diagnostics_with(model.diagnostics, severity=SR_DIAGNOSTIC_WARNING)))

	def test_identifier_decides_the_type(self):
		doc = self.angle_doc(cobb_patient())
		set_tracking_id(doc, ANGLE_ID)
		m = measurements_of(reparse(doc))[0]
		self.assertIsInstance(m, AngleMeasurement)

	def test_unknown_identifier_is_generic_with_values(self):
		doc = self.angle_doc(cobb_patient())
		set_tracking_id(doc, 'vendor:thing')
		model = reparse(doc)
		m = measurements_of(model)[0]
		self.assertIs(type(m), Measurement)
		self.assertEqual(m.type, SONADOR_MEASUREMENT_TYPE_GENERIC)
		self.assertEqual(len(m.geometry.points), 4)
		self.assertAlmostEqual(m.value, 135.0, places=3)

	def test_wrong_point_count_stays_generic(self):
		doc = self.angle_doc(cobb_patient())
		scoord = srdoc_children(num_of(group_of(doc)))[0]
		scoord.GraphicData = list(scoord.GraphicData)[:9]
		model = reparse(doc)
		m = measurements_of(model)[0]
		self.assertIs(type(m), Measurement)
		self.assertIn(SR_CONDITION_MEASUREMENT_READ_AS_GENERIC, conditions(sr_diagnostics_with(model.diagnostics, severity=SR_DIAGNOSTIC_WARNING)))
		# The generic form re-emits what it read
		again = roundtrip(model.create_sr())
		self.assertEqual(len(srdoc_children(num_of(group_of(again)))[0].GraphicData), 9)

	def test_sct_angle_concept_accepted(self):
		doc = self.angle_doc(angle_patient())
		set_concept(doc, '1483009', 'SCT', 'Angle')
		self.assertIsInstance(measurements_of(reparse(doc))[0], AngleMeasurement)

	def test_cornerstone3d_prefix(self):
		doc = self.angle_doc(cobb_patient())
		set_tracking_id(doc, COBB_ID_3D)
		self.assertIsInstance(measurements_of(reparse(doc))[0], CobbAngleMeasurement)

	def angle_image(self, calibration):
		return CobbAngleMeasurement(((0.0, 0.0), (10.0, 0.0)), ((0.0, 0.0), (0.0, 10.0)), reference=synthetic_reference(), calibration=calibration, tracking_uid=TRACKING)

	def test_image_domain_uses_physical_geometry(self):
		# Lines along a diagonal: anisotropic spacing changes the angle from 90 degrees
		anisotropic = ImageFrameGeometry((0.0, 0.0, 0.0), (1.0, 0.0, 0.0, 0.0, 1.0, 0.0), (1.0, 2.0))
		m = CobbAngleMeasurement(((0.0, 0.0), (10.0, 10.0)), ((0.0, 0.0), (10.0, -10.0)), reference=synthetic_reference(), calibration=anisotropic, tracking_uid=TRACKING)
		# v1 = (20, 10), v2 = p3 - p4 = (-20, 10) in millimetres
		expected = math.degrees(math.acos((-400 + 100) / (math.hypot(20, 10) ** 2)))
		self.assertAlmostEqual(m.value, expected)

	def test_uncalibrated_angle_on_square_pixels(self):
		self.assertAlmostEqual(self.angle_image(UNCALIBRATED_SQUARE).value, 90.0)

	def test_uncalibrated_angle_unknown_spacing_refused(self):
		with self.assertRaises(SRDiagnosticError) as raised:
			model_of([self.angle_image(UNCALIBRATED)]).create_sr(record_instances=True)
		self.assertEqual(raised.exception.diagnostic.condition, SR_CONDITION_MEASUREMENT_NOT_COMPUTABLE)

	def test_image_domain_read_without_calibration_keeps_the_stored_value(self):
		doc = written([self.angle_image(CALIBRATION)])
		model = reparse(doc)
		m = measurements_of(model)[0]
		self.assertIsNotNone(m.value)
		self.assertIn(SR_CONDITION_MEASUREMENT_VALUE_UNVERIFIED, conditions(sr_diagnostics_with(model.diagnostics, severity=SR_DIAGNOSTIC_INFO)))


class PropertyLayoutTestCase(unittest.TestCase):
	def measurement(self, **kwargs):
		kwargs.setdefault('description', 'Leg length')
		kwargs.setdefault('text', 'Note')
		kwargs.setdefault('location', 'Left')
		kwargs.setdefault('views', ['coronal', '3d'])
		kwargs.setdefault('display', DisplayAttributes.from_color(COLORS))
		return length_image(**kwargs)

	def assert_properties(self, m):
		self.assertEqual((m.description, m.text, m.location), ('Leg length', 'Note', 'Left'))
		self.assertEqual(m.views, ('coronal', '3d'))
		self.assertEqual(tuple(m.display.color['Border']), COLORS['Border'])

	def test_nested_layout_under_the_num(self):
		doc = written([self.measurement()])
		num = num_of(group_of(doc))
		names = [dcm_code_value(c.ConceptNameCodeSequence[0]) for c in srdoc_children(num) if 'ConceptNameCodeSequence' in c]
		self.assertEqual(names, [DCMSR_MEASUREMENT_DESCRIPTION.value, DCMSR_MEASUREMENT_TEXT.value, DCMSR_MEASUREMENT_LOCATION.value,
			DCMSR_MEASUREMENT_VIEW.value, DCMSR_MEASUREMENT_VIEW.value, DCMSR_DISPLAY_ATTRIBUTES.value])
		container = named(srdoc_children(num), DCMSR_DISPLAY_ATTRIBUTES.value)[0]
		self.assertEqual((container.ValueType, container.RelationshipType), ('CONTAINER', 'HAS PROPERTIES'))
		self.assert_properties(measurements_of(reparse(doc))[0])

	def test_no_group_level_properties_written(self):
		group = group_of(written([self.measurement()]))
		self.assertEqual([c.ValueType for c in srdoc_children(group)], ['TEXT', 'UIDREF', 'NUM'])

	def test_flat_layout_for_enhanced_sr(self):
		doc = written([self.measurement()])
		doc.SOPClassUID = DCMSR_SOP_CLASS_ENHANCED
		doc.file_meta.MediaStorageSOPClassUID = DCMSR_SOP_CLASS_ENHANCED
		num = num_of(group_of(doc))
		container = named(srdoc_children(num), DCMSR_DISPLAY_ATTRIBUTES.value)[0]
		color = srdoc_children(container)[0]
		flat = [c for c in srdoc_children(num) if not (('ConceptNameCodeSequence' in c) and dcm_code_value(c.ConceptNameCodeSequence[0]) == DCMSR_DISPLAY_ATTRIBUTES.value)]
		for role_item in srdoc_children(color):
			role_item.RelationshipType = 'HAS PROPERTIES'
			flat.append(role_item)
		num.ContentSequence = Sequence(flat)
		model = reparse(doc)
		self.assert_properties(measurements_of(model)[0])

		again = roundtrip(model.create_sr())
		self.assertEqual(str(again.SOPClassUID), DCMSR_SOP_CLASS_ENHANCED)
		children = srdoc_children(num_of(group_of(again)))
		self.assertFalse(named(children, DCMSR_DISPLAY_ATTRIBUTES.value))
		self.assertEqual(len([c for c in children if 'ConceptNameCodeSequence' in c and dcm_code_value(c.ConceptNameCodeSequence[0]).startswith('Sonador-DisplayAttributes.Color.')]), 4)

	def test_enhanced_parent_upgraded_for_patient_domain(self):
		doc = written([self.measurement()])
		doc.SOPClassUID = DCMSR_SOP_CLASS_ENHANCED
		doc.file_meta.MediaStorageSOPClassUID = DCMSR_SOP_CLASS_ENHANCED
		model = reparse(doc)
		model.measurements = [length_patient(tracking_uid='2.25.9100')]
		again = roundtrip(model.create_sr())
		self.assertEqual(str(again.SOPClassUID), DCMSR_SOP_CLASS_COMPREHENSIVE_3D)
		self.assertIn(SR_CONDITION_DOCUMENT_CLASS_UPGRADED, conditions(model.diagnostics))

	def legacy_doc(self):
		'''	Group-level siblings: the layout of the pre-0.3 documents
		'''
		doc = written([self.measurement()])
		group, num = group_of(doc), num_of(group_of(doc))
		siblings = []
		for child in srdoc_children(num):
			if child.get('ValueType') in ('SCOORD', 'SCOORD3D'):
				continue
			child.RelationshipType = 'HAS CONCEPT MOD' if child.ValueType == 'TEXT' else 'CONTAINS'
			siblings.append(child)
		num.ContentSequence = Sequence([c for c in srdoc_children(num) if c.get('ValueType') in ('SCOORD', 'SCOORD3D')])
		group.ContentSequence = Sequence(srdoc_children(group) + siblings)
		return doc

	def test_group_level_layout_read_with_a_warning_and_rewritten_under_the_num(self):
		doc = self.legacy_doc()
		model = reparse(doc)
		self.assert_properties(measurements_of(model)[0])
		self.assertIn(SR_CONDITION_GROUP_LEVEL_PROPERTIES, conditions(sr_diagnostics_with(model.diagnostics, severity=SR_DIAGNOSTIC_WARNING)))
		with self.assertRaises(SRDiagnosticError):
			reparse(doc, strict=True)
		group = group_of(roundtrip(model.create_sr()))
		self.assertEqual([c.ValueType for c in srdoc_children(group)], ['TEXT', 'UIDREF', 'NUM'])
		self.assert_properties(measurements_of(reparse(roundtrip(reparse(doc).create_sr())))[0])

	def test_finding_children_stay_with_the_finding(self):
		doc = written([self.measurement(description=None, text=None, location=None, views=[], display=None)])
		group = group_of(doc)
		finding = code_item('121071', 'DCM', 'Finding', 'x', 'Consumer-Plan', 'X')
		finding.ContentSequence = Sequence([text_item(DCMSR_MEASUREMENT_DESCRIPTION, 'Finding label')])
		num = num_of(group)
		num.ContentSequence = Sequence(srdoc_children(num) + [text_item(DCMSR_MEASUREMENT_DESCRIPTION, 'Num label')])
		group.ContentSequence = Sequence(srdoc_children(group) + [finding])
		model = reparse(doc)
		measurement, parsed_finding = model.groups[0].measurements[0], model.groups[0].findings[0]
		self.assertEqual(measurement.description, 'Num label')
		self.assertEqual(parsed_finding.description, 'Finding label')
		again = group_of(roundtrip(model.create_sr()))
		self.assertEqual(named(srdoc_children(num_of(again)), DCMSR_MEASUREMENT_DESCRIPTION.value)[0].TextValue, 'Num label')
		written_finding = next(c for c in srdoc_children(again) if c.get('ValueType') == 'CODE')
		self.assertEqual(named(srdoc_children(written_finding), DCMSR_MEASUREMENT_DESCRIPTION.value)[0].TextValue, 'Finding label')

	def test_consumer_coded_property_preserved_under_the_num(self):
		doc = written([self.measurement()])
		num = num_of(group_of(doc))
		num.ContentSequence = Sequence(srdoc_children(num) + [code_item(CONSUMER_PROPERTY.value, CONSUMER_PROPERTY.scheme_designator, CONSUMER_PROPERTY.meaning,
			CONSUMER_VALUE.value, CONSUMER_VALUE.scheme_designator, CONSUMER_VALUE.meaning, relationship='HAS PROPERTIES')])
		model = reparse(doc)
		m = measurements_of(model)[0]
		self.assertEqual(len(m.extra_items), 1)
		again = num_of(group_of(roundtrip(model.create_sr())))
		preserved = named(srdoc_children(again), CONSUMER_PROPERTY.value)[0]
		self.assertEqual(preserved.ConceptCodeSequence[0].CodeValue, CONSUMER_VALUE.value)

	def test_unknown_child_of_a_second_num_stays_there(self):
		doc = written([self.measurement()])
		group = group_of(doc)
		second = copy.deepcopy(num_of(group))
		name = second.ConceptNameCodeSequence[0]
		name.CodeValue, name.CodingSchemeDesignator, name.CodeMeaning = 'G-A166', 'SRT', 'Area'
		second.ContentSequence = Sequence([text_item(DCMSR_MEASUREMENT_DESCRIPTION, 'On the second NUM'), text_item(CONSUMER_PROPERTY, 'kept')])
		group.ContentSequence = Sequence(srdoc_children(group) + [second])
		model = reparse(doc)
		m = measurements_of(model)[0]
		self.assertEqual(m.description, 'Leg length')
		self.assertEqual(list(m.values), [('G-A166', 'SRT')])
		self.assertIsInstance(m.values[('G-A166', 'SRT')], MeasurementValue)
		self.assertAlmostEqual(m.values[('G-A166', 'SRT')].value, float(num_of(group).MeasuredValueSequence[0].NumericValue), places=3)

		again = group_of(roundtrip(model.create_sr()))
		numbers = [c for c in srdoc_children(again) if c.get('ValueType') == 'NUM']
		self.assertEqual(len(numbers), 2)
		self.assertEqual(named(srdoc_children(numbers[0]), DCMSR_MEASUREMENT_DESCRIPTION.value)[0].TextValue, 'Leg length')
		self.assertEqual(named(srdoc_children(numbers[1]), DCMSR_MEASUREMENT_DESCRIPTION.value)[0].TextValue, 'On the second NUM')
		self.assertEqual(named(srdoc_children(numbers[1]), CONSUMER_PROPERTY.value)[0].TextValue, 'kept')

	def test_edited_second_value_is_written(self):
		doc = written([self.measurement()])
		group = group_of(doc)
		second = copy.deepcopy(num_of(group))
		name = second.ConceptNameCodeSequence[0]
		name.CodeValue, name.CodingSchemeDesignator = 'G-A166', 'SRT'
		del second.ContentSequence
		group.ContentSequence = Sequence(srdoc_children(group) + [second])
		model = reparse(doc)
		m = measurements_of(model)[0]
		key = ('G-A166', 'SRT')
		m.values[key] = m.values[key]._replace(value=42.5)
		numbers = [c for c in srdoc_children(group_of(roundtrip(model.create_sr()))) if c.get('ValueType') == 'NUM']
		self.assertAlmostEqual(float(numbers[1].MeasuredValueSequence[0].NumericValue), 42.5)

	def test_foreign_comment_becomes_the_description_and_is_not_written(self):
		doc = written([self.measurement(description=None)])
		group = group_of(doc)
		comment = text_item(CodedConcept('121106', 'DCM', 'Comment'), 'Foreign label', relationship='CONTAINS')
		group.ContentSequence = Sequence(srdoc_children(group) + [comment])
		model = reparse(doc)
		self.assertEqual(measurements_of(model)[0].description, 'Foreign label')
		self.assertIn(SR_CONDITION_FOREIGN_MEASUREMENT_LABEL, conditions(sr_diagnostics_with(model.diagnostics, severity=SR_DIAGNOSTIC_INFO)))
		again = group_of(roundtrip(model.create_sr()))
		self.assertFalse(named(srdoc_children(again), '121106'))
		self.assertEqual(named(srdoc_children(num_of(again)), DCMSR_MEASUREMENT_DESCRIPTION.value)[0].TextValue, 'Foreign label')

	def test_free_text_finding_site_becomes_the_description(self):
		doc = written([self.measurement(description=None)])
		group = group_of(doc)
		site = code_item('G-C0E3', 'SRT', 'Finding Site', 'CORNERSTONEFREETEXT', 'CST4', 'Site label')
		group.ContentSequence = Sequence(srdoc_children(group) + [site])
		model = reparse(doc)
		self.assertEqual(measurements_of(model)[0].description, 'Site label')
		again = group_of(roundtrip(model.create_sr()))
		self.assertFalse(named(srdoc_children(again), 'G-C0E3'))

	def test_comment_kept_when_a_description_exists(self):
		doc = written([self.measurement()])
		group = group_of(doc)
		group.ContentSequence = Sequence(srdoc_children(group) + [text_item(CodedConcept('121106', 'DCM', 'Comment'), 'Foreign', relationship='CONTAINS')])
		model = reparse(doc)
		self.assertEqual(measurements_of(model)[0].description, 'Leg length')
		self.assertTrue(named(srdoc_children(group_of(roundtrip(model.create_sr()))), '121106'))

	def test_coded_finding_sites(self):
		doc = written([self.measurement()])
		group, num = group_of(doc), num_of(group_of(doc))
		num.ContentSequence = Sequence(srdoc_children(num) + [code_item('363698007', 'SCT', 'Finding Site', '7771000', 'SCT', 'Left', relationship='HAS CONCEPT MOD')])
		group.ContentSequence = Sequence(srdoc_children(group) + [code_item('G-C0E3', 'SRT', 'Finding Site', '24028007', 'SCT', 'Right')])
		model = reparse(doc)
		m = measurements_of(model)[0]
		self.assertEqual(m.finding_sites[0].ConceptCodeSequence[0].CodeValue, '7771000')
		self.assertEqual(m.group_finding_sites[0].value, '24028007')
		again = group_of(roundtrip(model.create_sr()))
		self.assertTrue(named(srdoc_children(again), 'G-C0E3'))
		self.assertTrue(named(srdoc_children(num_of(again)), '363698007'))

	def test_by_reference_child_refuses_the_rewrite(self):
		doc = written([self.measurement()])
		num = num_of(group_of(doc))
		item = Dataset()
		item.RelationshipType = 'INFERRED FROM'
		item.ReferencedContentItemIdentifier = [1, 1]
		num.ContentSequence = Sequence(srdoc_children(num) + [item])
		model = reparse(doc)
		self.assertIn(SR_CONDITION_BY_REFERENCE_ITEM, conditions(model.diagnostics))
		with self.assertRaises(SRDiagnosticError):
			model.create_sr()

	def test_item_without_a_value_type_dropped_with_an_error(self):
		doc = written([self.measurement()])
		num = num_of(group_of(doc))
		item = Dataset()
		item.RelationshipType = 'HAS PROPERTIES'
		item.TextValue = 'x'
		num.ContentSequence = Sequence(srdoc_children(num) + [item])
		model = reparse(doc)
		self.assertTrue(sr_diagnostics_with(model.diagnostics, severity=SR_DIAGNOSTIC_ERROR))
		self.assertEqual(measurements_of(model)[0].extra_items, ())


class ValueRuleTestCase(unittest.TestCase):
	def test_stored_value_authoritative_with_info_on_mismatch(self):
		doc = written([length_patient()])
		set_stored_value(doc, '5.5')
		model = reparse(doc)
		m = measurements_of(model)[0]
		self.assertEqual(m.value, 5.5)
		self.assertIn(SR_CONDITION_MEASUREMENT_VALUE_MISMATCH, conditions(sr_diagnostics_with(model.diagnostics, severity=SR_DIAGNOSTIC_INFO)))
		reparse(doc, strict=True)

	def test_difference_within_tolerance_is_silent(self):
		doc = written([length_patient()])
		set_stored_value(doc, '5.005')
		self.assertNotIn(SR_CONDITION_MEASUREMENT_VALUE_MISMATCH, conditions(reparse(doc).diagnostics))

	def test_unchanged_measurement_keeps_the_stored_value_verbatim(self):
		doc = written([length_patient()])
		set_stored_value(doc, '5.50')
		again = roundtrip(reparse(doc).create_sr())
		self.assertEqual(num_of(group_of(again)).MeasuredValueSequence[0].NumericValue.original_string, '5.50')

	def test_replaced_value_is_written(self):
		model = reparse(written([length_patient()]))
		measurements_of(model)[0].value = 7.25
		again = roundtrip(model.create_sr())
		self.assertAlmostEqual(float(num_of(group_of(again)).MeasuredValueSequence[0].NumericValue), 7.25)

	def test_edited_points_recompute(self):
		doc = written([length_patient()])
		set_stored_value(doc, '5.5')
		model = reparse(doc)
		m = measurements_of(model)[0]
		m.p2 = (6.0, 8.0, 0.0)
		self.assertAlmostEqual(m.value, 10.0)
		again = roundtrip(model.create_sr())
		self.assertAlmostEqual(float(num_of(group_of(again)).MeasuredValueSequence[0].NumericValue), 10.0)
		self.assertEqual(list(srdoc_children(num_of(group_of(again)))[0].GraphicData)[3:], [6.0, 8.0, 0.0])

	def test_edited_image_measurement_needs_a_calibration(self):
		model = reparse(written([length_image()]))
		m = measurements_of(model)[0]
		m.p2 = (20.0, 20.0)
		with self.assertRaises(SRDiagnosticError) as raised:
			model.create_sr()
		self.assertEqual(raised.exception.diagnostic.condition, SR_CONDITION_MEASUREMENT_NOT_COMPUTABLE)
		m.calibration = CALIBRATION
		again = roundtrip(model.create_sr())
		self.assertAlmostEqual(float(num_of(group_of(again)).MeasuredValueSequence[0].NumericValue), math.hypot(10 * 0.5, 10 * 2.0), places=3)

	def test_calibrations_verify_the_stored_image_value(self):
		doc = written([length_image()])
		set_stored_value(doc, '99')
		model = reparse(doc, calibrations={SYNTHETIC['SOPInstanceUID']: CALIBRATION})
		self.assertIn(SR_CONDITION_MEASUREMENT_VALUE_MISMATCH, conditions(model.diagnostics))
		self.assertEqual(measurements_of(model)[0].value, 99.0)

	def test_image_value_without_calibration_is_unverified(self):
		model = reparse(written([length_image()]))
		self.assertIn(SR_CONDITION_MEASUREMENT_VALUE_UNVERIFIED, conditions(model.diagnostics))

	def test_uncalibrated_length_round_trip(self):
		model = model_of([length_image(calibration=UNCALIBRATED)])
		doc = roundtrip(model.create_sr(record_instances=True))
		self.assertIn(SR_CONDITION_MEASUREMENT_UNCALIBRATED, conditions(sr_diagnostics_with(model.diagnostics, severity=SR_DIAGNOSTIC_INFO)))
		unit = num_of(group_of(doc)).MeasuredValueSequence[0].MeasurementUnitsCodeSequence[0]
		self.assertEqual((unit.CodeValue, unit.CodingSchemeDesignator, unit.CodeMeaning), ('1', 'UCUM', 'px'))
		parsed = reparse(doc)
		self.assertIn(SR_CONDITION_MEASUREMENT_UNCALIBRATED, conditions(parsed.diagnostics))
		self.assertAlmostEqual(measurements_of(parsed)[0].value, 5.0)
		self.assertNotIn(SR_CONDITION_MEASUREMENT_VALUE_MISMATCH, conditions(parsed.diagnostics))

	def test_uncalibrated_tolerance_is_in_pixels(self):
		doc = written([length_image(calibration=UNCALIBRATED)])
		set_stored_value(doc, '5.02')
		self.assertIn(SR_CONDITION_MEASUREMENT_VALUE_MISMATCH, conditions(reparse(doc).diagnostics))

	def test_new_image_measurement_without_calibration_refused(self):
		m = LengthMeasurement([(1.0, 1.0), (2.0, 2.0)], reference=synthetic_reference(), tracking_uid=TRACKING)
		with self.assertRaises(SRDiagnosticError) as raised:
			model_of([m]).create_sr(record_instances=True)
		self.assertEqual(raised.exception.diagnostic.condition, SR_CONDITION_MEASUREMENT_NOT_COMPUTABLE)

	def test_coincident_points_refused_on_write_warned_on_read(self):
		m = LengthMeasurement([(1.0, 1.0, 1.0), (1.0, 1.0, 1.0)], frame_of_reference_uid=FOR_UID, tracking_uid=TRACKING)
		with self.assertRaises(SRDiagnosticError) as raised:
			model_of([m]).create_sr(record_instances=True)
		self.assertEqual(raised.exception.diagnostic.condition, SR_CONDITION_DEGENERATE_GEOMETRY)
		doc = written([length_patient()])
		srdoc_children(num_of(group_of(doc)))[0].GraphicData = [1.0, 1.0, 1.0, 1.0, 1.0, 1.0]
		model = reparse(doc)
		self.assertIn(SR_CONDITION_DEGENERATE_GEOMETRY, conditions(sr_diagnostics_with(model.diagnostics, severity=SR_DIAGNOSTIC_WARNING)))
		with self.assertRaises(SRDiagnosticError):
			model.create_sr()

	def test_num_without_a_value_is_computed_on_write(self):
		doc = written([length_patient()])
		del num_of(group_of(doc)).MeasuredValueSequence
		model = reparse(doc)
		self.assertIn(SR_CONDITION_MEASUREMENT_WITHOUT_VALUE, conditions(sr_diagnostics_with(model.diagnostics, severity=SR_DIAGNOSTIC_WARNING)))
		again = roundtrip(model.create_sr())
		self.assertAlmostEqual(float(num_of(group_of(again)).MeasuredValueSequence[0].NumericValue), 5.0)

	def test_num_without_value_or_geometry_is_an_error_and_preserved(self):
		doc = written([length_patient()])
		num = num_of(group_of(doc))
		del num.MeasuredValueSequence
		del num.ContentSequence
		model = reparse(doc)
		self.assertIn(SR_CONDITION_MEASUREMENT_WITHOUT_VALUE, conditions(sr_diagnostics_with(model.diagnostics, severity=SR_DIAGNOSTIC_ERROR)))
		self.assertFalse(measurements_of(model))
		self.assertEqual(len([i for i in model.groups[0].extra_items if i.ValueType == 'NUM']), 1)
		with self.assertRaises(SRDiagnosticError):
			reparse(doc, strict=True)

	def test_scoord3d_without_frame_is_an_error_and_preserved(self):
		doc = written([length_patient()])
		del srdoc_children(num_of(group_of(doc)))[0].ReferencedFrameOfReferenceUID
		model = reparse(doc)
		self.assertIn(SR_CONDITION_SCOORD3D_WITHOUT_FRAME, conditions(sr_diagnostics_with(model.diagnostics, severity=SR_DIAGNOSTIC_ERROR)))
		m = measurements_of(model)[0]
		self.assertIs(type(m), Measurement)
		self.assertIsNone(m.geometry)
		self.assertEqual([i.ValueType for i in m.extra_items], ['SCOORD3D'])

	def test_undefined_3d_graphic_type_preserved(self):
		doc = written([length_patient()])
		set_tracking_id(doc, 'Cornerstone3DTools@^0.1.0:CircleROI')
		scoord = srdoc_children(num_of(group_of(doc)))[0]
		scoord.GraphicType = 'CIRCLE'
		model = reparse(doc)
		self.assertIn(SR_CONDITION_INVALID_GEOMETRY, conditions(sr_diagnostics_with(model.diagnostics, severity=SR_DIAGNOSTIC_WARNING)))
		again = roundtrip(model.create_sr())
		self.assertEqual(srdoc_children(num_of(group_of(again)))[0].GraphicType, 'CIRCLE')


class DocumentTestCase(unittest.TestCase):
	def mixed_model(self):
		tag = Finding.series_tag(TAG_REJECT, reference=synthetic_reference(), tracking_uid='2.25.9200')
		return new_model([tag], measurements=[length_patient(), length_image(tracking_uid='2.25.9201'),
			angle_patient(tracking_uid='2.25.9202'), cobb_patient(tracking_uid='2.25.9203')])

	def test_mixed_document(self):
		doc = roundtrip(self.mixed_model().create_sr(record_instances=True))
		model = reparse(doc)
		self.assertEqual(len(model.groups), 5)
		self.assertEqual(len(model.series_tag_findings), 1)
		self.assertEqual([type(m).__name__ for m in measurements_of(model)], ['LengthMeasurement', 'LengthMeasurement', 'AngleMeasurement', 'CobbAngleMeasurement'])
		again = reparse(roundtrip(model.create_sr()))
		self.assertEqual(sorted(m.tracking_uid for m in measurements_of(again)), sorted([TRACKING, '2.25.9201', '2.25.9202', '2.25.9203']))
		self.assertEqual(again.series_tag_findings[0].tracking_uid, '2.25.9200')

	def test_image_library_and_evidence_record_the_reference(self):
		doc = written([length_image()])
		library = next(c for c in srdoc_children(doc) if dcm_code_value(c.ConceptNameCodeSequence[0]) == '111028')
		images = srdoc_children(srdoc_children(library)[0])
		self.assertEqual(images[0].ReferencedSOPSequence[0].ReferencedSOPInstanceUID, SYNTHETIC['SOPInstanceUID'])

	def test_application_document_title_kept(self):
		model = SonadorComprehensiveSR3D(measurements=[length_patient()], report_procedure=PROCEDURE, dcm_evidence=[evidence_dataset()], person_observer='Observer^Test')
		doc = roundtrip(model.create_sr(record_instances=True))
		self.assertEqual(doc.ConceptNameCodeSequence[0].CodeValue, 'Clinical-Report')
		parsed = reparse(doc, cls=SonadorComprehensiveSR3D)
		self.assertEqual(parsed.report_title.value, 'Clinical-Report')
		self.assertIsInstance(measurements_of(parsed)[0], LengthMeasurement)
		self.assertEqual(roundtrip(parsed.create_sr()).ConceptNameCodeSequence[0].CodeValue, 'Clinical-Report')

	def test_exchange_document_title(self):
		self.assertEqual(written([length_patient()]).ConceptNameCodeSequence[0].CodeValue, '126000')

	def test_scalar_measurements_stay_in_the_primary_group(self):
		model = new_model([], measurements=[Measurement(7, name=codes.SCT.Length), length_patient()])
		doc = roundtrip(model.create_sr(record_instances=True))
		groups = srdoc_measurement_groups(doc)
		self.assertEqual(len(groups), 2)
		self.assertEqual(srdoc_children(groups[1])[0].TextValue, LENGTH_ID)

	def test_foreign_num_group_re_emitted(self):
		doc = written([length_patient()])
		group = group_of(doc)
		set_tracking_id(doc, 'vendor:tool')
		second = copy.deepcopy(num_of(group))
		name = second.ConceptNameCodeSequence[0]
		name.CodeValue, name.CodingSchemeDesignator = 'x2', 'Vendor'
		del second.ContentSequence
		group.ContentSequence = Sequence(srdoc_children(group) + [second])
		again = group_of(roundtrip(reparse(doc).create_sr()))
		self.assertEqual(srdoc_children(again)[0].TextValue, 'vendor:tool')
		self.assertEqual([dcm_code_value(c.ConceptNameCodeSequence[0]) for c in srdoc_children(again) if c.get('ValueType') == 'NUM'], ['G-D7FE', 'x2'])

	def test_collection_and_item_readers(self):
		group = group_of(written([length_patient(description='Leg')]))
		collection = MeasurementCollection.from_sr(group)
		self.assertIsInstance(collection[0], LengthMeasurement)
		item = Measurement.from_sr(num_of(group), group=group)
		self.assertIsInstance(item, LengthMeasurement)
		self.assertEqual((item.description, item.tracking_uid), ('Leg', TRACKING))
		forced = CobbAngleMeasurement.from_sr(num_of(group), group=group)
		self.assertIs(type(forced), Measurement)
		self.assertEqual(len(MeasurementCollection.from_sr(group_of(roundtrip(new_model([Finding.series_tag(TAG_REJECT, reference=synthetic_reference())]).create_sr(record_instances=True))))), 0)


class InvalidGeometryTestCase(unittest.TestCase):
	NON_FINITE = (float('nan'), float('inf'), float('-inf'))

	def test_non_finite_points_refused_at_construction(self):
		for bad in self.NON_FINITE:
			with self.assertRaises(ValueError):
				LengthMeasurement([(bad, 0, 0), (1, 0, 0)], frame_of_reference_uid=FOR_UID)
			with self.assertRaises(ValueError):
				AngleMeasurement((bad, 0, 0), (0, 0, 0), (0, 1, 0), frame_of_reference_uid=FOR_UID)
			with self.assertRaises(ValueError):
				CobbAngleMeasurement(((0, 0, bad), (1, 0, 0)), ((0, 1, 0), (1, 1, 0)), frame_of_reference_uid=FOR_UID)
			with self.assertRaises(ValueError):
				LengthMeasurement([(bad, 0), (1, 1)], reference=synthetic_reference(), calibration=CALIBRATION)

	def test_edit_to_non_finite_points_refused(self):
		for build, point in ((length_patient, (float('nan'), 0.0, 0.0)), (length_image, (float('inf'), 0.0))):
			m = measurements_of(reparse(written([build()])))[0]
			with self.assertRaises(ValueError):
				m.p2 = point

	def test_non_finite_document_coordinates_preserved_unmodelled(self):
		for bad in self.NON_FINITE:
			doc = written([cobb_patient()])
			scoord = srdoc_children(num_of(group_of(doc)))[0]
			data = list(scoord.GraphicData)
			data[0] = bad
			scoord.GraphicData = data
			doc = roundtrip(doc)
			model = reparse(doc)
			self.assertIn(SR_CONDITION_INVALID_GEOMETRY, conditions(sr_diagnostics_with(model.diagnostics, severity=SR_DIAGNOSTIC_WARNING)))
			m = measurements_of(model)[0]
			self.assertIs(type(m), Measurement)
			self.assertIsNone(m.geometry)
			self.assertAlmostEqual(m.value, 135.0, places=3)
			self.assertEqual([i.ValueType for i in m.extra_items], ['SCOORD3D'])
			with self.assertRaises(SRDiagnosticError):
				reparse(doc, strict=True)

	def test_non_finite_stored_value_reads_as_no_value(self):
		doc = written([length_patient()])
		measured = num_of(group_of(doc)).MeasuredValueSequence[0]
		measured.FloatingPointValue = float('nan')
		model = reparse(doc)
		self.assertIn(SR_CONDITION_MEASUREMENT_WITHOUT_VALUE, conditions(model.diagnostics))
		self.assertAlmostEqual(float(num_of(group_of(roundtrip(model.create_sr()))).MeasuredValueSequence[0].NumericValue), 5.0)

	def test_non_finite_scalar_refused_at_write(self):
		with self.assertRaises(SRDiagnosticError) as raised:
			Measurement(float('nan'), name=codes.SCT.Length).create_sr()
		self.assertEqual(raised.exception.diagnostic.condition, SR_CONDITION_MEASUREMENT_NOT_COMPUTABLE)

	UNUSABLE = (
		ImageFrameGeometry(None, None, (0.0, 0.0)),
		ImageFrameGeometry(None, None, (1.0, -1.0)),
		ImageFrameGeometry(None, None, (float('nan'), 1.0)),
		ImageFrameGeometry(None, None, (float('inf'), 1.0)),
		ImageFrameGeometry((float('nan'), 0.0, 0.0), None, (1.0, 1.0)),
		ImageFrameGeometry(None, (0.0, 0.0, 0.0, 0.0, 1.0, 0.0), (1.0, 1.0)),
		ImageFrameGeometry(None, (1.0, 0.0, 0.0, 1.0, 0.0, 0.0), (1.0, 1.0)),
		ImageFrameGeometry(None, None, None, 0.0),
	)

	def assert_refused(self, m):
		with self.assertRaises(SRDiagnosticError) as raised:
			model_of([m]).create_sr(record_instances=True)
		self.assertEqual(raised.exception.diagnostic.condition, SR_CONDITION_MEASUREMENT_NOT_COMPUTABLE)

	def test_unusable_calibration_refused_at_write(self):
		for calibration in self.UNUSABLE:
			self.assertFalse(calibration.is_usable)
			self.assertFalse(calibration.is_calibrated)
			self.assert_refused(length_image(calibration=calibration))
			self.assert_refused(CobbAngleMeasurement(((0.0, 0.0), (10.0, 0.0)), ((0.0, 0.0), (0.0, 10.0)), reference=synthetic_reference(), calibration=calibration, tracking_uid=TRACKING))

	def test_edited_parsed_measurement_with_unusable_calibration_refused(self):
		model = reparse(written([length_image()]))
		m = measurements_of(model)[0]
		m.p2 = (20.0, 20.0)
		m.calibration = ImageFrameGeometry(None, None, (0.0, 0.0))
		with self.assertRaises(SRDiagnosticError) as raised:
			model.create_sr()
		self.assertEqual(raised.exception.diagnostic.condition, SR_CONDITION_MEASUREMENT_NOT_COMPUTABLE)

	def test_degeneracy_is_checked_in_the_calculation_space(self):
		tiny = ImageFrameGeometry(None, None, (1e-9, 1e-9))
		m = length_image(calibration=tiny)
		self.assertTrue(m.is_degenerate)
		with self.assertRaises(SRDiagnosticError) as raised:
			model_of([m]).create_sr(record_instances=True)
		self.assertEqual(raised.exception.diagnostic.condition, SR_CONDITION_DEGENERATE_GEOMETRY)

	def test_overflowing_result_is_not_written(self):
		m = LengthMeasurement([(-1e308, 0.0, 0.0), (1e308, 1e308, 1e308)], frame_of_reference_uid=FOR_UID, tracking_uid=TRACKING)
		self.assertIsNone(m.computed_value())
		self.assert_refused(m)

	def test_non_finite_transform_refused(self):
		matrix = np.identity(4)
		matrix[0, 3] = float('nan')
		with self.assertRaisesRegex(ValueError, 'finite values'):
			ImageTransformMatrix(matrix, source_frame='a').convert([(1, 0, 0)])

	def test_controls_stay_valid_through_the_report(self):
		c = math.sqrt(0.5)
		oblique = ImageFrameGeometry((5.0, 6.0, 7.0), (c, c, 0.0, -c, c, 0.0), (2.0, 0.5))
		for m, expected in ((length_image(), math.hypot(2.0, 6.0)), (length_image(calibration=oblique), math.hypot(2.0, 6.0)),
				(angle_patient(), 90.0), (cobb_patient(), 135.0)):
			parsed = measurements_of(reparse(written([m])))[0]
			self.assertAlmostEqual(parsed.value, expected, places=3)


class SpatialEditTestCase(unittest.TestCase):
	OTHER_SOP = '2.25.1099'

	def serialise(self, model):
		return roundtrip(model.create_sr())

	def test_changed_frame_of_reference_is_written(self):
		model = reparse(written([length_patient()]))
		measurements_of(model)[0].frame_of_reference_uid = '2.25.7999'
		again = self.serialise(model)
		self.assertEqual(srdoc_children(num_of(group_of(again)))[0].ReferencedFrameOfReferenceUID, '2.25.7999')
		self.assertEqual(measurements_of(reparse(again))[0].frame_of_reference_uid, '2.25.7999')

	def test_cleared_frame_of_reference_refused(self):
		model = reparse(written([length_patient()]))
		measurements_of(model)[0].frame_of_reference_uid = None
		with self.assertRaises(ValueError):
			model.create_sr()

	def retargeted(self, frame_number=None):
		doc = written([length_image()])
		scoord = srdoc_children(num_of(group_of(doc)))[0]
		scoord.ContentSequence = Sequence(srdoc_children(scoord) + [text_item(CONSUMER_PROPERTY, 'kept', relationship='HAS PROPERTIES')])
		model = reparse(doc, dcm_evidence=[evidence_dataset(), evidence_dataset(SOPInstanceUID=self.OTHER_SOP)])
		return model, measurements_of(model)[0]

	def test_changed_image_recomputes_with_the_new_calibration(self):
		model, m = self.retargeted()
		m.reference = m.reference._replace(sop_instance_uid=self.OTHER_SOP, frame_number=3)
		m.calibration = ImageFrameGeometry(None, None, (1.0, 1.0))
		self.assertFalse(m.keeps_stored_value)
		self.assertAlmostEqual(m.value, 5.0)
		again = self.serialise(model)
		num = num_of(group_of(again))
		self.assertAlmostEqual(float(num.MeasuredValueSequence[0].NumericValue), 5.0)
		scoord = srdoc_children(num)[0]
		sop = next(c for c in srdoc_children(scoord) if c.ValueType == 'IMAGE').ReferencedSOPSequence[0]
		self.assertEqual((sop.ReferencedSOPInstanceUID, int(sop.ReferencedFrameNumber)), (self.OTHER_SOP, 3))
		self.assertEqual(named(srdoc_children(scoord), CONSUMER_PROPERTY.value)[0].TextValue, 'kept')
		parsed = measurements_of(reparse(again))[0]
		self.assertEqual((parsed.reference.sop_instance_uid, parsed.reference.frame_number), (self.OTHER_SOP, 3))

	def test_changed_image_without_a_calibration_refused(self):
		model, m = self.retargeted()
		m.reference = m.reference._replace(sop_instance_uid=self.OTHER_SOP)
		with self.assertRaises(SRDiagnosticError) as raised:
			model.create_sr()
		self.assertEqual(raised.exception.diagnostic.condition, SR_CONDITION_MEASUREMENT_NOT_COMPUTABLE)

	def test_unchanged_source_keeps_value_and_unknown_child_when_a_calibration_is_supplied(self):
		model, m = self.retargeted()
		stored = m.value
		m.calibration = ImageFrameGeometry(None, None, (1.0, 1.0))
		self.assertTrue(m.keeps_stored_value)
		self.assertEqual(m.value, stored)
		num = num_of(group_of(self.serialise(model)))
		self.assertAlmostEqual(float(num.MeasuredValueSequence[0].NumericValue), stored)
		scoord = srdoc_children(num)[0]
		self.assertEqual(named(srdoc_children(scoord), CONSUMER_PROPERTY.value)[0].TextValue, 'kept')

	def test_edited_points_keep_the_unknown_child(self):
		model, m = self.retargeted()
		m.p2 = (20.0, 20.0)
		m.calibration = CALIBRATION
		scoord = srdoc_children(num_of(group_of(self.serialise(model))))[0]
		self.assertEqual(list(scoord.GraphicData)[2:], [20.0, 20.0])
		self.assertEqual(named(srdoc_children(scoord), CONSUMER_PROPERTY.value)[0].TextValue, 'kept')

	def test_series_only_reference_change_keeps_the_value(self):
		model = reparse(written([length_image()]))
		m = measurements_of(model)[0]
		m.series_uid = '2.25.555'
		self.assertTrue(m.keeps_stored_value)

	def test_geometry_of_another_shape_refused(self):
		m = length_patient()
		with self.assertRaises(ValueError):
			m.geometry = Geometry('patient', 'POLYLINE', [(0, 0, 0), (1, 0, 0), (2, 0, 0)])
		with self.assertRaises(ValueError):
			m.geometry = None


class SecondaryValueEditTestCase(unittest.TestCase):
	KEY = ('G-A166', 'SRT')

	def document(self, stored=10):
		doc = written([length_image()])
		group = group_of(doc)
		second = copy.deepcopy(num_of(group))
		name = second.ConceptNameCodeSequence[0]
		name.CodeValue, name.CodingSchemeDesignator, name.CodeMeaning = 'G-A166', 'SRT', 'Area'
		measured = second.MeasuredValueSequence[0]
		measured.NumericValue = stored
		measured.FloatingPointValue = float(stored)
		second.ContentSequence = Sequence([text_item(CONSUMER_PROPERTY, 'kept')])
		group.ContentSequence = Sequence(srdoc_children(group) + [second])
		return doc

	def rewrite(self, doc, **changes):
		model = reparse(doc)
		m = measurements_of(model)[0]
		if changes:
			m.values[self.KEY] = m.values[self.KEY]._replace(**changes)
		again = roundtrip(model.create_sr())
		return [c for c in srdoc_children(group_of(again)) if c.get('ValueType') == 'NUM'][1], again

	def assert_child_kept(self, num):
		self.assertEqual(named(srdoc_children(num), CONSUMER_PROPERTY.value)[0].TextValue, 'kept')

	def test_value_and_unit_edited_together(self):
		cm = CodedConcept('cm', 'UCUM', 'centimeter')
		num, again = self.rewrite(self.document(), value=1.0, unit=cm)
		measured = num.MeasuredValueSequence[0]
		self.assertEqual((float(measured.NumericValue), measured.MeasurementUnitsCodeSequence[0].CodeValue), (1.0, 'cm'))
		self.assertEqual(float(measured.FloatingPointValue), 1.0)
		self.assert_child_kept(num)
		entry = measurements_of(reparse(again))[0].values[self.KEY]
		self.assertEqual((entry.value, entry.unit.value), (1.0, 'cm'))

	def test_unit_only_edit(self):
		num, _ = self.rewrite(self.document(), unit=codes.UCUM.Centimeter)
		measured = num.MeasuredValueSequence[0]
		self.assertEqual((float(measured.NumericValue), measured.MeasurementUnitsCodeSequence[0].CodeValue), (10.0, 'cm'))
		self.assert_child_kept(num)

	def test_value_edit_clears_rational_form(self):
		doc = self.document()
		measured = [c for c in srdoc_children(group_of(doc)) if c.get('ValueType') == 'NUM'][1].MeasuredValueSequence[0]
		measured.RationalNumeratorValue, measured.RationalDenominatorValue = 10, 1
		num, _ = self.rewrite(doc, value=2.5)
		self.assertNotIn('RationalNumeratorValue', num.MeasuredValueSequence[0])
		self.assertEqual(float(num.MeasuredValueSequence[0].FloatingPointValue), 2.5)

	def test_integer_value_edit_drops_the_floating_form(self):
		num, _ = self.rewrite(self.document(), value=3)
		self.assertNotIn('FloatingPointValue', num.MeasuredValueSequence[0])
		self.assertEqual(float(num.MeasuredValueSequence[0].NumericValue), 3.0)

	def test_concept_edit_honoured(self):
		num, _ = self.rewrite(self.document(), name=CodedConcept('131190003', 'SCT', 'Radius'))
		self.assertEqual(num.ConceptNameCodeSequence[0].CodeValue, '131190003')
		self.assert_child_kept(num)

	def test_non_finite_value_refused(self):
		with self.assertRaises(ValueError):
			self.rewrite(self.document(), value=float('inf'))

	def test_untouched_value_written_as_read(self):
		num, _ = self.rewrite(self.document())
		measured = num.MeasuredValueSequence[0]
		self.assertEqual((float(measured.NumericValue), float(measured.FloatingPointValue)), (10.0, 10.0))
		self.assertEqual(num.ConceptNameCodeSequence[0].CodeMeaning, 'Area')
		self.assert_child_kept(num)


class ViewerWrittenLengthTestCase(unittest.TestCase):
	'''	The document of an independent writer: what the Viewer's classic writer produces
	'''
	FIXTURE = os.path.join(FIXTURES, 'sonador-viewer.length-image.2026-1108.dcm')
	SQUARE = ImageFrameGeometry(None, None, (1.0, 1.0))

	def source(self):
		return read(self.FIXTURE)

	def parsed(self, **kwargs):
		model = reparse(self.source(), **kwargs)
		return model, measurements_of(model)[0]

	def test_writer_is_recorded(self):
		doc = self.source()
		self.assertEqual((doc.Manufacturer, doc.ManufacturerModelName), ('Sonador', 'Sonador-Viewer'))
		self.assertEqual(doc.SoftwareVersions, '0.5.0-a2+0fae4bdc')
		self.assertEqual((doc.PatientID, str(doc.StudyInstanceUID)), ('ANONYMOUS', SYNTHETIC['StudyInstanceUID']))

	def test_reader(self):
		model, m = self.parsed()
		self.assertIsInstance(m, LengthMeasurement)
		self.assertEqual(m.points, ((10.0, 10.0), (14.0, 13.0)))
		self.assertEqual(m.geometry.domain, 'image')
		self.assertEqual(m.value, 5.0)
		self.assertEqual((m.unit.value, m.unit.scheme_designator), ('mm', 'UCUM'))
		self.assertEqual((m.name.value, m.name.scheme_designator), ('410668003', 'SCT'))
		self.assertEqual((m.reference.sop_instance_uid, m.reference.frame_number), (SYNTHETIC['SOPInstanceUID'], None))
		self.assertEqual(m.series_uid, SYNTHETIC['SeriesInstanceUID'])
		self.assertEqual(m.tracking_id, LENGTH_ID)
		self.assertTrue(m.tracking_uid)
		self.assertEqual((m.description, m.text, m.location), ('Viewer length', 'Viewer text', 'Viewer location'))
		self.assertEqual(tuple(m.display.color['Background']), (16, 32, 48))
		self.assertEqual(tuple(m.display.color['Border']), (200, 100, 50))
		self.assertIn(SR_CONDITION_GROUP_LEVEL_PROPERTIES, conditions(sr_diagnostics_with(model.diagnostics, severity=SR_DIAGNOSTIC_WARNING)))
		self.assertIn(SR_CONDITION_MEASUREMENT_VALUE_UNVERIFIED, conditions(model.diagnostics))

	def test_stored_value_agrees_with_the_points(self):
		model, _ = self.parsed(calibrations={SYNTHETIC['SOPInstanceUID']: self.SQUARE})
		self.assertNotIn(SR_CONDITION_MEASUREMENT_VALUE_MISMATCH, conditions(model.diagnostics))

	def test_unchanged_resave(self):
		doc = self.source()
		_, m = self.parsed()
		again = roundtrip(reparse(doc).create_sr())
		self.assertEqual(str(again.SOPClassUID), DCMSR_SOP_CLASS_COMPREHENSIVE_3D)
		self.assertNotEqual(again.SOPInstanceUID, doc.SOPInstanceUID)
		group = group_of(again)
		self.assertEqual([c.ValueType for c in srdoc_children(group)], ['TEXT', 'UIDREF', 'NUM'])
		self.assertEqual(srdoc_children(group)[1].UID, m.tracking_uid)
		num = num_of(group)
		self.assertEqual((num.ConceptNameCodeSequence[0].CodeValue, num.ConceptNameCodeSequence[0].CodingSchemeDesignator), ('410668003', 'SCT'))
		measured = num.MeasuredValueSequence[0]
		self.assertEqual(measured.NumericValue.original_string, num_of(group_of(doc)).MeasuredValueSequence[0].NumericValue.original_string)
		self.assertEqual(measured.MeasurementUnitsCodeSequence[0].CodingSchemeVersion, '1.4')
		scoord = srdoc_children(num)[0]
		self.assertEqual((scoord.ValueType, scoord.GraphicType, list(scoord.GraphicData)), ('SCOORD', 'POLYLINE', [10.0, 10.0, 14.0, 13.0]))
		self.assertNotIn('ConceptNameCodeSequence', scoord)
		image = srdoc_children(scoord)[0]
		self.assertEqual((image.RelationshipType, image.ReferencedSOPSequence[0].ReferencedSOPInstanceUID), ('SELECTED FROM', SYNTHETIC['SOPInstanceUID']))
		self.assertNotIn('ConceptNameCodeSequence', image)
		self.assertEqual(named(srdoc_children(num), DCMSR_MEASUREMENT_DESCRIPTION.value)[0].TextValue, 'Viewer length')
		self.assertTrue(named(srdoc_children(num), DCMSR_DISPLAY_ATTRIBUTES.value))

	def test_parse_edit_write_reparse(self):
		model, m = self.parsed()
		tracking = m.tracking_uid
		m.p2 = (18.0, 16.0)
		m.calibration = self.SQUARE
		m.description = 'Edited in Python'
		again = roundtrip(model.create_sr())
		edited = measurements_of(reparse(again))[0]
		self.assertAlmostEqual(edited.value, 10.0)
		self.assertEqual(edited.unit.value, 'mm')
		self.assertEqual(edited.points, ((10.0, 10.0), (18.0, 16.0)))
		self.assertEqual((edited.tracking_uid, edited.description, edited.location), (tracking, 'Edited in Python', 'Viewer location'))
		self.assertEqual(edited.reference.sop_instance_uid, SYNTHETIC['SOPInstanceUID'])

	def test_unknown_children_survive_the_rewrite(self):
		doc = self.source()
		num = num_of(group_of(doc))
		num.ContentSequence = Sequence(srdoc_children(num) + [text_item(CONSUMER_PROPERTY, 'kept')])
		group = group_of(doc)
		group.ContentSequence = Sequence(srdoc_children(group) + [text_item(CONSUMER_VALUE, 'kept in the group', relationship='CONTAINS')])
		again = group_of(roundtrip(reparse(doc).create_sr()))
		self.assertEqual(named(srdoc_children(num_of(again)), CONSUMER_PROPERTY.value)[0].TextValue, 'kept')
		self.assertEqual(named(srdoc_children(again), CONSUMER_VALUE.value)[0].TextValue, 'kept in the group')


class MeasurementRetrievalPacs(MockPacs):
	'''	The mocked study with a further SR series C whose one document holds a patient-domain and an
		image-domain length
	'''
	def __init__(self, iserver):
		super().__init__(iserver)
		doc = new_model([], measurements=[length_patient(), length_image(tracking_uid='2.25.9301')]).create_sr(
			record_instances=True, dcmsr_series_uid='2.25.700', dcmsr_instance_uid='2.25.7001', previous=None)
		doc.ContentDate, doc.ContentTime = '20260101', '100000'
		doc = roundtrip(doc)
		self.add('Series', 'sx-src', {'SeriesInstanceUID': '2.25.700', 'Modality': 'SR', 'SeriesNumber': '46'}, ParentStudy='study-1', Instances=['sr-c1'])
		self.add('Instance', 'sr-c1', {'SOPInstanceUID': str(doc.SOPInstanceUID), 'SOPClassUID': str(doc.SOPClassUID),
			'ContentDate': doc.ContentDate, 'ContentTime': doc.ContentTime, 'InstanceNumber': '1'}, ParentSeries='sx-src', IndexInSeries=1)
		self.files['sr-c1'] = tobytes(doc)
		self.resources['study-1']['Series'].append('sx-src')
		self.resources['sx-ct']['MainDicomTags']['FrameOfReferenceUID'] = FOR_UID


class MeasurementRetrievalTestCase(unittest.TestCase):
	def setUp(self):
		self.server = SonadorServer('https://sonador.invalid', apitoken='token', verify=False)
		self.iserver = self.server.imageserver_modelinstance_from_json(dict(IMAGESERVER_JSON))
		self.pacs = MeasurementRetrievalPacs(self.iserver)
		studies = self.server._init_dataclass_from_json(ImagingStudyCollection, [dict(self.pacs.resources['study-1'])], pacs=self.iserver)
		studies.bulkpopulate_related(child_instances=True)
		self.study = studies[0]

	def test_budget_and_keys(self):
		self.assertEqual(self.pacs.count('GET'), 0)
		by_series = self.study.measurements()
		self.assertEqual(sorted(self.pacs.file_requests()), ['sr-a2', 'sr-b1', 'sr-c1'])
		self.assertEqual(self.pacs.count('POST'), 1)
		self.assertEqual(self.pacs.count('GET'), 3)
		before = len(self.pacs.requests)

		# The image-domain length is keyed by its image's series, the patient-domain one by the document's series
		self.assertEqual(set(by_series.keys()), set([CT_SERIES_UID, 'sx-src']))
		image = by_series[CT_SERIES_UID]['length'][0]
		patient = by_series['sx-src']['length'][0]
		self.assertEqual((image.tracking_uid, patient.tracking_uid), ('2.25.9301', TRACKING))

		# Links, from the cache
		self.assertIsInstance(image.series, ImagingSeries)
		self.assertEqual(image.series.pk, 'sx-ct')
		self.assertEqual(image.instance.pk, 'dcm-ct-1')
		self.assertIsInstance(image.sr_instance, DcmSRInstance)
		self.assertEqual(patient.series.pk, 'sx-ct')
		self.assertIsNone(patient.instance)

		# Shapes
		self.assertEqual(len(self.study.measurements(CT_SERIES_UID, 'length')), 1)
		self.assertEqual(sorted(self.study.measurements(measurement_type='length').keys()), sorted([CT_SERIES_UID, 'sx-src']))
		self.assertEqual(len(self.study.measurements('2.25.none', 'length')), 0)
		series = next(sx for sx in self.study.series_collection if sx.pk == 'sx-ct')
		self.assertEqual(len(series.measurements('length')), 1)
		self.assertEqual(self.study.findings(series.series_uid, 'series-tag')[0].type, 'series-tag')
		self.assertEqual(len(self.pacs.requests), before)

	def test_frame_of_reference_accessor_costs_no_request(self):
		self.study.measurements()
		before = len(self.pacs.requests)
		found = self.study.measurements(frame_of_reference_uid=FOR_UID)
		self.assertEqual(sorted(m.tracking_uid for by_type in found.values() for c in by_type.values() for m in c), sorted([TRACKING, '2.25.9301']))
		self.assertEqual(self.study.measurements(frame_of_reference_uid='2.25.other'), OrderedDict())

		series = next(sx for sx in self.study.series_collection if sx.pk == 'sx-ct')
		self.assertEqual(series.frame_of_reference_uid, FOR_UID)
		by_type = series.measurements(frame_of_reference_uid=FOR_UID)
		self.assertEqual(sorted(m.tracking_uid for m in by_type['length']), sorted([TRACKING, '2.25.9301']))
		self.assertEqual(len(series.measurements('length', frame_of_reference_uid=FOR_UID)), 2)
		self.assertEqual(len(series.measurements('angle', frame_of_reference_uid=FOR_UID)), 0)
		self.assertEqual(len(self.pacs.requests), before)

	def test_budget_shared_with_findings(self):
		self.study.findings()
		before = len(self.pacs.requests)
		self.study.measurements()
		self.assertEqual(len(self.pacs.requests), before)

	def test_invalidation_reparses_without_a_request(self):
		first = self.study.measurements(measurement_type='length')[CT_SERIES_UID][0]
		before = len(self.pacs.requests)
		self.study.invalidate_findings()
		second = self.study.measurements(measurement_type='length')[CT_SERIES_UID][0]
		self.assertIsNot(first, second)
		self.assertEqual(len(self.pacs.requests), before)

	def test_create_sr_makes_no_file_request(self):
		series = next(sx for sx in self.study.series_collection if sx.pk == 'sx-ct')
		before = self.pacs.count('GET')
		m = LengthMeasurement([(0, 0, 0), (1, 1, 1)], frame_of_reference_uid=FOR_UID, tracking_uid='2.25.9500')
		doc = SonadorImagingMeasurementReport(sonador_ref_series=series, measurements=[m], report_procedure=PROCEDURE, record_instances=False).create_sr()
		self.assertEqual(self.pacs.count('GET'), before)
		self.assertAlmostEqual(float(num_of(srdoc_measurement_groups(doc)[0]).MeasuredValueSequence[0].NumericValue), math.sqrt(3), places=5)


FIXTURE_DATE = '2026-1108'


def fixture_path(topic):
	return os.path.join(FIXTURES, 'sonador-io.%s.%s.dcm' % (topic, FIXTURE_DATE))


def fixture_measurement(topic):
	'''	The measurement of a Python-written fixture
	'''
	properties = dict(description='%s measurement' % topic, text='Synthetic', location='Synthetic location', views=['coronal'],
		display=DisplayAttributes.from_color(COLORS), tracking_uid='2.25.8100')
	return {'length': length_patient, 'length-image': length_image, 'angle': angle_patient, 'cobb-angle': cobb_patient}[topic](**properties)


def write_measurement_fixture(topic):
	doc = model_of([fixture_measurement(topic)]).create_sr(record_instances=True, dcmsr_series_uid='2.25.8000', dcmsr_instance_uid='2.25.8001')
	doc.save_as(fixture_path(topic))


class MeasurementFixtureTestCase(unittest.TestCase):
	EXPECTED = (('length', LengthMeasurement, 'SCOORD3D', 5.0, 'mm'), ('length-image', LengthMeasurement, 'SCOORD', math.hypot(2.0, 6.0), 'mm'),
		('angle', AngleMeasurement, 'SCOORD3D', 90.0, 'deg'), ('cobb-angle', CobbAngleMeasurement, 'SCOORD3D', 135.0, 'deg'))

	def test_committed_fixtures(self):
		for topic, cls, value_type, value, unit in self.EXPECTED:
			if os.environ.get('UPDATE_SR_FIXTURES'):
				write_measurement_fixture(topic)
			doc = read(fixture_path(topic))
			self.assertEqual((doc.PatientID, str(doc.StudyInstanceUID)), ('ANONYMOUS', '2.25.1001'))
			model = reparse(doc, strict=False)
			m = measurements_of(model)[0]
			self.assertIsInstance(m, cls)
			self.assertAlmostEqual(m.value, value, places=3)
			self.assertEqual(m.unit.value, unit)
			self.assertEqual(m.description, '%s measurement' % topic)
			num = num_of(group_of(doc))
			self.assertEqual(srdoc_children(num)[0].ValueType, value_type)
			again = roundtrip(reparse(doc).create_sr())
			self.assertEqual(str(again.SOPClassUID), DCMSR_SOP_CLASS_COMPREHENSIVE_3D)
			self.assertEqual(measurements_of(reparse(again))[0].tracking_uid, '2.25.8100')
