import copy, posixpath, functools
from collections import OrderedDict

from highdicom.sr import CodedConcept

from ...apisettings.sr import DCMSR_CODE_VALUE, DCMSR_CODE_MEANING, DCMSR_CODE_SCHEME, DCMSR_CDOE_SCHEME_VERSION, \
	DCMSR_TAG_META
from ...apisettings.display_attrs import DISPLAY_ATTRIBUTES, tag_display_color

from .ext import DcmExtBaseModel, DcmExtBaseCollection, DcmExtParentMixin, DcmExtCollectionParentMixin


_UNSET = object()


def tag_api_json(value, meaning, scheme_designator, scheme_version=None, meta=_UNSET):
	'''	Tag API payload for a coded concept. `Meta` is sent only when `meta` is given; `None`
		clears the stored document.
	'''
	_data = {
		DCMSR_CODE_VALUE: value,
		DCMSR_CODE_MEANING: meaning,
		DCMSR_CODE_SCHEME: scheme_designator,
	}
	if scheme_version:
		_data[DCMSR_CDOE_SCHEME_VERSION] = scheme_version
	if meta is not _UNSET:
		_data[DCMSR_TAG_META] = meta
	return _data


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
	def meta(self):
		'''	`Meta` document of the tag (display properties), empty when none is stored
		'''
		return self._objectdata.get(DCMSR_TAG_META) or {}

	@property
	def display_attributes(self):
		'''	Display attributes applied when the tag is rendered
		'''
		return self.meta.get(DISPLAY_ATTRIBUTES) or {}

	@property
	def display_color(self):
		'''	Colors of the rendered tag: `Background`, `Border`, `Value` and `Meaning` (hexadecimal `#RRGGBB`)
		'''
		return tag_display_color(self.meta)

	def update_meta(self, meta, **kwargs):
		'''	Replace the `Meta` document of the tag. `None` clears it. The instance then holds the
			document as the server stored it.

			@input meta (dict or None): e.g. `{ 'DisplayAttributes': { 'Color': { 'Value': '#8AD0A8' } } }`
		'''
		response = self.update(tag_api_json(self.value, self.meaning, self.scheme, self.scheme_version, meta=meta), **kwargs)

		# The server normalizes the document; read back what it stored
		stored = TagCollection.fetch_modelinstance(self.parent, self.pk)
		self._objectdata[DCMSR_TAG_META] = copy.deepcopy(stored._objectdata.get(DCMSR_TAG_META))
		if not self._objectdata[DCMSR_TAG_META]:
			self._objectdata.pop(DCMSR_TAG_META)
		return response

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