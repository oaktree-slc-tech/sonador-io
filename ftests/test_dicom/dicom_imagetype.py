'''	Offline tests of the Image Type readers (`sonador.imaging.helpers.imagetype`): the values of the
	attribute in its string, list and pydicom forms, the derived test and its fallback to the
	derivation attributes, the series classification from series or instance tags with its retry and
	unknown outcome, the list helpers with the `unknown` options, and the link guard.

	Run from the directory containing the `sonador` package:
		python -m unittest sonador.ftests.test_dicom.dicom_imagetype
'''
import logging
import unittest
from io import BytesIO
from types import SimpleNamespace

from pydicom import dcmread, dcmwrite
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian

from ...apisettings import DicomMetaKey, IMAGING_SERVER_RESOURCE_SERIES, DCMHEADER_SERIES_INSTANCE_UID, \
	DCMHEADER_IMAGE_TYPE, DCMHEADER_DERIVATION_DESCRIPTION, DCMHEADER_SR_SOURCE_IMAGE_SEQ, \
	DCM_IMAGE_TYPE_ORIGINAL, DCM_IMAGE_TYPE_DERIVED, DCM_IMAGE_TYPE_MIXED, DCM_IMAGE_TYPE_PRIMARY, DCM_IMAGE_TYPE_SECONDARY, \
	DCM_MULTIVALUE_SEPARATOR, SERIES_UNKNOWN_ERROR, SERIES_UNKNOWN_SKIP, SERIES_UNKNOWN_KEEP, SERIES_UNKNOWN_OPTIONS
from ...imaging.helpers.imagetype import dcm_image_type, dcm_tags_are_derived, dcm_series_imagetype_tags, \
	dcm_series_image_type, dcm_series_is_derived, dcm_exclude_derived_series, dcm_primary_series_list, \
	dcm_check_primary_series


__all__ = ['ImageTypeValuesTestCase', 'DerivedTagsTestCase', 'SeriesClassificationTestCase', 'SeriesListTestCase',
	'PrimaryLinkGuardTestCase', 'ReviewRegressionTestCase', 'ReviewRegression2TestCase']

logging.getLogger('sonador.imaging.helpers.imagetype').setLevel(logging.CRITICAL)

ORIGINAL_PRIMARY = DCM_MULTIVALUE_SEPARATOR.join((DCM_IMAGE_TYPE_ORIGINAL, DCM_IMAGE_TYPE_PRIMARY, 'AXIAL'))
RESAMPLED = DCM_MULTIVALUE_SEPARATOR.join((DCM_IMAGE_TYPE_DERIVED, DCM_IMAGE_TYPE_SECONDARY, 'AXIAL', 'RESAMPLED'))
SOP_CT = '1.2.840.10008.5.1.4.1.1.2'


class FakeInstance:
	'''	First instance of a fake series: `tags` raises `fail` times before returning the tag dictionary
	'''
	def __init__(self, tags, fail=0):
		self._tags, self._fail, self.reads = tags, fail, 0

	@property
	def tags(self):
		self.reads += 1
		if self.reads <= self._fail:
			raise IOError('tags unavailable')
		return self._tags


def fake_series(pk, modality='CT', image_type=ORIGINAL_PRIMARY, tags=None, dicomdata=None, fail=0, description=None):
	'''	Series-like object: series tags (`dicomdata`) and a first instance (`dcm0`) with instance tags
	'''
	instance_tags = dict(tags or {})
	if image_type is not None and DCMHEADER_IMAGE_TYPE not in instance_tags:
		instance_tags[DCMHEADER_IMAGE_TYPE] = image_type
	series_tags = {'Modality': modality, 'SeriesDescription': description or pk}
	series_tags.update(dicomdata or {})
	return SimpleNamespace(pk=pk, series_uid='1.2.%s' % pk, modality=modality, description=description or pk,
		dicomdata=series_tags, dcm0=FakeInstance(instance_tags, fail=fail))


def dcm_roundtrip(**attrs):
	'''	Dataset carrying the attributes, written and read back so that the readers see pydicom's own value types
	'''
	ds = Dataset()
	ds.file_meta = FileMetaDataset()
	ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
	ds.file_meta.MediaStorageSOPClassUID = SOP_CT
	ds.file_meta.MediaStorageSOPInstanceUID = '1.7.1'
	ds.SOPClassUID, ds.SOPInstanceUID = SOP_CT, '1.7.1'
	for keyword, value in attrs.items():
		setattr(ds, keyword, value)
	buffer = BytesIO()
	dcmwrite(buffer, ds, enforce_file_format=True)
	buffer.seek(0)
	return dcmread(buffer)


