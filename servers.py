import six, requests, json, csv, collections, logging, posixpath
from urllib.parse import urlencode

from tabulate import tabulate
from collections import OrderedDict

from client import auth as guru_auth
from client.utils.urls import build_url
from client.utils.object import pick
from client.utils.microservices import server_controloperation_json_response, RemotePage

from .helpers import request_client_error, fetch_sonador_session_token
from .remote import SonadorBaseObject, SonadorObjectCollection, fetch_sonador_data_collection

logger = logging.getLogger(__name__)



# PACS Imaging Servers

IMAGING_SERVER_OUTPUT_COLUMNS = OrderedDict((
		('pk', 'ID'),
		('name', 'Imaging Server Name'),
		('default', 'Default Server'),
		('hostname', 'Hostname'),
		('port', 'Port'),
		('description', 'Description'),
	))


class SonadorImagingServer(SonadorBaseObject):
	'''	Object representation of a Sonador imaging server
	'''
	fetch_endpoint = '/visionaire/api/pacs'
	tabulate_output_columns = IMAGING_SERVER_OUTPUT_COLUMNS
	details_exclude = ('token',)

	@property
	def netloc(self):
		'''	Return network location for the server (hostname:port)
		'''
		if getattr(self, 'port', None):
			return '%s:%s' % (self.hostname, self.port)

		return self.hostname

	@property
	def internal_netloc(self):
		'''	Return network location for the server (hostname:port)
		'''
		# Retrieve internal hostname/port with fallback to external hostname/port
		hostname = getattr(self, 'internal_hostname', None) or self.hostname
		port = getattr(self, 'internal_port', None) or getattr(self, 'port', None)

		if port:
			return '%s:%s' % (hostname, port)
		
		return hostname

	def orthanc_apiurl(self, resource_endpoint):
		'''	Create URL for Orthanc API call
		'''
		if self.server.internal_dns:
			return build_url(self.internal_scheme, self.internal_netloc, resource_endpoint)

		return build_url(self.scheme, self.netloc, resource_endpoint)

	def orthanc_request_headers(self, headers=None):
		'''	Add headers required by Orthanc API
		'''
		headers = headers or {}
		
		# Add API token and token type as the "Authorization" header.
		# If a token (API or session based) has not yet been populated,
		# it will be retrieved dynamically as part of the first access of the API token
		# property. The token type wiill be populated at that time.
		atoken, atype = self.server.apitoken, self.server.apitoken_type
		headers['Authorization'] = '%s %s' % (atype, atoken)

		return headers

	@property
	def dicom_modalities(self):
		'''	DICOM modalities associated with the imaging server
		'''
		if getattr(self, '_dicom', None) is None:
			setattr(self, '_dicom', 
				fetch_sonador_data_collection(self.server, DicomImagingModalityCollection,
					data_collection_endpoint=posixpath.join(self.fetch_endpoint, self.pk, 'dicom')))

		return self._dicom

	@property
	def dicomweb_remotes(self):
		'''	Remote DICOMweb  instances associated with the imaging server
		'''
		if getattr(self, '_dweb', None) is None:
			setattr(self, '_dweb', 
				fetch_sonador_data_collection(self.server, RemoteDICOMwebServerCollection,
					data_collection_endpoint=posixpath.join(self.fetch_endpoint, self.pk, 'dicom-web')))

		return self._dweb

	def upload_image(self, img, headers=None, retry_count=0, retry_limit=3):
		'''	Upload the provided image to via Orthanc REST API

			@input img (file-like object): Image data to be uploaded
			@input headers (dict, default=empty dict): Headers to be added to the upload request

			@returns requests.Response
		'''
		r = requests.post(
			self.orthanc_apiurl('instances'), files={ 'file': img }, 
			headers=self.orthanc_request_headers(headers=headers))

		if not r.ok:

			# Retry upload
			if retry_count < retry_limit:

				logger.warning('Unable to upload image to PACS %s. Status code: %s. Retry transfer: %s/%s.'
					% (self.pk, r.status_code, retry_count+1, retry_limit))

				# Reset position of image before attempting upload
				img.seek(0)
				r = self.upload_image(img, headers=headers, retry_count=retry_count+1, retry_limit=retry_limit)

			# Retry limit exceeded: notify user of failed transfer
			else:  request_client_error('Unable to upload image to PACS %s. Status code: %s.' % (self.pk, r.status_code), r)

		return r


class SonadorImagingServerCollection(SonadorObjectCollection):
	'''	Collection of Sonador PACS imaging servers
	'''
	model = SonadorImagingServer


class ImagingServerChildMixin(object):
	'''	Mixin object providing properties and methods common to objects associated
		with Sonador managed PACS imaging servers.
	'''
	@property
	def imaging_server(self):
		return self._objectdata.get('server')


class ImagingServerChildCollection(SonadorObjectCollection):
	'''	Collection which can be used to work with data models associated
		with Sonador managed PACS imaging servers
	'''
	def __init__(self, *args, **kwargs):
		self.imaging_server = kwargs.pop('imaging_server', None)
		super(ImagingServerChildCollection, self).__init__(*args, **kwargs)



# PACS Data Excahnge: PACS DICOM and DICOMweb

DICOM_MODALITY_OUTPUT_COLUMNS = OrderedDict((
		('imaging_server', 'Imaging Server ID'),
		('pk', 'Modality ID'),
		('name', 'DICOM Modality Name'),
		('aet', 'AET'),
		('host', 'Hostname'),
		('port', 'Port'),
	))


class DicomImagingModality(ImagingServerChildMixin, SonadorBaseObject):
	'''	DICOM imaging modalities associated with a server
	'''
	tabulate_output_columns = DICOM_MODALITY_OUTPUT_COLUMNS
	details_exclude = ('server', 'token')


class DicomImagingModalityCollection(ImagingServerChildCollection):
	'''	Collection of DICOM imaging modalities associated with a server
	'''
	model = DicomImagingModality


DICOMWEB_OUTPUT_COLUMNS = OrderedDict((
		('imaging_server', 'Imaging Server ID'),
		('pk', 'Remote Server ID'),
		('name', 'Server Name'),
		('hostname', 'Hostname'),
		('port', 'Port'),
		('description', 'Description'),
	))


class RemoteDICOMwebServer(ImagingServerChildMixin, SonadorBaseObject):
	'''	Remote DICOMweb server associated with a Sonador managed PACS imaging server
	'''
	tabulate_output_columns = DICOMWEB_OUTPUT_COLUMNS
	details_exclude = ('server', 'username', 'password')


class RemoteDICOMwebServerCollection(ImagingServerChildCollection):
	'''	Collection of DICOMweb servers
	'''
	model = RemoteDICOMwebServer


def sonador_apitoken_fetch(sonador_server, output_dest, verify=False):
	'''	Fetch API credentials for the server
	'''
	stoken = fetch_sonador_session_token(sonador_server, verify=verify)
	logger.info('Session token for API Access ID: %s' % sonador_server.access_id)
	output_dest.write(json.dumps(stoken))
