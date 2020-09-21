import six, requests, json, csv, collections, logging, posixpath, zipfile
from io import BytesIO
from abc import abstractmethod
from urllib.parse import urlencode
from collections import Iterable
from collections import OrderedDict

from tabulate import tabulate

from client import auth as guru_auth
from client.utils.urls import build_url
from client.utils.object import pick
from client.utils.microservices import server_controloperation_json_response, RemotePage

from ..apisettings import IMAGING_SERVER_RESOURCE_PATIENT, IMAGING_SERVER_RESOURCE_STUDY, IMAGING_SERVER_RESOURCE_SERIES
from ..helpers import request_client_error, fetch_sonador_session_token
from ..serialization import json_datetime_parser
from ..remote import SonadorBaseObject, SonadorObjectCollection, fetch_sonador_data_collection
from ..servers import ImagingServerChildCollection, ImagingServerBaseObject

logger = logging.getLogger(__name__)


FILEARCHIVE_TYPE_ZIPARCHIVE = 'zip'
FILEARCHIVE_TYPE_DICOMDIR = 'dicomdir'
FILEARCHIVE_TYPE_SUPPORTED = (FILEARCHIVE_TYPE_ZIPARCHIVE, FILEARCHIVE_TYPE_DICOMDIR)


class ImagingResourceCoreMixin(object):
	'''	Mixin class with convenience properties for accessing common Orthanc data fields.
	'''
	@property
	def meta(self):
		if getattr(self, '_meta', None) is None:
			r = requests.get(
				self.pacs.orthanc_apiurl(posixpath.join(self.resource_url, 'metadata'), query_params={ 'expand': True, }),
				headers=self.pacs.orthanc_request_headers())
			if not r.ok:
				request_client_error('Unable to retrieve metadata for %s on server %s. Status code: %s.'
					% (self.pk, self.pacs.server_label, r.status_code), r)

			self._meta = r.json()

		return self._meta

	@property
	@abstractmethod
	def resource_url(self):
		'''	URL for the imaging resource
		'''

	@property
	def url(self):
		return self.resource_url

	def modify(self, replace=None, remove=None, remove_private_tags=False, force=False, transcode=None, private_creator=None,
			modify=None, headers=None, verify=None, **kwargs):
		'''	Modify tags or metadata associated with a DICOM resource

			@input replace (dict): Dictionary of DICOM tags to be replaced for the resource
			@input remove (iterable of tags): Iterable of tag names to be removed for the resource
			@input remove_private_tags (bool, default=False): Flag that, when true, will cause
				private tags (i.e., manufacturer-specific tags) to be removed
			@input force (bool, default=False): Flag that, when true, allows modification of DICOM identifiers
				such as PatientID, StudyInstnceUID, SeriesInstanceuid, and SOPInstanceUID.
			@input transcode (str, default=None): Allows for the definition of the TransferSyntax of the 
				modified resources.
		'''
		modify = modify or {}
		if not isinstance(replace, dict):
			raise TypeError('Unable to modify DICOM resource, replace terms must be submitted as a dictonary')
		if remove and not isinstance(remove, Iterable):
			raise TypeError('Unable to remove requested DICOM tags, remove terms must be submitted as an interable')

		if verify is None:
			verify = self.server.verify

		# Create request structure
		modify.update({ 'RemovePrivateTags': remove_private_tags, 'Force': force })
		if replace:
			modify['Replace'] = replace
		if remove:
			modify['Remove'] = remove
		if remove_private_tags:
			modify['RemovePrivateTags'] = remove_private_tags
		if transcode:
			modify['Transcode'] = transcode
		if private_creator:
			modify['PrivateCreator'] = private_creator

		# Execute operation
		logger.debug('Structure of modification request:\n%s' % json.dumps(modify))
		r = requests.post(self.pacs.orthanc_apiurl(posixpath.join(self.resource_url, 'modify')), json=modify,
			headers=self.pacs.orthanc_request_headers(headers=headers), verify=verify)
		if not r.ok:
			request_client_error(
				'Unable to modify DICOM resource tags/metadata for %s on server %s. Status code: %s.'
					% (self.resource_url, self.pacs.server_label, r.status_code),
				r)

		logger.debug('Response from PACS imaging server:\n%s' % json.dumps(r.json()))
		return r

	def delete(self, verify=None, headers=None, **kwargs):
		'''	Remove the imaging resource from Orthanc
		'''
		if verify is None:
			verify = self.server.verify

		r = requests.delete(self.pacs.orthanc_apiurl(self.resource_url),
			headers=self.pacs.orthanc_request_headers(headers=headers), verify=verify, **kwargs)
		if not r.ok:
			request_client_error(
				'Unable to delete resource %s from imaging server %s, a server error occurred' % (self.url, self.pacs.server_label), r)

		return r


