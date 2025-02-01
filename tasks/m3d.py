'''	Helper methods for the encoding of 3D STL files to DICOM
'''
import os, logging, posixpath, yaml, requests, datetime, itertools, copy, array
from collections import OrderedDict

from io import BytesIO

# Client utilities
from client.utils.urls import validate_url
from client.utils.conversion import str2bool
from client.utils.object import pick
from client.errors import ClientOperationError

# PyDICOM 
import pydicom
from pydicom.sr.codedict import codes as dcmcodes
from pydicom.uid import generate_uid
from pydicom.dataset import Dataset, FileDataset
from pydicom.sequence import Sequence

# HighDICOM
from highdicom.color import CIELabColor

from ..apisettings import DicomMetaKey, DicomMeta, DCM_DATE_STRFORMAT, DCM_TIME_STRFORMAT, \
	IMAGING_SERVER_RESOURCE_SERIES, DCMHEADER_SERIES_NUMBER, DCMHEADER_SERIES_DESCRIPTION, \
	DCMHEADER_SERIES_DATE, DCMHEADER_SERIES_TIME, DCMHEADER_SR_REF_SERIES_SEQ, DCMHEADER_MODALITY, \
	DCMHEADER_SERIES_INSTANCE_UID, DCMHEADER_INSTANCE_NUMBER, DCMHEADER_CONTENT_DESCRIPTION, \
	DCMHEADER_CONTENT_DATE, DCMHEADER_CONTENT_TIME, DCMHEADER_RECOMMENDED_DISPLAY_COLOR_VALUE, \
	DCMHEADER_SR_SOP_CLASS_UID
from ..apisettings.m3d import M3D_DCM_PATIENT_TAGS, M3D_DCM_STUDY_TAGS, M3D_DCM_SERIES_TAGS, \
	M3D_CLINICAL_TRIAL_TAGS

from ..helpers import srcode2dataset, dcm_encode_series_ref
from ..helpers.color import hex2rgb, rgb2cielab

from .media import dcm_encode_stl

logger = logging.getLogger(__name__)


