'''	Utilities which allow conversion between different formats.
'''
import six, json, datetime, logging
from collections import namedtuple
from io import BytesIO

import numpy as np

import pydicom
from pydicom import DataElement
import pydicom.datadict as dcmcodes
from pydicom.dataset import Dataset, FileDataset
from pydicom.uid import generate_uid, RLELossless

from client.utils.object import gextend, pick

from ...apisettings import DCM_PREAMBLE, DCMHEADER_SOP_CLASS_UID, DCMHEADER_MEDIA_STORAGE_SOP_CLASS_UID, \
	DCMHEADER_IMPLEMENTATION_CLASS_UID, DCMHEADER_TRANSFER_SYNTAX_UID, DCM_MEDIA_STORAGE_SECONDARY_CAPTURE, \
	DCMHEADER_SOP_INSTANCE_UID, DCMHEADER_MEDIA_STORAGE_SOP_INSTANCE_UID, DCM_DATE_STRFORMAT, DCM_TIME_STRFORMAT, \
	DCMHEADER_PHOTOMETRIC_INTERPRETATION, DCM_PHOTOMETRIC_INTERPRETATION_MONOCHROME, DCM_PHOTOMETRIC_INTERPRETATION_RGB, \
	DCMHEADER_SAMPLES_PER_PIXEL, DCMHEADER_PIXEL_REPRESENTATION, DCMHEADER_PLANAR_CONFIGURATION, DCM_SUPPORTED_IMG_DTYPES, \
	DCMHEADER_IMG_PIXELDATA, DCMHEADER_IMG_ROWS, DCMHEADER_IMG_COLS

logger = logging.getLogger(__name__)


DcmEncodeSopClassUIDs = namedtuple('SopClassUIDs', ('sopclass_uid', 'sopclass_media_uid'))
DcmEncodeFileMetaData = namedtuple('DcmEncodeFileMetaData', ('dataset', 'sopclass_uid', 'sop_instance_uid'))


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


def dcm_sopclass_uid(dcm_attrs: dict, dcmfile_meta: Dataset, sopclass_uid_default=None):
	'''	Scan the provided DICOM attributes dictionary and DICOM file metadata for the SOP Class UID
		and SOP Class Media UID. Ensure that the two identifiers match if they are provided in both
		the attrs dict and file metadata.

		The encoding method looks for the following attributes for a value (in order of preference):

		* SOPClassUID from the dcm_attrs dict
		* MediaStorageSOPClassUID from the media metadata

		@returns tuple: sopclass_uid, sopclass_media_uid
	'''
	# Retrieve the SOP class UID and media storage SOP class UID
	# The encoding method looks for the following attributes for a value (in order of preference)
	# * SOPClassUID from the dcm_attrs dictionary
	# * MediaStorageSOPClassUID from the media metadata
	sopclass_uid = dcm_attrs.pop(DCMHEADER_SOP_CLASS_UID, None) \
		or getattr(dcmfile_meta, DCMHEADER_MEDIA_STORAGE_SOP_CLASS_UID, None)
	if not sopclass_uid and sopclass_uid_default:
		sopclass_uid = sopclass_uid_default

	if not sopclass_uid:
		raise ValueError('Invalid SOPClassUID or MediaStorageSOPClassUID. Please provide '
			+ 'a valid UID via the file or file metadata attributes.')	

	# Retrieve SOP class media UID (default to SOP Class UID if one not found in the metadata)
	sopclass_media_uid = dcm_attrs.get(DCMHEADER_MEDIA_STORAGE_SOP_CLASS_UID) \
		or getattr(dcmfile_meta, DCMHEADER_MEDIA_STORAGE_SOP_CLASS_UID, None) or sopclass_uid

	# Ensure that the SOP class UID and SOP class media UID match
	if sopclass_uid != sopclass_media_uid:
		raise ValueError('SOPClassUID="%s" does not match the provided MediaStorageSOPClassUID="%s"'
			% (sopclass_uid, sopclass_media_uid))

	return DcmEncodeSopClassUIDs(sopclass_uid, sopclass_media_uid)


