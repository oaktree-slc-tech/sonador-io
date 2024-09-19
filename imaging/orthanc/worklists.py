'''	DICOM extension model for worklists API provided by Orthanc

	* `ReviewerStudyWorklistItem`: class which defines the base model interface, core properties, and methods
	* `ReviewerStudyWorklistItemCollectionN`: collection class which provides methods to create and fetch
		collections of worklist items associated with a specific study.
'''
import posixpath
from collections import OrderedDict

from ...servers.auth import SonadorUserObjectMixin, SonadorGroupObjectMixin
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

	def __init__(self, *args, dicomweb_api=False, **kwargs):
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
