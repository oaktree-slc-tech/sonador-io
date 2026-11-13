'''	Offline tests of the point classes: `PixelCoord`, a `(column, row)` position on an image grid, and
	`ImageCoord`, an `(x, y, z)` triple. Neither carries a unit; the API returning a value states its role
	and unit. Both compare and hash as the tuple of their values, convert to numpy arrays, and have no
	arithmetic of their own: a numpy operand gives the numpy result, any other operand raises `TypeError`.

	Covered: the classes themselves, `Geometry` normalisation and `array`, the point and direction
	properties of the measurement classes, documents written from tuples, lists, numpy arrays and the
	point classes, the frame conversions, `points2array`, `srencode_coord3d`, `drawing_plane_vectors`,
	`ImageCoordCollection.array` and the legacy SCOORD path of `sr2points`.

	Run from the directory containing the `sonador` package: python -m unittest sonador.ftests.test_sr.sr_points
'''
import json, logging, math, pickle, unittest

import numpy as np

from highdicom.sr import ScoordContentItem, GraphicTypeValues
from pydicom.sr.codedict import codes

from ...apisettings.base import PixelCoord, ImageCoord, ImageCoordCollection, ImageTransformMatrix, Geometry, ImageFrameGeometry, \
	LengthMeasurement, AngleMeasurement, CobbAngleMeasurement, CircleMeasurement, BidirectionalMeasurement, drawing_plane_vectors, \
	DCMSR_GEOMETRY_DOMAIN_IMAGE, DCMSR_GEOMETRY_DOMAIN_PATIENT, DCMSR_GRAPHIC_POLYLINE
from ...apisettings.sr import points2array, srencode_coord3d
from ...helpers.sr import sr2points
from ...imaging import PixelCoord as ImagingPixelCoord, ImageCoord as ImagingImageCoord
from ...imaging.orthanc.base import parse_image_orientation
from .sr_seriestag import synthetic_reference
from .sr_measurements import FOR_UID, CALIBRATION, SAGITTAL, written, reparse, measurements_of
from .sr_circle import OracleMixin


__all__ = ['PixelCoordTestCase', 'ImageCoordTestCase', 'PointArithmeticTestCase', 'GeometryPointsTestCase',
	'MeasurementPointsTestCase', 'PointInputDocumentTestCase', 'TransformPointsTestCase', 'PointArrayTestCase']


TRACKING = '2.25.9961'


def setUpModule():
	logging.getLogger('sonador').setLevel(logging.CRITICAL)


def tearDownModule():
	logging.getLogger('sonador').setLevel(logging.NOTSET)


class PixelCoordTestCase(unittest.TestCase):
	def test_values_are_floats_in_column_row_order(self):
		p = PixelCoord(3, 4)
		self.assertEqual((p.column, p.row), (3.0, 4.0))
		self.assertIsInstance(p.column, float)
		self.assertEqual(tuple(p), (3.0, 4.0))
		self.assertEqual(p[0], 3.0)
		self.assertEqual(p[-1], 4.0)
		column, row = p
		self.assertEqual((column, row), (3.0, 4.0))
		self.assertEqual(PixelCoord(row=4, column=3), p)

	def test_tuple_equality_and_hash(self):
		p = PixelCoord(1.5, 2.5)
		self.assertEqual(p, (1.5, 2.5))
		self.assertEqual((1.5, 2.5), p)
		self.assertNotEqual(p, (2.5, 1.5))
		self.assertEqual(hash(p), hash((1.5, 2.5)))
		self.assertIn((1.5, 2.5), {p})
		self.assertIn(p, {(1.5, 2.5): 'value'})

	def test_tuple_methods_kept(self):
		p = PixelCoord(1.0, 2.0)
		self.assertEqual(p.index(2.0), 1)
		self.assertEqual(p.count(1.0), 1)
		self.assertIsInstance(p, tuple)

	def test_values_are_read_only(self):
		p = PixelCoord(1.0, 2.0)
		with self.assertRaises(AttributeError):
			p.column = 3.0

	def test_not_a_number(self):
		with self.assertRaises((TypeError, ValueError)):
			PixelCoord('a', 1.0)

	def test_numpy_conversion(self):
		p, q = PixelCoord(1.0, 2.0), PixelCoord(3.0, 4.0)
		self.assertEqual(np.asarray(p).shape, (2,))
		self.assertEqual(np.asarray([p, q]).shape, (2, 2))
		self.assertEqual(np.asarray([p, q]).dtype, np.float64)
		self.assertEqual(np.asarray(p, dtype=np.float32).dtype, np.float32)
		self.assertIsInstance(np.asarray([p, q])[0], np.ndarray)

	def test_array_index(self):
		self.assertEqual(PixelCoord(10.4, 3.9).array_index(), (3, 10))
		self.assertEqual(PixelCoord(0.0, 0.0).array_index(), (0, 0))
		# Pixel centres are at .5; a boundary belongs to the next pixel
		self.assertEqual(PixelCoord(0.5, 0.5).array_index(), (0, 0))
		self.assertEqual(PixelCoord(1.0, 2.0).array_index(), (2, 1))
		self.assertEqual(PixelCoord(19.99, 2.0).array_index((3, 20)), (2, 19))
		pixels = np.arange(12).reshape(3, 4)
		self.assertEqual(pixels[PixelCoord(3.2, 1.7).array_index(pixels.shape)], pixels[1, 3])

	def test_array_index_bounds(self):
		for p in (PixelCoord(-0.3, 1.0), PixelCoord(1.0, -0.01)):
			with self.assertRaises(IndexError):
				p.array_index()
		with self.assertRaises(IndexError):
			PixelCoord(20.0, 1.0).array_index((3, 20))
		with self.assertRaises(IndexError):
			PixelCoord(1.0, 3.0).array_index((3, 20))

	def test_pickle_and_json(self):
		p = PixelCoord(1.0, 2.0)
		restored = pickle.loads(pickle.dumps(p))
		self.assertEqual(restored, p)
		self.assertIsInstance(restored, PixelCoord)
		self.assertEqual(json.dumps(p), '[1.0, 2.0]')

	def test_exported(self):
		self.assertIs(ImagingPixelCoord, PixelCoord)
		self.assertIs(ImagingImageCoord, ImageCoord)


