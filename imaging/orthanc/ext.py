'''	DICOM extension models provided by Orthanc:

	* `DcmExtBaseModel`: class which defines the base model interface, core properties, and methods
	* `DcmExtBaseCollection`: collection class which provides methods to create and fetch
		collections of base models associated with a specific parent.
'''
import abc, logging, posixpath, traceback
from collections import OrderedDict

from client.local import GuruCoreObject, GuruCoreCollection
from client.remote import GuruBaseObject, request_client_error
from client.utils.object import pick, omit

from ...remote import sonador_dataobject_create, sonador_dataobject_update, SonadorObjectCollection
from ...servers.base import ImagingServerChildCollectionFetchMixin

logger = logging.getLogger(__name__)


class DcmExtBaseModel(GuruBaseObject):
	'''	Extension base model: data object associated with a DICOM resource
	'''
	def __init__(self, server, *args, parent=None, **kwargs):
		self.parent = parent
		super().__init__(server, *args, **kwargs)

		if not self.parent:
			raise Exception('Unable to initialize extension model instance, invalid parent')

	@property
	@abc.abstractmethod
	def resource_url(self):
		'''	API URL associated with the model instance
		'''

	@property
	def url(self):
		return self.resource_url

	def update(self, odata, *args, **kwargs):
		'''	Update the instance of the extension model with the provided parameters
		'''
		if kwargs.get('verify') is None:
			kwargs['verify'] = self.parent.pacs.server.verify

		return sonador_dataobject_update(self, odata, dataobject_endpoint=self.resource_url, 
			server=self.parent.pacs, apiurl_callable='orthanc_apiurl', headers_callable='orthanc_request_headers',
			error_msg=lambda r: request_client_error('Unable to edit %s=%s on PACS server %s. Status code: %s.' % (
					type(self).__name__, self.pk, self.parent.pacs.server_label, getattr(r, 'status_code', None),
				), r),
			update_callable=self.parent.pacs._request_put, **kwargs)

	def delete(self, verify=None, headers=None, **kwargs):
		'''	Remove the extension model instance from the server
		'''
		if verify is None:
			verify = self.parent.pacs.server.verify

		return self.parent.pacs._request_delete(
			self.parent.pacs.orthanc_apiurl(self.resource_url),
			lambda r: request_client_error('Unable to remove %s=%s from PACS server %s. Status code: %s.' % (
					type(self), self.pk, self.parent.pacs.server_label, getattr(r, 'status_code', None),
				), r),
			headers=self.parent.pacs.orthanc_request_headers(headers=headers), **kwargs)


class DcmExtBaseCollection(ImagingServerChildCollectionFetchMixin, SonadorObjectCollection):
	'''	Collection of base extension models
	'''
	model = DcmExtBaseModel

	def __init__(self, server, *args, parent=None, **kwargs):
		self.parent = parent
		super().__init__(server, *args, **kwargs)

		if not self.parent:
			raise ValueError('Unable to initialize collection, invalid parent')

	def _init_collection_models(self, **kwargs):
		if self.parent:
			kwargs['parent'] = self.parent
		return super()._init_collection_models(**kwargs)

	@classmethod
	def create(cls, parent, odata, headers=None, dataobject_endpoint=None, **kwargs):
		'''	Create an instance of the base model
		'''
		# Ensure that the parent resource has an image server associated with it
		if not hasattr(parent, 'pacs'):
			raise ValueError('Invalid parent object, missing PACS reference')

		# Retrieve data from the API
		rdata = sonador_dataobject_create(parent.pacs, cls.model, odata, headers=headers,
			apiurl_callable='orthanc_apiurl', headers_callable='orthanc_request_headers',
			error_msg=lambda r: request_client_error('Unable to create %s on PACS server %s (resource=%s). Status code: %s.' % (
					cls.model.__name__, parent.pacs.server_label, dataobject_endpoint, getattr(r, 'status_code', None)
				), r),
			create_callable=parent.pacs._request_post, dataobject_endpoint=dataobject_endpoint)

		# Initialize data class
		return parent.pacs._init_dataclass(cls.model, rdata, parent=parent, **kwargs)

	@classmethod
	def fetch(cls, parent, *args, **kwargs):
		''' Fetch collection model instances for the provided parent
		'''# Ensure that the parent resource has an image server associated with it
		if not hasattr(parent, 'pacs'):
			raise ValueError('Invalid parent object, missing PACS reference.')

		return super().fetch(parent.pacs, *args, parent=parent, **kwargs)

	@classmethod
	def fetch_modelinstance(cls, parent, objectid, *args, **kwargs):
		if not hasattr(parent, 'pacs'):
			raise ValueError('Invalid parent object, missing PACS reference.')

		return super().fetch_modelinstance(parent.pacs, objectid, parent=parent, *args, **kwargs)



