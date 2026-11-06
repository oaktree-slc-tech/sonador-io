'''	Offline tests of the coded series attributes (`sonador.imaging.helpers.codes`): the readers in the
	pydicom and simplified-tags forms, the fallback from the series tags to the first instance, the
	writer that adds codes after the items of the Series Description Code Sequence, and the record of
	modified attributes of `dcmheader_modify`.

	Run from the directory containing the `sonador` package:
		python -m unittest sonador.ftests.test_dicom.dicom_codes
'''
import copy
import datetime
import unittest
from io import BytesIO
from types import SimpleNamespace

from pydicom import dcmread, dcmwrite
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.sequence import Sequence
from pydicom.uid import ExplicitVRLittleEndian, generate_uid

from ...apisettings import Code, CodedConcept, SONADOR_CLIENT, DCMHEADER_SERIES_DESCRIPTION_CODE_SEQUENCE
from ...imaging.helpers.codes import dcm_coded_concepts, dcm_code_in, dcm_series_tags, \
	dcm_series_description_codes, dcm_merge_series_description_codes, dcm_add_series_description_codes
from ...imaging.helpers.workflow import dcmheader_modify
from ...imaging.orthanc.base import ImagingSeries


__all__ = ['CodedConceptsTestCase', 'SeriesCodesReaderTestCase', 'SeriesCodesWriterTestCase', 'HeaderModifyTestCase']

KEYWORD = DCMHEADER_SERIES_DESCRIPTION_CODE_SEQUENCE
CODE_A = Code('series.a', 'TEST-SCHEME', 'Series / A', scheme_version='1.0')
CODE_B = Code('series.b', 'TEST-SCHEME', 'Series / B', scheme_version='1.0')
FOREIGN = {'CodeValue': 'F1', 'CodingSchemeDesignator': 'VENDOR', 'CodeMeaning': 'Vendor code'}


def code_item(value='F1', designator='VENDOR', meaning='Vendor code', version=None):
	item = Dataset()
	item.CodeValue = value
	item.CodingSchemeDesignator = designator
	item.CodeMeaning = meaning
	if version:
		item.CodingSchemeVersion = version
	return item


def coded_dataset(*items):
	ds = Dataset()
	ds.PatientID = 'P1'
	if items:
		setattr(ds, KEYWORD, Sequence(list(items)))
	return ds


def round_trip(ds):
	'''	Write a dataset to a part-10 stream and read it back '''
	ds.SOPClassUID = '1.2.840.10008.5.1.4.1.1.7'
	ds.SOPInstanceUID = generate_uid()
	ds.file_meta = FileMetaDataset()
	ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
	ds.file_meta.MediaStorageSOPClassUID = ds.SOPClassUID
	ds.file_meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
	stream = BytesIO()
	dcmwrite(stream, ds, enforce_file_format=True)
	stream.seek(0)
	return dcmread(stream)


def values(codes):
	return [code.value for code in codes]