class ImageCoordTestCase(unittest.TestCase):
	def test_sequence_protocol(self):
		p = ImageCoord(1.0, 2.0, 3.0)
		self.assertEqual(len(p), 3)
		self.assertEqual((p[0], p[1], p[2], p[-1]), (1.0, 2.0, 3.0, 3.0))
		self.assertEqual(p[1:], (2.0, 3.0))
		x, y, z = p
		self.assertEqual((x, y, z), (1.0, 2.0, 3.0))
		self.assertEqual(tuple(p), (1.0, 2.0, 3.0))
		self.assertEqual(max(p), 3.0)
		self.assertFalse(isinstance(p, tuple))

	def test_equality(self):
		p = ImageCoord(1.0, 2.0, 3.0)
		self.assertEqual(p, (1, 2, 3))
		self.assertEqual((1.0, 2.0, 3.0), p)
		self.assertEqual(p, [1.0, 2.0, 3.0])
		self.assertEqual([1.0, 2.0, 3.0], p)
		self.assertEqual(p, ImageCoord(1.0, 2.0, 3.0))
		self.assertNotEqual(p, ImageCoord(1.0, 2.0, 3.5))
		self.assertNotEqual(p, (1.0, 2.0))
		self.assertNotEqual(p, PixelCoord(1.0, 2.0))
		self.assertNotEqual(p, 'abc')
		self.assertEqual(ImageCoord(np.float64(1.0), 2.0, 3.0), p)
		self.assertEqual((ImageCoord(0, 0, 0), ImageCoord(1, 1, 1)), ((0.0, 0.0, 0.0), (1.0, 1.0, 1.0)))

	def test_equality_ignores_sr_attributes(self):
		frame = ImageTransformMatrix(np.identity(4), uid='2.25.1')
		self.assertEqual(ImageCoord(1.0, 2.0, 3.0, reference_frame=frame, name=codes.DCM.Center, point_type='POINT'),
			ImageCoord(1.0, 2.0, 3.0))

	def test_hash(self):
		p = ImageCoord(1.0, 2.0, 3.0)
		self.assertEqual(hash(p), hash((1.0, 2.0, 3.0)))
		self.assertEqual(hash(ImageCoord(np.float64(1.0), np.float64(2.0), np.float64(3.0))), hash(p))
		self.assertIn((1.0, 2.0, 3.0), {p})
		self.assertIn(ImageCoord(1.0, 2.0, 3.0), {(1.0, 2.0, 3.0): 'value'})

	def test_coordinates_are_read_only(self):
		p = ImageCoord(1.0, 2.0, 3.0)
		for name in ('x', 'y', 'z'):
			with self.assertRaises(AttributeError):
				setattr(p, name, 0.0)
		self.assertEqual(p, (1.0, 2.0, 3.0))

	def test_numpy_conversion(self):
		a, b = ImageCoord(1.0, 2.0, 3.0), ImageCoord(4.0, 6.0, 3.0)
		self.assertEqual(np.asarray(a).shape, (3,))
		np.testing.assert_array_equal(np.asarray(a), [1.0, 2.0, 3.0])
		self.assertEqual(np.asarray([a, b]).shape, (2, 3))
		self.assertEqual(np.asarray([a, b]).dtype, np.float64)
		self.assertEqual(np.asarray(a, dtype=np.float32).dtype, np.float32)
		self.assertIsInstance(np.asarray([a, b])[0], np.ndarray)
		self.assertAlmostEqual(float(np.linalg.norm(np.subtract(b, a))), 5.0)
		self.assertTrue(np.allclose(a, (1.0, 2.0, 3.0000001)))
		with self.assertRaises(ValueError):
			np.array(a, copy=False)

	def test_conversion_is_float64(self):
		for coordinates in ((1, 2, 3), tuple(np.float32([1.0, 2.0, 3.0])), (np.int32(1), 2, 3.0)):
			point = ImageCoord(*coordinates)
			values = np.asarray(point)
			self.assertEqual(values.dtype, np.float64, repr(coordinates))
			self.assertEqual(np.asarray([point, point]).dtype, np.float64, repr(coordinates))
			self.assertEqual(np.array(point).dtype, np.float64, repr(coordinates))
			values[0] = 1.5
			self.assertEqual(values[0], 1.5)
			batch = np.asarray([point, point])
			batch /= 2.0
			np.testing.assert_array_equal(batch[0], [0.5, 1.0, 1.5])
		self.assertEqual(np.asarray(ImageCoord(1, 2, 3), dtype=np.float32).dtype, np.float32)
		self.assertEqual(np.asarray(ImageCoord(1.5, 2, 3), dtype=np.int64).dtype, np.int64)
		with self.assertRaises(ValueError):
			np.array(ImageCoord(1, 2, 3), copy=False)

	def test_conversion_is_a_copy(self):
		a = ImageCoord(1.0, 2.0, 3.0)
		values = np.asarray(a)
		values[0] = 99.0
		self.assertEqual(a, (1.0, 2.0, 3.0))

	def test_direction_cosines(self):
		row, column = parse_image_orientation('1\\0\\0\\0\\1\\0')
		self.assertIsInstance(row, ImageCoord)
		np.testing.assert_array_equal(np.cross(row, column), [0.0, 0.0, 1.0])
		self.assertEqual(float(np.dot(np.cross(row, column), ImageCoord(1.0, 2.0, 3.0))), 3.0)

	def test_comparison_with_arrays_is_element_wise(self):
		p, values = ImageCoord(1.0, 2.0, 3.0), np.array([1.0, 2.0, 4.0])
		np.testing.assert_array_equal(p == values, [True, True, False])
		np.testing.assert_array_equal(values == p, [True, True, False])
		with self.assertRaises(ValueError):
			bool(p == values)

	def test_truth_and_text(self):
		self.assertTrue(ImageCoord(0, 0, 0))
		self.assertEqual(repr(ImageCoord(1.0, 2.0, 3.0)), 'ImageCoord(x=1.0, y=2.0, z=3.0)')
		self.assertEqual(str(ImageCoord(1.0, 2.0, 3.0)), '(x=1.0,y=2.0,z=3.0)')

	def test_pickle(self):
		p = ImageCoord(1.0, 2.0, 3.0)
		self.assertEqual(pickle.loads(pickle.dumps(p)), p)

	def test_not_json(self):
		with self.assertRaises(TypeError):
			json.dumps(ImageCoord(1.0, 2.0, 3.0))


