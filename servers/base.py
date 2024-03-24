import six, abc, requests, json, csv, collections, logging, posixpath, zipfile, time
from urllib.parse import urlencode
from pprint import pprint
from io import BytesIO

from tabulate import tabulate
from collections import OrderedDict
from collections.abc import Iterable

from client import apisettings as gcapicodes
from client import auth as guru_auth
from client.utils.urls import build_url
from client.utils.object import pick, omit
from client.utils.microservices import RemotePage, server_controloperation_json_response
from client.utils.format import formerrors2str
from client.utils.conversion import str2bool
from client.errors import ClientOperationError, ConfigurationError
from client.remote import RemoteServer, request_client_error

from ..apisettings import IMAGING_SERVER_RESOURCE_PATIENT, IMAGING_SERVER_RESOURCE_STUDY, \
	IMAGING_SERVER_RESOURCE_SERIES, IMAGING_SERVER_RESOURCE_IMAGE, IMAGING_SERVER_RESOURCE_SUPPORTED, \
	DCMHEADER_MODALITY, DCM_MODALITY_SR, DCM_MODALITY_SEG, DCM_MODALITY_DOC, DCM_VERSION_2021b
from ..apisettings.media import DCMEDIA_M3D_MODALITY
from ..serialization import json_datetime_parser
from ..helpers import request_client_error, fetch_sonador_session_token, API_ACCESS_TOKEN, OAUTH_TOKEN_RESPONSE_TYPE, \
	OAUTH_TOKEN_IDTOKEN_RESPONSE_TYPE, OAUTH_ACCESS_TOKEN, OAUTH_TOKEN_TYPE, OAUTH_TOKEN_TYPE_BEARER, OAUTH_EXPIRATION
from ..remote import SonadorBaseObject, SonadorObjectCollection, \
	fetch_sonador_data_collection, fetch_sonador_dataobject, sonador_dataobject_update

logger = logging.getLogger(__name__)


