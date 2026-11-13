'''	Offline tests of the quantitative DICOM-SR measurements (Length, Angle, Cobb angle) at Sonador-SR
	0.3: the geometry and frame conversion, the value rules, the readers and writers per class and
	domain, property layouts and preservation, mixed documents, and the retrieval budget against a
	mocked imaging server.

	Also the patient-domain provenance (`Sonador-Measurement.Source-Series`, `.Drawing-Plane`), the
	View value registry, the SCT concept codes and the document class written.

	Fixtures (`ftests/test_sr/fixtures`, synthetic study, regenerated with `UPDATE_SR_FIXTURES=1`):
	`sonador-io.length.2026-1111.dcm`, `sonador-io.angle.2026-1111.dcm` and
	`sonador-io.cobb-angle.2026-1111.dcm` (patient domain, drawn in the plane x = 0 with a sagittal
	Drawing-Plane, Source-Series and View), `sonador-io.length-image.2026-1111.dcm` (image domain),
	`sonador-io.length-oblique.2026-1111.dcm` (patient domain, oblique Drawing-Plane and View) and
	`sonador-io.two-series.2026-1111.dcm` (patient-domain measurements from two series sharing one
	Frame of Reference). `sonador-viewer.out-of-plane.2026-1110.dcm` is the Viewer's out-of-plane report
	(oak-tree/medical-imaging/ohif-viewers!122): Length, Angle and Cobb angle drawn in a sagittal view and a
	Length on an oblique plane, with Source-Series, View and Drawing-Plane, on the Viewer's synthetic CT.
	`sonador-viewer.length-image.2026-1110.dcm`, `sonador-viewer.angle-image.2026-1108.dcm` and
	`sonador-viewer.cobb-angle-image.2026-1108.dcm` are written by the other side: an image-domain Length, Angle
	and Cobb angle produced by the Sonador Viewer's classic SR writer from a synthetic tool state on a 1 mm image,
	copied unchanged from oak-tree/medical-imaging/ohif-viewers `platform/core/src/DICOMSR/__fixtures__`. They
	were not drawn in a browser. The Length's tool state carries no cached length, so its NUM is written without
	a Numeric Value: it reads with the `measurement-without-value` warning and is written back with the length
	computed from its points.

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
from ...apisettings.sr.measurements import DCMSR_MEASUREMENT_VIEW, image_frame_geometry, SONADOR_MEASUREMENT_VIEWS, \
	DCMSR_MEASUREMENT_VIEW_CODES, DCMSR_MEASUREMENT_SOURCE_SERIES, DCMSR_MEASUREMENT_DRAWING_PLANE, drawing_plane_to_text, \
	drawing_plane_from_text, drawing_plane_failed_check
from ...apisettings.sr import SR_CONDITION_UNREGISTERED_VIEW, SR_CONDITION_INVALID_DRAWING_PLANE, SR_CONDITION_PROVENANCE_ON_IMAGE_DOMAIN, \
	SR_CONDITION_UNKNOWN_SOURCE_SERIES
from ...apisettings.base import SONADOR_SR, SONADOR_SCHEME_VERSION_03
from ...imaging.helpers.sr import SonadorComprehensiveSR3D, SonadorImagingMeasurementReport
from ...imaging.helpers.sr.parse import srdoc_measurement_groups, srdoc_children
from ...imaging.orthanc import ImagingStudyCollection, ImagingSeries
from ...imaging.orthanc.sr import DcmSRInstance
from ...test.fakes import IMAGESERVER_JSON
from .sr_seriestag import read, roundtrip, tobytes, new_model, synthetic_reference, named, TAG_REJECT, SYNTHETIC, PROCEDURE, evidence_dataset, FIXTURES
from .sr_retrieval import MockPacs, CT_SERIES_UID


__all__ = ['ViewerWrittenLengthTestCase', 'ViewerWrittenAngleTestCase', 'InvalidGeometryTestCase', 'SpatialEditTestCase', 'SecondaryValueEditTestCase', 'RegistryAndModelTestCase', 'FrameConversionTestCase', 'CalibrationTestCase', 'LengthRoundTripTestCase', 'AngleTestCase',
	'PropertyLayoutTestCase', 'ValueRuleTestCase', 'DocumentTestCase', 'MeasurementRetrievalTestCase', 'MeasurementFixtureTestCase',
	'ViewRegistryTestCase', 'DrawingPlaneTestCase', 'SourceSeriesTestCase', 'SourceSeriesRetrievalTestCase', 'DocumentClassTestCase',
	'SourceSeriesEvidenceTestCase', 'SourceSeriesCachedEvidenceTestCase', 'ViewerOutOfPlaneTestCase']


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
		self.assertEqual((num.RelationshipType, num.ConceptNameCodeSequence[0].CodeValue, num.ConceptNameCodeSequence[0].CodingSchemeDesignator), ('CONTAINS', '410668003', 'SCT'))
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

	def test_srt_length_concept_read_and_kept_on_resave(self):
		doc = written([length_patient()])
		set_concept(doc, 'G-D7FE', 'SRT', 'Length')
		model = reparse(doc)
		self.assertIsInstance(measurements_of(model)[0], LengthMeasurement)
		num = num_of(group_of(roundtrip(model.create_sr())))
		self.assertEqual((num.ConceptNameCodeSequence[0].CodeValue, num.ConceptNameCodeSequence[0].CodingSchemeDesignator), ('G-D7FE', 'SRT'))

	def test_new_length_written_with_the_sct_concept(self):
		for m in (length_patient(), length_image()):
			name = num_of(group_of(written([m]))).ConceptNameCodeSequence[0]
			self.assertEqual((name.CodeValue, name.CodingSchemeDesignator, name.CodeMeaning), ('410668003', 'SCT', 'Length'))

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

	def test_flat_layout_read_and_rewritten_nested(self):
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

		# Enhanced SR is read and written back as Comprehensive 3D SR, nested, with an `info`
		again = roundtrip(model.create_sr())
		self.assertEqual(str(again.SOPClassUID), DCMSR_SOP_CLASS_COMPREHENSIVE_3D)
		self.assertEqual([d.severity for d in sr_diagnostics_with(model.diagnostics, condition=SR_CONDITION_DOCUMENT_CLASS_UPGRADED)], ['info'])
		children = srdoc_children(num_of(group_of(again)))
		self.assertEqual(len(named(children, DCMSR_DISPLAY_ATTRIBUTES.value)), 1)
		self.assertFalse([c for c in children if 'ConceptNameCodeSequence' in c and dcm_code_value(c.ConceptNameCodeSequence[0]).startswith('Sonador-DisplayAttributes.Color.')])
		self.assert_properties(measurements_of(reparse(again))[0])

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
		self.assertEqual([dcm_code_value(c.ConceptNameCodeSequence[0]) for c in srdoc_children(again) if c.get('ValueType') == 'NUM'], ['410668003', 'x2'])

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


VIEWER_COLORS = {'Border': (255, 136, 0), 'Value': (138, 208, 168), 'Meaning': (192, 199, 248)}
VIEWER_SQUARE = {SYNTHETIC['SOPInstanceUID']: ImageFrameGeometry(None, None, (1.0, 1.0))}


def assert_same_content(testcase, source, resaved):
	'''	The content-tree oracle of the circle suite, imported when called (that suite imports this one)
	'''
	from .sr_circle import OracleMixin
	OracleMixin.assert_same_content(testcase, source, resaved)


class ViewerWrittenPropertiesMixin:
	'''	The properties the Viewer's fixture generator gives every tool, written under the NUM item
	'''
	def assert_viewer_properties(self, m, group):
		self.assertEqual((m.description, m.text, m.location, m.views), ('Hip abduction', 'Synthetic', 'Synthetic location', ()))
		self.assertEqual({role: tuple(color) for role, color in m.display.color.items()}, VIEWER_COLORS)
		self.assertEqual([c.ValueType for c in srdoc_children(group)], ['TEXT', 'UIDREF', 'NUM'])
		num = num_of(group)
		for concept, value in ((DCMSR_MEASUREMENT_DESCRIPTION, 'Hip abduction'), (DCMSR_MEASUREMENT_TEXT, 'Synthetic'),
				(DCMSR_MEASUREMENT_LOCATION, 'Synthetic location')):
			self.assertEqual([(c.RelationshipType, c.TextValue) for c in named(srdoc_children(num), concept.value)], [('HAS PROPERTIES', value)])
		self.assertEqual([c.RelationshipType for c in named(srdoc_children(num), DCMSR_DISPLAY_ATTRIBUTES.value)], ['HAS PROPERTIES'])


class ViewerWrittenLengthTestCase(ViewerWrittenPropertiesMixin, unittest.TestCase):
	'''	The document of an independent writer: an image-domain Length from the Viewer's classic writer, its NUM
		written without a Numeric Value
	'''
	FIXTURE = os.path.join(FIXTURES, 'sonador-viewer.length-image.2026-1110.dcm')
	SQUARE = ImageFrameGeometry(None, None, (1.0, 1.0))
	LENGTH = math.sqrt(500.0)

	def source(self):
		return read(self.FIXTURE)

	def parsed(self, **kwargs):
		model = reparse(self.source(), calibrations=VIEWER_SQUARE, **kwargs)
		return model, measurements_of(model)[0]

	def test_writer_is_recorded(self):
		doc = self.source()
		self.assertEqual((doc.Manufacturer, doc.ManufacturerModelName, doc.SoftwareVersions), ('Sonador', 'Sonador-Viewer', 'dev'))
		self.assertEqual((doc.PatientID, str(doc.StudyInstanceUID)), ('ANONYMOUS', SYNTHETIC['StudyInstanceUID']))
		self.assertEqual(str(doc.SOPClassUID), DCMSR_SOP_CLASS_COMPREHENSIVE_3D)

	def test_reader(self):
		model, m = self.parsed()
		self.assertIsInstance(m, LengthMeasurement)
		self.assertEqual(m.points, ((0.5, 0.5), (10.5, 20.5)))
		self.assertEqual(m.geometry.domain, 'image')
		self.assertAlmostEqual(m.value, self.LENGTH, places=9)
		self.assertEqual((m.unit.value, m.unit.scheme_designator), ('mm', 'UCUM'))
		self.assertEqual((m.name.value, m.name.scheme_designator), ('410668003', 'SCT'))
		self.assertEqual((m.reference.sop_instance_uid, m.reference.frame_number), (SYNTHETIC['SOPInstanceUID'], None))
		self.assertEqual(m.series_uid, SYNTHETIC['SeriesInstanceUID'])
		self.assertEqual((m.tracking_id, m.tracking_uid), (LENGTH_ID, '2.25.900000'))
		self.assert_viewer_properties(m, group_of(self.source()))
		# The NUM has units but no Numeric Value
		measured = num_of(group_of(self.source())).MeasuredValueSequence[0]
		self.assertNotIn('NumericValue', measured)
		self.assertEqual(measured.MeasurementUnitsCodeSequence[0].CodeValue, 'mm')
		warnings = sr_diagnostics_with(model.diagnostics, severity=SR_DIAGNOSTIC_WARNING)
		self.assertEqual(conditions(warnings), [SR_CONDITION_MEASUREMENT_WITHOUT_VALUE])
		self.assertNotIn(SR_CONDITION_MEASUREMENT_VALUE_MISMATCH, conditions(model.diagnostics))

	def test_strict_read_refuses_the_missing_value(self):
		with self.assertRaises(SRDiagnosticError) as raised:
			reparse(self.source(), strict=True, calibrations=VIEWER_SQUARE)
		self.assertEqual(raised.exception.diagnostic.condition, SR_CONDITION_MEASUREMENT_WITHOUT_VALUE)

	def test_resave_writes_the_computed_value(self):
		model, m = self.parsed()
		again = roundtrip(model.create_sr())
		self.assertEqual(str(again.SOPClassUID), DCMSR_SOP_CLASS_COMPREHENSIVE_3D)
		self.assertNotEqual(again.SOPInstanceUID, self.source().SOPInstanceUID)
		group = group_of(again)
		self.assertEqual(srdoc_children(group)[1].UID, m.tracking_uid)
		num = num_of(group)
		self.assertEqual((num.ConceptNameCodeSequence[0].CodeValue, num.ConceptNameCodeSequence[0].CodingSchemeDesignator), ('410668003', 'SCT'))
		measured = num.MeasuredValueSequence[0]
		self.assertEqual(str(measured.NumericValue), '22.3606797749979')
		self.assertEqual((measured.MeasurementUnitsCodeSequence[0].CodeValue, measured.MeasurementUnitsCodeSequence[0].CodingSchemeDesignator), ('mm', 'UCUM'))
		scoord = srdoc_children(num)[0]
		self.assertEqual((scoord.ValueType, scoord.GraphicType, list(scoord.GraphicData)), ('SCOORD', 'POLYLINE', [0.5, 0.5, 10.5, 20.5]))
		image = srdoc_children(scoord)[0]
		self.assertEqual((image.RelationshipType, image.ReferencedSOPSequence[0].ReferencedSOPInstanceUID), ('SELECTED FROM', SYNTHETIC['SOPInstanceUID']))
		self.assert_viewer_properties(m, group)
		# Read back strictly: the value is now stored, and agrees with the points
		reread = reparse(again, strict=True, calibrations=VIEWER_SQUARE)
		self.assertAlmostEqual(measurements_of(reread)[0].value, self.LENGTH, places=9)
		self.assertFalse(sr_diagnostics_with(reread.diagnostics, severity=SR_DIAGNOSTIC_WARNING))

	def test_parse_edit_write_reparse(self):
		model, m = self.parsed()
		tracking = m.tracking_uid
		m.p2 = (6.5, 8.5)
		m.description = 'Edited in Python'
		again = roundtrip(model.create_sr())
		edited = measurements_of(reparse(again))[0]
		self.assertAlmostEqual(edited.value, 10.0)
		self.assertEqual(edited.unit.value, 'mm')
		self.assertEqual(edited.points, ((0.5, 0.5), (6.5, 8.5)))
		self.assertEqual((edited.tracking_uid, edited.description, edited.location), (tracking, 'Edited in Python', 'Synthetic location'))
		self.assertEqual(edited.reference.sop_instance_uid, SYNTHETIC['SOPInstanceUID'])

	def test_unknown_children_survive_the_rewrite(self):
		doc = self.source()
		num = num_of(group_of(doc))
		num.ContentSequence = Sequence(srdoc_children(num) + [text_item(CONSUMER_PROPERTY, 'kept')])
		group = group_of(doc)
		group.ContentSequence = Sequence(srdoc_children(group) + [text_item(CONSUMER_VALUE, 'kept in the group', relationship='CONTAINS')])
		again = group_of(roundtrip(reparse(doc, calibrations=VIEWER_SQUARE).create_sr()))
		self.assertEqual(named(srdoc_children(num_of(again)), CONSUMER_PROPERTY.value)[0].TextValue, 'kept')
		self.assertEqual(named(srdoc_children(again), CONSUMER_VALUE.value)[0].TextValue, 'kept in the group')


class ViewerWrittenAngleTestCase(ViewerWrittenPropertiesMixin, unittest.TestCase):
	'''	An image-domain Angle and Cobb angle from the Viewer's classic writer: the tool resolved by the tracking
		identifier, the stored value kept verbatim
	'''
	# fixture -> (class, measurement type, tracking identifier, points as read, stored value text)
	FIXTURES = OrderedDict((
		('sonador-viewer.angle-image.2026-1108.dcm', (AngleMeasurement, SONADOR_MEASUREMENT_TYPE_ANGLE, ANGLE_ID,
			((20.5, 10.5), (10.5, 10.5), (10.5, 10.5), (10.5, 20.5)), '90')),
		('sonador-viewer.cobb-angle-image.2026-1108.dcm', (CobbAngleMeasurement, SONADOR_MEASUREMENT_TYPE_COBB_ANGLE, COBB_ID,
			((0.5, 0.5), (10.5, 0.5), (0.5, 10.5), (10.5, 20.5)), '135')),
	))

	def read_viewer(self, name):
		doc = read(os.path.join(FIXTURES, name))
		self.assertEqual((doc.Manufacturer, doc.ManufacturerModelName, doc.PatientID), ('Sonador', 'Sonador-Viewer', 'ANONYMOUS'))
		model = reparse(doc, strict=True, calibrations=VIEWER_SQUARE)
		self.assertFalse(sr_diagnostics_with(model.diagnostics, severity=SR_DIAGNOSTIC_WARNING), name)
		return doc, model, measurements_of(model)[0]

	def test_hydration(self):
		for name, (cls, measurement_type, tracking_id, points, stored) in self.FIXTURES.items():
			doc, model, m = self.read_viewer(name)
			self.assertIsInstance(m, cls, name)
			self.assertEqual((m.tracking_id, measurement_type_for_tracking_id(m.tracking_id)), (tracking_id, measurement_type), name)
			self.assertEqual((m.geometry.domain, m.points), ('image', points), name)
			self.assertEqual((m.reference.sop_instance_uid, m.series_uid), (SYNTHETIC['SOPInstanceUID'], SYNTHETIC['SeriesInstanceUID']), name)
			num = num_of(group_of(doc))
			self.assertEqual(num.MeasuredValueSequence[0].NumericValue.original_string, stored, name)
			self.assertEqual((m.value, m.unit.value), (float(stored), 'deg'), name)
			computed, unit = m.computed_value()
			self.assertAlmostEqual(computed, float(stored), places=6, msg=name)
			self.assertNotIn(SR_CONDITION_MEASUREMENT_VALUE_MISMATCH, conditions(model.diagnostics), name)
			self.assert_viewer_properties(m, group_of(doc))

	def test_unchanged_resave(self):
		for name, (cls, measurement_type, tracking_id, points, stored) in self.FIXTURES.items():
			doc, model, m = self.read_viewer(name)
			again = roundtrip(model.create_sr())
			self.assertEqual(num_of(group_of(again)).MeasuredValueSequence[0].NumericValue.original_string, stored, name)
			self.assertEqual(srdoc_children(group_of(again))[1].UID, m.tracking_uid, name)
			self.assert_viewer_properties(measurements_of(reparse(again, strict=True, calibrations=VIEWER_SQUARE))[0], group_of(again))
			assert_same_content(self, doc, again)


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


# Patient-domain provenance

SAGITTAL = ((0.0, 1.0, 0.0), (0.0, 0.0, -1.0))
SAGITTAL_TEXT = '0,1,0,0,0,-1'
SECOND_SERIES_UID, SECOND_SOP_UID = '2.25.1102', '2.25.1103'

# An oblique view: normal (1, 1, 1) / sqrt(3), more than 15 degrees from every patient axis
OBLIQUE_WIDTH = tuple(np.array((1.0, -1.0, 0.0)) / math.sqrt(2.0))
OBLIQUE_HEIGHT = tuple(np.array((1.0, 1.0, -2.0)) / math.sqrt(6.0))
OBLIQUE_TEXT = '0.707107,-0.707107,0,0.408248,0.408248,-0.816497'
OBLIQUE_ORIGIN = (10.0, 20.0, 30.0)


def sagittal_length(**kwargs):
	'''	Patient-domain Length of 5 mm in the plane x = 0
	'''
	kwargs.setdefault('frame_of_reference_uid', FOR_UID)
	kwargs.setdefault('tracking_uid', TRACKING)
	return LengthMeasurement([(0.0, 0.0, 0.0), (0.0, 3.0, -4.0)], **kwargs)


def sagittal_angle(**kwargs):
	'''	Patient-domain Angle of 90 degrees in the plane x = 0
	'''
	kwargs.setdefault('frame_of_reference_uid', FOR_UID)
	kwargs.setdefault('tracking_uid', TRACKING)
	return AngleMeasurement((0.0, 10.0, 0.0), (0.0, 0.0, 0.0), (0.0, 0.0, -10.0), **kwargs)


def sagittal_cobb(**kwargs):
	'''	Patient-domain Cobb angle of 135 degrees in the plane x = 0
	'''
	kwargs.setdefault('frame_of_reference_uid', FOR_UID)
	kwargs.setdefault('tracking_uid', TRACKING)
	return CobbAngleMeasurement(((0.0, 0.0, 0.0), (0.0, 10.0, 0.0)), ((0.0, 0.0, -10.0), (0.0, 10.0, -20.0)), **kwargs)


def oblique_length(**kwargs):
	'''	Patient-domain Length of 20 mm along the width direction of the oblique view
	'''
	kwargs.setdefault('frame_of_reference_uid', FOR_UID)
	kwargs.setdefault('tracking_uid', TRACKING)
	end = tuple(np.array(OBLIQUE_ORIGIN) + 20.0 * np.array(OBLIQUE_WIDTH))
	return LengthMeasurement([OBLIQUE_ORIGIN, end], **kwargs)


def two_series_evidence():
	return [evidence_dataset(), evidence_dataset(SeriesInstanceUID=SECOND_SERIES_UID, SOPInstanceUID=SECOND_SOP_UID)]


def provenance_written(measurements, **kwargs):
	'''	Serialised document of a new report whose evidence records both synthetic series
	'''
	kwargs.setdefault('dcm_evidence', two_series_evidence())
	return roundtrip(new_model([], measurements=list(measurements), **kwargs).create_sr(record_instances=True))


def children_named(item, concept):
	return named(srdoc_children(item), concept.value)


def set_drawing_plane_text(doc, text, index=0):
	num = num_of(group_of(doc, index))
	item = children_named(num, DCMSR_MEASUREMENT_DRAWING_PLANE)[0]
	item.TextValue = text


class ViewRegistryTestCase(unittest.TestCase):
	TABLE = (
		('axial', 'Axial view. Cross-sections, dividing head from feet.'),
		('sagittal', 'Sagittal view. Side-on slices, dividing left from right.'),
		('coronal', 'Coronal view. Front-on slices, dividing front from back.'),
		('oblique', 'Oblique view. A slice tilted away from the standard views.'),
		('3d', '3D view. The whole volume, seen from any direction.'),
	)

	def test_registry_table(self):
		'''	The table the Viewer mirrors: five values, their meanings, a Sonador-SR 0.3 code each
		'''
		self.assertEqual(SONADOR_MEASUREMENT_VIEWS, tuple(value for value, _ in self.TABLE))
		self.assertEqual(list(DCMSR_MEASUREMENT_VIEW_CODES.keys()), list(SONADOR_MEASUREMENT_VIEWS))
		for value, meaning in self.TABLE:
			code = DCMSR_MEASUREMENT_VIEW_CODES[value]
			self.assertEqual((code.value, code.scheme_designator, code.scheme_version, code.meaning), (value, SONADOR_SR, SONADOR_SCHEME_VERSION_03, meaning))
			self.assertLessEqual(len(meaning), 64)

	def test_constants(self):
		from ...apisettings.sr import measurements as registry
		for name in ('AXIAL', 'SAGITTAL', 'CORONAL', 'OBLIQUE', '3D'):
			value = getattr(registry, 'SONADOR_MEASUREMENT_VIEW_%s' % name)
			self.assertIs(getattr(registry, 'DCMSR_MEASUREMENT_VIEW_%s' % name), DCMSR_MEASUREMENT_VIEW_CODES[value])

	def test_registered_values_written_as_text_without_diagnostic(self):
		model = model_of([sagittal_length(views=['sagittal', 'oblique'])])
		doc = roundtrip(model.create_sr(record_instances=True))
		items = children_named(num_of(group_of(doc)), DCMSR_MEASUREMENT_VIEW)
		self.assertEqual([(i.ValueType, i.RelationshipType, i.TextValue) for i in items], [('TEXT', 'HAS PROPERTIES', 'sagittal'), ('TEXT', 'HAS PROPERTIES', 'oblique')])
		self.assertNotIn(SR_CONDITION_UNREGISTERED_VIEW, conditions(model.diagnostics))

	def test_unregistered_value_written_with_a_warning_refused_in_strict(self):
		model = model_of([sagittal_length(views=['Sagittal-ish'])])
		doc = roundtrip(model.create_sr(record_instances=True))
		self.assertEqual([i.TextValue for i in children_named(num_of(group_of(doc)), DCMSR_MEASUREMENT_VIEW)], ['Sagittal-ish'])
		self.assertEqual([d.severity for d in sr_diagnostics_with(model.diagnostics, condition=SR_CONDITION_UNREGISTERED_VIEW)], ['warning'])
		with self.assertRaises(SRDiagnosticError):
			model_of([sagittal_length(views=['Sagittal-ish'])]).create_sr(record_instances=True, strict=True)

	def test_unknown_value_read_verbatim_and_written_back_without_diagnostic(self):
		doc = written([sagittal_length(views=['legacy-view'])])
		model = reparse(doc)
		self.assertEqual(measurements_of(model)[0].views, ('legacy-view',))
		again = roundtrip(model.create_sr(strict=True))
		self.assertEqual([i.TextValue for i in children_named(num_of(group_of(again)), DCMSR_MEASUREMENT_VIEW)], ['legacy-view'])
		self.assertNotIn(SR_CONDITION_UNREGISTERED_VIEW, conditions(model.diagnostics))

	def test_view_and_drawing_plane_are_independent(self):
		'''	Neither is derived from the other: a plane without a view, a view without a plane
		'''
		doc = provenance_written([sagittal_length(drawing_plane=SAGITTAL, series_uid=SYNTHETIC['SeriesInstanceUID'])])
		num = num_of(group_of(doc))
		self.assertEqual(children_named(num, DCMSR_MEASUREMENT_VIEW), [])
		self.assertEqual(len(children_named(num, DCMSR_MEASUREMENT_DRAWING_PLANE)), 1)
		doc = provenance_written([sagittal_length(views=['sagittal'])])
		self.assertEqual(children_named(num_of(group_of(doc)), DCMSR_MEASUREMENT_DRAWING_PLANE), [])


class DrawingPlaneTestCase(unittest.TestCase):
	def test_concepts_registered_at_0_3(self):
		for code, value in ((DCMSR_MEASUREMENT_SOURCE_SERIES, 'Sonador-Measurement.Source-Series'),
				(DCMSR_MEASUREMENT_DRAWING_PLANE, 'Sonador-Measurement.Drawing-Plane')):
			self.assertEqual((code.value, code.scheme_designator, code.scheme_version), (value, SONADOR_SR, SONADOR_SCHEME_VERSION_03))
			self.assertTrue(code.meaning)

	def test_text_format(self):
		self.assertEqual(drawing_plane_to_text(SAGITTAL), SAGITTAL_TEXT)
		self.assertEqual(drawing_plane_to_text((OBLIQUE_WIDTH, OBLIQUE_HEIGHT)), OBLIQUE_TEXT)
		self.assertEqual(drawing_plane_to_text(((1e-9, -1e-9, 1.0), (-0.0, 1.0, 0.0))), '0,0,1,0,1,0')
		self.assertEqual(drawing_plane_to_text(((0.12345678, 0, 0), (0, 0, 0))), '0.123457,0,0,0,0,0')
		self.assertNotIn('e', drawing_plane_to_text(((1e-20, 1e20, 0), (0, 0, 0))))

	def test_text_read(self):
		self.assertEqual(drawing_plane_from_text(' 0, 1 ,0,0,0 , -1 '), SAGITTAL)
		for malformed in ('0,1,0,0,0', '0,1,0,0,0,-1,0', '0,1,0,0,0,x', '0,1,0,0,0,nan', '0,1,0,0,0,1e0', ''):
			self.assertIsNone(drawing_plane_from_text(malformed), malformed)

	def test_validation_names_the_check(self):
		points = [(0.0, 0.0, 0.0), (0.0, 3.0, -4.0)]
		self.assertIsNone(drawing_plane_failed_check(SAGITTAL, points))
		self.assertEqual(drawing_plane_failed_check(((0, 2, 0), (0, 0, -1)), points), 'unit-width')
		self.assertEqual(drawing_plane_failed_check(((0, 1, 0), (0, 0, -1.01)), points), 'unit-height')
		self.assertEqual(drawing_plane_failed_check(((0, 1, 0), (0, 1, 0)), points), 'perpendicular')
		self.assertEqual(drawing_plane_failed_check(SAGITTAL, [(0, 0, 0), (1.5, 3, -4)]), 'points-within-1mm')
		self.assertIsNone(drawing_plane_failed_check(SAGITTAL, [(0, 0, 0), (0.9, 3, -4)]))

	def test_model(self):
		m = sagittal_length(drawing_plane=[0, 1, 0, 0, 0, -1])
		self.assertEqual(m.drawing_plane, SAGITTAL)
		self.assertIsNone(m.drawing_plane_text)
		with self.assertRaises(ValueError):
			length_image(drawing_plane=SAGITTAL)
		with self.assertRaises(ValueError):
			sagittal_length(drawing_plane=((0, 1, 0), (0, 0)))
		with self.assertRaises(ValueError):
			sagittal_length(drawing_plane=((0, 1, 0), (0, 0, float('nan'))))
		with self.assertRaises(ValueError):
			m.geometry = Geometry('image', 'POLYLINE', [(0, 0), (1, 1)], synthetic_reference())

	def test_absent_reads_without_diagnostic(self):
		model = reparse(provenance_written([sagittal_length()]))
		self.assertIsNone(measurements_of(model)[0].drawing_plane)
		self.assertNotIn(SR_CONDITION_INVALID_DRAWING_PLANE, conditions(model.diagnostics))

	def test_valid_plane_on_a_length_in_x_0(self):
		doc = provenance_written([sagittal_length(drawing_plane=SAGITTAL)])
		item = children_named(num_of(group_of(doc)), DCMSR_MEASUREMENT_DRAWING_PLANE)[0]
		self.assertEqual((item.ValueType, item.RelationshipType, item.TextValue), ('TEXT', 'HAS PROPERTIES', SAGITTAL_TEXT))
		model = reparse(doc)
		m = measurements_of(model)[0]
		self.assertEqual(m.drawing_plane, ((0.0, 1.0, 0.0), (0.0, 0.0, -1.0)))
		self.assertEqual(model.diagnostics and [d for d in model.diagnostics if d.condition == SR_CONDITION_INVALID_DRAWING_PLANE], [])

	def test_malformed_values_warn_read_absent_and_written_back_as_read(self):
		for text in ('0,1,0,0,0', '0,2,0,0,0,-1', ' 0, 2,0,0,0,-1'):
			doc = provenance_written([sagittal_length(drawing_plane=SAGITTAL)])
			set_drawing_plane_text(doc, text)
			model = reparse(doc)
			m = measurements_of(model)[0]
			self.assertIsNone(m.drawing_plane, text)
			warnings_ = sr_diagnostics_with(model.diagnostics, condition=SR_CONDITION_INVALID_DRAWING_PLANE)
			self.assertEqual([(d.severity, d.value) for d in warnings_], [('warning', TRACKING)], text)
			again = roundtrip(model.create_sr())
			self.assertEqual([i.TextValue for i in children_named(num_of(group_of(again)), DCMSR_MEASUREMENT_DRAWING_PLANE)], [text])
			with self.assertRaises(SRDiagnosticError):
				reparse(doc, strict=True)

	def test_plane_off_the_points_is_malformed_on_read(self):
		doc = provenance_written([sagittal_length(drawing_plane=SAGITTAL)])
		set_drawing_plane_text(doc, '1,0,0,0,1,0')
		model = reparse(doc)
		self.assertIsNone(measurements_of(model)[0].drawing_plane)
		self.assertIn(SR_CONDITION_INVALID_DRAWING_PLANE, conditions(model.diagnostics))

	def test_parsed_text_written_back_verbatim(self):
		doc = provenance_written([sagittal_length(drawing_plane=SAGITTAL)])
		set_drawing_plane_text(doc, ' 0.0, 1.000 ,0,0,0,-1')
		model = reparse(doc)
		m = measurements_of(model)[0]
		self.assertEqual(m.drawing_plane, SAGITTAL)
		again = roundtrip(model.create_sr())
		self.assertEqual([i.TextValue for i in children_named(num_of(group_of(again)), DCMSR_MEASUREMENT_DRAWING_PLANE)], [' 0.0, 1.000 ,0,0,0,-1'])
		m.drawing_plane = SAGITTAL
		again = roundtrip(model.create_sr())
		self.assertEqual([i.TextValue for i in children_named(num_of(group_of(again)), DCMSR_MEASUREMENT_DRAWING_PLANE)], [SAGITTAL_TEXT])
		m.drawing_plane = None
		again = roundtrip(model.create_sr())
		self.assertEqual(children_named(num_of(group_of(again)), DCMSR_MEASUREMENT_DRAWING_PLANE), [])

	def test_write_refuses_an_invalid_plane_naming_the_check(self):
		for plane, check in ((((0, 2, 0), (0, 0, -1)), 'unit-width'), (((0, 1, 0), (0, 1, 0)), 'perpendicular'), (((1, 0, 0), (0, 1, 0)), 'points-within-1mm')):
			with self.assertRaises(ValueError) as ctx:
				provenance_written([sagittal_length(drawing_plane=plane)])
			self.assertIn(check, str(ctx.exception))

	def test_edited_points_are_checked_against_a_parsed_plane(self):
		model = reparse(provenance_written([sagittal_length(drawing_plane=SAGITTAL)]))
		m = measurements_of(model)[0]
		m.p2 = (5.0, 3.0, -4.0)
		with self.assertRaises(ValueError) as ctx:
			model.create_sr()
		self.assertIn('points-within-1mm', str(ctx.exception))
		m.p2 = (0.5, 6.0, -8.0)
		again = roundtrip(model.create_sr())
		self.assertEqual([i.TextValue for i in children_named(num_of(group_of(again)), DCMSR_MEASUREMENT_DRAWING_PLANE)], [SAGITTAL_TEXT])

	def test_collinear_angle_of_180_degrees_read_and_written_as_stored(self):
		angle = AngleMeasurement((0.0, -10.0, 0.0), (0.0, 0.0, 0.0), (0.0, 10.0, 0.0), frame_of_reference_uid=FOR_UID, tracking_uid=TRACKING)
		doc = provenance_written([angle])
		model = reparse(doc)
		m = measurements_of(model)[0]
		self.assertAlmostEqual(m.value, 180.0, places=6)
		self.assertIsNone(m.drawing_plane)
		again = roundtrip(model.create_sr())
		scoord = srdoc_children(num_of(group_of(again)))[0]
		self.assertEqual(list(scoord.GraphicData), list(srdoc_children(num_of(group_of(doc)))[0].GraphicData))
		self.assertEqual(num_of(group_of(again)).MeasuredValueSequence[0].NumericValue, num_of(group_of(doc)).MeasuredValueSequence[0].NumericValue)

	def test_skew_cobb_angle_is_90_degrees_and_never_flattened(self):
		cobb = CobbAngleMeasurement(((0.0, 0.0, 0.0), (10.0, 0.0, 0.0)), ((0.0, 10.0, 10.0), (0.0, 20.0, 10.0)), frame_of_reference_uid=FOR_UID, tracking_uid=TRACKING)
		self.assertAlmostEqual(cobb.value, 90.0, places=6)
		doc = provenance_written([cobb])
		points = [0.0, 0.0, 0.0, 10.0, 0.0, 0.0, 0.0, 10.0, 10.0, 0.0, 20.0, 10.0]
		self.assertEqual(list(srdoc_children(num_of(group_of(doc)))[0].GraphicData), points)
		# No common plane: no drawing plane holds the four points
		for plane in (((1, 0, 0), (0, 1, 0)), ((1, 0, 0), (0, 0, 1)), ((0, 1, 0), (0, 0, 1))):
			self.assertEqual(drawing_plane_failed_check(plane, cobb.points), 'points-within-1mm')
		model = reparse(doc)
		m = measurements_of(model)[0]
		self.assertAlmostEqual(m.value, 90.0, places=6)
		again = roundtrip(model.create_sr())
		self.assertEqual(list(srdoc_children(num_of(group_of(again)))[0].GraphicData), points)


class SourceSeriesTestCase(unittest.TestCase):
	def test_written_under_the_num_with_its_evidence(self):
		doc = provenance_written([sagittal_length(series_uid=SECOND_SERIES_UID)], dcm_evidence=two_series_evidence())
		items = children_named(num_of(group_of(doc)), DCMSR_MEASUREMENT_SOURCE_SERIES)
		self.assertEqual([(i.ValueType, i.RelationshipType, i.UID) for i in items], [('UIDREF', 'HAS PROPERTIES', SECOND_SERIES_UID)])
		recorded = [(s.SeriesInstanceUID, [i.ReferencedSOPInstanceUID for i in s.ReferencedSOPSequence])
			for study in doc.CurrentRequestedProcedureEvidenceSequence for s in study.ReferencedSeriesSequence]
		self.assertIn((SECOND_SERIES_UID, [SECOND_SOP_UID]), recorded)

	def test_read_into_series_uid_and_written_back(self):
		model = reparse(provenance_written([sagittal_length(series_uid=SECOND_SERIES_UID)]))
		m = measurements_of(model)[0]
		self.assertEqual((m.series_uid, m.parsed_series_uid), (SECOND_SERIES_UID, SECOND_SERIES_UID))
		again = roundtrip(model.create_sr())
		self.assertEqual([i.UID for i in children_named(num_of(group_of(again)), DCMSR_MEASUREMENT_SOURCE_SERIES)], [SECOND_SERIES_UID])

	def test_without_series_uid_no_property(self):
		doc = provenance_written([sagittal_length()])
		self.assertEqual(children_named(num_of(group_of(doc)), DCMSR_MEASUREMENT_SOURCE_SERIES), [])

	def test_unresolvable_series_refused(self):
		with self.assertRaises(ValueError) as ctx:
			provenance_written([sagittal_length(series_uid='2.25.404')], dcm_evidence=[evidence_dataset()])
		self.assertIn('2.25.404', str(ctx.exception))

	def test_parsed_unresolvable_series_carried_new_one_refused(self):
		doc = provenance_written([sagittal_length(series_uid=SECOND_SERIES_UID)])
		children_named(num_of(group_of(doc)), DCMSR_MEASUREMENT_SOURCE_SERIES)[0].UID = '2.25.404'
		model = reparse(doc)
		again = roundtrip(model.create_sr())
		self.assertEqual([i.UID for i in children_named(num_of(group_of(again)), DCMSR_MEASUREMENT_SOURCE_SERIES)], ['2.25.404'])
		measurements_of(model)[0].series_uid = '2.25.405'
		with self.assertRaises(ValueError):
			model.create_sr()

	def test_image_domain_writes_none_and_ignores_one_read(self):
		doc = written([length_image(series_uid=SECOND_SERIES_UID)])
		num = num_of(group_of(doc))
		self.assertEqual(children_named(num, DCMSR_MEASUREMENT_SOURCE_SERIES), [])
		item = Dataset()
		item.RelationshipType, item.ValueType, item.UID = 'HAS PROPERTIES', 'UIDREF', SECOND_SERIES_UID
		item.ConceptNameCodeSequence = Sequence([code_item(DCMSR_MEASUREMENT_SOURCE_SERIES.value, 'Sonador-SR', 'x', 'a', 'b', 'c').ConceptNameCodeSequence[0]])
		num.ContentSequence = Sequence(srdoc_children(num) + [item])
		model = reparse(doc)
		m = measurements_of(model)[0]
		self.assertEqual(m.series_uid, SYNTHETIC['SeriesInstanceUID'])
		self.assertEqual([d.severity for d in sr_diagnostics_with(model.diagnostics, condition=SR_CONDITION_PROVENANCE_ON_IMAGE_DOMAIN)], ['info'])
		self.assertEqual(len(m.extra_items), 1)

	def test_two_series_in_one_frame_of_reference(self):
		doc = provenance_written([
			sagittal_length(series_uid=SYNTHETIC['SeriesInstanceUID'], tracking_uid='2.25.9601'),
			sagittal_cobb(series_uid=SECOND_SERIES_UID, tracking_uid='2.25.9602'),
			sagittal_length(tracking_uid='2.25.9603'),
		])
		ms = measurements_of(reparse(doc))
		self.assertEqual([(m.tracking_uid, m.series_uid, m.frame_of_reference_uid) for m in ms],
			[('2.25.9601', SYNTHETIC['SeriesInstanceUID'], FOR_UID), ('2.25.9602', SECOND_SERIES_UID, FOR_UID), ('2.25.9603', None, FOR_UID)])


class SourceSeriesPacs(MeasurementRetrievalPacs):
	'''	The mocked study with a second image series in the same Frame of Reference and an SR series D
		whose document holds patient-domain measurements from both series and one naming a series the
		study does not hold
	'''
	def __init__(self, iserver):
		super().__init__(iserver)
		self.add('Series', 'sx-mr2', {'SeriesInstanceUID': SECOND_SERIES_UID, 'Modality': 'MR', 'SeriesNumber': '2',
			'FrameOfReferenceUID': FOR_UID}, ParentStudy='study-1', Instances=['dcm-mr2-1'])
		self.add('Instance', 'dcm-mr2-1', {'SOPInstanceUID': SECOND_SOP_UID, 'SOPClassUID': SYNTHETIC['SOPClassUID'], 'InstanceNumber': '1'},
			ParentSeries='sx-mr2', IndexInSeries=1)
		self.resources['study-1']['Series'].insert(1, 'sx-mr2')
		doc = new_model([], measurements=[sagittal_length(series_uid=SECOND_SERIES_UID, tracking_uid='2.25.9701'),
			sagittal_angle(series_uid=SYNTHETIC['SeriesInstanceUID'], tracking_uid='2.25.9702'),
			sagittal_length(series_uid=SECOND_SERIES_UID, tracking_uid='2.25.9703')],
			dcm_evidence=two_series_evidence()).create_sr(record_instances=True, dcmsr_series_uid='2.25.710', dcmsr_instance_uid='2.25.7101', previous=None)
		children_named(num_of(group_of(doc, 2)), DCMSR_MEASUREMENT_SOURCE_SERIES)[0].UID = '2.25.404'
		doc.ContentDate, doc.ContentTime = '20260101', '100000'
		doc = roundtrip(doc)
		self.add('Series', 'sx-srd', {'SeriesInstanceUID': '2.25.710', 'Modality': 'SR', 'SeriesNumber': '47'}, ParentStudy='study-1', Instances=['sr-d1'])
		self.add('Instance', 'sr-d1', {'SOPInstanceUID': str(doc.SOPInstanceUID), 'SOPClassUID': str(doc.SOPClassUID),
			'ContentDate': doc.ContentDate, 'ContentTime': doc.ContentTime, 'InstanceNumber': '1'}, ParentSeries='sx-srd', IndexInSeries=1)
		self.files['sr-d1'] = tobytes(doc)
		self.resources['study-1']['Series'].append('sx-srd')


class SourceSeriesRetrievalTestCase(unittest.TestCase):
	def setUp(self):
		self.server = SonadorServer('https://sonador.invalid', apitoken='token', verify=False)
		self.iserver = self.server.imageserver_modelinstance_from_json(dict(IMAGESERVER_JSON))
		self.pacs = SourceSeriesPacs(self.iserver)
		studies = self.server._init_dataclass_from_json(ImagingStudyCollection, [dict(self.pacs.resources['study-1'])], pacs=self.iserver)
		studies.bulkpopulate_related(child_instances=True)
		self.study = studies[0]

	def series(self, pk):
		return next(sx for sx in self.study.series_collection if sx.pk == pk)

	def test_keyed_by_source_series_with_one_file_request_per_sr_series(self):
		by_series = self.study.measurements()
		self.assertEqual(sorted(self.pacs.file_requests()), ['sr-a2', 'sr-b1', 'sr-c1', 'sr-d1'])
		self.assertEqual(self.pacs.count('GET'), 4)
		self.assertEqual([m.tracking_uid for m in by_series[SECOND_SERIES_UID]['length']], ['2.25.9701'])
		self.assertEqual([m.tracking_uid for m in by_series[CT_SERIES_UID]['angle']], ['2.25.9702'])
		self.assertEqual(by_series[SECOND_SERIES_UID]['length'][0].series.pk, 'sx-mr2')
		# A source series the study does not hold: the document's series, with a warning
		self.assertEqual([m.tracking_uid for m in by_series['sx-srd']['length']], ['2.25.9703'])
		instance = next(sx for sx in self.study.sr_collection if sx.pk == 'sx-srd').instances_collection.current_instance()
		warnings_ = sr_diagnostics_with(instance.diagnostics, condition=SR_CONDITION_UNKNOWN_SOURCE_SERIES)
		self.assertEqual([(d.severity, d.value) for d in warnings_], [('warning', '2.25.404')])
		self.study.measurements()
		self.assertEqual(len(sr_diagnostics_with(instance.diagnostics, condition=SR_CONDITION_UNKNOWN_SOURCE_SERIES)), 1)

	def test_series_and_frame_of_reference_accessors(self):
		self.study.measurements()
		before = len(self.pacs.requests)
		self.assertEqual([m.tracking_uid for m in self.series('sx-mr2').measurements('length')], ['2.25.9701'])
		found = self.series('sx-mr2').measurements('length', frame_of_reference_uid=FOR_UID)
		self.assertEqual(sorted(m.tracking_uid for m in found), sorted(['2.25.9701', '2.25.9703', TRACKING, '2.25.9301']))
		self.assertEqual(len(self.pacs.requests), before)

	def test_writer_resolves_against_the_cached_study_series_without_a_request(self):
		ct = self.series('sx-ct')
		before = self.pacs.count('GET')
		report = SonadorImagingMeasurementReport(sonador_ref_series=ct, report_procedure=PROCEDURE, record_instances=False,
			measurements=[sagittal_length(series_uid=SECOND_SERIES_UID, tracking_uid='2.25.9801')])
		doc = roundtrip(report.create_sr())
		self.assertEqual(self.pacs.count('GET'), before)
		recorded = dict((s.SeriesInstanceUID, [i.ReferencedSOPInstanceUID for i in s.ReferencedSOPSequence])
			for study in doc.CurrentRequestedProcedureEvidenceSequence for s in study.ReferencedSeriesSequence)
		self.assertEqual(recorded[SECOND_SERIES_UID], [SECOND_SOP_UID])
		self.assertIn(CT_SERIES_UID, recorded)

		with self.assertRaises(ValueError):
			SonadorImagingMeasurementReport(sonador_ref_series=ct, report_procedure=PROCEDURE, record_instances=False,
				measurements=[sagittal_length(series_uid='2.25.404')]).create_sr()


class DocumentClassTestCase(unittest.TestCase):
	def test_only_comprehensive_3d_written(self):
		from highdicom.sr import EnhancedSR
		from ...imaging.helpers.sr.parse import DCMSR_WRITER_CLASSES
		from ...apisettings.sr.seriestag import DCMSR_LAYOUTS_WRITTEN, DCMSR_LAYOUT_NESTED
		self.assertEqual(DCMSR_LAYOUTS_WRITTEN, (DCMSR_LAYOUT_NESTED,))
		self.assertNotIn(EnhancedSR, DCMSR_WRITER_CLASSES.values())
		for cls in (SonadorComprehensiveSR3D, SonadorImagingMeasurementReport):
			model = new_model([], measurements=[length_image()])
			doc = cls(measurements=[length_image()], report_procedure=PROCEDURE, dcm_evidence=[evidence_dataset()]).create_sr(record_instances=True)
			self.assertEqual(str(doc.SOPClassUID), DCMSR_SOP_CLASS_COMPREHENSIVE_3D)
			with self.assertRaises(ValueError):
				model.create_sr(document_template=EnhancedSR)

	def test_comprehensive_3d_parent_has_no_upgrade_diagnostic(self):
		model = reparse(written([length_image()]))
		model.create_sr()
		self.assertNotIn(SR_CONDITION_DOCUMENT_CLASS_UPGRADED, conditions(model.diagnostics))


FIXTURE_DATE = '2026-1111'
FIXTURE_SERIES = (SYNTHETIC['SeriesInstanceUID'], SECOND_SERIES_UID)


def fixture_path(topic):
	return os.path.join(FIXTURES, 'sonador-io.%s.%s.dcm' % (topic, FIXTURE_DATE))


def fixture_measurements(topic):
	'''	The measurements of a Python-written fixture
	'''
	def properties(view, series_uid=SYNTHETIC['SeriesInstanceUID'], plane=SAGITTAL, **kwargs):
		values = dict(description='%s measurement' % topic, text='Synthetic', location='Synthetic location', views=[view],
			display=DisplayAttributes.from_color(COLORS), tracking_uid='2.25.8100', series_uid=series_uid, drawing_plane=plane)
		values.update(kwargs)
		return values
	if topic == 'length-image':
		return [length_image(**properties('axial', series_uid=None, plane=None))]
	if topic == 'length-oblique':
		return [oblique_length(**properties('oblique', plane=(OBLIQUE_WIDTH, OBLIQUE_HEIGHT)))]
	if topic == 'two-series':
		return [sagittal_length(**properties('sagittal', tracking_uid='2.25.8101')),
			sagittal_cobb(**properties('sagittal', series_uid=SECOND_SERIES_UID, tracking_uid='2.25.8102')),
			sagittal_angle(**properties('sagittal', series_uid=SECOND_SERIES_UID, tracking_uid='2.25.8103'))]
	return [{'length': sagittal_length, 'angle': sagittal_angle, 'cobb-angle': sagittal_cobb}[topic](**properties('sagittal'))]


def write_measurement_fixture(topic):
	doc = new_model([], measurements=fixture_measurements(topic), dcm_evidence=two_series_evidence() if topic == 'two-series' else [evidence_dataset()]).create_sr(
		record_instances=True, dcmsr_series_uid='2.25.8000', dcmsr_instance_uid='2.25.8001')
	doc.save_as(fixture_path(topic))


class MeasurementFixtureTestCase(unittest.TestCase):
	# topic -> [(class, value type, value, unit, source series, drawing-plane text, view)]
	EXPECTED = OrderedDict((
		('length', [(LengthMeasurement, 'SCOORD3D', 5.0, 'mm', FIXTURE_SERIES[0], SAGITTAL_TEXT, 'sagittal')]),
		('length-image', [(LengthMeasurement, 'SCOORD', math.hypot(2.0, 6.0), 'mm', FIXTURE_SERIES[0], None, 'axial')]),
		('angle', [(AngleMeasurement, 'SCOORD3D', 90.0, 'deg', FIXTURE_SERIES[0], SAGITTAL_TEXT, 'sagittal')]),
		('cobb-angle', [(CobbAngleMeasurement, 'SCOORD3D', 135.0, 'deg', FIXTURE_SERIES[0], SAGITTAL_TEXT, 'sagittal')]),
		('length-oblique', [(LengthMeasurement, 'SCOORD3D', 20.0, 'mm', FIXTURE_SERIES[0], OBLIQUE_TEXT, 'oblique')]),
		('two-series', [(LengthMeasurement, 'SCOORD3D', 5.0, 'mm', FIXTURE_SERIES[0], SAGITTAL_TEXT, 'sagittal'),
			(CobbAngleMeasurement, 'SCOORD3D', 135.0, 'deg', FIXTURE_SERIES[1], SAGITTAL_TEXT, 'sagittal'),
			(AngleMeasurement, 'SCOORD3D', 90.0, 'deg', FIXTURE_SERIES[1], SAGITTAL_TEXT, 'sagittal')]),
	))

	def test_committed_fixtures(self):
		for topic, expected in self.EXPECTED.items():
			if os.environ.get('UPDATE_SR_FIXTURES'):
				write_measurement_fixture(topic)
			doc = read(fixture_path(topic))
			self.assertEqual((doc.PatientID, str(doc.StudyInstanceUID), str(doc.SOPClassUID)), ('ANONYMOUS', '2.25.1001', DCMSR_SOP_CLASS_COMPREHENSIVE_3D))
			model = reparse(doc, strict=True)
			ms = measurements_of(model)
			self.assertEqual(len(ms), len(expected), topic)
			for index, (m, (cls, value_type, value, unit, series_uid, plane_text, view)) in enumerate(zip(ms, expected)):
				self.assertIsInstance(m, cls)
				self.assertAlmostEqual(m.value, value, places=3)
				self.assertEqual(m.unit.value, unit)
				self.assertEqual(m.description, '%s measurement' % topic)
				self.assertEqual((m.series_uid, m.drawing_plane_text, m.views), (series_uid, plane_text, (view,)), topic)
				num = num_of(group_of(doc, index))
				self.assertEqual(srdoc_children(num)[0].ValueType, value_type)
				self.assertEqual((num.ConceptNameCodeSequence[0].CodeValue, num.ConceptNameCodeSequence[0].CodingSchemeDesignator),
					('410668003', 'SCT') if cls is LengthMeasurement else ('285285000', 'SCT'))
				if value_type == 'SCOORD3D':
					self.assertEqual(srdoc_children(num)[0].ReferencedFrameOfReferenceUID, FOR_UID)
			recorded = set(s.SeriesInstanceUID for study in doc.CurrentRequestedProcedureEvidenceSequence for s in study.ReferencedSeriesSequence)
			self.assertEqual(recorded, set(e[4] for e in expected))
			again = roundtrip(reparse(doc).create_sr())
			self.assertEqual(str(again.SOPClassUID), DCMSR_SOP_CLASS_COMPREHENSIVE_3D)
			self.assertEqual([m.tracking_uid for m in measurements_of(reparse(again))], [m.tracking_uid for m in ms])
			self.assertEqual([m.drawing_plane_text for m in measurements_of(reparse(again))], [e[5] for e in expected])


# Evidence of the source series, the writer class, the Viewer-authored out-of-plane fixture

def recorded_evidence(doc):
	'''	`{series_uid: [SOP Instance UID, ...]}` of both evidence sequences of a document
	'''
	recorded = OrderedDict()
	for name in ('CurrentRequestedProcedureEvidenceSequence', 'PertinentOtherEvidenceSequence'):
		for study in doc.get(name) or []:
			for series in study.ReferencedSeriesSequence:
				recorded.setdefault(series.SeriesInstanceUID, []).extend(i.ReferencedSOPInstanceUID for i in series.get('ReferencedSOPSequence') or [])
	return recorded


def generic_patient_length(series_uid, **kwargs):
	'''	Generic `Measurement` in the patient domain, written to the report's primary group
	'''
	return Measurement(5.0, name=codes.SCT.Length, unit=codes.UCUM.Millimeter,
		geometry=Geometry('patient', 'POLYLINE', [(0.0, 0.0, 0.0), (0.0, 3.0, -4.0)]),
		frame_of_reference_uid=FOR_UID, series_uid=series_uid, **kwargs)


class SourceSeriesEvidenceTestCase(unittest.TestCase):
	'''	Instance-level evidence for every source series, without `record_instances`
	'''
	def test_supplied_evidence_records_the_source_instance(self):
		model = model_of([sagittal_length(series_uid=SYNTHETIC['SeriesInstanceUID'])])
		self.assertEqual(model.source_series_references()[0].sop_instance_uid, SYNTHETIC['SOPInstanceUID'])
		doc = roundtrip(model.create_sr(record_instances=False))
		self.assertEqual(recorded_evidence(doc), {SYNTHETIC['SeriesInstanceUID']: [SYNTHETIC['SOPInstanceUID']]})
		again = roundtrip(reparse(doc).create_sr(record_instances=False))
		self.assertEqual(recorded_evidence(again), {SYNTHETIC['SeriesInstanceUID']: [SYNTHETIC['SOPInstanceUID']]})
		self.assertEqual(measurements_of(reparse(again))[0].series_uid, SYNTHETIC['SeriesInstanceUID'])

	def test_generic_measurement_in_the_primary_group_is_resolved(self):
		doc = roundtrip(model_of([generic_patient_length(SECOND_SERIES_UID)], dcm_evidence=two_series_evidence()).create_sr(record_instances=False))
		self.assertEqual(recorded_evidence(doc)[SECOND_SERIES_UID], [SECOND_SOP_UID])
		with self.assertRaises(ValueError) as ctx:
			model_of([generic_patient_length('2.25.404')]).create_sr()
		self.assertIn('2.25.404', str(ctx.exception))

	def test_measurement_of_an_explicit_group_is_resolved(self):
		from ...apisettings.base import ReportGroup
		group = ReportGroup(measurements=MeasurementCollection([generic_patient_length('2.25.404')]), tracking_id='consumer-group')
		with self.assertRaises(ValueError):
			model_of([], groups=[group]).create_sr()

	def test_only_comprehensive_3d_classes_written(self):
		from highdicom.sr import ComprehensiveSR, Comprehensive3DSR, EnhancedSR
		for cls in (ComprehensiveSR, EnhancedSR):
			with self.assertRaises(ValueError):
				model_of([length_image()]).create_sr(document_template=cls)
			model = model_of([length_image()])
			model.dcmsr_document_template = cls
			with self.assertRaises(ValueError):
				model.create_sr()

		class Extended3DSR(Comprehensive3DSR):
			pass
		doc = model_of([length_image()]).create_sr(document_template=Extended3DSR)
		self.assertEqual(str(doc.SOPClassUID), DCMSR_SOP_CLASS_COMPREHENSIVE_3D)


class SourceSeriesCachedEvidenceTestCase(unittest.TestCase):
	'''	Source-series evidence against a cached study: reference series, changes after a first write
	'''
	def setUp(self):
		SourceSeriesRetrievalTestCase.setUp(self)

	def series(self, pk):
		return next(sx for sx in self.study.series_collection if sx.pk == pk)

	def report(self, *measurements):
		return SonadorImagingMeasurementReport(sonador_ref_series=self.series('sx-ct'), report_procedure=PROCEDURE,
			record_instances=False, measurements=list(measurements))

	def test_source_equal_to_the_reference_series(self):
		before = self.pacs.count('GET')
		doc = roundtrip(self.report(sagittal_length(series_uid=CT_SERIES_UID)).create_sr(record_instances=False))
		self.assertEqual(recorded_evidence(doc), {CT_SERIES_UID: [SYNTHETIC['SOPInstanceUID']]})
		again = roundtrip(reparse(doc).create_sr(record_instances=False))
		self.assertEqual(recorded_evidence(again), {CT_SERIES_UID: [SYNTHETIC['SOPInstanceUID']]})
		self.assertEqual(self.pacs.count('GET'), before)

	def test_changed_source_after_a_write(self):
		m = sagittal_length(series_uid=CT_SERIES_UID)
		report = self.report(m)
		before = self.pacs.count('GET')
		first = roundtrip(report.create_sr())
		self.assertNotIn(SECOND_SERIES_UID, recorded_evidence(first))
		m.series_uid = SECOND_SERIES_UID
		second = roundtrip(report.create_sr())
		self.assertEqual([i.UID for i in children_named(num_of(group_of(second)), DCMSR_MEASUREMENT_SOURCE_SERIES)], [SECOND_SERIES_UID])
		self.assertEqual(recorded_evidence(second)[SECOND_SERIES_UID], [SECOND_SOP_UID])
		self.assertEqual(self.pacs.count('GET'), before)

	def test_measurement_added_after_the_evidence_was_read(self):
		report = self.report(sagittal_length(series_uid=CT_SERIES_UID, tracking_uid='2.25.9811'))
		before = self.pacs.count('GET')
		self.assertEqual(set(ds.SeriesInstanceUID for ds in report.evidence), {CT_SERIES_UID})
		report.measurements = list(report.measurements) + [sagittal_cobb(series_uid=SECOND_SERIES_UID, tracking_uid='2.25.9812')]
		self.assertEqual(set(ds.SeriesInstanceUID for ds in report.evidence), {CT_SERIES_UID, SECOND_SERIES_UID})
		doc = roundtrip(report.create_sr())
		self.assertEqual(recorded_evidence(doc), {CT_SERIES_UID: [SYNTHETIC['SOPInstanceUID']], SECOND_SERIES_UID: [SECOND_SOP_UID]})
		self.assertEqual(self.pacs.count('GET'), before)


class ViewerOutOfPlaneTestCase(unittest.TestCase):
	'''	The out-of-plane report written by the Sonador Viewer (oak-tree/medical-imaging/ohif-viewers!122):
		Length, Angle and Cobb angle drawn in a sagittal Inspector and a Length on an oblique plane,
		on the Viewer's synthetic CT series
	'''
	FIXTURE = os.path.join(FIXTURES, 'sonador-viewer.out-of-plane.2026-1110.dcm')
	SOURCE = '2.25.316170002'
	FRAME = '2.25.316170003'
	# tracking UID -> (class, value, unit, view, drawing-plane text)
	EXPECTED = OrderedDict((
		('2.25.900000', (LengthMeasurement, 27.6586333718786, 'mm', 'sagittal', '0,1,0,0,0,-1')),
		('2.25.900001', (LengthMeasurement, 28.7228132326901, 'mm', 'oblique', '0.707107,0,-0.707107,0,-1,0')),
		('2.25.900002', (AngleMeasurement, 90.0, 'deg', 'sagittal', '0,1,0,0,0,-1')),
		('2.25.900003', (CobbAngleMeasurement, 90.0, 'deg', 'sagittal', '0,1,0,0,0,-1')),
	))

	def assert_measurements(self, model):
		ms = measurements_of(model)
		self.assertEqual([m.tracking_uid for m in ms], list(self.EXPECTED))
		for m in ms:
			cls, value, unit, view, plane = self.EXPECTED[m.tracking_uid]
			self.assertIsInstance(m, cls)
			self.assertTrue(m.geometry.is_patient)
			self.assertEqual((m.frame_of_reference_uid, m.series_uid, m.views, m.drawing_plane_text), (self.FRAME, self.SOURCE, (view,), plane))
			self.assertIsNotNone(m.drawing_plane)
			self.assertAlmostEqual(m.value, value, places=6)
			self.assertEqual(m.unit.value, unit)
		return ms

	def test_reader(self):
		doc = read(self.FIXTURE)
		self.assertEqual(str(doc.SOPClassUID), DCMSR_SOP_CLASS_COMPREHENSIVE_3D)
		model = reparse(doc, strict=True)
		self.assert_measurements(model)
		self.assertFalse(sr_diagnostics_with(model.diagnostics, severity=SR_DIAGNOSTIC_WARNING))

	def test_unchanged_resave(self):
		doc = read(self.FIXTURE)
		ms = self.assert_measurements(reparse(doc))
		again = roundtrip(reparse(doc).create_sr(record_instances=False))
		self.assertEqual(str(again.SOPClassUID), DCMSR_SOP_CLASS_COMPREHENSIVE_3D)
		out = self.assert_measurements(reparse(again, strict=True))
		for source, written_ in zip(ms, out):
			self.assertEqual(written_.points, source.points)
		for index in range(len(self.EXPECTED)):
			src, dst = num_of(group_of(doc, index)), num_of(group_of(again, index))
			self.assertEqual(dst.MeasuredValueSequence[0].NumericValue, src.MeasuredValueSequence[0].NumericValue)
			self.assertEqual(dcm_code_value(dst.ConceptNameCodeSequence[0]), dcm_code_value(src.ConceptNameCodeSequence[0]))
		self.assertEqual(recorded_evidence(again), recorded_evidence(doc))
		self.assertIn(str(doc.SOPInstanceUID), [p.ReferencedSOPSequence[0].ReferencedSOPInstanceUID
			for study in again.PredecessorDocumentsSequence for p in study.ReferencedSeriesSequence])

	def test_parse_edit_write_reparse(self):
		model = reparse(read(self.FIXTURE))
		length = measurements_of(model)[0]
		length.p2 = (6.0, 8.0, 30.0)
		again = reparse(roundtrip(model.create_sr()), strict=True)
		edited = measurements_of(again)[0]
		self.assertAlmostEqual(edited.value, math.dist((6.0, -10.0, 4.0), (6.0, 8.0, 30.0)), places=4)
		self.assertEqual(edited.drawing_plane_text, '0,1,0,0,0,-1')
		length.p2 = (9.0, 8.0, 30.0)
		with self.assertRaises(ValueError):
			model.create_sr()