class CodedConceptsTestCase(unittest.TestCase):

	def test_simplified_tags(self):
		concepts = dcm_coded_concepts([dict(FOREIGN, CodingSchemeVersion='2')])
		self.assertEqual(len(concepts), 1)
		self.assertIsInstance(concepts[0], CodedConcept)
		self.assertEqual((concepts[0].value, concepts[0].scheme_designator, concepts[0].meaning, concepts[0].scheme_version),
			('F1', 'VENDOR', 'Vendor code', '2'))

	def test_pydicom_sequence(self):
		self.assertEqual(values(dcm_coded_concepts(Sequence([code_item(), code_item('F2')]))), ['F1', 'F2'])

	def test_mixed_list(self):
		mixed = [CODE_A, CodedConcept('c', 'S', 'm'), code_item(), FOREIGN]
		self.assertEqual(values(dcm_coded_concepts(mixed)), ['series.a', 'c', 'F1', 'F1'])

	def test_single_items(self):
		self.assertEqual(values(dcm_coded_concepts(CODE_A)), ['series.a'])
		self.assertEqual(values(dcm_coded_concepts(code_item())), ['F1'])
		self.assertEqual(values(dcm_coded_concepts(FOREIGN)), ['F1'])

	def test_generator_is_read_once(self):
		self.assertEqual(values(dcm_coded_concepts(item for item in [CODE_A, CODE_B])), ['series.a', 'series.b'])

	def test_missing_meaning_and_version(self):
		concept = dcm_coded_concepts([{'CodeValue': 'x', 'CodingSchemeDesignator': 'S'}])[0]
		self.assertEqual(concept.meaning, '')
		self.assertIsNone(concept.scheme_version)

	def test_long_code_value(self):
		item = {'LongCodeValue': 'v' * 20, 'CodingSchemeDesignator': 'S', 'CodeMeaning': 'm'}
		self.assertEqual(values(dcm_coded_concepts([item])), ['v' * 20])

	def test_items_without_value_or_designator_are_skipped(self):
		items = [{'CodeValue': 'x'}, {'CodingSchemeDesignator': 'S'}, {}, 'text', 7, None, FOREIGN]
		self.assertEqual(values(dcm_coded_concepts(items)), ['F1'])

	def test_empty_forms(self):
		for empty in (None, [], '', 'text', Sequence()):
			self.assertEqual(dcm_coded_concepts(empty), [])

	def test_code_in_ignores_version_and_meaning(self):
		concepts = dcm_coded_concepts([{'CodeValue': 'series.a', 'CodingSchemeDesignator': 'TEST-SCHEME', 'CodeMeaning': 'Other'}])
		self.assertTrue(dcm_code_in(concepts, CODE_A))
		self.assertFalse(dcm_code_in(concepts, CODE_B))
		self.assertFalse(dcm_code_in(concepts, Code('series.a', 'OTHER', 'Series / A')))
		self.assertFalse(dcm_code_in(None, CODE_A))

	def test_code_in_complete_identities(self):
		'''	Dataset, dictionary, CodedConcept and Code identities match by value and designator only '''
		same = ({'CodeValue': 'series.a', 'CodingSchemeDesignator': 'TEST-SCHEME'}, code_item('series.a', 'TEST-SCHEME', 'Other', '9'),
			CodedConcept('series.a', 'TEST-SCHEME', 'Other'), Code('series.a', 'TEST-SCHEME', 'Other'))
		other = ({'CodeValue': 'series.a', 'CodingSchemeDesignator': 'OTHER'}, code_item('series.a', 'OTHER'),
			CodedConcept('series.a', 'OTHER', 'm'), Code('series.a', 'OTHER', 'm'),
			{'CodeValue': 'series.b', 'CodingSchemeDesignator': 'TEST-SCHEME'}, Code('series.b', 'TEST-SCHEME', 'm'))
		for item in same:
			for expected in same:
				self.assertTrue(dcm_code_in([item], expected), (item, expected))
			for expected in other:
				self.assertFalse(dcm_code_in([item], expected), (item, expected))

	def test_code_in_incomplete_identities_never_match(self):
		incomplete = ({}, {'CodeValue': 'X'}, {'CodingSchemeDesignator': 'A'}, {'CodeValue': '', 'CodingSchemeDesignator': 'A'},
			{'CodeValue': 'X', 'CodingSchemeDesignator': '  '}, Dataset(), Code('', 'A', 'm'), Code('X', '', 'm'), None, 'X')
		complete = {'CodeValue': 'X', 'CodingSchemeDesignator': 'A'}
		for item in incomplete:
			self.assertFalse(dcm_code_in([complete], item), item)
			self.assertFalse(dcm_code_in([item], complete), item)
			for other in incomplete:
				self.assertFalse(dcm_code_in([item], other), (item, other))

	def test_code_in_finds_a_code_after_an_incomplete_item(self):
		self.assertTrue(dcm_code_in([{}, {'CodeValue': 'X'}, CODE_A], CODE_A))

	def test_code_in_reads_a_generator(self):
		self.assertTrue(dcm_code_in((concept for concept in dcm_coded_concepts([CODE_B, CODE_A])), CODE_A))


class SeriesCodesReaderTestCase(unittest.TestCase):

	def test_series_tags_win_without_touching_the_instance(self):
		class Series:
			dicomdata = {KEYWORD: [FOREIGN]}
			@property
			def dcm0(self):
				raise AssertionError('first instance read')

		self.assertEqual(values(dcm_series_description_codes(Series())), ['F1'])

	def test_fallback_to_the_first_instance(self):
		sx = SimpleNamespace(dicomdata={'Modality': 'CT'}, dcm0=SimpleNamespace(tags={KEYWORD: [FOREIGN]}))
		self.assertEqual(values(dcm_series_description_codes(sx)), ['F1'])

	def test_instance_only_object(self):
		sx = SimpleNamespace(dcm0=SimpleNamespace(tags={KEYWORD: [FOREIGN]}))
		self.assertEqual(values(dcm_series_description_codes(sx)), ['F1'])

	def test_series_only_object(self):
		self.assertEqual(values(dcm_series_description_codes(SimpleNamespace(dicomdata={KEYWORD: [FOREIGN]}))), ['F1'])

	def test_absent_everywhere(self):
		sx = SimpleNamespace(dicomdata={}, dcm0=SimpleNamespace(tags=None))
		self.assertEqual(dcm_series_description_codes(sx), [])
		self.assertEqual(dcm_series_tags(SimpleNamespace(), KEYWORD), {})
		self.assertEqual(dcm_series_description_codes(None), [])

	def test_instance_errors_propagate(self):
		class Series:
			dicomdata = {}
			@property
			def dcm0(self):
				raise ConnectionError('server unavailable')

		with self.assertRaises(ConnectionError):
			dcm_series_description_codes(Series())

	def test_dataset_and_tag_dictionary(self):
		self.assertEqual(values(dcm_series_description_codes(coded_dataset(code_item()))), ['F1'])
		self.assertEqual(values(dcm_series_description_codes({KEYWORD: [FOREIGN]})), ['F1'])
		self.assertEqual(dcm_series_description_codes(Dataset()), [])
		self.assertEqual(dcm_series_description_codes({}), [])

	def test_series_model_property(self):
		sx = SimpleNamespace(dicomdata={KEYWORD: [FOREIGN]})
		self.assertEqual(values(ImagingSeries.description_codes.fget(sx)), ['F1'])


