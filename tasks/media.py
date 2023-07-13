import datetime, pydicom
from pydicom.dataset import Dataset, FileDataset
from pydicom.uid import generate_uid

from ..apisettings import DCMHEADER_MODALITY, DCMHEADER_SOP_CLASS_UID, DCMHEADER_MEDIA_STORAGE_SOP_CLASS_UID, \
	DCMHEADER_IMPLEMENTATION_CLASS_UID, DCMHEADER_SOP_INSTANCE_UID, DCMHEADER_MEDIA_STORAGE_SOP_INSTANCE_UID, \
	DCMHEADER_ENCAPSULATED_DOCUMENT_MIMETYPE, \
	DCM_DATE_STRFORMAT, DCM_TIME_STRFORMAT, DCM_CONTENT_TYPE
from ..apisettings.media import DCMEDIA_ENCAPSULATED_PDF_SOP_CLASS, DCMEDIA_PDF_MIMETYPE, DCMEDIA_PDF_MODALITY, \
	DCMEDIA_ENCAPSULATED_OBJ_SOP_CLASS, DCMEDIA_GLB_MIMETYPE, DCMEDIA_GLB_MODALITY, \
	DCMEDIA_ENCAPSULATED_STL_SOP_CLASS, DCMEDIA_ENCAPSULATED_STL_SOP_CLASS_CT,  \
	DCMEDIA_ENCAPSULATED_STL_SOP_CLASS_MRI, DCMEDIA_SUPPORTED_STL_SOP_CLASSES, \
		DCMEDIA_STL_MIMETPYE, DCMEDIA_STL_MODALITY


def dcm_encode_media(mediafile_meta, media_file, dcm_attrs=None, dcm_preamble=b'\0'*128,
		dcm_little_endian=True, dcm_implicit_vr=True, dcm_ts=None):
	'''	Create a DICOM file from the provided meta and file

		@input mediafile_meta (pydicom.dataset.Dataset): file metadata (refer to SOP module of the DICOM standard)
		@input media_file (file-like object): media file to be encoded.
		@input dcm_attrs (dict): DICOM attributes to be applied to the encoded data.

		@returns (pydicom.dataset.FileDataset)
	'''
	dcm_attrs = dcm_attrs or {}

	# Retrieve the SOP class UID and media storage SOP class UID. 
	# The encoding method looks for the following attributes for a value (in order of preference):
	# * SOPClassUID from the dcm_attrs dictionary
	# * MediaStorageSOPClassUID from the media metadata
	sopclass_uid = dcm_attrs.pop(DCMHEADER_SOP_CLASS_UID, None) \
		or getattr(mediafile_meta, DCMHEADER_MEDIA_STORAGE_SOP_CLASS_UID, None)
	sopclass_media_uid = getattr(mediafile_meta, DCMHEADER_MEDIA_STORAGE_SOP_CLASS_UID, None) or sopclass_uid

	if not sopclass_uid:
		raise ValueError('Invalid SOPClassUID or MediaStorageSOPClassUID. Please provide '
			+ 'a valid UID via the file or file metadata attributes.')

	# Ensure that the SOP class UID and SOP class media UID match
	if not sopclass_uid == sopclass_media_uid:
		raise ValueError('SOPClassUID does not match the provided MediaStorageSOPClassUID')

	# Apply SOP class media UID to media meta (if the SOP class it not already set)
	if not getattr(mediafile_meta, DCMHEADER_MEDIA_STORAGE_SOP_CLASS_UID, None):
		setattr(mediafile_meta, DCMHEADER_MEDIA_STORAGE_SOP_CLASS_UID, sopclass_media_uid)

	# Retrieve implementation class UID from media attributes or use PyDICOM implementation UID as fallback
	sop_implementation_uid = getattr(mediafile_meta, DCMHEADER_IMPLEMENTATION_CLASS_UID, None) \
		or pydicom.uid.PYDICOM_IMPLEMENTATION_UID
	if not getattr(mediafile_meta, DCMHEADER_IMPLEMENTATION_CLASS_UID, None):
		setattr(mediafile_meta, DCMHEADER_IMPLEMENTATION_CLASS_UID, sop_implementation_uid)

	# Retrieve instance UID. If not instance UID exists, one will be created.
	# The encoding looks for values using the folling attributes (in order of preference):
	# * SOPInstanceUID from the dcm_attrs dictionary
	# * MediaStorageSOPInstanceUID from the media metadata
	sop_instance_uid = dcm_attrs.get(DCMHEADER_SOP_INSTANCE_UID) \
		or getattr(mediafile_meta, DCMHEADER_MEDIA_STORAGE_SOP_INSTANCE_UID, None)
	sop_media_uid = getattr(mediafile_meta, DCMHEADER_MEDIA_STORAGE_SOP_INSTANCE_UID, None) or sop_instance_uid

	# Ensure that the SOP instance UID and the media UID are the same
	if not sop_instance_uid == sop_media_uid:
		raise ValueError('SOPInstanceUID does not match the provided MediaStorageSOPInstanceUID')
	if not sop_instance_uid:
		sop_instance_uid = sop_media_uid = generate_uid()

	# Apply SOP media UID
	if not getattr(mediafile_meta, DCMHEADER_MEDIA_STORAGE_SOP_INSTANCE_UID, None):
		setattr(mediafile_meta, DCMHEADER_MEDIA_STORAGE_SOP_INSTANCE_UID, sop_media_uid)

	# Create file dataset from meta
	ds = FileDataset(media_file, {}, file_meta=mediafile_meta, preamble=dcm_preamble)
	ds.is_little_endian = dcm_little_endian
	ds.is_implicit_VR = dcm_implicit_vr

	# Media timestamp
	ts = dcm_ts or datetime.datetime.now()
	ds.ContentDate = ts.strftime(DCM_DATE_STRFORMAT)
	ds.ContentTime = ts.strftime(DCM_TIME_STRFORMAT)

	# Add identifiers to file
	setattr(ds, DCMHEADER_SOP_CLASS_UID, sopclass_uid)
	setattr(ds, DCMHEADER_SOP_INSTANCE_UID, sop_instance_uid)

	# Add media file attributes
	for dcm_key, dcm_val in dcm_attrs.items():
		setattr(ds, dcm_key, dcm_val)

	# Add file and mimetype to encapsulated document
	ds.EncapsulatedDocument = media_file.read()
	setattr(ds, DCMHEADER_ENCAPSULATED_DOCUMENT_MIMETYPE, 
		dcm_attrs.get(DCMHEADER_ENCAPSULATED_DOCUMENT_MIMETYPE, DCM_CONTENT_TYPE))

	return ds