class PointArithmeticTestCase(unittest.TestCase):
	'''	A numpy operand gives the numpy result in either order; any other operand raises `TypeError`
	'''
	def cases(self):
		return ((PixelCoord(1.0, 2.0), np.array([3.0, 4.0])), (ImageCoord(1.0, 2.0, 3.0), np.array([1.0, 1.0, 2.0])))

	def test_array_operands(self):
		for p, values in self.cases():
			base = np.asarray(tuple(p))
			for result, expected in ((p + values, base + values), (values + p, values + base), (p - values, base - values),
					(values - p, values - base), (p * values, base * values), (values * p, values * base),
					(np.add(p, values), base + values), (np.subtract(p, values), base - values)):
				self.assertIsInstance(result, np.ndarray)
				np.testing.assert_array_equal(result, expected)

	def test_numpy_scalar_operands(self):
		for p, _ in self.cases():
			base = np.asarray(tuple(p))
			np.testing.assert_array_equal(p * np.float64(2.0), base * 2.0)
			np.testing.assert_array_equal(np.float64(2.0) * p, base * 2.0)

	def test_augmented_assignment_gives_the_array(self):
		for p, values in self.cases():
			q = p
			q += values
			self.assertIsInstance(q, np.ndarray)
			self.assertEqual(tuple(p), tuple(p))

	def test_refused_operands(self):
		for p, _ in self.cases():
			same = (1.0,) * len(p)
			for operation in (lambda: p + p, lambda: p - p, lambda: p * p, lambda: p + same, lambda: same + p, lambda: p - same,
					lambda: same - p, lambda: p * 2, lambda: 2 * p, lambda: p * 2.0, lambda: p + 1, lambda: 1 - p,
					lambda: p + list(same), lambda: list(same) + p, lambda: sum([p, p])):
				with self.assertRaises(TypeError):
					operation()

	def test_message_points_to_numpy(self):
		with self.assertRaisesRegex(TypeError, 'numpy'):
			PixelCoord(1.0, 2.0) + (1.0, 1.0)