class SeriesCodesWriterTestCase(unittest.TestCase):

	def test_adds_to_an_empty_dataset(self):
		ds = coded_dataset()
		sequence = dcm_add_series_description_codes(ds, [CODE_A])
		self.assertIs(getattr(ds, KEYWORD), sequence)
		self.assertEqual(len(sequence), 1)
		self.assertEqual(sequence[0].CodeValue, 'series.a')
		self.assertEqual(sequence[0].CodingSchemeDesignator, 'TEST-SCHEME')
		self.assertEqual(sequence[0].CodingSchemeVersion, '1.0')
		self.assertEqual(sequence[0].CodeMeaning, 'Series / A')

	def test_existing_item_stays_first(self):
		'''	The item already present keeps the first position '''
		ds = coded_dataset(code_item())
		dcm_add_series_description_codes(ds, [CODE_A])
		self.assertEqual([item.CodeValue for item in getattr(ds, KEYWORD)], ['F1', 'series.a'])

	def test_idempotent(self):
		ds = coded_dataset(code_item())
		dcm_add_series_description_codes(ds, [CODE_A])
		first = copy.deepcopy(getattr(ds, KEYWORD))
		dcm_add_series_description_codes(ds, [CODE_A])
		self.assertEqual(getattr(ds, KEYWORD), first)

	def test_repeated_code_in_one_call(self):
		ds = coded_dataset()
		self.assertEqual(len(dcm_add_series_description_codes(ds, [CODE_A, CODE_A, CODE_B])), 2)

	def test_same_code_other_version_is_not_added(self):
		ds = coded_dataset(code_item('series.a', 'TEST-SCHEME', 'Old meaning', '0.9'))
		sequence = dcm_add_series_description_codes(ds, [CODE_A])
		self.assertEqual(len(sequence), 1)
		self.assertEqual(sequence[0].CodingSchemeVersion, '0.9')

	def test_existing_sequence_is_not_mutated(self):
		original = Sequence([code_item()])
		ds = coded_dataset()
		setattr(ds, KEYWORD, original)
		held = getattr(ds, KEYWORD)
		sequence = dcm_add_series_description_codes(ds, [CODE_A])
		self.assertIsNot(sequence, held)
		self.assertEqual(len(held), 1)
		sequence[0].CodeMeaning = 'Changed'
		self.assertEqual(held[0].CodeMeaning, 'Vendor code')

	def test_foreign_item_is_kept_whole(self):
		'''	An item the readers skip (no scheme designator) is not lost by the writer '''
		item = Dataset()
		item.CodeValue = 'odd'
		ds = coded_dataset(item)
		sequence = dcm_add_series_description_codes(ds, [CODE_A])
		self.assertEqual([i.CodeValue for i in sequence], ['odd', 'series.a'])

	def test_accepts_a_generator_and_a_single_code(self):
		ds = coded_dataset()
		dcm_add_series_description_codes(ds, (code for code in [CODE_A]))
		dcm_add_series_description_codes(ds, CODE_B)
		self.assertEqual([item.CodeValue for item in getattr(ds, KEYWORD)], ['series.a', 'series.b'])

	def test_no_codes_assign_nothing(self):
		ds = coded_dataset()
		self.assertEqual(len(dcm_add_series_description_codes(ds, [])), 0)
		self.assertNotIn(KEYWORD, ds)

	def test_merge_leaves_its_input_alone(self):
		existing = Sequence([code_item()])
		merged = dcm_merge_series_description_codes(existing, [CODE_A])
		self.assertEqual([item.CodeValue for item in merged], ['F1', 'series.a'])
		self.assertEqual(len(existing), 1)
		self.assertIsNot(merged[0], existing[0])

	def test_merge_without_existing_items(self):
		self.assertEqual([item.CodeValue for item in dcm_merge_series_description_codes(None, [CODE_A])], ['series.a'])
		self.assertEqual(len(dcm_merge_series_description_codes(None, None)), 0)

	def test_round_trip(self):
		ds = coded_dataset(code_item())
		dcm_add_series_description_codes(ds, [CODE_A, CODE_B])
		codes = dcm_series_description_codes(round_trip(ds))
		self.assertEqual(values(codes), ['F1', 'series.a', 'series.b'])
		self.assertEqual(codes[1].scheme_version, '1.0')
		self.assertTrue(dcm_code_in(codes, CODE_B))


