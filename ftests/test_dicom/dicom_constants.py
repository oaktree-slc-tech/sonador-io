'''	Offline checks of the DICOM header constants of `sonador.apisettings.base`: every keyword resolves in
	the pydicom data dictionary to the tag given by its `DCMCODE_*` companion, and the enumerated Image
	Type values are the defined terms of PS3.3 C.7.6.1.1.2 (upper-case CS values).

		python -m unittest sonador.ftests.test_dicom.dicom_constants
'''
import unittest

from pydicom.dataset import Dataset
from pydicom.datadict import tag_for_keyword, dictionary_VR
from pydicom.tag import Tag

from ...apisettings import DCMCODE_IMAGE_TYPE, DCMHEADER_IMAGE_TYPE, \
	DCM_IMAGE_TYPE_ORIGINAL, DCM_IMAGE_TYPE_DERIVED, DCM_IMAGE_TYPE_PRIMARY, DCM_IMAGE_TYPE_SECONDARY, \
	DCMCODE_DERIVATION_DESCRIPTION, DCMHEADER_DERIVATION_DESCRIPTION, \
	DCMCODE_WINDOW_CENTER, DCMHEADER_WINDOW_CENTER, DCMCODE_WINDOW_WIDTH, DCMHEADER_WINDOW_WIDTH, \
	DICOM_VR_CS


__all__ = ['HeaderConstantTestCase', 'ImageTypeValueTestCase']


# (keyword, tag code pair, VR)
HEADER_CONSTANTS = (
	(DCMHEADER_IMAGE_TYPE, DCMCODE_IMAGE_TYPE, 'CS'),
	(DCMHEADER_DERIVATION_DESCRIPTION, DCMCODE_DERIVATION_DESCRIPTION, 'ST'),
	(DCMHEADER_WINDOW_CENTER, DCMCODE_WINDOW_CENTER, 'DS'),
	(DCMHEADER_WINDOW_WIDTH, DCMCODE_WINDOW_WIDTH, 'DS'),
)


class HeaderConstantTestCase(unittest.TestCase):
	''' The header keywords name registered attributes and agree with their tag code constants.
	'''
	def test_keyword_resolves_to_code(self):
		for keyword, (group, element), vr in HEADER_CONSTANTS:
			with self.subTest(keyword=keyword):
				tag = tag_for_keyword(keyword)
				self.assertIsNotNone(tag, '%s is not a registered DICOM keyword' % keyword)
				self.assertEqual(Tag(tag), Tag(int(group, 16), int(element, 16)))
				self.assertEqual(dictionary_VR(tag), vr)


class ImageTypeValueTestCase(unittest.TestCase):
	''' The Image Type values are the defined terms and compose into a valid multi-valued attribute.
	'''
	def test_values_are_defined_terms(self):
		self.assertEqual(DCM_IMAGE_TYPE_ORIGINAL, 'ORIGINAL')
		self.assertEqual(DCM_IMAGE_TYPE_DERIVED, 'DERIVED')
		self.assertEqual(DCM_IMAGE_TYPE_PRIMARY, 'PRIMARY')
		self.assertEqual(DCM_IMAGE_TYPE_SECONDARY, 'SECONDARY')
		for value in (DCM_IMAGE_TYPE_ORIGINAL, DCM_IMAGE_TYPE_DERIVED, DCM_IMAGE_TYPE_PRIMARY,
				DCM_IMAGE_TYPE_SECONDARY):
			self.assertEqual(value, value.upper())
			self.assertNotIn('\\', value)

	def test_derived_secondary_attribute(self):
		ds = Dataset()
		setattr(ds, DCMHEADER_IMAGE_TYPE, [DCM_IMAGE_TYPE_DERIVED, DCM_IMAGE_TYPE_SECONDARY])
		elem = ds[DCMHEADER_IMAGE_TYPE]
		self.assertEqual(elem.VR, DICOM_VR_CS)
		self.assertEqual(elem.VM, 2)
		self.assertEqual(list(elem.value), ['DERIVED', 'SECONDARY'])
		# the attribute is written as a backslash-separated CS value
		self.assertEqual(str(elem.value), "['DERIVED', 'SECONDARY']")
		self.assertEqual('\\'.join(elem.value), 'DERIVED\\SECONDARY')
