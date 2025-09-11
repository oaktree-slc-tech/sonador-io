import posixpath, functools
from collections import OrderedDict

from client.utils.decorators import classproperty
from client.utils.object import pick, omit

from ..remote import fetch_sonador_data_collection, SonadorObjectUpdateMixin

from .base import SonadorBaseObject, SonadorObjectCollection, ImagingServerChildBaseObject, \
	ImagingServerChildCollection
from .dicom import ImagingServerModalityMixin


# Groups and Users

class SonadorGroup(SonadorObjectUpdateMixin, SonadorBaseObject):
	''' Sonador group
	'''
	pk_attr = 'id'
	fetch_endpoint = '/visionaire/api/group'

	def __str__(self):
		return self.name
	
	@property
	def url(self):
		return posixpath.join(self.fetch_endpoint, str(self.pk))


class SonadorGroupCollection(SonadorObjectCollection):
	'''	Collection of Sonador group instances
	'''
	model = SonadorGroup


class SonadorUser(SonadorObjectUpdateMixin, SonadorBaseObject):
	''' Sonador user
	'''
	pk_attr = 'id'
	fetch_endpoint = '/visionaire/api/user'

	def __str__(self):

		# Display name, ID and username
		if getattr(self, 'name', None) and getattr(self, 'id', None) and getattr(self, 'username', None):
			return '%s (id=%s username=%s)' % (self.name, self.pk, self.username)

		# Username and ID
		elif getattr(self, 'username', None) and getattr(self, 'pk', None):
			return '%s (id=%s)' % (self.username, self.pk)

		# User ID
		elif getattr(self, 'id', None):
			return self.pk

		return ''

	@property
	def url(self):
		return posixpath.join(self.fetch_endpoint, str(self.pk))


class SonadorUserCollection(SonadorObjectCollection):
	'''	Collection of Sonador user instances
	'''
	model = SonadorUser


class SonadorUserObjectMixin:
	'''	Mixin class which provides properites for parsing Sonador user attributes
	'''
	user_attr = 'user'

	def _init_user(self, *args, user=None, **kwargs):
		self._user = user
		self.user_attr = kwargs.get('user_attr', self.user_attr)

	@property
	def user(self):
		if getattr(self, '_user', None) is None:
			setattr(self, '_user', SonadorUser(self.server, (self._objectdata.get(self.user_attr) or {})))

		return self._user

	@property
	def user_id(self):
		return self.user.pk

	@property
	def username(self):
		return self.user.username

	@property
	def user_displayname(self):
		return self.user.name


class SonadorAdminUserObjectMixin:
	'''	Mixin class which provides user properties for objects created via the Sonador admin user API
	'''
	def _init_adminuser(self, *args, **kwargs):
		if not self.user:
			raise ValueError('Unable to initialize API credential, invalid user instance')


class SonadorAdminUserCollectionObjectMixin:
	'''	Mixin class which provides user properties for collections of objects created via the Sonador admin user API
	'''
	def _init_empty_collection(self, *args, **kwargs):
		if kwargs.get('user') is None and self.user:
			kwargs['user'] = self.user

		return super()._init_empty_collection(*args, **kwargs)

	def _init_collection_models(self, **kwargs):
		if self.user:
			kwargs['user'] = self.user

		return super()._init_collection_models(**kwargs)



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

	def __init__(self, *args, **kwargs):
		super().__init__(*args, **omit(kwargs, ('user',)))
		self._init_user(*args, **kwargs)

	@property
	def url(self):
		return posixpath.join(self.fetch_endpoint, self.pk)


class SonadorSecureApiCredentialCollection(SonadorObjectCollection):
	'''	Collection which can be used to work with user secure access API credentials
	'''
	model = SonadorSecureApiCredential


class AdminSonadorSecureApiCredential(SonadorAdminUserObjectMixin, SonadorSecureApiCredential):
	''' Data object representing a secure API access credential (access ID and secret key) issued
		via the admin API. Admin credential instances differ from user credential instances in that they require
		a user instance to initialize the class instance. Regular credential instances are identified via the
		API by the user credentials which were used to retrieve them.
	'''
	cred_urlroot = 'cred/access'

	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)
		self._init_adminuser(*args, **kwargs)

	@property
	def fetch_endpoint(self):
		return posixpath.join(self.user.url, self.cred_urlroot)


class AdminSonadorSecureApiCredentialCollection(SonadorAdminUserCollectionObjectMixin, 
		SonadorUserObjectMixin, SonadorAdminUserObjectMixin, SonadorObjectCollection):
	'''	Collection which can be used to work with user secure access API credentials via the admin API.
		Admin credential instances differ from user credential instances in that they require an explicit
		user instance be passed in during init. Regular credential instances are identified via the API
		by the user credentials used to retrieve them.
	'''
	model = AdminSonadorSecureApiCredential

	def __init__(self, *args, **kwargs):
		super().__init__(*args, **omit(kwargs, ('user',)))
		self._init_user(*args, **kwargs)
		self._init_adminuser(*args, **kwargs)



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

	def __init__(self, *args, **kwargs):
		super().__init__(*args, **omit(kwargs, ('user',)))
		self._init_user(*args, **kwargs)

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


class AdminSonadorApiToken(SonadorAdminUserObjectMixin, SonadorApiToken):
	'''	Data object representing a Sonador authentication token issued via the admin API.
		Admin credential instances differ from user credential instances in that they require an explicit
		user instance be passed in during init. Regular credential instances are identified via the API
		by the user credentials used to retrieve them.
	'''
	cred_urlroot = 'cred/token'

	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)
		self._init_adminuser(*args, **kwargs)

	@property
	def fetch_endpoint(self):
		return posixpath.join(self.user.url, self.cred_urlroot)

	@property
	def url(self):
		return self.fetch_endpoint


