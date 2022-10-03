from io import BytesIO
from pprint import pprint
import time
import six, requests, json, csv, collections, logging, posixpath, zipfile
from urllib.parse import urlencode

from tabulate import tabulate
from collections import OrderedDict
from collections import Iterable

from client import apisettings as gcapicodes
from client import auth as guru_auth
from client.utils.urls import build_url
from client.utils.object import pick, omit
from client.utils.microservices import RemotePage, server_controloperation_json_response
from client.utils.format import formerrors2str
from client.utils.conversion import str2bool
from client.errors import ClientOperationError, ConfigurationError
from client.remote import RemoteServer, request_client_error

from .apisettings import IMAGING_SERVER_RESOURCE_PATIENT, IMAGING_SERVER_RESOURCE_STUDY, \
	IMAGING_SERVER_RESOURCE_SERIES, IMAGING_SERVER_RESOURCE_IMAGE, IMAGING_SERVER_RESOURCE_SUPPORTED, \
	DCMHEADER_MODALITY, DCM_MODALITY_SR, DCM_MODALITY_SEG, DCM_VERSION_2021b
from .serialization import json_datetime_parser
from .helpers import request_client_error, fetch_sonador_session_token, API_ACCESS_TOKEN, OAUTH_TOKEN_RESPONSE_TYPE, \
	OAUTH_TOKEN_IDTOKEN_RESPONSE_TYPE, OAUTH_ACCESS_TOKEN, OAUTH_TOKEN_TYPE, OAUTH_TOKEN_TYPE_BEARER, OAUTH_EXPIRATION
from .remote import SonadorBaseObject, SonadorObjectCollection, \
	fetch_sonador_data_collection, fetch_sonador_dataobject

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


class SonadorServer(RemoteServer):
	'''	Sonador server client
	'''

	def __init__(self, sonador_url, access_id=None, secret_key=None, apitoken=None, verify=False,
			internal_dns=False):
		'''	Initialize the server instance

			@input sonador_url (str): Full URL to the server instance
			@input access_id (str): API Access ID for the server
			@input secret_key (str): Secret key associated with the specified access ID
		'''
		self.internal_dns = internal_dns
		
		# Auth: API token and token type
		self.sonador_authdata = None

		# Initialize parent class
		super().__init__(sonador_url, access_id=access_id, secret_key=secret_key, apitoken=apitoken, verify=verify)

	@property
	def apitoken(self):
		if self._apitoken is None and self.sonador_authdata is None:
			self.sonador_authdata = self.get_session_token(verify=self.verify)
			self._apitoken = self.sonador_authdata.get(OAUTH_ACCESS_TOKEN)
			self.apitoken_type = self.sonador_authdata.get(OAUTH_TOKEN_TYPE)

		return self._apitoken

	def apiurl(self, resource_endpoint, method=None):
		'''	Create a Sonador API URL which includes the parameters (AccessID, Signatures, and expirations)
			required to access a secure resource.
		'''
		# Add API token as a request header (if present)
		if self.apitoken_type == API_ACCESS_TOKEN and self.apitoken:
			return build_url(self.scheme, self.netloc, resource_endpoint)

		# Add optional URL signature components
		url_kwargs = {}
		if method:
			url_kwargs['method'] = method

		return build_url(self.scheme, self.netloc,
			guru_auth.create_signed_url(self.access_id, self.secret_key, resource_endpoint, **url_kwargs))

	def request_headers(self, headers=None):
		''' Add headers to a Sonador API request. If an API token is used to access Sonador
			resources, the token and corresponding heder are added to the dictionary.

			@input headers (dict, default=empty dict): Dictionary to which the Sonador auth
				headers should be added.

			@returns dict
		'''
		headers = headers or {}

		# Add API token as a request header (if present)
		if self.apitoken_type == API_ACCESS_TOKEN and self.apitoken:
			headers.update({ API_ACCESS_TOKEN: self.apitoken })

		return headers

	def sonador_apiurl(self, *args, **kwargs):
		'''	DEPRECATED: Compatibility method kept for code which uses the Sonador client. Use apiurl instead.
		'''
		return self.apiurl(*args, **kwargs)

	def sonador_request_headers(self, *args, **kwargs):
		'''	DEPRECATED: Compatibility method kept for code which uses the Sonador client. Use request_headers instead.
		'''
		return self.request_headers(*args, **kwargs)

	def get_imageserver(self, uid, verify=None, imageserver_datamodel_class=None):
		'''	Retrieve model data for the specified Imaging/PACS server

			@input uid (str): Sonador UID/pk for the imaging server.
			@input verify (bool, default=server default): Toggles whether SSL certificates
				should be validated as part of the request. If no value is passed, 
				the default setting included in the Sonador server will be used.
			
			@returns SonadorImagingServer model instance
		'''
		from .remote import fetch_sonador_dataobject
		if imageserver_datamodel_class is None:
			from .servers import SonadorImagingServer
			imageserver_datamodel_class = SonadorImagingServer
		
		if verify is None:
			verify = self.verify

		return fetch_sonador_dataobject(self, imageserver_datamodel_class, uid, verify=verify)

	def get_dataservice(self, uid, verify=None, dataservice_datamodel_class=None):
		'''	Retrieve model data for the specified Data Service

			@input uid (str): Sonador UID/pk for the data service
			@input verify (bool, default=server default): Toggles whether SSL certificates
				should be validated as part of the request. If no value is passed,
				the default setting included in the Sonador server will be used.
			
			@returns DataService model instance
		'''
		from .remote import fetch_sonador_dataobject
		from .services import DataService
		
		dataservice_datamodel_class = dataservice_datamodel_class or DataService
		if verify is None:
			verify = self.verify
		
		return fetch_sonador_dataobject(self, dataservice_datamodel_class, uid, verify=verify)
	
	def get_session_token(self, verify=None, *args, **kwargs):
		'''	Retrieve a session token using the provided acess ID/secret
		'''
		if verify is None:
			verify = self.verify
		
		return fetch_sonador_session_token(self, verify=verify)