def dcm_encode_filemeta(dcm_attrs: dict, dcmfile_meta:Dataset=None, **kwargs):
	'''	Create a DICOM file metadata block which references the SOP class UID, the 
		storage instance UID, and the storage instance UID. The file metadata block
		is used by the DICOM standard for transfer and validation of file structure.

		@input dcm_attrs (dict): DICOM attributes to be applied to copied to the meta Dataset
		@input dcmfile_meta (pydicom.Dataset, default=new Dataset): Dataset to which the
			UIDs and other identifiers should be copied.

		@returns pydicom.Dataset: Encoded file metadata
	'''
	dcmfile_meta = dcmfile_meta or Dataset()

	# Retrieve the SOP class UID and media storage SOP class UID.
	sopclass_uid, sopclass_media_uid = dcm_sopclass_uid(
		dcm_attrs, dcmfile_meta, **pick(kwargs, ('sopclass_uid_default',)))

	# Apply SOP class media UID to media meta (if the SOP class it not already set)
	if not getattr(dcmfile_meta, DCMHEADER_MEDIA_STORAGE_SOP_CLASS_UID, None):
		setattr(dcmfile_meta, DCMHEADER_MEDIA_STORAGE_SOP_CLASS_UID, sopclass_media_uid)

	# Retrieve implementation class UID from media attributes or use PyDICOM implementation UID as fallback
	sop_implementation_uid = getattr(dcmfile_meta, DCMHEADER_IMPLEMENTATION_CLASS_UID, None) \
		or pydicom.uid.PYDICOM_IMPLEMENTATION_UID
	if not getattr(dcmfile_meta, DCMHEADER_IMPLEMENTATION_CLASS_UID, None):
		setattr(dcmfile_meta, DCMHEADER_IMPLEMENTATION_CLASS_UID, sop_implementation_uid)

	# Retrieve instance UID. If there is not an instance UID, one will be created.
	# The encoding looks for values using the folling attributes (in order of preference):
	# * SOPInstanceUID from the dcm_attrs dictionary
	# * MediaStorageSOPInstanceUID from the media metadata
	sop_instance_uid = dcm_attrs.get(DCMHEADER_SOP_INSTANCE_UID) \
		or getattr(dcmfile_meta, DCMHEADER_MEDIA_STORAGE_SOP_INSTANCE_UID, None)
	sop_media_uid = getattr(dcmfile_meta, DCMHEADER_MEDIA_STORAGE_SOP_INSTANCE_UID, None) or sop_instance_uid

	# Ensure that the SOP instance UID and the media UID are the same
	if not sop_instance_uid == sop_media_uid:
		raise ValueError('SOPInstanceUID does not match the provided MediaStorageSOPInstanceUID')
	if not sop_instance_uid:
		sop_instance_uid = sop_media_uid = generate_uid()

	# Apply SOP media UID
	if not getattr(dcmfile_meta, DCMHEADER_MEDIA_STORAGE_SOP_INSTANCE_UID, None):
		setattr(dcmfile_meta, DCMHEADER_MEDIA_STORAGE_SOP_INSTANCE_UID, sop_media_uid)

	return DcmEncodeFileMetaData(dcmfile_meta, sopclass_uid, sop_instance_uid)


def dcm_encode_filedata(dcm_attrs: dict, dcm_file=None, dcmfile_meta=None, dcm_preamble=DCM_PREAMBLE,
		dcm_little_endian=True, dcm_implicit_vr=True, dcm_ts=None, **kwargs):
	'''	Create a DICOM file dataset from the provided parameters

		@input dcm_attrs (dict): DICOM attributes to be applied to the encoded data
		@input dcm_file (file-like object, default=new BytesIO): file object to be used in the 
			creation of the DICOM image.
		@input dcm_little_endian (bool, default=True): indicates whether the dataset is using a Little Endian
			VR transfer syntax which is an uncompressed (native) transfer syntax. When in use Little Endian Impmlicit
			VR implies that there is not compression of the Pixel Data.

		@returns pydicom.FileDataset
	'''
	# Create DICOM file metadata
	dcmfile_meta, sopclass_uid, sop_instance_uid = dcm_encode_filemeta(dcm_attrs, dcmfile_meta=dcmfile_meta, **kwargs)

	# Create file dataset from meta
	ds = FileDataset(dcm_file or BytesIO(), {}, file_meta=dcmfile_meta, preamble=dcm_preamble)
	ds.is_little_endian = dcm_little_endian
	ds.is_implicit_VR = dcm_implicit_vr

	# Content timestamp
	ts = dcm_ts or datetime.datetime.now()
	ds.ContentDate = ts.strftime(DCM_DATE_STRFORMAT)
	ds.ContentTime = ts.strftime(DCM_TIME_STRFORMAT)

	# Add identifiers to file
	setattr(ds, DCMHEADER_SOP_CLASS_UID, sopclass_uid)
	setattr(ds, DCMHEADER_SOP_INSTANCE_UID, sop_instance_uid)

	# Add media file attributes
	for dcm_key, dcm_val in dcm_attrs.items():

		# For values encoded as data elements, add the dataset directly
		if isinstance(dcm_val, DataElement):
			ds.add(dcm_val)
		
		# Set via attribute string (implicit conversion)
		else:
			setattr(ds, dcm_key, dcm_val)

	return ds