class ImageTypeValuesTestCase(unittest.TestCase):
	'''	`dcm_image_type`: the forms of the attribute
	'''
	def test_simplified_tags_string(self):
		self.assertEqual(dcm_image_type({DCMHEADER_IMAGE_TYPE: ORIGINAL_PRIMARY}), ['ORIGINAL', 'PRIMARY', 'AXIAL'])

	def test_list_and_case(self):
		self.assertEqual(dcm_image_type({DCMHEADER_IMAGE_TYPE: ['derived', ' Secondary ']}), ['DERIVED', 'SECONDARY'])

	def test_absent_or_empty(self):
		self.assertEqual(dcm_image_type({}), [])
		self.assertEqual(dcm_image_type({DCMHEADER_IMAGE_TYPE: ''}), [])
		self.assertEqual(dcm_image_type({DCMHEADER_IMAGE_TYPE: None}), [])
		self.assertEqual(dcm_image_type(None), [])

	def test_pydicom_dataset(self):
		ds = dcm_roundtrip(ImageType=[DCM_IMAGE_TYPE_DERIVED, DCM_IMAGE_TYPE_SECONDARY, 'AXIAL', 'RESAMPLED'])
		self.assertEqual(dcm_image_type(ds), ['DERIVED', 'SECONDARY', 'AXIAL', 'RESAMPLED'])
		self.assertEqual(dcm_image_type(dcm_roundtrip(ImageType=DCM_IMAGE_TYPE_ORIGINAL)), ['ORIGINAL'])
		self.assertEqual(dcm_image_type(dcm_roundtrip()), [])


class DerivedTagsTestCase(unittest.TestCase):
	'''	`dcm_tags_are_derived`: Image Type first, derivation attributes when it is absent
	'''
	def test_image_type_values(self):
		self.assertFalse(dcm_tags_are_derived({DCMHEADER_IMAGE_TYPE: ORIGINAL_PRIMARY}))
		self.assertTrue(dcm_tags_are_derived({DCMHEADER_IMAGE_TYPE: RESAMPLED}))
		self.assertTrue(dcm_tags_are_derived({DCMHEADER_IMAGE_TYPE: 'DERIVED\\PRIMARY'}))
		self.assertTrue(dcm_tags_are_derived({DCMHEADER_IMAGE_TYPE: 'ORIGINAL\\SECONDARY'}))
		self.assertFalse(dcm_tags_are_derived({DCMHEADER_IMAGE_TYPE: 'ORIGINAL'}))

	def test_derivation_attributes_without_image_type(self):
		self.assertFalse(dcm_tags_are_derived({}))
		self.assertFalse(dcm_tags_are_derived(None))
		self.assertTrue(dcm_tags_are_derived({DCMHEADER_DERIVATION_DESCRIPTION: 'resampled'}))
		self.assertTrue(dcm_tags_are_derived({DCMHEADER_SR_SOURCE_IMAGE_SEQ: [{}]}))

	def test_explicit_image_type_wins(self):
		self.assertFalse(dcm_tags_are_derived({DCMHEADER_IMAGE_TYPE: ORIGINAL_PRIMARY, DCMHEADER_DERIVATION_DESCRIPTION: 'x'}))

	def test_pydicom_dataset(self):
		self.assertTrue(dcm_tags_are_derived(dcm_roundtrip(ImageType=[DCM_IMAGE_TYPE_DERIVED, DCM_IMAGE_TYPE_SECONDARY])))
		self.assertFalse(dcm_tags_are_derived(dcm_roundtrip(ImageType=[DCM_IMAGE_TYPE_ORIGINAL, DCM_IMAGE_TYPE_PRIMARY])))
		self.assertTrue(dcm_tags_are_derived(dcm_roundtrip(DerivationDescription='resampled')))


