'''	Helper utilities which work with Sonador resource models.
'''
import six, os, logging, re, traceback, argparse, datetime, requests, shutil, zipfile
from collections import namedtuple, OrderedDict
from io import BytesIO
from six.moves.urllib import parse as urlparse

import pydicom
from pydicom.datadict import tag_for_keyword
from pydicom.dataset import Dataset
from pydicom.sequence import Sequence
from highdicom.sr.templates import Code as DcmCode, CodedConcept as DcmCodedConcept

from client import apisettings as gcapicodes
from client import auth as guru_auth
from client.utils.urls import build_url
from client.errors import ClientOperationError
from client.utils.format import formerrors2str, split_camelcase
from client.utils.conversion import str2bool
from client.utils.microservices import server_controloperation_json_response
from client.remote import RemoteServer, request_client_error

from ..apisettings import SONADOR_ACCESS_ID, SONADOR_SECRET_KEY, SONADOR_URL, SONADOR_APITOKEN, SONADOR_INTERNAL_DNS, \
		SONADOR_VERIFY_SSL, \
	DCMHEADER_SERIES_INSTANCE_UID, DCMHEADER_SR_REF_SERIES_SEQ, DCMHEADER_SR_REF_INSTANCE_SEQ, DCMHEADER_SR_REF_SOP_SEQ, \
	DCMHEADER_SOP_CLASS_UID, DCMHEADER_SOP_INSTANCE_UID, DCMHEADER_SR_SOP_CLASS_UID, DCMHEADER_SR_REF_INSTANCE_UID
from ..apisettings.sr import points2array, DCM_CODED_CONCEPT_HEADERS, DCM_CODED_CONCEPT_MAPPING
from ..apisettings.media import DCMEDIA_PDF_MIMETYPE, DCMEDIA_PDF_EXTENSION, \
	DCMEDIA_STL_MIMETPYE, DCMEDIA_STL_EXTENSION, \
	DCMEDIA_OBJ_MIMETYPE, DCMEDIA_OBJ_EXTENSION, DCMEDIA_GLB_MIMETYPE, DCMEDIA_GLB_EXTENSION, \
	DCMEDIA_MTL_MIMETYPE, DCMEDIA_MTL_EXTENSION
from ..apisettings.sr import srcode2dataset
from ..serialization import json_datetime_parser, dcm_str2datetime, DCM_DATETIME_STRFORMAT, DCM_DATETIME_STRFORMAT_ALT1

logger = logging.getLogger(__name__)


OAUTH_TOKEN_RESPONSE_TYPE = 'token'
OAUTH_TOKEN_IDTOKEN_RESPONSE_TYPE = 'id_token token'
OAUTH_ACCESS_TOKEN = 'access_token'
OAUTH_TOKEN_TYPE = 'token_type'
OAUTH_TOKEN_TYPE_BEARER = 'Bearer'
OAUTH_EXPIRATION = 'expires_in'

API_ACCESS_TOKEN = 'api-token'


def fetch_sonador_session_token(sonador_server, verify=False, credentials_endpoint='/visionaire/api/login'):
	'''	Retrieve credentials (session token) from Sonador API login endpoint

		@returns dict
	'''
	r = requests.get(sonador_server.sonador_apiurl(credentials_endpoint), verify=verify,
		headers=sonador_server.sonador_request_headers())

	if not r.ok:
		request_client_error('Unable to retrieve API credentials from Sonador due to a server error.', r)

	return r.json()


def initenv_sonador_server(sonador_url=os.environ.get(SONADOR_URL), 
		access_id=os.environ.get(SONADOR_ACCESS_ID), secret_key=os.environ.get(SONADOR_SECRET_KEY),
		apitoken=os.environ.get(SONADOR_APITOKEN), internal_dns=str2bool(os.environ.get(SONADOR_INTERNAL_DNS)),
		verify_ssl=str2bool(os.environ.get(SONADOR_VERIFY_SSL)), **kwargs):
	''' Initialize Sonador Server connection. The method reads the standard Sonador environment
		variables for default arguments. If the environment variable is not defined, the default 
		for the argument will be None.
	'''
	from ..servers import SonadorServer
	return SonadorServer(sonador_url, access_id=access_id, secret_key=secret_key, apitoken=apitoken,
		verify=verify_ssl, internal_dns=internal_dns, **kwargs)
	

