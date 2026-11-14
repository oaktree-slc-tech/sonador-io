'''	Offline tests of the reference frames of a DICOM-SR document (oak-tree/medical-imaging/sonador-client#45):
	named frames and rigid edges, conversion and target-relative
	representability, the wire format, the two-phase resolution with its classification rows, the quarantine and
	path rules, the legacy rule, the re-save invariant, preservation, the writer policy and the accessors.

	The expectations are independent of the code under test: the composition oracle is computed by hand, the
	classification rows and the Viewer's note states for held measurements are those the specification tables
	state (`CASE_ORACLE`, `PANEL_ORACLE`).

	Fixtures: `ftests/test_sr/fixtures/frames`, synthetic study only, written by `frames_fixtures` (the generator
	names the UID, container, declaration or matrix each variant changes) together with the shared case table
	`sonador-io.frames-cases.2026-1114.json`. `FramesFixtureTestCase` regenerates them and compares byte for byte.

	Run from the directory containing the `sonador` package: python -m unittest sonador.ftests.test_sr.sr_frames
'''
import copy, io, json, logging, os, tempfile, unittest
from collections import OrderedDict

import numpy as np
import pydicom
from pydicom.dataset import Dataset
from pydicom.sequence import Sequence

from ...apisettings.base import ImageTransformMatrix, ReferenceFrame, FrameGraph, FrameConversionError, LengthMeasurement, \
	AngleMeasurement, ArrowAnnotation, ImageCoord, ImageCoordCollection, VolumePointCollectionGroup, frame_unit, \
	DCMSR_FRAME_NATIVE, DCMSR_FRAME_REGISTERED, DCMSR_FRAME_UNREGISTERED, DCMSR_FRAME_NOT_APPLICABLE
from ...apisettings.sr import SRDiagnosticError, sr_diagnostics_with, srcontent_is_named, dcm_code_value, \
	SR_CONDITION_FRAME_UID_COLLISION, SR_CONDITION_FRAME_EDGE_INVALID, SR_CONDITION_FRAME_EDGE_UNVERIFIED, \
	SR_CONDITION_FRAME_NODE_QUARANTINED, SR_CONDITION_FRAME_STATUS_CONFLICT, SR_CONDITION_FRAME_STATUS_DEMOTED, \
	SR_CONDITION_FRAME_EDGE_LEGACY, SR_CONDITION_FRAME_NODE_INVALID, SR_CONDITION_FRAME_NODE_CONFLICT, \
	SR_CONDITION_FRAME_NODE_MENTIONED_ONLY, SR_CONDITION_FRAME_PRIMARY_INVALID, SR_CONDITION_FRAME_PRIMARY_CHANGED, \
	SR_CONDITION_LEGACY_TARGET_CHANGE, SR_CONDITION_UNKNOWN_FRAME, SR_CONDITION_FRAME_POLICY_REFUSED, \
	SR_CONDITION_UNKNOWN_SOURCE_SERIES
from ...apisettings.sr.frames import DCMSR_FRAME_CONTAINER, DCMSR_FRAME_EDGE, DCMSR_FRAME_PRIMARY, DCMSR_FRAME_LEGACY, \
	DCMSR_FRAME_UNVERIFIED, DCMSR_FRAME_PARENT, DCMSR_FRAMES_MIN_VERSION
from ...imaging.helpers.sr import SonadorComprehensiveSR3D, SonadorImagingMeasurementReport
from . import frames_fixtures as F


__all__ = ['FramesCompositionOracleTestCase', 'FramesConstructorPathTestCase', 'FramesConventionTestCase', 'FramesUnitTestCase', 'FramesRepresentabilityTestCase', 'FramesImagingFrameTestCase',
	'FramesPrimaryTestCase', 'FramesWriterPolicyTestCase', 'FramesRoundTripTestCase', 'FramesTrustAndDamageTestCase', 'FramesResaveTestCase',
	'FramesPreservationTestCase', 'FramesEditTestCase', 'FramesAccessorTestCase', 'FramesPanelOracleTestCase', 'FramesCaseTableTestCase', 'FramesFixtureTestCase']


P, S = F.P_FOR, F.S_FOR
A2, SIB, R3, R3B, T, X, Y, G6, U5 = (F.FRAMES[a] for a in ('A2', 'SIB', 'R3', 'R3B', 'T', 'X', 'Y', 'G6', 'U5'))
REFS = F.references_map(('P', 'S'))
P_ONLY = F.references_map(('P',))

# Item 1: two edges, written down independently of the code under test
EDGE_1 = F.RZ		# +90 degrees about Z, then (10, 0, 0) mm
EDGE_2 = F.RX		# +90 degrees about X, then (0, 20, 0) mm
SOURCE_M = (0.001, 0.002, 0.003)
EXPECTED_MM = (8.0, 17.0, 1.0)


def setUpModule():
	logging.getLogger('sonador').setLevel(logging.CRITICAL)


def tearDownModule():
	logging.getLogger('sonador').setLevel(logging.NOTSET)


_BUILT = {}


def built():
	'''	Every fixture scenario, built once per run.
	'''
	if not _BUILT:
		_BUILT.update(F.build_all())
	return _BUILT


def dataset(name):
	return copy.deepcopy(built()[name].dataset)


def read(name, **kwargs):
	scenario = built()[name]
	kwargs.setdefault('primary_frame_uid', scenario.caller_primary)
	return F.read_report(scenario.dataset, scenario.referenced, scenario.root, **kwargs)


def new_report(referenced=('P', 'S'), cls=SonadorImagingMeasurementReport, **kwargs):
	kwargs.setdefault('primary_frame_uid', P)
	kwargs.setdefault('imaging_frames', F.references_map(referenced))
	return cls(report_procedure=F.codes.LN.CTUnspecifiedBodyRegion, dcm_evidence=[F.evidence_dataset(a) for a in referenced],
		person_observer=F.PERSON_OBSERVER, **kwargs)


def chained(report):
	'''	The item 1 chain on a report: child B (unit m) -> A -> P, plus a parentless root R.
	'''
	graph = report.frames
	a = graph.add_frame(ReferenceFrame('2.25.46001', name='Anatomic'))
	b = graph.add_frame(ReferenceFrame('2.25.46002', name='Planned', unit='m'))
	root = graph.add_frame(ReferenceFrame('2.25.46003', name='Device'))
	graph.add_edge(EDGE_2, a, P)
	graph.add_edge(EDGE_1, b, a)
	return a, b, root


def lengths(report):
	return [m for g in (report.groups or []) for m in (g.measurements or [])]


def by_uid(report, tracking_uid):
	return next(m for m in lengths(report) + list(report.measurements or []) if m.tracking_uid == tracking_uid)


def disk(doc):
	'''	Write a document to bytes with dcmwrite and read it back with dcmread
	'''
	buf = io.BytesIO()
	pydicom.dcmwrite(buf, doc)
	buf.seek(0)
	return pydicom.dcmread(buf)


def conditions(report, condition):
	return sr_diagnostics_with(report.diagnostics, condition=condition)


def declarations(ds):
	found = OrderedDict((('primary', []), ('legacy', []), ('unverified', [])))
	for item in ds.ContentSequence:
		for kind, code in (('primary', DCMSR_FRAME_PRIMARY), ('legacy', DCMSR_FRAME_LEGACY), ('unverified', DCMSR_FRAME_UNVERIFIED)):
			if srcontent_is_named(item, code):
				found[kind].append(str(item.UID))
	return found


def frame_subtrees(ds):
	'''	The Registration Sequence, the declarations and the frame containers of a document, for direct comparison
	'''
	root = [item for item in ds.ContentSequence if srcontent_is_named(item, DCMSR_FRAME_CONTAINER)
		or any(srcontent_is_named(item, c) for c in (DCMSR_FRAME_PRIMARY, DCMSR_FRAME_LEGACY, DCMSR_FRAME_UNVERIFIED))]
	return (list(ds.get('RegistrationSequence') or []), root)


def _item_bytes(item):
	'''	Explicit VR little endian encoding of one item: tags, VRs, values and nesting
	'''
	from pydicom.filewriter import write_dataset
	from pydicom.filebase import DicomBytesIO
	out = DicomBytesIO()
	out.is_little_endian, out.is_implicit_VR = True, False
	write_dataset(out, item)
	return out.getvalue()


def item_bytes(ds, uid):
	return [_item_bytes(i) for i in F.items_of(ds, uid)]


# ---- item 1 ----


class FramesCompositionOracleTestCase(unittest.TestCase):
	def test_metre_point_through_two_edges(self):
		report = new_report()
		a, b, _ = chained(report)
		source = b.to_mm([SOURCE_M])
		self.assertTrue(np.allclose(source[0], (1.0, 2.0, 3.0), atol=1e-6, rtol=0))
		converted = report.frames.convert(source, b, P)[0]
		for got, expected in zip(converted, EXPECTED_MM):
			self.assertTrue(abs(got - expected) <= 1e-6, (converted, EXPECTED_MM))

	def test_direction_rotates_without_scale(self):
		report = new_report()
		_, b, _ = chained(report)
		direction = report.frames.rotate([(1.0, 0.0, 0.0)], b, P)[0]
		self.assertTrue(np.allclose(direction, (0.0, 0.0, 1.0), atol=1e-6, rtol=0))

	def test_inverse_recovers_the_source(self):
		report = new_report()
		_, b, _ = chained(report)
		source = b.to_mm([SOURCE_M])
		back = report.frames.convert([EXPECTED_MM], P, b)
		self.assertTrue(np.allclose(back[0], (1.0, 2.0, 3.0), atol=1e-6, rtol=0))
		self.assertTrue(np.allclose(b.from_mm(back)[0], SOURCE_M, atol=1e-9, rtol=0))
		self.assertEqual(tuple(source[0]), (1.0, 2.0, 3.0))

	def test_source_measurement_unchanged_by_conversion(self):
		report = new_report()
		_, b, _ = chained(report)
		length = LengthMeasurement([b.to_mm([SOURCE_M])[0], (4.0, 2.0, 3.0)], frame_of_reference_uid=b.uid, tracking_uid='2.25.46101')
		report.measurements = [length]
		converted = report.convert(length, P)
		self.assertTrue(np.allclose(converted.points[0], EXPECTED_MM, atol=1e-6, rtol=0))
		self.assertEqual(length.frame_of_reference_uid, b.uid)
		self.assertTrue(np.allclose(length.points[0], (1.0, 2.0, 3.0), atol=1e-9, rtol=0))
		self.assertEqual(converted.frame_of_reference_uid, P)
		self.assertEqual(converted.tracking_uid, length.tracking_uid)


