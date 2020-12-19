import six, os, logging, re, traceback, argparse, datetime, requests, shutil
from collections import namedtuple
from six.moves.urllib import parse as urlparse

from client import apisettings as gcapicodes
from client import auth as guru_auth
from client.utils.urls import build_url
from client.errors import ClientOperationError
from client.utils.format import formerrors2str
from client.utils.conversion import str2bool

from .apisettings import SONADOR_ACCESS_ID, SONADOR_SECRET_KEY, SONADOR_URL, SONADOR_APITOKEN, SONADOR_INTERNAL_DNS

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
	return SonadorServer(sonador_url, access_id=access_id, secret_key=secret_key, apitoken=apitoken,
		internal_dns=internal_dns, **kwargs)


class SonadorServer(object):
	'''	Sonador server client
	'''

	def __init__(self, sonador_url, access_id=None, secret_key=None, apitoken=None, verify=False,
			internal_dns=False):
		'''	Initialize the server instance

			@input sonador_url (str): Full URL to the server instance
			@input access_id (str): API Access ID for the server
			@input secret_key (str): Secret key associated with the specified access ID
		'''
		self.url = sonador_url
		self.urlcomponents = urlparse.urlparse(self.url)
		self.verify = verify
		self.internal_dns = internal_dns

		# Credentials
		self.access_id = access_id
		self.secret_key = secret_key
		
		# Auth: API token and token type
		self.sonador_authdata = None
		self._apitoken = apitoken
		if apitoken:
			self.apitoken_type = API_ACCESS_TOKEN
		else: self.apitoken_type = None

		if not self._apitoken and (not self.access_id or not self.secret_key):
			raise ValueError('Unable to initialize Sonador server connection, invalid auth credentials. '
				+ 'An API token or access ID and secret key must be provided.')

	@property
	def scheme(self):
		return self.urlcomponents.scheme

	@property
	def netloc(self):
		return self.urlcomponents.netloc

	@property
	def apitoken(self):
		if self._apitoken is None and self.sonador_authdata is None:
			self.sonador_authdata = fetch_sonador_session_token(self, verify=self.verify)
			self._apitoken = self.sonador_authdata.get(OAUTH_ACCESS_TOKEN)
			self.apitoken_type = self.sonador_authdata.get(OAUTH_TOKEN_TYPE)

		return self._apitoken

	def sonador_apiurl(self, resource_endpoint, method=None):
		'''	Create a Sonador API URL which includes the parameters (AccessID, Signatures, and expirations)
			required to access a secure resource.
		'''
		# Add API token as a request header (if present)
		if self.apitoken_type == API_ACCESS_TOKEN and self.apitoken:
			return build_url(self.scheme, self.netloc, resource_endpoint)

		# Add optional URL signature components
		url_kwargs = {}
		if method:
			url_kwargs['method'] = method

		return build_url(self.scheme, self.netloc,
			guru_auth.create_signed_url(self.access_id, self.secret_key, resource_endpoint, **url_kwargs))

	def sonador_request_headers(self, headers=None):
		''' Add headers to a Sonador API request. If an API token is used to access Sonador
			resources, the token and corresponding heder are added to the dictionary.

			@input headers (dict, default=empty dict): Dictionary to which the Sonador auth
				headers should be added.

			@returns dict
		'''
		headers = headers or {}

		# Add API token as a request header (if present)
		if self.apitoken_type == API_ACCESS_TOKEN and self.apitoken:
			headers.update({ API_ACCESS_TOKEN: self.apitoken })

		return headers

	def get_imageserver(self, uid, verify=None, imageserver_datamodel_class=None):
		'''	Retrieve data for the specified Imaging/PACS server

			@input uid (str): Sonador UID/pk for the imaging server.
			@input verify (bool, default=server default): Toggles whether SSL certificates
				should be validated as part of the request. If no value is passed, 
				the default setting included in the Sonder server will be used.
		'''
		if imageserver_datamodel_class is None:
			from .servers import SonadorImagingServer
			imageserver_datamodel_class = SonadorImagingServer
		from .remote import fetch_sonador_dataobject
		
		if verify is None:
			verify = self.verify

		return fetch_sonador_dataobject(self, imageserver_datamodel_class, uid, verify=verify)


def request_client_error(msg, r, rdata=None, exception_class=ClientOperationError):
	'''	Raise a ClientOperationError if the provided request was not completed successfully
		
		@input msg (str): Message which should be associated with the error
		@input r (requests.Response): Response object
		@input exception_class (Exception, default=ClientOperationError): Exception class
			which should be used for the error.

		@raises Exception
	'''
	rdata = rdata or {}

	# Attempt to de-serialize response and retrieve server errors
	try: rdata.update(r.json())
	except ValueError as err:
		logger.debug('Unable to serialize response to JSON\n%s' % err)
		logger.debug('Server response body:\n%s' % r.content.decode('utf-8'))

	edetails = {
		gcapicodes.STATUS_CODE: r.status_code,
		gcapicodes.SERVER_RESPONSE: r.content,
	}
	if rdata.get(gcapicodes.ERRORS):
		edetails[gcapicodes.ERRORS] = rdata.get(gcapicodes.ERRORS)

	raise exception_class(msg, http_code=r.status_code, details=edetails)
	

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


# DCM Series Utilities

DCMFileIndex = namedtuple('DCMFileIndex', ('filename', 'prefix', 'number'))
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


def reindex_dcm_images(dcmimage_dir, index_start=1, tmp_prefix='tmp'):
	'''	Scan the images in the provided directory, order them by their index number
		and re-index the filenames to start at the provided start index.

		@input dcmimage_dir (str): Directory in which all image indexes should be shifted.
	'''
	# Create a sorted list of the filenames
	dcmimg_list = sorted(
		filter(lambda v: v is not None, map(dcmimage_index, os.listdir(dcmimage_dir))),
		key=lambda v: v.number)

	# Create a temporary subfolder to prevent name collisions when moving files
	tmp = os.path.join(dcmimage_dir, tmp_prefix)
	if not os.path.exists(tmp):
		os.makedirs(tmp, exist_ok=True)

	# Shift the index of all files in the directory
	for dcm_index in dcmimg_list:

		# Source image path
		spath = os.path.join(dcmimage_dir, dcm_index.filename)
		if os.path.exists(spath):

			# Move to tmp directory
			dpath = os.path.join(tmp, '%s%s' % (dcm_index.prefix, dcm_index.number+index_start))
			shutil.move(spath, dpath)

	# Move images from tmp directory to working directory
	for fname in os.listdir(tmp):
		shutil.move(os.path.join(tmp, fname), os.path.join(dcmimage_dir, fname))

	# Remove tmp
	os.rmdir(tmp)
	