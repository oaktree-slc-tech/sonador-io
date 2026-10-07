'''	Display Attributes: DICOM attributes a Sonador group curates for the viewer's corner overlay.
	Served by the orthanc-sonador plugin at /groups/<id>/display-attributes.
'''
import posixpath
from collections import OrderedDict

from .ext import DcmExtBaseModel, DcmExtBaseCollection, DcmExtParentMixin, DcmExtCollectionParentMixin


DISPLAY_ATTR_CODE = 'Code'
DISPLAY_ATTR_KEYWORD = 'Tag'
DISPLAY_ATTR_LABEL = 'Label'
DISPLAY_ATTR_PRIVATE = 'Private'
DISPLAY_ATTR_GROUP = 'Group'

DISPLAY_ATTR_OUTPUT_COLUMNS = OrderedDict((
	('parent_pk', 'Group ID'),
	('pk', 'Attribute ID'),
	('code', 'Code'),
	('keyword', 'Tag'),
	('label', 'Label'),
	('private', 'Private'),
))


class DisplayAttribute(DcmExtParentMixin, DcmExtBaseModel):
	'''	One display attribute owned by a group
	'''
	pk_attr = 'ID'
	tabulate_output_columns = DISPLAY_ATTR_OUTPUT_COLUMNS

	def __init__(self, *args, dicomweb_api=False, **kwargs):
		# The shared parent fetch and create paths pass the keyword down; the attribute has no DICOMweb form
		super().__init__(*args, **kwargs)

	@property
	def resource_url(self):
		return posixpath.join(self.parent.display_attributes_url, self.pk)

	@property
	def code(self):
		'''	Canonical tag code, `GGGG,EEEE`
		'''
		return self._objectdata.get(DISPLAY_ATTR_CODE)

	@property
	def keyword(self):
		'''	DICOM dictionary keyword, when the server's dictionary knows the tag
		'''
		return self._objectdata.get(DISPLAY_ATTR_KEYWORD)

	@property
	def label(self):
		'''	Display label override, or None
		'''
		return self._objectdata.get(DISPLAY_ATTR_LABEL)

	@property
	def private(self):
		return bool(self._objectdata.get(DISPLAY_ATTR_PRIVATE))

	@property
	def group(self):
		'''	`{ id, name }` of the owning group, as reported by the server
		'''
		return self._objectdata.get(DISPLAY_ATTR_GROUP)

	def relabel(self, label, **kwargs):
		'''	Change the display label; an empty label clears the override
		'''
		return self.update({ DISPLAY_ATTR_LABEL: label or None }, **kwargs)


class DisplayAttributeCollection(DcmExtCollectionParentMixin, DcmExtBaseCollection):
	'''	Collection of display attributes for one group
	'''
	model = DisplayAttribute

	def __init__(self, *args, dicomweb_api=False, **kwargs):
		super().__init__(*args, **kwargs)

	@classmethod
	def _verify_parent(cls, parent, dicomweb_api=False, **kwargs):
		if not hasattr(parent, 'display_attributes_url'):
			raise ValueError('Unable to perform display attributes operation, parent does not have a valid display_attributes_url property')

	@classmethod
	def _parent_endpoint(cls, parent, dicomweb_api=False, **kwargs):
		return parent.display_attributes_url