class SonadorImagingServer(SonadorBaseObject):
	'''	Object representation of a Sonador imaging server
	'''
	fetch_endpoint = '/visionaire/api/pacs'
	tabulate_output_columns = IMAGING_SERVER_OUTPUT_COLUMNS
	details_exclude = ('token',)
	tools_endpoint = 'tools'

	def __init__(self, *args, resource_cache=None, **kwargs):

		# Cache to be used when fetching resources
		self.resource_cache = resource_cache or {}
		super().__init__(*args, **kwargs)

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
	
	def bulk_anonymize(self, resources: list, asynchronous: bool=False, dicom_version: str=DCM_VERSION_2021b, 
			force: bool=True, keep: list=None, keep_private_tags: bool=False, keep_source: bool=True, 
			permissive: bool=True, priority: int=0, private_creator: str=None, remove: list=None,
			replace: list=None, bulk_anonymize_dict: dict=None, headers=None, verify=None, 
			merge_anonymized: bool= False, **kwargs):
		'''	Start a job that will anonymize all DICOM patients, studies, series, or instances 
			whose identifiers are provided in the Resources field. Anonymization erases all tags specified 
			in Table E.1-1 from PS 3.15 of the DICOM standard. 
			(Refer to http://dicom.nema.org/medical/dicom/current/output/chtml/part15/chapter_E.html#table_E.1-1.)
			 
			@input resources (list): List of the Orthanc identifiers of the patients, studies, series, and instances
				to be anonymized.
			@input asynchronous (bool, default=True): If true, run the job in asynchronous mode, which means that the REST API 
				call will immediately return, reporting the identifier of a job. Prefer this flavor wherever possible.
			@input dicom_version (str, default='2021b'): version of the DICOM standard to be used
				for anonymization.
			@input keep (list, default=None): List of DICOM tags whose value must not be destroyed by the anonymization.
			@input keep_private_tags (bool, default=True): Keep the private tags from the DICOM instances.
			@input keep_source (bool, default=True): If set to false, instructs Orthanc to the remove original resources. 
				By default, the original resources are kept in Orthanc.
			@input force (bool, default=False): Allow the modification of tags related to DICOM identifiers, at the risk of breaking 
				the DICOM model of the real world.
			@input permissive (bool, default=True): If true, ignore errors during the individual steps of the job.
			@input priority (int, default=0): In asynchronous mode, the priority of the job. The lower the value, the higher the priority.
			@input private_creator (str, default=None): The private creator to be used for private tags in Replace.
			@input remove (list, default=None): List of additional tags to be removed from the DICOM instances.
			@input replace (dict, default=None): Associative array to change the value of some DICOM tags in the DICOM instances.	 

			@returns OrthancJob if async is True, request.Response otherwise
		'''
		bulk_anonymize_dict = bulk_anonymize_dict or {}

		if replace and not isinstance(replace, dict):
			raise TypeError('Unable to anonymize DICOM resource, replace terms must be submitted as a dictionary.')
		if remove and not isinstance(remove, Iterable):
			raise TypeError('Unable to remove DICOM tags, remove terms must be submitted as an iterable.')

		if verify is None:
			verify = self.server.verify

		# Structure of anonymize request
		bulk_anonymize_dict.update({
			'Asynchronous': asynchronous, 
			'Force': force,
			'KeepSource': keep_source,
			'Permissive': permissive,
			'Priority': priority,
			'KeepPrivateTags': keep_private_tags,
			'Resources': resources,
			'DicomVersion': dicom_version
		})

		# Add options to request
		if replace:
			bulk_anonymize_dict.update({ 'Replace': replace })
		if remove:
			bulk_anonymize_dict.update({ 'Remove': remove })
		if keep:
			bulk_anonymize_dict.update({ 'Keep': keep })
		if private_creator:
			bulk_anonymize_dict.update({ 'PrivateCreator': private_creator })

		# Execute operation
		r = self._bulk_content_request(posixpath.join(self.tools_endpoint, 'bulk-anonymize'),
			bulk_anonymize_dict, headers=headers, verify=verify)
		
		returned_objects = []
		if asynchronous:
			response_json = r.json()
			from .imaging.orthanc.jobs import OrthancJob
			return self.get_imaging_resource(response_json['ID'], OrthancJob, headers=headers, **kwargs)
		
		else:
			
			# Returns the model object of the new resources created (based on the level executed)
			return r
			

	def bulk_delete(self, resources: list, headers: dict=None, verify: bool=False, **kwargs) ->dict:
		''' Delete all of the provided DICOM patients, studies, series, and instances whose identifiers.

			@input resources (list): List of the Orthanc identifiers of the patients, studies, series, 
				instances of interest.

			@returns requests.Response
		'''
		if not resources:
			raise ValueError('You must set resources to be deleted')
        
		bulk_delete_dict = {'Resources': resources,}
		if verify is None:
			verify = self.server.verify

		# Execute operation
		r = self._bulk_content_request(posixpath.join(self.tools_endpoint, 'bulk-delete'),
			bulk_delete_dict, headers=headers, verify=verify)
		
		return r

	def create_archive(self, resources: list, asynchronous: bool=True, priority: int=0, transcode: str=None, headers: dict=None, 
			verify=None, create_archive_dict: dict=None, **kwargs) -> dict:
		''' Create a zip archive containing the requested DICOM resources (patients, studies, series, and instances).

            @input resources (list): Orthanc UIDs of resources to include in the archive file.
			@input asynchronous (boolean, default=False): 
			@input priority (integer, default=0): In asynchronous mode, the priority of the job. 
				The lower the value, the higher the priority.
			@input transcode(string, default=None): If present, the DICOM files in the archive 
				will be transcoded to the provided transfer syntax: https://book.orthanc-server.com/faq/transcoding.html
        	
        	@returns OrthancJob if async is True, otherwise zipfile.ZipFile archive.
		'''
		create_archive_dict = create_archive_dict or {}
		if verify is None:
			verify = self.server.verify

		# Create request structure
		create_archive_dict.update({ 
			'Asynchronous': asynchronous, 
			'Priority': priority,
			'Resources': resources,
		})

		if transcode:
			create_archive_dict.update({ 
				'Transcode': transcode 
			})

		# Execute operation
		r = self._bulk_content_request(posixpath.join(self.tools_endpoint, 'create-archive'),
			create_archive_dict, headers=headers, verify=verify, )

		# Initialize file archive from request data, attach the raw content of the request
		# to the archive
		if asynchronous:
			response_json = r.json()
			from .imaging.orthanc.jobs import OrthancJob
			return self.get_imaging_resource(response_json['ID'], OrthancJob, headers=headers, **kwargs)
		else:
			zbuffer = BytesIO(r.content)
			farchive = zipfile.ZipFile(zbuffer, mode='r')
			setattr(farchive, 'raw', zbuffer)

		return farchive
	
	def fetch_bulk_content(self, uids: list, full: bool=False, metadata: str=True, resource=None,
			short: bool=False, headers: dict=None, verify=None, bulk_content_dict: dict=None, cache=False, 
			rapid_lookup: bool=False, **kwargs) -> dict:
		''' Get the content all the DICOM patients, studies, series or instances whose identifiers are provided in 
            the Resources field, in one single call.

            @input uids (list): Orthanc resource UIDS (pk) of the Orthanc identifiers of the 
            	patients/studies/series/instances of interest.
			@input resource (string): Optional argument which specifies the level of interest (can be Patient, Study, 
				Series or Instance). Orthanc will loop over the items inside Resources, and explore upward or 
				downward in the DICOM hierarchy in order to find the level of interest.
			@input full (bool, default=False): If set to true, report the DICOM tags in full format 
				(tags indexed by their hexadecimal format, associated with their symbolic name and their value)
			@input metadata(bool, default=True): If set to true (default value), the metadata 
				associated with the resources will also be retrieved. 
			@input short (bool, default=False): If set to true, report the DICOM tags in hexadecimal format.
		'''	
		bulk_content_dict = bulk_content_dict or {}
		if verify is None:
			verify = self.server.verify

		# Create request structure
		bulk_content_dict.update({ 
			'Full': full, 
			'Metadata': metadata,
			'Resources': uids,
			'Short': short 
		})

		if resource:
			bulk_content_dict['Level'] = resource

		# Determine bulk endpoint URL to use
		bulk_endpoint = posixpath.join('cache', self.tools_endpoint, 'bulk-content') if rapid_lookup \
			else posixpath.join(self.tools_endpoint, 'bulk-content')

		# Execute operation
		logger.debug('Structure of bulk content request:\n%s' % json.dumps(bulk_content_dict))
		resources_response = self._bulk_content_request(
			bulk_endpoint, bulk_content_dict, headers=headers, verify=verify, cache=cache)
		resources = {}

		# Initialize resource model instances
		for rjson in resources_response.json():

			# Create resource type in response dictionary (if it does not already exist)
			if rjson.get('Type') and not rjson.get('Type') in resources:
				resources[rjson.get('Type')] = []

			# Separate resources by type
			resources[rjson['Type']].append(rjson)

		# Initialize collection instances for each resource type
		for rtype, rdata in resources.items():
			if not kwargs.get('pacs'):
				kwargs['pacs'] = self
			resources[rtype] = self.server._init_dataclass_from_json(
				self.get_resource_modelcollection_class(rtype), rdata, **kwargs)

		# Add resources to local cache
		if cache:

			# Iterate through collections and add items to resource cache
			for rtype, rdata in resources.items():
				for resource in rdata:
					self.resource_cache[resource.pk] = resource

		return resources
	
	def _bulk_content_request(self, bulk_content_url, bulk_content_dict: dict, 
			headers=None, verify=None, **kwargs):
		''' Function that wraps the tools endpoint and make requests against it, returning the response.
		'''
		bulk_request = requests.post(self.orthanc_apiurl(bulk_content_url), 
			json=bulk_content_dict, headers=self.orthanc_request_headers(headers=headers), verify=verify)
		
		if not bulk_request.ok:
			
			request_client_error(
				'Unable to retrieve/modify DICOM resource on server %s. Status code: %s.'
					% (self.server_label, bulk_request.status_code),
				bulk_request)

		logger.debug('Response from PACS imaging server:\n%s' % bulk_request.content)
		return bulk_request
	
	def bulk_modify(self, resources: list, replace: dict, asynchronous: bool=False, force: bool=False, keep: list=None, 
			keep_sources: list=True, level: str=None, permissive: bool=True, priority: int=0, 
			private_creator: str=None, remove: list=None, remove_private_tags=False, transcode: str=None, 
			bulk_modify_dict: dict=None, headers=None, verify=None, bring_parent: bool=False, **kwargs):
		'''	Start a job that will modify all the DICOM patients, studies, series or instances whose identifiers 
			are provided in the Resources field.
			 
			@input resources (list): List of the Orthanc identifiers of the patients/studies/series/instances of interest.
			@input replace (dict): Associative array to change the value of some DICOM tags in the DICOM instances. 
			 	Starting with Orthanc 1.9.4, paths to subsequences can be provided using the same syntax 
				as the dcmodify command-line tool (wildcards are supported as well).
			@input asynchronous (bool, default=False): If true, run the job in asynchronous mode, which 
			 	means that the REST API call will immediately return, reporting the identifier of a job. 
			 	Prefer this flavor wherever possible.
			@input force (boolean, default=False): Allow the modification of tags related to DICOM 
			 	identifiers, at the risk of breaking the DICOM model of the real world.
			@input keep (list, default=None): Keep the original value of the specified tags, to be 
			 	chosen among the StudyInstanceUID, SeriesInstanceUID and SOPInstanceUID tags. Avoid this 
			 	feature as much as possible, as this breaks the DICOM model of the real world.
			@input keep_sources (bool, default=True): If set to false, instructs Orthanc to the remove 
			 	original resources. By default, the original resources are kept in Orthanc.
			@input level (str, default=None): Level of the modification (Patient, Study, Series or Instance). 
			 	If absent, the level defaults to Instance, but is set to Patient if PatientID is modified, 
			 	to Study if StudyInstanceUID is modified, or to Series if SeriesInstancesUID is modified
			@input permissive (bool, default=True): If true, ignore errors during the individual steps of the job.
			@input priority (int, default=0): In asynchronous mode, the priority of the job. The lower the value, 
			 	the higher the priority.
			@input private_creator (string, default=None): The private creator to be used for private tags in Replace.
			@input remove (list, default=None): List of tags that must be removed from the DICOM instances. 
			 	Starting with Orthanc 1.9.4, paths to subsequences can be provided using the same syntax as 
			 	the dcmodify command-line tool (wildcards are supported as well).
			@input remove_private_tags (bool, default=False): Remove the private tags from the DICOM instances 
			 	(defaults to false).
			@input transcode (str, default=None): iterable ot tags to be removed outside of those
			 	specified in the standard.

			@returns request.Response
		'''
		bulk_modify_dict = bulk_modify_dict or {}
		if verify is None:
			verify = self.server.verify

		# Create request structure
		bulk_modify_dict.update({ 
			'Asynchronous': asynchronous, 
			'Force': force,
			'KeepSource': keep_sources,
			'Permissive': permissive,
			'Priority': priority,
			'RemovePrivateTags': remove_private_tags,
			'Replace': replace,
			'Resources': resources,
		})

		if keep:
			bulk_modify_dict.update({ 'Keep': keep })
		if level:
			bulk_modify_dict.update({ 'Level': level })
		if private_creator:
			bulk_modify_dict.update({ 'PrivateCreator': private_creator })
		if remove:
			bulk_modify_dict.update({ 'Remove': remove })
		if transcode:
			bulk_modify_dict.update({ 'Transcode': transcode })

		# Execute operation
		logger.debug('Structure of modification request:\n%s' % json.dumps(bulk_modify_dict))
		r = requests.post(self.orthanc_apiurl(posixpath.join(self.tools_endpoint, 'bulk-modify')), json=bulk_modify_dict,
			headers=self.orthanc_request_headers(headers=headers), verify=verify)
		if not r.ok:
			request_client_error(
				'Unable to modify resources %s on server %s. Status code: %s.'
					% (resources, self.server_label, r.status_code),
				r)

		# Initialize file archive from request data, attach the raw content of the request
		# to the archive
		returned_objects = []
		if asynchronous:
			response_json = r.json()
			from .imaging.orthanc.jobs import OrthancJob
			return self.get_imaging_resource(response_json['ID'], OrthancJob, headers=headers, **kwargs)
			
		else:
			#Returns the model object of the new resources created (based on the level executed)
			response_json = r.json()
			return_instances = {}
			for resource in response_json['Resources']:
				if resource['Type'] not in return_instances:
					return_instances.update({resource['Type']: []})
				
				return_instances[resource['Type']].append(resource['ID'])
				
			returned_objects.append(self.bulk_content(resources=return_instances[level], level=level))
			
			return returned_objects

	def get_resource_modelcollection_class(self, resource_type: str):
		'''	Retrieve the collection class for the provided resource type
		'''
		from .imaging.orthanc import IMAGING_SERVER_RESOURCE_DATAMODEL_COLLECTIONTYPES
		if not resource_type in IMAGING_SERVER_RESOURCE_DATAMODEL_COLLECTIONTYPES:
			raise ValueError('Invalid resource type: %s' % resource_type)

		return IMAGING_SERVER_RESOURCE_DATAMODEL_COLLECTIONTYPES.get(resource_type)
		
	def get_resource_model_class(self, resource_type: str):
		''' Get the resource class type for a given resource type
		'''
		return self.get_resource_modelcollection_class(resource_type).model

	@property
	def server_label(self):
		if getattr(self, 'name', None):
			'%s (%s)' % (self.name, self.pk)
		return self.pk

	def orthanc_apiurl(self, resource_endpoint, query_params=''):
		'''	Create URL for Orthanc API call
		'''
		if self.server.internal_dns:
			return build_url(self.internal_scheme, self.internal_netloc, resource_endpoint, query_params=query_params)

		return build_url(self.scheme, self.netloc, resource_endpoint, query_params=query_params)

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

	def fetch_dicom_modalities(self, **kwargs):
		'''	Retrieve the DICOM modalities associated with the imaging server
		'''
		return fetch_sonador_data_collection(self.server, DicomImagingModalityCollection,
			data_collection_endpoint=posixpath.join(self.fetch_endpoint, self.pk, 'dicom'), pacs=self, **kwargs)

	@property
	def dicom_modalities(self):
		'''	DICOM modalities associated with the imaging server (cached property)
		'''
		if getattr(self, '_dicom', None) is None:
			setattr(self, '_dicom', self.fetch_dicom_modalities())

		return self._dicom

	def fetch_dicomweb_remotes(self, **kwargs):
		'''	Retrieve the DICOMweb remotes associated with the imaging server
		'''
		return fetch_sonador_data_collection(self.server, RemoteDICOMwebServerCollection,
			data_collection_endpoint=posixpath.join(self.fetch_endpoint, self.pk, 'dicom-web'), pacs=self, **kwargs)

	@property
	def dicomweb_remotes(self):
		'''	Remote DICOMweb  instances associated with the imaging server (cached property)
		'''
		if getattr(self, '_dweb', None) is None:
			setattr(self, '_dweb', self.fetch_dicomweb_remotes())

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
					% (self.server_label, r.status_code, retry_count+1, retry_limit))

				# Reset position of image before attempting upload
				img.seek(0)
				r = self.upload_image(img, headers=headers, retry_count=retry_count+1, retry_limit=retry_limit)

			# Retry limit exceeded: notify user of failed transfer
			else:  request_client_error('Unable to upload image to PACS %s. Status code: %s.' % (self.server_label, r.status_code), r)

		return r

	def get_dicomweb_remote(self, rid, verify=None):
		'''	Retrieve DICOMweb remote instance
		'''
		if verify is None:
			verify = self.server.verify

		return fetch_sonador_dataobject(self.server, RemoteDICOMwebServer, rid, verify=verify, pacs=self,
			dataobject_endpoint=posixpath.join(self.fetch_endpoint, self.pk, 'dicom-web', rid))

	def get_imaging_resource(self, rid, resource_type, headers=None, verify=None, cache=False, **kwargs):
		'''	Retrieve the requested resource

			@input rid (str): Orthanc ID (resource.pk) of the resource to be retrieved.
			@input cache (bool, default=False): toggles whether to retrieve a cached copy of the resource.
				If True, the imaging server instance will store a reference to the imaging resource
				which will be used in subsequent calls to `get_imaging_resource`.
		'''
		# Return resource instance from local cache
		if cache and rid in self.resource_cache:
			return self.resource_cache[rid]

		if verify is None:
			verify = self.server.verify

		r = requests.get(self.orthanc_apiurl(posixpath.join(resource_type.fetch_endpoint, rid)),
				headers=self.orthanc_request_headers(headers=headers), verify=verify)
		if not r.ok:
			request_client_error('Unable to retrieve requested resource %s instance %s. Status code: %s'
				% (rid, resource_type, r.status_code), r)

		# Retrieve resource instance
		if not kwargs.get('pacs'): 
			kwargs['pacs'] = self
		resource = self.server._init_dataclass(resource_type, r, **kwargs)

		# Cache local copy
		if cache:
			self.resource_cache[rid] = resource
		
		return resource

	def get_patient(self, pid, headers=None, cache=False, **kwargs):
		'''	Retrieve patient data for the specified UID
		'''
		from .imaging.orthanc import ImagingPatient
		return self.get_imaging_resource(pid, ImagingPatient, headers=headers, cache=cache, **kwargs)

	def get_study(self, sid, headers=None, cache=False, **kwargs):
		'''	Retrieve a study instance
		'''
		from .imaging.orthanc import ImagingStudy
		return self.get_imaging_resource(sid, ImagingStudy, headers=headers, cache=cache, **kwargs)

	def get_series(self, rid, headers=None, cache=False, **kwargs):
		'''	Retrieve a series instance 

			@input rid (str): Orthanc resource ID (resource.pk) of the imaging series to be retrieved.
			@input cache (bool, default=False): toggles whether to retrieve a cached copy of the resource.
				If True, the imaging server instance will store a reference to the imaging series which
				will be used in subsequent calls to `get_series`.
		'''
		from .imaging.orthanc import ImagingSeries
		return self.get_imaging_resource(rid, ImagingSeries, headers=headers, cache=cache, **kwargs)

	def get_dcm_instance(self, rid, headers=None, cache=False, **kwargs):
		'''	Retrieve a DCM instance

			@input rid (str): Orthanc resource ID (resource.pk) of the DICOM instance to be retrieved.
			@input cache (bool, default=False): toggles whether to retrieve a cached copy of the resource.
				If True, the imaging server instance will store a reference to the imaging series
				which will be used in subsequent calls to `get_dcm_instance`.
		'''
		from .imaging.orthanc import DcmInstance
		return self.get_imaging_resource(rid, DcmInstance, headers=headers, cache=cache, **kwargs)

	def query(self, sfilter, expand=True, limit=None, offset=None, query=None, headers=None, verify=None, 
			resource=IMAGING_SERVER_RESOURCE_SERIES, resource_modelcollection_class=None, 
			rapid_lookup=None, bulkpopulate_related=False, bulkpopulate_options=None, **kwargs):
		'''	Submit a query to Orthanc with the provided filter terms

			@input sfilter (dict): Terms to be included in the request
			@input expand (bool, default=True): Desired response from Orthanc. If True, the full
				record listing will be retrieved. If False, only the resource IDs will be returned.
			@input resource (str, default='Series'): Type of resource for which the query should be executed.
			@input limit (int, default=None): Number of records which should be included in the response.
				If None, Orthanc will retrieve all records matching the query.
			@input offset (int, default=None): Any offset to apply to the record list. Used together
				with limit to paginate query results.
			@input query (dict, default=new dict): Existing dictionary structure to be expanded with 
				the provided search query.
			@input headers (dict, default=new dict): Headers to be included with the query request.
			@input rapid_lookup (bool or None, default=None): Use the Orthanc/Sonador cache API to perform queries.
				(The resource cache is a retrieved from a REST endpoint and is distinct from the local image server cache.)
				Cache API queries are faster than the `/tools/find` but are "eventually consistent"
				and may return different results than the traditional endpoint. True will use resource cache
				endpoints and indicate that linked resources should also cache endpoints when calling query methods.
				False will set a strong preference against use of the cache (also propagates to linked resources),
				None will avoid use of cache endpoints but does not propagate to child resources.
			@input bulkpopulate_related (bool, default=False): toggles whether to call the bulkpopulate_related method
				on the results collection, which is able to fetch related models for the collection.
			@input bulkpopulate_options (dict, default=None): options to be passed to the bulk populate method.
				Refer to the documentation to the resource collection bulk populate methods.

			@returns iterable of resource IDs if expanded is False, collection of the matching resource type if 
				expanded is True
		'''
		from .imaging.orthanc import IMAGING_SERVER_RESOURCE_DATAMODEL_COLLECTIONTYPES
		if not isinstance(sfilter, dict):
			raise TypeError('Unable to execute query, terms must be submitted as a dictionary')
		if not resource in IMAGING_SERVER_RESOURCE_SUPPORTED:
			raise ValueError('Unable to execute query, invalid resource type: %s' % resource)

		# Retrieve resource model class
		if resource_modelcollection_class is None:
			resource_modelcollection_class = IMAGING_SERVER_RESOURCE_DATAMODEL_COLLECTIONTYPES.get(resource)

		# Check resource model properties to ensure that they are compatible with the request type.
		if rapid_lookup and not hasattr(resource_modelcollection_class.model, 'cache_queryurl'):
			raise ConfigurationError('Unable to use Sonador cache endpoint for query for resource %s' 
				% resource_modelcollection_class.model.__name__)

		if verify is None:
			verify = self.server.verify

		# Create query structure
		query = query or {}
		query.update({
			'Level': resource, 'Expand': expand, 'Query': sfilter
		})
		if limit is not None:
			query['Limit'] = limit
		if offset is not None:
			query['Since'] = offset

		# Orthanc query structure
		logger.debug('Orthanc query:\n%s' % json.dumps(query))

		# Execute query
		r = requests.post(
			self.orthanc_apiurl(resource_modelcollection_class.model.cache_queryurl) if rapid_lookup else self.orthanc_apiurl('tools/find'), 
			json=query, headers=self.orthanc_request_headers(headers=headers))
		if not r.ok:
			request_client_error('Unable to execute resource query to PACS %s. Status code: %s.' % (self.server_label, r.status_code), r)

		# Parse response
		if not kwargs.get('pacs'):
			kwargs['pacs'] = self
		rcollection = self.server._init_dataclass(resource_modelcollection_class, r, rapid_lookup=rapid_lookup, **kwargs) if expand \
			else self.server._parse_apiresponse_json(r)

		# Populate related resources
		if bulkpopulate_related and callable(getattr(rcollection, 'bulkpopulate_related', None)):
			rcollection.bulkpopulate_related(verify=verify, rapid_lookup=rapid_lookup, **(bulkpopulate_options or {}))
		elif bulkpopulate_related and not callable(getattr(rcollection, 'bulkpopulate_related', None)):
			logger.warning(
				'Unable to retrieve related models for collection type "%s". Invalid bulkpopulate_related method.' % type(rcollection))

		return rcollection

	def _check_query_structure(self, sfilter):
		'''	Check the query structure to ensure that it is well formed
		'''
		if not isinstance(sfilter, dict):
			raise ValueError('Invalid resource query type: %s. Resource queries must be a dictionary.' % type(sfilter))

	def query_patient(self, sfilter, **kwargs):
		'''Query patient resources on the imaging server. (Wrapper function for "query".)
		'''	
		self._check_query_structure(sfilter)
		return self.query(sfilter, resource=IMAGING_SERVER_RESOURCE_PATIENT, **kwargs)

	def query_study(self, sfilter, **kwargs):
		'''	Query study resources on the imaging server.  (Wrapper function for "query".)
		'''
		self._check_query_structure(sfilter)
		return self.query(sfilter, resource=IMAGING_SERVER_RESOURCE_STUDY, **kwargs)

	def query_series(self, sfilter, **kwargs):
		'''	Query series resources on the imaging server. (Wrapper function for "query".)
		'''
		self._check_query_structure(sfilter)
		return self.query(sfilter, resource=IMAGING_SERVER_RESOURCE_SERIES, **kwargs)

	def query_sr(self, sfilter, **kwargs):
		'''	Query DICOM-SR resources on the imaging server. (Wrapper function for "query".)
		'''
		from .imaging.orthanc.sr import DcmSRSeriesCollection

		self._check_query_structure(sfilter)
		sfilter.update({ DCMHEADER_MODALITY: DCM_MODALITY_SR })

		return self.query(sfilter, resource=IMAGING_SERVER_RESOURCE_SERIES, 
			resource_modelcollection_class=DcmSRSeriesCollection, **kwargs)

	def query_seg(self, sfilter, **kwargs):
		'''	Query DICOM-SEG resources on the imaging server. (Wrapper function for "query".)
		'''
		from .imaging.orthanc.seg import DcmSegmentationSeriesCollection

		self._check_query_structure(sfilter)
		sfilter.update({ DCMHEADER_MODALITY: DCM_MODALITY_SEG })

		return self.query(sfilter, resource=IMAGING_SERVER_RESOURCE_SERIES,
			resource_modelcollection_class=DcmSegmentationSeriesCollection, **kwargs)

	def fetch_jobs(self, verify=None, headers=None, limit=None, offset=None, expand=True, **kwargs):
		'''	Retrieve the processing jobs for the server
		'''
		from .imaging.orthanc.jobs import OrthancJobCollection
		
		if verify is None:
			verify = self.server.verify
		
		# Retrieve jobs
		r = requests.get(self.orthanc_apiurl(OrthancJobCollection.model.fetch_endpoint, query_params={ 'expand': expand }), 
			headers=self.orthanc_request_headers(headers=headers), verify=verify)
		if not r.ok:
			request_client_error('Unable to retrieve jobs from PACS %s. Status code: %s.' % (self.server_label, r.status_code), r)

		# Parse response
		if not kwargs.get('pacs'):
			kwargs['pacs'] = self
		return self.server._init_dataclass(OrthancJobCollection, r, **kwargs)

	def get_job(self, jid, headers=None, **kwargs):
		'''	Retrieve a processing job instance
		'''
		from .imaging.orthanc.jobs import OrthancJob
		return self.get_imaging_resource(jid, OrthancJob, headers=headers, **kwargs)

	def dicomweb_push(self, rdweb, resources, op=None, headers=None, async_transfer=True, priority=None):
		'''	Push resources from the current imaging server to the provided remote DICOMweb instance

			@input rdweb (RemoteDICOMwebServer): Remote DICOMweb instances to which the resources
				should be pushed.
			@input resources (iterable of Orthanc resource IDs): IDs of the resources to be pushed
				to the remote DICOMweb instance.
		'''
		# Ensure the provided DICOMweb instance is associated with the imaging server
		if self.pk != rdweb.pacs.pk:
			raise ValueError(('Unable to push resources, DICOMweb %s instance is associated with another '
				+ 'imaging server: %s. Current server: %s') % (rdweb.pk, rdweb.pacs.server_label, self.server_label))

		# Create resource operation request
		op = rdweb._remote_resource_operation_request(
			resources, op=op, async_transfer=async_transfer, priority=priority)

		# Execute request
		r = requests.post(self.orthanc_apiurl(posixpath.join(rdweb.dicomweb_urlbase, 'stow')), json=op,
			headers=self.orthanc_request_headers(headers=headers))
		if not r.ok:
			request_client_error('Unable to push resources to DICOMweb for %s on PACS %s. Status code: %s.' 
					% (rdweb.pk, self.server_label, r.status_code), 
				r)

		# Parse response
		return rdweb._parse_remote_resource_operation(r, async_transfer)