class SeriesClassificationTestCase(unittest.TestCase):
	'''	`dcm_series_imagetype_tags`, `dcm_series_image_type`, `dcm_series_is_derived`
	'''
	def test_series_tags_preferred(self):
		sx = fake_series('a', dicomdata={DCMHEADER_IMAGE_TYPE: RESAMPLED})
		self.assertIs(dcm_series_imagetype_tags(sx), sx.dicomdata)
		self.assertTrue(dcm_series_is_derived(sx))
		self.assertEqual(sx.dcm0.reads, 0)

	def test_instance_tags_fallback(self):
		sx = fake_series('b')
		self.assertEqual(dcm_series_image_type(sx), ['ORIGINAL', 'PRIMARY', 'AXIAL'])
		self.assertFalse(dcm_series_is_derived(sx))
		self.assertTrue(dcm_series_is_derived(fake_series('c', image_type=RESAMPLED)))
		self.assertTrue(dcm_series_is_derived(fake_series('d', image_type=None, tags={DCMHEADER_DERIVATION_DESCRIPTION: 'x'})))

	def test_unknown_after_retries(self):
		sx = fake_series('e', fail=9)
		self.assertIsNone(dcm_series_is_derived(sx))
		self.assertEqual(sx.dcm0.reads, 2)
		sx = fake_series('f', fail=9)
		self.assertIsNone(dcm_series_is_derived(sx, retries=0))
		self.assertEqual(sx.dcm0.reads, 1)
		with self.assertRaises(IOError):
			dcm_series_image_type(fake_series('g', fail=9))

	def test_transient_failure_recovers(self):
		sx = fake_series('h', fail=1)
		self.assertFalse(dcm_series_is_derived(sx))
		self.assertEqual(sx.dcm0.reads, 2)


class SeriesListTestCase(unittest.TestCase):
	'''	`dcm_exclude_derived_series`, `dcm_primary_series_list`
	'''
	def study(self):
		self.ct = fake_series('ct')
		self.ct2 = fake_series('ct2')
		self.copies = [fake_series('copy%d' % n, image_type=RESAMPLED) for n in (512, 256)]
		self.capture = fake_series('sc', modality='OT', image_type='DERIVED\\SECONDARY')
		self.models = SimpleNamespace(pk='m3d', modality='M3D', series_uid='1.2.m3d')  # no tags at all
		return [self.copies[0], self.ct, self.capture, self.copies[1], self.ct2]

	def test_exclude_every_modality_by_default(self):
		listing = self.study()
		self.assertEqual([sx.pk for sx in dcm_exclude_derived_series(listing)], ['ct', 'ct2'])

	def test_modality_scope(self):
		listing = self.study()
		self.assertEqual([sx.pk for sx in dcm_exclude_derived_series(listing, modalities=('CT',))], ['ct', 'sc', 'ct2'])
		self.assertEqual([sx.pk for sx in dcm_primary_series_list(listing, modalities=('CT',))], ['ct', 'ct2'])
		self.assertEqual([sx.pk for sx in dcm_primary_series_list(listing)], ['ct', 'ct2'])

	def test_constants(self):
		self.assertEqual(SERIES_UNKNOWN_OPTIONS, (SERIES_UNKNOWN_ERROR, SERIES_UNKNOWN_SKIP, SERIES_UNKNOWN_KEEP))

	def test_unknown_options(self):
		bad = fake_series('bad', fail=9)
		good = fake_series('good')
		with self.assertRaisesRegex(ValueError, 'job 7: Unable to tell whether CT series bad'):
			dcm_primary_series_list([bad, good], context='job 7')
		bad.dcm0.reads = 0
		self.assertEqual([sx.pk for sx in dcm_primary_series_list([bad, good], unknown=SERIES_UNKNOWN_SKIP)], ['good'])
		bad.dcm0.reads = 0
		self.assertEqual([sx.pk for sx in dcm_primary_series_list([bad, good], unknown=SERIES_UNKNOWN_KEEP)], ['bad', 'good'])
		with self.assertRaises(ValueError):
			dcm_primary_series_list([good], unknown='bogus')

	def test_none_entries_and_empty_input(self):
		self.assertEqual(dcm_exclude_derived_series(None), [])
		self.assertEqual([sx.pk for sx in dcm_exclude_derived_series([None, fake_series('ct')])], ['ct'])


