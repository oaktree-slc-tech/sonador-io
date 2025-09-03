import os, logging, datetime

from client.utils.general import create_token

from pydicom import dcmread
from pydicom.dataset import FileMetaDataset
from pydicom.uid import generate_uid, ImplicitVRLittleEndian, ExplicitVRLittleEndian, ExplicitVRBigEndian, \
	PYDICOM_ROOT_UID

from ..apisettings.base import DCMHEADER_SOP_INSTANCE_UID, DCMHEADER_TRANSFER_SYNTAX_UID, DCMHEADER_SOP_CLASS_UID, \
	DCMHEADER_SERIES_INSTANCE_UID, DCMHEADER_STUDY_INSTANCE_UID, \
	DCMHEADER_PATIENT_ID, DCMTS_STUDY, DCMTS_SERIES, DCMTS_CONTENT, \
	SONADOR_CLIENT, SONADOR_SCHEME_VERSION

logger = logging.getLogger(__name__)


def dcmread_backfill(fpath, patient_id=None, study_uid=None, series_uid=None, instance_uid=None, 
		study_ts=None, series_ts=None, content_ts=None, attrs=None,):
	'''	Read the provided file from disk, backfill any missing required attributes.

			@input fpath (str): file to load from disk
			@input patient_id (str, default=random string): Patient ID to be used as a back-fill
				if the patient ID is missing from the dataset.
			@input series_uid (str, default=new UID): study instance UID to be used as a back-fill
				if the study UID is missing.
			@input series_uid (str, default=new UID): series instance UID to be used as a back-fill
				if the series UID is missing.
			@input instance_uid (str, default=new UID): SOP instance UID to be used as a back-fill
				if the instance UID is missing.
			@input study_ts (datetime.datetime, default=datetime.datetime.utcnow): timestamp to be
				used for the study date and time
			@input series_ts (datetime.datetime, default=datetime.datetime.utcnow): timestamp to be
				used for the series date and time
			@input content_ts (datetime.datetime, default=datetime.datetime.utcnow): timestamp to be
				used for the content date and time
			@input attrs (dict, default=empty dict): additional header/value pairs to be checked
				and back-filled.

			@returns dataset
	'''
	if not os.path.exists(fpath):
		raise ValueError('Unable to load DICOM "%s". File does not exist.' % fpath)

	# Load DICOM dataset from disk
	_dcm = dcmread(fpath)

	# Verify required components and back-fill those which are missing
	for attr, val, default_callable in (
		(DCMHEADER_PATIENT_ID, patient_id, create_token),
		(DCMHEADER_STUDY_INSTANCE_UID, study_uid, generate_uid),
		(DCMHEADER_SERIES_INSTANCE_UID, series_uid, generate_uid),
		(DCMHEADER_SOP_INSTANCE_UID, instance_uid, generate_uid)):

		if not getattr(_dcm, attr, None):
			setattr(_dcm, attr, val or default_callable())

	# Ensure that study, series, and content times are filled
	for ts, dcm_ts in ((study_ts, DCMTS_STUDY), (series_ts, DCMTS_SERIES), (content_ts, DCMTS_CONTENT)):
		ts = ts or datetime.datetime.utcnow()
		if not getattr(_dcm, dcm_ts.date_tag, None):
			setattr(_dcm, dcm_ts.date_tag, ts.date())
		if not getattr(_dcm, dcm_ts.time_tag, None):
			setattr(_dcm, dcm_ts.time_tag, ts.time())

	# Check and back-fill extra attributes
	for attr,val in (attrs or {}).items():
		
		if not getattr(_dcm, attr, None):
			setattr(_dcm, attr, val)

	return _dcm


def dcm_part10_backfill(dcm, implementation_class_uid=PYDICOM_ROOT_UID, 
		implementation_class_version='%s %s' % (SONADOR_CLIENT, SONADOR_SCHEME_VERSION)):
	'''	Read the provied DICOM dataset and ensure that it is well formed and complete 
		(per Part10 encoding rules).

		@input dcm (pydicom.Dataset): dataset to verify
		@returns tuple
			- pydicom.Dataset: updated version of the DICOM dataset
			- bool: True if the dataset was modified, False otherwise
	'''
	_updated = False

	# Check for file meta
	_filemeta = getattr(dcm, 'file_meta', None)
	if _filemeta is None or not getattr(_filemeta, DCMHEADER_TRANSFER_SYNTAX_UID, None):

		# Infer transfer syntax from the dataset flags
		if dcm.is_little_endian and dcm.is_implicit_VR:
			ts = ImplicitVRLittleEndian
		elif dcm.is_little_endian and not dcm.is_implicit_VR:
			ts = ExplicitVRLittleEndian
		elif not dcm.is_little_endian and not dcm.is_implicit_VR:
			rs = ExplicitVRBigEndian

		else:
			# Extremely rare, default to Explicit LE
			ts = ExplicitVRLittleEndian

		# Create file meta
		_filemeta = _filemeta or FileMetaDataset()
		_filemeta.TransferSyntaxUID = ts
		
		# Populate required meta fields if available in dataset
		if DCMHEADER_SOP_CLASS_UID in dcm:
			_filemeta.MediaStorageSOPClassUID = dcm.SOPClassUID
		
		if DCMHEADER_SOP_INSTANCE_UID in dcm:
			_filemeta.MediaStorageSOPInstanceUID = dcm.SOPInstanceUID
		
		# Implementation class headers
		_filemeta.ImplementationClassUID = implementation_class_uid
		_filemeta.ImplementationVersionName = implementation_class_version[:16]

		dcm.file_meta = _filemeta
		_updated = True

	return dcm, _updated