class SonadorImagingServerCollection(SonadorObjectCollection):
	'''	Collection of Orthanc/PACS imaging servers managed by Sonador
	'''
	model = SonadorImagingServer


# Orthanc DICOM Server Base Objects

class ImagingServerBaseObject(SonadorBaseObject):
	''' Data object associated with a PACS server. Includes a reference to the server
		from which the object came.
	'''
	def __init__(self, *args, **kwargs):
		self.pacs = kwargs.pop('pacs', None)
		self.resource_cache_lookup = kwargs.pop('rapid_lookup', None)
		super().__init__(*args, **kwargs)


class ImagingServerChildCollection(SonadorObjectCollection):
	'''	Collection which can be used to work with data models associated
		with Sonador managed PACS imaging servers
	'''
	def __init__(self, *args, **kwargs):
		'''	
		'''
		self.pacs = kwargs.pop('pacs', None)
		self.resource_cache_lookup = kwargs.pop('rapid_lookup', None)
		
		super().__init__(*args, **kwargs)

	def _init_collection_models(self, **kwargs):
		if self.pacs:
			kwargs['pacs'] = self.pacs
		if self.resource_cache_lookup is not None:
			kwargs['rapid_lookup'] = self.resource_cache_lookup

		return super()._init_collection_models(**kwargs)


