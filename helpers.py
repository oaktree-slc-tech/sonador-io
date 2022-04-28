'''	Helper utilities which work with Sonador resource models.
'''
import six, os, logging, re, traceback, argparse, datetime, requests, shutil
from collections import namedtuple, OrderedDict
from six.moves.urllib import parse as urlparse
import pydicom

from client import apisettings as gcapicodes
from client import auth as guru_auth
from client.utils.urls import build_url
from client.errors import ClientOperationError
from client.utils.format import formerrors2str
from client.utils.conversion import str2bool
from client.utils.microservices import server_controloperation_json_response
from client.remote import RemoteServer, request_client_error

from .apisettings import SONADOR_ACCESS_ID, SONADOR_SECRET_KEY, SONADOR_URL, SONADOR_APITOKEN, SONADOR_INTERNAL_DNS
from .serialization import json_datetime_parser

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
		**kwargs):
	''' Initialize Sonador Server connection. The method reads the standard Sonador environment
		variables for default arguments. If the environment variable is not defined, the default 
		for the argument will be None.
	'''
	from .servers import SonadorServer
	return SonadorServer(sonador_url, access_id=access_id, secret_key=secret_key, apitoken=apitoken,
		internal_dns=internal_dns, **kwargs)
	

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

