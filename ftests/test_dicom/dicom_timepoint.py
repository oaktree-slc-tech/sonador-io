'''	Offline tests of the timepoint value (`sonador.apisettings.base.Timepoint`) and of the reader and writer of the
	series-level timepoint (`sonador.imaging.helpers.timepoint`), on pydicom datasets and on mappings of tags. The
	encoding is the contract of oak-tree/medical-imaging/sonador-client#50.

	Run from the directory containing the `sonador` package:
		python -m unittest sonador.ftests.test_dicom.dicom_timepoint
'''
import copy, datetime, io, logging, unittest, warnings

import pydicom
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.sequence import Sequence
from pydicom.uid import ExplicitVRLittleEndian

from ...apisettings.base import Timepoint, SONADOR_TIMEPOINT_SOP_CLASS_UID, TIMEPOINT_DEFAULT, TIMEPOINT_ID_MAX_LENGTH, \
	TIMEPOINT_DESCRIPTION_MAX_LENGTH, DCMTS_PERFORMED_PROCEDURE_STEP, DCMHEADER_TIMEPOINT_MIRROR, IMAGING_SERVER_RESOURCE_SERIES
from ...apisettings.sr import SRDiagnosticError, SR_CONDITION_TIMEPOINT_MULTIPLE_ITEMS, SR_CONDITION_TIMEPOINT_MISSING_UID, \
	SR_CONDITION_TIMEPOINT_INVALID_DATE
from ...imaging.helpers.timepoint import dcm_timepoint, dcm_timepoint_items, dcm_encode_timepoint_item, dcm_set_timepoint, \
	dcm_remove_timepoint, dcm_copy_foreign_items

__all__ = ['TimepointValueTestCase', 'TimepointReaderTestCase', 'TimepointWriterTestCase']

MPPS = '1.2.840.10008.3.1.2.3.3'
SEQUENCE = 'ReferencedPerformedProcedureStepSequence'
UID, OTHER_UID = '2.25.151286667574101141145491442274872811985', '2.25.999'
TP = Timepoint(UID, 'TP-POSTOP-6W', '20261104', '093000', 'Post-operative review, 6 weeks')


def setUpModule():
	logging.getLogger('sonador').setLevel(logging.CRITICAL)


def tearDownModule():
	logging.getLogger('sonador').setLevel(logging.NOTSET)


def foreign_item(uid='1.2.3.4.5.6.7.8.9.10'):
	'''	A Modality Performed Procedure Step item with an attribute the reference macro does not define '''
	item = Dataset()
	item.ReferencedSOPClassUID, item.ReferencedSOPInstanceUID, item.InstanceNumber = MPPS, uid, 7
	return item


def item_dict(timepoint=TP, **overrides):
	'''	A timepoint item as a mapping of tags (the simplified tags of an imaging server) '''
	found = {'ReferencedSOPClassUID': SONADOR_TIMEPOINT_SOP_CLASS_UID, 'ReferencedSOPInstanceUID': timepoint.uid,
		'PerformedProcedureStepID': timepoint.id, 'PerformedProcedureStepStartDate': timepoint.date,
		'PerformedProcedureStepStartTime': timepoint.time, 'PerformedProcedureStepDescription': timepoint.description}
	found.update(overrides)
	return dict((k, v) for k, v in found.items() if v is not None)


def mirror_of(ds):
	return dict((name, ds.get(name)) for name in DCMHEADER_TIMEPOINT_MIRROR if name in ds)


def roundtrip(ds):
	ds = copy.deepcopy(ds)
	ds.file_meta = FileMetaDataset()
	ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
	buf = io.BytesIO()
	ds.save_as(buf, enforce_file_format=False)
	buf.seek(0)
	return pydicom.dcmread(buf, force=True)