# PACS Data Excahnge: PACS DICOM and DICOMweb

class ImagingServerModalityMixin(object):
	'''	Mixin object providing properties and methods common to objects associated
		with Sonador managed PACS imaging servers.
	'''
	@property
	def imaging_server(self):
		return self._objectdata.get('server')


DICOM_MODALITY_OUTPUT_COLUMNS = OrderedDict((
		('imaging_server', 'Imaging Server ID'),
		('pk', 'Modality ID'),
		('name', 'DICOM Modality Name'),
		('aet', 'AET'),
		('host', 'Hostname'),
		('port', 'Port'),
	))


class DicomImagingModality(ImagingServerModalityMixin, ImagingServerBaseObject):
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


class RemoteDICOMwebServer(ImagingServerModalityMixin, ImagingServerBaseObject):
	'''	Remote DICOMweb server associated with a Sonador managed PACS imaging server
	'''
	tabulate_output_columns = DICOMWEB_OUTPUT_COLUMNS
	details_exclude = ('server', 'username', 'password', 'token')

	@property
	def dicomweb_urlbase(self):
		return posixpath.join('dicom-web/servers', self.orthanc_name)

	def remote_query(self, sfilter, expand=True, resource=IMAGING_SERVER_RESOURCE_SERIES,
			limit=None, offset=None, fuzzy=True, query=None, headers=None, verify=None, **kwargs):
		'''	Submit a query (via Orthanc) to a DICOMweb remote instance

			@input sfilter (dict): Terms to be included in the request
			@input expand (bool, default=True): If false, only the resource IDs will be returned.
			@input resource (str, default='Series'): Type of resource for which the query should be executed.
			@input limit (int, default=None): Njumber of records which should be included in the response.
				If None, all records matching the query will be returned.
			@input offset (int, default=None): Any offset to apply to the record list. Used together with
				limit to paginate query results.
			@input fuzzy (bool, default=True): Toggles whether the query should use fuzzy matching
			@input query (dict, default=new dict): Existing ditionary structure to be expanded with the provided
				search query.
			@input headers (dict, default=new dict): Headers to be included with the query request.

			@returns iterable of resource IDs is expand is False, colleciton of th matching resource type if 
				expand is True
		'''
		from .imaging.dicomweb import REMOTE_IMAGING_SERVER_RESOURCE_DATAMODEL_COLLECTIONTYPES, \
			REMOTE_DICOMWEB_RESOURCE_TYPE
		if not isinstance(sfilter, dict):
			raise TypeError('Unable to execute query, terms must be submitted as a dictionary')
		if not resource in REMOTE_IMAGING_SERVER_RESOURCE_DATAMODEL_COLLECTIONTYPES:
			raise ValueError('Unable to execute query, invalid resource type: %s' % resource)

		# Create query structure
		query = query or {}
		query.update({ 'Uri': REMOTE_DICOMWEB_RESOURCE_TYPE.get(resource), 'Arguments': sfilter })
		if limit:
			sfilter['limit'] = str(limit)
		if offset:
			sfilter['offset'] = str(offset)
		if fuzzy:
			sfilter['fuzzymatching'] = str(fuzzy).lower()

		# DICOMweb query structure
		logger.debug('DICOMWeb query:\n%s' % json.dumps(query))

		# Execute query
		r = requests.post(self.pacs.orthanc_apiurl(posixpath.join(self.dicomweb_urlbase, 'qido')), json=query, 
			headers=self.pacs.orthanc_request_headers(headers=headers))
		if not r.ok:
			request_client_error('Unable to execute DICOMweb resource query for %s on PACS %s. Status code: %s.' 
					% (self.pk, self.pacs.server_label, r.status_code), 
				r)

		# Parse response
		return self.server._init_dataclass(REMOTE_IMAGING_SERVER_RESOURCE_DATAMODEL_COLLECTIONTYPES.get(resource), r, dicomweb=self, **kwargs) \
			if expand else self.server._parse_apiresponse_json(r)

	def _remote_resource_operation_request(self, resources, op=None, async_transfer=True, priority=None):
		'''	Structure an Orthanc DICOMweb request for a remote resource operation.

			@input rdata (iterable or esource UIDs)
			@input op (dict, default=new dictionary): Existing operation to which the remote resource paramters
				should be added.
			@input async_transer (bool, default=True): When True, the job will be queued and executed asynchronously.
			@input priority (int, default=None): Associate a priority with the transfer

			@returns JSON (dict) structure of the request
		'''
		op = op or {}
		op.update({ 'Resources': resources })
		if async_transfer:
			op['Synchronous'] = not async_transfer
		if priority is not None:
			op['Priority'] = priority

		return op

	def _parse_remote_resource_operation(self, r, async_transfer):
		'''	Parse a remote resource response

			@input r (requests.Response): API response from server
			@input async_transfer (bool): Indicates whether the request was synchronous or asynchronous.

			@returns OrthancJob instance if the transfer was async or OrthancJobResult is synchronous
		'''
		from .imaging.orthanc.jobs import OrthancJob, OrthancJobResult

		rdata = self.server._parse_apiresponse_json(r)
		return OrthancJob(self.server, rdata, pacs=self.pacs, dicomweb=self) if async_transfer \
			else OrthancJobResult(self.server, rdata, pacs=self.pacs, dicomweb=self)

	def remote_fetch(self, resources, fetch=None, headers=None, async_transfer=True, priority=None):
		''' Create a job to retrieve the resources specified in the resource list. Series should be retrieved using
			the SeriesInstanceUID and StudyInstanceUID. The request is posted to the retrieve endpoint of Orthanc
			and all resources will be retrieved in a single batch.
		'''		
		# Create resource operation request
		fetch = self._remote_resource_operation_request(resources, op=fetch, async_transfer=async_transfer, priority=priority)

		# Execute request
		r = requests.post(self.pacs.orthanc_apiurl(posixpath.join(self.dicomweb_urlbase, 'retrieve')), json=fetch,
			headers=self.pacs.orthanc_request_headers(headers=headers))
		if not r.ok:
			request_client_error('Unable to execute DICOMweb fetch for %s on PACS %s. Status code: %s.' 
					% (self.pk, self.pacs.server_label, r.status_code), 
				r)

		# Parse response
		return self._parse_remote_resource_operation(r, async_transfer)
		


class RemoteDICOMwebServerCollection(ImagingServerChildCollection):
	'''	Collection of DICOMweb servers
	'''
	model = RemoteDICOMwebServer


# API methods

def sonador_apitoken_fetch(sonador_server, output_dest, verify=False):
	'''	Fetch API credentials for the server
	'''
	stoken = fetch_sonador_session_token(sonador_server, verify=verify)
	logger.info('Session token for API Access ID: %s' % sonador_server.access_id)
	output_dest.write(json.dumps(stoken))
