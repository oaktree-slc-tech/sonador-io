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
from ..imaging.helpers.conversion import dcm_encode_filedata


def dcm_encode_media(mediafile_meta, media_file, dcm_attrs=None, **kwargs):
	'''	Create a DICOM file from the provided meta and file

		@input mediafile_meta (pydicom.dataset.Dataset): file metadata (refer to SOP module of the DICOM standard)
		@input media_file (file-like object): media file to be encoded.
		@input dcm_attrs (dict): DICOM attributes to be applied to the encoded data.

		@returns (pydicom.dataset.FileDataset)
	'''
	dcm_attrs = dcm_attrs or {}

	# Encode base DICOM file metadata and dataset structure
	ds = dcm_encode_filedata(dcm_attrs, dcm_file=media_file, dcmfile_meta=mediafile_meta, **kwargs)

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
