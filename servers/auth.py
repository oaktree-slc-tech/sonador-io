import posixpath, functools
from collections import OrderedDict

from client.utils.decorators import classproperty

from ..remote import fetch_sonador_data_collection, SonadorObjectUpdateMixin

from .base import SonadorBaseObject, SonadorObjectCollection


class SonadorUser(SonadorBaseObject):
	''' Sonador user
	'''
	def __str__(self):

		# Display name, ID and username
		if getattr(self, 'name', None) and getattr(self, 'id', None) and getattr(self, 'username', None):
			return '%s (id=%s username=%s)' % (self.name, self.id, self.username)

		# Username and ID
		elif getattr(self, 'username', None) and getattr(self, 'id', None):
			return '%s (id=%s)' % (self.username, self.id)

		# User ID
		elif getattr(self, 'id', None):
			return self.id

		return ''


class SonadorUserObjectMixin:
	'''	Mixin class wwhich provides properites for parsing Sonador user attributes
	'''
	@property
	@functools.lru_cache()
	def user(self):
		return SonadorUser(self.server, (self._objectdata.get('user') or {}))

	@property
	def user_id(self):
		return self.user.id

	@property
	def username(self):
		return self.user.username

	@property
	def user_displayname(self):
		return self.user.name



# Sonador Secure API Credentials

SECURE_API_CREDENTIAL_OUTPUT_COLUMNS = OrderedDict((
	('username', 'User'),
	('user_displayname', ''),
	('pk', 'Access ID'),
	('secret_key', 'Secret Key'),
	('description', 'Description'),
	('ctime', 'Created'),
))


class SonadorSecureApiCredential(SonadorUserObjectMixin, SonadorObjectUpdateMixin, SonadorBaseObject):
	'''	Data object representing a secure API access credential (access ID and secret key)
	'''
	pk_attr = 'access_id'

	tabulate_output_columns = SECURE_API_CREDENTIAL_OUTPUT_COLUMNS
	fetch_endpoint = '/auth/api/cred/access'

	@property
	def url(self):
		return posixpath.join(self.fetch_endpoint, self.pk)


class SonadorSecureApiCredentialCollection(SonadorObjectCollection):
	'''	Collection which can be used to work with user secure access API credentials
	'''
	model = SonadorSecureApiCredential



# Sonador Authentication Tokens

AUTHTOKEN_OUTPUT_COLUMNS = OrderedDict((
	('username', 'User'),
	('user_displayname', ''),
	('pk', 'Token'),
	('description', 'Description'),
	('ctime', 'Created'),
))


class SonadorApiToken(SonadorUserObjectMixin, SonadorObjectUpdateMixin, SonadorBaseObject):
	'''	Data object representing a Sonador authentication token. Sonador access tokens are one of the
		methods that can be used to access API endpoints and methods. They are comprised of a string
		that uniquely identifies user.

		Because API tokens are sensitive credentials, the only time the API will provide the full value
		is when they are first created. Subsequent calls to the API will return a masked value. Because of
		this, the API token endpoint works differently than other resources. All requests are made to a
		single endpoint (the fetch_endpoint). The `url` property of this class maps t

		* GET: fetch the set of API tokens associated with the user account.
		* POST: create a new API token. Once a token has been generated, its value cannot be changed.
		* PUT: update properties for a specific token
		 	- 	`token` (required): token string, may be a masked value. The API will 
		  		attempt to retrieve the token instance based on the value provided. Example: `Bnyv...Ju73`.
		  		The token string may be split with either an ellipsis (`...`) or a dash (`-`). If the API
		  		is unable to match the requsted token, it will return a 404.
		  	- 	`description`: description
		  	-	...
		* DELETE: remove the instance from the server
	'''
	tabulate_output_columns = AUTHTOKEN_OUTPUT_COLUMNS
	fetch_endpoint = '/auth/api/cred/token'

	@classproperty
	def url(cls):
		return cls.fetch_endpoint

	def update(self, object_data, *args, **kwargs):
		'''	Update the token instance with the provided object data.
		'''
		if not object_data.get(self.pk_attr):
			object_data[self.pk_attr] = self.pk

		return super().update(object_data, *args, dataobject_endpoint=self.url, **kwargs)

	def delete(self, *args, **kwargs):
		'''	Remove the token instance from Sonador
		'''
		return super().delete(*args, json={ self.pk_attr: self.pk }, **kwargs)


class SonadorApiTokenCollection(SonadorObjectCollection):
	'''	Collection of user API authentication tokens
	'''
	model = SonadorApiToken