# ---- items 2 and 3 ----


class FramesConventionTestCase(unittest.TestCase):
	def test_row_and_column_forms_store_one_matrix(self):
		column = ImageTransformMatrix(EDGE_1)
		row = ImageTransformMatrix(EDGE_1.T, convention='row')
		self.assertTrue(np.allclose(np.asarray(row.matrix), EDGE_1))
		self.assertEqual(column.convert([(1, 2, 3)]), row.convert([(1, 2, 3)]))

	def test_wrong_convention_refused_naming_both_checks(self):
		with self.assertRaises(ValueError) as caught:
			ImageTransformMatrix(EDGE_1.T)
		message = str(caught.exception)
		self.assertIn('frame-convention-ambiguous', message)
		self.assertIn('homogeneous row', message)
		self.assertIn('row convention it passes', message)
		with self.assertRaises(ValueError):
			ImageTransformMatrix(EDGE_1, convention='row')

	def test_non_rigid_in_both_conventions_is_not_ambiguous(self):
		matrix = ImageTransformMatrix(F.NON_RIGID)
		with self.assertRaises(ValueError):
			matrix.convert([(1, 0, 0)])


class FramesUnitTestCase(unittest.TestCase):
	def test_registry(self):
		self.assertEqual(frame_unit('mm').to_mm, 1.0)
		self.assertEqual(frame_unit('m').to_mm, 1000.0)
		with self.assertRaises(ValueError):
			ReferenceFrame(name='Feet', unit='ft')

	def test_metre_translation_converted_once(self):
		matrix = ImageTransformMatrix.from_translation_unit([[1, 0, 0, 0.01], [0, 1, 0, 0], [0, 0, 1, 0.002], [0, 0, 0, 1]], 'm')
		self.assertTrue(np.allclose(np.asarray(matrix.matrix)[:3, 3], (10.0, 0.0, 2.0)))
		self.assertTrue(np.allclose(np.asarray(matrix.matrix)[:3, :3], np.identity(3)))

	def test_translation_never_scaled_by_frame_unit(self):
		report = new_report()
		graph = report.frames
		frame = graph.add_frame(ReferenceFrame('2.25.46011', name='Metric', unit='m'))
		graph.add_edge(F.SHIFT, frame, P)
		self.assertTrue(np.allclose(graph.convert([(0, 0, 0)], frame, P)[0], (5.0, 0.0, -5.0)))

	def test_parsed_points_not_rescaled(self):
		report = new_report()
		_, b, _ = chained(report)
		report.measurements = [LengthMeasurement([(1, 2, 3), (4, 2, 3)], frame_of_reference_uid=b.uid, tracking_uid='2.25.46102')]
		parsed = SonadorImagingMeasurementReport.from_sr(disk(report.create_sr()), imaging_frames=REFS)
		self.assertEqual(by_uid(parsed, '2.25.46102').points, ((1.0, 2.0, 3.0), (4.0, 2.0, 3.0)))
		self.assertEqual(parsed.frames.frame(b.uid).unit.ucum, 'm')


# ---- items 4 to 7 ----


class FramesRepresentabilityTestCase(unittest.TestCase):
	def setUp(self):
		self.report = new_report()
		self.a, self.b, self.root = chained(self.report)

	def test_three_frames_against_the_primary(self):
		graph = self.report.frames
		self.assertEqual(graph.representability(P), DCMSR_FRAME_NATIVE)
		self.assertEqual(graph.representability(self.b), DCMSR_FRAME_REGISTERED)
		self.assertEqual(graph.representability(self.root), DCMSR_FRAME_UNREGISTERED)

	def test_against_a_second_imaging_frame(self):
		graph = self.report.frames
		self.assertEqual(graph.representability(S, target=S), DCMSR_FRAME_NATIVE)
		self.assertEqual(graph.representability(self.b, target=S), DCMSR_FRAME_UNREGISTERED)
		graph.add_edge(F.SHIFT, S, P)
		self.assertEqual(graph.representability(self.b, target=S), DCMSR_FRAME_REGISTERED)

	def test_image_domain_not_applicable(self):
		length = LengthMeasurement([(1, 1), (5, 4)], reference=_image_reference())
		self.assertEqual(self.report.representability(length), DCMSR_FRAME_NOT_APPLICABLE)


class FramesImagingFrameTestCase(unittest.TestCase):
	def test_two_referenced_series_with_their_own_frames(self):
		report = new_report()
		graph = report.frames
		self.assertEqual([f.uid for f in graph.imaging_frames], [P, S])
		self.assertEqual(graph.primary_uid, P)
		doc = disk(report.create_sr())
		self.assertEqual(declarations(doc)['primary'], [P])

	def test_series_sharing_a_frame_coalesce(self):
		report = new_report(imaging_frames=OrderedDict(((F.SERIES['P']['series_uid'], P), (F.SERIES['S2']['series_uid'], P))))
		self.assertEqual([f.uid for f in report.frames.imaging_frames], [P])
		self.assertEqual(report.frames.imaging_series[P], [F.SERIES['P']['series_uid'], F.SERIES['S2']['series_uid']])

	def test_second_series_length(self):
		report = new_report()
		length = LengthMeasurement([(0, 0, 0), (1, 0, 0)], frame_of_reference_uid=S, tracking_uid='2.25.46103')
		self.assertEqual(report.representability(length, target=S), DCMSR_FRAME_NATIVE)
		self.assertEqual(report.representability(length), DCMSR_FRAME_UNREGISTERED)
		report.frames.add_edge(F.SHIFT, S, P)
		self.assertEqual(report.representability(length), DCMSR_FRAME_REGISTERED)
		self.assertTrue(all(f.uid in (P, S) for f in report.frames))


class FramesPrimaryTestCase(unittest.TestCase):
	def test_caller_primary_persisted_without_extra_lookups(self):
		report = new_report(primary_frame_uid=S)
		doc = disk(report.create_sr())
		self.assertEqual(declarations(doc)['primary'], [S])
		parsed = SonadorImagingMeasurementReport.from_sr(doc, imaging_frames=REFS)
		self.assertEqual((parsed.frames.primary_uid, parsed.frames.persisted_primary_uid), (S, S))

	def test_no_usable_frame_of_reference(self):
		report = SonadorComprehensiveSR3D(report_procedure=F.codes.LN.CTUnspecifiedBodyRegion, dcm_evidence=[F.evidence_dataset('P')],
			person_observer=F.PERSON_OBSERVER)
		graph = report.frames
		root = graph.add_frame(ReferenceFrame('2.25.46021', name='Root'))
		child = graph.add_frame(ReferenceFrame('2.25.46022', name='Child'))
		graph.add_edge(F.SHIFT, child, root)
		report.measurements = [LengthMeasurement([(0, 0, 0), (1, 0, 0)], frame_of_reference_uid=child.uid, tracking_uid='2.25.46104')]
		with self.assertRaises(FrameConversionError) as caught:
			report.frames.representability(child)
		self.assertEqual(caught.exception.reason, 'no target')
		with self.assertRaises(FrameConversionError):
			report.create_sr(require='native')
		doc = disk(report.create_sr())
		self.assertEqual(declarations(doc), OrderedDict((('primary', []), ('legacy', []), ('unverified', []))))
		parsed = SonadorComprehensiveSR3D.from_sr(doc)
		self.assertIsNone(parsed.frames.primary_uid)
		self.assertTrue(parsed.frames.document.marked)
		converted = parsed.convert(by_uid(parsed, '2.25.46104'), root.uid)
		self.assertTrue(np.allclose(converted.points[0], (5.0, 0.0, -5.0)))

	def test_persisted_primary_absent_from_references(self):
		parsed = read('primary-invalid')
		self.assertTrue(conditions(parsed, SR_CONDITION_FRAME_PRIMARY_INVALID))
		self.assertIsNone(parsed.frames.primary_uid)
		single = F.read_report(dataset('legacy-saved-two-primaries'), ('P',), 'exchange')
		self.assertIsNone(single.frames.primary_uid)
		self.assertTrue(conditions(single, SR_CONDITION_FRAME_PRIMARY_INVALID))

	def test_caller_primary_differs_from_persisted(self):
		parsed = read('exc-intact', primary_frame_uid=S)
		self.assertTrue(conditions(parsed, SR_CONDITION_FRAME_PRIMARY_CHANGED))
		self.assertEqual(parsed.frames.primary_uid, S)

	def test_application_uid_collision_refused(self):
		report = new_report()
		with self.assertRaises(SRDiagnosticError) as caught:
			report.frames.add_frame(ReferenceFrame(P, name='Clash'))
		self.assertEqual(caught.exception.diagnostic.condition, SR_CONDITION_FRAME_UID_COLLISION)