class ImagingResourceMixin(ImagingResourceCoreMixin):
	'''	Mixin class with convenience properties for accessing data fields on higher-order resources 
		such as series, studies, and patients.
	'''
	@property
	def dicomdata(self):
		return self._objectdata.get('MainDicomTags', {})

	@property
	def patientdata(self):
		return self._objectdata.get('PatientMainDicomTags', {})

	@property
	@abstractmethod
	def filearchive_url(self):
		''' File archive URL for the imaging resource
		'''

	@property
	@abstractmethod
	def dicomdir_url(self):
		''' DICOMDIR archive URL for the resource
		'''

	def filearchive(self, cache=False, filearchive_type=FILEARCHIVE_TYPE_ZIPARCHIVE, verify=None):
		'''	Retrieve a ZIP archive of all data associated with the resource.

			@input cache (bool, default=False): Cache the data locally to speed up access.

			@returns zipfile.ZipFile
		'''
		# Retrieve cached copy of the file (if available)
		if getattr(self, '_filearchive', None):
			return self._filearchive

		if verify is None:
			verify = self.server.verify

		# Determine URL from which to retrieve the data
		if FILEARCHIVE_TYPE_DICOMDIR == FILEARCHIVE_TYPE_ZIPARCHIVE:
			filearchive_url = self.filearchive_url
		elif FILEARCHIVE_TYPE_DICOMDIR == FILEARCHIVE_TYPE_DICOMDIR:
			filearchive_url = self.dicomdir_url
		else:
			raise TypeError('Unable to download archive of image data, invalid archive type: %s' % filearchive_type)

		# Retrieve file data from Orthanc
		r = requests.get(self.pacs.orthanc_apiurl(filearchive_url), headers=self.pacs.orthanc_request_headers(), verify=verify)
		if not r.ok:
			request_client_error('Unable to retrieve DICOM resource file data for %s on server % s. Status code: %s.'
					% (self.filearchive_url, self.pacs.server_label, r.status_code),
				r)

		# Initialize file archive from request data, attach the raw content of the request
		# to the archive
		zbuffer = BytesIO(r.content)
		farchive = zipfile.ZipFile(zbuffer, mode='r')
		setattr(farchive, 'raw', zbuffer)

		# Cache (if indicated)
		if cache:
			setattr(self, '_filearchive', farchive)

		return farchive


# PACS Imaging


IMAGING_PATIENT_OUTPUT_COLUMNS = OrderedDict((
		('pk', 'Patient ID'),
		('patient_name', 'Patient Name'),
		('patientid', 'MRN'),
		('patient_sex', 'Sex'),
		('birth_date', 'Birth Date'),
	))


class ImagingPatient(ImagingResourceMixin, ImagingServerBaseObject):
	'''	Patient 
	'''
	pk_attr = 'ID'
	tabulate_output_columns = IMAGING_PATIENT_OUTPUT_COLUMNS
	fetch_endpoint = 'patients'

	@property
	def resource_url(self):
		return posixpath.join(self.fetch_endpoint, self.pk)

	@property
	def filearchive_url(self):
		return posixpath.join(self.fetch_endpoint, self.pk, 'archive')

	@property
	def dicomdir_url(self):
		return posixpath.join(self.fetch_endpoint, self.pk, 'media')

	@property
	def patient_name(self):
		return self.dicomdata.get('PatientName')

	@property
	def patientid(self):
		return self.dicomdata.get('PatientID')

	@property
	def patient_sex(self):
		return self.dicomdata.get('PatientSex')

	@property
	def birth_date(self):
		return self.dicomdata.get('PatientBirthDate')

	@property
	def studies(self):
		return self._objectdata.get('Studies')


class ImagingPatientCollection(ImagingServerChildCollection):
	'''	Collection of patients
	'''
	model = ImagingPatient


IMAGING_STUDY_OUTPUT_COLUMNS = OrderedDict((
		('patient', 'Parent Patient'),
		('pk', 'Study ID'),
		('patient_name', 'Patient Name'),
		('patientid', 'MRN'),
		('accession_number', 'Accession#'),
		('study_date', 'Study Date'),
		('physician', 'Requesting Physician'),
		('description', 'Description'),
	))


class ImagingStudy(ImagingResourceMixin, ImagingServerBaseObject):
	'''	Imaging study: set of sequences/series/scans
	'''
	pk_attr = 'ID'
	tabulate_output_columns = IMAGING_STUDY_OUTPUT_COLUMNS
	fetch_endpoint = 'studies'

	@property
	def resource_url(self):
		return posixpath.join(self.fetch_endpoint, self.pk)

	@property
	def filearchive_url(self):
		return posixpath.join(self.fetch_endpoint, self.pk, 'archive')

	@property
	def dicomdir_url(self):
		return posixpath.join(self.fetch_endpoint, self.pk, 'media')

	@property
	def patient(self):
		return self._objectdata.get('ParentPatient')

	@property
	def patient_name(self):
		return self.patientdata.get('PatientName')

	@property
	def patientid(self):
		return self.patientdata.get('PatientID')

	@property
	def description(self):
		return self.dicomdata.get('StudyDescription')

	@property
	def accession_number(self):
		return self.dicomdata.get('AccessionNumber')

	@property
	def study_date(self):
		return self.dicomdata.get('StudyDate')

	@property
	def physician(self):
		return self.dicomdata.get('RequestingPhysician')

	@property
	def series(self):
		return self._objectdata.get('Series')