def report_operation_error(err, error_traceback=None, 
		user_message_template='Unable to execute operation due to an error:\n%s'):
	'''	Log operation error

		@input err (Exception): Error to log
		@input error_traceback (str, default=None): Traceback string to be included
			in the logger output.
	'''
	edetails = getattr(err, 'details', {}) or {}

	logger.error(user_message_template % err)

	# Show details reported by the server
	if edetails.get(gcapicodes.ERRORS):
		logger.error('Errors reported by the server:\n\n%s\n' \
			% formerrors2str(edetails.get(gcapicodes.ERRORS), separator='\n'))

	# Show server response
	if edetails.get(gcapicodes.STATUS_CODE) and edetails.get(gcapicodes.SERVER_RESPONSE):
		logger.debug('Response Details\nStatus Code: %r\nServer Response: %r'
			% (six.u(edetails.get(gcapicodes.STATUS_CODE)), six.u(edetails.get(gcapicodes.SERVER_RESPONSE))))

	if error_traceback:
		logger.error('Traceback: %s\n\n%s' % (err, error_traceback))


def argparse_date_type(arg_datestr, date_format='%Y-%m-%d'):
	'''	`argparse` validation method that can be used to parse/validate date arguments from the CLI
	'''
	try: return datetime.datetime.strptime(arg_datestr, date_format)
	except ValueError as err:
		raise argparse.ArgumentTypeError('Invalid date: %s. Expected format: %s.' % (arg_datestr, date_format))


def argparse_datetime_type(arg_datestr, datetime_format='%Y-%m-%d %H:%M:%S'):
	'''	`argparse` validation method that can be used to parse/validate date/time arguments from the CLI
	'''
	try: return datetime.datetime.strptime(arg_datestr, datetime_format)
	except ValueError as err:
		raise argparse.ArgumentTypeError('Invalid date: %s. Expected format: %s.' % (arg_datestr, datetime_format))



# Remote data helper methods

def response2filearchive(r):
	''' Initialize a ZipFile instance from the provided reponse. The response stream used to buffer the data
		is provided as a property via the `raw` pproperty.

		@returns zipfile.ZipFile
	'''
	# Initialize file archive from request data, attach the raw the content of the request
	zbuffer = BytesIO(r.content)
	farchive = zipfile.ZipFile(zbuffer, mode='r')
	setattr(farchive, 'raw', zbuffer)

	return farchive


# DCM Series Utilities: Provides methos to re-order/re-number images on the local disk.

DCMFileIndex = namedtuple('DCMFileIndex', ('filename', 'prefix', 'number'))
DCMSliceLocation = namedtuple('DCMSliceLocation', ('filename', 'instance', 'number', 'location', 'prefix', 'filenum'))
DCMIMAGE_RE = re.compile(r'^(?P<prefix>[A-Za-z]+)(?P<number>\d+)$')


def dcmimage_index(fname, pattern=DCMIMAGE_RE):
	'''	Split the provided file name into the file prefix and file index.

		@input fname: File name from which to extract the file prefix
			and the index number.
		
		@returns DCMFileIndex instance (or None) if a match can't be made
	'''
	fmatch = pattern.match(fname)
	if fmatch:
		return DCMFileIndex(fname, fmatch.group('prefix'), int(fmatch.group('number')))

	return None


def dcmimage_slicelocation(fpath, pattern=DCMIMAGE_RE):
	'''	Load the provided file and retrieve the slice location, instance number, file prefix, 
		and number in the file name.

		@input fpath (str): full path to the file
	'''
	# Split to folder and file name, determine if the file matches the expected DICOM pattern
	dcm_folder, fname = os.path.split(fpath)
	fmatch = pattern.match(fname)
	
	# Load the file and retrieve the instance number, slice location, file prefix, and file number
	if fmatch:	
		dcm = pydicom.dcmread(fpath)
		if hasattr(dcm, 'SliceLocation'):
			return DCMSliceLocation(fname, dcm, int(dcm.InstanceNumber), float(dcm.SliceLocation),
				fmatch.group('prefix'), int(fmatch.group('number')))

	return None