class PrimaryLinkGuardTestCase(unittest.TestCase):
	'''	`dcm_check_primary_series`
	'''
	def test_primary_link_passes(self):
		ct, ct2 = fake_series('ct'), fake_series('ct2')
		models = SimpleNamespace(series_uid='1.2.4', modality='M3D')  # a model series: no imaging tags at all
		dcm_check_primary_series(ct, [ct, ct2, '1.2.3', models], modalities=('CT',))
		dcm_check_primary_series(None, None)
		# Without a modality scope every series resource is checked, and one without any tags is rejected
		with self.assertRaisesRegex(ValueError, 'could not be read'):
			dcm_check_primary_series(ct, [models])

	def test_derived_linked_series_rejected(self):
		with self.assertRaisesRegex(ValueError, 'case 1: The linked CT series copy is derived imaging \\(ImageType=DERIVED'):
			dcm_check_primary_series(fake_series('copy', image_type=RESAMPLED), context='case 1')

	def test_derived_reference_rejected(self):
		with self.assertRaisesRegex(ValueError, 'The referenced CT series copy is derived imaging'):
			dcm_check_primary_series(fake_series('ct'), [fake_series('ct2'), fake_series('copy', image_type=RESAMPLED)])

	def test_unknown_series_rejected(self):
		with self.assertRaisesRegex(ValueError, 'could not be read'):
			dcm_check_primary_series(fake_series('ct'), [fake_series('bad', fail=9)])

	def test_modality_scope(self):
		capture = fake_series('sc', modality='OT', image_type='DERIVED\\SECONDARY')
		dcm_check_primary_series(fake_series('ct'), [capture], modalities=('CT',))
		with self.assertRaises(ValueError):
			dcm_check_primary_series(fake_series('ct'), [capture])


class ReviewRegressionTestCase(unittest.TestCase):
	'''	Regressions from the review of the first revision: positional Image Type values, one-use modality
		scopes, and the unknown/skip path
	'''
	def test_empty_components_keep_their_position(self):
		expected = ['ORIGINAL', 'PRIMARY', '', 'CUSTOM']
		self.assertEqual(dcm_image_type({DCMHEADER_IMAGE_TYPE: 'ORIGINAL\\PRIMARY\\\\CUSTOM'}), expected)
		self.assertEqual(dcm_image_type({DCMHEADER_IMAGE_TYPE: ['ORIGINAL', 'PRIMARY', '', 'CUSTOM']}), expected)
		self.assertEqual(dcm_image_type({DCMHEADER_IMAGE_TYPE: ['ORIGINAL', 'PRIMARY', None, 'CUSTOM']}), expected)
		self.assertEqual(dcm_image_type(dcm_roundtrip(ImageType=['ORIGINAL', 'PRIMARY', '', 'CUSTOM'])), expected)
		self.assertEqual(dcm_image_type({DCMHEADER_IMAGE_TYPE: ['', '']}), [])
		self.assertEqual(dcm_image_type({DCMHEADER_IMAGE_TYPE: '\\'}), [])
		# A leading empty value is kept too: value 2 stays at index 1
		self.assertTrue(dcm_tags_are_derived({DCMHEADER_IMAGE_TYPE: '\\SECONDARY'}))

	def test_one_use_modality_scope(self):
		primary, copy = fake_series('ct'), fake_series('copy', image_type=RESAMPLED)
		for scope in (('CT',), ['CT'], iter(('CT',)), (m for m in ('CT',))):
			with self.subTest(scope=type(scope).__name__):
				with self.assertRaises(ValueError):
					dcm_check_primary_series(primary, [copy], modalities=scope)
		for make_scope in (lambda: ('CT',), lambda: iter(('CT',))):
			self.assertEqual([sx.pk for sx in dcm_primary_series_list([copy, primary], modalities=make_scope())], ['ct'])
			self.assertEqual([sx.pk for sx in dcm_exclude_derived_series([copy, primary, fake_series('ot', modality='OT')],
				modalities=make_scope())], ['ct', 'ot'])
		with self.assertRaises(ValueError):
			dcm_check_primary_series(primary, [fake_series('bad', fail=9)], modalities=iter(('CT',)))

	def test_skipped_unknown_series_is_not_reread_or_called_derived(self):
		# Fails twice, then would read ORIGINAL/PRIMARY: with one retry the series stays unknown
		sx = fake_series('flaky', fail=2)
		with self.assertLogs('sonador.imaging.helpers.imagetype', level='WARNING') as logs:
			self.assertEqual(dcm_exclude_derived_series([sx], unknown=SERIES_UNKNOWN_SKIP), [])
		self.assertEqual(sx.dcm0.reads, 2)
		self.assertTrue(any('cannot be classified' in line and 'left out' in line for line in logs.output))
		self.assertFalse(any('Derived' in line for line in logs.output))

		sx = fake_series('flaky2', fail=2)
		with self.assertLogs('sonador.imaging.helpers.imagetype', level='WARNING') as logs:
			self.assertEqual([s.pk for s in dcm_exclude_derived_series([sx], unknown=SERIES_UNKNOWN_KEEP)], ['flaky2'])
		self.assertEqual(sx.dcm0.reads, 2)
		self.assertTrue(any('kept unclassified' in line for line in logs.output))

	def test_derived_message_uses_the_tags_already_read(self):
		sx = fake_series('copy', image_type=RESAMPLED)
		with self.assertLogs('sonador.imaging.helpers.imagetype', level='INFO') as logs:
			dcm_exclude_derived_series([sx])
		self.assertEqual(sx.dcm0.reads, 1)
		self.assertTrue(any('ImageType=%s' % RESAMPLED in line for line in logs.output))
		sx = fake_series('copy2', image_type=RESAMPLED)
		with self.assertRaisesRegex(ValueError, 'ImageType=DERIVED'):
			dcm_check_primary_series(sx)
		self.assertEqual(sx.dcm0.reads, 1)