class TimepointValueTestCase(unittest.TestCase):
	def test_constants(self):
		self.assertEqual(SONADOR_TIMEPOINT_SOP_CLASS_UID, '2.25.33188558902346601872773385043464853716')
		self.assertEqual((TIMEPOINT_ID_MAX_LENGTH, TIMEPOINT_DESCRIPTION_MAX_LENGTH), (16, 64))
		self.assertEqual(tuple(DCMTS_PERFORMED_PROCEDURE_STEP), (IMAGING_SERVER_RESOURCE_SERIES, 'PerformedProcedureStepStartDate',
			'PerformedProcedureStepStartTime'))
		self.assertTrue(TIMEPOINT_DEFAULT)

	def test_immutable(self):
		with self.assertRaises(AttributeError):
			TP.description = 'changed'
		with self.assertRaises(AttributeError):
			TP.other = 1

	def test_identity_is_the_uid_and_equality_every_field(self):
		later = TP._replace(time='101500')
		self.assertTrue(TP.same(later))
		self.assertNotEqual(TP, later)
		self.assertEqual(TP, Timepoint(UID, 'TP-POSTOP-6W', '20261104', '093000', 'Post-operative review, 6 weeks'))
		self.assertFalse(TP.same(TP._replace(uid=OTHER_UID)))
		self.assertFalse(TP.same(None))
		# Presence takes part in equality
		self.assertNotEqual(Timepoint(UID, description=''), Timepoint(UID))

	def test_create_generates_a_uuid_derived_uid_or_keeps_the_one_given(self):
		created = Timepoint.create('Review', datetime.datetime(2026, 11, 4, 9, 30), id='TP-1')
		self.assertTrue(created.uid.startswith('2.25.'))
		self.assertNotEqual(created.uid, Timepoint.create('Review').uid)
		self.assertEqual((created.date, created.time, created.id, created.description), ('20261104', '093000', 'TP-1', 'Review'))
		self.assertEqual(Timepoint.create(uid=UID).uid, UID)
		self.assertEqual(Timepoint.create(ts=datetime.date(2026, 11, 4), uid=UID), Timepoint(UID, date='20261104'))
		self.assertEqual(Timepoint.create(ts=datetime.datetime(2026, 11, 4, 9, 30, 0, 250000), uid=UID).time, '093000.250000')

	def test_ts_is_local_midnight_for_a_date_alone_and_none_without_a_valid_date(self):
		self.assertEqual(TP.ts, datetime.datetime(2026, 11, 4, 9, 30))
		self.assertEqual(Timepoint(UID, date='20261104').ts, datetime.datetime(2026, 11, 4))
		self.assertEqual(Timepoint(UID, date='20261104', time='0930').ts, datetime.datetime(2026, 11, 4, 9, 30))
		self.assertEqual(Timepoint(UID, date='20261104', time='9:30').ts, datetime.datetime(2026, 11, 4))
		for date in (None, '2026-11-04', '202611', '20261399'):
			self.assertIsNone(Timepoint(UID, date=date, time='093000').ts)

	def test_validate_refuses_what_a_writer_does_not_write(self):
		faults = (dict(id='x' * 17), dict(description='x' * 65), dict(date=None), dict(uid=None), dict(uid=' '),
			dict(date='2026-11-04'), dict(time='9:30'), dict(time='250000'))
		for fault in faults:
			with self.assertRaises(ValueError, msg=fault):
				TP._replace(**fault).validate()
		for fault in (dict(id='x' * 17), dict(description='x' * 65), dict(uid=UID, ts=None, id=None, description='x' * 65)):
			with self.assertRaises(ValueError, msg=fault):
				Timepoint.create(**dict(dict(description='d', ts=datetime.datetime(2026, 1, 1), id='i', uid=UID), **fault))
		with self.assertRaises(ValueError):
			Timepoint.create(ts='20261104')
		# The limits themselves are accepted
		self.assertIs(TP._replace(id='x' * 16, description='y' * 64).validate().id, TP._replace(id='x' * 16).id)

	def test_a_value_built_directly_is_not_checked(self):
		held = Timepoint(UID, 'x' * 17, 'not a date', '9:30', 'y' * 65)
		self.assertEqual(held.description, 'y' * 65)
		self.assertIsNone(held.ts)

	def test_sort_key_is_the_order_of_the_contract(self):
		ordered = [
			Timepoint('2.25.9', date='20260101', time='093000', description='b'),
			Timepoint('2.25.8', date='20260101', time='0930', description='c'),			# same time once padded
			Timepoint('2.25.7', date='20260101', time='093000.5', description='a'),
			Timepoint('2.25.6', date='20260102'),											# no time: twelve zeros
			Timepoint('2.25.5', date='20260102', time='000001'),
			Timepoint('2.25.3', date='20260103', time='9:30', description='x'),				# invalid time: twelve zeros
			Timepoint('2.25.4', date='20260103', time='000000', description='x'),
			Timepoint('2.25.2', description='A'),											# undated last
			Timepoint('2.25.1', date='2026-01-01', description='B'),						# invalid date is undated
			Timepoint('2.25.0'),
		]
		expected = ['2.25.9', '2.25.8', '2.25.7', '2.25.6', '2.25.5', '2.25.3', '2.25.4', '2.25.0', '2.25.2', '2.25.1']
		for candidates in (ordered, list(reversed(ordered))):
			self.assertEqual([t.uid for t in sorted(candidates, key=lambda t: t.sort_key)], expected)

	def test_descriptions_are_ordered_by_utf16_code_unit(self):
		# U+10000 is the surrogates D800 DC00 in UTF-16 and sorts before U+E000; by code point it would sort after
		supplementary = Timepoint('2.25.2', date='20260101', description='\U00010000')
		private_use = Timepoint('2.25.1', date='20260101', description='\ue000')
		self.assertLess('\ue000', '\U00010000')
		for candidates in ([supplementary, private_use], [private_use, supplementary]):
			self.assertEqual(sorted(candidates, key=lambda t: t.sort_key), [supplementary, private_use])
		# Equal descriptions fall back to the UID, and an absent description is the empty string
		same = [Timepoint('2.25.2', date='20260101', description=''), Timepoint('2.25.1', date='20260101')]
		self.assertEqual([t.uid for t in sorted(same, key=lambda t: t.sort_key)], ['2.25.1', '2.25.2'])
		lone = Timepoint('2.25.3', date='20260101', description='\ud800')
		self.assertLess(lone.sort_key, private_use.sort_key)