class GeometryPointsTestCase(unittest.TestCase):
	def test_points_become_point_classes(self):
		for points in (((1.0, 2.0), (3.0, 4.0)), [[1, 2], [3, 4]], np.array([[1.0, 2.0], [3.0, 4.0]]),
				(PixelCoord(1.0, 2.0), PixelCoord(3.0, 4.0))):
			geometry = Geometry(DCMSR_GEOMETRY_DOMAIN_IMAGE, DCMSR_GRAPHIC_POLYLINE, points, synthetic_reference())
			self.assertTrue(all(type(p) is PixelCoord for p in geometry.points))
			self.assertEqual(geometry.points, ((1.0, 2.0), (3.0, 4.0)))
		for points in (((1.0, 2.0, 3.0),), [[1, 2, 3]], np.array([[1.0, 2.0, 3.0]]), (ImageCoord(1.0, 2.0, 3.0),)):
			geometry = Geometry(DCMSR_GEOMETRY_DOMAIN_PATIENT, DCMSR_GRAPHIC_POLYLINE, points)
			self.assertTrue(all(type(p) is ImageCoord for p in geometry.points))
			self.assertEqual(geometry.points, ((1.0, 2.0, 3.0),))
			self.assertIsInstance(geometry.points[0].x, float)

	def test_input_sr_attributes_are_not_kept(self):
		frame = ImageTransformMatrix(np.identity(4), uid='2.25.1')
		point = ImageCoord(1.0, 2.0, 3.0, reference_frame=frame, name=codes.DCM.Center, point_type='POINT')
		geometry = Geometry(DCMSR_GEOMETRY_DOMAIN_PATIENT, DCMSR_GRAPHIC_POLYLINE, [point])
		self.assertIsNone(geometry.points[0].reference_frame)
		self.assertIsNone(geometry.points[0].name)
		self.assertIsNone(geometry.points[0].type)
		self.assertIsNone(geometry.reference)
		image = Geometry(DCMSR_GEOMETRY_DOMAIN_IMAGE, DCMSR_GRAPHIC_POLYLINE, [PixelCoord(1.0, 2.0)], synthetic_reference())
		self.assertEqual(image.reference, synthetic_reference())

	def test_checks_unchanged(self):
		with self.assertRaises(ValueError):
			Geometry(DCMSR_GEOMETRY_DOMAIN_IMAGE, DCMSR_GRAPHIC_POLYLINE, [ImageCoord(1.0, 2.0, 3.0)])
		with self.assertRaises(ValueError):
			Geometry(DCMSR_GEOMETRY_DOMAIN_PATIENT, DCMSR_GRAPHIC_POLYLINE, [PixelCoord(1.0, 2.0)])
		with self.assertRaises(ValueError):
			Geometry(DCMSR_GEOMETRY_DOMAIN_PATIENT, DCMSR_GRAPHIC_POLYLINE, [(1.0, float('nan'), 3.0)])

	def test_array(self):
		geometry = Geometry(DCMSR_GEOMETRY_DOMAIN_PATIENT, DCMSR_GRAPHIC_POLYLINE, [(1.0, 2.0, 3.0), (4.0, 5.0, 6.0)])
		values = geometry.array
		self.assertEqual(values.shape, (2, 3))
		self.assertEqual(values.dtype, np.float64)
		self.assertIsNot(values, geometry.array)
		values[0, 0] = 99.0
		self.assertEqual(geometry.points[0], (1.0, 2.0, 3.0))
		self.assertEqual(geometry.array[0, 0], 1.0)

	def test_empty_array(self):
		self.assertEqual(Geometry(DCMSR_GEOMETRY_DOMAIN_IMAGE, DCMSR_GRAPHIC_POLYLINE, ()).array.shape, (0, 2))
		self.assertEqual(Geometry(DCMSR_GEOMETRY_DOMAIN_PATIENT, DCMSR_GRAPHIC_POLYLINE, ()).array.shape, (0, 3))

	def test_replace_and_with_points_normalise(self):
		geometry = Geometry(DCMSR_GEOMETRY_DOMAIN_IMAGE, DCMSR_GRAPHIC_POLYLINE, [(1.0, 2.0)], synthetic_reference())
		for other in (geometry._replace(points=[[5, 6], [7, 8]]), geometry.with_points([[5, 6], [7, 8]])):
			self.assertEqual(other.points, ((5.0, 6.0), (7.0, 8.0)))
			self.assertIsInstance(other.points, tuple)
			self.assertTrue(all(type(p) is PixelCoord for p in other.points))
			self.assertEqual(other.reference, geometry.reference)
		with self.assertRaises(ValueError):
			geometry._replace(points=[(1.0, 2.0, 3.0)])


def image_measurements():
	'''	One measurement of each class in the image domain, on the synthetic calibrated frame
	'''
	common = dict(calibration=CALIBRATION, reference=synthetic_reference(), tracking_uid=TRACKING)
	return (LengthMeasurement([(10.0, 10.0), (14.0, 13.0)], **common), AngleMeasurement((20.0, 10.0), (10.0, 10.0), (10.0, 20.0), **common),
		CobbAngleMeasurement(((0.0, 0.0), (10.0, 0.0)), ((0.0, 10.0), (10.0, 20.0)), **common),
		CircleMeasurement.from_points((10.0, 20.0), (20.0, 10.0), (30.0, 20.0), **common),
		BidirectionalMeasurement.from_axes(((20.0, 10.0), (20.0, 20.0)), ((15.0, 15.0), (25.0, 15.0)), **common))


