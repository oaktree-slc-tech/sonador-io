import six, requests, json, csv, collections, logging, posixpath
from urllib.parse import urlencode

from tabulate import tabulate
from collections import OrderedDict

from client import auth as guru_auth
from client.utils.urls import build_url
from client.utils.object import pick, omit
from client.utils.microservices import RemotePage

from .apisettings import IMAGING_SERVER_RESOURCE_PATIENT, IMAGING_SERVER_RESOURCE_STUDY, \
	IMAGING_SERVER_RESOURCE_SERIES, IMAGING_SERVER_RESOURCE_IMAGE, IMAGING_SERVER_RESOURCE_SUPPORTED, \
	DCMHEADER_MODALITY, DCM_MODALITY_SR, DCM_MODALITY_SEG
from .serialization import json_datetime_parser
from .helpers import request_client_error, fetch_sonador_session_token
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

	def get_imaging_resource(self, rid, resource_type, headers=None, verify=None, **kwargs):
		'''	Retrieve the requested resource
		'''
		if verify is None:
			verify = self.server.verify

		r = requests.get(self.orthanc_apiurl(posixpath.join(resource_type.fetch_endpoint, rid)),
			headers=self.orthanc_request_headers(headers=headers), verify=verify)
		if not r.ok:
			request_client_error('Unable to retrieve requested resource %s instance %s. Status code: %s'
				% (rid, resource_type, r.status_code), r)

		return self.server._init_dataclass(resource_type, r, pacs=self, **kwargs)

	def get_patient(self, pid, headers=None, **kwargs):
		'''	Retrieve patient data for the specified UID
		'''
		from .imaging.orthanc import ImagingPatient
		return self.get_imaging_resource(pid, ImagingPatient, headers=headers, **kwargs)

	def get_study(self, sid, headers=None, **kwargs):
		'''	Retrieve a study instance
		'''
		from .imaging.orthanc import ImagingStudy
		return self.get_imaging_resource(sid, ImagingStudy, headers=headers, **kwargs)

	def get_series(self, rid, headers=None, **kwargs):
		'''	Retrieve a series instance 
		'''
		from .imaging.orthanc import ImagingSeries
		return self.get_imaging_resource(rid, ImagingSeries, headers=headers, **kwargs)

	def get_dcm_instance(self, rid, headers=None, **kwargs):
		'''	Retrieve a DCM instance
		'''
		from .imaging.orthanc import DcmInstance
		return self.get_imaging_resource(rid, DcmInstance, headers=headers, **kwargs)

	def query(self, sfilter, expand=True,limit=None, offset=None, query=None, headers=None, verify=None, 
			resource=IMAGING_SERVER_RESOURCE_SERIES, resource_modelcollection_class=None, **kwargs):
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
		r = requests.post(self.orthanc_apiurl('tools/find'), json=query, headers=self.orthanc_request_headers(headers=headers))
		if not r.ok:
			request_client_error('Unable to execute resource query to PACS %s. Status code: %s.' % (self.server_label, r.status_code), r)

		# Parse response
		return self.server._init_dataclass(resource_modelcollection_class, r, pacs=self, **kwargs) if expand \
			else self.server._parse_apiresponse_json(r)

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
		from .imaging.jobs import OrthancJobCollection
		
		if verify is None:
			verify = self.server.verify
		
		# Retrieve jobs
		r = requests.get(self.orthanc_apiurl(OrthancJobCollection.model.fetch_endpoint, query_params={ 'expand': expand }), 
			headers=self.orthanc_request_headers(headers=headers), verify=verify)
		if not r.ok:
			request_client_error('Unable to retrieve jobs from PACS %s. Status code: %s.' % (self.server_label, r.status_code), r)

		# Parse response
		return self.server._init_dataclass(OrthancJobCollection, r, pacs=self, **kwargs)

	def get_job(self, jid, headers=None, **kwargs):
		'''	Retrieve a processing job instance
		'''
		from .imaging.jobs import OrthancJob
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
		super().__init__(*args, **kwargs)


class ImagingServerChildCollection(SonadorObjectCollection):
	'''	Collection which can be used to work with data models associated
		with Sonador managed PACS imaging servers
	'''
	def __init__(self, *args, **kwargs):
		self.pacs = kwargs.pop('pacs', None)
		super().__init__(*args, **kwargs)

	def _init_collection_models(self, **kwargs):
		if self.pacs:
			kwargs['pacs'] = self.pacs

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
		from .imaging.jobs import OrthancJob, OrthancJobResult

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