class HeaderModifyTestCase(unittest.TestCase):

	def merged(self, ds, *codes):
		'''	Merged sequence of a dataset, computed without touching it '''
		return dcm_merge_series_description_codes(ds.get(KEYWORD), codes)

	def test_replaced_sequence_is_recorded(self):
		ds = coded_dataset(code_item())
		dcmheader_modify(ds, {KEYWORD: self.merged(ds, CODE_A)}, modifying_system='Test/System')

		self.assertEqual(len(getattr(ds, KEYWORD)), 2)
		self.assertEqual(len(ds.OriginalAttributesSequence), 1)
		record = ds.OriginalAttributesSequence[0]
		self.assertEqual(record.ModifyingSystem, 'Test/System')
		previous = getattr(record.ModifiedAttributesSequence[0], KEYWORD)
		self.assertEqual([item.CodeValue for item in previous], ['F1'])

	def test_previous_value_does_not_alias_the_live_one(self):
		ds = coded_dataset(code_item())
		live = getattr(ds, KEYWORD)
		dcmheader_modify(ds, {KEYWORD: self.merged(ds, CODE_A)})
		live[0].CodeMeaning = 'Changed'
		previous = getattr(ds.OriginalAttributesSequence[0].ModifiedAttributesSequence[0], KEYWORD)
		self.assertEqual(previous[0].CodeMeaning, 'Vendor code')

	def test_added_attribute_records_nothing(self):
		ds = coded_dataset()
		dcmheader_modify(ds, {KEYWORD: self.merged(ds, CODE_A)})
		self.assertEqual(len(getattr(ds, KEYWORD)), 1)
		self.assertNotIn('OriginalAttributesSequence', ds)

	def test_unchanged_sequence_records_nothing(self):
		ds = coded_dataset(code_item())
		dcmheader_modify(ds, {KEYWORD: self.merged(ds, CODE_A)})
		dcmheader_modify(ds, {KEYWORD: self.merged(ds, CODE_A)})
		self.assertEqual(len(getattr(ds, KEYWORD)), 2)
		self.assertEqual(len(ds.OriginalAttributesSequence), 1)

	def test_default_modifying_system(self):
		ds = coded_dataset()
		ds.PatientName = 'A^B'
		dcmheader_modify(ds, {'PatientName': 'C^D'})
		self.assertEqual(ds.OriginalAttributesSequence[0].ModifyingSystem, SONADOR_CLIENT)
		self.assertEqual(ds.OriginalAttributesSequence[0].ModifiedAttributesSequence[0].PatientName, 'A^B')

	def test_without_the_record(self):
		ds = coded_dataset()
		ds.PatientName = 'A^B'
		dcmheader_modify(ds, {'PatientName': 'C^D'}, modified_attributes_sequence=False)
		self.assertEqual(ds.PatientName, 'C^D')
		self.assertNotIn('OriginalAttributesSequence', ds)

	def test_items_accumulate_after_existing_records(self):
		ds = coded_dataset()
		ds.PatientName = 'A^B'
		ts = datetime.datetime(2026, 1, 2, 3, 4, 5)
		dcmheader_modify(ds, {'PatientName': 'C^D'}, mod_ts=ts)
		dcmheader_modify(ds, {'PatientName': 'E^F'}, mod_ts=ts, modifying_system='Test/System')
		self.assertEqual([item.ModifyingSystem for item in ds.OriginalAttributesSequence], [SONADOR_CLIENT, 'Test/System'])

	def test_round_trip(self):
		ds = coded_dataset(code_item())
		dcmheader_modify(ds, {KEYWORD: self.merged(ds, CODE_A)}, modifying_system='Test/System')
		ds = round_trip(ds)
		previous = getattr(ds.OriginalAttributesSequence[0].ModifiedAttributesSequence[0], KEYWORD)
		self.assertEqual(previous[0].CodeValue, 'F1')
		self.assertEqual(values(dcm_series_description_codes(ds)), ['F1', 'series.a'])