class TimepointReaderTestCase(unittest.TestCase):
	def dataset(self, items):
		ds = Dataset()
		ds.SOPInstanceUID = '1.2.3'
		ds.ReferencedPerformedProcedureStepSequence = Sequence(items)
		return ds

	def test_reads_the_item_from_a_dataset_at_any_position(self):
		item = dcm_encode_timepoint_item(TP)
		for items in ([item], [item, foreign_item()], [foreign_item(), item], [foreign_item('1.1'), item, foreign_item('1.2')]):
			ds = self.dataset([copy.deepcopy(i) for i in items])
			self.assertEqual(dcm_timepoint(ds), TP)
			self.assertEqual(dcm_timepoint(roundtrip(ds)), TP)
			self.assertEqual([position for position, _ in dcm_timepoint_items(ds)], [items.index(item)])

	def test_reads_a_mapping_of_tags_as_a_list_and_as_a_single_item(self):
		foreign = {'ReferencedSOPClassUID': MPPS, 'ReferencedSOPInstanceUID': '1.2.3'}
		for value in ([item_dict()], [foreign, item_dict()], [item_dict(), foreign], item_dict()):
			self.assertEqual(dcm_timepoint({SEQUENCE: value}), TP)
		self.assertEqual(dcm_timepoint({SEQUENCE: item_dict(Timepoint(UID))}), Timepoint(UID))

	def test_none_without_a_timepoint_item(self):
		series_level = {'PerformedProcedureStepID': 'ID', 'PerformedProcedureStepStartDate': '20261104',
			'PerformedProcedureStepDescription': 'Description'}
		for headers in ({}, {SEQUENCE: []}, {SEQUENCE: None}, {SEQUENCE: [{'ReferencedSOPClassUID': MPPS, 'ReferencedSOPInstanceUID': UID}]},
				series_level, dict(series_level, **{SEQUENCE: []}), None, Dataset(), self.dataset([]), self.dataset([foreign_item()])):
			diagnostics = []
			self.assertIsNone(dcm_timepoint(headers, diagnostics=diagnostics, strict=True))
			self.assertEqual(diagnostics, [])
			self.assertEqual(dcm_timepoint_items(headers), [])

	def test_several_items_use_the_first(self):
		headers = {'SOPInstanceUID': '1.2.3', SEQUENCE: [item_dict(), item_dict(TP._replace(uid=OTHER_UID))]}
		diagnostics = []
		self.assertEqual(dcm_timepoint(headers, diagnostics=diagnostics), TP)
		self.assertEqual([(d.condition, d.severity, d.path) for d in diagnostics],
			[(SR_CONDITION_TIMEPOINT_MULTIPLE_ITEMS, 'warning', 'instance 1.2.3')])
		with self.assertRaises(SRDiagnosticError):
			dcm_timepoint(headers, strict=True)

	def test_first_item_without_a_uid_is_no_timepoint(self):
		for first in (item_dict(ReferencedSOPInstanceUID=None), item_dict(ReferencedSOPInstanceUID=''), item_dict(ReferencedSOPInstanceUID='  ')):
			for items in ([first], [first, item_dict(TP._replace(uid=OTHER_UID))]):
				diagnostics = []
				self.assertIsNone(dcm_timepoint({SEQUENCE: items}, diagnostics=diagnostics))
				self.assertIn(SR_CONDITION_TIMEPOINT_MISSING_UID, [d.condition for d in diagnostics])
				with self.assertRaises(SRDiagnosticError):
					dcm_timepoint({SEQUENCE: [first]}, strict=True)

	def test_invalid_date_and_time_keep_their_text(self):
		for field, keyword, text in (('date', 'PerformedProcedureStepStartDate', '2026-11-04'), ('date', 'PerformedProcedureStepStartDate', '202611'),
				('time', 'PerformedProcedureStepStartTime', '9:30'), ('time', 'PerformedProcedureStepStartTime', '2500')):
			headers = {SEQUENCE: [item_dict(**{keyword: text})]}
			diagnostics = []
			timepoint = dcm_timepoint(headers, diagnostics=diagnostics)
			self.assertEqual(getattr(timepoint, field), text)
			self.assertEqual([(d.condition, d.value) for d in diagnostics], [(SR_CONDITION_TIMEPOINT_INVALID_DATE, text)])
			with self.assertRaises(SRDiagnosticError):
				dcm_timepoint(headers, strict=True)

	def test_values_over_the_limits_are_returned_as_read(self):
		long_id, long_description = 'x' * 17, 'y' * 65
		diagnostics = []
		timepoint = dcm_timepoint({SEQUENCE: [item_dict(PerformedProcedureStepID=long_id, PerformedProcedureStepDescription=long_description)]},
			diagnostics=diagnostics, strict=True)
		self.assertEqual((timepoint.id, timepoint.description), (long_id, long_description))
		self.assertEqual(diagnostics, [])

	def test_optional_values_keep_their_presence(self):
		absent = Timepoint(UID)
		empty = Timepoint(UID, id='', date='', time='', description='')
		self.assertNotEqual(absent, empty)
		keywords = ('PerformedProcedureStepID', 'PerformedProcedureStepStartDate', 'PerformedProcedureStepStartTime',
			'PerformedProcedureStepDescription')
		base = {'ReferencedSOPClassUID': SONADOR_TIMEPOINT_SOP_CLASS_UID, 'ReferencedSOPInstanceUID': UID}
		# A mapping of tags: an absent key, an empty text and a null value
		self.assertEqual(dcm_timepoint({SEQUENCE: [dict(base)]}), absent)
		self.assertEqual(dcm_timepoint({SEQUENCE: [dict(base, **dict((k, '') for k in keywords))]}), empty)
		self.assertEqual(dcm_timepoint({SEQUENCE: [dict(base, **dict((k, None) for k in keywords))]}), empty)
		self.assertEqual(dcm_timepoint({SEQUENCE: [dict(base, PerformedProcedureStepDescription=' ')]}), Timepoint(UID, description=' '))
		# A dataset, in memory and read back from its bytes; an empty date or time is not an invalid one
		for timepoint in (absent, empty, Timepoint(UID, id='', description='Described'), Timepoint(UID, 'ID', description='')):
			ds = self.dataset([dcm_encode_timepoint_item(timepoint)])
			item = ds.ReferencedPerformedProcedureStepSequence[0]
			self.assertEqual([k in item for k in keywords], [v is not None for v in timepoint[1:]])
			for source in (ds, roundtrip(ds)):
				diagnostics = []
				self.assertEqual(dcm_timepoint(source, diagnostics=diagnostics, strict=True), timepoint)
				self.assertEqual(diagnostics, [])
		whitespace = self.dataset([dcm_encode_timepoint_item(Timepoint(UID, description=' '))])
		self.assertEqual(dcm_timepoint(whitespace), Timepoint(UID, description=' '))
		self.assertIsNotNone(dcm_timepoint(roundtrip(whitespace)).description)
		# The UID is required: empty or absent, the dataset has no timepoint
		self.assertIsNone(dcm_timepoint({SEQUENCE: [dict(base, ReferencedSOPInstanceUID='')]}))

	def test_a_repeated_read_records_a_condition_once(self):
		headers = {'SOPInstanceUID': '1.2.3', SEQUENCE: [item_dict(PerformedProcedureStepStartDate='bad')]}
		diagnostics = []
		for _ in range(3):
			dcm_timepoint(headers, diagnostics=diagnostics)
		self.assertEqual(len(diagnostics), 1)
		with self.assertRaises(SRDiagnosticError):
			dcm_timepoint(headers, diagnostics=diagnostics, strict=True)