class OrthancServerBase(SonadorBaseObject):
	'''	Mixin object which provides methods for working with Orthanc.
	'''
	details_exclude = ('token',)

	def __init__(self, *args, resource_cache=None, **kwargs):
		self.resource_cache = resource_cache or {}
		super().__init__(*args, **kwargs)

	@abc.abstractmethod
	def _request_get(self, resource_endpoint, error_msg=None, headers=None, verify=None, **kwargs):
		''' Send a GET request to the imaging server. Raises an exception with the provided error message
			if the request could not be completed successfully.

			@returns request.Response or JSON object (dict/array)
		'''

	@abc.abstractmethod
	def _request_post(self, resource_endpoint, error_msg=None, headers=None, verify=None, **kwargs):
		'''	Send a POST request to the imaging server. Raises an exception with the provided error message
			if the request could not be completed successfully.

			@returns request.Response or JSON object (dict/array)
		'''

	@abc.abstractmethod
	def _request_delete(self, resource_endpoint, error_msg=None, headers=None, verify=None, **kwargs):
		'''	Send a DELETE request to the imaging server. Raises an exception with the provided error message
			if the request could not be completed successfully.

			@returns request.Response or JSON object (dict/array)
		'''
	
	@abc.abstractmethod
	def _request_put(self, resource_endpoint, error_msg=None, headers=None, verify=None, **kwargs):
		'''	Send a PUT request to the imaging server. Raises an exception with the provided error message
			if the request could not be completed successfully.

			@returns request.Response or JSON object (dict/array)
		'''

	@property
	@abc.abstractmethod
	def fetch_endpoint(self):
		''' Sonador API URL from which the base server configuration should be retrieved.
		'''

	def orthanc_apiurl(self, resource_endpoint, query_params=''):
		'''	Create URL for the Orthanc API call
		'''
		return build_url(self.scheme, self.netloc, resource_endpoint, query_params=query_params)

	@abc.abstractmethod
	def orthanc_request_headers(self, headers=None):
		'''	Add headers required by Orthanc API
		'''

	@property
	@abc.abstractmethod
	def modality_datacollection_class(self, *args, **kwargs):
		'''	Data collection class which should be used by the server base for DICOM modalities
		'''

	@property
	@abc.abstractmethod
	def server_label(self):
		'''	Display label for the Orthanc instance
		'''

	@property
	@abc.abstractmethod
	def dicomweb_remote_datacollection_class(self, *args, **kwargs):
		'''	Data collection class which should be used by the server base for managing remote
			DICOMweb instances.
		'''

	def fetch_dicom_modalities(self, **kwargs):
		'''	Retrieve the DICOM modalities associated with the imaging server
		'''
		return fetch_sonador_data_collection(self.server, self.modality_datacollection_class,
			data_collection_endpoint=posixpath.join(
				self.fetch_endpoint, self.pk, self.modality_datacollection_class.model.dcm_urlroot), 
			pacs=self, **kwargs)

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
		return fetch_sonador_data_collection(self.server, self.dicomweb_remote_datacollection_class,
			data_collection_endpoint=posixpath.join(
				self.fetch_endpoint, self.pk, self.dicomweb_remote_datacollection_class.model.dcmweb_urlroot), 
			pacs=self, **kwargs)

	@property
	def dicomweb_remotes(self):
		'''	Remote DICOMweb  instances associated with the imaging server (cached property)
		'''
		if getattr(self, '_dweb', None) is None:
			setattr(self, '_dweb', self.fetch_dicomweb_remotes())

		return self._dweb

	def get_dicomweb_remote(self, rid, verify=None):
		'''	Retrieve DICOMweb remote instance
		'''
		if verify is None:
			verify = self.server.verify

		return fetch_sonador_dataobject(
			self.server, self.dicomweb_remote_datacollection_class.model, rid, verify=verify, pacs=self,
			dataobject_endpoint=posixpath.join(
				self.fetch_endpoint, self.pk, self.dicomweb_remote_datacollection_class.model.dcmweb_urlroot, rid))

	def dicomweb_push(self, rdweb, resources, op=None, headers=None, verify=None, async_transfer=True, priority=None):
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
		r = self._request_post(
			self.orthanc_apiurl(posixpath.join(rdweb.dicomweb_urlbase, 'stow')),
			lambda r: request_client_error(
				'Unable to push resources to DICOMweb for %s on PACS %s. Status code: %s.' % (
					rdweb.pk, self.server_label, getattr(r, 'status_code')),
				r),
			json=op, headers=self.orthanc_request_headers(headers=headers))

		# Parse response
		return rdweb._parse_remote_resource_operation(r, async_transfer)

	@property
	def netloc(self):
		'''	Return network location for the server (hostname:port)
		'''
		if getattr(self, 'port', None):
			return '%s:%s' % (self.hostname, self.port)

		return self.hostname

	def get_resource_modelcollection_class(self, resource_type: str):
		'''	Retrieve the collection class for the provided resource type
		'''
		from ..imaging.orthanc import IMAGING_SERVER_RESOURCE_DATAMODEL_COLLECTIONTYPES
		if not resource_type in IMAGING_SERVER_RESOURCE_DATAMODEL_COLLECTIONTYPES:
			raise ValueError('Invalid resource type: %s' % resource_type)

		return IMAGING_SERVER_RESOURCE_DATAMODEL_COLLECTIONTYPES.get(resource_type)

	def get_resource_model_class(self, resource_type: str):
		''' Get the resource class type for a given resource type
		'''
		return self.get_resource_modelcollection_class(resource_type).model

	def upload_image(self, img, headers=None, retry_count=0, retry_limit=3, verify=None, pause_for_retry=None, **kwargs):
		'''	Upload the provided image to via Orthanc REST API. Raises ClientOperationError is not able 
			to successfully complete the request. Will retry the request up to the provided retry_limit.

			@input img (file-like object): Image data to be uploaded
			@input headers (dict, default=empty dict): Headers to be added to the upload request
            
            @pause_for_retry (int): Seconds to sleep before retrying upload

			@returns requests.Response
		'''
		try:
			r = self._request_post(
				self.orthanc_apiurl('instances'),
				lambda r: request_client_error(
					'Unable to upload image to PACS %s. Status code: %s. Retry transfer: %s/%s' % (
						self.server_label, getattr(r, 'status_code'), retry_count+1, retry_limit),
					r),
				files={ 'file': img }, headers=self.orthanc_request_headers(headers=headers), verify=verify)
			
			return r

		except ClientOperationError as err:
			r = getattr(err, 'response', None)

			if r is not None and not r.ok:

				# Retry upload
				if retry_count < retry_limit:
                    
                    # Pause for retry
                    if pause_for_retry: time.sleep(pause_for_retry)

					logger.warning('Unable to upload image to PACS %s. Status code: %s. Retry transfer: %s/%s.'
						% (self.server_label, r.status_code, retry_count+1, retry_limit))

					# Reset position of image before attempting upload
					img.seek(0)
					r = self.upload_image(
						img, headers=headers, retry_count=retry_count+1, retry_limit=retry_limit, verify=verify, pause_for_retry=pause_for_retry)

				# Retry limit exceeded: notify user of failed transfer
				else: 
					request_client_error(
						'Unable to upload image to PACS %s. Status code: %s.' % (self.server_label, r.status_code), r)

			else: raise err

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

		r = self._request_get(
			self.orthanc_apiurl(posixpath.join(resource_type.fetch_endpoint, rid)), 
			lambda r: request_client_error(
				'Unable to retrieve requested resource %s instance %s. Status code: %s' % (
					rid, resource_type, r.status_code),
				r),
			headers=self.orthanc_request_headers(headers=headers), verify=verify)

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
		from ..imaging.orthanc import ImagingPatient
		return self.get_imaging_resource(pid, ImagingPatient, headers=headers, cache=cache, **kwargs)

	def get_study(self, sid, headers=None, cache=False, **kwargs):
		'''	Retrieve a study instance
		'''
		from ..imaging.orthanc import ImagingStudy
		return self.get_imaging_resource(sid, ImagingStudy, headers=headers, cache=cache, **kwargs)

	def get_series(self, rid, headers=None, cache=False, **kwargs):
		'''	Retrieve a series instance 

			@input rid (str): Orthanc resource ID (resource.pk) of the imaging series to be retrieved.
			@input cache (bool, default=False): toggles whether to retrieve a cached copy of the resource.
				If True, the imaging server instance will store a reference to the imaging series which
				will be used in subsequent calls to `get_series`.
		'''
		from ..imaging.orthanc import ImagingSeries
		return self.get_imaging_resource(rid, ImagingSeries, headers=headers, cache=cache, **kwargs)

	def get_dcm_instance(self, rid, headers=None, cache=False, **kwargs):
		'''	Retrieve a DCM instance

			@input rid (str): Orthanc resource ID (resource.pk) of the DICOM instance to be retrieved.
			@input cache (bool, default=False): toggles whether to retrieve a cached copy of the resource.
				If True, the imaging server instance will store a reference to the imaging series
				which will be used in subsequent calls to `get_dcm_instance`.
		'''
		from ..imaging.orthanc import DcmInstance
		return self.get_imaging_resource(rid, DcmInstance, headers=headers, cache=cache, **kwargs)

	def query(self, sfilter, expand=True, limit=None, offset=None, query=None, headers=None, verify=None, 
			resource=IMAGING_SERVER_RESOURCE_SERIES, resource_modelcollection_class=None, 
			rapid_lookup=None, bulkpopulate_related=False, bulkpopulate_options=None, order_by=None, **kwargs):
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
		from ..imaging.orthanc import IMAGING_SERVER_RESOURCE_DATAMODEL_COLLECTIONTYPES
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

		# For order_by requests, ensure that the cache is being used (rapid_lookup=True).
		# tools/find does not support the OrderBy API parameter.
		if not rapid_lookup and order_by:
			raise ConfigurationError('order_by is only supported on Sonador Cache endpoints (rapid_lookup=True)')

		# Create query structure
		query = query or {}
		query.update({
			'Level': resource, 'Expand': expand, 'Query': sfilter
		})
		if limit is not None:
			query['Limit'] = limit
		if offset is not None:
			query['Since'] = offset
		if order_by:
			if isinstance(order_by, (tuple, list)):
				query['OrderBy'] = order_by
			elif isinstance(order_by, str):
				query['OrderBy'] = (order_by,)
			else:
				raise ValueError('Invalid order_by value "%s". order_by supports str and tuple/list sequences.' % str(order_by))

		# Orthanc query structure
		logger.debug('Orthanc query:\n%s' % json.dumps(query))

		# Execute query
		r = self._request_post(
			self.orthanc_apiurl(resource_modelcollection_class.model.cache_queryurl) if rapid_lookup else self.orthanc_apiurl('tools/find'),
			lambda r: request_client_error('Unable to execute resource query to PACS %s. Status code: %s.' % (self.server_label, r.status_code), r),
			json=query, headers=self.orthanc_request_headers(headers=headers), verify=verify)

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

	def _parse_apiresponse_json(self, *args, **kwargs):
		'''	Parse JSON response to Python representation. (Method added so that the OrthancServerBase
			implements the same API as the client.remote.RemoteServer and can be used with the
			data processing methods of the Guru client library and sonador.remote module.
			Delegates to self.server._parse_apiresponse_json.
		'''
		return self.server._parse_apiresponse_json(*args, **kwargs)

	def _init_dataclass(self, *args, **kwargs):
		'''	Initiliaze data class with provided data. (Method added so that the OrthancServer Base
			implements the same API as the client.remote.RemoteServer and can be used with the
			data processing methods of the Guru client library and sonador.remote module.
			Deletates to self.server._init_dataclass.)
		'''
		return self.server._init_dataclass(*args, server=self, **kwargs)

	def verify_ssl(self, *args, **kwargs):
		'''	Reads the provided keyword arguments and determines the correct value for the `verify` argument
			of remote callable functions. If verify is provided as None, the verify SSL value from the Sonador connection
			instance is used as a default. (Delegates to the server instance of the Orthanc server, which should
			be a `SonadorServer`.)

			@returns bool: True if SSL connections should be validated
		'''
		return self.server.verify_ssl(*args, **kwargs)

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
		from ..imaging.orthanc.sr import DcmSRSeriesCollection

		self._check_query_structure(sfilter)
		sfilter.update({ DCMHEADER_MODALITY: DCM_MODALITY_SR })

		return self.query(sfilter, resource=IMAGING_SERVER_RESOURCE_SERIES, 
			resource_modelcollection_class=DcmSRSeriesCollection, **kwargs)

	def query_seg(self, sfilter, **kwargs):
		'''	Query DICOM-SEG resources on the imaging server. (Wrapper function for "query".)
		'''
		from ..imaging.orthanc.seg import DcmSegmentationSeriesCollection

		self._check_query_structure(sfilter)
		sfilter.update({ DCMHEADER_MODALITY: DCM_MODALITY_SEG })

		return self.query(sfilter, resource=IMAGING_SERVER_RESOURCE_SERIES,
			resource_modelcollection_class=DcmSegmentationSeriesCollection, **kwargs)

	def query_m3d(self, sfilter, **kwargs):
		'''	Query M3D resources on the imaging server. (Wrapper function for "query".)
		'''
		from ..imaging.orthanc.m3d import DcmM3DSeriesCollection
		
		self._check_query_structure(sfilter)
		sfilter.update({ DCMHEADER_MODALITY: DCMEDIA_M3D_MODALITY })

		return self.query(sfilter, resource=IMAGING_SERVER_RESOURCE_SERIES,
			resource_modelcollection_class=DcmM3DSeriesCollection, **kwargs)

	def query_doc(self, sfilter, **kwargs):
		'''	Query DOC resources on the imaging server. (Wrapper function for "query".)
		'''
		from ..imaging.orthanc.media import DcmEncapsulatedDocumentSeriesCollection

		self._check_query_structure(sfilter)
		sfilter.update({ DCMHEADER_MODALITY: DCM_MODALITY_DOC })

		return self.query(sfilter, resource=IMAGING_SERVER_RESOURCE_SERIES,
			resource_modelcollection_class=DcmEncapsulatedDocumentSeriesCollection, **kwargs)

	def fetch_jobs(self, verify=None, headers=None, limit=None, offset=None, expand=True, **kwargs):
		'''	Retrieve the processing jobs for the server
		'''
		from ..imaging.orthanc.jobs import OrthancJobCollection
		
		# Retrieve jobs
		r = self._request_get(
			self.orthanc_apiurl(OrthancJobCollection.model.fetch_endpoint, query_params={ 'expand': expand }),
			error_msg=lambda r: request_client_error(
				'Unable to retrieve jobs from PACS %s. Status code: %s.' % (self.server_label, r.status_code), r),
			headers=self.orthanc_request_headers(headers=headers), verify=verify)

		# Parse response
		if not kwargs.get('pacs'):
			kwargs['pacs'] = self
		return self.server._init_dataclass(OrthancJobCollection, r, **kwargs)

	def get_job(self, jid, headers=None, **kwargs):
		'''	Retrieve a processing job instance
		'''
		from ..imaging.orthanc.jobs import OrthancJob
		return self.get_imaging_resource(jid, OrthancJob, headers=headers, **kwargs)

	def update(self, odata, *args, **kwargs):
		'''	Update the server instance with the provided parameters.

			@input odata (dict): new attributes/values for the server

			@returns response data
		'''
		if kwargs.get('verify') is None:
			kwargs['verify'] = self.server.verify

		# Send update to server and retrieve updated instance
		return sonador_dataobject_update(self, odata, *args, **kwargs)

	def system_info(self, *args, **kwargs):
		'''	Retrieve the configuration for the Sonador server
		'''
		r = self._request_get(
			self.orthanc_apiurl('system'), 
			error_msg=lambda r: request_client_error(
				'Unable to retrieve configuration from PACS %s. Status code: %s.' % (self.server_label, r.status_code), r),
			headers=self.orthanc_request_headers(headers=kwargs.get('headers', {})), **kwargs)

		return r.json()