class ImagingStudyCollection(ImagingServerChildCollection):
	'''	Collection of imaging studies
	'''
	model = ImagingStudy


IMAGING_SERIES_OUTPUT_COLUMNS = OrderedDict((
		('study', 'Parent Study'),
		('pk', 'Series ID'),
		('modality', 'Modality'),
		('sequence_name', 'Name'),
		('series_number', 'Number'),
		('series_date', 'Date'),
		('series_time', 'Time'),
		('series_uid', 'UID'),
		('body_part', 'Body Part Examined'),
		('description', 'Description'),
	))


class ImagingSeries(ImagingResourceMixin, ImagingServerBaseObject):
	'''	Imaging series: set of grouped images
	'''
	pk_attr = 'ID'
	tabulate_output_columns = IMAGING_SERIES_OUTPUT_COLUMNS
	fetch_endpoint = 'series'

	@property
	def resource_url(self):
		return posixpath.join(self.fetch_endpoint, self.pk)

	@property
	def filearchive_url(self):
		return posixpath.join(self.fetch_endpoint, self.pk, 'archive')

	@property
	def dicomdir_url(self):
		return posixpath.join(self.fetch_endpoint, self.pk, 'media')

	@property
	def study(self):
		return self._objectdata.get('ParentStudy')

	@property
	def sequence_name(self):
		return self.dicomdata.get('SequenceName')

	@property
	def modality(self):
		return self.dicomdata.get('Modality')

	@property
	def description(self):
		return self.dicomdata.get('SeriesDescription')

	@property
	def series_number(self):
		return self.dicomdata.get('SeriesNumber')

	@property
	def series_date(self):
		return self.dicomdata.get('SeriesDate')

	@property
	def series_time(self):
		return self.dicomdata.get('SeriesTime')

	@property
	def series_uid(self):
		return self.dicomdata.get('SeriesInstanceUID')

	@property
	def body_part(self):
		return self.dicomdata.get('BodyPartExamined')

	@property
	def slices(self):
		'''	Retrieve instance UIDs for the series
		'''
		return self._objectdata.get('Instances')

	def fetch_slices(self, **kwargs):
		'''	Retrieve details for slices in the series

			@returns collection of DICOM instances
		'''
		# Retrieve instances details
		r = requests.get(self.pacs.orthanc_apiurl(posixpath.join(self.resource_url, 'instances')),
			headers=self.pacs.orthanc_request_headers(headers=kwargs.get('headers')))
		if not r.ok:
			request_client_error(
				'Unable to retrieve details for series %s instances on server %s. Status code: %s.' % (self.pk, self.pacs.server_label, r.status_code),
				r)

		# Parse response and return collection
		rdata = server_controloperation_json_response(r,
			json_loads=lambda rd, mkwargs: json_datetime_parser(rd.json(**mkwargs)), object_pairs_hook=OrderedDict)
		return DcmInstanceCollection(self.server, rdata, pacs=self.pacs, series=self, **kwargs)

	@property
	def slices_collection(self):
		'''	Cached property for retrieving the slice/image instances which belong to the series
		'''
		if getattr(self, '_slices', None) is None:
			setattr(self, '_slices', self.fetch_slices())

		return self._slices


class ImagingSeriesCollection(ImagingServerChildCollection):
	''' Collection of imaging series
	'''
	model = ImagingSeries


IMAGING_SERVER_RESOURCE_DATAMODEL_COLLECTIONTYPES = OrderedDict((
		(IMAGING_SERVER_RESOURCE_PATIENT, ImagingPatientCollection),
		(IMAGING_SERVER_RESOURCE_STUDY, ImagingStudyCollection), 
		(IMAGING_SERVER_RESOURCE_SERIES, ImagingSeriesCollection),
	))


IMAGING_INSTANCE_OUTPUT_COLUMNS = OrderedDict((
		('series', 'Series'),
		('pk', 'Instance UID'),
	))


class DcmInstance(ImagingResourceCoreMixin, ImagingServerBaseObject):
	'''	DCM instance
	'''
	pk_attr = 'ID'

	def __init__(self, *args, **kwargs):
		self.series = kwargs.pop('series', None)
		super(DcmInstance, self).__init__(*args, **kwargs)


class DcmInstanceCollection(ImagingServerChildCollection):
	'''	Collection of instances
	'''
	def __init__(self, *args, **kwargs):
		self.series = kwargs.pop('series', None)
		super(DcmInstanceCollection, self).__init__(*args, **kwargs)

	def _init_collection_models(self, **kwargs):
		if self.series:
			kwargs['series'] = self.series

		return super(DcmInstanceCollection, self)._init_collection_models(**kwargs)
		