def patient_measurements():
	'''	One measurement of each class in the patient domain
	'''
	common = dict(frame_of_reference_uid=FOR_UID, tracking_uid=TRACKING)
	return (LengthMeasurement([(0.0, 0.0, 0.0), (3.0, 4.0, 0.0)], drawing_plane=((1.0, 0.0, 0.0), (0.0, 1.0, 0.0)), **common),
		AngleMeasurement((10.0, 0.0, 0.0), (0.0, 0.0, 0.0), (0.0, 10.0, 0.0), **common),
		CobbAngleMeasurement(((0.0, 0.0, 0.0), (10.0, 0.0, 0.0)), ((0.0, 10.0, 0.0), (10.0, 20.0, 0.0)), **common),
		CircleMeasurement.from_points((0.0, 5.0, 2.0), (5.0, 0.0, 2.0), (0.0, -5.0, 2.0), **common),
		BidirectionalMeasurement.from_axes(((-10.0, 20.0, 30.0), (10.0, 20.0, 30.0)), ((0.0, 15.0, 30.0), (0.0, 25.0, 30.0)), **common))


def positions(measurement):
	'''	Every public position of a measurement, by property name
	'''
	found = {'points': measurement.points, 'geometry.points': measurement.geometry.points}
	if isinstance(measurement, LengthMeasurement):
		found.update(p1=(measurement.p1,), p2=(measurement.p2,))
	elif isinstance(measurement, AngleMeasurement):
		found.update(p1=(measurement.p1,), vertex=(measurement.vertex,), p3=(measurement.p3,))
	elif isinstance(measurement, CobbAngleMeasurement):
		found.update(line1=measurement.line1, line2=measurement.line2)
	elif isinstance(measurement, CircleMeasurement):
		found.update(center=(measurement.center,), edge=(measurement.edge,), construction_points=measurement.construction_points,
			construction_points_raw=measurement.construction_points_raw)
	elif isinstance(measurement, BidirectionalMeasurement):
		found.update(long_axis=measurement.long_axis, short_axis=measurement.short_axis)
	return found


class MeasurementPointsTestCase(unittest.TestCase):
	def assert_positions(self, measurement, point_class):
		for name, points in positions(measurement).items():
			self.assertTrue(points, name)
			for point in points:
				self.assertIs(type(point), point_class, '%s of %s' % (name, type(measurement).__name__))

	def test_image_domain_positions(self):
		for measurement in image_measurements():
			self.assert_positions(measurement, PixelCoord)

	def test_patient_domain_positions(self):
		for measurement in patient_measurements():
			self.assert_positions(measurement, ImageCoord)

	def test_parsed_positions(self):
		for measurements, point_class in ((image_measurements(), PixelCoord), (patient_measurements(), ImageCoord)):
			for source in measurements:
				parsed = measurements_of(reparse(written([source])))[0]
				self.assert_positions(parsed, point_class)
				self.assertEqual(parsed.points, source.points)
				self.assertFalse(parsed.points_changed)

	def test_values_unchanged(self):
		length = image_measurements()[0]
		self.assertEqual(length.points, ((10.0, 10.0), (14.0, 13.0)))
		self.assertEqual(length.p1, (10.0, 10.0))
		bidirectional = patient_measurements()[4]
		self.assertEqual(bidirectional.long_axis, ((-10.0, 20.0, 30.0), (10.0, 20.0, 30.0)))
		self.assertEqual(bidirectional.short_axis, ((0.0, 15.0, 30.0), (0.0, 25.0, 30.0)))
		cobb = patient_measurements()[2]
		self.assertEqual(cobb.line2, ((0.0, 10.0, 0.0), (10.0, 20.0, 0.0)))

	def test_directions(self):
		circle = patient_measurements()[3]
		for name in ('normal', 'u', 'plane_normal'):
			value = getattr(circle, name)
			self.assertIs(type(value), ImageCoord, name)
			self.assertAlmostEqual(float(np.linalg.norm(value)), 1.0)
		self.assertTrue(np.allclose(np.abs(circle.normal), (0.0, 0.0, 1.0)))
		self.assertIsNone(image_measurements()[3].normal)
		self.assertIsNone(image_measurements()[3].u)
		length = patient_measurements()[0]
		self.assertEqual(len(length.drawing_plane), 2)
		self.assertTrue(all(type(v) is ImageCoord for v in length.drawing_plane))
		self.assertEqual(length.drawing_plane, ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0)))

	def test_parsed_drawing_plane(self):
		parsed = measurements_of(reparse(written([patient_measurements()[0]])))[0]
		self.assertTrue(all(type(v) is ImageCoord for v in parsed.drawing_plane))
		self.assertEqual(parsed.drawing_plane, ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0)))

	def test_legacy_normal(self):
		circle = CircleMeasurement(geometry=Geometry(DCMSR_GEOMETRY_DOMAIN_PATIENT, 'CIRCLE', [(0.0, 0.0, 0.0), (5.0, 0.0, 0.0)]),
			legacy_normal=(0.0, 0.0, 2.0), frame_of_reference_uid=FOR_UID)
		self.assertIs(type(circle.normal), ImageCoord)
		self.assertEqual(circle.normal, (0.0, 0.0, 1.0))
		self.assertIs(type(circle.u), ImageCoord)

	def test_arrays(self):
		for measurement in image_measurements() + patient_measurements():
			dimension = 2 if measurement.geometry.is_image else 3
			values = np.asarray(measurement.points)
			self.assertEqual(values.shape, (len(measurement.points), dimension))
			self.assertEqual(values.dtype, np.float64)
			np.testing.assert_array_equal(measurement.array, values)
			self.assertIsNot(measurement.array, measurement.array)
			before = measurement.points
			measurement.array[0, 0] = 99.0
			self.assertEqual(measurement.points, before)

	def test_returned_points_cannot_be_changed_in_place(self):
		length = patient_measurements()[0]
		with self.assertRaises(AttributeError):
			length.p1.x = 1.0
		with self.assertRaises(AttributeError):
			image_measurements()[0].p1.column = 1.0

	def test_setters_take_every_point_kind(self):
		length = image_measurements()[0]
		for point in ((11.0, 12.0), [11, 12], np.array([11.0, 12.0]), PixelCoord(11.0, 12.0)):
			length.p1 = point
			self.assertEqual(length.p1, (11.0, 12.0))
			self.assertIs(type(length.p1), PixelCoord)
		bidirectional = patient_measurements()[4]
		bidirectional.long_axis = np.array([[-12.0, 20.0, 30.0], [12.0, 20.0, 30.0]])
		self.assertEqual(bidirectional.long_axis, ((-12.0, 20.0, 30.0), (12.0, 20.0, 30.0)))
		self.assertAlmostEqual(bidirectional.length, 24.0)
		bidirectional.short_axis = (ImageCoord(0.0, 14.0, 30.0), ImageCoord(0.0, 26.0, 30.0))
		self.assertAlmostEqual(bidirectional.width, 12.0)
		cobb = patient_measurements()[2]
		cobb.line1 = [ImageCoord(0.0, 0.0, 0.0), np.array([20.0, 0.0, 0.0])]
		self.assertEqual(cobb.line1, ((0.0, 0.0, 0.0), (20.0, 0.0, 0.0)))
		circle = patient_measurements()[3]
		circle.center = ImageCoord(1.0, 1.0, 2.0)
		self.assertTrue(np.allclose(circle.center, (1.0, 1.0, 2.0)))
		self.assertIs(type(circle.center), ImageCoord)
		circle.normal = ImageCoord(0.0, 0.0, -1.0)
		self.assertIs(type(circle.normal), ImageCoord)

	def test_change_tracking(self):
		for source in image_measurements() + patient_measurements():
			parsed = measurements_of(reparse(written([source])))[0]
			self.assertTrue(parsed.keeps_stored_value)
			parsed.points = [tuple(p) for p in parsed.points]
			self.assertFalse(parsed.points_changed)
			self.assertTrue(parsed.keeps_stored_value)
		parsed = measurements_of(reparse(written([patient_measurements()[4]])))[0]
		parsed.short_axis = np.asarray(parsed.short_axis)
		self.assertFalse(parsed.axis_changed('short_axis'))
		self.assertFalse(parsed.axis_changed('long_axis'))
		self.assertTrue(parsed.keeps_axis_value('short_axis'))