# Orthanc DICOM Server Base Objects

class ImagingServerChildBaseObject(SonadorBaseObject):
	''' Data object associated with a PACS server. Includes a reference to the server
		from which the object came.
	'''
	def __init__(self, *args, **kwargs):
		self.pacs = kwargs.pop('pacs', None)
		self.resource_cache_lookup = kwargs.pop('rapid_lookup', None)
		super().__init__(*args, **kwargs)

		# If no PACS instance provided, but self.server is an OrthancServerBase, use self.server
		# as self.pacs. This fixes an issue with some parent/child classes where the PACS instance
		# is not always passed to the child instance correctly.
		if self.pacs is None and isinstance(self.server, OrthancServerBase):
			self.pacs = self.server


class ImagingServerChildCollectionFetchMixin:
	'''	Mixin class which can be used to add a "fetch" method to datamodel collections associated
		with a PACS server.
	'''
	@classmethod
	def fetch(cls, pacs, data_collection_endpoint=None, rkwargs=None, error_msg=None, **kwargs):
		'''	Retrieve collection models
		'''
		rkwargs = rkwargs or {}

		data_collection_endpoint = data_collection_endpoint or cls.fetch_endpoint
		if not data_collection_endpoint:
			raise ValueError('Invalid fetch endpoint: %s' % data_collection_endpoint)

		verify = kwargs.pop('verify', None)
		if verify is None:
			verify = pacs.server.verify

		if not error_msg:
			error_msg = lambda r: request_client_error(
				'Unable to retrieve model %s from PACS server %s. Status code: %s.' % (
					cls.model.__name__, pacs.server_label, r.status_code
				), r)

		# Retrieve server data
		r = pacs._request_get(
			pacs.orthanc_apiurl(data_collection_endpoint), error_msg,
			headers=pacs.orthanc_request_headers(headers=kwargs.get('headers')),
			verify=verify, **rkwargs)

		# Parse response and return collection
		return pacs._init_dataclass(cls, r, **kwargs)

	@classmethod
	def fetch_modelinstance(cls, pacs, objectid, error_msg=None,
			apiurl_callable='orthanc_apiurl', headers_callable='orthanc_request_headers', **kwargs):
		'''	Retrieve 
		'''
		verify = kwargs.pop('verify', None)
		if verify is None:
			verify = pacs.server.verify

		if not error_msg:
			error_msg = lambda r: request_client_error(
				'Unable to retrieve %s=%s from PACS server %s. Status code: %s' % (
					cls.model.__name__, pacs.server_label, r.status_code
				), r)

		return fetch_sonador_dataobject(
			pacs, cls.model, objectid, verify=verify, 
			apiurl_callable=apiurl_callable, headers_callable=headers_callable,
			error_msg=error_msg, fetch_callable=pacs._request_get, **kwargs)


class ImagingServerChildCollection(ImagingServerChildCollectionFetchMixin, SonadorObjectCollection):
	'''	Collection which can be used to work with data models associated
		with Sonador managed PACS imaging servers
	'''
	def __init__(self, *args, **kwargs):
		self.pacs = kwargs.pop('pacs', None)
		self.resource_cache_lookup = kwargs.pop('rapid_lookup', None)
		
		super().__init__(*args, **kwargs)

	def _init_empty_collection(self, *args, **kwargs):
		'''	Initialize empty collection: propagates PACS and resource cache lookup
			settings to the new collection instance.
		'''
		if kwargs.get('pacs') is None and self.pacs:
			kwargs['pacs'] = self.pacs
		if kwargs.get('resource_cache_lookup') is None:
			kwargs['rapid_lookup'] = self.resource_cache_lookup

		return super()._init_empty_collection(*args, **kwargs)

	def _init_collection_models(self, **kwargs):
		if self.pacs:
			kwargs['pacs'] = self.pacs
		if self.resource_cache_lookup is not None:
			kwargs['rapid_lookup'] = self.resource_cache_lookup

		return super()._init_collection_models(**kwargs)