def reindex_dcm_images(dcmimage_dir, dcmimg_list, index_start=0, tmp_prefix='tmp'):
	'''	Scan the images in the provided directory, order them by their index number
		and re-index the filenames to start at the provided start index.

		@input dcmimage_dir (str): Directory in which all image indexes should be shifted.
	'''
	# Create a temporary subfolder to prevent name collisions when moving files
	tmp = os.path.join(dcmimage_dir, tmp_prefix)
	if not os.path.exists(tmp):
		os.makedirs(tmp, exist_ok=True)

	# Shift the index of all files in the directory
	for i, j in enumerate(range(index_start, len(dcmimg_list)+index_start)):
		dcm_index = dcmimg_list[i]

		# Source image path
		spath = os.path.join(dcmimage_dir, dcm_index.filename)
		if os.path.exists(spath):

			# Move to tmp directory
			dpath = os.path.join(tmp, '%s%s' % (dcm_index.prefix, j))
			shutil.move(spath, dpath)

	# Move images from tmp directory to working directory
	for fname in os.listdir(tmp):
		shutil.move(os.path.join(tmp, fname), os.path.join(dcmimage_dir, fname))

	# Remove tmp
	os.rmdir(tmp)


def filesort_fileindex(dcmimage_dir, indexfn=dcmimage_index):
	'''	Returns a sorted list of file index objects sorted by their file number.
		The file number is parsed from the image name and may differ from the instance number.
	'''
	# Create a sorted list of the filenames
	return sorted(
		filter(lambda v: v is not None, map(indexfn, os.listdir(dcmimage_dir))),
		key=lambda v: v.number)


def filesort_slicelocation(dcmimage_dir, indexfn=dcmimage_slicelocation):
	'''	Returns a list of file index objects sorted by their slice location.
	'''
	return sorted(
		filter(lambda v: v is not None, map(indexfn, [os.path.join(dcmimage_dir, fname) for fname in os.listdir(dcmimage_dir)])),
		key=lambda v: v.location, reverse=True)


def reindex_fileindex_shift(dcmimage_dir, index_start=0, tmp_prefix='tmp', indexfn=dcmimage_index):
	'''	Scan the images in the provided directory, order them by their file number (which is used
		as the image index). Re-index the filenames to start at the provided start index.

		@input dcmimage_dir (str): Directory in which all image indexes should be shifted
	'''
	dcm_list = filesort_fileindex(dcmimage_dir, indexfn=indexfn)

	# Re-index/re-order the images
	reindex_dcm_images(dcmimage_dir, dcmimg_list, index_start=index_start, tmp_prefix=tmp_prefix)
	

def reindex_slicelocation(dcmimage_dir, index_start=0, tmp_prefix='tmp', indexfn=dcmimage_slicelocation):
	'''	Scan the images in the provided directory, order them by the value of their 
	'''
	# Create a sorted list of the filenames
	dcmimg_list = filesort_slicelocation(dcmimage_dir, indexfn=indexfn)

	# Re-index/re-order the images
	reindex_dcm_images(dcmimage_dir, dcmimg_list, index_start=index_start, tmp_prefix=tmp_prefix)


def dcm_encode_instance_ref(instance, dcm_mdata=None):
	'''	Create a DICOM ReferencedInstanceSequence element for the provided instance.

		@input instance (sonador.imaging.orthanc.base.DcmInstance):
			instance for which the instance reference should be created.
		@input dcm_mdata (pydicom.dataset.Dataset, default=blank dataset instance):
			dataset to which the reference should be added

		@returns pydicom.dataset.Dataset
	'''
	from ..imaging.orthanc.base import DcmInstance

	# Ensure that the provided instance is an Orthanc imaging instance
	if not isinstance(instance, DcmInstance):
		raise ValueError('Unable to create reference for the instance, input must be '
			+ 'a DcmInstance.')

	# Initialize dataset and add instance class and UID references
	dcm_mdata = dcm_mdata or Dataset()
	setattr(dcm_mdata, DCMHEADER_SR_SOP_CLASS_UID, instance.sop_class_uid)
	setattr(dcm_mdata, DCMHEADER_SR_REF_INSTANCE_UID, instance.sop_instance_uid)

	return dcm_mdata


