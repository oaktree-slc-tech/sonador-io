'''	Offline tests of landmark sets (oak-tree/medical-imaging/sonador-client#47): the model, the label and reserved-value
	rules, the reader and writer of the 0.3 group, preserved content, frames, series attribution and accessors, the
	reader outcomes and conditions, and legacy point groups with their migration.

	Label cases go through real serialization (`disk`), not model round trips. Fixtures: `ftests/test_sr/fixtures`,
	written by `landmarks_fixtures` (each fixture's root title and template are recorded there) and compared byte for
	byte in `LandmarkFixtureTestCase`.

	Run from the directory containing the `sonador` package: python -m unittest sonador.ftests.test_sr.sr_landmarks
'''
import copy, io, logging, os, tempfile, unittest
from collections import OrderedDict

import numpy as np
import pydicom
from pydicom.dataset import Dataset
from pydicom.sequence import Sequence
from pydicom.sr.codedict import codes

from highdicom.sr.templates import Code, CodedConcept

from ...apisettings.base import Landmark, LandmarkSet, LandmarkSetGroup, RawReportGroup, ImageTransformMatrix, ImageReference, \
	ReferenceFrame, FrameConversionError, DCMSR_LANDMARK, DCMSR_LANDMARK_POINT, DCMSR_LANDMARK_ANATOMIC, DCMSR_LANDMARK_REGISTRATION, \
	DCMSR_FINDING, DCMSR_FRAME_NATIVE, DCMSR_FRAME_REGISTERED, DCMSR_FRAME_UNREGISTERED, DCMSR_FRAME_NOT_APPLICABLE, code_key, \
	free_text_finding
from ...apisettings.display_attrs import DisplayAttributes
from ...apisettings.sr import SRDiagnosticError, sr_diagnostics_with, srcontent_is_named, dcm_code_value, srcode2dataset, \
	DCMSR_SCOORD3D, DCMSR_REPORT_VOLUME_MEAUREMENTS, \
	SR_CONDITION_LANDMARK_RESERVED_CONCEPT, SR_CONDITION_LANDMARK_LABEL_UNREPRESENTABLE, SR_CONDITION_LANDMARK_LABEL_DUPLICATE, \
	SR_CONDITION_LANDMARK_LABEL_DUPLICATE_DROPPED, SR_CONDITION_LANDMARK_TYPE_ABSENT, SR_CONDITION_LANDMARK_TYPE_UNKNOWN, \
	SR_CONDITION_LANDMARK_PIXEL_ORIGIN_UNSUPPORTED, SR_CONDITION_LANDMARK_SET_EMPTY, SR_CONDITION_LANDMARK_SET_READ_AS_GROUP, \
	SR_CONDITION_LANDMARK_SET_WITHOUT_NAME, SR_CONDITION_LANDMARK_WITHOUT_POINT, SR_CONDITION_LANDMARK_POINT_AMBIGUOUS, \
	SR_CONDITION_LANDMARK_POINT_INVALID, SR_CONDITION_LANDMARK_IMAGE_REFERENCE_MISSING, SR_CONDITION_LANDMARK_IMAGE_REFERENCE_AMBIGUOUS, \
	SR_CONDITION_LANDMARK_POINT_OUT_OF_BOUNDS, SR_CONDITION_LANDMARK_SET_MIXED_FRAMES, SR_CONDITION_DUPLICATE_FIDUCIAL_UID, \
	SR_CONDITION_LANDMARK_IDENTITY_PROVISIONAL, SR_CONDITION_LANDMARK_SOURCE_SERIES_UNKNOWN, SR_CONDITION_LANDMARK_CONVERSION_OPAQUE_GEOMETRY, \
	SR_CONDITION_LANDMARK_LEGACY_COLLECTION, SR_CONDITION_LANDMARK_LEGACY_MIGRATED, SR_CONDITION_LANDMARK_LEGACY_NOT_MIGRATABLE, \
	SR_CONDITION_LANDMARK_SET_INVALID, \
	SR_CONDITION_OVERLENGTH_CODE_MEANING, SR_CONDITION_BY_REFERENCE_ITEM, SR_CONDITION_UNKNOWN_FRAME, SR_CONDITION_FRAME_POLICY_REFUSED, \
	SR_CONDITION_UNRESOLVED_REFERENCE, SR_CONDITION_INVALID_DISPLAY_COLOR, SR_CONDITION_SHARED_TRACKING_UID
from ...apisettings.sr.seriestag import DCMSR_MEASUREMENT_TEXT, DCMSR_MEASUREMENT_DESCRIPTION, DCMSR_DISPLAY_ATTRIBUTES
from ...imaging.helpers.sr import SonadorComprehensiveSR3D, SonadorImagingMeasurementReport
from ...imaging.helpers.sr.parse import srdoc_measurement_groups, srdoc_children
from ...apisettings.sr.landmarks import srparse_landmark_set_group, ImageMetadata
from . import frames_fixtures as F
from . import landmarks_fixtures as L


__all__ = ['LandmarkLabelTestCase', 'LandmarkEncodingTestCase', 'LandmarkModelTestCase', 'LandmarkSeriesTestCase', 'LandmarkFramesTestCase',
	'LandmarkRetainedItemTestCase', 'LandmarkPreservedContentTestCase', 'LandmarkOutcomeTestCase', 'LandmarkConditionTestCase',
	'LandmarkIdentityTestCase', 'LandmarkLegacyTestCase', 'LandmarkFixtureTestCase']


P, S = F.P_FOR, F.S_FOR
REFS = F.references_map(('P', 'S'))
OTHER = Code('Sonador-Landmark.Point', '99OTHER', 'Point')
OTHER_TEXT = Code('CORNERSTONEFREETEXT', '99OTHER', 'Free text')
P_IMAGE = ImageReference(F.MR_IMAGE_STORAGE, L.P_SOP, None, L.P_SERIES)
MULTIFRAME = '1.2.840.10008.5.1.4.1.1.4.1'
WHOLE_SLIDE = '1.2.840.10008.5.1.4.1.1.77.1.6'


def setUpModule():
	logging.getLogger('sonador').setLevel(logging.CRITICAL)


def tearDownModule():
	logging.getLogger('sonador').setLevel(logging.NOTSET)


_BUILT = {}


def fixture(name):
	'''	A fresh copy of a fixture dataset, built once per run.
	'''
	if not _BUILT:
		_BUILT.update(L.build_all())
	return copy.deepcopy(_BUILT[name])


def disk(doc):
	buf = io.BytesIO()
	pydicom.dcmwrite(buf, doc)
	buf.seek(0)
	return pydicom.dcmread(buf)


def read(ds, root='exchange', **kwargs):
	cls = SonadorImagingMeasurementReport if root == 'exchange' else SonadorComprehensiveSR3D
	kwargs.setdefault('imaging_frames', REFS)
	return cls.from_sr(copy.deepcopy(ds), **kwargs)


def new_report(root='exchange', referenced=('P', 'S'), **kwargs):
	return L._report(root, referenced, **kwargs)


def write(lsets, root='exchange', **kwargs):
	report = new_report(root, **kwargs)
	report.landmarks = list(lsets)
	return disk(report.create_sr())


def conditions(report, condition):
	return sr_diagnostics_with(report.diagnostics, condition=condition)


def groups(ds):
	return srdoc_measurement_groups(ds)


def set_group(ds, tracking_uid):
	for group in groups(ds):
		for child in group.ContentSequence:
			if srcontent_is_named(child, codes.DCM.TrackingUniqueIdentifier) and str(child.UID) == tracking_uid:
				return group
	raise KeyError(tracking_uid)


def finding_of(group):
	return [c for c in group.ContentSequence if c.ValueType == 'CODE' and srcontent_is_named(c, DCMSR_FINDING)]


def landmark_items(group):
	return [c for c in group.ContentSequence if c.ValueType == 'CODE' and srcontent_is_named(c, DCMSR_LANDMARK)]


def coordinate_of(item):
	return next(c for c in item.ContentSequence if c.ValueType in ('SCOORD', 'SCOORD3D'))


def text_children(item, concept=DCMSR_MEASUREMENT_TEXT):
	return [str(c.TextValue) for c in item.get('ContentSequence') or [] if srcontent_is_named(c, concept)]


def value_key(item):
	return (str(item.ConceptCodeSequence[0].CodingSchemeDesignator), dcm_code_value(item.ConceptCodeSequence[0]))


def item_bytes(item):
	from pydicom.filewriter import write_dataset
	from pydicom.filebase import DicomBytesIO
	out = DicomBytesIO()
	out.is_little_endian, out.is_implicit_VR = True, False
	write_dataset(out, item)
	return out.getvalue()


def single(point=(1.0, 2.0, 3.0), frame=P, name='Single', **kwargs):
	return LandmarkSet.from_points([point], name=name, frame_of_reference_uid=frame if len(point) == 3 else None,
		reference=P_IMAGE if len(point) == 2 else None, **kwargs)


def round_trip_landmark(landmark, frame=P):
	'''	The landmark as read back from a document holding it in a one-landmark set, written to bytes and read.
	'''
	lset = LandmarkSet([landmark], label='Holder', frame_of_reference_uid=frame)
	report = read(write([lset]))
	return report.landmarks[0].landmarks[0], report


# ---- labels (3.1 label table) ----


class LandmarkLabelTestCase(unittest.TestCase):
	ROWS = OrderedDict((
		('leading whitespace', (' leading', True, False)),
		('trailing whitespace', ('trailing ', False, False)),
		('backslash', ('back\\slash', True, False)),
		('line break', ('two\r\nlines', True, False)),
		('tab', ('tab\there', False, False)),
		('form feed', ('form\x0cfeed', False, False)),
		('inner spaces', ('inner  double  spaces', True, True)),
		('more than 64 characters', ('x' * 70, True, True)),
	))

	def test_each_row_in_both_places(self):
		for row, (label, beside_concept, without_concept) in self.ROWS.items():
			for concept, allowed in ((L.ASIS, beside_concept), (None, without_concept)):
				if allowed:
					landmark, _ = round_trip_landmark(Landmark((1, 2, 3), concept=concept, label=label))
					self.assertEqual(landmark.label, label, (row, concept))
					self.assertEqual(code_key(landmark.concept) if landmark.concept else None, code_key(concept) if concept else None)
				else:
					with self.assertRaises(SRDiagnosticError) as caught:
						Landmark((1, 2, 3), concept=concept, label=label)
					self.assertEqual(caught.exception.diagnostic.condition, SR_CONDITION_LANDMARK_LABEL_UNREPRESENTABLE, (row, concept))
		# The same rules at set level
		with self.assertRaises(SRDiagnosticError):
			LandmarkSet([Landmark((1, 2, 3))], label=' leading', frame_of_reference_uid=P)
		lset = LandmarkSet([Landmark((1, 2, 3))], concept=L.PELVIS, label=' leading', frame_of_reference_uid=P)
		self.assertEqual(read(write([lset])).landmarks[0].label, ' leading')

	def test_removing_the_concept_of_a_text_only_label_refused_whole(self):
		landmark = Landmark((1, 2, 3), concept=L.ASIS, label='back\\slash')
		with self.assertRaises(SRDiagnosticError) as caught:
			landmark.concept = None
		self.assertEqual(caught.exception.diagnostic.condition, SR_CONDITION_LANDMARK_LABEL_UNREPRESENTABLE)
		self.assertEqual((code_key(landmark.concept), landmark.label), (code_key(L.ASIS), 'back\\slash'))
		lset = LandmarkSet([Landmark((1, 2, 3))], concept=L.PELVIS, label='two\r\nlines', frame_of_reference_uid=P)
		with self.assertRaises(SRDiagnosticError):
			lset.concept = None
		self.assertEqual(lset.label, 'two\r\nlines')

	def test_blank_label_is_absent(self):
		for blank in ('', '   ', '\t'):
			self.assertIsNone(Landmark((1, 2, 3), label=blank).label)
		landmark = Landmark((1, 2, 3), label='kept')
		landmark.label = ' '
		self.assertIsNone(landmark.label)

	def test_new_scoord_written_with_frame_and_imported_as_read(self):
		ds = fixture('image')
		group = set_group(ds, L.SET_UIDS['image'])
		self.assertTrue(all(coordinate_of(i).PixelOriginInterpretation == 'FRAME' for i in landmark_items(group)))
		for item in landmark_items(group):
			del coordinate_of(item).PixelOriginInterpretation
		report = read(ds)
		self.assertFalse(conditions(report, SR_CONDITION_LANDMARK_PIXEL_ORIGIN_UNSUPPORTED))
		again = disk(report.create_sr())
		self.assertTrue(all('PixelOriginInterpretation' not in coordinate_of(i) for i in landmark_items(set_group(again, L.SET_UIDS['image']))))
		report.landmarks[0].landmarks[0].point = (11.0, 21.0)
		edited = disk(report.create_sr())
		self.assertEqual(coordinate_of(landmark_items(set_group(edited, L.SET_UIDS['image']))[0]).PixelOriginInterpretation, 'FRAME')

	def test_set_converted_to_its_own_frame(self):
		report = read(fixture('exchange'))
		lset = report.landmarks[0]
		copied = report.convert(lset, P)
		self.assertIsNot(copied, lset)
		self.assertEqual(copied.points, lset.points)
		self.assertEqual([l.fiducial_uid for l in copied.landmarks], [l.fiducial_uid for l in lset.landmarks])


# ---- 3.1 encoding ----