def dcm_encode_pdf(pdf_dcmattrs, pdf_file, *args, dcmfile_meta=None, **kwargs):
	'''	Create a DICOM file that encapsulates a PDF document. Delegates to dcm_encode_media.

		@input pdf_dcmattrs	(dict): DICOM attributes to be added to the file
		@input pdf_file (file-like-object): PDF file to be encoded.
		@input dcmfile_meta (pydicom.dataset.Dataset, default=empty dataset): pre-existing 
			set of headers to which media files should be added.

		@returns pydicom.dataset.FileDataset
	'''
	if not pdf_dcmattrs or not isinstance(pdf_dcmattrs, dict):
		raise ValueError('Invalid DICOM metadata dictionary')

	if pdf_dcmattrs.get(DCMHEADER_SOP_CLASS_UID) \
		and pdf_dcmattrs.get(DCMHEADER_SOP_CLASS_UID) != DCMEDIA_ENCAPSULATED_PDF_SOP_CLASS:
		raise ValueError('Invalid PDF MediaStorageSOPClassUID: %s' % pdf_dcmattrs.get(DCMHEADER_SOP_CLASS_UID))
	if pdf_dcmattrs.get(DCMHEADER_ENCAPSULATED_DOCUMENT_MIMETYPE) \
		and pdf_dcmattrs.get(DCMHEADER_ENCAPSULATED_DOCUMENT_MIMETYPE) != DCMEDIA_PDF_MIMETYPE:
		raise ValueError('Invalid PDF MIMETypeOfEncapsulatedDocument: %s' % pdf_dcmattrs.get(DCMHEADER_ENCAPSULATED_DOCUMENT_MIMETYPE))

	# Add PDF mimetype to metadata
	if not pdf_dcmattrs.get(DCMHEADER_ENCAPSULATED_DOCUMENT_MIMETYPE):
		pdf_dcmattrs[DCMHEADER_ENCAPSULATED_DOCUMENT_MIMETYPE] = DCMEDIA_PDF_MIMETYPE

	# Add "DOC" as the modality if another value is not specified
	if not pdf_dcmattrs.get(DCMHEADER_MODALITY):
		pdf_dcmattrs[DCMHEADER_MODALITY] = DCMEDIA_PDF_MODALITY

	# Create file metadata structure for an encapsulated PDF document
	fmeta = dcmfile_meta or Dataset()
	setattr(fmeta, DCMHEADER_MEDIA_STORAGE_SOP_CLASS_UID, 
		pdf_dcmattrs.get(DCMHEADER_SOP_CLASS_UID, DCMEDIA_ENCAPSULATED_PDF_SOP_CLASS))

	return dcm_encode_media(fmeta, pdf_file, *args, dcm_attrs=pdf_dcmattrs, **kwargs)


