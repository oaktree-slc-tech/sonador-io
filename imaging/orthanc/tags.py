import posixpath, functools
from collections import OrderedDict

from highdicom.sr import CodedConcept

from ...apisettings.sr import DCMSR_CODE_VALUE, DCMSR_CODE_MEANING, DCMSR_CODE_SCHEME, DCMSR_CDOE_SCHEME_VERSION

from .ext import DcmExtBaseModel, DcmExtBaseCollection, DcmExtParentMixin, DcmExtCollectionParentMixin


TAG_OUTPUT_COLUMNS = OrderedDict((
	('parent_pk', 'Group ID'),
	('pk', 'Tag ID'),
	('scheme', 'Scheme'),
	('value', 'Value'),
	('meaning', 'Meaning')
))


class Tag(DcmExtParentMixin, DcmExtBaseModel):
	''' Tag. Tags within Orthanc are structured "codes" which describe categorical concepts.
	'''
	pk_attr = 'ID'
	tabulate_output_columns = TAG_OUTPUT_COLUMNS

	def __init__(self, *args, dicomweb_api=False, **kwargs):
		self.dicomweb_web = dicomweb_api
		super().__init__(*args, **kwargs)

	@property
	def resource_url(self):
		return posixpath.join(self.parent.tags_url, self.pk)

	@property
	def value(self):
		'''	Tag value
		'''
		return self._objectdata.get(DCMSR_CODE_VALUE)

	@property
	def meaning(self):
		'''	Tag meaning
		'''
		return self._objectdata.get(DCMSR_CODE_MEANING)

	@property
	def scheme(self):
		'''	Scheme used to define the tag
		'''
		return self._objectdata.get(DCMSR_CODE_SCHEME)

	@property
	def scheme_designator(self):
		'''	Scheme designator: grammar used to define the tag
		'''
		return self.scheme

	@property
	def scheme_version(self):
		'''	Version of the scheme used to define the tag
		'''
		return self._objectdata.get(DCMSR_CDOE_SCHEME_VERSION)

	@property
	def concept(self):
		'''	Coded concept version of the tag
		'''
		return CodedConcept(
			self.value, self.scheme, self.meaning, scheme_version=self.scheme_version)


class TagCollection(DcmExtCollectionParentMixin, DcmExtBaseCollection):
	'''	Collection of tags
	'''
	model = Tag

	def __init__(self, *args, dicomweb_api=False, **kwargs):
		self.dicomweb_web = dicomweb_api
		super().__init__(*args, **kwargs)

	@classmethod
	def _verify_parent(cls, parent, dicomweb_api=False, **kwargs):
		'''	Verify that the provided parent has the required properties required to complete API requests.
		'''
		if not hasattr(parent, 'tags_url'):
			raise ValueError('Unable to perform tags operation, parent does not have a valid tags_url property')

	@classmethod
	def _parent_endpoint(cls, parent, dicomweb_api=False, **kwargs):
		'''	Retrieve the endpoint to be used by the collection from the parent
		'''
		return parent.tags_url