'''	Models and collections with methods for working with objectsin Orthanc that are owned by Sonador groups.
'''
import posixpath

from ...apisettings.sr import DCMSR_CODE_VALUE, DCMSR_CODE_MEANING, DCMSR_CODE_SCHEME, DCMSR_CDOE_SCHEME_VERSION

from ...servers import SonadorImagingServer
from ...servers.auth import SonadorGroup


class OrthancGroup:
	'''	Proxy model which provides methods/properties for working with objects in Orthanc that are
		owned by Sonador groups.
	'''
	def __init__(self, pacs, group, **kwargs):
		'''	Initialize the Orthanc group

			@input pacs (sonador.servers.SonadorImagingServer): imaging server instance
			@input group (sonador.servers.auth.SonadorGroup): group instance
		'''
		self.pacs = pacs
		self.group = group

	@property
	def pk(self):
		return self.group.pk

	@property
	def dicomweb_resource_url(self):
		return posixpath.join(self.pacs.dicomweb_root, 'groups', str(self.group.pk))

	@property
	def resource_url(self):
		'''	Imaging server base URL for the group
		'''
		return posixpath.join('/groups', str(self.group.pk))

	@property
	def tags_url(self):
		'''	URL for tags associated with the group
		'''
		return posixpath.join(self.resource_url, 'tags')

	@property
	def display_attributes_url(self):
		'''	URL for the display attributes curated by the group
		'''
		return posixpath.join(self.resource_url, 'display-attributes')

	@property
	def tags_modelcollection_class(self):
		from .tags import TagCollection
		return TagCollection

	@property
	def display_attributes_modelcollection_class(self):
		from .displayattrs import DisplayAttributeCollection
		return DisplayAttributeCollection

	def fetch_tags(self, **kwargs):
		'''	Retrieve tags for the group
		'''
		return self.tags_modelcollection_class.fetch(parent=self, **kwargs)

	def create_tag(self, tag, **kwargs):
		'''	Create a tag for the group

			@input tag (highdicom.CodedConcept): coded concept to be used for the tag
		'''
		# Unpack coded concept values to JSON
		_data = {
			DCMSR_CODE_VALUE: tag.value, 
			DCMSR_CODE_MEANING: tag.meaning, 
			DCMSR_CODE_SCHEME: tag.scheme_designator,
		}
		if tag.scheme_version:
			_data[DCMSR_CDOE_SCHEME_VERSION] = tag.scheme_version

		# Convert coded concept data to dict
		return self.tags_modelcollection_class.create(self, _data, **kwargs)

	def get_tag(self, uid, *args, **kwargs):
		'''	Retrieve a tag instance

			@input uid (str): Orthanc resource ID (resource.pk) of the tag to be retrieved

			@returns tag instance
		'''
		return self.tags_modelcollection_class.fetch_modelinstance(self, uid, *args, **kwargs)

	def fetch_display_attributes(self, **kwargs):
		'''	Retrieve the display attributes of the group
		'''
		return self.display_attributes_modelcollection_class.fetch(parent=self, **kwargs)

	def create_display_attribute(self, code, label=None, **kwargs):
		'''	Add a display attribute to the group

			@input code (str): DICOM tag code, e.g. `0018,1030`, `(0018,1030)` or `00181030`
			@input label (str): optional display label
		'''
		_data = { 'Code': code }
		if label:
			_data['Label'] = label

		return self.display_attributes_modelcollection_class.create(self, _data, **kwargs)

	def get_display_attribute(self, uid, *args, **kwargs):
		'''	Retrieve one display attribute of the group by its ID
		'''
		return self.display_attributes_modelcollection_class.fetch_modelinstance(self, uid, *args, **kwargs)
