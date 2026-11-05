'''	Offline tests of the Referenced Series Sequence writers (`sonador.imaging.helpers.dcmref`) and of
	the legacy helpers that delegate to them (`sonador.helpers.dcm_encode_series_ref`,
	`dcm_encode_instance_ref`).

	Run from the directory containing the `sonador` package:
		python -m unittest sonador.ftests.test_dicom.dicom_references
'''
import unittest
from io import BytesIO
from types import SimpleNamespace

from pydicom import dcmread, dcmwrite
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.sequence import Sequence
from pydicom.uid import ExplicitVRLittleEndian

from ...apisettings import DicomMetaKey, IMAGING_SERVER_RESOURCE_SERIES, IMAGING_SERVER_RESOURCE_STUDY, \
	DCMHEADER_SERIES_INSTANCE_UID, DCMHEADER_STUDY_INSTANCE_UID, DCMHEADER_SR_REF_SERIES_SEQ, DCMHEADER_SR_REF_INSTANCE_SEQ, \
	DCMHEADER_SOP_CLASS_UID, DCMHEADER_SOP_INSTANCE_UID
from ...helpers import dcm_encode_series_ref, dcm_encode_instance_ref
from ...imaging.helpers.headers import dcm_referenced_series_uids
from ...imaging.helpers.dcmref import dcm_reference_series_uid, dcm_reference_instances, dcm_encode_instance_reference, \
	dcm_encode_series_reference, dcm_reference_series_list, dcm_encode_reference_series_seq, DCM_REFERENCE_INSTANCES_DEFAULT


SOP_CT = '1.2.840.10008.5.1.4.1.1.2'
SOP_SEG = '1.2.840.10008.5.1.4.1.1.66.4'
UID_A, UID_B, UID_C = '1.2.3.4.1', '1.2.3.4.2', '1.2.3.4.3'


def fake_instance(sop_instance_uid, sop_class_uid=SOP_CT):
	return SimpleNamespace(sop_class_uid=sop_class_uid, sop_instance_uid=sop_instance_uid)


def fake_series(series_uid, instances=(), attr='instances_collection'):
	'''	Series-like object: a `series_uid` and an instance collection under `instances_collection`
		(the SR, SEG and encapsulated-document series) or `slices_collection` (the imaging series)
	'''
	return SimpleNamespace(series_uid=series_uid, **{attr: list(instances)})


def dcm_roundtrip(items):
	'''	Serialize a dataset carrying the items as its Referenced Series Sequence and read it back, so
		that assertions run against the written form rather than the in-memory objects
	'''
	ds = Dataset()
	ds.file_meta = FileMetaDataset()
	ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
	ds.file_meta.MediaStorageSOPClassUID = SOP_SEG
	ds.file_meta.MediaStorageSOPInstanceUID = '1.7.1'
	ds.SOPClassUID, ds.SOPInstanceUID = SOP_SEG, '1.7.1'
	setattr(ds, DCMHEADER_SR_REF_SERIES_SEQ, Sequence(list(items)))

	buffer = BytesIO()
	dcmwrite(buffer, ds, enforce_file_format=True)
	buffer.seek(0)
	return dcmread(buffer)


class ReferenceUidTestCase(unittest.TestCase):
	'''	`dcm_reference_series_uid`: the accepted reference types
	'''

	def test_series_resource(self):
		self.assertEqual(dcm_reference_series_uid(fake_series(UID_A)), UID_A)
		self.assertEqual(dcm_reference_series_uid(SimpleNamespace(series_uid=' %s ' % UID_A)), UID_A)

	def test_series_metakey(self):
		key = DicomMetaKey(IMAGING_SERVER_RESOURCE_SERIES, DCMHEADER_SERIES_INSTANCE_UID, UID_A)
		self.assertEqual(dcm_reference_series_uid(key), UID_A)

	def test_uid_string(self):
		self.assertEqual(dcm_reference_series_uid(' %s\n' % UID_A), UID_A)

	def test_invalid_references(self):
		for reference in ('', '  ', None, object(), SimpleNamespace(series_uid=None), SimpleNamespace(series_uid=''),
				DicomMetaKey(IMAGING_SERVER_RESOURCE_STUDY, DCMHEADER_STUDY_INSTANCE_UID, UID_A),
				DicomMetaKey(IMAGING_SERVER_RESOURCE_SERIES, DCMHEADER_SERIES_INSTANCE_UID, None)):
			with self.subTest(reference=reference):
				with self.assertRaises(ValueError):
					dcm_reference_series_uid(reference)