class AdminSonadorApiTokenCollection(SonadorAdminUserCollectionObjectMixin,
		SonadorUserObjectMixin, SonadorAdminUserObjectMixin, SonadorObjectCollection):
	'''	Collection which can be used to work with API token instances via the admin API.
		Admin credential instances differ from user credential instances in that they require an explicit
		user instance be passed in during init. Regular credential instances are identified via the API
		by the user credentials used to retrieve them.
	'''
	model = AdminSonadorApiToken

	def __init__(self, *args, **kwargs):
		super().__init__(*args, **omit(kwargs, ('user',)))
		self._init_user(*args, **kwargs)
		self._init_adminuser(*args, **kwargs)



# Sonador Access Control Management
ACL_PERM_QUERY = 'query'
ACL_PERM_UPLOAD = 'upload'
ACL_PERM_VIEW = 'view'
ACL_PERM_MODIFY = 'modify'
ACL_PERM_REMOVE = 'remove'
ACL_PERM_COMMENT_EDIT = 'comment_edit'
ACL_PERM_COMMENT_VIEW = 'comment_view'
ACL_PERM_ACL = 'acl'

ACL_PERM_SERVER = (ACL_PERM_QUERY, ACL_PERM_UPLOAD)
ACL_PERM_RESOURCE = (ACL_PERM_VIEW, ACL_PERM_MODIFY, ACL_PERM_REMOVE, 
	ACL_PERM_COMMENT_EDIT, ACL_PERM_COMMENT_VIEW, ACL_PERM_ACL)


ACL_PERM_ORTHANC_VIEW =  ACL_PERM_VIEW.title()
ACL_PERM_ORTHANC_MODIFY = ACL_PERM_MODIFY.title()
ACL_PERM_ORTHANC_REMOVE = ACL_PERM_REMOVE.title()
ACL_PERM_ORTHANC_COMMENT_EDIT = 'CommentEdit'
ACL_PERM_ORTHANC_COMMENT_VIEW = 'CommentView'
ACL_PERM_ORTHANC_ACL = ACL_PERM_ACL.upper()

ACL_PERM_ORTHANC_MAPPINGS = {
	ACL_PERM_VIEW: ACL_PERM_ORTHANC_VIEW,
	ACL_PERM_MODIFY: ACL_PERM_ORTHANC_MODIFY,
	ACL_PERM_REMOVE: ACL_PERM_ORTHANC_REMOVE,
	ACL_PERM_COMMENT_EDIT: ACL_PERM_ORTHANC_COMMENT_EDIT,
	ACL_PERM_COMMENT_VIEW: ACL_PERM_ORTHANC_COMMENT_VIEW,
	ACL_PERM_ACL: ACL_PERM_ORTHANC_ACL,
}


ACL_OUTPUT_COLUMNS = OrderedDict((
	('imaging_server', 'Imaging Server ID'),
	('group', 'Group'),
	('pk', 'ACL ID'),
	(ACL_PERM_QUERY, 'Query'),
	(ACL_PERM_UPLOAD, 'Upload'),
	('resource', 'Resource'),
	(ACL_PERM_VIEW, 'View'),
	(ACL_PERM_MODIFY, 'Modify'),
	(ACL_PERM_REMOVE, 'Remove'),
	(ACL_PERM_COMMENT_EDIT, 'Manage Comments'),
	(ACL_PERM_COMMENT_VIEW, 'View Comments'),
	(ACL_PERM_ACL, 'Access Control'),
))


class SonadorGroupAccessControlList(
		ImagingServerModalityMixin, SonadorObjectUpdateMixin, ImagingServerChildBaseObject):
	'''	Data object representing a group access control list (ACL) for an imaging server. ACLs provide
		a set of permissions for a group that authorize members of a group to access resources stored
		on an imaging server.
	'''	
	tabulate_output_columns = ACL_OUTPUT_COLUMNS
	acl_urlroot = 'acl'

	def update(self, object_data, *args, **kwargs):
		'''	Update the group access control policy
		'''
		# Add data object update endpoint
		kwargs['dataobject_endpoint'] = self.url
		return super().update(object_data, *args, **kwargs)

	@property
	def url(self):
		if not self.pacs:
			raise ValueError('Unable to update group access control policy, no image server instance')

		return posixpath.join(self.pacs.fetch_endpoint, str(self.pacs.pk), self.acl_urlroot, str(self.pk))


class SonadorGroupAccessControlListCollection(ImagingServerChildCollection):
	'''	Collection of group access control lists
	'''
	model = SonadorGroupAccessControlList

	def get_group_acl(self, group_pk, *args, **kwargs):
		'''	Retrieve the ACL instance for the specified group

			@returns ACL policy instance or None if a policy for the provided group UID
				cannot be located.
		'''
		_acl = self.filter(lambda _p: _p.group == group_pk)
		if len(_acl):
			return _acl[0]

		return None


class SonadorGroupObjectMixin:
	'''	Mixin class which provides properites for parsing Sonador user attributes
	'''
	group_attr = 'group'

	def _init_group(self, *args, group=None, **kwargs):
		self._group = group
		self.group_attr = kwargs.get('group_attr', self.group_attr)

	@property
	def group(self):
		if getattr(self, '_group', None) is None:
			setattr(self, '_group', SonadorGroup(self.server, (self._objectdata.get(self.group_attr) or {})))

		return self._group

	@property
	def group_id(self):
		return self.group.pk

	@property
	def group_name(self):
		return self.group.name
