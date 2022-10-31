import posixpath, logging, requests
from collections import OrderedDict

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
	DCMHEADER_MODALITY, DCM_MODALITY_SR, DCM_MODALITY_SEG, DCM_VERSION_2021b
from ..serialization import json_datetime_parser
from ..helpers import request_client_error, fetch_sonador_session_token, API_ACCESS_TOKEN, OAUTH_TOKEN_RESPONSE_TYPE, \
	OAUTH_TOKEN_IDTOKEN_RESPONSE_TYPE, OAUTH_ACCESS_TOKEN, OAUTH_TOKEN_TYPE, OAUTH_TOKEN_TYPE_BEARER, OAUTH_EXPIRATION
from ..remote import SonadorBaseObject, SonadorObjectCollection, \
	fetch_sonador_data_collection, fetch_sonador_dataobject, sonador_dataobject_update

from .base import ImagingServerChildBaseObject, ImagingServerChildCollection

logger = logging.getLogger(__name__)


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


class DicomImagingModality(ImagingServerModalityMixin, ImagingServerChildBaseObject):
	'''	DICOM imaging modalities associated with a server
	'''
	tabulate_output_columns = DICOM_MODALITY_OUTPUT_COLUMNS
	details_exclude = ('server', 'token')
	dcm_urlroot = 'dicom'


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


class RemoteDICOMwebServer(ImagingServerModalityMixin, ImagingServerChildBaseObject):
	'''	Remote DICOMweb server associated with a Sonador managed PACS imaging server
	'''
	tabulate_output_columns = DICOMWEB_OUTPUT_COLUMNS
	details_exclude = ('server', 'username', 'password', 'token')
	dcmweb_urlroot = 'dicom-web'

	@property
	def dicomweb_urlbase(self):
		return posixpath.join(self.dcmweb_urlroot, 'servers', self.orthanc_name)

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
		from ..imaging.dicomweb import REMOTE_IMAGING_SERVER_RESOURCE_DATAMODEL_COLLECTIONTYPES, \
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
		from ..imaging.orthanc.jobs import OrthancJob, OrthancJobResult

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