class InstanceReferenceTestCase(unittest.TestCase):
	'''	`dcm_encode_instance_reference` and the legacy `dcm_encode_instance_ref`
	'''

	def test_instance_resource(self):
		item = dcm_encode_instance_reference(fake_instance('1.9.1'))
		self.assertEqual((item.ReferencedSOPClassUID, item.ReferencedSOPInstanceUID), (SOP_CT, '1.9.1'))
		self.assertEqual(len(item), 2)

	def test_tags_and_dataset(self):
		tags = {DCMHEADER_SOP_CLASS_UID: SOP_SEG, DCMHEADER_SOP_INSTANCE_UID: '1.9.2'}
		for source in (tags, self._dataset(tags)):
			with self.subTest(source=type(source).__name__):
				item = dcm_encode_instance_reference(source)
				self.assertEqual((item.ReferencedSOPClassUID, item.ReferencedSOPInstanceUID), (SOP_SEG, '1.9.2'))

	def test_existing_dataset_is_extended(self):
		ds = Dataset()
		ds.InstanceNumber = 3
		self.assertIs(dcm_encode_instance_reference(fake_instance('1.9.3'), dcm_mdata=ds), ds)
		self.assertEqual((ds.InstanceNumber, ds.ReferencedSOPInstanceUID), (3, '1.9.3'))

	def test_missing_identifiers(self):
		for instance in (fake_instance(None), fake_instance('1.9.4', sop_class_uid=''), {}, object()):
			with self.subTest(instance=instance):
				with self.assertRaises(ValueError):
					dcm_encode_instance_reference(instance)

	def test_blank_identifiers_are_rejected_for_resources_and_tags(self):
		'''	A whitespace-only UID is rejected whether it comes from a resource attribute or a tag
		'''
		blanks = [fake_instance('  ', sop_class_uid=SOP_CT), fake_instance('1.9.4', sop_class_uid='\t'),
			fake_instance(' ', sop_class_uid='  '),
			{DCMHEADER_SOP_CLASS_UID: SOP_CT, DCMHEADER_SOP_INSTANCE_UID: '  '},
			{DCMHEADER_SOP_CLASS_UID: '\t', DCMHEADER_SOP_INSTANCE_UID: '1.9.4'},
			self._dataset({DCMHEADER_SOP_CLASS_UID: SOP_CT, DCMHEADER_SOP_INSTANCE_UID: ' '})]
		for instance in blanks:
			with self.subTest(instance=instance):
				with self.assertRaises(ValueError):
					dcm_encode_instance_reference(instance)

		# Surrounding whitespace around a real UID is removed, for both sources
		for instance in (fake_instance(' 1.9.4 ', sop_class_uid=' %s\n' % SOP_CT),
				{DCMHEADER_SOP_CLASS_UID: ' %s ' % SOP_CT, DCMHEADER_SOP_INSTANCE_UID: '\t1.9.4'}):
			with self.subTest(instance=instance):
				item = dcm_encode_instance_reference(instance)
				self.assertEqual((item.ReferencedSOPClassUID, item.ReferencedSOPInstanceUID), (SOP_CT, '1.9.4'))

	def test_legacy_helper_delegates(self):
		item = dcm_encode_instance_ref(fake_instance('1.9.5'))
		self.assertEqual((item.ReferencedSOPClassUID, item.ReferencedSOPInstanceUID), (SOP_CT, '1.9.5'))
		with self.assertRaises(ValueError):
			dcm_encode_instance_ref(object())

	@staticmethod
	def _dataset(tags):
		ds = Dataset()
		for key, value in tags.items():
			setattr(ds, key, value)
		return ds


