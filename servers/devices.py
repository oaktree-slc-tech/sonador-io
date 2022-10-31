import posixpath
from collections import OrderedDict

from ..remote import fetch_sonador_data_collection

from .base import OrthancServerBase, ImagingServerChildBaseObject, ImagingServerChildCollection
from .dicom import DicomImagingModality, DicomImagingModalityCollection, \
	RemoteDICOMwebServer, RemoteDICOMwebServerCollection


CLINICAL_GATEWAY_ENV_OUTPUT_COLUMNS = OrderedDict((
		('server', 'Imaging Server ID'),
		('key', 'Variable Key'),
		('value', 'Variable Value'),
		('active', 'Active'),
	))


class ClinicalGatewayEnvironmentVariable(ImagingServerChildBaseObject):
	'''	Sonador Clinical Gateway shared environment variable
	'''
	tabulate_output_columns = CLINICAL_GATEWAY_ENV_OUTPUT_COLUMNS
	env_urlroot = 'env'


class ClinicalGatewayEnvironmentVariableCollection(ImagingServerChildCollection):
	'''	Collection of gateway shared environment variables
	'''
	model = ClinicalGatewayEnvironmentVariable


class LocalEnvMixin(object):
	'''	Mixin class which can be used with OrthancServerBase instances that will be used
		in environments where they need to interact with a locally running instance
		of Orthanc (such as the Sonador Clnical Gateway).
	'''

	def _localenv_attr(self, name):
		'''	Attempt to retrieve a property that may be defined in the remote _objectdata
			or in the server.localenv. Raises an AttributeError if unable to find
			the attribute in either location. self._objectdata takes precedence
			if the attribute is defined in both locations.
		'''
		# Attempt to retrieve from local object data
		if self._objectdata.get(name) is not None:
			return self._objectdata.get(name)

		# Fallback to server.localenv
		elif isinstance(self.server.localenv, dict) and name in self.server.localenv:
			return self.server.localenv[name]

		raise AttributeError('%s has no attribute %s in local data or server localenv' % (type(self), name))


CLINICAL_GATEWAY_OUTPUT_COLUMNS = OrderedDict((
		('pk', 'ID'),
		('name', 'Gateway Name'),
		('description', 'Description'),
	))


class ClinicalGateway(LocalEnvMixin, OrthancServerBase):
	''' Object representation of a Sonador Clinical Gateway.
	'''
	fetch_endpoint = '/gateway/api/device'
	tabulate_output_colums = CLINICAL_GATEWAY_OUTPUT_COLUMNS

	def orthanc_request_headers(self, *args, **kwargs):
		return kwargs.get('headers', {})

	@property
	def modality_datacollection_class(self):
		return DicomImagingModalityCollection

	@property
	def dicomweb_remote_datacollection_class(self):
		return RemoteDICOMwebServerCollection

	@property
	def env_datacollection_class(self):
		return ClinicalGatewayEnvironmentVariableCollection

	@property
	def server_label(self):
		if getattr(self, 'name', None):
			return '%s (%s)' % (self.name, self.pk)
		return self.pk

	@property
	def scheme(self):
		'''	Connection scheme for the gateway Orthanc instance.
		'''
		return self._localenv_attr('scheme')

	@property
	def hostname(self):
		'''	Hostname for the gateway Orthanc instance.
		'''
		return self._localenv_attr('hostname')

	@property
	def port(self):
		'''	HTTP/REST port for the gateway Orthanc instance.
		'''
		return self._localenv_attr('port')

	def fetch_environment_variables(self, **kwargs):
		'''	Retrieve the environment variables for the gateway
		'''
		return fetch_sonador_data_collection(self.server, self.env_datacollection_class,
			data_collection_endpoint=posixpath.join(
				self.fetch_endpoint, self.pk, self.env_datacollection_class.model.env_urlroot),
			pacs=self, **kwargs)

	@property
	def env_collection(self):
		'''	Cached property for retrieving the environment variables associated with the gateway
		'''
		if getattr(self, '_env_collection', None) is None:
			setattr(self, '_env_collection', self.fetch_environment_variables())

		return self._env_collection

	@property
	def env(self):
		'''	Dictionary representation of the Gateway environment
		'''
		return OrderedDict((v.key, v.value) for v in self.env_collection)
	