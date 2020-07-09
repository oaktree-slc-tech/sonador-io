import six, logging, traceback, argparse, datetime, requests
from six.moves.urllib import parse as urlparse

from client import apisettings as gcapicodes
from client import auth as guru_auth
from client.utils.urls import build_url
from client.errors import ClientOperationError
from client.utils.format import formerrors2str

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



class SonadorServer(object):

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