class SeriesReferenceTestCase(unittest.TestCase):
	'''	`dcm_encode_series_reference`: the two forms of an item
	'''

	def setUp(self):
		self.instances = [fake_instance('1.9.%d' % i) for i in range(1, 4)]
		self.series = fake_series(UID_A, self.instances)

	def test_default_form_lists_the_instances(self):
		self.assertTrue(DCM_REFERENCE_INSTANCES_DEFAULT)
		item = dcm_encode_series_reference(self.series)
		self.assertEqual(item.SeriesInstanceUID, UID_A)
		self.assertEqual([ref.ReferencedSOPInstanceUID for ref in item.ReferencedInstanceSequence],
			[dcm.sop_instance_uid for dcm in self.instances])
		self.assertTrue(all(ref.ReferencedSOPClassUID == SOP_CT for ref in item.ReferencedInstanceSequence))

	def test_series_only_form(self):
		item = dcm_encode_series_reference(self.series, reference_instances=False)
		self.assertEqual(item.SeriesInstanceUID, UID_A)
		self.assertNotIn(DCMHEADER_SR_REF_INSTANCE_SEQ, item)
		self.assertEqual(len(item), 1)

	def test_series_only_form_removes_a_stale_instance_sequence(self):
		'''	Reusing an item for another series in the series-only form must not keep the instances of
			the previous series; other elements of the supplied dataset are kept
		'''
		item = dcm_encode_series_reference(self.series)
		item.SeriesDescription = 'kept'
		self.assertEqual(len(item.ReferencedInstanceSequence), 3)

		self.assertIs(dcm_encode_series_reference(UID_B, reference_instances=False, dcm_mdata=item), item)
		self.assertEqual(item.SeriesInstanceUID, UID_B)
		self.assertNotIn(DCMHEADER_SR_REF_INSTANCE_SEQ, item)
		self.assertEqual(item.SeriesDescription, 'kept')

		written = dcm_roundtrip([item])[DCMHEADER_SR_REF_SERIES_SEQ][0]
		self.assertEqual(written.SeriesInstanceUID, UID_B)
		self.assertNotIn(DCMHEADER_SR_REF_INSTANCE_SEQ, written)
		self.assertEqual(written.SeriesDescription, 'kept')

	def test_instance_form_replaces_a_previous_instance_sequence(self):
		'''	The instance form rewrites the list from the new reference rather than appending to it
		'''
		item = dcm_encode_series_reference(self.series)
		dcm_encode_series_reference(fake_series(UID_B, self.instances[:1]), dcm_mdata=item)
		written = dcm_roundtrip([item])[DCMHEADER_SR_REF_SERIES_SEQ][0]
		self.assertEqual(written.SeriesInstanceUID, UID_B)
		self.assertEqual([ref.ReferencedSOPInstanceUID for ref in written.ReferencedInstanceSequence], ['1.9.1'])

	def test_explicit_instances_select_a_subset_of_a_resource(self):
		item = dcm_encode_series_reference(self.series, instances=self.instances[1:])
		self.assertEqual([ref.ReferencedSOPInstanceUID for ref in item.ReferencedInstanceSequence], ['1.9.2', '1.9.3'])

	def test_series_only_from_key_and_uid(self):
		key = DicomMetaKey(IMAGING_SERVER_RESOURCE_SERIES, DCMHEADER_SERIES_INSTANCE_UID, UID_B)
		for reference in (key, UID_B):
			with self.subTest(reference=reference):
				item = dcm_encode_series_reference(reference, reference_instances=False)
				self.assertEqual((item.SeriesInstanceUID, len(item)), (UID_B, 1))

	def test_instances_require_a_resource_or_an_explicit_list(self):
		for reference in (UID_B, DicomMetaKey(IMAGING_SERVER_RESOURCE_SERIES, DCMHEADER_SERIES_INSTANCE_UID, UID_B),
				SimpleNamespace(series_uid=UID_B)):
			with self.subTest(reference=reference):
				with self.assertRaises(ValueError):
					dcm_encode_series_reference(reference, reference_instances=True)

		# An explicit instance list makes the instance form available for a bare UID
		item = dcm_encode_series_reference(UID_B, reference_instances=True, instances=self.instances[:1])
		self.assertEqual([ref.ReferencedSOPInstanceUID for ref in item.ReferencedInstanceSequence], ['1.9.1'])

	def test_slices_collection_is_read_for_imaging_series(self):
		series = fake_series(UID_A, self.instances, attr='slices_collection')
		item = dcm_encode_series_reference(series)
		self.assertEqual(len(item.ReferencedInstanceSequence), 3)
		self.assertEqual(dcm_reference_instances(series), self.instances)

	def test_derived_series_without_a_workaround(self):
		'''	A SEG or SR series (instances under `instances_collection`, no `slices_collection`) is
			referenced directly
		'''
		seg = fake_series(UID_C, [fake_instance('1.8.1', sop_class_uid=SOP_SEG)])
		item = dcm_encode_series_reference(seg)
		self.assertEqual(item.SeriesInstanceUID, UID_C)
		self.assertEqual(item.ReferencedInstanceSequence[0].ReferencedSOPClassUID, SOP_SEG)

	def test_empty_series_lists_no_instances(self):
		item = dcm_encode_series_reference(fake_series(UID_A, []))
		self.assertEqual(len(item.ReferencedInstanceSequence), 0)

	def test_legacy_helper_delegates(self):
		'''	`dcm_encode_series_ref` writes the same item as before: series UID and every instance
		'''
		item = dcm_encode_series_ref(fake_series(UID_A, self.instances, attr='slices_collection'))
		expected = dcm_encode_series_reference(self.series, reference_instances=True)
		self.assertEqual(item.to_json_dict(), expected.to_json_dict())
		for invalid in (UID_A, DicomMetaKey(IMAGING_SERVER_RESOURCE_SERIES, DCMHEADER_SERIES_INSTANCE_UID, UID_A), object()):
			with self.subTest(invalid=invalid):
				with self.assertRaises(ValueError):
					dcm_encode_series_ref(invalid)