class FramesWriterPolicyTestCase(unittest.TestCase):
	def report_with(self, frame):
		report = new_report()
		self.a, self.b, self.root = chained(report)
		frames = {'native': P, 'registered': self.b.uid, 'unregistered': self.root.uid}
		report.measurements = [LengthMeasurement([(0, 0, 0), (1, 0, 0)], frame_of_reference_uid=frames[frame], tracking_uid='2.25.46201'),
			LengthMeasurement([(1, 1), (5, 4)], reference=_image_reference(), tracking_uid='2.25.46202', calibration=SQUARE)]
		return report

	def outcome(self, frame, require, target=None):
		try:
			self.report_with(frame).create_sr(require=require, target=target)
			return 'written'
		except SRDiagnosticError as err:
			self.assertEqual(err.diagnostic.condition, SR_CONDITION_FRAME_POLICY_REFUSED)
			message = str(err)
			self.assertIn('2.25.46201', message)
			return 'refused: ' + message.rsplit(': ', 1)[-1]

	def test_policy_table_against_the_primary(self):
		table = {(None, 'native'): 'written', (None, 'registered'): 'written', (None, 'unregistered'): 'written',
			('registered', 'native'): 'written', ('registered', 'registered'): 'written',
			('registered', 'unregistered'): 'refused: no trusted path',
			('native', 'native'): 'written', ('native', 'registered'): 'refused: conversion required',
			('native', 'unregistered'): 'refused: no trusted path'}
		for (require, frame), expected in table.items():
			self.assertEqual(self.outcome(frame, require), expected, (require, frame))

	def test_policy_against_a_secondary_target(self):
		self.assertEqual(self.outcome('native', 'native', target=S), 'refused: no trusted path')
		self.assertEqual(self.outcome('registered', 'registered', target=S), 'refused: no trusted path')

	def test_refusal_names_frame_and_target(self):
		with self.assertRaises(SRDiagnosticError) as caught:
			self.report_with('registered').create_sr(require='native')
		message = str(caught.exception)
		self.assertIn(self.b.uid, message)
		self.assertIn(P, message)

	def test_convert_then_native(self):
		report = self.report_with('registered')
		report.measurements = [report.convert(report.measurements[0], P), report.measurements[1]]
		self.assertTrue(report.create_sr(require='native'))


from ...apisettings.base import ImageFrameGeometry
SQUARE = ImageFrameGeometry((0.0, 0.0, 0.0), (1.0, 0.0, 0.0, 0.0, 1.0, 0.0), (1.0, 1.0))


def _image_reference():
	from ...apisettings.base import ImageReference
	return ImageReference(F.MR_IMAGE_STORAGE, F.SERIES['P']['sop_uid'], None, F.SERIES['P']['series_uid'])


class FramesConstructorPathTestCase(unittest.TestCase):
	def test_measurement_transform_becomes_an_edge_to_its_target(self):
		report = new_report()
		transform = ImageTransformMatrix(EDGE_1, uid='2.25.46701', source_frame='Planner', target_frame=S)
		length = LengthMeasurement([(1, 2, 3), (2, 2, 3)], transform=transform, tracking_uid='2.25.46702')
		self.assertEqual(length.frame_of_reference_uid, S)
		self.assertTrue(np.allclose(length.points[0], EDGE_1.dot((1, 2, 3, 1))[:3]))
		report.measurements = [length]
		doc = disk(report.create_sr())
		parsed = SonadorImagingMeasurementReport.from_sr(doc, imaging_frames=REFS)
		self.assertEqual(parsed.frames.edges['2.25.46701'].target_uid, S)
		self.assertEqual(parsed.frames.frame('2.25.46701').name, 'Planner')
		self.assertEqual(parsed.frames.primary_uid, P)

	def test_unframed_points_in_the_primary(self):
		report = new_report()
		report.groups = [VolumePointCollectionGroup(points=[ImageCoordCollection([ImageCoord(1, 2, 3), ImageCoord(4, 5, 6)],
			point_type='MULTIPOINT')], tracking_id='landmarks', tracking_uid='2.25.46711')]
		parsed = SonadorImagingMeasurementReport.from_sr(disk(report.create_sr()), imaging_frames=REFS)
		self.assertEqual([pc.frame_uid for pc in parsed.point_collections], [P])

	def test_unframed_points_refused_without_a_primary(self):
		report = new_report(primary_frame_uid=None)
		report.groups = [VolumePointCollectionGroup(points=[ImageCoordCollection([ImageCoord(1, 2, 3), ImageCoord(4, 5, 6)],
			point_type='MULTIPOINT')], tracking_id='landmarks', tracking_uid='2.25.46712')]
		self.assertIsNone(report.frames.primary_uid)
		with self.assertRaises(ValueError) as caught:
			report.create_sr()
		self.assertIn('Points without a frame', str(caught.exception))

	def test_reference_frames_matrices_become_declared_frames(self):
		report = new_report()
		report.reference_frames = [ImageTransformMatrix(EDGE_2, uid='2.25.46721', comment='Morphology')]
		doc = disk(report.create_sr())
		parsed = SonadorImagingMeasurementReport.from_sr(doc, imaging_frames=REFS)
		frame = parsed.frames.frame('2.25.46721')
		self.assertEqual((frame.name, frame.parent_uid, parsed.frames.edges[frame.uid].state), ('Morphology', P, 'trusted'))


# ---- item 8 ----


