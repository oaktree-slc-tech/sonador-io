import six, requests, json, csv, collections, logging, posixpath, zipfile, time
from warnings import warn

from urllib.parse import urlencode
from pprint import pprint
from io import BytesIO

from typing import List, Union

from tabulate import tabulate
from collections import OrderedDict
from collections.abc import Iterable

from highdicom.sr import CodedConcept

from client import apisettings as gcapicodes
from client import auth as guru_auth
from client.utils.urls import build_url
from client.utils.object import pick, omit
from client.utils.microservices import RemotePage, server_controloperation_json_response
from client.utils.format import formerrors2str, ERRORS_ALL
from client.utils.conversion import str2bool
from client.errors import ClientOperationError, ConfigurationError
from client.remote import RemoteServer, request_client_error

from ..apisettings import  DicomMetaKey, DicomHeaderData, \
	IMAGING_SERVER_RESOURCE_PATIENT, IMAGING_SERVER_RESOURCE_STUDY, \
	IMAGING_SERVER_RESOURCE_SERIES, IMAGING_SERVER_RESOURCE_IMAGE, IMAGING_SERVER_RESOURCE_SUPPORTED, \
	DCMHEADER_MODALITY, DCM_MODALITY_SR, DCM_MODALITY_SEG, DCM_VERSION_2021b, DICOM_VR_DESCRIPTION
from ..serialization import json_datetime_parser
from ..helpers import request_client_error, fetch_sonador_session_token, API_ACCESS_TOKEN, OAUTH_TOKEN_RESPONSE_TYPE, \
	OAUTH_TOKEN_IDTOKEN_RESPONSE_TYPE, OAUTH_ACCESS_TOKEN, OAUTH_TOKEN_TYPE, OAUTH_TOKEN_TYPE_BEARER, OAUTH_EXPIRATION
from ..remote import SonadorBaseObject, SonadorObjectCollection, \
	fetch_sonador_data_collection, fetch_sonador_dataobject, \
	sonador_dataobject_create, sonador_dataobject_update
from ..errors import only_duplicate_resource_error

from .base import OrthancServerBase, ImagingServerChildBaseObject, ImagingServerChildCollection, OrthancServerAuthDataCollectionMixin
from .dicom import ImagingServerModalityMixin, DicomImagingModality, DicomImagingModalityCollection, \
	RemoteDICOMwebServer, RemoteDICOMwebServerCollection
from .auth import SonadorGroupAccessControlListCollection

logger = logging.getLogger(__name__)



# Sonador Server

IMAGING_SERVER_OUTPUT_COLUMNS = OrderedDict((
		('pk', 'ID'),
		('name', 'Imaging Server Name'),
		('default', 'Default Server'),
		('hostname', 'Hostname'),
		('port', 'Port'),
		('description', 'Description'),
	))