class TimepointWriterTestCase(unittest.TestCase):
	def with_foreign(self):
		ds = Dataset()
		ds.ReferencedPerformedProcedureStepSequence = Sequence([foreign_item()])
		ds.PerformedProcedureStepID, ds.PerformedProcedureStepDescription = 'EXISTING-ID', 'EXISTING DESCRIPTION'
		return ds

	def test_writer_refuses_what_validate_refuses(self):
		for fault in (dict(id='x' * 17), dict(description='x' * 65), dict(date=None), dict(uid=None)):
			with self.assertRaises(ValueError, msg=fault):
				dcm_set_timepoint(Dataset(), TP._replace(**fault))
		for value in (None, tuple(TP), 'timepoint'):
			with self.assertRaises(ValueError):
				dcm_set_timepoint(Dataset(), value)
		with self.assertRaises(ValueError):
			dcm_set_timepoint(Dataset(), TP._replace(uid=None), validate=False)

	def test_unchecked_write_keeps_the_values_as_they_are(self):
		held = Timepoint(UID, 'x' * 17, '2026-11-04', '9:30', 'y' * 70)
		with warnings.catch_warnings():
			warnings.simplefilter('ignore')
			ds = dcm_set_timepoint(Dataset(), held, validate=False)
			self.assertEqual(dcm_timepoint(ds), held)
			self.assertEqual(dcm_timepoint(roundtrip(ds)), held)

	def test_foreign_item_and_series_level_values_are_untouched(self):
		ds, before = self.with_foreign(), self.with_foreign()
		dcm_set_timepoint(ds, TP)
		items = list(ds.ReferencedPerformedProcedureStepSequence)
		self.assertEqual(len(items), 2)
		self.assertEqual(items[0], before.ReferencedPerformedProcedureStepSequence[0])
		self.assertEqual(items[0].InstanceNumber, 7)
		self.assertEqual(items[1], dcm_encode_timepoint_item(TP))
		self.assertEqual(mirror_of(ds), mirror_of(before))
		self.assertEqual(dcm_timepoint(ds), TP)

	def test_mirror_default_writes_the_copy_only_when_none_is_held(self):
		ds = dcm_set_timepoint(Dataset(), TP)
		self.assertEqual(mirror_of(ds), {'PerformedProcedureStepID': 'TP-POSTOP-6W', 'PerformedProcedureStepStartDate': '20261104',
			'PerformedProcedureStepStartTime': '093000', 'PerformedProcedureStepDescription': 'Post-operative review, 6 weeks'})
		for name in DCMHEADER_TIMEPOINT_MIRROR:
			held = Dataset()
			setattr(held, name, '')				# present and empty counts as held
			dcm_set_timepoint(held, TP)
			self.assertEqual(list(mirror_of(held)), [name])
			self.assertFalse(held.get(name))

	def test_empty_values_are_written_as_attributes_without_a_value(self):
		empty = Timepoint(UID, id='', description='')
		ds = dcm_set_timepoint(Dataset(), empty)
		item = dcm_timepoint_items(ds)[0][1]
		self.assertEqual(sorted(elem.keyword for elem in item),
			['PerformedProcedureStepDescription', 'PerformedProcedureStepID', 'ReferencedSOPClassUID', 'ReferencedSOPInstanceUID'])
		self.assertEqual(mirror_of(ds), {'PerformedProcedureStepID': '', 'PerformedProcedureStepDescription': ''})
		self.assertEqual(dcm_timepoint(roundtrip(ds)), empty)
		empty.validate()

	def test_mirror_false_and_true(self):
		self.assertEqual(mirror_of(dcm_set_timepoint(Dataset(), TP, mirror=False)), {})
		ds = self.with_foreign()
		ds.PerformedProcedureStepStartTime = '111111'
		dcm_set_timepoint(ds, Timepoint(UID, date='20261104', description='Only a date'), mirror=True)
		self.assertEqual(mirror_of(ds), {'PerformedProcedureStepStartDate': '20261104', 'PerformedProcedureStepDescription': 'Only a date'})

	def test_writing_twice_is_writing_once_and_replacement_drops_missing_fields(self):
		once, twice = dcm_set_timepoint(self.with_foreign(), TP), dcm_set_timepoint(dcm_set_timepoint(self.with_foreign(), TP), TP)
		self.assertEqual(once, twice)
		replaced = dcm_set_timepoint(dcm_set_timepoint(Dataset(), TP, mirror=False), Timepoint(UID, description='Shorter'), mirror=False)
		item = dcm_timepoint_items(replaced)[0][1]
		self.assertEqual(sorted(elem.keyword for elem in item),
			['PerformedProcedureStepDescription', 'ReferencedSOPClassUID', 'ReferencedSOPInstanceUID'])
		self.assertEqual(dcm_timepoint(replaced), Timepoint(UID, description='Shorter'))

	def test_another_uid_and_several_items_need_replace(self):
		ds = dcm_set_timepoint(self.with_foreign(), TP)
		with self.assertRaises(ValueError):
			dcm_set_timepoint(ds, TP._replace(uid=OTHER_UID))
		self.assertEqual(dcm_timepoint(ds), TP)
		several = self.with_foreign()
		several.ReferencedPerformedProcedureStepSequence.extend([dcm_encode_timepoint_item(TP), dcm_encode_timepoint_item(TP)])
		with self.assertRaises(ValueError):
			dcm_set_timepoint(several, TP)
		for dataset in (ds, several):
			dcm_set_timepoint(dataset, TP._replace(uid=OTHER_UID), replace=True)
			self.assertEqual([dcm_timepoint(dataset).uid, len(dcm_timepoint_items(dataset))], [OTHER_UID, 1])
			self.assertEqual(dataset.ReferencedPerformedProcedureStepSequence[0], foreign_item())
			self.assertEqual(len(dataset.ReferencedPerformedProcedureStepSequence), 2)

	def test_remove_restores_a_dataset_the_writer_filled(self):
		original = Dataset()
		original.SOPInstanceUID = '1.2.3'
		original.ReferencedPerformedProcedureStepSequence = Sequence([])
		written = dcm_set_timepoint(copy.deepcopy(original), TP)
		self.assertNotEqual(written, original)
		self.assertEqual(dcm_remove_timepoint(written, mirror=True), original)

	def test_remove_leaves_series_level_values_unless_the_caller_owns_them(self):
		ds = dcm_set_timepoint(Dataset(), TP)
		self.assertEqual(mirror_of(dcm_remove_timepoint(ds)), mirror_of(dcm_set_timepoint(Dataset(), TP)))		# equal to the item, kept
		self.assertEqual(len(ds.ReferencedPerformedProcedureStepSequence), 0)
		kept = dcm_remove_timepoint(dcm_set_timepoint(self.with_foreign(), TP))
		self.assertEqual(list(kept.ReferencedPerformedProcedureStepSequence), [foreign_item()])
		absent = dcm_remove_timepoint(Dataset())
		self.assertIn(SEQUENCE, absent)
		self.assertEqual(len(absent.ReferencedPerformedProcedureStepSequence), 0)

	def test_copy_foreign_items(self):
		source = dcm_set_timepoint(self.with_foreign(), TP)
		source.ReferencedPerformedProcedureStepSequence.insert(0, foreign_item('1.1'))
		target = dcm_set_timepoint(Dataset(), TP._replace(uid=OTHER_UID), mirror=False)
		dcm_copy_foreign_items(source, target)
		items = list(target.ReferencedPerformedProcedureStepSequence)
		self.assertEqual([str(i.ReferencedSOPInstanceUID) for i in items], ['1.1', '1.2.3.4.5.6.7.8.9.10', OTHER_UID])
		self.assertIsNot(items[0], source.ReferencedPerformedProcedureStepSequence[0])
		self.assertEqual(items[1].InstanceNumber, 7)
		# A source without other items leaves the destination as it is, sequence included
		untouched = Dataset()
		dcm_copy_foreign_items(dcm_set_timepoint(Dataset(), TP), untouched)
		self.assertNotIn(SEQUENCE, untouched)

	def test_copy_foreign_items_takes_datasets_only(self):
		tags = {SEQUENCE: [{'ReferencedSOPClassUID': MPPS, 'ReferencedSOPInstanceUID': '1.1'}]}
		for source, target in ((tags, Dataset()), (None, Dataset()), (Dataset(), tags)):
			with self.assertRaises(TypeError):
				dcm_copy_foreign_items(source, target)