class ReferenceListTestCase(unittest.TestCase):
	'''	`dcm_reference_series_list` and `dcm_encode_reference_series_seq`
	'''

	def test_linked_series_first_each_once(self):
		linked = fake_series(UID_A)
		key_a = DicomMetaKey(IMAGING_SERVER_RESOURCE_SERIES, DCMHEADER_SERIES_INSTANCE_UID, UID_A)
		references = dcm_reference_series_list(linked, [UID_B, None, key_a, fake_series(UID_B), UID_C])
		self.assertEqual([dcm_reference_series_uid(ref) for ref in references], [UID_A, UID_B, UID_C])
		self.assertIs(references[0], linked)

	def test_without_a_linked_series(self):
		references = dcm_reference_series_list(None, [UID_B, UID_B])
		self.assertEqual(references, [UID_B])
		self.assertEqual(dcm_reference_series_list(None), [])

	def test_sequence_matches_the_reader(self):
		series = [fake_series(UID_A, [fake_instance('1.9.1')]), fake_series(UID_B, [fake_instance('1.9.2')])]
		seq = dcm_encode_reference_series_seq(series, reference_instances=False)
		self.assertIsInstance(seq, Sequence)
		self.assertEqual([len(item) for item in seq], [1, 1])

		ds = Dataset()
		setattr(ds, DCMHEADER_SR_REF_SERIES_SEQ, seq)
		self.assertEqual(dcm_referenced_series_uids(ds), [UID_A, UID_B])

		seq = dcm_encode_reference_series_seq(series)
		self.assertEqual([len(item.ReferencedInstanceSequence) for item in seq], [1, 1])

	def test_sequence_refuses_an_invalid_reference(self):
		with self.assertRaises(ValueError):
			dcm_encode_reference_series_seq([UID_A, ''], reference_instances=False)