class SonadorServer(RemoteServer):
	'''	Sonador server client

		@property localenv (key/value pairs, default=empty dict): key/value pairs 
			which provide information about the local environment where the Sonador server instance
			was initilized. 
	'''

	def __init__(self, sonador_url, access_id=None, secret_key=None, apitoken=None, verify=False,
			internal_dns=False, localenv=None, sonador_authdata=None, apitoken_type=None):
		'''	Initialize the server instance

			@input sonador_url (str): Full URL to the server instance
			@input access_id (str): API Access ID for the server
			@input secret_key (str): Secret key associated with the specified access ID
			@input localenv (key/value pairs, default=empty dict): key/value pairs
				which provide information about the local environment where the Sonador server instance
				was initialized.
		'''		
		self.internal_dns = internal_dns
		
		# Auth: API token and token type
		self.sonador_authdata = sonador_authdata

		# Local environment variables for the Sonador Server instance.
		self.localenv = localenv or {}

		# Initialize parent class
		super().__init__(sonador_url, access_id=access_id, secret_key=secret_key, 
			apitoken=apitoken, apitoken_type=apitoken_type, verify=verify)

	def with_credentials(self, **kwargs):
		return super().with_credentials(internal_dns=self.internal_dns, **kwargs)

	@property
	def apitoken(self):
		if self._apitoken is None and self.sonador_authdata is None:
			self.sonador_authdata = self.get_session_token(verify=self.verify)
			self._apitoken = self.sonador_authdata.get(OAUTH_ACCESS_TOKEN)
			self.apitoken_type = self.sonador_authdata.get(OAUTH_TOKEN_TYPE)

		return self._apitoken

	def apiurl(self, resource_endpoint, method=None):
		'''	Create a Sonador API URL which includes the parameters (AccessID, Signatures, and expirations)
			required to access a secure resource.
		'''
		# Add API token as a request header (if present)
		if self.apitoken_type in (API_ACCESS_TOKEN, OAUTH_TOKEN_TYPE_BEARER) and self.apitoken:
			return build_url(self.scheme, self.netloc, resource_endpoint)

		# Add optional URL signature components
		url_kwargs = {}
		if method:
			url_kwargs['method'] = method

		return build_url(self.scheme, self.netloc,
			guru_auth.create_signed_url(self.access_id, self.secret_key, resource_endpoint, **url_kwargs))

	def request_headers(self, headers=None, **kwargs):
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

		# Add Bearer token to Authorization header (if present)
		elif self.apitoken_type == OAUTH_TOKEN_TYPE_BEARER and self.apitoken:
			headers.update({ gcapicodes.AUTH.title(): '%s %s' % (OAUTH_TOKEN_TYPE_BEARER, self.apitoken) })

		return headers

	def sonador_apiurl(self, *args, **kwargs):
		'''	DEPRECATED: Compatibility method kept for code which uses the Sonador client. Use apiurl instead.
		'''
		return self.apiurl(*args, **kwargs)

	def sonador_request_headers(self, *args, **kwargs):
		'''	DEPRECATED: Compatibility method kept for code which uses the Sonador client. Use request_headers instead.
		'''
		return self.request_headers(*args, **kwargs)

	def verify_ssl(self, *args, verify=None, **kwargs):
		'''	Reads the provided keyword arguments and determines the correct value for the `verify` argument
			of remote callable functions. If verify is provided as None, the verify SSL value from the server
			instance is used as a default.

			@returns bool: True if SSL connections should be validated
		'''
		if verify is None:
			verify = self.verify

		return verify

	@property
	def imageserver_datacollection_class(self):
		return SonadorImagingServerCollection

	def get_imageserver(self, uid, imageserver_datamodel_class=None, **kwargs):
		'''	Retrieve model data for the specified Imaging/PACS server

			@input uid (str): Sonador UID/pk for the imaging server.
			@input verify (bool, default=server default): Toggles whether SSL certificates
				should be validated as part of the request. If no value is passed, 
				the default setting included in the Sonador server will be used.
			
			@returns SonadorImagingServer model instance
		'''		
		if imageserver_datamodel_class is None:
			imageserver_datamodel_class = self.imageserver_datacollection_class.model

		return fetch_sonador_dataobject(self, imageserver_datamodel_class, uid, verify=self.verify_ssl(**kwargs), 
			**omit(kwargs, ('verify',)))

	def imageserver_modelinstance_from_json(self, jdata, **kwargs):
		'''	Initialize Imaging/PACS server from JSON data
		'''
		return self._init_dataclass(
			self.imageserver_datacollection_class.model, jdata, **kwargs)

	def get_gateway(self, uid, gateway_datamodel_class=None, **kwargs):
		'''	Retrieve model data for the specified Clinical Gateway

			@input uid (str): Sonador UID/pk for the clinical gateway
			@input verify (bool, default=server default): Toggles whether SSL certificates
				should be validated as part of the request. If no value is passed
				the default setting included in the Sonador server will be used.

			@returns ClinicalGateway model instace
		'''
		if gateway_datamodel_class is None:
			from .devices import ClinicalGateway
			gateway_datamodel_class = ClinicalGateway

		return fetch_sonador_dataobject(self, gateway_datamodel_class, uid, verify=self.verify_ssl(**kwargs),
			**omit(kwargs, ('verify', )))

	def get_dataservice(self, uid, dataservice_datamodel_class=None, **kwargs):
		'''	Retrieve model data for the specified Data Service

			@input uid (str): Sonador UID/pk for the data service
			@input verify (bool, default=server default): Toggles whether SSL certificates
				should be validated as part of the request. If no value is passed,
				the default setting included in the Sonador server will be used.
			
			@returns DataService model instance
		'''
		from ..remote import fetch_sonador_dataobject
		from ..services import DataService
		
		dataservice_datamodel_class = dataservice_datamodel_class or DataService
		
		return fetch_sonador_dataobject(self, dataservice_datamodel_class, uid, 
			verify=self.verify_ssl(**kwargs), **omit(kwargs, ('verify',)))

	def get_session_token(self, *args, **kwargs):
		'''	Retrieve a session token using the provided acess ID/secret
		'''
		return fetch_sonador_session_token(self, verify=self.verify_ssl(**kwargs))

	def fetch_imageservers(self, *args, imageserver_datacollection_class=None, **kwargs):
		''' Retrieve collection of PACS servers for a given Sonador instance
		'''
		if imageserver_datacollection_class is None:
			imageserver_datacollection_class = self.imageserver_datacollection_class

		return fetch_sonador_data_collection(
			self, imageserver_datacollection_class, *args, verify=self.verify_ssl(**kwargs), **omit(kwargs, ('verify',)))

	@property
	def credential_datacollection_class(self):
		from .auth import SonadorSecureApiCredentialCollection
		return SonadorSecureApiCredentialCollection

	def fetch_user_apiaccess_credentials(self, *args, credential_class=None, **kwargs):
		'''	Retrieve secure API access credentials for the user. (User identity is taken from the
			Sonador server Access ID/secret or active auth token.)
		'''		
		if credential_class is None:
			credential_class = self.credential_datacollection_class

		return fetch_sonador_data_collection(self, credential_class, *args, 
			verify=self.verify_ssl(**kwargs), **omit(kwargs, ('verify',)))

	def create_user_apiaccess_credential(self, object_data=None, credential_class=None, **kwargs):
		'''	Create API access credential for the user
		'''
		if credential_class is None:
			credential_class = self.credential_datacollection_class.model

		return sonador_dataobject_create(self, credential_class, object_data or {}, 
			verify=self.verify_ssl(**kwargs), **omit(kwargs, ('verify',)))

	@property
	def apitoken_datacollection_class(self):
		from  .auth import SonadorApiTokenCollection
		return SonadorApiTokenCollection

	def fetch_user_apitokens(self, object_data=None, credential_class=None, **kwargs):
		'''	Retrieve API access tokens for the user. (User identity is taken from the Sonador server Access ID/secret
			or the active auth token.)
		'''
		if credential_class is None:
			credential_class = self.apitoken_datacollection_class

		return fetch_sonador_data_collection(self, credential_class, verify=self.verify_ssl(**kwargs), 
			**omit(kwargs, ('verify',)))

	def create_user_apitoken(self, object_data=None, credential_class=None, **kwargs):
		'''	Create an API access token for the user
		'''
		if credential_class is None:
			credential_class = self.apitoken_datacollection_class.model
		
		return sonador_dataobject_create(self, credential_class, object_data or {}, \
			verify=self.verify_ssl(**kwargs), **omit(kwargs, ('verify',)))

	@property
	def group_datacollection_class(self):
		from .auth import SonadorGroupCollection
		return SonadorGroupCollection

	def _admin_group_query(self, query, group_datacollection_class=None, **kwargs):
		'''	Submit a group query to Sonador admin endpoint with the provided filter terms.

			@input query (dict): dictionary of terms for the query

			returns instance of group_datacollection_class
		'''
		if group_datacollection_class is None:
			group_datacollection_class = self.group_datacollection_class

		return fetch_sonador_data_collection(self, group_datacollection_class, filters=query, verify=self.verify_ssl(**kwargs))

	def admin_group_lookup(self, group_uids: List[int], group_datacollection_class=None, **kwargs):
		'''	Retrieve the details of the groups specified in the group_uids

			@input group_uids (list of integer user IDs/int): group IDs for the which the details should be retrieved

			@returns collection of group instances
		'''
		if group_datacollection_class is None:
			group_datacollection_class = self.group_datacollection_class

		r = requests.post(self.sonador_apiurl(posixpath.join(group_datacollection_class.model.fetch_endpoint, 'lookup')),
			json={ 'groups': group_uids }, verify=self.verify_ssl(**kwargs), headers=self.sonador_request_headers())

		if not r.ok:
			return request_client_error('Unable to execute group lookup due to an error.', r)

		rdata = server_controloperation_json_response(r)
		return self._init_dataclass_from_json(
			group_datacollection_class, rdata.get('results', []), **omit(kwargs, ('verify',)))
	
	def admin_create_group(self, name, group_datacollection_class=None, attrs=None, fetch_existing=True, **kwargs):
		''' Create a group for users on Sonador server.
		'''
		if group_datacollection_class is None:
			group_datacollection_class = self.group_datacollection_class

		try:

			# Add name to group attributes
			attrs = attrs or {}
			attrs.update({ 'name': name })

			# Create group instance
			_r = sonador_dataobject_create(self, group_datacollection_class.model, attrs, verify=self.verify_ssl(**kwargs), 
				**omit(kwargs, ('verify',)))

			# Retrieve group instance UID
			_uid = _r.get(gcapicodes.OBJECT_DATA, {}).get(group_datacollection_class.model.pk_attr)
			if not _uid:
				raise ValueError('Unable to retrieve group ID from creation request, unable to set attributes')

			# Add id attribute and retrieve instance from Server
			return self.admin_get_group(_uid, group_datacollection_class=group_datacollection_class)

		# If a group instance already exists, return the existing model instance
		except ClientOperationError as err:

			# Attempt to retrieve existing instance of the group
			if fetch_existing and only_duplicate_resource_error(err, field_check='name'):
				_groups = self._admin_group_query({ 'name': name }, group_datacollection_class=group_datacollection_class, **kwargs)
				if len(_groups) == 1:
					return _groups[0]

			# Raise operation error
			raise err
	
	def admin_get_group(self, uid, group_datacollection_class=None, **kwargs):
		'''	Retrieve group instance from the Sonador server. This method requires admin access to the server.
		'''
		if group_datacollection_class is None:
			group_datacollection_class = self.group_datacollection_class

		return fetch_sonador_dataobject(self, group_datacollection_class.model, uid, verify=self.verify_ssl(**kwargs),
			**omit(kwargs, ('verify',)))

	@property
	def user_datacollection_class(self):
		from .auth import SonadorUserCollection
		return SonadorUserCollection

	def _admin_user_query(self, query, user_datacollection_class=None, **kwargs):
		'''	Submit a user query to Sonador admin endpoint with the provided filter terms.

			@input query (dict): dictionary of terms for the query

			@returns instance of user_datacollection_class
		'''
		if user_datacollection_class is None:
			user_datacollection_class = self.user_datacollection_class

		return fetch_sonador_data_collection(self, user_datacollection_class, filters=query, verify=self.verify_ssl(**kwargs))

	def admin_user_lookup(self, user_uids: List[int], user_datacollection_class=None, **kwargs):
		'''	Retrieve the details of the users specified in user_uids

			@input user_uids (list of integer user IDs/int): user IDs for which the details should be retrieved

			@returns collection of user instances
		'''
		if user_datacollection_class is None:
			user_datacollection_class = self.user_datacollection_class

		r = requests.post(self.sonador_apiurl(posixpath.join(user_datacollection_class.model.fetch_endpoint, 'lookup')),
			json={ 'users': user_uids }, verify=self.verify_ssl(**kwargs), headers=self.sonador_request_headers())

		if not r.ok:
			return request_client_error('Unable to execute user lookup due to an error.', r)

		rdata = server_controloperation_json_response(r)
		return self._init_dataclass_from_json(
			user_datacollection_class, rdata.get('results', []), **omit(kwargs, ('verify',)))

	def admin_create_user(self, username, password, is_staff=False, is_superuser=False, 
			user_datacollection_class=None, attrs=None, fetch_existing=True, update_existing=True, **kwargs):
		'''	Create a user account on the Sonador server. User creation is a two-step process.
			The user instance is first created and then the account attributes are set
			via an update request. This method requires admin access to the server.

			@returns user_class instance
		'''
		if user_datacollection_class is None:
			user_datacollection_class = self.user_datacollection_class

		# Create attributes hash
		attrs = attrs or {}
		attrs.update({ 'is_staff': is_staff, 'is_superuser': is_superuser })

		try:

			# Create user instance
			_r = sonador_dataobject_create(self, user_datacollection_class.model, {
					'username': username, 'password1': password, 'password2': password,
				}, verify=self.verify_ssl(**kwargs), **omit(kwargs, ('verify',)))

			# Retrieve user instance UID
			_uid = _r.get(gcapicodes.OBJECT_DATA, {}).get(user_datacollection_class.model.pk_attr)
			if not _uid:
				raise ValueError('Unable to retrieve user ID from creation request, unable to set attributes')

			# Add user attributes and retrieve instance from server
			self.admin_get_user(_uid, user_datacollection_class=user_datacollection_class).update(attrs)
			return self.admin_get_user(_uid, user_datacollection_class=user_datacollection_class)

		# If user instance already exists, return the existing model instance
		except ClientOperationError as err:

			# Attempt to retrieve existing instance of the user
			if fetch_existing and only_duplicate_resource_error(err, field_check='username'):
				_users = self._admin_user_query({ 'username': username }, user_datacollection_class=user_datacollection_class, **kwargs)
				if len(_users) == 1:
					_user = _users[0]

					if update_existing:
						_user.update(attrs)
						_user = self.admin_get_user(_user.pk, user_datacollection_class=user_datacollection_class)

					return _user

			# Raise operation error
			raise err

	def admin_get_user(self, uid, user_datacollection_class=None, **kwargs):
		'''	Retrieve user instance from the Sonador server. This method requires admin access to the server.
		'''
		if user_datacollection_class is None:
			user_datacollection_class = self.user_datacollection_class

		return fetch_sonador_dataobject(self, user_datacollection_class.model, uid, verify=self.verify_ssl(**kwargs),
			**omit(kwargs, ('verify',)))

	def _check_userinstance(self, user, user_datacollection_class=None, **kwargs):
		'''	Ensure that the provided user instance matches the user type for the server
		'''
		if user_datacollection_class is None:
			user_datacollection_class = self.user_datacollection_class

		if not isinstance(user, user_datacollection_class.model):
			raise TypeError('Invalid user instance, must be of type: %s' % user_datacollection_class.model.__name__)

	@property
	def admin_credential_datacollection_class(self):
		from .auth import AdminSonadorSecureApiCredentialCollection
		return AdminSonadorSecureApiCredentialCollection

	def admin_fetch_user_apiaccess_credentials(self, user, credential_class=None, **kwargs):
		'''	Retrieve secure API access credentials for the provided user instance via the Sonador
			admin API.
		'''
		if credential_class is None:
			credential_class = self.admin_credential_datacollection_class

		# Specify data collection endpoint, retrieve the credentials for the provided user
		self._check_userinstance(user, **kwargs)
		kwargs['data_collection_endpoint'] = posixpath.join(user.url, credential_class.model.cred_urlroot)
		return self.fetch_user_apiaccess_credentials(credential_class=credential_class, user=user, **kwargs)

	def admin_create_user_apiaccess_credential(self, user, credential_class=None, **kwargs):
		'''	Create API access credentials for the specified user instance from the Sonador admin API
		'''
		if credential_class is None:
			credential_class = self.admin_credential_datacollection_class.model

		# Specify data creation endpoint, create the credentials for the provided user
		self._check_userinstance(user, **kwargs)
		kwargs['dataobject_endpoint'] = posixpath.join(user.url, credential_class.cred_urlroot)
		return self.create_user_apiaccess_credential(credential_class=credential_class, user=user, **kwargs)

	@property
	def admin_apitoken_datacollection_class(self):
		from .auth import AdminSonadorApiTokenCollection
		return AdminSonadorApiTokenCollection

	def admin_fetch_user_apitokens(self, user, credential_class=None, **kwargs):
		'''	Retrieve API access tokens for the provided user via the admin API.
		'''
		if credential_class is None:
			credential_class = self.admin_apitoken_datacollection_class

		# Specify data collection ednpoint, retrieve the API token instances for the provided user
		self._check_userinstance(user, **kwargs)
		kwargs['data_collection_endpoint'] = posixpath.join(user.url, credential_class.model.cred_urlroot)
		return self.fetch_user_apitokens(credential_class=credential_class, user=user, **kwargs)

	def admin_create_user_apitoken(self, user, credential_class=None, **kwargs):
		'''	Create API access token for the provided user via the admin API.
		'''
		if credential_class is None:
			credential_class = self.admin_apitoken_datacollection_class.model

		# Specify data creation endpoint, create the credentials for the provided user
		self._check_userinstance(user, **kwargs)
		kwargs['dataobject_endpoint'] = posixpath.join(user.url, credential_class.cred_urlroot)
		return self.create_user_apitoken(credential_class=credential_class, user=user, **kwargs)

	def admin_verify_user_credentials(self, token_key, token_value, **kwargs):
		'''	Verify the provided token key and value using the Sonador global endpoint.
			If valid, a copy of the full user context (profile and group membership) will be
			part of the response.

			@returns response-like object
		'''
		r = requests.post(self.sonador_apiurl('/visionaire/api/user/introspect/profile'),
			json={ 'token_key': token_key, 'token_value': token_value, },
			verify=self.verify_ssl(**kwargs), headers=self.sonador_request_headers())

		if not r.ok:
			request_client_error('Unable to verify Sonador API credentials due to an error.', r)

		return server_controloperation_json_response(r)