def dcm_encode_m3d_models(sx, m3d_models, m3d_series_uid=None, m3d_series_number=200,
		ref_sopclass_uid=None, m3d_series_headers=None, m3d_instance_headers=None,
		stl_headers=[v for v in itertools.chain(
			M3D_DCM_PATIENT_TAGS, M3D_DCM_STUDY_TAGS, M3D_DCM_SERIES_TAGS, M3D_CLINICAL_TRIAL_TAGS)],
		stl_instance_numbers=None, stl_file_colors=None, privateheaders_blocks=None,
		privateheaders_callback=None, presave_callback=None, dry_run=False, hcache=None):
	''' Encode M3D models for the provided imaging series. All provided models will be part of a common series.

		@input sx (sonador.imaging.orthanc.base.ImagingSeries): imaging series to be used
			as the source for the M3D models. The models will be linked to the series
			and be part of the same study.
		@input m3d_models (dict of file-like object): STL files to be encoded and uploaded to Orthanc
		@input m3d_series_uid (str, default=new UID): UID to be used to identify the M3D model series
		@input m3d_series_number (int, default=200): number to be used for the M3D series
		@input ref_sopclass_uid (str, default=source series class UID): SOP class UID used to 
			identify the source files for the models
		@input stl_headers (iterable): iterable of headers to be sourced from the provided 
			series and copied to the M3D series.
		@input stl_instance_numbers (dict): instance numbers keyed to the file labels
		@input stl_file_colors (dict): color codes keyed to the file labels
		@input dry_run (bool, default=False): when True, models are converted to encapsulated STL
			but no files are transferred to the server.
		
		@input privateheaders_callback (callable, default=None): Callback function which
			can be used to dynamically add extra headers to the DICOM headers block.
			- visblk (pydicom.dataset.Dataset): Private data block containing
				the private headers.
			- mlabel (str): label of the model
			- dcmfile (pydicom.dataset.Dataset): PyDicom dataset object,
				containing a dictionary of the DICOM data elements.
		
		@input presave_callback (callable, default=None): Callback function invoked before saving
			the modified DICOM file structure. Signature:
			- mlabel (str): label of the model
			- dcmfile (pydicom.dataset.Dataset): PyDicom dataset object,
				containing a dictionary of the DICOM data elements.

		@returns hcache (OrderedDict): dictionary of meta keys and metadata for the uploaded data
	'''
	hcache = hcache if isinstance(hcache, (OrderedDict, dict)) else OrderedDict()
	stl_instance_numbers = stl_instance_numbers or {}
	stl_file_colors = stl_file_colors or {}

	# Retrieve white listed set of patient study and series headers. Used by all instances in the series.
	sx_hdata = pick(sx.slices_collection[0].tags, stl_headers)

	# Add series identifiers and extra headers
	m3d_series_uid = m3d_series_uid or generate_uid()
	sx_hdata[DCMHEADER_SERIES_INSTANCE_UID] = m3d_series_uid
	sx_hdata[DCMHEADER_SERIES_NUMBER] = m3d_series_number
	sx_hdata.update(m3d_series_headers or {})

	# Create series timestamp
	sx_ts = datetime.datetime.now()
	sx_hdata[DCMHEADER_SERIES_DATE] = sx_ts.strftime(DCM_DATE_STRFORMAT)
	sx_hdata[DCMHEADER_SERIES_TIME] = sx_ts.strftime(DCM_TIME_STRFORMAT)

	# Reference SOP class UID for the series
	sx_hdata[DCMHEADER_SR_SOP_CLASS_UID] = ref_sopclass_uid or sx.slices_collection[0].sop_class_uid

	for i, (mlabel, model_stream) in enumerate(m3d_models.items()):

		# Create copy of series headers and add instance specific elements
		model_hdata = copy.copy(sx_hdata)
		model_hdata[DCMHEADER_SR_REF_SERIES_SEQ] = Sequence([dcm_encode_series_ref(sx)])

		# Instance identifiers and headers (unique to the model)
		model_hdata[DCMHEADER_INSTANCE_NUMBER] = stl_instance_numbers.get(mlabel) or i+1
		model_hdata.update((m3d_instance_headers or {}).get(mlabel, {}))

		# If no content description provided, add label to DICOM
		if model_hdata.get(DCMHEADER_CONTENT_DESCRIPTION) is None:
			model_hdata[DCMHEADER_CONTENT_DESCRIPTION] = mlabel

		# Create timestamp for content date/time
		model_ts = datetime.datetime.now()
		model_hdata[DCMHEADER_CONTENT_DATE] = model_ts.strftime(DCM_DATE_STRFORMAT)
		model_hdata[DCMHEADER_CONTENT_TIME] = model_ts.strftime(DCM_TIME_STRFORMAT)
		
		# Display color
		if stl_file_colors.get(mlabel):
			model_hdata[DCMHEADER_RECOMMENDED_DISPLAY_COLOR_VALUE] = list(
				CIELabColor(*rgb2cielab(hex2rgb(stl_file_colors.get(mlabel)))).value)

		# Encode file to DICOM
		dcm_model = dcm_encode_stl(model_hdata, model_stream)

		# Add private headers
		if privateheaders_blocks:

			# Iterate through private headers block
			for _blk in privateheaders_blocks:

				if len(_blk) != 2:
					raise ValueError(('Invalid private headers block: "%s". Blocks must be composed of two elements, '
						+ 'the block prefix (as an integer) and the header label.') % str(blk))

				blk = dcm_model.private_block(_blk[0], _blk[1], create=True)
				if callable(privateheaders_callback):
					privateheaders_callback(visblk, mlabel, dcm_model)

		# Invoke presave callback
		if callable(presave_callback):
			presave_callback(mlabel, dcm_model)

		# Create stream
		dcm_model_stream = BytesIO()
		dcm_model.save_as(dcm_model_stream)
		dcm_model_stream.seek(0)

		# Upload to Sonador
		if not dry_run:
			sx.pacs.upload_image(dcm_model_stream)
			logger.info('patient=%s series=%s/%d: DCM upload for STL model "%s" successful' 
				  % (sx.model_patient.patientid, sx.pk, i+1, mlabel))
		else:
			logger.info('Dry Run: patient=%s series=%s/%d: DCM STL model "%s" processed successfully'
				% (sx.model_patient.patientid, sx.pk, i+1, mlabel))
			logger.debug('DICOM for patient=%s series=%s/%d model="%s"\n%s' % (
				sx.model_patient.patientid, sx.pk, i+1, mlabel, str(dcm_model)
			))

	# Add study metadata to the hcache (if not already present)
	if not hcache.get(sx.parent.hmeta_key):
		hcache[sx.parent.hmeta_key] = sx.parent.hmeta

	# Add series metadata to the hcache
	hmeta_sx = DicomMetaKey(IMAGING_SERVER_RESOURCE_SERIES, DCMHEADER_SERIES_INSTANCE_UID, m3d_series_uid)
	hcache[hmeta_sx] = DicomMeta(
		sx_hdata.get(DCMHEADER_SERIES_DESCRIPTION), sx_hdata.get(DCMHEADER_MODALITY), meta=hmeta_sx)

	return hcache