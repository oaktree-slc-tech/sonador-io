'''	DICOM extension model for worklists API provided by Orthanc

	* `ReviewerStudyWorklistItem`: class which defines the base model interface, core properties, and methods
	* `ReviewerStudyWorklistItemCollectionN`: collection class which provides methods to create and fetch
		collections of worklist items associated with a specific study.
'''
import posixpath
from collections import OrderedDict

from client.remote import request_client_error
from client.utils.object import pick, omit

from ...apisettings import IMAGING_SERVER_RESOURCE_STUDY
from ...servers.auth import SonadorUserObjectMixin, SonadorGroupObjectMixin
from ..dicomweb import dicomweb_tag_name, dicomweb_tag_keys, dicomweb_code_keys, \
	dicomweb_value, dicomweb2keyval, dcmjson2orthanc
from .ext import DcmExtBaseModel, DcmExtBaseCollection, DcmExtParentMixin, DcmExtCollectionParentMixin


WORKLIST_OUTPUT_COLUMNS = OrderedDict((
	('pk', 'Worklist Item ID'),
	('group_name', 'Group'),
	('user_displayname', 'User'),
	('parent_pk', 'Study ID'),
	('state', 'Status'),
))


class ReviewerStudyWorklistItem(SonadorGroupObjectMixin, SonadorUserObjectMixin, DcmExtParentMixin, DcmExtBaseModel):
	'''	Worklist item associated with a study
	'''
	pk_attr = 'ID'
	tabulate_output_columns = WORKLIST_OUTPUT_COLUMNS
	user_attr = 'User'
	group_attr = 'Group'
	parent_attr = IMAGING_SERVER_RESOURCE_STUDY

	def __init__(self, *args, dicomweb_api=False, **kwargs):
		self.dicomweb_api = dicomweb_api
		super().__init__(*args, **kwargs)
		self._init_user(*args, **kwargs)
		self._init_group(*args, **kwargs)

	@property
	def resource_url(self):		
		endpoint_url = self.parent.dicomweb_worklist_reviewer_url if self.dicomweb_api else self.parent.worklist_reviewer_url
		return posixpath.join(endpoint_url, self.pk)

	@property
	def state(self):
		return self._objectdata.get('State')


class ReviewerStudyWorklistItemCollection(DcmExtCollectionParentMixin, DcmExtBaseCollection):
	'''	Collection of study reviewer worklist items
	'''
	model = ReviewerStudyWorklistItem
	user_dcmweb_worklist_fetch_endpoint = 'worklist/studies'

	def __init__(self, *args, dicomweb_api=False, **kwargs):
		self.parent_required = kwargs.pop('parent_required', self.parent_required)
		self.dicomweb_api = dicomweb_api
		super().__init__(*args, **kwargs)

	@classmethod
	def _verify_parent(cls, parent, dicomweb_api=False, **kwargs):
		'''	Verify that the provided parent has the required properties required to complete API requests.
		'''
		if dicomweb_api and not hasattr(parent, 'dicomweb_worklist_reviewer_url'):			
			raise ValueError('Unable to perform worklists operation, parent does not have a valid dicomweb_worklist_reviewer_url property')
		elif not hasattr(parent, 'worklist_reviewer_url'):
			raise ValueError('Unable to perform worklists operation, parent does not have a valid worklist_reviewer_url property')

	@classmethod
	def _parent_endpoint(cls, parent, dicomweb_api=False, **kwargs):
		'''	Retrieve the endpoint to be used by the collection from the parent
		'''
		return parent.dicomweb_worklist_reviewer_url if dicomweb_api else parent.worklist_reviewer_url

	@classmethod
	def fetch_user_dcmweb_worklist(cls, pacs, parent_model_class, error_msg=None,
			data_collection_endpoint=None, query_params=None, rkwargs=None, cache_dcm_tags=None, **kwargs):
		'''	Fetch reviewer study workitems for the user from the PACS via the DICOMweb worklist endpoint.
			This method populates both worklist items and parent metadata.
		'''
		query_params = query_params or {}
		rkwargs = rkwargs or {}
		cache_dcm_tags = cache_dcm_tags or pacs.cache_dcm_tags()

		data_collection_endpoint = data_collection_endpoint or posixpath.join(
			pacs.dicomweb_root, cls.user_dcmweb_worklist_fetch_endpoint)

		if not error_msg:
			error_msg = lambda r: request_client_error(
				'Unable to retrieve user worklist from PACS server (url="%s"). Status code: %s.' % (
					cls.model.__name__, pacs.server_label, r.request.url, r.status_code,
				), r)

		# Retrieve worklist for the user
		r = pacs._request_get(pacs.orthanc_apiurl(
				data_collection_endpoint, query_params=query_params, query_lowercase=False), 
			error_msg, headers=pacs.orthanc_request_headers(**kwargs), verify=pacs.verify_ssl(**kwargs), **rkwargs)

		# Initialize empty collection
		dcmweb_worklist = cls(pacs, [], parent_required=False)

		# Parse respnse data and create worklist/parent pairs for each object in the response.
		# The user DICOMweb worklist item endpoint returns information about user, group, 
		# and the DICOM resource the worklist item is associated with. The DICOM tags follow
		# the conventions for DICOMweb responses.
		# Refer to https://www.dicomstandard.org/using/dicomweb.
		for rdata in pacs._parse_apiresponse_json(r):

			# Parse API response to parent DICOM data and worklist components
			parent_dcm_json = dcmjson2orthanc(dicomweb2keyval(rdata), parent_model_class, cache_dcm_tags)
			worklist_item_json = omit(rdata, dicomweb_code_keys(rdata))

			# Add parent identifier to parent DICOM JSON
			if worklist_item_json.get(cls.model.parent_attr):
				parent_dcm_json[parent_model_class.pk_attr] = worklist_item_json.get(cls.model.parent_attr)

			# Initialize worklist item
			worklist_item = cls.model(pacs, worklist_item_json,
				parent=parent_model_class(pacs, parent_dcm_json), **kwargs)

			# Add worklist item to collection
			dcmweb_worklist.append(worklist_item)

		return dcmweb_worklist