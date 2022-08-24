'''	Utilities which allow conversion between different formats.
'''
import six, json
import pydicom.datadict as dcmcodes
from pydicom.dataset import Dataset


def json2dcmjson(jdata: dict, dcm=None):
	'''	Convert the provided JSON data (tag name and value) to a Wado-RS compliant
		representation (encoded to hexadecimal tag number and value representation).

		@input jdata (dict): Wado-RS encoded DICOMweb compliant JSON dictionary
	'''
	dcm = dcm or Dataset()

	for k,v in jdata.items():
		setattr(dcm, k, v)

	# Convert DICOM dataset to JSON
	dcmjson = dcm.to_json()

	# Ensure that the JSON data is returned as a dictionary not a string
	if isinstance(dcmjson, six.string_types):
		dcmjson = json.loads(dcmjson)
	
	return dcmjson


def dcmhexcode2tagname(hcode):
	'''	Retrieve the DICOM tag name for the provided hexadecimal code.

		@input dcmhexcode (str, tuple): hexcode for which the tag name should be retrieved.
			Supported formats
			* String: '(0010,0010)', '0010,0010', '00100010'
			* Tuple: ('0010', '0010')

		@returns str: name of the tag
	'''
	# Convert string representations of the code to a tuple for use with pydicom datadict.
	if isinstance(hcode, str):
		hcode0 = hcode

		# Convert DICOM tag to Wado-RS representation: 00100010
		hcode = hcode.replace('(', '').replace(')', '').replace(',', '')
		if not len(hcode) == 8:
			raise ValueError('Invalid DICOM tag: %s' % hcode0)

		# Split hexcode to group and element components
		hcode = (hcode[:4], hcode[4:])

	# Ensure that the DICOM tag is well formed before attempting to retrieve tag name
	if not isinstance(hcode, tuple):
		raise TypeError('Invalid DICOM tag: %s. Only tuple string representations are supported.'  % str(hcode))
	if not len(hcode) == 2:
		raise ValueError('Invalid DICOM tag: %s' % str(hcode))

	# Retrieve keyword/tagname from pydicom data dictionary
	return dcmcodes.keyword_for_tag(hcode)