# Image compression algorithms supported by Sonador
DCM_COMPRESSION_SUPPORTED = set((RLELossless,))
	

def array2dcmimg(arr: np.ndarray, photometric_interpretation=DCM_PHOTOMETRIC_INTERPRETATION_MONOCHROME, 
		dcm_attrs=None, dcm_compression=None, **kwargs):
	'''	Convert the provided array to a DICOM instance

		@input dcm_attrs (dict): DICOM attributes to be applied to the encoded data
		@input dcm_file (file-like object, default=new BytesIO): file object to be used in the 
			creation of the DICOM image.

		@returns pydicom.FileDataset
	'''
	dcm_attrs = dcm_attrs or {}

	# Populate the Storage SOP Class UID with a default value (secondary capture) if 
	# one was not provided
	if not dcm_attrs.get(DCMHEADER_SOP_CLASS_UID):
		dcm_attrs[DCMHEADER_SOP_CLASS_UID] = DCM_MEDIA_STORAGE_SECONDARY_CAPTURE

	# Create DICOM file metadata
	ds = dcm_encode_filedata(dcm_attrs, **kwargs)

	# Image encoding parameters: photometric interpretation, data type, compression
	setattr(ds, DCMHEADER_PHOTOMETRIC_INTERPRETATION, photometric_interpretation)

	# Grayscale image
	if photometric_interpretation == DCM_PHOTOMETRIC_INTERPRETATION_MONOCHROME:

		# Ensure that the provided NumPy array has one pixel channel
		if len(arr.shape) > 2:
			raise ValueError('Invalid array for MONOCHROME image: %s' % str(arr.shape))

		setattr(ds, DCMHEADER_SAMPLES_PER_PIXEL, 1)

	# RGB image
	elif photometric_interpretation == DCM_PHOTOMETRIC_INTERPRETATION_RGB:

		# Ensure the provided NumPy array has three pixel channels
		if not len(arr.shape) == 3 and not arr.shape[2] == 3:
			raise ValueError(('Invalid array for RGB image: %s. RGB images must have separate red, green, '
					+ 'and blue color.') % str(arr.shape))

		# SamplesPerPixel: Number of samples (planes) in this image.
    	#   https://dicom.innolitics.com/ciods/us-image/us-image/00280002
		setattr(ds, DCMHEADER_SAMPLES_PER_PIXEL, 3)
		
		# PlanarRepresentation: Indicates whether the pixel data are encoded color-by-plane or color-by-pixel.
    	#   https://dicom.innolitics.com/ciods/ct-image/image-pixel/00280006
		setattr(ds, DCMHEADER_PLANAR_CONFIGURATION, 0)

	# Unsupported photometric interpretation
	else: raise TypeError('Unsupported photometric interpretation: %s' % photometric_interpretation)

	# Add image pixel representation headers to dataset
	if not str(arr.dtype) in DCM_SUPPORTED_IMG_DTYPES:
		raise ValueError('Unsupported image dtype: %s' % arr.dtype)

	for h,hval in DCM_SUPPORTED_IMG_DTYPES[str(arr.dtype)].items():
		setattr(ds, h, hval)

	# Rows/Columns
	setattr(ds, DCMHEADER_IMG_ROWS, arr.shape[0])
	setattr(ds, DCMHEADER_IMG_COLS, arr.shape[1])

	# Compress dataset
	if dcm_compression:

		# Ensure that compression type is supported
		if not dcm_compression in DCM_COMPRESSION_SUPPORTED:
			raise ValueError('Unsupported compression type: %s' % str(dcm_compression))

		# Apply compression
		ds.compress(dcm_compression, arr)

	# Save uncompressed image
	else:
		setattr(ds, DCMHEADER_IMG_PIXELDATA, arr.tobytes())

	return ds