def dcm_encode_glb(glb_dcmattrs, glb_file, *args, dcmfile_meta=None, **kwargs):
	'''	Create a DICOM file that encapsulates a GLB document. Delegates to dcm_encode_media.

		@input glb_dcmattrs (dict): DICOM attributes to be added to the file.
		@input glb_file (file-like-object): GLB file to be encoded
		@input dcmfile_meta (pydicom.dataset.Dataset, default=empty dataset): pre-existing
			set of headers to which media files should be added.

		@returns pydicom.dataset.FileDataset
	'''
	if not glb_dcmattrs or not isinstance(glb_dcmattrs, dict):
		raise ValueError('Invalid DICOM metadata dictionary')

	# Check for OBJ class UID and mimetype compatability
	if glb_dcmattrs.get(DCMHEADER_SOP_CLASS_UID) \
		and glb_dcmattrs.get(DCMHEADER_SOP_CLASS_UID) != DCMEDIA_ENCAPSULATED_OBJ_SOP_CLASS:
		raise ValueError('Invalid GLB MediaStorageSOPClassUID: %s. GLB must use the OBJ encapsulated document storage.')
	if glb_dcmattrs.get(DCMHEADER_ENCAPSULATED_DOCUMENT_MIMETYPE) \
		and glb_dcmattrs.get(DCMHEADER_ENCAPSULATED_DOCUMENT_MIMETYPE) != DCMEDIA_GLB_MIMETYPE:
		raise ValueError('Invalid GLB MIMETypeOfEncapsulatedDocument: %s' 
			% glb_dcmattrs.get(DCMHEADER_ENCAPSULATED_DOCUMENT_MIMETYPE))

	# # Add "M3D" as modality if another value is not specified
	if not glb_dcmattrs.get(DCMHEADER_MODALITY):
		glb_dcmattrs[DCMHEADER_MODALITY] = DCMEDIA_GLB_MODALITY

	# Add GLB mimetype to metadata
	if not glb_dcmattrs.get(DCMHEADER_ENCAPSULATED_DOCUMENT_MIMETYPE):
		glb_dcmattrs[DCMHEADER_ENCAPSULATED_DOCUMENT_MIMETYPE] = DCMEDIA_GLB_MIMETYPE

	# Create file metadata structure for an encapsulated GLB object
	fmeta = dcmfile_meta or Dataset()
	setattr(fmeta, DCMHEADER_MEDIA_STORAGE_SOP_CLASS_UID,
		glb_dcmattrs.get(DCMHEADER_SOP_CLASS_UID, DCMEDIA_ENCAPSULATED_OBJ_SOP_CLASS))

	return dcm_encode_media(fmeta, glb_file, *args, dcm_attrs=glb_dcmattrs, **kwargs)


def dcm_encode_stl(stl_dcmattrs, stl_file, *args, dcmfile_meta=None, **kwargs):
	'''	Create a DICOM file that encapsulates an STL document. Delegates to dcm_encode_media.

		@input stl_dcmattrs (dict): DICOM attributes to be added to the file
		@input stl_file (like-like-object): STL file to be encoded
		@input dcmfile_meta (pydicom.dataset.Dataset, default=empty dataset): pre-existing
			set of headers to which media files should be added.

		@returns pydicom.dataset.FileDataset
	'''
	if not stl_dcmattrs or not isinstance(stl_dcmattrs, dict):
		raise ValueError('Invalid DICOM metadata dictionary')

	# Ensure that an STL class UID was provided
	if not stl_dcmattrs.get(DCMHEADER_SOP_CLASS_UID):
		stl_dcmattrs[DCMHEADER_SOP_CLASS_UID] = DCMEDIA_ENCAPSULATED_STL_SOP_CLASS

	# Check for STL class UIDs and mimetype compatability
	if stl_dcmattrs.get(DCMHEADER_SOP_CLASS_UID) \
		and not stl_dcmattrs.get(DCMHEADER_SOP_CLASS_UID) in DCMEDIA_SUPPORTED_STL_SOP_CLASSES:
		raise ValueError('Invalid STL SOPClassUID: %s. Supported classes: %s.'
			% (stl_dcmattrs.get(DCMHEADER_SOP_CLASS_UID), ' ,'.join(v for v in DCMEDIA_SUPPORTED_STL_SOP_CLASSES)))
	if stl_dcmattrs.get(DCMHEADER_ENCAPSULATED_DOCUMENT_MIMETYPE) \
		and stl_dcmattrs.get(DCMHEADER_ENCAPSULATED_DOCUMENT_MIMETYPE) != DCMEDIA_STL_MIMETPYE:
		raise ValueError('Invalid STL MIMETypeOfEncapsulatedDocument: %s' 
			% stl_dcmattrs.get(DCMHEADER_ENCAPSULATED_DOCUMENT_MIMETYPE))

	# Add "M3D" as modality if another value is not specified
	if not stl_dcmattrs.get(DCMHEADER_MODALITY):
		stl_dcmattrs[DCMHEADER_MODALITY] = DCMEDIA_STL_MODALITY

	# Add STL mimetype to metadata
	if not stl_dcmattrs.get(DCMHEADER_ENCAPSULATED_DOCUMENT_MIMETYPE):
		stl_dcmattrs[DCMHEADER_ENCAPSULATED_DOCUMENT_MIMETYPE] = DCMEDIA_STL_MIMETPYE

	# Create file metadata structure for an encapsulated STL object
	fmeta = dcmfile_meta or Dataset()
	setattr(fmeta, DCMHEADER_MEDIA_STORAGE_SOP_CLASS_UID,
		stl_dcmattrs.get(DCMHEADER_SOP_CLASS_UID))

	return dcm_encode_media(fmeta, stl_file, *args, dcm_attrs=stl_dcmattrs, **kwargs)