class LandmarkEncodingTestCase(unittest.TestCase):
	def assert_tree(self, ds, tracking_uid, lset):
		group = set_group(ds, tracking_uid)
		self.assertEqual(len(finding_of(group)), 1)
		self.assertEqual(finding_of(group)[0].RelationshipType, 'CONTAINS')
		items = landmark_items(group)
		self.assertEqual([str(coordinate_of(i).FiducialUID) for i in items], [l.fiducial_uid for l in lset.landmarks])
		for item in items:
			self.assertEqual(item.RelationshipType, 'CONTAINS')
			self.assertEqual(coordinate_of(item).RelationshipType, 'INFERRED FROM')
			for child in item.ContentSequence:
				if child.ValueType in ('TEXT', 'CONTAINER', 'UIDREF'):
					self.assertEqual(child.RelationshipType, 'HAS PROPERTIES')
		for child in finding_of(group)[0].get('ContentSequence') or []:
			self.assertEqual(child.RelationshipType, 'HAS PROPERTIES')

	def test_content_tree_oracle_for_each_fixture(self):
		for name, uids in (('patient', ['patient']), ('exchange', ['patient']), ('mixed', ['patient']), ('image', ['image', 'image-second']),
				('application-frame', ['application', 'root'])):
			ds = fixture(name)
			report = read(ds, root=L.ROOTS[name])
			resaved = disk(report.create_sr())
			for key, lset in zip(uids, report.landmarks):
				self.assert_tree(ds, L.SET_UIDS[key], lset)
				self.assertEqual(item_bytes(set_group(resaved, L.SET_UIDS[key])), item_bytes(set_group(ds, L.SET_UIDS[key])), name)
			# An edited set keeps the tree, the order and the identities
			first = report.landmarks[0]
			first.description = 'edited'
			edited = disk(report.create_sr())
			self.assert_tree(edited, L.SET_UIDS[uids[0]], first)

	def test_value_table_at_both_levels(self):
		ds = fixture('patient')
		group = set_group(ds, L.SET_UIDS['patient'])
		items = landmark_items(group)
		# concept and label / label only / concept only / concept and label / neither
		self.assertEqual((value_key(items[0]), text_children(items[0])), (code_key(L.ASIS), ['ASIS (left)']))
		self.assertEqual((value_key(items[1]), text_children(items[1])), (('CST4', 'CORNERSTONEFREETEXT'), []))
		self.assertIn('LongCodeValue', items[1].ConceptCodeSequence[0])
		self.assertEqual(str(items[1].ConceptCodeSequence[0].CodeMeaning), 'Label only')
		self.assertEqual((value_key(items[2]), text_children(items[2])), (code_key(L.PUBIC), []))
		self.assertEqual((value_key(items[4]), text_children(items[4])), (code_key(DCMSR_LANDMARK_POINT), []))
		finding = finding_of(group)[0]
		self.assertEqual((value_key(finding), text_children(finding)), (code_key(L.PELVIS), ['Pelvis (planning)']))
		parsed = read(ds).landmarks[0]
		self.assertEqual([(code_key(l.concept) if l.concept else None, l.label) for l in parsed.landmarks][:5], [
			(code_key(L.ASIS), 'ASIS (left)'), (None, 'Label only'), (code_key(L.PUBIC), None), (code_key(L.ASIS), 'Concept and label'),
			(None, None)])
		for concept, label in ((L.PELVIS, None), (None, 'Label set'), (L.PELVIS, 'Both')):
			lset = LandmarkSet([Landmark((1, 2, 3))], concept=concept, label=label, frame_of_reference_uid=P)
			back = read(write([lset])).landmarks[0]
			self.assertEqual((code_key(back.concept) if back.concept else None, back.label), (code_key(concept) if concept else None, label))

	def test_reserved_codes(self):
		for concept in (DCMSR_LANDMARK_POINT, free_text_finding('Anything')):
			for label in (None, 'With label'):
				with self.assertRaises(SRDiagnosticError) as caught:
					Landmark((1, 2, 3), concept=concept, label=label)
				self.assertEqual(caught.exception.diagnostic.condition, SR_CONDITION_LANDMARK_RESERVED_CONCEPT)
		with self.assertRaises(SRDiagnosticError):
			LandmarkSet([Landmark((1, 2, 3))], concept=free_text_finding('Set'), frame_of_reference_uid=P)
		lset = LandmarkSet([Landmark((1, 2, 3), concept=OTHER), Landmark((2, 3, 4), concept=OTHER_TEXT)], concept=DCMSR_LANDMARK_POINT,
			frame_of_reference_uid=P)
		back = read(write([lset])).landmarks[0]
		self.assertEqual(code_key(back.concept), code_key(DCMSR_LANDMARK_POINT))
		self.assertEqual([code_key(l.concept) for l in back.landmarks], [code_key(OTHER), code_key(OTHER_TEXT)])
		landmark = Landmark((1, 2, 3))
		with self.assertRaises(SRDiagnosticError):
			landmark.concept = DCMSR_LANDMARK_POINT

	def test_concept_added_and_removed_keeps_the_label(self):
		landmark, report = round_trip_landmark(Landmark((1, 2, 3), label='Kept label'))
		landmark.concept = L.ASIS
		back = read(disk(report.create_sr())).landmarks[0].landmarks[0]
		self.assertEqual((code_key(back.concept), back.label), (code_key(L.ASIS), 'Kept label'))
		back.concept = None
		report2 = read(write([LandmarkSet([back], label='Holder', frame_of_reference_uid=P)]))
		self.assertEqual((report2.landmarks[0].landmarks[0].concept, report2.landmarks[0].landmarks[0].label), (None, 'Kept label'))

	def test_point_value_with_a_text_child_read_and_kept(self):
		ds = fixture('exchange')
		group = set_group(ds, L.SET_UIDS['patient'])
		item = landmark_items(group)[4]
		text = Dataset()
		text.RelationshipType, text.ValueType, text.TextValue = 'HAS PROPERTIES', 'TEXT', 'Named point'
		text.ConceptNameCodeSequence = Sequence([srcode2dataset(DCMSR_MEASUREMENT_TEXT)])
		item.ContentSequence.append(text)
		before = item_bytes(item)
		report = read(ds)
		landmark = report.landmarks[0].landmarks[4]
		self.assertEqual((landmark.concept, landmark.label), (None, 'Named point'))
		report.landmarks[0].description = 'set edited, landmark not'
		after = landmark_items(set_group(disk(report.create_sr()), L.SET_UIDS['patient']))[4]
		self.assertEqual(item_bytes(after), before)

	def test_label_lengths_and_utf8(self):
		for length, overlength in ((64, False), (65, True)):
			label = ('é' * length)
			landmark, report = round_trip_landmark(Landmark((1, 2, 3), label=label))
			self.assertEqual(landmark.label, label)
			report2 = new_report()
			report2.landmarks = [LandmarkSet([Landmark((1, 2, 3), label=label)], label='Holder', frame_of_reference_uid=P)]
			doc = disk(report2.create_sr())
			self.assertEqual(bool(conditions(report2, SR_CONDITION_OVERLENGTH_CODE_MEANING)), overlength)
			self.assertEqual(str(doc.SpecificCharacterSet), 'ISO_IR 192')
		long_label = 'y' * 300
		landmark, _ = round_trip_landmark(Landmark((1, 2, 3), concept=L.ASIS, label=long_label))
		self.assertEqual(landmark.label, long_label)
		landmark, _ = round_trip_landmark(Landmark((1, 2, 3), concept=L.ASIS, label='a\\b'))
		self.assertEqual(landmark.label, 'a\\b')

	def test_landmark_types(self):
		ds = fixture('exchange')
		group = set_group(ds, L.SET_UIDS['patient'])
		items = landmark_items(group)
		del coordinate_of(items[1]).ConceptNameCodeSequence
		coordinate_of(items[2]).ConceptNameCodeSequence = Sequence([srcode2dataset(Code('CUSTOM', '99OTHER', 'Custom type'))])
		coordinate_of(items[3]).ConceptNameCodeSequence = Sequence([srcode2dataset(Code('Sonador-Landmark.Anatomic', '99OTHER', 'Anatomic'))])
		report = read(ds)
		landmarks = report.landmarks[0].landmarks
		self.assertEqual(code_key(landmarks[1].landmark_type), code_key(DCMSR_LANDMARK_POINT))
		self.assertTrue(conditions(report, SR_CONDITION_LANDMARK_TYPE_ABSENT))
		self.assertEqual(code_key(landmarks[2].landmark_type), ('99OTHER', 'CUSTOM'))
		self.assertEqual(code_key(landmarks[3].landmark_type), ('99OTHER', 'Sonador-Landmark.Anatomic'))
		self.assertEqual(len(conditions(report, SR_CONDITION_LANDMARK_TYPE_UNKNOWN)), 2)
		self.assertEqual([code_key(l.landmark_type) for l in (landmarks[0], landmarks[4], landmarks[9])],
			[code_key(DCMSR_LANDMARK_ANATOMIC), code_key(DCMSR_LANDMARK_POINT), code_key(DCMSR_LANDMARK_REGISTRATION)])
		# Absent: written back as read while unchanged, with the Point code once edited; unknown kept through an edit
		report.landmarks[0].description = 'edited'
		again = landmark_items(set_group(disk(report.create_sr()), L.SET_UIDS['patient']))
		self.assertNotIn('ConceptNameCodeSequence', coordinate_of(again[1]))
		landmarks[1].label = 'now edited'
		landmarks[2].label = 'edited too'
		again = landmark_items(set_group(disk(report.create_sr()), L.SET_UIDS['patient']))
		self.assertEqual(dcm_code_value(coordinate_of(again[1]).ConceptNameCodeSequence[0]), DCMSR_LANDMARK_POINT.value)
		self.assertEqual(dcm_code_value(coordinate_of(again[2]).ConceptNameCodeSequence[0]), 'CUSTOM')


# ---- 3.2 model ----