RESOURCE_COMMENT_OUTPUT_COLUMNS = OrderedDict((
		('parent_pk', 'Parent ID'),
		('pk', 'Comment ID'),
		('text', 'Text'),
	))


class ResourceComment(DcmExtBaseModel):
	'''	Comment associated with a resource	
	'''
	pk_attr = 'ID'
	tabulate_output_columns = RESOURCE_COMMENT_OUTPUT_COLUMNS

	def __init__(self, *args, dicomweb_api=False, **kwargs):
		self.dicomweb_api = dicomweb_api
		super().__init__(*args, **kwargs)

	@property
	def parent_pk(self):
		return self.parent.pk

	@property
	def resource_url(self):		
		endpoint_url = self.parent.dicomweb_comments_url if self.dicomweb_api else self.parent.comments_url
		return posixpath.join(endpoint_url, self.pk)

	@property
	def text(self):
		return self._objectdata.get('Text')


class ResourceCommentCollection(DcmExtBaseCollection):
	'''	Collection of comments
	'''
	model = ResourceComment

	def __init__(self, *args, dicomweb_api=False, **kwargs):
		self.dicomweb_api = dicomweb_api
		super().__init__(*args, **kwargs)

	@classmethod
	def create(cls, parent, *args, dicomweb_api=False, **kwargs):
		'''	Create a comment instance
		'''
		cls._verify_parent(parent, dicomweb_api=dicomweb_api, **kwargs)
		endpoint_url = parent.dicomweb_comments_url if dicomweb_api else parent.comments_url
		return super().create(parent, *args, dataobject_endpoint=endpoint_url, dicomweb_api=dicomweb_api, **kwargs)

	@classmethod
	def _verify_parent(cls, parent, dicomweb_api=False, **kwargs):
		'''	Verify that the provided endpoint has the required properties required to complete API requests.
		'''
		if dicomweb_api and not hasattr(parent, 'dicomweb_comments_url'):			
			raise ValueError('Unable to perform comments opreation, parent doews not have a valid dicomweb_comments_url property')
		elif not hasattr(parent, 'comments_url'):
			raise ValueError('Unable to perform comments operation, parent does not have a valid comments_url property')

	@classmethod
	def fetch(cls, parent, *args, dicomweb_api=False, **kwargs):
		'''	Fetch comments for the provided parent
		'''
		cls._verify_parent(parent, dicomweb_api=dicomweb_api, **kwargs)
		
		# Notify user of an error
		error_msg = lambda r: request_client_error(
			'Unable to retrieve comments for %s=%s from PACS server "%s". Status code: %s.' % (
			type(parent).__name__, parent.pk, parent.pacs.server_label, getattr(r, 'status_code', None),
		), r)

		return super().fetch(parent, *args, 
			data_collection_endpoint=parent.dicomweb_comments_url if dicomweb_api else parent.comments_url, 
			error_msg=error_msg, dicomweb_api=dicomweb_api, **kwargs)

	@classmethod
	def fetch_modelinstance(cls, parent, objectid, *args, dicomweb_api=False, **kwargs):
		'''	Fetch comment
		'''
		cls._verify_parent(parent, dicomweb_api=dicomweb_api, **kwargs)
		endpoint_url = parent.dicomweb_comments_url if dicomweb_api else parent.comments_url

		return super().fetch_modelinstance(parent, objectid, *args, 
			dataobject_endpoint=posixpath.join(endpoint_url, objectid), dicomweb_api=dicomweb_api, **kwargs)