class PointInputDocumentTestCase(OracleMixin, unittest.TestCase):
	'''	Documents written from tuples, lists, numpy arrays and the point classes are equivalent under the
		content-tree oracle
	'''
	def assert_equivalent(self, build, kinds):
		reference = written([build(kinds[0])])
		for kind in kinds[1:]:
			self.assert_same_content(reference, written([build(kind)]))

	def test_length(self):
		image = dict(calibration=CALIBRATION, reference=synthetic_reference(), tracking_uid=TRACKING)
		self.assert_equivalent(lambda points: LengthMeasurement(points, **image),
			(((10.0, 10.0), (14.0, 13.0)), [[10, 10], [14, 13]], np.array([[10.0, 10.0], [14.0, 13.0]]),
			[PixelCoord(10.0, 10.0), PixelCoord(14.0, 13.0)]))
		patient = dict(frame_of_reference_uid=FOR_UID, tracking_uid=TRACKING)
		self.assert_equivalent(lambda points: LengthMeasurement(points, drawing_plane=SAGITTAL, **patient),
			(((0.0, 0.0, 0.0), (0.0, 3.0, -4.0)), [[0, 0, 0], [0, 3, -4]], np.array([[0.0, 0.0, 0.0], [0.0, 3.0, -4.0]]),
			[ImageCoord(0.0, 0.0, 0.0), ImageCoord(0.0, 3.0, -4.0)]))

	def test_drawing_plane(self):
		patient = dict(frame_of_reference_uid=FOR_UID, tracking_uid=TRACKING)
		self.assert_equivalent(lambda plane: LengthMeasurement([(0.0, 0.0, 0.0), (0.0, 3.0, -4.0)], drawing_plane=plane, **patient),
			(SAGITTAL, [list(v) for v in SAGITTAL], np.array(SAGITTAL), tuple(ImageCoord(*v) for v in SAGITTAL), (0.0, 1.0, 0.0, 0.0, 0.0, -1.0)))

	def test_angle_and_cobb(self):
		patient = dict(frame_of_reference_uid=FOR_UID, tracking_uid=TRACKING)
		self.assert_equivalent(lambda handles: AngleMeasurement(*handles, **patient),
			(((10.0, 0.0, 0.0), (0.0, 0.0, 0.0), (0.0, 10.0, 0.0)), ([10, 0, 0], [0, 0, 0], [0, 10, 0]),
			tuple(np.array([[10.0, 0.0, 0.0], [0.0, 0.0, 0.0], [0.0, 10.0, 0.0]])),
			(ImageCoord(10.0, 0.0, 0.0), ImageCoord(0.0, 0.0, 0.0), ImageCoord(0.0, 10.0, 0.0))))
		self.assert_equivalent(lambda lines: CobbAngleMeasurement(*lines, **patient),
			((((0.0, 0.0, 0.0), (10.0, 0.0, 0.0)), ((0.0, 10.0, 0.0), (10.0, 20.0, 0.0))),
			(np.array([[0.0, 0.0, 0.0], [10.0, 0.0, 0.0]]), np.array([[0.0, 10.0, 0.0], [10.0, 20.0, 0.0]])),
			((ImageCoord(0.0, 0.0, 0.0), ImageCoord(10.0, 0.0, 0.0)), [ImageCoord(0.0, 10.0, 0.0), ImageCoord(10.0, 20.0, 0.0)])))

	def test_circle(self):
		patient = dict(frame_of_reference_uid=FOR_UID, tracking_uid=TRACKING)
		self.assert_equivalent(lambda args: CircleMeasurement.from_center_radius(args[0], 5.0, normal=args[1], **patient),
			(((10.0, 20.0, 30.0), (0.0, 0.0, 1.0)), ([10, 20, 30], [0, 0, 1]), (np.array([10.0, 20.0, 30.0]), np.array([0.0, 0.0, 1.0])),
			(ImageCoord(10.0, 20.0, 30.0), ImageCoord(0.0, 0.0, 1.0))))
		image = dict(calibration=CALIBRATION, reference=synthetic_reference(), tracking_uid=TRACKING)
		self.assert_equivalent(lambda points: CircleMeasurement.from_points(*points, **image),
			(((10.0, 20.0), (20.0, 10.0), (30.0, 20.0)), ([10, 20], [20, 10], [30, 20]), tuple(np.array([[10.0, 20.0], [20.0, 10.0], [30.0, 20.0]])),
			(PixelCoord(10.0, 20.0), PixelCoord(20.0, 10.0), PixelCoord(30.0, 20.0))))

	def test_bidirectional(self):
		patient = dict(frame_of_reference_uid=FOR_UID, tracking_uid=TRACKING)
		long_axis, short_axis = ((-10.0, 20.0, 30.0), (10.0, 20.0, 30.0)), ((0.0, 15.0, 30.0), (0.0, 25.0, 30.0))
		self.assert_equivalent(lambda axes: BidirectionalMeasurement.from_axes(*axes, **patient),
			((long_axis, short_axis), ([list(p) for p in long_axis], [list(p) for p in short_axis]), (np.array(long_axis), np.array(short_axis)),
			(tuple(ImageCoord(*p) for p in long_axis), tuple(ImageCoord(*p) for p in short_axis))))

	def test_transform_inputs(self):
		transform = ImageTransformMatrix(np.identity(4), source_frame='planning', target_frame=FOR_UID)
		patient = dict(transform=transform, tracking_uid=TRACKING)
		self.assert_equivalent(lambda points: LengthMeasurement(points, **patient),
			(((0.0, 0.0, 0.0), (3.0, 4.0, 0.0)), np.array([[0.0, 0.0, 0.0], [3.0, 4.0, 0.0]]), [ImageCoord(0.0, 0.0, 0.0), ImageCoord(3.0, 4.0, 0.0)]))