class LandmarkModelTestCase(unittest.TestCase):
	def test_worked_example(self):
		report = new_report(referenced=('P',))
		landmarks = LandmarkSet.from_points(
			[(10.0, 20.0, 30.0), (12.0, 24.0, 31.0), (15.0, 22.0, 29.0)],
			name='Pelvic landmarks', description='Points picked on the planning model',
			names=[L.ASIS, 'Tear drop', None], labels=['ASIS (left)', None, None],
			landmark_type=DCMSR_LANDMARK_ANATOMIC, landmark_types=[None, None, DCMSR_LANDMARK_REGISTRATION],
			display=DisplayAttributes(color={'Line': '#00C0FF', 'Text': '#FFFFFF'}),
			displays=[None, DisplayAttributes(color={'Line': '#FF8000'}), None],
			frame_of_reference_uid=report.frames.primary_uid, series_uid=L.P_SERIES)
		report.landmarks = [landmarks]
		dataset = disk(report.create_sr())
		reread = SonadorImagingMeasurementReport.from_sr(dataset, imaging_frames=F.references_map(('P',)))
		parsed = reread.landmarks[0]
		self.assertEqual(len(parsed.landmarks.find(name='Tear drop')), 1)
		self.assertIs(parsed.landmarks.find(name='ASIS (left)')[0], parsed.landmarks[0])
		self.assertIs(parsed.landmarks.find(name=L.ASIS.meaning)[0], parsed.landmarks[0])
		first = parsed.landmarks.by_fiducial_uid(landmarks.landmarks[0].fiducial_uid)
		self.assertEqual(parsed.effective_display(parsed.landmarks[1]).hex, OrderedDict((('Line', '#FF8000'), ('Text', '#FFFFFF'))))
		self.assertEqual([l.landmark_type.meaning for l in parsed.landmarks], ['Anatomic', 'Anatomic', 'Registration'])
		self.assertFalse(parsed.edited)
		first.label = 'ASIS, left'
		parsed.landmarks.move(parsed.landmarks[2], 0)
		self.assertTrue(parsed.edited)
		again = SonadorImagingMeasurementReport.from_sr(disk(reread.create_sr()), imaging_frames=F.references_map(('P',))).landmarks[0]
		self.assertEqual([l.fiducial_uid for l in again.landmarks], [landmarks.landmarks[i].fiducial_uid for i in (2, 0, 1)])
		self.assertEqual(again.landmarks[1].label, 'ASIS, left')

	def test_edit_of_a_document_read_in_memory(self):
		# The dataset create_sr returns holds highdicom content items; reading it back and editing must work as from a file
		report = new_report(referenced=('P',))
		lset = LandmarkSet.from_points([(1.0, 2.0, 3.0), (4.0, 5.0, 6.0)], name='In memory', names=['A', None],
			frame_of_reference_uid=P, series_uid=L.P_SERIES)
		report.landmarks = [lset]
		reread = SonadorImagingMeasurementReport.from_sr(report.create_sr(), imaging_frames=F.references_map(('P',)))
		parsed = reread.landmarks[0]
		parsed.landmarks[0].label = 'B'
		parsed.landmarks[1].landmark_type = DCMSR_LANDMARK_REGISTRATION
		parsed.landmarks.move(parsed.landmarks[1], 0)
		again = read(disk(reread.create_sr())).landmarks[0]
		self.assertEqual([l.label for l in again.landmarks], [None, 'B'])
		self.assertEqual(again.landmarks[0].landmark_type.meaning, 'Registration')

	def test_reading_never_edits(self):
		for name, root in (('exchange', 'exchange'), ('legacy', 'application')):
			ds = fixture(name)
			report = read(ds, root=root)
			lset = report.landmarks[0]
			list(lset.landmarks), len(lset.landmarks), lset.landmarks[0], lset.array, lset.landmarks.find(name='x')
			lset.landmarks.by_fiducial_uid(lset.landmarks[0].fiducial_uid)
			lset.effective_display(lset.landmarks[0])
			self.assertFalse(lset.edited, name)
			resaved = disk(report.create_sr())
			if name == 'legacy':
				self.assertFalse(conditions(report, SR_CONDITION_LANDMARK_LEGACY_MIGRATED))
				self.assertEqual(len(groups(resaved)), 0)
			else:
				self.assertEqual(item_bytes(set_group(resaved, L.SET_UIDS['patient'])), item_bytes(set_group(ds, L.SET_UIDS['patient'])))

	def test_display_role_change_edits(self):
		lset = read(fixture('exchange')).landmarks[0]
		lset.display.color['Line'] = DisplayAttributes._rgb('#123456')
		self.assertTrue(lset.edited)
		lset = read(fixture('exchange')).landmarks[0]
		lset.landmarks[5].display.color['Line'] = DisplayAttributes._rgb('#123456')
		self.assertTrue(lset.edited)

	def test_metre_transform_applied_once(self):
		transform = ImageTransformMatrix(np.array([[1, 0, 0, 10.0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]), source_frame='planner',
			target_frame=P, source_unit='m')
		lset = LandmarkSet.from_points([(0.001, 0.002, 0.003)], name='Metres', transform=transform)
		self.assertTrue(np.allclose(lset.landmarks[0].point, (11.0, 2.0, 3.0)))
		self.assertEqual(lset.frame_of_reference_uid, P)
		with self.assertRaises(ValueError):
			LandmarkSet.from_points([(0, 0, 0)], name='Scaled', transform=ImageTransformMatrix(np.diag([2.0, 1, 1, 1]), source_frame='p',
				target_frame=P))

	def test_list_lengths_type_fallback_and_supplied_landmarks(self):
		with self.assertRaises(ValueError):
			LandmarkSet.from_points([(0, 0, 0), (1, 1, 1)], name='Set', names=['only one'])
		lset = LandmarkSet.from_points([(0, 0, 0), (1, 1, 1)], name='Set', landmark_type=DCMSR_LANDMARK_ANATOMIC,
			landmark_types=[DCMSR_LANDMARK_REGISTRATION, None], frame_of_reference_uid=P)
		self.assertEqual([l.landmark_type.meaning for l in lset.landmarks], ['Registration', 'Anatomic'])
		given = Landmark((0, 0, 0), label='Given', fiducial_uid='2.25.47401')
		with self.assertRaises(ValueError):
			LandmarkSet.from_points([given], name='Set', names=['Other'])
		with self.assertRaises(ValueError):
			LandmarkSet.from_points([given], name='Set', fiducial_uids=['2.25.47402'])
		filled = LandmarkSet.from_points([given], name='Set', descriptions=['Filled'])
		self.assertEqual((filled.landmarks[0].description, filled.landmarks[0].fiducial_uid), ('Filled', '2.25.47401'))
		with self.assertRaises(ValueError):
			LandmarkSet.from_points([(0, 0, 0)], name='Name', label='Different')

	def test_supplied_landmark_left_unchanged_on_conflict(self):
		given = Landmark((0, 0, 0))
		with self.assertRaises(ValueError):
			LandmarkSet.from_points([given], name='Set', landmark_types=[DCMSR_LANDMARK_REGISTRATION])
		self.assertEqual(code_key(given.landmark_type), code_key(DCMSR_LANDMARK_POINT))
		described = Landmark((0, 0, 0), description='Own')
		with self.assertRaises(ValueError):
			LandmarkSet.from_points([described], name='Set', names=['Filled first'], descriptions=['Contradicts'])
		self.assertEqual((described.label, described.description), (None, 'Own'))
		same = Landmark((0, 0, 0), landmark_type=DCMSR_LANDMARK_ANATOMIC)
		self.assertEqual(LandmarkSet.from_points([same], name='Set', landmark_types=[DCMSR_LANDMARK_ANATOMIC],
			landmark_type=DCMSR_LANDMARK_REGISTRATION).landmarks[0].landmark_type.meaning, 'Anatomic')

	def test_from_points_is_atomic_across_points(self):
		first, second = Landmark((0, 0, 0)), Landmark((1, 1, 1), description='Own')
		with self.assertRaises(ValueError):
			LandmarkSet.from_points([first, second], name='Set', names=['Filled', None], descriptions=[None, 'Contradicts'])
		self.assertEqual((first.label, second.description), (None, 'Own'))
		transform = ImageTransformMatrix(F.SHIFT, source_frame='Planner', target_frame=P)
		with self.assertRaises(SRDiagnosticError):
			LandmarkSet.from_points([first], name=free_text_finding('Reserved'), names=['Filled'], transform=transform)
		self.assertEqual((first.label, tuple(first.point)), (None, (0.0, 0.0, 0.0)))

	def test_from_points_refused_by_the_set_leaves_landmarks_unchanged(self):
		for kwargs in ({'display': {'Line': '#XYZXYZ'}}, {'tracking_id': 'not routable'}, {'label': ' leading'}):
			given = Landmark((0, 0, 0))
			with self.assertRaises((ValueError, SRDiagnosticError)):
				LandmarkSet.from_points([given], name='Set' if 'label' not in kwargs else None, names=['Filled'],
					descriptions=['Described'], frame_of_reference_uid=P, **kwargs)
			self.assertEqual((given.label, given.description), (None, None), kwargs)
		# A frozen supplied landmark refuses its fill, and the other supplied landmark is left as given
		legacy = read(fixture('legacy'), root='application').landmarks[1]
		frozen, free = legacy.landmarks[0], Landmark((1, 1, 1))
		with self.assertRaises(SRDiagnosticError):
			LandmarkSet.from_points([free, frozen], name='Set', names=['Free', 'Frozen'], frame_of_reference_uid=P)
		self.assertEqual((free.label, frozen.label), (None, None))

	def test_from_points_reads_a_one_shot_iterable_once(self):
		lset = LandmarkSet.from_points([(1.0, 2.0, 3.0)], name='Iterated', frame_of_reference_uid=P,
			extra_items=(item for item in [opaque_text('kept under the finding', 'HAS PROPERTIES')]))
		self.assertEqual(len(lset.extra_items), 1)
		lset.tracking_uid = '2.25.47830'
		finding = finding_of(set_group(write([lset]), '2.25.47830'))[0]
		self.assertIn('kept under the finding', [str(c.TextValue) for c in finding.ContentSequence if c.ValueType == 'TEXT'])

	def test_tracking_identifier_routes_to_a_landmark_set(self):
		with self.assertRaises(ValueError):
			LandmarkSet([Landmark((1, 2, 3))], label='Other', tracking_id='cornerstoneTools@^4.0.0:Length', frame_of_reference_uid=P)
		lset = LandmarkSet([Landmark((1, 2, 3))], label='Routed', tracking_id='cornerstoneTools@^4.0.0:SonadorLandmarkSet',
			frame_of_reference_uid=P, tracking_uid='2.25.47820')
		self.assertEqual([s.tracking_uid for s in read(write([lset])).landmarks], ['2.25.47820'])
		parsed = read(fixture('exchange')).landmarks[0]
		with self.assertRaises(ValueError):
			parsed.tracking_id = 'not routable'
		self.assertFalse(parsed.edited)

	def test_one_domain_per_set(self):
		with self.assertRaises(ValueError):
			LandmarkSet([Landmark((1, 2))], label='Both', reference=P_IMAGE, frame_of_reference_uid=P)
		with self.assertRaises(ValueError):
			LandmarkSet.from_points([(1, 2)], name='Both', reference=P_IMAGE, frame_of_reference_uid=P)
		with self.assertRaises(ValueError):
			LandmarkSet.from_points([(1, 2, 3)], name='Moved', reference=P_IMAGE,
				transform=ImageTransformMatrix(F.SHIFT, source_frame='Planner', target_frame=P))
		image = read(fixture('image')).landmarks[0]
		self.assertEqual((image.reference.sop_instance_uid, image.frame_of_reference_uid), (L.P_SOP, None))
		patient = read(fixture('exchange')).landmarks[0]
		self.assertEqual((patient.frame_of_reference_uid, patient.reference), (P, None))
		report = read(fixture('image'))
		report.landmarks[0].frame_of_reference_uid = P
		with self.assertRaises(SRDiagnosticError) as caught:
			report.create_sr()
		self.assertEqual(caught.exception.diagnostic.condition, SR_CONDITION_LANDMARK_SET_MIXED_FRAMES)

	def test_edit_operations(self):
		for operation in (lambda c: c.append(Landmark((9, 9, 9))), lambda c: c.insert(0, Landmark((9, 9, 9))), lambda c: c.remove(c[0]),
				lambda c: c.move(c[0], 3)):
			lset = read(fixture('exchange')).landmarks[0]
			operation(lset.landmarks)
			self.assertTrue(lset.edited)

	def test_effective_display_and_array_leave_members(self):
		lset = read(fixture('exchange')).landmarks[0]
		display = lset.effective_display(lset.landmarks[5])
		self.assertEqual(display.hex, OrderedDict((('Line', '#FF8000'), ('Text', '#FFFFFF'))))
		self.assertEqual(lset.landmarks[5].display.hex, OrderedDict((('Line', '#FF8000'),)))
		array = lset.array
		array[0, 0] = 999.0
		self.assertEqual(lset.landmarks[0].point.x, 10.0)
		self.assertEqual(lset.array.shape, (10, 3))

	def test_lookup(self):
		lset = read(fixture('exchange')).landmarks[0]
		self.assertEqual(len(lset.landmarks.find(name=L.TEARDROP)), 2)
		self.assertEqual(len(lset.landmarks.find(landmark_type=DCMSR_LANDMARK_REGISTRATION)), 1)
		with self.assertRaises(KeyError):
			lset.landmarks.by_fiducial_uid('2.25.47499')
		lset.landmarks[1].fiducial_uid = lset.landmarks[0].fiducial_uid
		with self.assertRaises(LookupError):
			lset.landmarks.by_fiducial_uid(lset.landmarks[0].fiducial_uid)


# ---- 3.3 series attribution and accessors ----


class LandmarkSeriesTestCase(unittest.TestCase):
	def keys(self, ds, root='exchange', referenced=('P', 'S'), **kwargs):
		cls = SonadorImagingMeasurementReport if root == 'exchange' else SonadorComprehensiveSR3D
		series = cls.parse(copy.deepcopy(ds), imaging_frames=F.references_map(referenced), **kwargs)
		found = OrderedDict()
		for key, sets in series.landmarks().items():
			for lset in sets:
				found[lset.tracking_uid] = key
		return found, series, str(ds.SeriesInstanceUID)

	def test_series_rules(self):
		named = LandmarkSet.from_points([(0, 0, 0)], name='Named', frame_of_reference_uid=P, series_uid=L.S_SERIES)
		named.tracking_uid = '2.25.47501'
		unknown = LandmarkSet.from_points([(0, 0, 0)], name='Unknown series', frame_of_reference_uid=P, series_uid='2.25.47599')
		unknown.tracking_uid = '2.25.47502'
		framed = LandmarkSet.from_points([(0, 0, 0)], name='Frame', frame_of_reference_uid=P)
		framed.tracking_uid = '2.25.47503'
		image = LandmarkSet.from_points([(1, 1)], name='Image', reference=ImageReference(F.MR_IMAGE_STORAGE, L.S_SOP, None, L.S_SERIES))
		image.tracking_uid = '2.25.47504'
		report = new_report()
		report.landmarks = [named, framed, image]
		ds = disk(report.create_sr())
		keys, series, document = self.keys(ds)
		self.assertEqual(dict(keys), {'2.25.47501': L.S_SERIES, '2.25.47503': L.P_SERIES, '2.25.47504': L.S_SERIES})
		self.assertEqual(series.landmarks(series_uid=L.P_SERIES)[L.P_SERIES][0].tracking_uid, '2.25.47503')
		self.assertEqual(set(s.tracking_uid for sets in series.landmarks(frame_of_reference_uid=P).values() for s in sets),
			set(['2.25.47501', '2.25.47503']))
		# A Source-Series the document does not reference: the document's series, with the warning
		ds2 = copy.deepcopy(ds)
		group = set_group(ds2, '2.25.47501')
		source = next(c for c in finding_of(group)[0].ContentSequence if c.ValueType == 'UIDREF')
		source.UID = '2.25.47599'
		keys2, series2, document2 = self.keys(ds2)
		self.assertEqual(keys2['2.25.47501'], document2)
		report2 = read(ds2)
		self.assertTrue(conditions(report2, SR_CONDITION_LANDMARK_SOURCE_SERIES_UNKNOWN))
		# An application frame and a frame two referenced series share: the document's series
		keys3, _, document3 = self.keys(fixture('application-frame'), root='application')
		self.assertEqual(set(keys3.values()), set([document3]))
		shared = OrderedDict(((L.P_SERIES, P), (L.S_SERIES, P)))
		series4 = SonadorImagingMeasurementReport.parse(copy.deepcopy(ds), imaging_frames=shared)
		self.assertEqual(next(k for k, sets in series4.landmarks().items() for s in sets if s.tracking_uid == '2.25.47503'),
			str(ds.SeriesInstanceUID))

	def test_findings_and_measurements_never_list_sets(self):
		series = SonadorImagingMeasurementReport.parse(fixture('mixed'), imaging_frames=REFS)
		for by_type in series.findings().values():
			for collection in by_type.values():
				self.assertFalse(any(isinstance(item, LandmarkSet) for item in collection))
		for by_type in series.measurements().values():
			self.assertNotIn('landmark-set', by_type)
		self.assertEqual(sum(len(v) for v in series.landmarks().values()), 1)

	def test_report_holding_only_a_landmark_set(self):
		ds = fixture('image')
		self.assertEqual([str(r.ReferencedSOPSequence[0].ReferencedSOPInstanceUID)
			for e in ds.CurrentRequestedProcedureEvidenceSequence for r in e.ReferencedSeriesSequence], [L.P_SOP, L.S_SOP])
		report = read(ds)
		self.assertEqual([s.reference.series_uid for s in report.landmarks], [L.P_SERIES, L.S_SERIES])

	def test_missing_evidence(self):
		ds = fixture('image')
		evidence = ds.CurrentRequestedProcedureEvidenceSequence[0]
		evidence.ReferencedSeriesSequence = Sequence([s for s in evidence.ReferencedSeriesSequence if str(s.SeriesInstanceUID) != L.S_SERIES])
		keys, _, document = self.keys(ds)
		self.assertEqual(keys[L.SET_UIDS['image-second']], document)
		self.assertTrue(conditions(read(ds), SR_CONDITION_UNRESOLVED_REFERENCE))

	def test_request_budget(self):
		from ...servers import SonadorServer
		from ...imaging.orthanc import ImagingStudyCollection
		from ...test.fakes import IMAGESERVER_JSON
		from .sr_retrieval import MockPacs
		from .sr_seriestag import tobytes

		class Pacs(MockPacs):
			def __init__(self, iserver):
				super().__init__(iserver)
				doc = fixture('exchange')
				self.add('Series', 'sx-lm', {'SeriesInstanceUID': str(doc.SeriesInstanceUID), 'Modality': 'SR', 'SeriesNumber': '47'},
					ParentStudy='study-1', Instances=['sr-lm1'])
				self.add('Instance', 'sr-lm1', {'SOPInstanceUID': str(doc.SOPInstanceUID), 'SOPClassUID': str(doc.SOPClassUID),
					'ContentDate': doc.ContentDate, 'ContentTime': doc.ContentTime, 'InstanceNumber': '1'}, ParentSeries='sx-lm', IndexInSeries=1)
				self.files['sr-lm1'] = tobytes(doc)
				self.resources['study-1']['Series'].append('sx-lm')

		server = SonadorServer('https://sonador.invalid', apitoken='token', verify=False)
		iserver = server.imageserver_modelinstance_from_json(dict(IMAGESERVER_JSON))
		pacs = Pacs(iserver)
		studies = server._init_dataclass_from_json(ImagingStudyCollection, [dict(pacs.resources['study-1'])], pacs=iserver)
		studies.bulkpopulate_related(child_instances=True)
		study = studies[0]
		study.findings()
		before = len(pacs.requests)
		found = study.landmarks()
		self.assertEqual(sum(len(v) for v in found.values()), 1)
		study.landmarks()
		for series in study.series_collection:
			series.landmarks()
		self.assertEqual(len(pacs.requests), before)


# ---- 3.4 frames ----


class LandmarkFramesTestCase(unittest.TestCase):
	def report(self):
		report = new_report()
		graph = report.frames
		graph.add_frame(ReferenceFrame(L.APP_FRAME, name='Anatomic'))
		graph.add_edge(F.RZ, L.APP_FRAME, P)
		graph.add_frame(ReferenceFrame(L.ROOT_FRAME, name='Implant'))
		return report

	def test_representability(self):
		report = self.report()
		native, registered, unregistered = single(frame=P), single(frame=L.APP_FRAME), single(frame=L.ROOT_FRAME)
		self.assertEqual([report.representability(s) for s in (native, registered, unregistered)],
			[DCMSR_FRAME_NATIVE, DCMSR_FRAME_REGISTERED, DCMSR_FRAME_UNREGISTERED])
		self.assertEqual(report.representability(native, target=S), DCMSR_FRAME_UNREGISTERED)
		self.assertEqual(report.representability(single((1.0, 2.0))), DCMSR_FRAME_NOT_APPLICABLE)
		bare = SonadorComprehensiveSR3D(report_procedure=codes.LN.CTUnspecifiedBodyRegion, dcm_evidence=[F.evidence_dataset('P')])
		with self.assertRaises(FrameConversionError):
			bare.representability(single(frame=L.ROOT_FRAME))

	def test_conversion(self):
		report = self.report()
		source = LandmarkSet.from_points([(1, 2, 3), (4, 5, 6)], name='Source', names=[L.ASIS, 'Second'], frame_of_reference_uid=L.APP_FRAME,
			descriptions=['d1', None], displays=[{'Line': '#FF0000'}, None])
		converted = report.convert(source, P)
		self.assertEqual(converted.frame_of_reference_uid, P)
		self.assertTrue(np.allclose(converted.landmarks[0].point, F.RZ.dot((1, 2, 3, 1))[:3]))
		self.assertEqual([l.fiducial_uid for l in converted.landmarks], [l.fiducial_uid for l in source.landmarks])
		self.assertEqual([l.name for l in converted.landmarks], [l.name for l in source.landmarks])
		self.assertEqual(converted.landmarks[0].description, 'd1')
		self.assertEqual(converted.landmarks[0].display.hex['Line'], '#FF0000')
		self.assertEqual(source.frame_of_reference_uid, L.APP_FRAME)
		self.assertEqual(tuple(source.landmarks[0].point), (1.0, 2.0, 3.0))
		with self.assertRaises(FrameConversionError):
			report.convert(single(frame=L.ROOT_FRAME), P)
		with self.assertRaises(FrameConversionError):
			report.convert(single((1.0, 2.0)), P)
		with self.assertRaises(FrameConversionError):
			report.convert(single(frame='2.25.47699'), P)

	def test_metadata_edit_in_an_unknown_frame_and_identity_copy(self):
		ds = fixture('exchange')
		for item in landmark_items(set_group(ds, L.SET_UIDS['patient'])):
			coordinate_of(item).ReferencedFrameOfReferenceUID = '2.25.47695'
		report = read(disk(ds))
		lset = report.landmarks[0]
		lset.description = 'metadata only'
		self.assertFalse(lset.geometry_edited)
		finding = finding_of(set_group(disk(report.create_sr()), L.SET_UIDS['patient']))[0]
		self.assertIn('metadata only', [str(c.TextValue) for c in finding.ContentSequence if c.ValueType == 'TEXT'])
		lset.landmarks[0].point = (1.0, 1.0, 1.0)
		with self.assertRaises(SRDiagnosticError) as caught:
			report.create_sr()
		self.assertEqual(caught.exception.diagnostic.condition, SR_CONDITION_FRAME_POLICY_REFUSED)
		# A copy in the set's own frame keeps the baseline and is written as read
		ds = fixture('exchange')
		report = read(ds)
		copied = report.convert(report.landmarks[0], P)
		self.assertFalse(copied.edited or copied.structure_edited)
		report.landmarks = [copied]
		self.assertEqual(item_bytes(set_group(disk(report.create_sr()), L.SET_UIDS['patient'])), item_bytes(set_group(ds, L.SET_UIDS['patient'])))

	def test_policies(self):
		for frame, require, expected in ((P, 'native', True), (L.APP_FRAME, 'native', False), (L.APP_FRAME, 'registered', True),
				(L.ROOT_FRAME, 'registered', False), (L.ROOT_FRAME, None, True)):
			report = self.report()
			report.landmarks = [single(frame=frame)]
			try:
				report.create_sr(require=require)
				written = True
			except SRDiagnosticError as err:
				self.assertEqual(err.diagnostic.condition, SR_CONDITION_FRAME_POLICY_REFUSED)
				written = False
			self.assertEqual(written, expected, (frame, require))
		report = self.report()
		lset = single(frame=L.APP_FRAME)
		report.landmarks = [report.convert(lset, P)]
		self.assertTrue(report.create_sr(require='native'))

	def test_unknown_frame_and_frame_only_edit(self):
		report = self.report()
		report.landmarks = [single(frame='2.25.47698')]
		with self.assertRaises(SRDiagnosticError) as caught:
			report.create_sr()
		self.assertEqual(caught.exception.diagnostic.condition, SR_CONDITION_FRAME_POLICY_REFUSED)
		parsed = read(fixture('exchange'))
		parsed.landmarks[0].frame_of_reference_uid = '2.25.47697'
		with self.assertRaises(SRDiagnosticError):
			parsed.create_sr()
		ds = fixture('exchange')
		for item in landmark_items(set_group(ds, L.SET_UIDS['patient'])):
			coordinate_of(item).ReferencedFrameOfReferenceUID = '2.25.47696'
		unknown = read(ds)
		self.assertEqual([d.value for d in conditions(unknown, SR_CONDITION_UNKNOWN_FRAME)], ['2.25.47696'])
		self.assertTrue(unknown.create_sr())

	def test_quarantine_across_save_and_reload(self):
		ds = fixture('application-frame')
		F.add_declaration(ds, 'unverified', L.APP_FRAME)
		report = read(ds, root='application')
		lset = report.landmarks[0]
		self.assertEqual(report.representability(lset), DCMSR_FRAME_UNREGISTERED)
		with self.assertRaises(FrameConversionError):
			report.convert(lset, P)
		reloaded = read(disk(report.create_sr()), root='application')
		self.assertTrue(reloaded.frames.frame(L.APP_FRAME).quarantined)
		self.assertEqual(reloaded.representability(reloaded.landmarks[0]), DCMSR_FRAME_UNREGISTERED)

	def test_constructor_transform_recorded_as_an_edge(self):
		report = new_report()
		transform = ImageTransformMatrix(F.RZ, uid='2.25.47601', source_frame='Planner', target_frame=P)
		report.landmarks = [LandmarkSet.from_points([(1, 2, 3)], name='Moved', transform=transform)]
		reloaded = read(disk(report.create_sr()))
		self.assertEqual(reloaded.frames.edges['2.25.47601'].target_uid, P)
		self.assertTrue(np.allclose(reloaded.landmarks[0].landmarks[0].point, F.RZ.dot((1, 2, 3, 1))[:3]))


# ---- retained items ----


class LandmarkRetainedItemTestCase(unittest.TestCase):
	def test_converted_set_serialized(self):
		ds = fixture('application-frame')
		report = read(ds, root='application')
		converted = report.convert(report.landmarks[0], P)
		report.landmarks = [converted, report.landmarks[1]]
		out = disk(report.create_sr())
		items = landmark_items(set_group(out, L.SET_UIDS['application']))
		self.assertEqual(str(coordinate_of(items[0]).ReferencedFrameOfReferenceUID), P)
		self.assertTrue(np.allclose([float(v) for v in coordinate_of(items[0]).GraphicData], F.RZ.dot((1, 2, 3, 1))[:3], atol=1e-4))

	def test_patched_fields_keep_unknown_attributes(self):
		ds = fixture('image')
		items = landmark_items(set_group(ds, L.SET_UIDS['image']))
		for item in items:
			coordinate_of(item).ObservationDateTime = '20261114120000'
			coordinate_of(item).ContentSequence[0].ObservationDateTime = '20261114120001'
		del coordinate_of(items[1]).FiducialUID
		report = read(ds)
		lset = report.landmarks[0]
		self.assertTrue(lset.landmarks[1].provisional)
		lset.landmarks[0].landmark_type = DCMSR_LANDMARK_REGISTRATION
		lset.reference = ImageReference(F.MR_IMAGE_STORAGE, L.S_SOP, None, L.S_SERIES)
		out = landmark_items(set_group(disk(report.create_sr()), L.SET_UIDS['image']))
		first, second = coordinate_of(out[0]), coordinate_of(out[1])
		self.assertEqual(dcm_code_value(first.ConceptNameCodeSequence[0]), DCMSR_LANDMARK_REGISTRATION.value)
		self.assertEqual(str(second.FiducialUID), lset.landmarks[1].fiducial_uid)
		for item in (first, second):
			self.assertEqual(str(item.ObservationDateTime), '20261114120000')
			self.assertEqual(str(item.ContentSequence[0].ReferencedSOPSequence[0].ReferencedSOPInstanceUID), L.S_SOP)
			self.assertEqual(str(item.ContentSequence[0].ObservationDateTime), '20261114120001')

	def test_conversion_refused_with_opaque_geometry(self):
		ds = fixture('application-frame')
		item = landmark_items(set_group(ds, L.SET_UIDS['application']))[1]
		coordinate_of(item).GraphicData = [1.0, 2.0]
		report = read(ds, root='application')
		with self.assertRaises(FrameConversionError) as caught:
			report.convert(report.landmarks[0], P)
		self.assertEqual(caught.exception.reason, SR_CONDITION_LANDMARK_CONVERSION_OPAQUE_GEOMETRY)

	def test_conversion_refused_with_a_coordinate_in_preserved_content(self):
		def opaque_slot(ds, group, items):
			stray = copy.deepcopy(coordinate_of(items[0]))
			stray.RelationshipType = 'CONTAINS'
			group.ContentSequence.append(stray)

		def under_extra_item(ds, group, items):
			comment = opaque_text('holds a point', 'HAS PROPERTIES')
			nested = copy.deepcopy(coordinate_of(items[0]))
			nested.RelationshipType = 'INFERRED FROM'
			comment.ContentSequence = Sequence([nested])
			items[1].ContentSequence.append(comment)

		def under_coordinate(ds, group, items):
			nested = copy.deepcopy(coordinate_of(items[0]))
			nested.RelationshipType = 'HAS PROPERTIES'
			coordinate_of(items[1]).ContentSequence = Sequence([nested])

		for change in (opaque_slot, under_extra_item, under_coordinate):
			ds = fixture('application-frame')
			group = set_group(ds, L.SET_UIDS['application'])
			change(ds, group, landmark_items(group))
			report = read(disk(ds), root='application')
			lset = report.landmarks[0]
			self.assertTrue(lset.has_opaque_geometry, change.__name__)
			with self.assertRaises(FrameConversionError) as caught:
				report.convert(lset, P)
			self.assertEqual(caught.exception.reason, SR_CONDITION_LANDMARK_CONVERSION_OPAQUE_GEOMETRY)
			# A copy in the set's own frame is still allowed: nothing moves
			self.assertFalse(report.convert(lset, L.APP_FRAME).edited)

	def test_conversion_refused_with_a_coordinate_in_display_content(self):
		for level in ('set', 'landmark'):
			ds = fixture('exchange')
			group = set_group(ds, L.SET_UIDS['patient'])
			owner = finding_of(group)[0] if level == 'set' else landmark_items(group)[5]
			display = next(c for c in owner.ContentSequence if srcontent_is_named(c, DCMSR_DISPLAY_ATTRIBUTES))
			stray = copy.deepcopy(coordinate_of(landmark_items(group)[0]))
			stray.RelationshipType = 'CONTAINS'
			display.ContentSequence.append(stray)
			report = read(disk(ds))
			report.frames.add_frame(ReferenceFrame(L.APP_FRAME, name='Anatomic'))
			report.frames.add_edge(F.RZ, L.APP_FRAME, P)
			lset = report.landmarks[0]
			self.assertTrue(lset.has_opaque_geometry, level)
			with self.assertRaises(FrameConversionError) as caught:
				report.convert(lset, L.APP_FRAME)
			self.assertEqual(caught.exception.reason, SR_CONDITION_LANDMARK_CONVERSION_OPAQUE_GEOMETRY)

	def test_duplicate_text_dropped_once_label_edited(self):
		ds = fixture('exchange')
		item = landmark_items(set_group(ds, L.SET_UIDS['patient']))[1]
		text = Dataset()
		text.RelationshipType, text.ValueType, text.TextValue = 'HAS PROPERTIES', 'TEXT', 'Stale'
		text.ConceptNameCodeSequence = Sequence([srcode2dataset(DCMSR_MEASUREMENT_TEXT)])
		item.ContentSequence.append(text)
		report = read(ds)
		self.assertTrue(conditions(report, SR_CONDITION_LANDMARK_LABEL_DUPLICATE))
		self.assertEqual(report.landmarks[0].landmarks[1].label, 'Label only')
		report.landmarks[0].description = 'set edited'
		kept = landmark_items(set_group(disk(report.create_sr()), L.SET_UIDS['patient']))[1]
		self.assertEqual(text_children(kept), ['Stale'])
		report.landmarks[0].landmarks[1].label = 'Renamed'
		dropped = landmark_items(set_group(disk(report.create_sr()), L.SET_UIDS['patient']))[1]
		self.assertEqual(text_children(dropped), [])
		self.assertTrue(conditions(report, SR_CONDITION_LANDMARK_LABEL_DUPLICATE_DROPPED))

	def test_property_layouts(self):
		base = fixture('exchange')
		finding = finding_of(set_group(base, L.SET_UIDS['patient']))[0]
		display = next(c for c in finding.ContentSequence if srcontent_is_named(c, DCMSR_DISPLAY_ATTRIBUTES))
		# flat: the role items directly under the Finding
		flat = copy.deepcopy(base)
		flat_finding = finding_of(set_group(flat, L.SET_UIDS['patient']))[0]
		flat_display = next(c for c in flat_finding.ContentSequence if srcontent_is_named(c, DCMSR_DISPLAY_ATTRIBUTES))
		roles = list(flat_display.ContentSequence[0].ContentSequence)
		for role in roles:
			role.RelationshipType = 'HAS PROPERTIES'
		flat_finding.ContentSequence = Sequence([c for c in flat_finding.ContentSequence if c is not flat_display] + roles)
		# legacy: description and display at group level
		legacy = copy.deepcopy(base)
		group = set_group(legacy, L.SET_UIDS['patient'])
		legacy_finding = finding_of(group)[0]
		moved = [c for c in legacy_finding.ContentSequence if srcontent_is_named(c, DCMSR_MEASUREMENT_DESCRIPTION)
			or srcontent_is_named(c, DCMSR_DISPLAY_ATTRIBUTES)]
		legacy_finding.ContentSequence = Sequence([c for c in legacy_finding.ContentSequence if not any(c is m for m in moved)])
		for item in moved:
			item.RelationshipType = 'CONTAINS'
			group.ContentSequence.append(item)
		for ds in (base, flat, legacy):
			report = read(ds)
			lset = report.landmarks[0]
			self.assertEqual(lset.description, 'Points picked on the planning model')
			self.assertEqual(lset.display.hex, OrderedDict((('Line', '#00C0FF'), ('Text', '#FFFFFF'))))
			self.assertEqual(item_bytes(set_group(disk(report.create_sr()), L.SET_UIDS['patient'])), item_bytes(set_group(ds, L.SET_UIDS['patient'])))
			lset.label = 'Edited'
			nested = finding_of(set_group(disk(report.create_sr()), L.SET_UIDS['patient']))[0]
			self.assertTrue(any(srcontent_is_named(c, DCMSR_DISPLAY_ATTRIBUTES) for c in nested.ContentSequence))

	def test_pixel_origins_and_frame_numbers(self):
		ds = fixture('image')
		items = landmark_items(set_group(ds, L.SET_UIDS['image']))
		coordinate_of(items[0]).PixelOriginInterpretation = 'VOLUME'
		report = read(ds)
		self.assertTrue(conditions(report, SR_CONDITION_LANDMARK_PIXEL_ORIGIN_UNSUPPORTED))
		self.assertEqual(len(report.landmarks[0].landmarks), 2)
		multiframe = LandmarkSet.from_points([(1, 1), (2, 2)], name='Frame two', reference=ImageReference(MULTIFRAME, L.P_SOP, 2, L.P_SERIES))
		multiframe.tracking_uid = '2.25.47610'
		out = landmark_items(set_group(write([multiframe]), multiframe.tracking_uid))
		self.assertEqual(int(coordinate_of(out[0]).ContentSequence[0].ReferencedSOPSequence[0].ReferencedFrameNumber), 2)
		self.assertEqual(len(read(write([multiframe])).landmarks[0].landmarks), 2)

		def sop(ds, index=0):
			return coordinate_of(landmark_items(set_group(ds, multiframe.tracking_uid))[index]).ContentSequence[0].ReferencedSOPSequence[0]
		# A multiframe image without a frame number: ambiguous, the landmark is kept opaque (strict mode raises)
		ds2 = write([multiframe])
		del sop(ds2).ReferencedFrameNumber
		report2 = read(ds2)
		self.assertTrue(conditions(report2, SR_CONDITION_LANDMARK_IMAGE_REFERENCE_AMBIGUOUS))
		self.assertEqual(len(report2.landmarks[0].landmarks), 1)
		with self.assertRaises(SRDiagnosticError):
			read(ds2, strict=True)
		# A cached frame count of one makes the frame number optional
		del sop(ds2, 1).ReferencedFrameNumber
		self.assertEqual(len(read(ds2, images={L.P_SOP: ImageMetadata(frames=1)}).landmarks[0].landmarks), 2)
		ds3 = write([multiframe])
		sop(ds3).ReferencedFrameNumber = [1, 2]
		self.assertTrue(conditions(read(ds3), SR_CONDITION_LANDMARK_IMAGE_REFERENCE_AMBIGUOUS))
		# A frame number past the cached frame count
		self.assertTrue(conditions(read(write([multiframe]), images={L.P_SOP: ImageMetadata(frames=1)}),
			SR_CONDITION_LANDMARK_IMAGE_REFERENCE_AMBIGUOUS))
		# Writing: a multiframe reference without its frame is refused
		report = new_report()
		report.landmarks = [LandmarkSet.from_points([(1, 1)], name='No frame', reference=ImageReference(MULTIFRAME, L.P_SOP, None, L.P_SERIES))]
		with self.assertRaises(SRDiagnosticError) as caught:
			report.create_sr()
		self.assertEqual(caught.exception.diagnostic.condition, SR_CONDITION_LANDMARK_IMAGE_REFERENCE_AMBIGUOUS)

	def test_frame_number_must_be_a_positive_integer(self):
		for bad in (-1, 0):
			ds = fixture('image')
			for item in landmark_items(set_group(ds, L.SET_UIDS['image'])):
				coordinate_of(item).ContentSequence[0].ReferencedSOPSequence[0].ReferencedFrameNumber = bad
			ds = disk(ds)
			report = read(ds)
			self.assertTrue(conditions(report, SR_CONDITION_LANDMARK_IMAGE_REFERENCE_AMBIGUOUS), bad)
			self.assertNotIn(L.SET_UIDS['image'], [s.tracking_uid for s in report.landmarks])
			self.assertEqual(item_bytes(set_group(disk(report.create_sr()), L.SET_UIDS['image'])), item_bytes(set_group(ds, L.SET_UIDS['image'])))
			with self.assertRaises(SRDiagnosticError):
				read(ds, strict=True)
			for sop_class in (F.MR_IMAGE_STORAGE, MULTIFRAME):
				written = new_report()
				written.landmarks = [LandmarkSet.from_points([(1, 1)], name='Frame', reference=ImageReference(sop_class, L.P_SOP, bad, L.P_SERIES))]
				with self.assertRaises(SRDiagnosticError) as caught:
					written.create_sr()
				self.assertEqual(caught.exception.diagnostic.condition, SR_CONDITION_LANDMARK_IMAGE_REFERENCE_AMBIGUOUS)

	def test_tiled_images(self):
		tiled = LandmarkSet.from_points([(5, 5)], name='Slide', reference=ImageReference(WHOLE_SLIDE, L.P_SOP, 1, L.P_SERIES))
		tiled.tracking_uid = '2.25.47611'
		ds = write([tiled])
		item = coordinate_of(landmark_items(set_group(ds, tiled.tracking_uid))[0])
		self.assertEqual(item.PixelOriginInterpretation, 'FRAME')
		self.assertEqual(len(read(ds).landmarks), 1)
		del item.PixelOriginInterpretation
		report = read(ds)
		self.assertTrue(conditions(report, SR_CONDITION_LANDMARK_PIXEL_ORIGIN_UNSUPPORTED))
		self.assertEqual(report.landmarks, [])
		# The same on an image the cache marks as tiled; an ordinary image without the attribute reads as frame-relative
		image = fixture('image')
		for each in landmark_items(set_group(image, L.SET_UIDS['image'])):
			del coordinate_of(each).PixelOriginInterpretation
		self.assertTrue(conditions(read(image, images={L.P_SOP: ImageMetadata(tiled=True)}), SR_CONDITION_LANDMARK_PIXEL_ORIGIN_UNSUPPORTED))
		self.assertFalse(conditions(read(image), SR_CONDITION_LANDMARK_PIXEL_ORIGIN_UNSUPPORTED))

	def test_non_square_bounds(self):
		# (columns, rows): 100 columns, 40 rows
		inside, outside = (90.0, 30.0), (30.0, 90.0)
		lset = LandmarkSet.from_points([inside, outside], name='Bounds', reference=P_IMAGE)
		lset.tracking_uid = '2.25.47612'
		report = read(write([lset]), images={L.P_SOP: (100, 40)})
		warnings = conditions(report, SR_CONDITION_LANDMARK_POINT_OUT_OF_BOUNDS)
		self.assertEqual([d.value for d in warnings], [lset.landmarks[1].fiducial_uid])
		report = new_report(images={L.P_SOP: ImageMetadata(columns=100, rows=40)})
		report.landmarks = [LandmarkSet.from_points([inside], name='Inside', reference=P_IMAGE)]
		self.assertTrue(report.create_sr())
		report.landmarks = [LandmarkSet.from_points([outside], name='Outside', reference=P_IMAGE)]
		with self.assertRaises(SRDiagnosticError) as caught:
			report.create_sr()
		self.assertEqual(caught.exception.diagnostic.condition, SR_CONDITION_LANDMARK_POINT_OUT_OF_BOUNDS)

	def test_cached_image_metadata(self):
		from ...imaging.orthanc.sr import dcmsr_image_metadata

		class Instance:
			def __init__(self, tags):
				self.dicomdata = tags

		class Series:
			def __init__(self, instances):
				self._slices = instances

		found = dcmsr_image_metadata([Series([Instance({'SOPInstanceUID': L.P_SOP, 'NumberOfFrames': '3', 'Rows': '40', 'Columns': '100'}),
			Instance({'SOPInstanceUID': L.S_SOP, 'DimensionOrganizationType': 'TILED_FULL'})])])
		self.assertEqual(found[L.P_SOP], ImageMetadata(columns=100, rows=40, frames=3, tiled=None))
		self.assertTrue(found[L.S_SOP].tiled)


# ---- 3.5 preserved content ----


def opaque_text(value, relationship='CONTAINS'):
	item = Dataset()
	item.RelationshipType, item.ValueType, item.TextValue = relationship, 'TEXT', value
	item.ConceptNameCodeSequence = Sequence([srcode2dataset(codes.DCM.Comment)])
	return item


class LandmarkPreservedContentTestCase(unittest.TestCase):
	def prepared(self):
		ds = fixture('exchange')
		group = set_group(ds, L.SET_UIDS['patient'])
		items = landmark_items(group)
		malformed = copy.deepcopy(items[2])
		malformed.ContentSequence = Sequence([c for c in malformed.ContentSequence if c.ValueType not in ('SCOORD3D',)])
		children = list(group.ContentSequence)
		position = children.index(items[1]) + 1
		children[position:position] = [malformed, opaque_text('after landmark 2')]
		finding = finding_of(group)[0]
		children.insert(children.index(finding) + 1, opaque_text('after finding'))
		group.ContentSequence = Sequence(children)
		finding.ContentSequence.append(opaque_text('under finding', 'HAS PROPERTIES'))
		items[0].ContentSequence.append(opaque_text('under landmark 1', 'HAS PROPERTIES'))
		display = next(c for c in items[0].ContentSequence if srcontent_is_named(c, DCMSR_DISPLAY_ATTRIBUTES))
		display.ContentSequence.append(opaque_text('under display', 'HAS PROPERTIES'))
		return ds

	def comments(self, items):
		return [str(c.TextValue) for c in items if c.ValueType == 'TEXT' and srcontent_is_named(c, codes.DCM.Comment)]

	def test_unrelated_edit_and_reorder(self):
		ds = self.prepared()
		report = read(ds)
		self.assertTrue(conditions(report, SR_CONDITION_LANDMARK_WITHOUT_POINT))
		lset = report.landmarks[0]
		self.assertEqual(len(lset.landmarks), 10)
		lset.landmarks.move(lset.landmarks[1], 5)
		out = set_group(disk(report.create_sr()), L.SET_UIDS['patient'])
		children = list(out.ContentSequence)
		names = ['finding' if srcontent_is_named(c, DCMSR_FINDING) else 'landmark' if srcontent_is_named(c, DCMSR_LANDMARK) and
			any(x.ValueType == 'SCOORD3D' for x in c.ContentSequence) else 'malformed' if srcontent_is_named(c, DCMSR_LANDMARK) else
			str(c.TextValue) if c.ValueType == 'TEXT' and srcontent_is_named(c, codes.DCM.Comment) else 'pair' for c in children]
		moved = lset.landmarks[5]
		position = [i for i, c in enumerate(children) if c.ValueType == 'CODE' and srcontent_is_named(c, DCMSR_LANDMARK)
			and any(x.ValueType == 'SCOORD3D' and str(x.FiducialUID) == moved.fiducial_uid for x in c.ContentSequence)][0]
		self.assertEqual(names[:5], ['pair', 'pair', 'finding', 'after finding', 'landmark'])
		self.assertEqual(names[position + 1:position + 3], ['malformed', 'after landmark 2'])
		self.assertEqual(names.count('malformed'), 1)
		self.assertEqual(self.comments(finding_of(out)[0].ContentSequence), ['under finding'])
		first = landmark_items(out)[0]
		self.assertEqual(self.comments(first.ContentSequence), ['under landmark 1'])
		display = next(c for c in first.ContentSequence if srcontent_is_named(c, DCMSR_DISPLAY_ATTRIBUTES))
		self.assertEqual(self.comments(display.ContentSequence), ['under display'])
		# Removing the anchor passes its slots to the nearest earlier survivor
		lset.landmarks.remove(moved)
		out = set_group(disk(report.create_sr()), L.SET_UIDS['patient'])
		self.assertEqual(sum(1 for c in out.ContentSequence if c.ValueType == 'TEXT' and str(c.TextValue) == 'after landmark 2'), 1)

	def test_second_text_and_replaced_extra_items(self):
		ds = fixture('exchange')
		item = landmark_items(set_group(ds, L.SET_UIDS['patient']))[0]
		second = Dataset()
		second.RelationshipType, second.ValueType, second.TextValue = 'HAS PROPERTIES', 'TEXT', 'A second text'
		second.ConceptNameCodeSequence = Sequence([srcode2dataset(DCMSR_MEASUREMENT_TEXT)])
		item.ContentSequence.append(second)
		report = read(disk(ds))
		landmark = report.landmarks[0].landmarks[0]
		self.assertEqual(landmark.label, 'ASIS (left)')
		landmark.description = 'description only'
		written = landmark_items(set_group(disk(report.create_sr()), L.SET_UIDS['patient']))[0]
		self.assertEqual(text_children(written), ['ASIS (left)', 'A second text'])
		# Replacing a preserved item, at either level, is an edit and is written
		report = read(disk(self.prepared()))
		lset = report.landmarks[0]
		lset.extra_items = (opaque_text('replaced under finding', 'HAS PROPERTIES'),)
		self.assertTrue(lset.edited)
		lset.landmarks[0].extra_items = (opaque_text('replaced under landmark', 'HAS PROPERTIES'),)
		self.assertTrue(lset.landmarks[0].edited)
		out = set_group(disk(report.create_sr()), L.SET_UIDS['patient'])
		self.assertEqual(self.comments(finding_of(out)[0].ContentSequence), ['replaced under finding'])
		self.assertEqual(self.comments(landmark_items(out)[0].ContentSequence), ['replaced under landmark'])

	def test_by_reference_child_refuses_the_save(self):
		ds = fixture('exchange')
		item = landmark_items(set_group(ds, L.SET_UIDS['patient']))[0]
		ref = Dataset()
		ref.RelationshipType = 'HAS PROPERTIES'
		ref.ReferencedContentItemIdentifier = [1, 1]
		item.ContentSequence.append(ref)
		report = read(ds)
		with self.assertRaises(SRDiagnosticError) as caught:
			report.create_sr()
		self.assertEqual(caught.exception.diagnostic.condition, SR_CONDITION_BY_REFERENCE_ITEM)


# ---- 3.6 outcomes and conditions ----


def strip_landmarks(ds, tracking_uid, keep=lambda item: False):
	group = set_group(ds, tracking_uid)
	group.ContentSequence = Sequence([c for c in group.ContentSequence if not srcontent_is_named(c, DCMSR_LANDMARK) or keep(c)])
	return group


class LandmarkOutcomeTestCase(unittest.TestCase):
	def outcome(self, ds, strict=False):
		report = read(ds, strict=strict)
		return report, [g for g in report.groups if isinstance(g, (LandmarkSetGroup, RawReportGroup))]

	def test_no_landmark_item(self):
		ds = fixture('exchange')
		strip_landmarks(ds, L.SET_UIDS['patient'])
		report, found = self.outcome(ds)
		self.assertIsInstance(found[0], RawReportGroup)
		self.assertEqual(report.landmarks, [])
		self.assertEqual([d.condition for d in report.diagnostics if d.condition.startswith('landmark')],
			[SR_CONDITION_LANDMARK_SET_EMPTY, SR_CONDITION_LANDMARK_SET_READ_AS_GROUP])
		self.assertEqual(item_bytes(set_group(disk(report.create_sr()), L.SET_UIDS['patient'])), item_bytes(set_group(ds, L.SET_UIDS['patient'])))
		with self.assertRaises(SRDiagnosticError):
			read(ds, strict=True)

	def test_no_readable_landmark(self):
		ds = fixture('exchange')
		for item in landmark_items(set_group(ds, L.SET_UIDS['patient'])):
			coordinate_of(item).GraphicType = 'POLYLINE'
		report, found = self.outcome(ds)
		self.assertIsInstance(found[0], RawReportGroup)
		conditions_ = [d.condition for d in report.diagnostics if d.condition.startswith('landmark')]
		self.assertEqual(conditions_, [SR_CONDITION_LANDMARK_WITHOUT_POINT] * 10 + [SR_CONDITION_LANDMARK_SET_READ_AS_GROUP])
		self.assertEqual(item_bytes(set_group(disk(report.create_sr()), L.SET_UIDS['patient'])), item_bytes(set_group(ds, L.SET_UIDS['patient'])))

	def test_some_readable(self):
		ds = fixture('exchange')
		coordinate_of(landmark_items(set_group(ds, L.SET_UIDS['patient']))[3]).GraphicData = [1.0, float('nan'), 3.0]
		report, found = self.outcome(ds)
		self.assertEqual(len(report.landmarks[0].landmarks), 9)
		self.assertTrue(conditions(report, SR_CONDITION_LANDMARK_POINT_INVALID))
		self.assertEqual(item_bytes(set_group(disk(report.create_sr()), L.SET_UIDS['patient'])), item_bytes(set_group(ds, L.SET_UIDS['patient'])))
		report.landmarks[0].description = 'edited'
		self.assertEqual(len(landmark_items(set_group(disk(report.create_sr()), L.SET_UIDS['patient']))), 10)
		with self.assertRaises(SRDiagnosticError):
			read(ds, strict=True)

	def test_no_name(self):
		ds = fixture('exchange')
		finding = finding_of(set_group(ds, L.SET_UIDS['patient']))[0]
		finding.ConceptCodeSequence = Sequence([srcode2dataset(free_text_finding(''))])
		finding.ContentSequence = Sequence([c for c in finding.ContentSequence if not srcontent_is_named(c, DCMSR_MEASUREMENT_TEXT)])
		report = read(ds)
		self.assertEqual(report.landmarks[0].name, '')
		self.assertIsNone(report.landmarks[0].concept)
		self.assertTrue(conditions(report, SR_CONDITION_LANDMARK_SET_WITHOUT_NAME))
		self.assertTrue(disk(report.create_sr()))
		report.landmarks[0].description = 'edited'
		with self.assertRaises(SRDiagnosticError) as caught:
			report.create_sr()
		self.assertEqual(caught.exception.diagnostic.condition, SR_CONDITION_LANDMARK_SET_WITHOUT_NAME)
		report.landmarks[0].label = 'Named now'
		self.assertTrue(report.create_sr())

	def test_unrepresentable_label_read_as_stored(self):
		ds = fixture('exchange')
		item = landmark_items(set_group(ds, L.SET_UIDS['patient']))[1]
		item.ConceptCodeSequence[0].CodeMeaning = ' Label only'
		before = item_bytes(set_group(ds, L.SET_UIDS['patient']))
		report = read(ds)
		self.assertTrue(conditions(report, SR_CONDITION_LANDMARK_LABEL_UNREPRESENTABLE))
		self.assertEqual(item_bytes(set_group(disk(report.create_sr()), L.SET_UIDS['patient'])), before)

	def test_strict_mode_raises_on_the_first_warning(self):
		for change in (lambda ds: strip_landmarks(ds, L.SET_UIDS['patient']),
				lambda ds: setattr(coordinate_of(landmark_items(set_group(ds, L.SET_UIDS['patient']))[0]), 'GraphicType', 'POLYLINE')):
			ds = fixture('exchange')
			change(ds)
			self.assertTrue(read(ds))
			with self.assertRaises(SRDiagnosticError):
				read(ds, strict=True)

	def test_valueless_landmark_item_through_the_report(self):
		ds = fixture('exchange')
		del landmark_items(set_group(ds, L.SET_UIDS['patient']))[2].ConceptCodeSequence
		ds = disk(ds)
		report = read(ds)
		self.assertTrue(conditions(report, SR_CONDITION_LANDMARK_SET_INVALID))
		self.assertEqual(len(report.landmarks[0].landmarks), 9)
		self.assertEqual(item_bytes(set_group(disk(report.create_sr()), L.SET_UIDS['patient'])), item_bytes(set_group(ds, L.SET_UIDS['patient'])))
		report.landmarks[0].description = 'edited'
		items = set_group(disk(report.create_sr()), L.SET_UIDS['patient']).ContentSequence
		self.assertEqual(sum(1 for c in items if c.ValueType == 'CODE' and srcontent_is_named(c, DCMSR_LANDMARK)), 10)
		with self.assertRaises(SRDiagnosticError):
			read(ds, strict=True)
		# Every landmark item without a value: the group is kept as read
		ds = fixture('exchange')
		for item in landmark_items(set_group(ds, L.SET_UIDS['patient'])):
			del item.ConceptCodeSequence
		ds = disk(ds)
		report, found = self.outcome(ds)
		self.assertIsInstance(found[0], RawReportGroup)
		self.assertTrue(conditions(report, SR_CONDITION_LANDMARK_SET_READ_AS_GROUP))
		self.assertEqual(item_bytes(set_group(disk(report.create_sr()), L.SET_UIDS['patient'])), item_bytes(set_group(ds, L.SET_UIDS['patient'])))

	def test_unrelated_edit_leaves_each_group_as_read(self):
		ds = fixture('mixed')
		coordinate_of(landmark_items(set_group(ds, L.SET_UIDS['patient']))[3]).GraphicData = [1.0, 2.0]
		report = read(ds)
		report.measurements = None
		length = next(m for g in report.groups for m in (g.measurements or []))
		length.description = 'unrelated edit'
		self.assertEqual(item_bytes(set_group(disk(report.create_sr()), L.SET_UIDS['patient'])), item_bytes(set_group(ds, L.SET_UIDS['patient'])))


class LandmarkConditionTestCase(unittest.TestCase):
	def read_modified(self, change, name='exchange', uid='patient'):
		ds = fixture(name)
		change(landmark_items(set_group(ds, L.SET_UIDS[uid])), ds)
		return read(ds)

	def test_write_refusals(self):
		cases = (
			(lambda: LandmarkSet([Landmark((1, 2, 3))], frame_of_reference_uid=P), SR_CONDITION_LANDMARK_SET_WITHOUT_NAME),
			(lambda: LandmarkSet([], label='Empty', frame_of_reference_uid=P), SR_CONDITION_LANDMARK_SET_EMPTY),
			(lambda: LandmarkSet([Landmark((1, 2, 3)), Landmark((1, 2))], label='Mixed', frame_of_reference_uid=P), SR_CONDITION_LANDMARK_SET_MIXED_FRAMES),
			(lambda: LandmarkSet.from_points([(5000.0, 1.0)], name='Out', reference=P_IMAGE), SR_CONDITION_LANDMARK_POINT_OUT_OF_BOUNDS),
		)
		for build, condition in cases:
			report = new_report(images={L.P_SOP: (512, 512)})
			report.landmarks = [build()]
			with self.assertRaises(SRDiagnosticError) as caught:
				report.create_sr()
			self.assertEqual(caught.exception.diagnostic.condition, condition)
		with self.assertRaises(ValueError):
			Landmark((1.0, float('inf'), 3.0))
		with self.assertRaises(ValueError):
			Landmark((1.0, 2.0, 3.0), display={'Line': '#XYZXYZ'})

	def test_read_conditions(self):
		def no_image(items, ds):
			coordinate_of(items[0]).ContentSequence = Sequence([])
		def two_images(items, ds):
			image = coordinate_of(items[0]).ContentSequence[0]
			coordinate_of(items[0]).ContentSequence.append(copy.deepcopy(image))
		def two_coordinates(items, ds):
			items[0].ContentSequence.append(copy.deepcopy(coordinate_of(items[0])))
		def wrong_count(items, ds):
			coordinate_of(items[0]).GraphicData = [1.0, 2.0, 3.0]
		def other_image(items, ds):
			coordinate_of(items[1]).ContentSequence[0].ReferencedSOPSequence[0].ReferencedSOPInstanceUID = L.S_SOP
		def other_frame_number(items, ds):
			coordinate_of(items[0]).ContentSequence[0].ReferencedSOPSequence[0].ReferencedFrameNumber = 1
			coordinate_of(items[1]).ContentSequence[0].ReferencedSOPSequence[0].ReferencedFrameNumber = 2
		def no_fiducial(items, ds):
			del coordinate_of(items[0]).FiducialUID
		for change, condition, count in ((no_image, SR_CONDITION_LANDMARK_IMAGE_REFERENCE_MISSING, 2),
				(two_images, SR_CONDITION_LANDMARK_IMAGE_REFERENCE_AMBIGUOUS, 2), (two_coordinates, SR_CONDITION_LANDMARK_POINT_AMBIGUOUS, 2),
				(wrong_count, SR_CONDITION_LANDMARK_POINT_INVALID, 2), (other_image, SR_CONDITION_LANDMARK_SET_MIXED_FRAMES, 2),
				(other_frame_number, SR_CONDITION_LANDMARK_SET_MIXED_FRAMES, 1), (no_fiducial, SR_CONDITION_LANDMARK_IDENTITY_PROVISIONAL, 3)):
			report = self.read_modified(change, 'image', 'image')
			self.assertTrue(conditions(report, condition), condition)
			self.assertEqual(len(report.landmarks[0].landmarks), count, condition)
			ds = fixture('image')
			change(landmark_items(set_group(ds, L.SET_UIDS['image'])), ds)
			if condition != SR_CONDITION_LANDMARK_IDENTITY_PROVISIONAL:
				with self.assertRaises(SRDiagnosticError):
					read(ds, strict=True)
		# Patient domain: landmarks in two frames
		report = self.read_modified(lambda items, ds: setattr(coordinate_of(items[4]), 'ReferencedFrameOfReferenceUID', S))
		self.assertTrue(conditions(report, SR_CONDITION_LANDMARK_SET_MIXED_FRAMES))
		self.assertEqual(len(report.landmarks[0].landmarks), 9)
		# A landmark item without a value (highdicom refuses such a document on load; the group reader keeps it opaque)
		group = copy.deepcopy(set_group(fixture('exchange'), L.SET_UIDS['patient']))
		del landmark_items(group)[2].ConceptCodeSequence
		diagnostics = []
		lset = srparse_landmark_set_group(group, diagnostics=diagnostics)
		self.assertTrue(sr_diagnostics_with(diagnostics, condition=SR_CONDITION_LANDMARK_SET_INVALID))
		self.assertEqual(len(lset.landmarks), 9)
		# Out of bounds when the image size is known: kept, with the warning
		ds = fixture('image')
		report = read(ds, images={L.P_SOP: (10, 10)})
		self.assertTrue(conditions(report, SR_CONDITION_LANDMARK_POINT_OUT_OF_BOUNDS))
		self.assertEqual(len(report.landmarks[0].landmarks), 3)
		# Invalid colour on read
		ds = fixture('exchange')
		finding = finding_of(set_group(ds, L.SET_UIDS['patient']))[0]
		display = next(c for c in finding.ContentSequence if srcontent_is_named(c, DCMSR_DISPLAY_ATTRIBUTES))
		display.ContentSequence[0].ContentSequence[0].TextValue = 'not a colour'
		self.assertTrue(conditions(read(ds), SR_CONDITION_INVALID_DISPLAY_COLOR))


class LandmarkIdentityTestCase(unittest.TestCase):
	def test_identity_across_writes(self):
		lset = single()
		report = new_report()
		report.landmarks = [lset]
		first, second = disk(report.create_sr()), disk(report.create_sr())
		self.assertEqual([str(coordinate_of(i).FiducialUID) for i in landmark_items(set_group(first, lset.tracking_uid))],
			[str(coordinate_of(i).FiducialUID) for i in landmark_items(set_group(second, lset.tracking_uid))])
		again = read(first)
		self.assertEqual(again.landmarks[0].landmarks[0].fiducial_uid, lset.landmarks[0].fiducial_uid)
		third = read(disk(again.create_sr()))
		self.assertEqual(third.landmarks[0].landmarks[0].fiducial_uid, lset.landmarks[0].fiducial_uid)

	def test_duplicate_identities_across_sets(self):
		a = single(fiducial_uids=['2.25.47801'])
		b = single(name='Other', fiducial_uids=['2.25.47801'])
		report = new_report()
		report.landmarks = [a, b]
		with self.assertRaises(SRDiagnosticError) as caught:
			report.create_sr()
		self.assertEqual(caught.exception.diagnostic.condition, SR_CONDITION_DUPLICATE_FIDUCIAL_UID)
		ds = write([a])
		group = copy.deepcopy(set_group(ds, a.tracking_uid))
		next(c for c in group.ContentSequence if srcontent_is_named(c, codes.DCM.TrackingUniqueIdentifier)).UID = '2.25.47802'
		container = next(c for c in ds.ContentSequence if srcontent_is_named(c, codes.DCM.ImagingMeasurements))
		container.ContentSequence.append(group)
		parsed = read(ds)
		self.assertTrue(conditions(parsed, SR_CONDITION_DUPLICATE_FIDUCIAL_UID))
		self.assertEqual(len(parsed.landmarks), 2)
		self.assertTrue(parsed.create_sr())
		parsed.landmarks[0].landmarks[0].label = 'edited'
		with self.assertRaises(SRDiagnosticError):
			parsed.create_sr()

	def test_another_occurrence_of_a_parsed_landmark_refused(self):
		report = read(fixture('exchange'))
		lset = report.landmarks[0]
		lset.landmarks.append(lset.landmarks[0])
		with self.assertRaises(SRDiagnosticError) as caught:
			report.create_sr()
		self.assertEqual(caught.exception.diagnostic.condition, SR_CONDITION_DUPLICATE_FIDUCIAL_UID)
		report = read(fixture('image'))
		first, second = report.landmarks
		second.landmarks.insert(0, Landmark((3.0, 4.0), fiducial_uid=first.landmarks[0].fiducial_uid))
		with self.assertRaises(SRDiagnosticError) as caught:
			report.create_sr()
		self.assertEqual(caught.exception.diagnostic.condition, SR_CONDITION_DUPLICATE_FIDUCIAL_UID)

	def test_a_copy_of_a_parsed_set_beside_its_source_refused(self):
		report = read(fixture('exchange'))
		source = report.landmarks[0]
		for copied in (copy.deepcopy(source), report.convert(source, P)):
			copied.tracking_uid = '2.25.47810'
			report.landmarks = [source, copied]
			with self.assertRaises(SRDiagnosticError) as caught:
				report.create_sr()
			self.assertEqual(caught.exception.diagnostic.condition, SR_CONDITION_DUPLICATE_FIDUCIAL_UID)
		report.landmarks = [source]
		self.assertTrue(report.create_sr())

	def test_a_set_read_by_another_report_is_new(self):
		destination = read(fixture('exchange'))
		incoming = read(fixture('exchange')).landmarks[0]
		incoming.tracking_uid = '2.25.47811'
		destination.landmarks = destination.landmarks + [incoming]
		with self.assertRaises(SRDiagnosticError) as caught:
			destination.create_sr()
		self.assertEqual(caught.exception.diagnostic.condition, SR_CONDITION_DUPLICATE_FIDUCIAL_UID)
		# The writing report's own historical duplicate is still kept as read
		ds = write([single(fiducial_uids=['2.25.47812'])])
		group = copy.deepcopy(groups(ds)[0])
		next(c for c in group.ContentSequence if srcontent_is_named(c, codes.DCM.TrackingUniqueIdentifier)).UID = '2.25.47813'
		next(c for c in ds.ContentSequence if srcontent_is_named(c, codes.DCM.ImagingMeasurements)).ContentSequence.append(group)
		own = read(disk(ds))
		self.assertTrue(own.create_sr())
		own.landmarks = own.landmarks + [read(disk(ds)).landmarks[0]]
		with self.assertRaises(SRDiagnosticError):
			own.create_sr()

	def test_registration_item_citing_a_landmark_is_not_a_second_landmark(self):
		lset = single(fiducial_uids=['2.25.47803'])
		report = new_report()
		report.landmarks = [lset]
		fiducial = Dataset()
		fiducial.ReferencedFiducialUID = '2.25.47803'
		report.frames.add_frame(ReferenceFrame('2.25.47804', name='Cited'))
		report.frames.add_edge(ImageTransformMatrix(F.SHIFT, fiducials=[fiducial]), '2.25.47804', P)
		reloaded = read(disk(report.create_sr()))
		self.assertFalse(conditions(reloaded, SR_CONDITION_DUPLICATE_FIDUCIAL_UID))


# ---- 3.7 legacy point groups ----


class LandmarkLegacyTestCase(unittest.TestCase):
	def legacy(self, ds=None):
		return read(ds if ds is not None else fixture('legacy'), root='application')

	def volume(self, ds):
		return next(c for c in ds.ContentSequence if srcontent_is_named(c, DCMSR_REPORT_VOLUME_MEAUREMENTS))

	def test_recognition_by_parent(self):
		report = self.legacy()
		self.assertEqual([s.tracking_uid for s in report.landmarks], [L.SET_UIDS['legacy'], L.SET_UIDS['legacy-polyline']])
		self.assertTrue(all(s.legacy for s in report.landmarks))
		self.assertTrue(conditions(report, SR_CONDITION_LANDMARK_LEGACY_COLLECTION))
		first = report.landmarks[0]
		self.assertEqual(first.label, 'Femoral head points')
		self.assertEqual([l.landmark_type.meaning for l in first.landmarks], ['Anatomic', 'Point', 'Point'])
		self.assertEqual(first.landmarks[0].fiducial_uid, L._fid('legacy-point'))
		self.assertTrue(first.landmarks[1].provisional)
		# One point collection per SCOORD3D item of the legacy groups
		self.assertEqual(len(report.point_collections), 4)

	def test_unchanged_passthrough(self):
		ds = fixture('legacy')
		report = self.legacy(ds)
		out = disk(report.create_sr())
		self.assertEqual(item_bytes(self.volume(out)), item_bytes(self.volume(ds)))
		self.assertEqual(groups(out), [])

	def test_migration_of_one_group(self):
		ds = fixture('legacy')
		report = self.legacy(ds)
		report.landmarks[0].description = 'migrated'
		out = disk(report.create_sr())
		self.assertTrue(conditions(report, SR_CONDITION_LANDMARK_LEGACY_MIGRATED))
		group = set_group(out, L.SET_UIDS['legacy'])
		self.assertEqual(str(next(c for c in group.ContentSequence if srcontent_is_named(c, codes.DCM.TrackingIdentifier)).TextValue),
			'cornerstoneTools@^4.0.0:SonadorLandmarkSet')
		self.assertEqual(len(landmark_items(group)), 3)
		volume = self.volume(out)
		self.assertEqual(len(volume.ContentSequence), 1)
		meta = [c for c in out.ContentSequence if dcm_code_value(c.ConceptNameCodeSequence[0]).endswith('.Meta')]
		self.assertEqual(len(meta), 1)
		reloaded = self.legacy(out)
		self.assertEqual([s.legacy for s in reloaded.landmarks], [False, True])
		self.assertEqual(reloaded.landmarks[0].landmarks[0].fiducial_uid, L._fid('legacy-point'))
		self.assertEqual(len(reloaded.point_collections), 2)

	def assert_refused(self, edit, *reasons):
		with self.assertRaises(SRDiagnosticError) as caught:
			edit()
		self.assertEqual(caught.exception.diagnostic.condition, SR_CONDITION_LANDMARK_LEGACY_NOT_MIGRATABLE)
		for reason in reasons:
			self.assertIn(reason, str(caught.exception))

	def test_not_migratable(self):
		ds = fixture('legacy')
		report = self.legacy(ds)
		polyline = report.landmarks[1]
		before = (polyline.label, polyline.landmarks[0].label, len(polyline.landmarks))
		# Refused at the edit, whole: name only, a landmark, the membership
		self.assert_refused(lambda: setattr(polyline, 'label', 'Renamed only'), 'POLYLINE', 'other than its points')
		self.assert_refused(lambda: setattr(polyline, 'description', 'Described'))
		self.assert_refused(lambda: setattr(polyline.landmarks[0], 'label', 'Nested'))
		self.assert_refused(lambda: polyline.landmarks.append(Landmark((1, 2, 3))))
		self.assert_refused(lambda: polyline.landmarks.remove(polyline.landmarks[0]))
		self.assertEqual((polyline.label, polyline.landmarks[0].label, len(polyline.landmarks)), before)
		self.assertFalse(polyline.edited)
		# An unrelated save afterwards succeeds and keeps the group as read
		self.assertEqual(item_bytes(self.volume(disk(report.create_sr()))), item_bytes(self.volume(ds)))
		# Not convertible either
		report.frames.add_frame(ReferenceFrame(L.APP_FRAME, name='Anatomic'))
		report.frames.add_edge(F.RZ, L.APP_FRAME, P)
		with self.assertRaises(FrameConversionError) as caught:
			report.convert(polyline, L.APP_FRAME)
		self.assertEqual(caught.exception.reason, SR_CONDITION_LANDMARK_LEGACY_NOT_MIGRATABLE)

		ds = fixture('legacy')
		for item in self.legacy_items(ds, 'MULTIPOINT'):
			item.FiducialUID = '2.25.47901'
		self.assert_refused(lambda: setattr(self.legacy(ds).landmarks[0], 'description', 'edit'), 'MULTIPOINT', 'Fiducial UID')
		mixed = self.legacy(fixture('legacy-mixed'))
		self.assert_refused(lambda: setattr(mixed.landmarks[0], 'label', 'Name only'), 'different frames')

	def legacy_items(self, ds, graphic):
		return [item for container in ds.ContentSequence for group in container.get('ContentSequence') or []
			for item in group.get('ContentSequence') or [] if item.get('GraphicType') == graphic]

	def test_migration_keeps_the_source_and_allows_a_revert(self):
		ds = fixture('legacy')
		report = self.legacy(ds)
		children = len(report.extra_items[[i for i, item in enumerate(report.extra_items)
			if srcontent_is_named(item, DCMSR_REPORT_VOLUME_MEAUREMENTS)][0]].ContentSequence)
		first = report.landmarks[0]
		first.description = 'migrated'
		migrated = disk(report.create_sr())
		self.assertEqual(len(self.volume(migrated).ContentSequence), 1)
		volume = next(item for item in report.extra_items if srcontent_is_named(item, DCMSR_REPORT_VOLUME_MEAUREMENTS))
		self.assertEqual(len(volume.ContentSequence), children)
		first.description = None
		reverted = disk(report.create_sr())
		self.assertEqual(item_bytes(self.volume(reverted)), item_bytes(self.volume(ds)))
		self.assertEqual(groups(reverted), [])
		self.assertEqual(len(self.legacy(reverted).landmarks), 2)

	def test_converted_legacy_copy_replaces_its_group_once(self):
		report = self.legacy()
		report.frames.add_frame(ReferenceFrame(L.APP_FRAME, name='Anatomic'))
		report.frames.add_edge(F.RZ, L.APP_FRAME, P)
		first, polyline = report.landmarks
		converted = report.convert(first, L.APP_FRAME)
		report.landmarks = [converted, polyline]
		out = disk(report.create_sr())
		self.assertEqual(len(self.volume(out).ContentSequence), 1)
		self.assertEqual(len([g for g in groups(out) for c in g.ContentSequence if srcontent_is_named(c, codes.DCM.TrackingUniqueIdentifier)
			and str(c.UID) == L.SET_UIDS['legacy']]), 1)
		self.assertEqual(str(coordinate_of(landmark_items(set_group(out, L.SET_UIDS['legacy']))[0]).ReferencedFrameOfReferenceUID), L.APP_FRAME)
		report.landmarks = [converted, report.convert(first, L.APP_FRAME), polyline]
		with self.assertRaises(SRDiagnosticError):
			report.create_sr()

	def test_migration_keeps_point_content_and_refuses_ambiguous_multipoint(self):
		ds = fixture('legacy')
		point = [item for item in self.legacy_items(ds, 'POINT') if str(item.get('FiducialUID') or '') == L._fid('legacy-point')][0]
		point.ObservationDateTime = '20261114120000'
		comment = opaque_text('kept under the point', 'HAS PROPERTIES')
		point.ContentSequence = Sequence([comment])
		report = self.legacy(disk(ds))
		report.landmarks[0].description = 'migrated'
		out = disk(report.create_sr())
		item = coordinate_of(landmark_items(set_group(out, L.SET_UIDS['legacy']))[0])
		self.assertEqual(item.RelationshipType, 'INFERRED FROM')
		self.assertEqual(str(item.ObservationDateTime), '20261114120000')
		self.assertEqual([str(c.TextValue) for c in item.ContentSequence], ['kept under the point'])
		self.assertEqual(str(item.FiducialUID), L._fid('legacy-point'))
		ds = fixture('legacy')
		for multipoint in self.legacy_items(ds, 'MULTIPOINT'):
			multipoint.ObservationDateTime = '20261114120000'
		self.assert_refused(lambda: setattr(self.legacy(disk(ds)).landmarks[0], 'description', 'edit'), 'MULTIPOINT', 'ObservationDateTime')

	def test_a_set_from_another_report_leaves_this_report_intact(self):
		source = self.legacy()
		foreign = source.landmarks[0]
		foreign.description = 'carried over'
		# A different document with groups at the same places: other tracking UIDs and points
		other = fixture('legacy')
		for item in self.legacy_items(other, 'POINT') + self.legacy_items(other, 'MULTIPOINT'):
			item.GraphicData = [float(v) + 100.0 for v in item.GraphicData]
			if item.get('FiducialUID'):
				item.FiducialUID = '2.25.47930'
		count = 0
		for container in other.ContentSequence:
			for group in container.get('ContentSequence') or []:
				for child in group.get('ContentSequence') or []:
					if srcontent_is_named(child, codes.DCM.TrackingUniqueIdentifier):
						count += 1
						child.UID = '2.25.4794%d' % count
		other = disk(other)
		report = self.legacy(other)
		report.landmarks = report.landmarks + [foreign]
		out = disk(report.create_sr())
		self.assertEqual(item_bytes(self.volume(out)), item_bytes(self.volume(other)))
		self.assertEqual(len(landmark_items(set_group(out, L.SET_UIDS['legacy']))), 3)

	def test_identical_group_in_another_report_is_kept(self):
		# Two distinct reports holding byte-identical legacy groups (the second with its root items in another order)
		source = self.legacy()
		incoming = source.landmarks[0]
		incoming.tracking_uid = '2.25.47950'
		incoming.description = 'imported'
		reordered = fixture('legacy')
		roots = list(reordered.ContentSequence)
		volume = next(item for item in roots if srcontent_is_named(item, DCMSR_REPORT_VOLUME_MEAUREMENTS))
		reordered.ContentSequence = Sequence([item for item in roots if item is not volume] + [volume])
		reordered = disk(reordered)
		report = self.legacy(reordered)
		report.landmarks = [report.landmarks[1], incoming]
		out = disk(report.create_sr())
		self.assertEqual(item_bytes(self.volume(out)), item_bytes(self.volume(reordered)))
		self.assertEqual(len(landmark_items(set_group(out, '2.25.47950'))), 3)
		self.assertEqual(len(self.legacy(out).landmarks), 3)
		# The destination's own set, edited, replaces its own group (found at the place it was read, root order as read)
		own = self.legacy(reordered)
		own.landmarks[0].description = 'own edit'
		out = disk(own.create_sr())
		self.assertEqual(len(self.volume(out).ContentSequence), 1)

	def test_own_group_found_after_the_root_and_siblings_move(self):
		for move in ('root', 'siblings'):
			report = self.legacy()
			report.landmarks[0].description = 'moved after reading'
			roots = list(report.extra_items)
			volume = next(item for item in roots if srcontent_is_named(item, DCMSR_REPORT_VOLUME_MEAUREMENTS))
			if move == 'root':
				report.extra_items = tuple([item for item in roots if item is not volume] + [volume])
			else:
				volume.ContentSequence = Sequence(list(reversed(list(volume.ContentSequence))))
			out = disk(report.create_sr())
			self.assertEqual(len(self.volume(out).ContentSequence), 1, move)
			reread = self.legacy(out)
			self.assertEqual(len(reread.landmarks), 2, move)
			self.assertEqual(sorted(s.tracking_uid for s in reread.landmarks), sorted([L.SET_UIDS['legacy'], L.SET_UIDS['legacy-polyline']]))

	def test_own_group_changed_after_reading_refuses_the_migration(self):
		def in_place(volume, group):
			group.ContentSequence.append(opaque_text('added after reading'))

		def by_copy(volume, group):
			edited = copy.deepcopy(group)
			edited.ContentSequence.append(opaque_text('added after reading'))
			children = list(volume.ContentSequence)
			children[children.index(group)] = edited
			volume.ContentSequence = Sequence(children)

		for change in (in_place, by_copy):
			report = self.legacy()
			volume = next(item for item in report.extra_items if srcontent_is_named(item, DCMSR_REPORT_VOLUME_MEAUREMENTS))
			group = next(child for child in volume.ContentSequence if any(srcontent_is_named(c, codes.DCM.TrackingUniqueIdentifier)
				and str(c.UID) == L.SET_UIDS['legacy'] for c in child.ContentSequence))
			change(volume, group)
			lset = report.landmarks[0]
			lset.description = 'edited too'
			with self.assertRaises(SRDiagnosticError) as caught:
				report.create_sr()
			self.assertEqual(caught.exception.diagnostic.condition, SR_CONDITION_LANDMARK_LEGACY_NOT_MIGRATABLE, change.__name__)
			# The report stays usable: undo the set's edit and the changed group is written as it now stands
			lset.description = None
			out = disk(report.create_sr())
			written = self.volume(out)
			self.assertEqual(len(written.ContentSequence), 2)
			self.assertIn('added after reading', [str(c.TextValue) for g in written.ContentSequence for c in g.ContentSequence
				if c.ValueType == 'TEXT'])
			self.assertEqual(groups(out), [])

	def test_own_group_removed_after_reading(self):
		report = self.legacy()
		volume = next(item for item in report.extra_items if srcontent_is_named(item, DCMSR_REPORT_VOLUME_MEAUREMENTS))
		volume.ContentSequence = Sequence([child for child in volume.ContentSequence if not any(
			srcontent_is_named(c, codes.DCM.TrackingUniqueIdentifier) and str(c.UID) == L.SET_UIDS['legacy'] for c in child.ContentSequence)])
		report.landmarks[0].description = 'source removed'
		out = disk(report.create_sr())
		self.assertEqual(len(self.volume(out).ContentSequence), 1)
		self.assertEqual(len(landmark_items(set_group(out, L.SET_UIDS['legacy']))), 3)

	def duplicate_group_report(self):
		ds = fixture('legacy')
		volume = self.volume(ds)
		volume.ContentSequence.append(copy.deepcopy(volume.ContentSequence[0]))
		return self.legacy(disk(ds))

	def test_changed_original_beside_an_identical_sibling_refused(self):
		report = self.duplicate_group_report()
		volume = next(item for item in report.extra_items if srcontent_is_named(item, DCMSR_REPORT_VOLUME_MEAUREMENTS))
		before = len(volume.ContentSequence)
		lset = report.landmarks[0]
		original = lset._parsed['legacy_item']
		original.ContentSequence.append(opaque_text('added after reading'))
		report.landmarks = [lset]
		lset.description = 'edited too'
		with self.assertRaises(SRDiagnosticError) as caught:
			report.create_sr()
		self.assertEqual(caught.exception.diagnostic.condition, SR_CONDITION_LANDMARK_LEGACY_NOT_MIGRATABLE)
		self.assertEqual(len(volume.ContentSequence), before)
		# Undoing the set's edit writes every group as it now stands, the sibling included
		lset.description = None
		out = disk(report.create_sr())
		self.assertEqual(len(self.volume(out).ContentSequence), before)
		self.assertEqual(groups(out), [])

	def test_a_copy_facing_several_candidates_refused(self):
		report = self.duplicate_group_report()
		copied = copy.deepcopy(report.landmarks[0])
		copied.description = 'copied and edited'
		report.landmarks = [copied]
		with self.assertRaises(SRDiagnosticError) as caught:
			report.create_sr()
		self.assertEqual(caught.exception.diagnostic.condition, SR_CONDITION_SHARED_TRACKING_UID)
		# A copy of a set whose group is unique replaces that group
		report = self.legacy()
		copied = copy.deepcopy(report.landmarks[0])
		copied.description = 'copied and edited'
		report.landmarks = [copied, report.landmarks[1]]
		out = disk(report.create_sr())
		self.assertEqual(len(self.volume(out).ContentSequence), 1)
		self.assertEqual(len(landmark_items(set_group(out, L.SET_UIDS['legacy']))), 3)

	def test_ambiguous_own_group_refused(self):
		ds = fixture('legacy')
		volume = self.volume(ds)
		volume.ContentSequence.append(copy.deepcopy(volume.ContentSequence[0]))
		report = self.legacy(disk(ds))
		# The items the sets were read from are replaced by equal copies: the set's group cannot be told apart
		report.extra_items = copy.deepcopy(report.extra_items)
		report.landmarks = [report.landmarks[0]]
		report.landmarks[0].description = 'ambiguous'
		with self.assertRaises(SRDiagnosticError) as caught:
			report.create_sr()
		self.assertEqual(caught.exception.diagnostic.condition, SR_CONDITION_SHARED_TRACKING_UID)

	def test_tracking_item_content_refuses_migration(self):
		ds = fixture('legacy')
		volume = self.volume(ds)
		group = volume.ContentSequence[0]
		tracking = next(c for c in group.ContentSequence if srcontent_is_named(c, codes.DCM.TrackingIdentifier))
		tracking.ObservationDateTime = '20261114120000'
		ds = disk(ds)
		report = self.legacy(ds)
		lset = next(item for item in report.landmarks if item.tracking_uid == L.SET_UIDS['legacy'])
		self.assert_refused(lambda: setattr(lset, 'label', 'Renamed'), 'tracking item', 'ObservationDateTime')
		self.assertEqual(item_bytes(self.volume(disk(report.create_sr()))), item_bytes(self.volume(ds)))

	def test_unframed_or_malformed_legacy_point_is_never_placed(self):
		for change, reason in ((lambda item: delattr(item, 'ReferencedFrameOfReferenceUID'), 'names no Frame of Reference'),
				(lambda item: setattr(item, 'GraphicData', [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]), 'unusable GraphicData')):
			ds = fixture('legacy')
			point = [item for item in self.legacy_items(ds, 'POINT') if str(item.get('FiducialUID') or '') == L._fid('legacy-point')][0]
			change(point)
			ds = disk(ds)
			report = self.legacy(ds)
			lset = report.landmarks[0]
			self.assertNotIn(L._fid('legacy-point'), [l.fiducial_uid for l in lset.landmarks])
			self.assertEqual(lset.frame_of_reference_uid, P)
			self.assert_refused(lambda: setattr(lset, 'description', 'edit'), reason)
			self.assertEqual(item_bytes(self.volume(disk(report.create_sr()))), item_bytes(self.volume(ds)))
			report.frames.add_frame(ReferenceFrame(L.APP_FRAME, name='Anatomic'))
			report.frames.add_edge(F.RZ, L.APP_FRAME, P)
			with self.assertRaises(FrameConversionError):
				report.convert(lset, L.APP_FRAME)

	def test_mixed_frame_group(self):
		ds = fixture('legacy-mixed')
		report = self.legacy(ds)
		lset = report.landmarks[0]
		self.assertTrue(conditions(report, SR_CONDITION_LANDMARK_SET_MIXED_FRAMES))
		self.assertEqual(lset.frame_of_reference_uid, P)
		self.assertTrue(np.allclose(lset.array, [(1, 2, 3), (7, 8, 9)]))
		self.assertEqual(lset.tracking_uid, L.SET_UIDS['legacy-mixed'])
		self.assertEqual([p.frame_uid for p in report.point_collections], [P, S, P])
		self.assertEqual(item_bytes(self.volume(disk(report.create_sr()))), item_bytes(self.volume(ds)))
		with self.assertRaises(FrameConversionError):
			report.convert(lset, P)

	def test_unknown_legacy_type_kept_through_an_edit(self):
		ds = fixture('legacy')
		for container in ds.ContentSequence:
			for group in container.get('ContentSequence') or []:
				for item in group.get('ContentSequence') or []:
					if item.get('GraphicType') == 'MULTIPOINT':
						item.ConceptNameCodeSequence = Sequence([srcode2dataset(Code('CUSTOM-0.2', '99OTHER', 'Custom'))])
		report = self.legacy(ds)
		self.assertEqual(code_key(report.landmarks[0].landmarks[1].landmark_type), ('99OTHER', 'CUSTOM-0.2'))
		report.landmarks[0].description = 'different field'
		reloaded = self.legacy(disk(report.create_sr()))
		self.assertEqual(code_key(reloaded.landmarks[0].landmarks[1].landmark_type), ('99OTHER', 'CUSTOM-0.2'))


# ---- fixtures ----


class LandmarkFixtureTestCase(unittest.TestCase):
	def test_committed_fixtures_reproduce(self):
		with tempfile.TemporaryDirectory() as directory:
			L.write_fixtures(directory)
			for name, (topic, _, _, _, _) in L.FIXTURES.items():
				with open(os.path.join(directory, L.fixture_name(topic)), 'rb') as a, \
						open(os.path.join(L.FIXTURES_DIR, L.fixture_name(topic)), 'rb') as b:
					self.assertEqual(a.read(), b.read(), name)

	def test_root_title_and_template_recorded(self):
		for name, (topic, _, title, template, _) in L.FIXTURES.items():
			ds = pydicom.dcmread(os.path.join(L.FIXTURES_DIR, L.fixture_name(topic)))
			self.assertEqual(str(ds.ContentTemplateSequence[0].TemplateIdentifier), template, name)
			value = dcm_code_value(ds.ConceptNameCodeSequence[0])
			self.assertTrue(title.startswith(value), (name, value, title))