class ReviewRegression2TestCase(unittest.TestCase):
	'''	Regressions from the reassessment of the second revision: series-like resources without series
		tags, and Enhanced images with MIXED frames
	'''
	@staticmethod
	def lightweight(pk, image_type=ORIGINAL_PRIMARY, fail=0):
		'''	Series-like object with instance tags only: no `dicomdata` attribute at all
		'''
		tags = {DCMHEADER_IMAGE_TYPE: image_type} if image_type else {}
		return SimpleNamespace(pk=pk, series_uid='1.2.%s' % pk, modality='CT', dcm0=FakeInstance(tags, fail=fail))

	def test_resources_without_series_tags_are_classified(self):
		derived = self.lightweight('copy', image_type=RESAMPLED)
		self.assertTrue(dcm_series_is_derived(derived))
		self.assertEqual(dcm_primary_series_list([derived]), [])
		for linked, refs in ((derived, None), (fake_series('ct'), [derived])):
			with self.assertRaisesRegex(ValueError, 'copy is derived imaging'):
				dcm_check_primary_series(linked, refs)
		unreadable = self.lightweight('bad', fail=9)
		for linked, refs in ((unreadable, None), (fake_series('ct'), [unreadable])):
			with self.assertRaisesRegex(ValueError, 'could not be read'):
				dcm_check_primary_series(linked, refs)
		# The same shape with original tags passes in both positions
		dcm_check_primary_series(self.lightweight('ct2'), [self.lightweight('ct3')])

	def test_object_without_any_tags_is_rejected_not_skipped(self):
		bare = SimpleNamespace(pk='bare', series_uid='1.2.bare', modality='CT')
		with self.assertRaisesRegex(ValueError, 'could not be read'):
			dcm_check_primary_series(fake_series('ct'), [bare])
		self.assertEqual(dcm_primary_series_list([bare], unknown=SERIES_UNKNOWN_SKIP), [])

	def test_uids_and_keys_remain_exempt(self):
		key = DicomMetaKey(IMAGING_SERVER_RESOURCE_SERIES, DCMHEADER_SERIES_INSTANCE_UID, '1.2.3')
		dcm_check_primary_series(fake_series('ct'), ['1.2.4', key])

	def test_mixed_frames_are_not_primary(self):
		mixed = DCM_MULTIVALUE_SEPARATOR.join((DCM_IMAGE_TYPE_MIXED, DCM_IMAGE_TYPE_PRIMARY, 'VOLUME', 'MIXED'))
		self.assertTrue(dcm_tags_are_derived({DCMHEADER_IMAGE_TYPE: mixed}))
		self.assertTrue(dcm_tags_are_derived(dcm_roundtrip(ImageType=[DCM_IMAGE_TYPE_MIXED, DCM_IMAGE_TYPE_PRIMARY, 'VOLUME', 'MIXED'])))
		sx = fake_series('enh', image_type=mixed)
		self.assertTrue(dcm_series_is_derived(sx))
		self.assertEqual([s.pk for s in dcm_primary_series_list([sx, fake_series('ct')])], ['ct'])
		with self.assertRaisesRegex(ValueError, 'enh is derived imaging \\(ImageType=MIXED'):
			dcm_check_primary_series(fake_series('ct'), [sx])
		with self.assertRaises(ValueError):
			dcm_check_primary_series(sx)
		# Ordinary acquisitions are unchanged
		self.assertFalse(dcm_tags_are_derived({DCMHEADER_IMAGE_TYPE: ORIGINAL_PRIMARY}))
		self.assertFalse(dcm_tags_are_derived({DCMHEADER_IMAGE_TYPE: 'ORIGINAL\\PRIMARY\\VOLUME\\NONE'}))
