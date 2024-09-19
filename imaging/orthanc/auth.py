import abc, posixpath
from collections import OrderedDict
from .ext import DcmExtBaseModel, DcmExtBaseCollection, DcmExtParentMixin, DcmExtCollectionParentMixin


RESOURCE_ACL_OUTPUT_COLUMNS = OrderedDict((
		('parent_pk', 'Parent ID'),
		('pk', 'ACL ID'),
		('view', 'View'),
		('modify', 'Modify'),
		('remove', 'Remove'),
		('acl', 'Manage Access'),
	))


RESOURCE_ACL_OUTPUT_EXTENDED_COLUMNS = OrderedDict((
		('parent_pk', 'Parent ID'),
		('pk', 'ACL ID'),
		('view', 'View'),
		('modify', 'Modify'),
		('remove', 'Remove'),
		('acl', 'Manage Access'),
		('manage_comments', 'Manage Comments'),
		('view_comments', 'View Comments'),
	))


class OrthancResourceAccessControlListBaseModel(DcmExtParentMixin, DcmExtBaseModel):
	'''	Data object representing an access control list (ACL) for a resource. ACLs provide
		a set of permissions for a user or group that authorize members to access resources
		stored on the imaging server. Orthanc resource ACL instances provide direct access
		to the resource with which they are associated.
	'''
	pk_attr = 'ID'
	tabulate_output_columns = RESOURCE_ACL_OUTPUT_COLUMNS

	def __init__(self, *args, dicomweb_api=False, **kwargs):
		self.dicomweb_api = dicomweb_api
		self.resource_model = kwargs.pop('resource_model', None)
		super().__init__(*args, **kwargs)


class OrthancResourceAccessControlListCollection(DcmExtCollectionParentMixin, DcmExtBaseCollection):
	'''	Collection of ACL policies
	'''
	model = OrthancResourceAccessControlListBaseModel

	def __init__(self, *args, dicomweb_api=False, **kwargs):
		self.dicomweb_api = dicomweb_api
		super().__init__(*args, **kwargs)


class OrthancUserResourceAccessControlList(OrthancResourceAccessControlListBaseModel):
	'''	Access control list associated with a user account
	'''
	@property
	def resource_url(self):
		endpoint_url = self.parent.dicomweb_user_acl_url if self.dicomweb_api else self.parent.user_acl_url
		return posixpath.join(endpoint_url, self.pk)


class OrthancGroupResourceAccessControlList(OrthancResourceAccessControlListBaseModel):
	'''	Access control list associated with a group account
	'''
	@property
	def resource_url(self):
		endpoint_url = self.parent.dicomweb_group_acl_url if self.dicomweb_api else self.parent.group_acl_url
		return posixpath.join(endpoint_url, self.pk)


class OrthancUserResourceAccessControlListCollection(OrthancResourceAccessControlListCollection):
	'''	Collection of user ACL policies
	'''
	model = OrthancUserResourceAccessControlList

	@classmethod
	def _verify_parent(cls, parent, dicomweb_api=False, **kwargs):
		'''	Verify that the provided parent has the required properties required to complete API requests.
		'''
		if dicomweb_api and not hasattr(parent, 'dicomweb_user_acl_url'):			
			raise ValueError('Unable to perform ACL operation, parent does not have a valid dicomweb_user_acl_url property')
		elif not hasattr(parent, 'user_acl_url'):
			raise ValueError('Unable to perform ACL operation, parent does not have a valid user_acl_url property')

	@classmethod
	def _parent_endpoint(cls, parent, dicomweb_api=False, **kwargs):
		'''	Retrieve the endpoint to be used by the collection from the parent
		'''
		return parent.dicomweb_user_acl_url if dicomweb_api else parent.user_acl_url


class OrthancGroupResourceAccessControlListCollection(OrthancResourceAccessControlListCollection):
	'''	Collection of group ACL policies
	'''
	model = OrthancGroupResourceAccessControlList

	@classmethod
	def _verify_parent(cls, parent, dicomweb_api=False, **kwargs):
		'''	Verify that the provided parent has the required properties required to complete API requests.
		'''
		if dicomweb_api and not hasattr(parent, 'dicomweb_group_acl_url'):			
			raise ValueError('Unable to perform ACL operation, parent does not have a valid dicomweb_user_acl_url property')
		elif not hasattr(parent, 'group_acl_url'):
			raise ValueError('Unable to perform ACL operation, parent does not have a valid user_acl_url property')

	@classmethod
	def _parent_endpoint(cls, parent, dicomweb_api=False, **kwargs):
		'''	Retrieve the endpoint to be used by the collection from the parent
		'''
		return parent.dicomweb_group_acl_url if dicomweb_api else parent.group_acl_url