# Imaging Server

class SonadorImagingServer(OrthancServerAuthDataCollectionMixin, OrthancServerBase):
	'''	Object representation of a Sonador imaging server
	'''
	fetch_endpoint = '/visionaire/api/pacs'
	tabulate_output_columns = IMAGING_SERVER_OUTPUT_COLUMNS
	tools_endpoint = 'tools'
	tools_secure_find_endpoint = 'tools/secure-find'

	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)
		self.init_auth_collection_mixin(*args, **kwargs)

	def _request_get(self, resource_endpoint, error_msg=None, headers=None, **kwargs):
		''' Send a GET request to the imaging server. Raises an exception with the provided error message
			if the request could not be completed successfully.

			@input resource_endpoint (str): Resource endpoint to which the request should be sent.
			@input error_msg (str or callable): Error message (or callable function) to be triggered in the 
				case of a failed request.

			@returns request.Response or JSON object (dict/array)
		'''
		r = requests.get(resource_endpoint, headers=headers, verify=self.server.verify_ssl(**kwargs), 
			**omit(kwargs, ('verify',)))
		if not r.ok:

			# Custom error message
			if error_msg:
				if callable(error_msg): error_msg(r)
				else: request_client_error(error_msg, r)

			else:
				request_client_error('Unable to execute GET request to %s due to an error. Status code: %s'  % (
					resource_endpoint, r.status_code,
				), r)

		return r

	def _request_post(self, resource_endpoint, error_msg=None, headers=None, **kwargs):
		'''	Send a POST request to the imaging server. Raises an exception with the provided error message
			if the request could not be completed successfully.

			@returns request.Response or JSON object (dict/array)
		'''
		r = requests.post(resource_endpoint, headers=headers, verify=self.server.verify_ssl(**kwargs), 
				**omit(kwargs, ('verify',)))
		if not r.ok:

			# Custom error message
			if error_msg:
				if callable(error_msg): error_msg(r)
				else: request_client_error(error_msg, r)

			else:
				request_client_error('Unable to execute POST request to %s due to an error. Status code: %s'  % (
					resource_endpoint, r.status_code,
				), r)

		return r

	def _request_delete(self, resource_endpoint, error_msg=None, headers=None, **kwargs):
		'''	Send a DELETE request to the imaging server. Raises an exception with the provided error message
			if the request could not be completed successfully.

			@returns request.Response or JSON object (dict/array)
		'''
		r = requests.delete(resource_endpoint, headers=headers, verify=self.server.verify_ssl(**kwargs), 
				**omit(kwargs, ('verify',)))
		if not r.ok:

			# Custom error message
			if error_msg:
				if callable(error_msg): error_msg(r)
				else: request_client_error(error_msg, r)

			else:
				request_client_error('Unable to execute DELETE request to %s due to an error. Status code: %s'  % (
					resource_endpoint, r.status_code,
				), r)

		return r

	def _request_put(self, resource_endpoint, error_msg=None, headers=None, verify=None, **kwargs):
		'''	Send a PUT request to the imaging server. Raises an exception with the provided error message
			if the request could not be completed successfully.

			@returns request.Response or JSON object (dict/array)
		'''
		if verify is None:
			verify= self.server.verify

		r = requests.put(resource_endpoint, headers=headers, verify=self.verify_ssl(**kwargs), 
				**omit(kwargs, ('verify',)))
		if not r.ok:

			# Custom error message
			if error_msg:
				if callable(error_msg): error_msg(r)
				else: request_client_error(error_msg, r)

			else:
				request_client_error('Unable to execute PUT request to %s due to an error. Status code: %s'  % (
					resource_endpoint, r.status_code,
				), r)

		return r

	def _orthanc_group_datamodel_class(self, orthanc_group_datamodel_class=None, **kwargs):
		'''	Retrieve the Orthanc group data model class to be used for operations
		'''
		if orthanc_group_datamodel_class is None:
			orthanc_group_datamodel_class = self.orthanc_group_datamodel_class

		return orthanc_group_datamodel_class

	def __init__(self, *args, **kwargs):

		# Cache to be used when fetching resources
		super().__init__(*args, **kwargs)

	def query_url(self, resource_modelcollection_class, secure_find=True, rapid_lookup=True, order_by=None, *args, **kwargs):
		'''	Retrieve the URL which should be used for resource queries:
			
			Cloud query URLs:
			* /tools/secure-find: ACL mediated (unified) query endpoint. Provides
				the same set of functionality and features as the Sonador resource
				cache endpoints (see below).
			* /cache/{resource-type} (DEPRECATED): query endpoint provided by the
				Sonador resource cache which provides additional functionality
				beyond the built-in (database) API of Orthanc. Requires administrative access.
			* /tools/find: build-in (database) interface provided by Orthanc. Requires administrative access.

			@input secure_find (bool or None, default=True): Utilize the /tools/secure-find 
				endpoint for resource queries. /tools/secure-find is a cache-enabled, 
				ACL mediated endpoint that provides the same API as the internal /tools/find, 
				and is equivalent to the Sonador cache resource specific endpoint.
			@input rapid_lookup (bool or None, default=None): DEPRECATED. Use the Orthanc/Sonador 
				cache API to perform queries. (The resource cache is a retrieved from a REST 
				endpoint and is distinct from the local image server cache.)
				Cache API queries are faster than the `/tools/find` but are "eventually 
				consistent" and may return different results than the traditional endpoint.
				True will use resource cache endpoints and indicate that linked resources 
				should also cache endpoints when calling query methods. False will set a strong 
				preference against use of the cache (also propagates to linked resources),
				None will avoid use of cache endpoints but does not propagate to child resources.

			@returns str: query endpoint

			Using `secure_find=False` and `rapid_lookup=True` will cause the cache
			endpoint to be used. To direct requests to `/tools/find`, both `secure_find` 
			and `rapid_lookup` must be false.
		'''
		# Deprecation warning for the use of RapidLookup by itself
		if not secure_find and rapid_lookup:
			warn('The use of `rapid_lookup=True` via Sonador resource cache endpoints is deprecated, use `/tools/secure-find` '
				+ '(`secure_find=True` option) instead.', DeprecationWarning, stacklevel=2)

		# Use /tools/secure-find
		if secure_find:
			query_endpoint = self.tools_secure_find_endpoint

		# /cache/{resource-type} Sonador resource cache endpoint (requires administrative permissions)
		elif not secure_find and rapid_lookup:
			query_endpoint = resource_modelcollection_class.model.cache_queryurl

		# Database lookup: /tools/find
		else:
			query_endpoint = self.tools_find_endpoint

		return self.orthanc_apiurl(query_endpoint)

	@property
	def modality_datacollection_class(self):
		return DicomImagingModalityCollection

	@property
	def dicomweb_remote_datacollection_class(self):
		return RemoteDICOMwebServerCollection

	@property
	def group_acl_datacollection_class(self, *args, **kwargs):
		'''	Data collection class which should be used by the server base for managing ACL instances.
		'''
		return SonadorGroupAccessControlListCollection

	@property
	def orthanc_group_datamodel_class(self, *args, **kwargs):
		from ..imaging.orthanc.group import OrthancGroup
		return OrthancGroup

	def fetch_acl(self, verify=None, **kwargs):
		'''	Retrieve access control lists associated with the server
		'''
		return fetch_sonador_data_collection(self.server, self.group_acl_datacollection_class, pacs=self, 
			verify=self.server.verify_ssl(**kwargs),
			data_collection_endpoint=posixpath.join(self.fetch_endpoint, str(self.pk), self.group_acl_datacollection_class.model.acl_urlroot),
			**omit(kwargs, ('verify',)))

	@property
	def acl(self):
		'''	Group access control lists associated with the server
		'''
		if getattr(self, '_acl', None) is None:
			setattr(self, '_acl', self.fetch_acl())

		return self._acl

	def get_acl(self, rid, group_acl_datacollection_class=None,  **kwargs):
		'''	Retrieve group access control list
		'''
		if group_acl_datacollection_class is None:
			group_acl_datacollection_class = self.group_acl_datacollection_class

		return fetch_sonador_dataobject(
			self.server, group_acl_datacollection_class.model, rid, pacs=self, verify=self.server.verify_ssl(**kwargs),
			dataobject_endpoint=posixpath.join(
				self.fetch_endpoint, str(self.pk), self.group_acl_datacollection_class.model.acl_urlroot, str(rid)),
			**omit(kwargs, ('verify',)))

	@property
	def internal_netloc(self):
		'''	Return network location for the server (hostname:port)
		'''
		# Retrieve internal hostname/port with fallback to external hostname/port
		hostname = getattr(self, 'internal_hostname', None) or self.hostname
		port = getattr(self, 'internal_port', None) or getattr(self, 'port', None)

		if port:
			return '%s:%s' % (hostname, port)
		
		return hostname
	
	def bulk_anonymize(self, resources: list, asynchronous: bool=False, dicom_version: str=DCM_VERSION_2021b, 
			force: bool=True, keep: list=None, keep_private_tags: bool=False, keep_source: bool=True, 
			permissive: bool=True, priority: int=0, private_creator: str=None, remove: list=None,
			replace: list=None, bulk_anonymize_dict: dict=None, headers=None, verify=None, 
			merge_anonymized: bool= False, **kwargs):
		'''	Start a job that will anonymize all DICOM patients, studies, series, or instances 
			whose identifiers are provided in the Resources field. Anonymization erases all tags specified 
			in Table E.1-1 from PS 3.15 of the DICOM standard. 
			(Refer to http://dicom.nema.org/medical/dicom/current/output/chtml/part15/chapter_E.html#table_E.1-1.)
			 
			@input resources (list): List of the Orthanc identifiers of the patients, studies, series, and instances
				to be anonymized.
			@input asynchronous (bool, default=True): If true, run the job in asynchronous mode, which means that the REST API 
				call will immediately return, reporting the identifier of a job. Prefer this flavor wherever possible.
			@input dicom_version (str, default='2021b'): version of the DICOM standard to be used
				for anonymization.
			@input keep (list, default=None): List of DICOM tags whose value must not be destroyed by the anonymization.
			@input keep_private_tags (bool, default=True): Keep the private tags from the DICOM instances.
			@input keep_source (bool, default=True): If set to false, instructs Orthanc to the remove original resources. 
				By default, the original resources are kept in Orthanc.
			@input force (bool, default=False): Allow the modification of tags related to DICOM identifiers, at the risk of breaking 
				the DICOM model of the real world.
			@input permissive (bool, default=True): If true, ignore errors during the individual steps of the job.
			@input priority (int, default=0): In asynchronous mode, the priority of the job. The lower the value, the higher the priority.
			@input private_creator (str, default=None): The private creator to be used for private tags in Replace.
			@input remove (list, default=None): List of additional tags to be removed from the DICOM instances.
			@input replace (dict, default=None): Associative array to change the value of some DICOM tags in the DICOM instances.	 

			@returns OrthancJob if async is True, request.Response otherwise
		'''
		bulk_anonymize_dict = bulk_anonymize_dict or {}

		if replace and not isinstance(replace, dict):
			raise TypeError('Unable to anonymize DICOM resource, replace terms must be submitted as a dictionary.')
		if remove and not isinstance(remove, Iterable):
			raise TypeError('Unable to remove DICOM tags, remove terms must be submitted as an iterable.')

		# Structure of anonymize request
		bulk_anonymize_dict.update({
			'Asynchronous': asynchronous, 
			'Force': force,
			'KeepSource': keep_source,
			'Permissive': permissive,
			'Priority': priority,
			'KeepPrivateTags': keep_private_tags,
			'Resources': resources,
			'DicomVersion': dicom_version
		})

		# Add options to request
		if replace:
			bulk_anonymize_dict.update({ 'Replace': replace })
		if remove:
			bulk_anonymize_dict.update({ 'Remove': remove })
		if keep:
			bulk_anonymize_dict.update({ 'Keep': keep })
		if private_creator:
			bulk_anonymize_dict.update({ 'PrivateCreator': private_creator })

		# Execute operation
		r = self._bulk_content_request(posixpath.join(self.tools_endpoint, 'bulk-anonymize'),
			bulk_anonymize_dict, headers=headers, verify=verify)
		
		returned_objects = []
		if asynchronous:
			response_json = r.json()
			from ..imaging.orthanc.jobs import OrthancJob
			return self.get_imaging_resource(response_json['ID'], OrthancJob, headers=headers, **kwargs)
		
		else:
			
			# Returns the model object of the new resources created (based on the level executed)
			return r

	def bulk_delete(self, resources: list, headers: dict=None, verify: bool=False, **kwargs) ->dict:
		''' Delete all of the provided DICOM patients, studies, series, and instances whose identifiers.

			@input resources (list): List of the Orthanc identifiers of the patients, studies, series, 
				instances of interest.

			@returns requests.Response
		'''
		if not resources:
			raise ValueError('You must set resources to be deleted')
		
		bulk_delete_dict = {'Resources': resources,}

		# Execute operation
		r = self._bulk_content_request(posixpath.join(self.tools_endpoint, 'bulk-delete'),
			bulk_delete_dict, headers=headers, verify=verify)
		
		return r

	def create_archive(self, resources: list, asynchronous: bool=True, priority: int=0, transcode: str=None, headers: dict=None, 
			verify=None, create_archive_dict: dict=None, **kwargs) -> dict:
		''' Create a zip archive containing the requested DICOM resources (patients, studies, series, and instances).

			@input resources (list): Orthanc UIDs of resources to include in the archive file.
			@input asynchronous (boolean, default=False): 
			@input priority (integer, default=0): In asynchronous mode, the priority of the job. 
				The lower the value, the higher the priority.
			@input transcode(string, default=None): If present, the DICOM files in the archive 
				will be transcoded to the provided transfer syntax: https://book.orthanc-server.com/faq/transcoding.html
			
			@returns OrthancJob if async is True, otherwise zipfile.ZipFile archive.
		'''
		create_archive_dict = create_archive_dict or {}

		# Create request structure
		create_archive_dict.update({ 
			'Asynchronous': asynchronous, 
			'Priority': priority,
			'Resources': resources,
		})

		if transcode:
			create_archive_dict.update({ 
				'Transcode': transcode 
			})

		# Execute operation
		r = self._bulk_content_request(posixpath.join(self.tools_endpoint, 'create-archive'),
			create_archive_dict, headers=headers, verify=verify)

		# Initialize file archive from request data, attach the raw content of the request
		# to the archive
		if asynchronous:
			response_json = r.json()
			from ..imaging.orthanc.jobs import OrthancJob
			return self.get_imaging_resource(response_json['ID'], OrthancJob, headers=headers, **kwargs)
		else:
			zbuffer = BytesIO(r.content)
			farchive = zipfile.ZipFile(zbuffer, mode='r')
			setattr(farchive, 'raw', zbuffer)

		return farchive
	
	def fetch_bulk_content(self, uids: list, full: bool=False, metadata: str=True, resource=None,
			short: bool=False, headers: dict=None, verify=None, bulk_content_dict: dict=None, cache=False, 
			bulk_endpoint=None, rapid_lookup: bool=True, **kwargs) -> dict:
		''' Get the content all the DICOM patients, studies, series or instances whose identifiers are provided in 
			the Resources field, in one single call.

			@input uids (list): Orthanc resource UIDS (pk) of the Orthanc identifiers of the 
				patients/studies/series/instances of interest.
			@input resource (string): Optional argument which specifies the level of interest (can be Patient, Study, 
				Series or Instance). Orthanc will loop over the items inside Resources, and explore upward or 
				downward in the DICOM hierarchy in order to find the level of interest.
			@input full (bool, default=False): If set to true, report the DICOM tags in full format 
				(tags indexed by their hexadecimal format, associated with their symbolic name and their value)
			@input metadata(bool, default=True): If set to true (default value), the metadata 
				associated with the resources will also be retrieved. 
			@input short (bool, default=False): If set to true, report the DICOM tags in hexadecimal format.
			@input bulk_endpoint (str, default='/tools/bulk-content'): bulk endpoint to which the request
				should be sent
		'''	
		bulk_content_dict = bulk_content_dict or {}

		# Create request structure
		bulk_content_dict.update({ 
			'Full': full, 
			'Metadata': metadata,
			'Resources': uids,
			'Short': short,
			'RapidLookup': rapid_lookup,
		})

		if resource:
			bulk_content_dict['Level'] = resource

		# Determine bulk endpoint URL to use
		bulk_endpoint = bulk_endpoint or posixpath.join(self.tools_endpoint, 'bulk-content')

		# Execute operation
		logger.debug('Structure of bulk content request:\n%s' % json.dumps(bulk_content_dict))
		resources_response = self._bulk_content_request(
			posixpath.join(self.tools_endpoint, 'bulk-content'), bulk_content_dict, headers=headers, verify=verify, cache=cache)
		resources = {}

		# Initialize resource model instances
		for rjson in resources_response.json():

			# Create resource type in response dictionary (if it does not already exist)
			if rjson.get('Type') and not rjson.get('Type') in resources:
				resources[rjson.get('Type')] = []

			# Separate resources by type
			resources[rjson['Type']].append(rjson)

		# Initialize collection instances for each resource type
		for rtype, rdata in resources.items():
			if not kwargs.get('pacs'):
				kwargs['pacs'] = self
			resources[rtype] = self.server._init_dataclass_from_json(
				self.get_resource_modelcollection_class(rtype), rdata, **kwargs)

		# Add resources to local cache
		if cache:

			# Iterate through collections and add items to resource cache
			for rtype, rdata in resources.items():
				for resource in rdata:
					self.resource_cache[resource.pk] = resource

		return resources
	
	def _bulk_content_request(self, bulk_content_url, bulk_content_dict: dict, 
			headers=None, verify=None, **kwargs):
		''' Function that wraps the tools endpoint and make requests against it, returning the response.
		'''
		bulk_request = self._request_post(
			self.orthanc_apiurl(bulk_content_url), 
			lambda r: request_client_error(
				'Unable to retrieve/modify DICOM resource on server %s. Status code: %s.' % (self.server_label, r.status_code),
				r),
			json=bulk_content_dict, headers=self.orthanc_request_headers(headers=headers), verify=verify)

		logger.debug('Response from PACS imaging server:\n%s' % bulk_request.content)
		return bulk_request
	
	def bulk_modify(self, resources: list, replace: dict, asynchronous: bool=False, force: bool=False, keep: list=None, 
			keep_sources: list=True, level: str=None, permissive: bool=True, priority: int=0, 
			private_creator: str=None, remove: list=None, remove_private_tags=False, transcode: str=None, 
			bulk_modify_dict: dict=None, headers=None, verify=None, bring_parent: bool=False, **kwargs):
		'''	Start a job that will modify all the DICOM patients, studies, series or instances whose identifiers 
			are provided in the Resources field.
			 
			@input resources (list): List of the Orthanc identifiers of the patients/studies/series/instances of interest.
			@input replace (dict): Associative array to change the value of some DICOM tags in the DICOM instances. 
				Starting with Orthanc 1.9.4, paths to subsequences can be provided using the same syntax 
				as the dcmodify command-line tool (wildcards are supported as well).
			@input asynchronous (bool, default=False): If true, run the job in asynchronous mode, which 
				means that the REST API call will immediately return, reporting the identifier of a job. 
				Prefer this flavor wherever possible.
			@input force (boolean, default=False): Allow the modification of tags related to DICOM 
				identifiers, at the risk of breaking the DICOM model of the real world.
			@input keep (list, default=None): Keep the original value of the specified tags, to be 
				chosen among the StudyInstanceUID, SeriesInstanceUID and SOPInstanceUID tags. Avoid this 
				feature as much as possible, as this breaks the DICOM model of the real world.
			@input keep_sources (bool, default=True): If set to false, instructs Orthanc to the remove 
				original resources. By default, the original resources are kept in Orthanc.
			@input level (str, default=None): Level of the modification (Patient, Study, Series or Instance). 
				If absent, the level defaults to Instance, but is set to Patient if PatientID is modified, 
				to Study if StudyInstanceUID is modified, or to Series if SeriesInstancesUID is modified
			@input permissive (bool, default=True): If true, ignore errors during the individual steps of the job.
			@input priority (int, default=0): In asynchronous mode, the priority of the job. The lower the value, 
				the higher the priority.
			@input private_creator (string, default=None): The private creator to be used for private tags in Replace.
			@input remove (list, default=None): List of tags that must be removed from the DICOM instances. 
				Starting with Orthanc 1.9.4, paths to subsequences can be provided using the same syntax as 
				the dcmodify command-line tool (wildcards are supported as well).
			@input remove_private_tags (bool, default=False): Remove the private tags from the DICOM instances 
				(defaults to false).
			@input transcode (str, default=None): iterable ot tags to be removed outside of those
				specified in the standard.

			@returns request.Response
		'''
		bulk_modify_dict = bulk_modify_dict or {}

		# Create request structure
		bulk_modify_dict.update({ 
			'Asynchronous': asynchronous, 
			'Force': force,
			'KeepSource': keep_sources,
			'Permissive': permissive,
			'Priority': priority,
			'RemovePrivateTags': remove_private_tags,
			'Replace': replace,
			'Resources': resources,
		})

		if keep:
			bulk_modify_dict.update({ 'Keep': keep })
		if level:
			bulk_modify_dict.update({ 'Level': level })
		if private_creator:
			bulk_modify_dict.update({ 'PrivateCreator': private_creator })
		if remove:
			bulk_modify_dict.update({ 'Remove': remove })
		if transcode:
			bulk_modify_dict.update({ 'Transcode': transcode })

		# Execute operation
		logger.debug('Structure of modification request:\n%s' % json.dumps(bulk_modify_dict))
		r = self._request_post(
			self.orthanc_apiurl(posixpath.join(self.tools_endpoint, 'bulk-modify')), 
			lambda r: request_client_error(
				'Unable to modify resources %s on server %s. Status code: %s.' % (resources, self.server_label, r.status_code),
				r),
			json=bulk_modify_dict, headers=self.orthanc_request_headers(headers=headers), verify=verify)

		# Initialize file archive from request data, attach the raw content of the request
		# to the archive
		returned_objects = []
		if asynchronous:
			response_json = r.json()
			from ..imaging.orthanc.jobs import OrthancJob
			return self.get_imaging_resource(response_json['ID'], OrthancJob, headers=headers, **kwargs)
			
		else:
			# Return the model object of the new resources created (based on the level executed)
			response_json = r.json()
			return_instances = {}
			for resource in response_json['Resources']:
				if resource['Type'] not in return_instances:
					return_instances.update({resource['Type']: []})
				
				return_instances[resource['Type']].append(resource['ID'])
				
			returned_objects.append(self.bulk_content(resources=return_instances[level], level=level))
			
			return returned_objects

	@property
	def server_label(self):
		if getattr(self, 'name', None):
			'%s (%s)' % (self.name, self.pk)
		return self.pk

	def orthanc_apiurl(self, resource_endpoint, query_params='', query_lowercase=False):
		'''	Create URL for Orthanc API call
		'''
		if self.server.internal_dns:
			return build_url(self.internal_scheme, self.internal_netloc, resource_endpoint, query_params=query_params, query_lowercase=query_lowercase)

		return super().orthanc_apiurl(resource_endpoint, query_params=query_params, query_lowercase=query_lowercase)

	def orthanc_request_headers(self, headers=None, **kwargs):
		'''	Add headers required by Orthanc API
		'''
		headers = headers or {}
		
		# Add API token and token type as the "Authorization" header.
		# If a token (API or session based) has not yet been populated,
		# it will be retrieved dynamically as part of the first access of the API token
		# property. The token type wiill be populated at that time.
		atoken, atype = self.server.apitoken, self.server.apitoken_type
		headers['Authorization'] = '%s %s' % (atype, atoken)

		return headers

	def update(self, odata, *args, **kwargs):
		''' Update the image server with the provided parameters.

			@input odata (dict): new attributes/values for the image server

			@returns updated SonadorImagingServer instance
		'''
		rdata = super().update(odata, *args, **kwargs)
		return self.server.get_imageserver(self.pk)

	def connection_state(self, *args, **kwargs):
		'''	Retrieve the connection state for the server
		'''
		return self._request_get(
			self.orthanc_apiurl('/system/status'), 
			lambda r: request_client_error(
				'Unable to retrieve connection status for server %s. Status code: %s.' % (self.server_label, r.status_code),
				r),
			headers=self.orthanc_request_headers(**kwargs), **kwargs)	

	def cache_dcm_tags(self, *args, sep=',', **kwargs):
		'''	Retrieve list of DICOM tags configured for the server
		'''
		dcmtags = kwargs.get('dcmtags') or OrderedDict()

		rtags = self._request_get(
			self.orthanc_apiurl('/cache/dcm-tags'),
			lambda r: request_client_error('Unable to retrieve DICOM tags for server %s. Status code: %s.' % (
					self.server_label, r.status_code
				), r),
			headers=self.orthanc_request_headers(**kwargs), **omit(kwargs, ('headers',)))

		# Unpack DICOM tag data
		for rtype, rtags in rtags.json().items():
			for code,dcm in rtags.items():

				# Convert DCM code to tuple (follow pydicom convention)
				_code = tuple(code.split(sep)) if sep in code else code
				if not isinstance(_code, tuple):
					raise ValueError('Invalid DICOM code: %s' % _code)

				# Retrieve VR
				_vr = DICOM_VR_DESCRIPTION.get(dcm.get('vr', {}).get('code'))
				if not _vr:
					raise ValueError('Invalid DICOM VR: %s' % _vr)
				
				dcmtags[_code] = (rtype, DicomHeaderData(dcm.get('tag'), _code, int(_code[1], 16), _vr))

		return dcmtags

	def admin_create_acl(self, group, perms, group_acl_datacollection_class=None, 
			fetch_existing=True, update_existing=True, **kwargs):
		'''	Create ACL policy for the provided group contining the specified permissions

			@input group (SonadorGroup): Sonador group instance for which the policy should be created
			@input perms (dict): permissions which should be applied to the group
			@input fetch_existing (bool, default=True): if a policy exists for the specified group, retrieve
				the existing instance
			@input update_existing (bool, default=True): if a policy exists for the specified group, update
				the permissions of the existing instance to match those provided in perms.

			@returns ACL instance
		'''
		group_datacollection_class = self._group_datacollection_class(**kwargs)
		if group_acl_datacollection_class is None:
			group_acl_datacollection_class = self.group_acl_datacollection_class

		if not isinstance(group, group_datacollection_class.model):
			raise TypeError('Invalid group instance, must be of type %s' % group_datacollection_class.model.__name__)
		if not isinstance(perms, dict):
			raise TypeError('Invalid permissions dict')

		# Create JSON payload for request
		perms['group'] = group.pk

		try:

			# Create ACL instance
			_r = sonador_dataobject_create(self.server, group_acl_datacollection_class, perms, pacs=self, 
				verify=self.server.verify_ssl(**kwargs),
				dataobject_endpoint=posixpath.join(self.fetch_endpoint, self.pk, group_acl_datacollection_class.model.acl_urlroot),
				**omit(kwargs, ('verify', 'group_datacollection_class')))

			# Retrieve ACL instance UID
			_uid = _r.get(gcapicodes.OBJECT_DATA, {}).get(group_acl_datacollection_class.model.pk_attr)
			if not _uid:
				raise ValueError('Unable to retrieve ACL ID from creation request.')

			# Retrieve ACL model instance
			return self.get_acl(_uid)

		except ClientOperationError as err:

			# Check to see if the only error is due to an already existing policy, if so
			# retrieve the existing instance and update.
			if fetch_existing and only_duplicate_resource_error(err):
				_acls = fetch_sonador_data_collection(self.server, group_acl_datacollection_class, pacs=self,
					verify=self.verify_ssl(**kwargs),
					data_collection_endpoint=posixpath.join(self.fetch_endpoint, self.pk, group_acl_datacollection_class.model.acl_urlroot),
					filters={ 'group': group.name })
				if len(_acls) == 1:
					_acl = _acls[0]

					if update_existing:
						_acl.update(perms)
						_acl = self.get_acl(_acl.pk, group_acl_datacollection_class=group_acl_datacollection_class)

					return _acl
			
			# Raise operation error
			raise err

	def admin_verify_user_credentials(self, token_key, token_value, **kwargs):
		'''	Verify the provided token key and value using the imaging server introspection endpoint. If valid, a copy of the
			user context will be provided for the server including the profile and the groups
			that have been authorized for the imaging server instance.

			@returns response-like object
		'''
		r = requests.post(self.server.sonador_apiurl(posixpath.join(self.fetch_endpoint, self.pk, 'user/introspect/profile')),
			json={ 'token_key': token_key, 'token_value': token_value },
			verify=self.server.verify_ssl(**kwargs), headers=self.server.sonador_request_headers())

		if not r.ok:
			request_client_error(
				'Unable to verify Sonador API credentials server="%s" due to an error.' % self.server_label, r)

		return server_controloperation_json_response(r)

	def user_query(self, ufilter, *args, **kwargs):
		'''	Submit a user query to Sonador with the provided filter terms. Search results are only those
			users which have a group policy authorizing access to the imaging server.
		'''
		user_datacollection_class = self._user_datacollection_class(**kwargs)

		r = requests.post(self.server.sonador_apiurl(posixpath.join(self.fetch_endpoint, self.pk, 'user/search')),
			json=ufilter, verify=self.server.verify_ssl(**kwargs), headers=self.server.sonador_request_headers())

		if not r.ok:
			request_client_error('Unable to execute user search query due to an error.', r)

		rdata = server_controloperation_json_response(r)
		return self.server._init_dataclass_from_json(
			user_datacollection_class, rdata.get('results', []), *args, **omit(kwargs, ('verify', 'user_datacollection_class')))

	def user_lookup(self, user_uids: List[int], **kwargs):
		'''	Retrieve the details of the users specified in users_uids

			@input user_uids (list of integer user IDs/int): user IDs for which the details should be retrieved

			@returns collection of user instances
		'''
		user_datacollection_class = self._user_datacollection_class(**kwargs)
				
		r = requests.post(self.server.sonador_apiurl(posixpath.join(self.fetch_endpoint, self.pk, 'user/lookup')),
			json={ 'users': user_uids }, verify=self.server.verify_ssl(**kwargs), headers=self.server.sonador_request_headers())

		if not r.ok:
			return request_client_error('Unable to execute user lookup due to an error.', r)

		rdata = server_controloperation_json_response(r)
		return self.server._init_dataclass_from_json(
			user_datacollection_class, rdata.get('results', []), **omit(kwargs, ('verify', 'user_datacollection_class')))

	def group_query(self, gfilter, *args, **kwargs):
		'''     Submit a group query to Sonador with the provided filter terms
		'''
		group_datacollection_class = self._group_datacollection_class(**kwargs)

		r = requests.post(self.server.sonador_apiurl(posixpath.join(self.fetch_endpoint, self.pk, 'group/search')),
			json=gfilter, verify=self.server.verify_ssl(**kwargs), headers=self.server.sonador_request_headers())

		if not r.ok:
			return request_client_error('Unable to execute group search query due to an error.', r)

		rdata = server_controloperation_json_response(r)
		return self.server._init_dataclass_from_json(
			group_datacollection_class, rdata.get('results', []), *args, **omit(kwargs, ('verify', 'group_datacollection_class')))

	def group_lookup(self, group_uids: List[int], **kwargs):
		'''	Retrieve the details of the groups specified in groups_uids

			@input group_uids (list of integer user IDs/int): group IDs for which the details should be retrieved

			@returns collection of group instances
		'''
		group_datacollection_class = self._group_datacollection_class(**kwargs)

		r = requests.post(self.server.sonador_apiurl(posixpath.join(self.fetch_endpoint, self.pk, 'group/lookup')),
			json={ 'groups': group_uids }, verify=self.server.verify_ssl(**kwargs), headers=self.server.sonador_request_headers())

		if not r.ok:
			return request_client_error('Unable to execute group lookup due to an error.', r)

		rdata = server_controloperation_json_response(r)
		return self.server._init_dataclass_from_json(
			group_datacollection_class, rdata.get('results', []), **omit(kwargs, ('verify',)))

	def create_tag(self, group, tag, orthanc_group_datamodel_class=None, **kwargs):
		'''	Create a tag from the provided concept

			@input group (sonador.servers.auth.SonadorGroup): group which the tag should be added to
			@input tag (highdicom.sr.CodedConcept): coded concept that should be used to create the tag
		'''
		group_datacollection_class = self._group_datacollection_class(**kwargs)
		orthanc_group_datamodel_class = self._orthanc_group_datamodel_class(**kwargs)

		if not isinstance(group, group_datacollection_class.model):
			raise TypeError('Invalid group instance, must be of type %s' % group_datacollection_class.model.__name__)
		if not isinstance(tag, CodedConcept):
			raise TypeError('Invalid concept %s, must be of type %s' % (content, CodedConcept.__name__))

		# Initialize Orthanc group instance and create tag
		orthanc_group = orthanc_group_datamodel_class(self, group)
		return orthanc_group.create_tag(tag, 
			**omit(kwargs, ('verify', 'group_datacollection_class', 'orthanc_group_datamodel_class')))

	def fetch_tags(self, group, **kwargs):
		''' Retrieve the tags for the provided group
		'''
		group_datacollection_class = self._group_datacollection_class(**kwargs)
		orthanc_group_datamodel_class = self._orthanc_group_datamodel_class(**kwargs)

		if not isinstance(group, group_datacollection_class.model):
			raise TypeError('Invalid group instance, must be of type %s' % group_datacollection_class.model.__name__)

		# Initialize Orthanc group instance and fetch tag collection
		orthanc_group = orthanc_group_datamodel_class(self, group)
		return orthanc_group.fetch_tags(
			**omit(kwargs, ('verify', 'group_datacollection_class', 'orthanc_group_datamodel_class')))

	def get_tag(self, group, uid, *args, **kwargs):
		'''	Retrieve a tag instance
		'''
		group_datacollection_class = self._group_datacollection_class(**kwargs)
		orthanc_group_datamodel_class = self._orthanc_group_datamodel_class(**kwargs)

		if not isinstance(group, group_datacollection_class.model):
			raise TypeError('Invalid group instance, must be of type %s' % group_datacollection_class.model.__name__)

		# Initialize Orthanc group instance and fetch tag collection
		orthanc_group = orthanc_group_datamodel_class(self, group)
		return orthanc_group.get_tag(uid, 
			**omit(kwargs, ('verify', 'group_datacollection_class', 'orthanc_group_datamodel_class')))

	@property
	def reviewer_worklist_item_class(self):
		'''	Model collection class to use for worklist items
		'''
		from ..imaging.orthanc.worklists import ReviewerStudyWorklistItemCollection
		return ReviewerStudyWorklistItemCollection

	def fetch_user_dcmweb_worklist(self, worklist_datacollection_class=None, parent_model_class=None, **kwargs):
		'''	Fetch reviewer study workitems for the user from the PACS via the DICOMweb worklist endpoint.
			This method populates both worklist items and parent metadata.
		'''
		# Worklist item data collection class
		worklist_datacollection_class = worklist_datacollection_class or self.reviewer_worklist_item_class

		# Parent model class, default to ImagingStudy
		if not parent_model_class:
			from ..imaging.orthanc import ImagingStudy
			parent_model_class = ImagingStudy

		return worklist_datacollection_class.fetch_user_dcmweb_worklist(self, parent_model_class, **kwargs)

	def with_credentials(self, *args, **kwargs):
		'''	Initialize an instance of the imaging server with the provided credentials
		'''
		return self.server.with_credentials(*args, **kwargs).imageserver_modelinstance_from_json(self._objectdata)


class SonadorImagingServerCollection(SonadorObjectCollection):
	'''	Collection of Orthanc/PACS imaging servers managed by Sonador
	'''
	model = SonadorImagingServer


# API methods

def sonador_apitoken_fetch(sonador_server, output_dest, verify=False):
	'''	Fetch API credentials for the server
	'''
	stoken = fetch_sonador_session_token(sonador_server, verify=verify)
	logger.info('Session token for API Access ID: %s' % sonador_server.access_id)
	output_dest.write(json.dumps(stoken))