class FramesRoundTripTestCase(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		logging.getLogger('sonador').setLevel(logging.CRITICAL)
		report = new_report()
		cls.a, cls.b, cls.root = chained(report)
		cls.chain_point = cls.b.to_mm([SOURCE_M])[0]
		report.measurements = [
			LengthMeasurement([cls.chain_point, (4.0, 2.0, 3.0)], frame_of_reference_uid=cls.b.uid, tracking_uid='2.25.46301',
				drawing_plane=((1, 0, 0), (0, 1, 0)), series_uid=F.SERIES['P']['series_uid']),
			LengthMeasurement([(0, 0, 0), (5, 0, 0)], frame_of_reference_uid=P, tracking_uid='2.25.46302')]
		report.groups = [
			VolumePointCollectionGroup(points=[ImageCoordCollection([ImageCoord(*cls.chain_point), ImageCoord(0, 0, 0)],
				reference_frame=cls.b.uid, point_type='MULTIPOINT')], tracking_id='landmarks-planned', tracking_uid='2.25.46311'),
			VolumePointCollectionGroup(points=[ImageCoordCollection([ImageCoord(1, 1, 1), ImageCoord(2, 2, 2)],
				reference_frame=cls.root.uid, point_type='MULTIPOINT')], tracking_id='landmarks-device', tracking_uid='2.25.46312')]
		cls.report = report
		cls.doc = disk(report.create_sr(dcmsr_instance_uid='2.25.46399'))
		cls.parsed = SonadorImagingMeasurementReport.from_sr(cls.doc, imaging_frames=REFS)

	def test_graph_reproduced(self):
		graph = self.parsed.frames
		for frame, parent, unit in ((self.a, P, 'mm'), (self.b, self.a.uid, 'm'), (self.root, None, 'mm')):
			got = graph.frame(frame.uid)
			self.assertEqual((got.name, got.parent_uid, got.unit.ucum, got.name_source), (frame.name, parent, unit, 'container'))
		self.assertTrue(np.allclose(graph.edges[self.b.uid].matrix.rigid_matrix(), EDGE_1))
		self.assertTrue(np.allclose(graph.edges[self.a.uid].matrix.rigid_matrix(), EDGE_2))
		self.assertEqual([graph.edges[u].state for u in (self.a.uid, self.b.uid)], ['trusted', 'trusted'])

	def test_tags_and_nesting(self):
		items = list(self.doc.RegistrationSequence)
		self.assertEqual([str(i.FrameOfReferenceUID) for i in items], [self.a.uid, self.b.uid])
		for item, name in zip(items, ('Anatomic', 'Planned')):
			registration = item[0x0070, 0x0309].value[0]
			self.assertEqual(registration[0x3006, 0x00C8].value, name)
			self.assertEqual(dcm_code_value(registration[0x0040, 0xA043].value[0]), DCMSR_FRAME_EDGE.value)
			self.assertIn('LongCodeValue', registration[0x0040, 0xA043].value[0])
			matrix = registration[0x0070, 0x030A].value[0]
			self.assertEqual(matrix[0x0070, 0x030C].value, 'RIGID')
			self.assertEqual(len(matrix[0x3006, 0x00C6].value), 16)
		containers = [c for c in self.doc.ContentSequence if srcontent_is_named(c, DCMSR_FRAME_CONTAINER)]
		self.assertEqual([F._container_uid(c) for c in containers], [self.a.uid, self.root.uid, self.b.uid])
		self.assertTrue(all(c.ContinuityOfContent == 'SEPARATE' and c.RelationshipType == 'CONTAINS' for c in containers))
		root_concepts = [dcm_code_value(c.ConceptNameCodeSequence[0]) for c in self.doc.ContentSequence]
		self.assertLess(root_concepts.index('126010'), root_concepts.index(DCMSR_FRAME_PRIMARY.value))
		self.assertLess(root_concepts.index(DCMSR_FRAME_PRIMARY.value), root_concepts.index(DCMSR_FRAME_CONTAINER.value))

	def test_comment_read_back_without_container(self):
		ds = copy.deepcopy(self.doc)
		F.remove_container(ds, self.b.uid)
		parsed = SonadorImagingMeasurementReport.from_sr(ds, imaging_frames=REFS)
		frame = parsed.frames.frame(self.b.uid)
		self.assertEqual((frame.name, frame.name_source), ('Planned', 'comment'))

	def test_chained_length_converts_to_the_oracle(self):
		length = by_uid(self.parsed, '2.25.46301')
		converted = self.parsed.convert(length, P)
		self.assertTrue(np.allclose(converted.points[0], EXPECTED_MM, atol=1e-6, rtol=0))
		self.assertTrue(np.allclose(converted.drawing_plane[0], (0, 0, 1), atol=1e-9))
		self.assertTrue(np.allclose(length.points[0], (1.0, 2.0, 3.0), atol=1e-9, rtol=0))
		self.assertEqual((converted.tracking_uid, converted.series_uid), ('2.25.46301', F.SERIES['P']['series_uid']))
		self.assertAlmostEqual(converted.value, length.value)

	def test_chained_points_convert_and_root_refused(self):
		planned, device = self.parsed.point_collections
		converted = self.parsed.convert(planned, P)
		self.assertTrue(np.allclose(converted.array[0], EXPECTED_MM, atol=1e-6, rtol=0))
		self.assertEqual(converted.frame_uid, P)
		with self.assertRaises(FrameConversionError) as caught:
			self.parsed.convert(device, P)
		self.assertEqual(caught.exception.reason, 'no trusted path')

	def test_second_save_identical_frame_subtrees(self):
		again = disk(SonadorImagingMeasurementReport.from_sr(self.doc, imaging_frames=REFS).create_sr())
		first_items, first_root = frame_subtrees(self.doc)
		second_items, second_root = frame_subtrees(again)
		self.assertEqual([_item_bytes(i) for i in first_items], [_item_bytes(i) for i in second_items])
		self.assertEqual([_item_bytes(i) for i in first_root], [_item_bytes(i) for i in second_root])
		regenerated = disk(self.report.create_sr(dcmsr_instance_uid='2.25.46398'))
		self.assertEqual([_item_bytes(i) for i in frame_subtrees(regenerated)[0]], [_item_bytes(i) for i in first_items])
		self.assertEqual([_item_bytes(i) for i in frame_subtrees(regenerated)[1]], [_item_bytes(i) for i in first_root])


# ---- item 9 ----


class FramesTrustAndDamageTestCase(unittest.TestCase):
	def assertFrame(self, report, uid, state, reason=None, quarantined=None, row=None):
		found = report.frames.document.classification.get(uid)
		self.assertIsNotNone(found, uid)
		self.assertEqual((found.state, found.reason), (state, reason), uid)
		if row is not None:
			self.assertEqual(found.row, row, uid)
		if quarantined is not None:
			self.assertEqual(report.frames.frame(uid).quarantined, quarantined, uid)

	def test_a_child_container_removed(self):
		parsed = read('exc-l2-container-removed')
		self.assertFrame(parsed, A2, 'unverified', 'no-container', True, 13)
		self.assertFrame(parsed, SIB, 'trusted', None, False, 11)
		self.assertEqual(parsed.frames.frame(A2).name_source, 'comment')
		self.assertIsNone(parsed.frames.path(A2, P))

	def test_b_all_containers_removed(self):
		parsed = read('exc-all-containers-removed')
		for uid in (A2, SIB):
			self.assertFrame(parsed, uid, 'unverified', 'no-container')
		self.assertFalse(any(c.state == 'legacy' for c in parsed.frames.document.classification.values()))

	def test_c_legacy_candidates(self):
		self.assertFrame(read('legacy'), G6, 'legacy', 'legacy-provenance', False, 17)
		self.assertTrue(conditions(read('legacy'), SR_CONDITION_FRAME_EDGE_LEGACY))
		for name in ('legacy-equipment-post-boundary', 'legacy-equipment-dev', 'legacy-equipment-missing', 'legacy-equipment-foreign'):
			self.assertFrame(read(name), G6, 'unverified', 'no-marker', True, 18)
		self.assertFrame(read('exc-two-reference-legacy'), A2, 'unverified', 'legacy-target-ambiguous', True, 18)

	def test_d_non_rigid_edge(self):
		parsed = read('exc-intermediate-non-rigid')
		self.assertFrame(parsed, T, 'unverified', 'non-rigid:orthonormal rotation', True, 9)
		self.assertIsNone(parsed.frames.path(X, P))
		self.assertIsNotNone(parsed.frames.path(SIB, P))

	def test_e_cycle_and_duplicates(self):
		parsed = read('row10-cycle')
		self.assertFrame(parsed, A2, 'unverified', 'cycle')
		self.assertFrame(parsed, X, 'unverified', 'cycle')
		identical = dataset('exc-intact')
		identical.RegistrationSequence.append(copy.deepcopy(F.items_of(identical, A2)[0]))
		parsed = F.read_report(identical, ('P', 'S'), 'exchange')
		self.assertFrame(parsed, A2, 'trusted', None, False, 11)
		self.assertEqual(len(parsed.create_sr().RegistrationSequence), 3)
		conflicting = copy.deepcopy(identical)
		F.items_of(conflicting, A2)[1].MatrixRegistrationSequence[0].MatrixSequence[0].FrameOfReferenceTransformationMatrix = \
			[float(v) for v in F.SHIFT.flatten()]
		self.assertFrame(F.read_report(conflicting, ('P', 'S'), 'exchange'), A2, 'unverified', 'conflicting-duplicate', True)

	def test_f_disconnected_chain(self):
		parsed = read('exc-topology-a')
		self.assertIsNotNone(parsed.frames.path(A2, R3))
		self.assertEqual(parsed.frames.representability(A2), DCMSR_FRAME_UNREGISTERED)
		self.assertTrue(np.allclose(parsed.frames.convert([(1, 2, 3)], A2, R3)[0], EDGE_1.dot((1, 2, 3, 1))[:3]))

	def test_g_item_removed(self):
		parsed = read('row4-item-removed')
		self.assertFrame(parsed, A2, 'unverified', 'missing-item', True, 4)
		self.assertEqual(parsed.frames.frame(A2).name, 'Anatomic')
		self.assertIsNone(parsed.frames.path(A2, P))

	def test_h_malformed_and_conflicting_containers(self):
		parsed = read('row3-container-without-unit')
		self.assertFrame(parsed, A2, 'unverified', 'frame-node-invalid', True, 3)
		self.assertTrue(conditions(parsed, SR_CONDITION_FRAME_NODE_INVALID))
		parsed = read('row2-conflicting-containers')
		self.assertFrame(parsed, A2, 'unverified', 'frame-node-conflict', True, 2)
		self.assertTrue(conditions(parsed, SR_CONDITION_FRAME_NODE_CONFLICT))
		self.assertIsNone(parsed.frames.path(A2, P))

	def test_i_new_write_refusals(self):
		report = new_report()
		graph = report.frames
		a = graph.add_frame(ReferenceFrame('2.25.46401', name='A'))
		b = graph.add_frame(ReferenceFrame('2.25.46402', name='B'))
		graph.add_edge(F.SHIFT, a, P)
		graph.add_edge(F.SHIFT, b, a)
		r = graph.add_frame(ReferenceFrame('2.25.46404', name='R'))
		q = graph.add_frame(ReferenceFrame('2.25.46405', name='Q'))
		graph.add_edge(F.SHIFT, q, r)
		cases = ((F.SHIFT, r, q, 'cycle'), (F.SHIFT, b, P, 'second-parent'), (F.SHIFT, r, '2.25.46409', 'no-parent'))
		for matrix, source, target, reason in cases:
			with self.assertRaises(SRDiagnosticError) as caught:
				graph.add_edge(matrix, source, target)
			self.assertEqual(caught.exception.diagnostic.condition, SR_CONDITION_FRAME_EDGE_INVALID)
			self.assertIn(reason, str(caught.exception))
		c = graph.add_frame(ReferenceFrame('2.25.46403', name='C'))
		with self.assertRaises(SRDiagnosticError) as caught:
			graph.add_edge(F.NON_RIGID, c, P)
		self.assertIn('non-rigid:orthonormal rotation', str(caught.exception))

	def test_j_dangling_parent(self):
		parsed = read('exc-dangling-parent')
		self.assertEqual(parsed.frames.frame(T).evidence, set(('mention',)))
		self.assertTrue(conditions(parsed, SR_CONDITION_FRAME_NODE_MENTIONED_ONLY))
		self.assertFrame(parsed, A2, 'unverified', 'parent-unknown', True, 8)
		self.assertFrame(parsed, Y, 'unverified', 'parent-unknown', True, 8)
		self.assertIsNone(parsed.frames.path(A2, T))
		self.assertIsNone(parsed.frames.path(A2, Y))
		with self.assertRaises(FrameConversionError):
			parsed.convert(by_uid(parsed, F.LENGTHS['L2'][0]), T)
		control = read('exc-dangling-root-control')
		self.assertFrame(control, A2, 'trusted', None, False, 11)
		self.assertIsNotNone(control.frames.path(A2, Y))

	def test_k_container_plus_unmarked_item(self):
		parsed = read('exc-container-unmarked')
		self.assertFrame(parsed, A2, 'unverified', 'no-marker', True, 7)
		self.assertIsNone(parsed.frames.path(A2, P))
		self.assertIsNone(parsed.frames.path(A2, S))
		self.assertEqual((parsed.frames.frame(A2).name, parsed.frames.frame(A2).name_source), ('Anatomic', 'container'))

	def test_l_quarantined_container_backed_edge(self):
		parsed = read('exc-quarantined-edge')
		self.assertFrame(parsed, A2, 'unverified', 'quarantined', True, 1)
		self.assertTrue(conditions(parsed, SR_CONDITION_FRAME_STATUS_CONFLICT))
		with self.assertRaises(FrameConversionError) as caught:
			parsed.convert(by_uid(parsed, F.LENGTHS['L2'][0]), P)
		self.assertEqual(caught.exception.reason, 'quarantined')
		self.assertFrame(parsed, SIB, 'trusted', None, False, 11)
		only = dataset('exc-intact')
		F.add_declaration(only, 'unverified', A2)
		self.assertFrame(F.read_report(only, ('P', 'S'), 'exchange'), A2, 'unverified', 'quarantined', True, 1)

	def test_m_quarantined_parentless_root(self):
		parsed = read('exc-quarantined-root')
		self.assertFrame(parsed, R3, 'unverified', 'quarantined', True, 1)
		self.assertTrue(conditions(parsed, SR_CONDITION_FRAME_NODE_QUARANTINED))
		self.assertEqual(parsed.frames.frame(R3).name, 'Implant')
		self.assertEqual(parsed.frames.reachable(R3), [])
		self.assertFrame(parsed, R3B, 'root', None, False, 12)

	def test_n_quarantined_intermediate_node(self):
		for name, reason in (('exc-intermediate-declared', 'quarantined'), ('exc-intermediate-non-rigid', 'non-rigid:orthonormal rotation')):
			parsed = read(name)
			graph = parsed.frames
			self.assertFrame(parsed, X, 'trusted', None, False, 11)
			self.assertFrame(parsed, Y, 'trusted', None, False, 11)
			self.assertFrame(parsed, T, 'unverified', reason, True)
			self.assertFalse([d for d in parsed.diagnostics if d.value in (X, Y)])
			for source, target in ((X, Y), (X, T), (Y, T)):
				self.assertIsNone(graph.path(source, target), (name, source, target))
			with self.assertRaises(FrameConversionError):
				parsed.convert(by_uid(parsed, F.LENGTHS['LX'][0]), Y)
			self.assertEqual(graph.representability(X, target=Y), DCMSR_FRAME_UNREGISTERED)
			self.assertEqual(graph.representability(X, target=X), DCMSR_FRAME_NATIVE)
			self.assertIsNotNone(graph.path(SIB, P))

			saved = disk(parsed.create_sr())
			self.assertEqual(declarations(saved)['unverified'], [T])
			self.assertEqual(item_bytes(saved, T), item_bytes(built()[name].dataset, T))
			reloaded = F.read_report(saved, ('P', 'S'), 'exchange')
			self.assertFrame(reloaded, T, 'unverified', 'quarantined', True, 1)
			self.assertFrame(reloaded, X, 'trusted', None, False, 11)
			self.assertIsNone(reloaded.frames.path(X, Y))

			# Re-declaring T alone (a rigid edge) restores the path on the next read
			reloaded.frames.remove_frame(T)
			reloaded.frames.add_frame(ReferenceFrame(T, name='Pelvis'))
			reloaded.frames.add_edge(EDGE_2, T, P)
			repaired = F.read_report(disk(reloaded.create_sr()), ('P', 'S'), 'exchange')
			self.assertFrame(repaired, T, 'trusted', None, False, 11)
			self.assertIsNotNone(repaired.frames.path(X, Y))
			self.assertEqual(declarations(disk(repaired.create_sr()))['unverified'], [])

		clean = read('exc-intermediate-clean')
		self.assertIsNotNone(clean.frames.path(X, Y))
		expected = np.linalg.inv(F.SHIFT).dot(F.RZ)
		self.assertTrue(np.allclose(clean.frames.transform(X, Y).rigid_matrix(), expected))


# ---- item 10 ----


class FramesResaveTestCase(unittest.TestCase):
	def test_a_foreign_item_never_promoted(self):
		before = read('exc-foreign')
		self.assertEqual(before.frames.document.classification[A2].reason, 'no-marker')
		after_ds = built()['exc-foreign-save1'].dataset
		after = read('exc-foreign-save1')
		self.assertEqual(after.frames.document.classification[A2].reason, 'quarantined')
		self.assertEqual(item_bytes(after_ds, A2), item_bytes(built()['exc-foreign'].dataset, A2))
		self.assertEqual(declarations(after_ds)['unverified'], [A2])
		self.assertEqual(after_ds.ManufacturerModelName, 'Sonador-IO')
		with self.assertRaises(FrameConversionError):
			after.convert(by_uid(after, F.LENGTHS['L2'][0]), P)

	def test_b_genuine_legacy_kept(self):
		for name in ('legacy', 'legacy-save1', 'legacy-save2'):
			parsed = read(name)
			found = parsed.frames.document.classification[G6]
			self.assertEqual((found.state, found.target), ('legacy', P), name)
		saved = built()['legacy-save1'].dataset
		self.assertEqual(declarations(saved), OrderedDict((('primary', [P]), ('legacy', [G6]), ('unverified', []))))
		self.assertEqual(item_bytes(saved, G6), item_bytes(built()['legacy'].dataset, G6))

	def test_c_stripped_marker_never_legacy(self):
		parsed = read('row16-marked-unmarked-item')
		self.assertEqual(parsed.frames.document.classification[A2].reason, 'no-marker')
		self.assertEqual(parsed.frames.document.classification[A2].row, 16)

	def test_d_primary_change_never_rebinds(self):
		parsed = read('legacy-saved-caller-primary')
		self.assertTrue(conditions(parsed, SR_CONDITION_FRAME_PRIMARY_CHANGED))
		self.assertEqual(parsed.frames.edges[G6].target_uid, P)
		l6 = by_uid(parsed, F.LENGTHS['L6'][0])
		self.assertEqual(parsed.representability(l6), DCMSR_FRAME_UNREGISTERED)
		self.assertEqual(parsed.representability(l6, target=P), DCMSR_FRAME_REGISTERED)
		with self.assertRaises(FrameConversionError):
			parsed.convert(l6, S)
		with self.assertRaises(SRDiagnosticError) as caught:
			parsed.create_sr()
		self.assertEqual(caught.exception.diagnostic.condition, SR_CONDITION_LEGACY_TARGET_CHANGE)
		kept = read('legacy-saved-second-series')
		self.assertEqual(declarations(disk(kept.create_sr()))['primary'], [P])
		bridged = read('legacy-saved-p-to-q')
		self.assertEqual(bridged.representability(by_uid(bridged, F.LENGTHS['L6'][0]), target=S), DCMSR_FRAME_REGISTERED)
		self.assertEqual(read('legacy-saved-no-primary').frames.document.classification[G6].reason, 'legacy-target-unknown')
		two = read('legacy-saved-two-primaries')
		self.assertEqual(two.frames.document.classification[G6].reason, 'legacy-target-unknown')
		self.assertTrue(conditions(two, SR_CONDITION_FRAME_PRIMARY_INVALID))

	def test_e_two_saves_container_backed(self):
		initial = read('exc-quarantined-edge')
		saved = disk(initial.create_sr())
		self.assertTrue(conditions(initial, SR_CONDITION_FRAME_STATUS_DEMOTED))
		self.assertEqual(declarations(saved)['unverified'], [A2])
		self.assertEqual(declarations(saved)['legacy'], [])
		self.assertEqual(item_bytes(saved, A2), item_bytes(built()['exc-quarantined-edge'].dataset, A2))
		self.assertEqual([_item_bytes(c) for c in F.containers_of(saved, A2)],
			[_item_bytes(c) for c in F.containers_of(built()['exc-quarantined-edge'].dataset, A2)])
		for name in ('exc-quarantined-edge-save1', 'exc-quarantined-edge-save2'):
			parsed = read(name)
			self.assertEqual(parsed.frames.document.classification[A2].reason, 'quarantined')
			self.assertEqual(parsed.frames.document.classification[SIB].state, 'trusted')
			with self.assertRaises(FrameConversionError):
				parsed.convert(by_uid(parsed, F.LENGTHS['L2'][0]), P)
		self.assertEqual(frame_subtrees(built()['exc-quarantined-edge-save1'].dataset)[0],
			frame_subtrees(built()['exc-quarantined-edge-save2'].dataset)[0])
		self.assertEqual(declarations(built()['exc-quarantined-edge-save1'].dataset), declarations(built()['exc-quarantined-edge-save2'].dataset))
		redeclared = read('exc-quarantined-edge-save2')
		redeclared.frames.remove_frame(A2)
		redeclared.frames.add_frame(ReferenceFrame(A2, name='Anatomic'))
		redeclared.frames.add_edge(F.RZ, A2, P)
		third = disk(redeclared.create_sr())
		self.assertEqual(declarations(third)['unverified'], [])
		self.assertEqual(F.read_report(third, ('P', 'S'), 'exchange').frames.document.classification[A2].state, 'trusted')
		with self.assertRaises(SRDiagnosticError):
			read('exc-quarantined-edge', strict=True)

	def test_f_declaration_only_orphan(self):
		for series in (('row5-orphan-legacy', 'row5-orphan-legacy-save1', 'row5-orphan-legacy-save2'),
				('row5-orphan-unverified', 'row5-orphan-unverified-save1')):
			for name in series:
				parsed = read(name)
				found = parsed.frames.document.classification[A2]
				self.assertEqual((found.row, found.reason), (5, 'missing-item'), name)
				self.assertIn(A2, parsed.frames)
				self.assertFalse(conditions(parsed, SR_CONDITION_UNKNOWN_FRAME) and any(d.value == A2 for d in conditions(parsed, SR_CONDITION_UNKNOWN_FRAME)))
				self.assertEqual(parsed.frames.reachable(A2), [])
			for name in series[1:]:
				ds = built()[name].dataset
				self.assertEqual(declarations(ds)['unverified'], [A2])
				self.assertEqual((F.items_of(ds, A2), F.containers_of(ds, A2)), ([], []))

	def test_g_legacy_without_primary_demoted(self):
		parsed = read('legacy-saved-no-primary-save1')
		self.assertEqual(parsed.frames.document.classification[G6].state, 'unverified')
		self.assertEqual(declarations(built()['legacy-saved-no-primary-save1'].dataset)['legacy'], [])

	def test_h_quarantined_root_across_saves(self):
		for name in ('exc-quarantined-root-save1', 'exc-quarantined-root-save2'):
			ds = built()[name].dataset
			self.assertEqual(declarations(ds)['unverified'], [R3])
			self.assertEqual([_item_bytes(c) for c in F.containers_of(ds, R3)],
				[_item_bytes(c) for c in F.containers_of(built()['exc-quarantined-root'].dataset, R3)])
			self.assertEqual(F.items_of(ds, R3), [])
			parsed = read(name)
			self.assertEqual(parsed.frames.document.classification[R3].reason, 'quarantined')
			self.assertEqual(parsed.frames.document.classification[R3B].state, 'root')
		parsed = read('exc-quarantined-root-save2')
		parsed.frames.remove_frame(R3)
		parsed.frames.add_frame(ReferenceFrame(R3, name='Implant'))
		again = F.read_report(disk(parsed.create_sr()), ('P', 'S'), 'exchange')
		self.assertEqual(again.frames.document.classification[R3].state, 'root')
		self.assertFalse(again.frames.frame(R3).quarantined)
		# The stray item of row 6 lost between saves: still quarantined
		stray = dataset('exc-quarantined-root')
		item = copy.deepcopy(F.items_of(stray, A2)[0])
		item.FrameOfReferenceUID = R3
		stray.RegistrationSequence.append(item)
		first = F.read_report(stray, ('P', 'S'), 'exchange')
		self.assertEqual(first.frames.document.classification[R3].reason, 'quarantined')
		saved = disk(first.create_sr())
		F.remove_item(saved, R3)
		self.assertEqual(F.read_report(saved, ('P', 'S'), 'exchange').frames.document.classification[R3].reason, 'quarantined')


class FramesEditTestCase(unittest.TestCase):
	'''	Edits of a read document: an edge given to an imported root, a removed parent, a replaced `reference_frames`
		matrix, a frame-only geometry edit, and the conversion of a single landmark.
	'''
	def containers(self, ds, uid):
		return F.containers_of(ds, uid)

	def test_edge_on_a_clean_imported_root_regenerates_its_record(self):
		report = read('exc-intact')
		report.frames.add_edge(F.SHIFT, R3, P)
		self.assertFalse(report.frames.frame(R3).imported)
		saved = disk(report.create_sr())
		self.assertEqual(len(self.containers(saved, R3)), 1)
		self.assertEqual(len(F.items_of(saved, R3)), 1)
		self.assertEqual(declarations(saved)['unverified'], [])
		reloaded = F.read_report(saved, ('P', 'S'), 'exchange')
		found = reloaded.frames.document.classification[R3]
		self.assertEqual((found.row, found.state, found.target), (11, 'trusted', P))
		self.assertEqual(reloaded.frames.frame(R3).name, 'Implant')
		self.assertEqual(reloaded.frames.representability(R3), DCMSR_FRAME_REGISTERED)
		self.assertEqual(reloaded.frames.document.classification[SIB].state, 'trusted')
		self.assertEqual(reloaded.frames.document.classification[R3B].state, 'root')

	def test_edge_on_a_quarantined_root_refused_until_redeclared(self):
		report = read('exc-quarantined-root')
		before = dict(report.frames.edges)
		with self.assertRaises(SRDiagnosticError) as caught:
			report.frames.add_edge(F.SHIFT, R3, P)
		self.assertEqual(caught.exception.diagnostic.condition, SR_CONDITION_FRAME_EDGE_INVALID)
		self.assertIn('remove_frame', str(caught.exception))
		self.assertEqual(dict(report.frames.edges), before)
		self.assertTrue(report.frames.frame(R3).quarantined)
		saved = disk(report.create_sr())
		self.assertEqual(declarations(saved)['unverified'], [R3])
		self.assertEqual(F.read_report(saved, ('P', 'S'), 'exchange').frames.document.classification[R3].reason, 'quarantined')

		report.frames.remove_frame(R3)
		report.frames.add_frame(ReferenceFrame(R3, name='Implant'))
		report.frames.add_edge(F.SHIFT, R3, P)
		saved = disk(report.create_sr())
		self.assertEqual((len(self.containers(saved, R3)), declarations(saved)['unverified']), (1, []))
		self.assertEqual(F.read_report(saved, ('P', 'S'), 'exchange').frames.document.classification[R3].state, 'trusted')

	def test_removed_parent_disconnects_its_children(self):
		report = read('exc-intermediate-clean')
		graph = report.frames
		self.assertIsNotNone(graph.path(X, Y))
		graph.remove_frame(T)
		for source, target in ((X, Y), (Y, X)):
			self.assertIsNone(graph.path(source, target))
			self.assertEqual(graph.representability(source, target=target), DCMSR_FRAME_UNREGISTERED)
		with self.assertRaises(FrameConversionError):
			report.convert(by_uid(report, F.LENGTHS['LX'][0]), Y)
		self.assertIsNotNone(graph.path(SIB, P))
		graph.add_frame(ReferenceFrame(T, name='Pelvis'))
		graph.add_edge(EDGE_2, T, P)
		self.assertIsNotNone(graph.path(X, Y))
		reloaded = F.read_report(disk(report.create_sr()), ('P', 'S'), 'exchange')
		self.assertIsNotNone(reloaded.frames.path(X, Y))

	def test_reference_frames_replaced_and_cleared(self):
		report = new_report()
		own = report.frames.add_frame(ReferenceFrame('2.25.46801', name='Own'))
		report.frames.add_edge(F.SHIFT, own, P)
		report.reference_frames = [ImageTransformMatrix(EDGE_1, uid='2.25.46802', comment='First')]
		self.assertEqual(report.frames.frame('2.25.46802').name, 'First')
		report.reference_frames = [ImageTransformMatrix(EDGE_2, uid='2.25.46802', comment='Second')]
		parsed = SonadorImagingMeasurementReport.from_sr(disk(report.create_sr()), imaging_frames=REFS)
		self.assertEqual(parsed.frames.frame('2.25.46802').name, 'Second')
		self.assertTrue(np.allclose(parsed.frames.edges['2.25.46802'].matrix.rigid_matrix(), EDGE_2))
		self.assertIn('2.25.46801', parsed.frames)

		report.reference_frames = []
		self.assertNotIn('2.25.46802', report.frames)
		parsed = SonadorImagingMeasurementReport.from_sr(disk(report.create_sr()), imaging_frames=REFS)
		self.assertNotIn('2.25.46802', parsed.frames)
		self.assertEqual(parsed.frames.document.classification['2.25.46801'].state, 'trusted')

		imported = read('exc-intact')
		imported.reference_frames = [ImageTransformMatrix(EDGE_1, uid='2.25.46803', comment='Added')]
		imported.frames
		imported.reference_frames = None
		saved = disk(imported.create_sr())
		self.assertEqual(F.items_of(saved, '2.25.46803'), [])
		self.assertEqual([len(F.containers_of(saved, uid)) for uid in (A2, SIB, R3, R3B)], [1, 1, 1, 1])

	def test_registration_provenance_written_and_replaced(self):
		from highdicom.sr import CodedConcept

		def fiducial(uid):
			ds = Dataset()
			ds.ReferencedFiducialUID = uid
			return ds

		def image(sop):
			ds = Dataset()
			ds.ReferencedSOPClassUID, ds.ReferencedSOPInstanceUID = F.MR_IMAGE_STORAGE, sop
			return ds

		def matrix(f, i, meaning):
			return ImageTransformMatrix(EDGE_1, uid='2.25.46821', comment='Planned',
				fiducials=[fiducial(f)], ref_images=[image(i)], codes=[CodedConcept('SCoord3DTx', 'Sonador-SR', meaning)])

		def written(report):
			saved = disk(report.create_sr())
			item = F.items_of(saved, '2.25.46821')[0]
			registration = item.MatrixRegistrationSequence[0]
			codes = [(dcm_code_value(c), str(c.CodeMeaning)) for c in registration.ConceptNameCodeSequence]
			return (str(item.UsedFiducialsSequence[0].ReferencedFiducialUID), str(item.ReferencedImageSequence[0].ReferencedSOPInstanceUID),
				codes)

		report = new_report()
		report.reference_frames = [matrix('2.25.46822', '2.25.46823', 'First method')]
		fiducial_uid, image_uid, codes = written(report)
		self.assertEqual((fiducial_uid, image_uid), ('2.25.46822', '2.25.46823'))
		self.assertEqual(codes, [(DCMSR_FRAME_EDGE.value, DCMSR_FRAME_EDGE.meaning), ('SCoord3DTx', 'First method')])

		report.reference_frames = [matrix('2.25.46824', '2.25.46825', 'Second method')]
		fiducial_uid, image_uid, codes = written(report)
		self.assertEqual((fiducial_uid, image_uid), ('2.25.46824', '2.25.46825'))
		self.assertEqual(codes[1], ('SCoord3DTx', 'Second method'))

		edge = new_report()
		frame = edge.frames.add_frame(ReferenceFrame('2.25.46821', name='Planned'))
		edge.frames.add_edge(matrix('2.25.46826', '2.25.46827', 'Direct'), frame, P)
		self.assertEqual(written(edge)[:2], ('2.25.46826', '2.25.46827'))

	def test_converted_measurement_retagged_in_another_report(self):
		parsed = read('exc-intact')
		converted = parsed.convert(by_uid(parsed, F.LENGTHS['L2'][0]), P)
		other = new_report()
		other.measurements = [converted]
		reloaded = F.read_report(disk(other.create_sr()), ('P', 'S'), 'exchange')
		self.assertEqual(by_uid(reloaded, F.LENGTHS['L2'][0]).frame_of_reference_uid, P)
		converted.frame_of_reference_uid = '2.25.46831'
		with self.assertRaises(SRDiagnosticError) as caught:
			other.create_sr()
		self.assertEqual(caught.exception.diagnostic.condition, SR_CONDITION_FRAME_POLICY_REFUSED)
		self.assertIn(F.LENGTHS['L2'][0], str(caught.exception))

		moved = read('exc-intact')
		retained = new_report()
		l5 = by_uid(moved, F.LENGTHS['L5'][0])
		l5.description = 'edited'
		retained.measurements = [l5]
		self.assertEqual(by_uid(F.read_report(disk(retained.create_sr()), ('P', 'S'), 'exchange'), F.LENGTHS['L5'][0]).frame_of_reference_uid, U5)

	def test_frame_only_edit_to_an_unknown_frame_refused(self):
		report = read('exc-intact')
		by_uid(report, F.LENGTHS['L1'][0]).frame_of_reference_uid = '2.25.46901'
		with self.assertRaises(SRDiagnosticError) as caught:
			report.create_sr()
		self.assertEqual(caught.exception.diagnostic.condition, SR_CONDITION_FRAME_POLICY_REFUSED)
		self.assertIn(F.LENGTHS['L1'][0], str(caught.exception))

		unrelated = read('exc-intact')
		by_uid(unrelated, F.LENGTHS['L1'][0]).description = 'edited'
		saved = F.read_report(disk(unrelated.create_sr()), ('P', 'S'), 'exchange')
		self.assertEqual(by_uid(saved, F.LENGTHS['L5'][0]).frame_of_reference_uid, U5)

		report = new_report()
		report.frames.add_frame(ReferenceFrame(A2, name='Anatomic'))
		report.frames.add_edge(F.RZ, A2, P)
		report.findings = [ArrowAnnotation.from_points((0, 0, 0), (5, 0, 0), 'Tip', frame_of_reference_uid=A2, tracking_uid='2.25.46902')]
		parsed = SonadorImagingMeasurementReport.from_sr(disk(report.create_sr()), imaging_frames=REFS)
		arrow = parsed.arrow_findings[0]
		arrow.frame_of_reference_uid = '2.25.46903'
		with self.assertRaises(SRDiagnosticError) as caught:
			parsed.create_sr()
		self.assertIn('2.25.46902', str(caught.exception))
		arrow.frame_of_reference_uid = P
		self.assertTrue(parsed.create_sr())

	def test_single_landmark_converts(self):
		report = new_report()
		_, b, _ = chained(report)
		landmark = ImageCoord(*b.to_mm([SOURCE_M])[0], reference_frame=b.uid, point_type='POINT')
		group = VolumePointCollectionGroup(points=[landmark], tracking_id='landmark', tracking_uid='2.25.46911')
		report.groups = [group]
		point = report.point_collections[0]
		self.assertEqual(report.representability(point), DCMSR_FRAME_REGISTERED)
		converted = report.convert(point, P)
		self.assertIsInstance(converted, ImageCoord)
		self.assertTrue(np.allclose(converted, EXPECTED_MM, atol=1e-6, rtol=0))
		self.assertEqual(converted.frame_uid, P)
		self.assertTrue(np.allclose(landmark, (1.0, 2.0, 3.0), atol=1e-9, rtol=0))
		self.assertEqual(landmark.frame_uid, b.uid)
		with self.assertRaises(SRDiagnosticError):
			report.create_sr(require='native')
		group._points = [converted]
		parsed = SonadorImagingMeasurementReport.from_sr(disk(report.create_sr(require='native')), imaging_frames=REFS)
		self.assertEqual(parsed.point_collections[0].frame_uid, P)
		self.assertTrue(np.allclose(parsed.point_collections[0].points[0], EXPECTED_MM, atol=1e-4, rtol=0))


# ---- item 11 ----


class FramesPreservationTestCase(unittest.TestCase):
	def test_slots_multiplicity_and_bytes(self):
		ds = dataset('exc-intact')
		unverified = copy.deepcopy(F.items_of(ds, A2)[0])
		unverified.FrameOfReferenceUID = '2.25.46501'
		ds.RegistrationSequence.insert(1, unverified)
		opaque = Dataset()
		opaque.RelationshipType, opaque.ValueType = 'CONTAINS', 'TEXT'
		opaque.ConceptNameCodeSequence = Sequence([F.srcode2dataset(F.codes.DCM.Comment)])
		opaque.TextValue = 'opaque root item'
		containers = F.containers_of(ds)
		ds.ContentSequence.insert(list(ds.ContentSequence).index(containers[1]), opaque)
		before = [str(i.FrameOfReferenceUID) for i in ds.RegistrationSequence]
		before_bytes = [_item_bytes(i) for i in ds.RegistrationSequence]
		report = F.read_report(ds, ('P', 'S'), 'exchange')
		by_uid(report, F.LENGTHS['L1'][0]).description = 'edited'
		saved = disk(report.create_sr())
		self.assertEqual([str(i.FrameOfReferenceUID) for i in saved.RegistrationSequence], before)
		self.assertEqual([_item_bytes(i) for i in saved.RegistrationSequence], before_bytes)
		root = [dcm_code_value(c.ConceptNameCodeSequence[0]) for c in saved.ContentSequence]
		position = root.index('121106')
		self.assertEqual(root[position - 1], DCMSR_FRAME_CONTAINER.value)
		self.assertEqual(root[position + 1], DCMSR_FRAME_CONTAINER.value)
		l5 = F.read_report(saved, ('P', 'S'), 'exchange')
		self.assertEqual(by_uid(l5, F.LENGTHS['L5'][0]).frame_of_reference_uid, U5)
		self.assertEqual(declarations(saved)['unverified'], ['2.25.46501'])

	def test_new_geometry_in_unknown_frame_refused(self):
		report = read('exc-intact')
		report.measurements = [LengthMeasurement([(0, 0, 0), (1, 0, 0)], frame_of_reference_uid='2.25.46599', tracking_uid='2.25.46598')]
		with self.assertRaises(SRDiagnosticError) as caught:
			report.create_sr()
		self.assertEqual(caught.exception.diagnostic.condition, SR_CONDITION_FRAME_POLICY_REFUSED)


# ---- item 12 ----


class FramesAccessorTestCase(unittest.TestCase):
	def keys(self, ds, referenced=('P', 'S')):
		series = SonadorImagingMeasurementReport.parse(copy.deepcopy(ds), imaging_frames=F.references_map(referenced))
		found = OrderedDict()
		for key, by_type in series.measurements().items():
			for collection in by_type.values():
				for m in collection:
					found[m.tracking_uid] = key
		return found, series

	def test_buckets(self):
		ds = built()['exc-intact'].dataset
		keys, series = self.keys(ds)
		document = str(ds.SeriesInstanceUID)
		self.assertEqual(keys[F.LENGTHS['L1'][0]], F.SERIES['P']['series_uid'])
		self.assertEqual(keys[F.LENGTHS['L4'][0]], F.SERIES['S']['series_uid'])
		for alias in ('L2', 'L3', 'L5'):
			self.assertEqual(keys[F.LENGTHS[alias][0]], document, alias)
		frame_only = series.measurements(frame_of_reference_uid=A2)
		self.assertEqual([m.tracking_uid for by_type in frame_only.values() for c in by_type.values() for m in c], [F.LENGTHS['L2'][0]])

	def test_buckets_of_damaged_frames(self):
		for name, alias in (('row5-orphan-legacy', 'L2'), ('row5-orphan-legacy-save1', 'L2'), ('exc-quarantined-root', 'L3'),
				('exc-dangling-parent', 'L2'), ('legacy', 'L6')):
			ds = built()[name].dataset
			keys, _ = self.keys(ds, built()[name].referenced)
			self.assertEqual(keys[F.LENGTHS[alias][0]], str(ds.SeriesInstanceUID), name)

	def test_unknown_frame_and_source_series_independent(self):
		self.assertEqual([d.value for d in conditions(read('exc-intact'), SR_CONDITION_UNKNOWN_FRAME)], [U5])
		for name in ('legacy', 'row5-orphan-legacy', 'row5-orphan-legacy-save1', 'exc-quarantined-root', 'exc-dangling-parent'):
			self.assertNotIn(F.FRAMES['U5'] if name == 'legacy' else None,
				[d.value for d in conditions(read(name), SR_CONDITION_UNKNOWN_FRAME)])
			values = [d.value for d in conditions(read(name), SR_CONDITION_UNKNOWN_FRAME)]
			self.assertFalse(set(values) & set((A2, G6, R3, T)), name)
		report = new_report()
		report.measurements = [LengthMeasurement([(0, 0, 0), (1, 0, 0)], frame_of_reference_uid=P, tracking_uid='2.25.46601',
			series_uid=F.SERIES['S']['series_uid'])]
		keys, series = self.keys(disk(report.create_sr()))
		self.assertEqual(keys['2.25.46601'], F.SERIES['S']['series_uid'])

	def test_reference_frames_from_cache(self):
		from .sr_measurements import MeasurementRetrievalTestCase
		case = MeasurementRetrievalTestCase('test_budget_and_keys')
		case.setUp()
		case.study.measurements()
		before = len(case.pacs.requests)
		for series in case.study.series_collection:
			for instance in (series.__dict__.get('_instances') or []):
				report = instance.__dict__.get('_report')
				if report is not None:
					report.frames
		self.assertEqual(len(case.pacs.requests), before)


# ---- items 14 and 15 ----

# The Viewer's per-Length note states (P loaded, S and S2 not)
PANEL_ORACLE = OrderedDict((
	('intact', dict(L1='drawn', L2='A', L3='B', L4='B', L5='D')),
	('all-containers-removed', dict(L1='drawn', L2='C', L3='D', L4='B', L5='D')),
	('l2-non-rigid', dict(L1='drawn', L2='C', L3='B', L4='B', L5='D')),
	('l2-container-removed', dict(L1='drawn', L2='C', L3='B', L4='B', L5='D')),
	('l3-container-removed', dict(L1='drawn', L2='A', L3='D', L4='B', L5='D')),
	('topology-a', dict(L1='drawn', L2='B', L4='B', L5='D')),
	('topology-b', dict(L1='drawn', L2='C', L4='B', L5='D')),
	('dangling-parent', dict(L1='drawn', L2='C', L4='B', L5='D')),
	('dangling-root-control', dict(L1='drawn', L2='B', L4='B', L5='D')),
	('dangling-edge-control', dict(L1='drawn', L2='A', L4='B', L5='D')),
	('container-unmarked', dict(L1='drawn', L2='C', L3='B', L4='B', L5='D')),
	('quarantined-edge', dict(L1='drawn', L2='C', L3='B', L4='B', L5='D')),
	('quarantined-edge-save1', dict(L1='drawn', L2='C', L3='B', L4='B', L5='D')),
	('quarantined-edge-save2', dict(L1='drawn', L2='C', L3='B', L4='B', L5='D')),
	('quarantined-root', dict(L1='drawn', L2='A', L3='C', L4='B', L5='D')),
	('quarantined-root-save1', dict(L1='drawn', L2='A', L3='C', L4='B', L5='D')),
	('quarantined-root-save2', dict(L1='drawn', L2='A', L3='C', L4='B', L5='D')),
	('intermediate-clean', dict(L1='drawn', LX='A', LY='A', L4='B', L5='D')),
	('intermediate-declared', dict(L1='drawn', LX='C', LY='C', L4='B', L5='D')),
	('intermediate-declared-save1', dict(L1='drawn', LX='C', LY='C', L4='B', L5='D')),
	('intermediate-non-rigid', dict(L1='drawn', LX='C', LY='C', L4='B', L5='D')),
	('intermediate-non-rigid-save1', dict(L1='drawn', LX='C', LY='C', L4='B', L5='D')),
	('two-reference-legacy', dict(L1='drawn', L2='C', L4='B', L5='D')),
	('foreign', dict(L1='drawn', L2='C', L5='D')),
	('foreign-save1', dict(L1='drawn', L2='C', L5='D')),
))
EMPTY_SET_ORACLE = dict(L1='B', L2='B', L3='B', L4='B', L5='D')


def _state(value):
	return value.split(' ', 1)[0]


class FramesPanelOracleTestCase(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		logging.getLogger('sonador').setLevel(logging.CRITICAL)
		cls.table = F.case_table(built())

	def test_both_roots_against_the_oracle(self):
		for root in ('app', 'exc'):
			for variant, expected in PANEL_ORACLE.items():
				entry = self.table['scenarios']['%s-%s' % (root, variant)]
				got = dict((alias, _state(l['viewer']['P'])) for alias, l in entry['lengths'].items())
				self.assertEqual(got, expected, '%s-%s' % (root, variant))

	def test_empty_loaded_set(self):
		entry = self.table['scenarios']['exc-intact']
		self.assertEqual(dict((a, _state(l['viewer']['none'])) for a, l in entry['lengths'].items()), EMPTY_SET_ORACLE)

	def test_loaded_set_changes(self):
		entry = self.table['scenarios']['exc-intact']
		self.assertEqual(entry['lengths']['L4']['viewer']['S'], 'drawn')
		self.assertEqual(entry['lengths']['L2']['viewer']['P+S2'], 'A (P, S2)')
		self.assertEqual(entry['lengths']['L2']['viewer']['P'], 'A (P)')

	def test_genuine_legacy_pair(self):
		for name in ('legacy', 'legacy-save1'):
			viewer = self.table['scenarios'][name]['lengths']['L6']['viewer']
			self.assertEqual((viewer['P'], viewer['S'], viewer['S2']), ('A (P)', 'B', 'A (S2)'), name)
		bridged = self.table['scenarios']['legacy-saved-p-to-q']['lengths']['L6']['viewer']
		self.assertEqual(bridged['S'], 'A (S)')
		unrelated = self.table['scenarios']['legacy-saved-second-series']['lengths']['L6']['viewer']
		self.assertEqual(unrelated['S'], 'B')

	def test_names_only_from_valid_containers(self):
		for name, alias, shown in (('exc-all-containers-removed', 'A2', None), ('exc-container-unmarked', 'A2', 'Anatomic'),
				('exc-quarantined-edge', 'A2', 'Anatomic'), ('exc-quarantined-root', 'R3', 'Implant'), ('exc-foreign', 'A2', None)):
			self.assertEqual(self.table['scenarios'][name]['frames'][alias]['displayed_name'], shown, name)


# Rows of the edge classification table, by scenario and frame: (row, trust, reason, quarantined, written back)
CASE_ORACLE = OrderedDict((
	(('exc-quarantined-edge', 'A2'), (1, 'unverified', 'quarantined', True, 'unverified')),
	(('exc-quarantined-root', 'R3'), (1, 'unverified', 'quarantined', True, 'unverified')),
	(('exc-foreign-save1', 'A2'), (1, 'unverified', 'quarantined', True, 'unverified')),
	(('legacy-saved-conflict', 'G6'), (1, 'unverified', 'quarantined', True, 'unverified')),
	(('row2-conflicting-containers', 'A2'), (2, 'unverified', 'frame-node-conflict', True, 'unverified')),
	(('row3-container-without-unit', 'A2'), (3, 'unverified', 'frame-node-invalid', True, 'unverified')),
	(('row4-item-removed', 'A2'), (4, 'unverified', 'missing-item', True, 'unverified')),
	(('row5-orphan-legacy', 'A2'), (5, 'unverified', 'missing-item', True, 'unverified')),
	(('row5-orphan-unverified', 'A2'), (5, 'unverified', 'missing-item', True, 'unverified')),
	(('row6-container-without-parent', 'A2'), (6, 'unverified', 'no-parent', True, 'unverified')),
	(('exc-container-unmarked', 'A2'), (7, 'unverified', 'no-marker', True, 'unverified')),
	(('exc-dangling-parent', 'A2'), (8, 'unverified', 'parent-unknown', True, 'unverified')),
	(('exc-l2-non-rigid', 'A2'), (9, 'unverified', 'non-rigid:orthonormal rotation', True, 'unverified')),
	(('exc-intermediate-non-rigid', 'T'), (9, 'unverified', 'non-rigid:orthonormal rotation', True, 'unverified')),
	(('row10-cycle', 'A2'), (10, 'unverified', 'cycle', True, 'unverified')),
	(('exc-intact', 'A2'), (11, 'trusted', None, False, None)),
	(('exc-intermediate-declared', 'X'), (11, 'trusted', None, False, None)),
	(('exc-intact', 'R3'), (12, 'root', None, False, None)),
	(('row13-all-containers-removed', 'A2'), (13, 'unverified', 'no-container', True, 'unverified')),
	(('legacy-save1', 'G6'), (14, 'legacy', 'legacy-provenance', False, 'legacy')),
	(('legacy-saved-no-primary', 'G6'), (15, 'unverified', 'legacy-target-unknown', True, 'unverified')),
	(('legacy-saved-two-primaries', 'G6'), (15, 'unverified', 'legacy-target-unknown', True, 'unverified')),
	(('legacy-saved-non-rigid', 'G6'), (15, 'unverified', 'non-rigid:orthonormal rotation', True, 'unverified')),
	(('row16-marked-unmarked-item', 'A2'), (16, 'unverified', 'no-marker', True, 'unverified')),
	(('legacy', 'G6'), (17, 'legacy', 'legacy-provenance', False, 'legacy')),
	(('legacy-equipment-dev-code', 'G6'), (17, 'legacy', 'legacy-provenance', False, 'legacy')),
	(('legacy-equipment-missing-code', 'G6'), (17, 'legacy', 'legacy-provenance', False, 'legacy')),
	(('exc-two-reference-legacy', 'A2'), (18, 'unverified', 'legacy-target-ambiguous', True, 'unverified')),
	(('exc-foreign', 'A2'), (18, 'unverified', 'no-marker', True, 'unverified')),
	(('legacy-equipment-dev', 'G6'), (18, 'unverified', 'no-marker', True, 'unverified')),
	(('legacy-equipment-missing', 'G6'), (18, 'unverified', 'no-marker', True, 'unverified')),
	(('legacy-equipment-foreign', 'G6'), (18, 'unverified', 'no-marker', True, 'unverified')),
	(('legacy-equipment-foreign-code', 'G6'), (18, 'unverified', 'no-marker', True, 'unverified')),
	(('legacy-equipment-post-boundary', 'G6'), (18, 'unverified', 'no-marker', True, 'unverified')),
	(('legacy-equipment-post-boundary-code', 'G6'), (18, 'unverified', 'no-marker', True, 'unverified')),
	(('legacy-non-rigid', 'G6'), (18, 'unverified', 'non-rigid:orthonormal rotation', True, 'unverified')),
))

# Declarations a client save of the scenario writes
DECLARATION_ORACLE = OrderedDict((
	('exc-intact', dict(primary=['P'], legacy=[], unverified=[])),
	('exc-quarantined-edge', dict(primary=['P'], legacy=[], unverified=['A2'])),
	('exc-quarantined-root', dict(primary=['P'], legacy=[], unverified=['R3'])),
	('exc-intermediate-declared', dict(primary=['P'], legacy=[], unverified=['T'])),
	('exc-intermediate-non-rigid', dict(primary=['P'], legacy=[], unverified=['T'])),
	('exc-dangling-parent', dict(primary=['P'], legacy=[], unverified=['A2', 'Y'])),
	('legacy', dict(primary=['P'], legacy=['G6'], unverified=[])),
	('legacy-save1', dict(primary=['P'], legacy=['G6'], unverified=[])),
	('legacy-saved-conflict', dict(primary=['P'], legacy=[], unverified=['G6'])),
	('row5-orphan-legacy', dict(primary=['P'], legacy=[], unverified=['A2'])),
	('exc-foreign', dict(primary=[], legacy=[], unverified=['A2'])),
))


class FramesCaseTableTestCase(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		logging.getLogger('sonador').setLevel(logging.CRITICAL)
		cls.table = F.case_table(built())

	def test_every_row_of_the_classification_table(self):
		self.assertEqual(sorted(set(row for row, *_ in CASE_ORACLE.values())), list(range(1, 19)))
		for (name, alias), (row, trust, reason, quarantined, written) in CASE_ORACLE.items():
			entry = self.table['scenarios'][name]['frames'][alias]
			self.assertEqual((entry['row'], entry['trust'], entry['reason'], entry['quarantined'], entry['written_back']),
				(row, trust, reason, quarantined, written), (name, alias))

	def test_declarations_written(self):
		for name, expected in DECLARATION_ORACLE.items():
			self.assertEqual(dict(self.table['scenarios'][name]['declarations_written']), expected, name)

	def test_established_and_mentioned(self):
		dangling = self.table['scenarios']['exc-dangling-parent']['frames']
		self.assertEqual((dangling['T']['evidence'], dangling['T']['established']), (['mention'], False))
		self.assertEqual(dangling['A2']['target'], 'T')
		orphan = self.table['scenarios']['row5-orphan-legacy']['frames']['A2']
		self.assertEqual((orphan['evidence'], orphan['established'], orphan['bucket']), (['declaration'], False, 'document'))

	def test_caller_changed_primary(self):
		entry = self.table['scenarios']['legacy-saved-caller-primary']
		self.assertEqual((entry['primary'], entry['persisted_primary'], entry['save']), ('S', 'P', 'refused: legacy-target-change'))
		self.assertEqual(entry['frames']['G6']['target'], 'P')

	def test_invalid_and_duplicated_primary(self):
		for name in ('primary-invalid', 'primary-duplicated'):
			entry = self.table['scenarios'][name]
			self.assertIsNone(entry['primary'], name)
			self.assertIn('warning frame-primary-invalid', ' '.join(entry['diagnostics']))


class FramesFixtureTestCase(unittest.TestCase):
	def test_committed_fixtures_reproduce(self):
		with tempfile.TemporaryDirectory() as directory:
			done = F.write_fixtures(directory)
			committed = sorted(os.listdir(F.FIXTURES_DIR))
			self.assertEqual(sorted(os.listdir(directory)), committed)
			for name in committed:
				with open(os.path.join(directory, name), 'rb') as a, open(os.path.join(F.FIXTURES_DIR, name), 'rb') as b:
					self.assertEqual(a.read(), b.read(), name)
		self.assertEqual(len(done) + 1, len(committed))

	def test_case_table_names_every_fixture(self):
		with open(os.path.join(F.FIXTURES_DIR, F.CASES_FILE)) as handle:
			table = json.load(handle)
		for name, entry in table['scenarios'].items():
			self.assertTrue(os.path.exists(os.path.join(F.FIXTURES_DIR, entry['fixture'])), name)
			self.assertTrue(entry['changes'], name)

	def test_committed_inputs_read_as_built(self):
		for name in ('exc-intact', 'legacy', 'exc-intermediate-non-rigid'):
			committed = pydicom.dcmread(os.path.join(F.FIXTURES_DIR, F.fixture_name(name)))
			scenario = built()[name]
			report = F.read_report(committed, scenario.referenced, scenario.root)
			expected = F.read_report(scenario.dataset, scenario.referenced, scenario.root)
			self.assertEqual(dict((u, (c.row, c.state, c.reason)) for u, c in report.frames.document.classification.items()),
				dict((u, (c.row, c.state, c.reason)) for u, c in expected.frames.document.classification.items()))