def dcm_encode_series_ref(series, dcm_mdata=None):
	'''	Create a DICOM ReferencedSeriesSequence for the provided series and associated instances.

		@input series (sonador.imaging.orthanc.base.ImagingSeries): series for which the series
			reference should be created.
		@input dcm_mdata (pydicom.dataset.Dataset, default=blank dataset instance):
			dataset to which the references should be added

		@returns pydicom.dataset.Dataset
	'''
	from ..imaging.orthanc.base import ImagingSeries

	# Ensure that the provided series is an Orthanc imaging series
	if not isinstance(series, ImagingSeries):
		raise ValueError('Unable to create reference series for series, reference series must be '
			+ 'an ImagingSeries instance.')

	# Initialize dataset and add series
	dcm_mdata = dcm_mdata or Dataset()
	setattr(dcm_mdata, DCMHEADER_SERIES_INSTANCE_UID, series.series_uid)
	setattr(dcm_mdata, DCMHEADER_SR_REF_INSTANCE_SEQ, Sequence(
		[dcm_encode_instance_ref(dcm) for dcm in series.slices_collection]))

	return dcm_mdata



DCM_MIMETYPE_EXTENSION_MAPPING = {
	DCMEDIA_PDF_MIMETYPE: DCMEDIA_PDF_EXTENSION,
	DCMEDIA_STL_MIMETPYE: DCMEDIA_STL_EXTENSION,
	DCMEDIA_OBJ_MIMETYPE: DCMEDIA_OBJ_EXTENSION,
	DCMEDIA_GLB_MIMETYPE: DCMEDIA_GLB_EXTENSION,
	DCMEDIA_MTL_MIMETYPE: DCMEDIA_MTL_EXTENSION,
}

def dcm_mimetype_guess_extension(mtype):
	'''	Guess the extension for a file from its MIME type. The return value is a
		filename extension, including the leading dot (`.`). If no extension can be 
		guessed for the provied mimetype, None is returned.

		@input mtype (str): MIME type for which the file extension should be guessed

		@returns str or None
	'''
	return DCM_MIMETYPE_EXTENSION_MAPPING.get(mtype)


def dcm_datetime2rangestr(ts_start=None, ts_stop=None, ts_format=DCM_DATETIME_STRFORMAT_ALT1):
	''' Convert the provided date/time components to a date/time range string. DICOM 
		supports three types of range strings:

		* start from (open ended range): "{start}-"
		* end on (open ended range): "-{end}"
		* date range: "{start}-{end}"

		@returns str
	'''
	# Ensure that the inputs are date/time strings
	if ts_start and isinstance(ts_start, str):
		ts_start = dcm_str2datetime(ts_start)
	if ts_stop and isinstance(ts_stop, str):
		ts_stop = dcm_str2datetime(ts_stop)

	# Ensure that the start date is before the stop date
	if ts_start and ts_stop and ts_start > ts_stop:
		raise ValueError(
			'Invalid date range (start="%s", stop="%s"). Start date must be before end date.' % (ts_start, ts_stop))

	# Date range
	if ts_start and ts_stop:
		return '%s-%s' % (ts_start.strftime(ts_format), ts_stop.strftime(ts_format))

	# Start from
	elif ts_start and ts_stop is None:
		return '%s-' % ts_start.strftime(ts_format)

	# End on
	elif ts_stop and ts_start is None:
		return '-%s' % ts_stop.strftime(ts_format)

	raise ValueError('Invalid date range: start=%s stop=%s' % (ts_start, ts_stop))


# DICOM Tag Helper Methods

def dcm_tag2label(header, sep=' ', **kwargs):
	'''	Convert the provided header name to a space separated label	
	'''
	return sep.join(split_camelcase(header, **kwargs))


def dcm_tag2hexcode(header):
	'''	Convert the provided header value to the associated DICOM tag hexcode

		@returns tuple or None if a tag matching the header cannot be found
	'''
	dcm_int = tag_for_keyword(header)

	# Convert integer representation returned by pydicom to hexcode
	if dcm_int:
		dcm_hstr = hex(dcm_int).replace('0x', '00')
		return dcm_hstr[:4], dcm_hstr[4:]

	# Unable to retrieve valid tag for the provided header, return None
	return	