ROTATION_Z = np.array([[0.0, -1.0, 0.0, 10.0], [1.0, 0.0, 0.0, 20.0], [0.0, 0.0, 1.0, 30.0], [0.0, 0.0, 0.0, 1.0]])


class TransformPointsTestCase(unittest.TestCase):
	def test_convert_and_invert_millimetres(self):
		transform = ImageTransformMatrix(ROTATION_Z, source_frame='planning', target_frame=FOR_UID)
		converted = transform.convert([ImageCoord(1.0, 0.0, 0.0)])
		self.assertIs(type(converted[0]), ImageCoord)
		self.assertTrue(np.allclose(converted[0], (10.0, 21.0, 30.0)))
		back = transform.invert(converted)
		self.assertIs(type(back[0]), ImageCoord)
		self.assertTrue(np.allclose(back[0], (1.0, 0.0, 0.0)))

	def test_metre_round_trip(self):
		transform = ImageTransformMatrix(ROTATION_Z, source_frame='planning', target_frame=FOR_UID, source_unit='m')
		converted = transform.convert([(0.001, 0.0, 0.0)])
		self.assertTrue(np.allclose(converted[0], (10.0, 21.0, 30.0)))
		back = transform.invert(converted)
		self.assertTrue(np.allclose(back[0], (0.001, 0.0, 0.0)))

	def test_directions_rotate_only(self):
		transform = ImageTransformMatrix(ROTATION_Z, source_frame='planning', target_frame=FOR_UID, source_unit='m')
		rotated = transform.rotate_directions([ImageCoord(1.0, 0.0, 0.0), (0.0, 0.0, 1.0)])
		self.assertTrue(all(type(d) is ImageCoord for d in rotated))
		self.assertTrue(np.allclose(rotated[0], (0.0, 1.0, 0.0)))
		self.assertTrue(np.allclose(rotated[1], (0.0, 0.0, 1.0)))

	def test_other_shapes_refused(self):
		transform = ImageTransformMatrix(ROTATION_Z, source_frame='planning', target_frame=FOR_UID)
		for points in ([(1.0, 2.0)], [1.0, 2.0, 3.0], [], [(1.0, 2.0), (3.0, 4.0), (5.0, 6.0)]):
			with self.assertRaises(ValueError):
				transform.convert(points)
			with self.assertRaises(ValueError):
				transform.invert(points)

	def test_patient_point(self):
		point = CALIBRATION.patient_point(10.5, 2.5)
		self.assertIs(type(point), ImageCoord)
		self.assertEqual(point, (5.0, 4.0, 0.0))


class PointArrayTestCase(unittest.TestCase):
	def test_points2array_shapes(self):
		self.assertEqual(points2array(ImageCoord(1.0, 2.0, 3.0)).shape, (3,))
		self.assertEqual(points2array((1, 2, 3)).shape, (3,))
		self.assertEqual(points2array(PixelCoord(1.0, 2.0)).shape, (2,))
		self.assertEqual(points2array([1.0, 2.0]).shape, (2,))
		self.assertEqual(points2array([ImageCoord(1.0, 2.0, 3.0), (4.0, 5.0, 6.0)]).shape, (2, 3))
		self.assertEqual(points2array([PixelCoord(1.0, 2.0), [3.0, 4.0], np.array([5.0, 6.0])]).shape, (3, 2))
		self.assertEqual(points2array(ImageCoordCollection([ImageCoord(1.0, 2.0, 3.0), ImageCoord(4.0, 5.0, 6.0)])).shape, (2, 3))
		self.assertEqual(points2array((1, 2, 3)).dtype, np.float64)
		values = np.zeros((4, 3))
		self.assertIs(points2array(values), values)
		# A numpy array passes through unchecked, with its own dtype and shape
		for values in (np.arange(6).reshape(3, 2), np.zeros(0), np.zeros((2, 5))):
			self.assertIs(points2array(values), values)

	def test_points2array_refusals(self):
		for pts in ([], (), [(1.0, 2.0), (1.0, 2.0, 3.0)], [(1.0, 2.0, 3.0, 4.0)], (1.0, 2.0, 3.0, 4.0), (1.0,),
				[(1.0, 2.0, 3.0), 4.0], [('a', 'b', 'c')], 'abc', None, {1: 2}):
			with self.assertRaises(ValueError, msg=repr(pts)):
				points2array(pts)

	def test_srencode_coord3d(self):
		item = srencode_coord3d(FOR_UID, [ImageCoord(1.0, 2.0, 3.0), ImageCoord(4.0, 5.0, 6.0)])
		self.assertEqual([float(v) for v in item.GraphicData], [1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
		self.assertEqual(str(item.GraphicType), 'MULTIPOINT')
		single = srencode_coord3d(FOR_UID, ImageCoord(1.0, 2.0, 3.0))
		self.assertEqual(str(single.GraphicType), 'POINT')
		for pts in (PixelCoord(1.0, 2.0), [PixelCoord(1.0, 2.0), PixelCoord(3.0, 4.0)], np.zeros((2, 2))):
			with self.assertRaisesRegex(ValueError, '3 values per point'):
				srencode_coord3d(FOR_UID, pts)

	def test_drawing_plane_vectors(self):
		for plane in (SAGITTAL, tuple(ImageCoord(*v) for v in SAGITTAL), np.array(SAGITTAL), (0, 1, 0, 0, 0, -1)):
			width, height = drawing_plane_vectors(plane)
			self.assertIs(type(width), ImageCoord)
			self.assertEqual((width, height), SAGITTAL)

	def test_collection_array(self):
		collection = ImageCoordCollection([ImageCoord(1.0, 2.0, 3.0), ImageCoord(4.0, 5.0, 6.0)])
		values = collection.array
		self.assertEqual(values.shape, (2, 3))
		self.assertIsNot(values, collection.array)
		values[0, 0] = 99.0
		self.assertEqual(collection.array[0, 0], 1.0)
		self.assertEqual(collection[0], (1.0, 2.0, 3.0))
		del collection[0]
		np.testing.assert_array_equal(collection.array, [[4.0, 5.0, 6.0]])
		del collection[0]
		self.assertEqual(collection.array.shape, (0, 3))

	def test_scoord_points_legacy_path(self):
		item = ScoordContentItem(name=codes.DCM.Center, graphic_type=GraphicTypeValues.POLYLINE, graphic_data=np.array([[1.0, 2.0], [3.0, 4.0]]))
		points = sr2points(item)
		self.assertIsInstance(points, ImageCoordCollection)
		self.assertEqual([tuple(p) for p in points], [(1.0, 2.0, 0.0), (3.0, 4.0, 0.0)])
		self.assertTrue(all(type(p) is ImageCoord for p in points))
