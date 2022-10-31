''' Model classes associated with Orthanc DICOM resources. Provides tools
	for representing, queryying, modifying, and removing core DICOM resource instances.
'''
import six, requests, json, csv, collections, logging, functools, posixpath, zipfile, pydicom, datetime
from io import BytesIO

from abc import ABCMeta, abstractmethod

from urllib.parse import urlencode

from collections import namedtuple
from collections.abc import Iterable
from collections import OrderedDict

from tabulate import tabulate

from client import apisettings as gcapicodes
from client import auth as guru_auth
from client.utils.urls import build_url
from client.utils.object import pick
from client.utils.microservices import server_controloperation_json_response, RemotePage

from ...apisettings import IMAGING_SERVER_RESOURCE_PATIENT, IMAGING_SERVER_RESOURCE_STUDY, IMAGING_SERVER_RESOURCE_SERIES, \
	IMAGING_SERVER_RESOURCE_IMAGE, IMAGING_SERVER_LAST_UPDATE, IMAGING_SERVER_DICOMTAGS_SIGNATURE, \
	DCMHEADER_PATIENT_ID, DCMHEADER_PATIENT_NAME, \
	DCMHEADER_PATIENT_SEX, DCMHEADER_PATIENT_BIRTHDATE, \
	DCMHEADER_IMAGE_POSITION_PATIENT, DCMHEADER_IMAGE_ORIENTATION_PATIENT, DCM_DATE_STRFORMAT, DCM_TIME_STRFORMAT, \
	DCMHEADER_MODALITY, DCMHEADER_STUDY_INSTANCE_UID, DCMHEADER_STUDY_ID, \
	DCMHEADER_STUDY_DATE, DCMHEADER_STUDY_TIME, \
	DCMHEADER_SERIES_INSTANCE_UID, DCMHEADER_SERIES_NUMBER, \
	DCMHEADER_SERIES_DATE, DCMHEADER_SERIES_TIME, DCMHEADER_SERIES_DESCRIPTION, \
	DCMHEADER_BODY_PART_EXAMINED, DCM_VERSION_2021b, \
	DCMHEADER_MODALITIES_IN_STUDY, DCM_MODALITY_SR, DCM_MODALITY_SEG

from ...helpers import request_client_error, fetch_sonador_session_token
from ...serialization import json_datetime_parser, json_str2datetime, dcm_str2date, dcm_str2time
from ...remote import SonadorBaseObject, SonadorObjectCollection, fetch_sonador_data_collection
from ...servers import ImagingServerChildCollection, ImagingServerChildBaseObject, SonadorImagingServer

logger = logging.getLogger(__name__)


FILEARCHIVE_TYPE_ZIPARCHIVE = 'zip'
FILEARCHIVE_TYPE_DICOMDIR = 'dicomdir'
FILEARCHIVE_TYPE_SUPPORTED = (FILEARCHIVE_TYPE_ZIPARCHIVE, FILEARCHIVE_TYPE_DICOMDIR)


ImageCoord = namedtuple('ImageCoord', ('x', 'y', 'z'))
ImageSpacing = namedtuple('ImageSpacing', ('x', 'y', 'thickness'))
ImageOrientation = namedtuple('ImageOrientation', ('row', 'col'))
ImageStackShape = namedtuple('ImageStackShape', ('slices', 'rows', 'cols'))


EUCLID_COORD_ORIGIN = ImageCoord(0, 0, 0)


def parse_image_orientation(coords):
	'''	Parse the provided coordinates to a row/column paris of X,Y,Z values

		@input coords (variable): If the input is a string, it will be parsed to a pair
			of image coordinates containing the x,y,z values for row/column image position
			values.

		@returns tuple (row,column) of ImageCoord
	'''
	# Split into a pair of x, y, z coordinates by delimeter. Try '\', ',', before 
	# falling back to ' '
	if isinstance(coords, six.string_types):

		# Split the string into x,y,z,x,y,z tuple
		coords = coords.split('\\' if '\\' in coords
			else ',' if ',' in coords
			else ' ')

		# Ensure that all expected values are present
		if not len(coords) == 6:
			raise ValueError('Invalid patient orientation, expected row/column x,y,z pairs')

		# Unpack components of the tuple into row and column ImageCoord
		row, col = ImageCoord(*tuple(float(v) for v in coords[:3])), ImageCoord(*tuple(float(v) for v in coords[3:]))
		coords = ImageOrientation(row, col)

	return coords


class ImagingResourceCoreMixin(object, metaclass=ABCMeta):
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
	def lastupdate(self):
		'''	Timestamp of last resource update
		'''
		if getattr(self, '_lastupdate', None) is None and self.meta.get(IMAGING_SERVER_LAST_UPDATE):
			setattr(self, '_lastupdate', json_str2datetime(self.meta.get(IMAGING_SERVER_LAST_UPDATE)))

		return getattr(self, '_lastupdate', None)

	@property
	def tags_signature(self):
		return self.meta.get(IMAGING_SERVER_DICOMTAGS_SIGNATURE)

	@property
	def stable(self):
		return self._objectdata.get('IsStable')

	@property
	def dicomdata(self):
		return self._objectdata.get('MainDicomTags', {})

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
		'''	Modify tags or metadata associated with a DICOM resource.  The modified DICOM instances will be stored into a brand 
			new resource, whose Orthanc identifiers will be returned by the job. 

			@input replace (dict): Dictionary of DICOM tags to be replaced for the resource
			@input remove (iterable of tags): Iterable of tag names to be removed for the resource
			@input remove_private_tags (bool, default=False): Flag that, when true, will cause
				private tags (i.e., manufacturer-specific tags) to be removed
			@input force (bool, default=False): Flag that, when true, allows modification of DICOM identifiers
				such as PatientID, StudyInstanceUID, SeriesInstanceuid, and SOPInstanceUID.
			@input transcode (str, default=None): Allows for the definition of the TransferSyntax of the 
				modified resources.

			@returns requests.Response
		'''
		modify = modify or {}
		if replace and not isinstance(replace, dict):
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

		logger.debug('Response from PACS imaging server:\n%s' % r.content)
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


class ImagingResourceParentMixin(object, metaclass=ABCMeta):
	'''	Mixin class which defines the resource/parent interface for Orthanc imaging resources
	'''
	@property
	@abstractmethod
	def parent(self):
		'''	Retrieve the parent of the current resource
		'''


class ImagingResourceMixin(ImagingResourceCoreMixin):
	'''	Mixin class with convenience properties for accessing data fields on higher-order resources 
		such as series, studies, and patients.
	'''

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

	@property
	@abstractmethod
	def cache_indexurl(self):
		'''	Sonador cache URL used to index the resource
		'''

	@property
	def type(self):
		return self._objectdata.get('Type')

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

	def index(self, link=True, headers=None, verify=None, rdata=None):
		''' Add the resouce to the Sonador resource cache.

			@returns requests.Response
		'''
		rdata = rdata or {}
		if verify is None:
			verify = self.server.verify

		# Request components
		rdata['link'] = link

		r = requests.post(self.pacs.orthanc_apiurl(posixpath.join(self.cache_indexurl)),
			json=rdata, headers=self.pacs.orthanc_request_headers(headers=headers), verify=verify)
		if not r.ok:
			request_client_error(
				'Unable to add DICOM resource %s on server %s to the Sonador resource cache. Status code: %s.'
					% (self.cache_indexurl, self.pacs.server_label, r.status_code),
				r)

		logger.debug('Response from PACS imaging server:\n%s' % r.content)
		return r

	def reconstruct(self, reconstruct_files=False, headers=None, verify=None, rdata=None):
		'''	Launch a job which to re-build the resource in the database. Refer to 
			https://api.orthanc-server.com/ for endpoint details.

			@input reconstruct_files (bool, default=False): When true, the reconstruction will
				also reconstruct the files of resources, which will re-code the instances to use 
				the server's "ingest transcoding" and "storage compression."

			@returns requests.Response
		'''
		rdata = rdata or {}
		if verify is None:
			verify = self.server.verify

		# Request components
		if reconstruct_files:
			rdata['ReconstructFiles'] = reconstruct_files
		
		r = requests.post(self.pacs.orthanc_apiurl(posixpath.join(self.resource_url, 'reconstruct')),
			json=rdata, headers=self.pacs.orthanc_request_headers(headers=headers), verify=verify)
		if not r.ok:
			request_client_error(
				'Unable to reconstruct DICOM resource for %s on server %s. Status code: %s.'
					% (self.resource_url, self.pacs.server_label, r.status_code),
				r)

		logger.debug('Response from PACS imaging server:\n%s' % r.content)
		return r

	def anonymize(self, replace=None, keep=None, remove=None, keep_private_tags=True, 
			dicom_version=DCM_VERSION_2021b, anonymize=None, headers=None, verify=None):
		'''	Anonymize the resource. Anonymization erases all tags specified in Table E.1-1 from PS 3.15
			 of the DICOM standard. (Refer to 
			 http://dicom.nema.org/medical/dicom/current/output/chtml/part15/chapter_E.html#table_E.1-1.)

			 @input replace (dict, default=None): dict of DICOM tags to replace on the resource.
			 	Example: { 'PatientName': 'Example Patient', '0010-1011': 'Example Tag Value'}.
			 	Replacements are applied after all the tags to anonymize have been removed. replace
			 	may be used to add new tags to the resource.
			 @input keep (iterable, default=None): iterable of tags to be preserved from full anonymization.
			 @input keep_private_tags (bool, default=True): preserves private (manufacturer-specific) tag
			 	values. The default behavior of the server is to remove the tags.
			 @input dicom_version (str, default='2021b'): version of the DICOM standard to be used
			 	for anonymization.
			 @input remove (iterable, default=None): iterable ot tags to be removed outside of those
			 	specified in the standard.

			 @returns request.Response
		'''
		anonymize = anonymize or {}
		if replace and not isinstance(replace, dict):
			raise TypeError('Unable to anonymize DICOM resource, replace terms must be submitted as a dictionary.')
		if remove and not isinstance(remove, Iterable):
			raise TypeError('Unable to remove DICOM tags, remove terms must be submitted as an iterable.')

		if verify is None:
			verify = self.server.verify

		# Create request structure
		anonymize.update({ 'KeepPrivateTags': keep_private_tags, 'DicomVersion': dicom_version })
		if replace:
			anonymize['Replace'] = replace
		if remove:
			anonymize['Remove'] = remove
		if keep:
			anonymize['Keep'] = keep

		# Execute operation
		logger.debug('Structure of modification request:\n%s' % json.dumps(anonymize))
		r = requests.post(self.pacs.orthanc_apiurl(posixpath.join(self.resource_url, 'anonymize')), json=anonymize,
			headers=self.pacs.orthanc_request_headers(headers=headers), verify=verify)
		if not r.ok:
			request_client_error(
				'Unable to anonymize DICOM resource tags for %s on server %s. Status code: %s.'
					% (self.resource_url, self.pacs.server_label, r.status_code),
				r)

		logger.debug('Response from PACS imaging server:\n%s' % r.content)
		return r



# Imaging Resource Base Collection
class ImagingResourceBaseCollection(ImagingServerChildCollection):
	'''	Collection of imaging resources stored in a Sonador managed PACS imaging server.
		The base collection defines an interface that can be used to bulk populate
		models related to the instances within the collection.
	'''
	@abstractmethod
	def bulkpopulate_related(self, *args, **kwargs):
		'''	Populate models related to collection instances in the most efficient manner possible
			via the Orthanc /tools/bulk-content endpoint.
		'''


# PACS Imaging


IMAGING_PATIENT_OUTPUT_COLUMNS = OrderedDict((
		('pk', 'Patient ID'),
		('patient_name', 'Patient Name'),
		('patientid', 'MRN'),
		('patient_sex', 'Sex'),
		('birth_date', 'Birth Date'),
	))


class ImagingPatient(ImagingResourceMixin, ImagingServerChildBaseObject):
	'''	Patient 
	'''
	pk_attr = 'ID'
	tabulate_output_columns = IMAGING_PATIENT_OUTPUT_COLUMNS
	fetch_endpoint = 'patients'
	cache_queryurl = '/cache/patients'

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
	def cache_indexurl(self):
		return posixpath.join(self.cache_queryurl, self.pk, 'index')

	@property
	def patient_name(self):
		return self.dicomdata.get(DCMHEADER_PATIENT_NAME)

	@property
	def patientid(self):
		return self.dicomdata.get(DCMHEADER_PATIENT_ID)

	@property
	def patient_sex(self):
		return self.dicomdata.get(DCMHEADER_PATIENT_SEX)

	@property
	def birth_datestr(self):
		return self.dicomdata.get(DCMHEADER_PATIENT_BIRTHDATE)

	@property
	def birth_date(self):
		'''	Patient birth date

			@returns datetime.date
		'''
		if getattr(self, '_birthdate', None) is None and self.birth_datestr:
			setattr(self, '_birthdate', dcm_str2date(self.birth_datestr))

		return getattr(self, '_birthdate', None)

	@property
	def studies(self):
		return self._objectdata.get('Studies')

	def fetch_studies(self, **kwargs):
		'''	Retrieve details of the studies associated with the patient

			@returns collection of DICOM study instances associated with the current patient
		'''
		# Retrieve study details
		r = requests.get(self.pacs.orthanc_apiurl(posixpath.join(self.resource_url, 'studies')),
			headers=self.pacs.orthanc_request_headers(headers=kwargs.get('headers')))
		if not r.ok:
			request_client_error(
				'Unable to retrieve details for patient %s studies on server %s. Status code: %s.' 
					% (self.pk, self.pacs.server_label, r.status_code),
				r)

		# Parse response and return collection
		return self.server._init_dataclass(ImagingStudyCollection, r, pacs=self.pacs, patient=self, **kwargs)

	def studies_from_json(self, jdata, **kwargs):
		'''	Initialize studies collection from JSON structure.
		'''
		return self.server._init_dataclass_from_json(
			ImagingStudyCollection, jdata, pacs=self.pacs, patient=self, **kwargs)

	@property
	def studies_collection(self):
		'''	Cached property for retrieving the study instances associated with the patient
		'''
		if getattr(self, '_studies', None) is None:
			setattr(self, '_studies', self.fetch_studies())

		return self._studies

	def fetch_series(self, **kwargs):
		'''	Retrieve details of the series associated with the patient

			@returns collection of DICOM series instances that associated with the current patient
		'''
		# Retrieve series details
		r = requests.get(self.pacs.orthanc_apiurl(posixpath.join(self.resource_url, 'series')),
			headers=self.pacs.orthanc_request_headers(headers=kwargs.get('headers')))
		if not r.ok:
			request_client_error(
				'Unable to retrieve details for patient %s series on server %s. Status code: %s.' % (self.pk, self.pacs.server_label, r.status_code),
				r)

		# Parse response and return collection
		return self.server._init_dataclass(ImagingSeriesCollection, r, pacs=self.pacs, patient=self, **kwargs)

	def series_from_json(self, jdata, **kwargs):
		'''	Initialize series collection from JSON structure
		'''
		return self.server._init_dataclass_from_json(
			ImagingSeriesCollection, jdata, pacs=self.pacs, patient=self, **kwargs)

	@property
	def series_collection(self):
		'''	Cached property for retrieving the series instances associated with the patient
		'''
		if getattr(self, '_series', None) is None:
			setattr(self, '_series', self.fetch_series())

		return self._series


class ImagingPatientCollection(ImagingResourceBaseCollection):
	'''	Collection of patients
	'''
	model = ImagingPatient

	def bulkpopulate_related(self, child_studies=True, child_series=True):
		'''	Populate models related to collection instances in the most efficient manner possible.

			@input child_studies (bool, default=True): bulk populate "studies_collection" of collection instances.
			@input child_series (bool, default=True): bulk populate "series_collection" of child studies.
		'''
		if child_series and not child_studies:
			raise ValueError('Unable to populate series data, option rquires that study be retrieved.')

		if child_studies:
			studies_uids = []

			# Aggregate patient UIDs
			for p in self:
				studies_uids.extend(p.studies)

			# Retrieve child studies and unpack
			bdata = self.pacs.fetch_bulk_content(studies_uids, *args, **kwargs)
			bdata_study = bdata.get(IMAGING_SERVER_RESOURCE_STUDY)

			# Unpack data
			for p in self:

				# Study
				if bdata_study:
					p.studies_from_json([bdata_study.get_modelinstance(sid)._objectdata for sid in p.studies if bdata_study.get_modelinstance(sid)])

		if child_series:
			series_uids = []

			# Aggregate series UIDs
			for p in self:
				for s in p.studies_collection:
					series_uids.extend(s.series)

			# Retrieve child series and unpack
			bdata = self.pacs.fetch_bulk_content(series_uids, *args, **kwargs)
			bdata_series = bdata.get(IMAGING_SERVER_RESOURCE_SERIES)

			# Unpack data
			for p in self:
				for s in p.studies_collection:

					# Child series
					if bdata_series:
						s.series_collection = s.series_from_json(
							[bdata_series.get_modelinstance(sid)._objectdata for sid in s.series if bdata_series.get_modelinstance(sid)])
						s._populate_subcollections()


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


class ImagingStudy(ImagingResourceMixin, ImagingResourceParentMixin, ImagingServerChildBaseObject):
	'''	Imaging study: set of sequences/series/scans
	'''
	pk_attr = 'ID'
	tabulate_output_columns = IMAGING_STUDY_OUTPUT_COLUMNS
	fetch_endpoint = 'studies'
	cache_queryurl = '/cache/studies'

	def __init__(self, *args, **kwargs):
		self._parent = kwargs.pop('patient', None)
		super().__init__(*args, **kwargs)

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
	def cache_indexurl(self):
		return posixpath.join(self.cache_queryurl, self.pk, 'index')

	@property
	def patient(self):
		return self._objectdata.get('ParentPatient')

	@property
	def parent(self):
		'''	Retrieve the parent patient for the study
		'''
		if getattr(self, '_parent', None) is None:
			self._parent = self.pacs.get_patient(self.patient)

			# Propagate cache lookup settings of current instance
			if getattr(self, 'resource_cache_lookup', None) is not None:
				setattr(self._parent, 'resource_cache_lookup', self.resource_cache_lookup)

		return self._parent
		
	@parent.setter
	def parent(self, patient_model):
		'''	Sets the parent patient for the study
		'''
		if not isinstance(patient_model, ImagingPatient):
			raise ValueError("Input must be a instance of a patient")

		setattr(self, '_parent', patient_model)

	def parent_from_json(self, jdata, **kwargs):
		''' Initialize patient model from the provided JSON data.
		'''
		return self.server._init_dataclass_from_json(ImagingPatient, jdata, pacs=self.pacs, **kwargs)

	@property
	def model_patient(self):
		return self.parent

	@property
	def study_uid(self):
		return self.dicomdata.get(DCMHEADER_STUDY_INSTANCE_UID)

	@property
	def study_id(self):
		return self.dicomdata.get(DCMHEADER_STUDY_ID)

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
	def study_datestr(self):
		return self.dicomdata.get(DCMHEADER_STUDY_DATE)

	@property
	def study_date(self):
		'''	Date that the study was acquired. (Parsed from study_datestr.)
		'''
		if getattr(self, '_sdate', None) is None and self.study_datestr:
			self._sdate = dcm_str2date(self.study_datestr)

		return getattr(self, '_sdate', None)

	@property
	def study_timestr(self):
		return self.dicomdata.get(DCMHEADER_STUDY_TIME)

	@property
	def study_time(self):
		if getattr(self, '_stime', None) is None and self.study_timestr:
			self._stime = dcm_str2time(self.study_timestr)

		return getattr(self, '_stime', None)

	@property
	def ts(self):
		'''	Date/time that the series was acquired (Created from study_date and study_time properties.)
			Returns None is there is no sutdy date value. Study time is used if available, with midnight
			being used if it is not.
		'''
		try:
			if getattr(self, '_ts', None) is None and self.study_date:

				# Create timestamp by grouping series date and series time
				sdate = self.study_date
				stime = self.study_time or datetime.time(0,0,0)
				self._ts = datetime.datetime.combine(sdate, stime)
		
		except Exception as err:
			self._ts = None

		return getattr(self, '_ts', None)

	@property
	def modalities(self):
		'''	List of modalities in the study
		'''
		return self.dicomdata.get(DCMHEADER_MODALITIES_IN_STUDY)

	@property
	def physician(self):
		return self.dicomdata.get('RequestingPhysician')

	@property
	def series(self):
		return self._objectdata.get('Series')

	def fetch_series(self, **kwargs):
		'''	Retrieve details of the series associated with the study

			@returns collection of DICOM series instances that associated with the current study
		'''
		# Retrieve series details
		r = requests.get(self.pacs.orthanc_apiurl(posixpath.join(self.resource_url, 'series')),
			headers=self.pacs.orthanc_request_headers(headers=kwargs.get('headers')))
		if not r.ok:
			request_client_error(
				'Unable to retrieve details for study %s series on server %s. Status code: %s.' % (self.pk, self.pacs.server_label, r.status_code),
				r)

		# Parse response and return collection
		return self.server._init_dataclass(ImagingSeriesCollection, r, pacs=self.pacs, study=self, **kwargs)

	def series_from_json(self, jdata, **kwargs):
		'''	Initialize series collection from JSON structure
		'''
		return self.server._init_dataclass_from_json(
			ImagingSeriesCollection, jdata, pacs=self.pacs, study=self, **kwargs)

	@property
	def series_collection(self):
		'''	Series instances associated with the study
		'''
		
		if getattr(self, '_series', None) is None:
			setattr(self, '_series', self.fetch_series())

		return self._series
	
	@series_collection.setter
	def series_collection(self, series_collection):
		'''	Series instances associated with the study
		'''
		if not isinstance(series_collection, ImagingSeriesCollection):
			raise ValueError("Series must be a instance of the Series Collection")

		setattr(self, '_series', series_collection)

	def fetch_sr(self, **kwargs):
		'''	Fetch the DICOM-SR instances that are associated with the study
		'''
		return self.pacs.query_sr({ DCMHEADER_STUDY_INSTANCE_UID: self.study_uid, }, 
			rapid_lookup=getattr(self, 'resource_cache_lookup', None), pacs=self.pacs, study=self, **kwargs)

	def sr_from_json(self, jdata, **kwargs):
		''' Initialize segmentation collection from JSON structure
		'''
		from .sr import DcmSRSeriesCollection
		return self.server._init_dataclass_from_json(
			DcmSRSeriesCollection, jdata, pacs=self.pacs, study=self, **kwargs)

	@property
	def sr_collection(self):
		'''	DICOM-SR instances associated with the study
		'''
		if getattr(self, '_sr', None) is None:
			setattr(self, '_sr', self.fetch_sr())

		return self._sr

	@sr_collection.setter
	def sr_collection(self, sr_collection):
		'''	Set SR collection instances which belong to the study
		'''
		from .sr import DcmSRSeriesCollection
		if not isinstance(sr_collection, DcmSRSeriesCollection):
			raise ValueError('Input must be an instance of a DcmSRSeriesCollection')

		setattr(self, '_sr', sr_collection)

	def fetch_seg(self, **kwargs):
		'''	Fetch the DICOM-SEG instances that are associated with the study
		'''
		return self.pacs.query_seg({ DCMHEADER_STUDY_INSTANCE_UID: self.study_uid },
			rapid_lookup=getattr(self, 'resource_cache_lookup', None), pacs=self.pacs, study=self, **kwargs)

	def seg_from_json(self, jdata, **kwargs):
		'''	Initialize segmentation collection from JSON structure
		'''
		from .seg import DcmSegmentationSeriesCollection
		return self.server._init_dataclass_from_json(
			DcmSegmentationSeriesCollection, jdata, pacs=self.pacs, study=self, **kwargs)

	@property
	def seg_collection(self):
		'''	DICOM-SEG instances associated with the study
		'''
		if getattr(self, '_seg', None) is None:
			setattr(self, '_seg', self.fetch_seg())

		return self._seg

	@seg_collection.setter
	def seg_collection(self, seg_collection):
		'''	Set segmentation instances which belong to the study
		'''
		from .seg import DcmSegmentationSeriesCollection
		if not isinstance(seg_collection, DcmSegmentationSeriesCollection):
			raise ValueError('Input must be an instance of a DcmSegmentationSeriesCollection')

		setattr(self, '_seg', seg_collection)

	def _populate_subcollections(self, populate_sr=True, populate_seg=True):
		'''	Populate study SR and SEG collections from the series collection
		'''
		if populate_sr:
			self.sr_collection = self.sr_from_json(
				[sx._objectdata for sx in self.series_collection if sx.modality == DCM_MODALITY_SR])

		if populate_seg:
			self.seg_collection = self.seg_from_json(
				[sx._objectdata for sx in self.series_collection if sx.modality == DCM_MODALITY_SEG])
	
	def merge_resources(self, resources: list, asynchronous=False, keep_source=False, permissive=False, 
			priority=0, merge=None, verify=None, headers=None):
		'''	Merge the specified resources into a the current study. This is done by updating 
			the following DICOM tags of the provided resources: StudyInstanceUID (0x0020, 0x000d), 
			SeriesInstanceUID (0x0020, 0x000e), and SOPInstanceUID (0x0008, 0x0018). 
			Additionally, all the DICOM tags that are part of the “Patient Module Attributes” and 
			the “General Study Module Attributes” (as specified by the DICOM 2011 standard in 
			Tables C.7-1 and C.7-3), are modified to match the target study. 
			(Refer to https://book.orthanc-server.com/users/anonymization.html#split-merge-of-dicom-studies)

			 @input resources (list): The UIDS of DICOM resources (studies, series, and/or instances) 
			 	to be merged into the study of interest.
			 @input asynchronous (boolean, default=False): If true, run the job in asynchronous mode.
			 	When run asynchronously, the REST API call will immediately return, reporting the identifier 
			 	of a job. The job instance can be used to retrieve the status of the job.
			 @input keep_source (bool, default=False): If set to true, instructs Orthanc to keep 
			 	a copy of the original series in the source study. By default, the original 
			 	resources are deleted from Orthanc.
			 @input permissive (permissive, default=False): If true, ignore errors during the individual 
			 	steps of the job.
			 @input priority (int, default=0): The priority of the job. The lower the number, 
			 	the higher the priority.
			 @input merge (iterable, default=None): iterable ot tags to be removed outside of those
			 	specified in the standard.

			 @returns requests.Response
		'''
		merge = merge or {}
		if verify is None:
			verify = self.server.verify

		# Create request structure
		merge.update({ 
			'Asynchronous': asynchronous, 
			'Permissive': permissive,
			'KeepSource': keep_source,
			'Priority': priority,
			'Resources': resources,
			'Synchronous': False
		})

		return self._merge_split_request(merge, 'merge', asynchronous, headers, verify)
	
	def split_study(self, resources: list, asynchronous: bool=False, keep_source: bool=False, permissive: bool=True, priority: int=0, 
			remove: list=None, replace: dict=None, split: dict=None, verify=None, headers=None, **kwargs):
		'''	Remove the DICOM series specified in resources from the current study and placing them in a new study.
			The new study is created by setting the StudyInstanceUID of the specified series to a new value.
			(Refer to https://book.orthanc-server.com/users/anonymization.html#split-merge-of-dicom-studies.)

			 @input resources (list): the list of series UIDs to be split from the current study. (Must
			 	be part of the current study.)
			 @input asynchronous (boolean, default=False): If true, run the job in asynchronous mode. When asynchronously,
			 	REST API call will immediately return, reporting the identifier of a job. The job instance can be used
			 	to retrieve the status of the job.
			 @input keep_source (bool, default=False): If set to true, instructs Orthanc to keep a copy of 
			 	the original resources in their source study. By default, the original resources are deleted from Orthanc.
			 @input permissive (permissive, default=False): If true, ignore errors during the individual steps of the job.
			 @input priority (iterable, default=None): In asynchronous mode, the priority of the job. 
			 	The lower the value, the higher the priority.
			 @input remove (list, default=None): List of tags that must be removed in the new study 
			 	(from the same modules as in the Replace option).
			 @input replace (dict, default=None): Associative array to change the value of some 
			 	DICOM tags in the new study. The tags must be part of the "Patient Module Attributes" 
			 	or the "General Study Module Attributes",  as specified by the DICOM 2011 standard in Tables 
			 	C.7-1 and C.7-3.

			 @returns requests.Response
		'''
		
		# Check resources are available
		for series in resources:
			if series not in self.series:
				raise ValueError('Invalid series UID. %s not in study.' % (series))

		# Request options
		split = split or {}
		if verify is None:
			verify = self.server.verify

		# Create request structure adding the series retrieved from the search
		split.update({ 
			'Asynchronous': asynchronous, 
			'Permissive': permissive,
			'KeepSource': keep_source,
			'Priority': priority,
			'Series': resources,
		})

		# DICOM tags to be replaced
		if replace:
			split.update({'Replace': replace})

		# DICOM tags to be removed 
		if remove:
			split.update({'Remove': remove})

		return self._merge_split_request(split, 'split', asynchronous, headers, verify)
		
	def _merge_split_request(self, data_send: dict, endpoint: str, asynchronous=False, headers=None, verify=None, **kwargs):
		'''	Function that is responsible for communication with the server and executing the request
		'''
		# Execute operation
		logger.debug('Structure of merge/split request:\n%s' % json.dumps(data_send))
		r = requests.post(self.pacs.orthanc_apiurl(posixpath.join(self.resource_url, endpoint)), json=data_send,
			headers=self.pacs.orthanc_request_headers(headers=headers), verify=verify)
		
		# Check operation results
		if not r.ok:
			request_client_error(
				'Unable to merge DICOM resource tags for %s on server %s. Status code: %s.'
					% (self.resource_url, self.pacs.server_label, r.status_code),
				r)

		logger.debug('Response from PACS imaging server:\n%s' % r.content)

		# Retrieve job instance 
		if asynchronous:
			response_json = r.json()
			from .jobs import OrthancJob
			return self.pacs.get_imaging_resource(response_json['ID'], OrthancJob, headers=headers, **kwargs)
		
		# Retrieve new imaging study
		else:
			response_json = r.json()
			return self.pacs.get_imaging_resource(response_json['TargetStudy'], ImagingStudy, headers=headers, **kwargs)


class ImagingStudyCollection(ImagingResourceBaseCollection):
	'''	Collection of imaging studies
	'''
	model = ImagingStudy

	def __init__(self, *args, **kwargs):
		self.parent = kwargs.pop('patient', None)
		super().__init__(*args, **kwargs)

	def _init_collection_models(self, **kwargs):
		if self.parent:
			kwargs['patient'] = self.parent

		return super()._init_collection_models(**kwargs)

	def bulkpopulate_related(self, *args, parent_patient=True, child_series=True, **kwargs):
		'''	Populate models related to collection instances in the most efficient manner possible.

			@input parent_patient (bool, default=True): bulk populate "parent" attribute of collection instances.
			@input child_series (bool, default=True): bulk populate the "series_collection" attribute
				of model instances.
		'''
		# Retrieve patient and sibling series data. Both types of resources can be retrieved in a single request.
		if parent_patient or child_series:

			# Aggregate resouce UIDs
			patient_uids = []
			child_uids = []

			for s in self:
				if parent_patient: patient_uids.append(s.patient)
				if child_series: child_uids.extend(s.series)

			# Retrieve bulk resources and unpack
			bdata = self.pacs.fetch_bulk_content(patient_uids+child_uids, *args, **kwargs)
			bdata_patient = bdata.get(IMAGING_SERVER_RESOURCE_PATIENT)
			bdata_series = bdata.get(IMAGING_SERVER_RESOURCE_SERIES)

			# Unpack data
			for s in self:

				# Patient
				if bdata_patient and bdata_patient.get_modelinstance(s.patient):
					s.parent = s.parent_from_json(
						bdata_patient.get_modelinstance(s.patient)._objectdata)

				# Sibling series, DICOM-SR, and DICOM-SEG attributes
				if bdata_series:
					s.series_collection = s.series_from_json(
						[bdata_series.get_modelinstance(sid)._objectdata for sid in s.series if bdata_series.get_modelinstance(sid)])
					s._populate_subcollections()


IMAGING_SERIES_OUTPUT_COLUMNS = OrderedDict((
		('study', 'Parent Study'),
		('pk', 'Series ID'),
		('modality', 'Modality'),
		('sequence_name', 'Name'),
		('series_number', 'Number'),
		('series_datestr', 'Date'),
		('series_timestr', 'Time'),
		('series_uid', 'UID'),
		('body_part', 'Body Part Examined'),
		('description', 'Description'),
	))


FileDataResponse = namedtuple('FileDataRequest', ('buffer', 'response'))


class ImagingSeriesCoreResource(ImagingResourceMixin, ImagingResourceParentMixin, ImagingServerChildBaseObject):
	'''	Imaging series: set of grouped images
	'''
	pk_attr = 'ID'
	tabulate_output_columns = IMAGING_SERIES_OUTPUT_COLUMNS
	fetch_endpoint = 'series'
	cache_queryurl = '/cache/series'

	def __init__(self, *args, **kwargs):
		self._parent = kwargs.pop('study', None) or kwargs.pop('patient', None)
		super().__init__(*args, **kwargs)

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
	def cache_indexurl(self):
		return posixpath.join(self.cache_queryurl, self.pk, 'index')

	@property
	def study(self):
		return self._objectdata.get('ParentStudy')

	@property
	def parent(self):
		'''	Retrieve parent study for the series
		'''
		if getattr(self, '_parent', None) is None:
			self._parent = self.pacs.get_study(self.study)

			# Propagate cache lookup settings of current instance
			if getattr(self, 'resource_cache_lookup', None) is not None:
				setattr(self._parent, 'resource_cache_lookup', self.resource_cache_lookup)

		return self._parent

	@parent.setter
	def parent(self, study_model):
		'''	Sets the parent study for the series
		'''
		if not isinstance(study_model, ImagingStudy):
			raise ValueError('Input must be an instance of a study')

		setattr(self, '_parent', study_model)

	def parent_from_json(self, jdata, **kwargs):
		'''	Initialize study model from the provided JSON data.
		'''
		return self.server._init_dataclass_from_json(ImagingStudy, jdata, pacs=self.pacs, **kwargs)

	@property
	def model_study(self):
		return self.parent

	@property
	def model_patient(self):
		return self.model_study.parent

	@property
	def sequence_name(self):
		return self.dicomdata.get('SequenceName')

	@property
	def modality(self):
		return self.dicomdata.get(DCMHEADER_MODALITY)

	@property
	def description(self):
		return self.dicomdata.get(DCMHEADER_SERIES_DESCRIPTION)

	@property
	def series_number(self):
		return self.dicomdata.get(DCMHEADER_SERIES_NUMBER)

	@property
	def series_datestr(self):
		'''	DICOM string representation of when the series was acquired. (Created from the SeriesDate header.)
		'''
		return self.dicomdata.get(DCMHEADER_SERIES_DATE)

	@property
	def series_date(self):
		'''	Date that the series was acquired. (Parsed from series_datestr.)
		'''
		if getattr(self, '_sdate', None) is None and self.series_datestr:
			self._sdate = dcm_str2date(self.series_datestr)

		return getattr(self, '_sdate', None)

	@property
	def series_timestr(self):
		'''	DICOM string representation of when the series was acquired. (Created from the SeriesTime header.)
		'''
		return self.dicomdata.get(DCMHEADER_SERIES_TIME)

	@property
	def series_time(self):
		'''	Time that the series was acquired. (Parsed from series_timestr.)

			@returns datetime.time
		'''
		if getattr(self, '_stime', None) is None and self.series_timestr:
			self._stime = dcm_str2time(self.series_timestr)
	
		return getattr(self, '_stime', None)

	@property
	def ts(self):
		'''	Date/time that the series was acquired. (Created from series_date and series_time properties.)
			Returns None is there is no series date value. Series time is used if available, with midnight
			being used if it is not.
		'''
		if getattr(self, '_ts', None) is None and self.series_date:

			# Create timestamp by grouping series date and series time
			sdate = self.series_date
			stime = self.series_time or datetime.time(0,0,0)				
			self._ts = datetime.datetime.combine(sdate, stime)

		return getattr(self, '_ts', None)

	@property
	def series_uid(self):
		return self.dicomdata.get(DCMHEADER_SERIES_INSTANCE_UID)

	@property
	def body_part(self):
		return self.dicomdata.get(DCMHEADER_BODY_PART_EXAMINED)

	@property
	@abstractmethod
	def dcminstance_modelcollection_class(self):
		'''	Model collection class that should be used to initialize instances associated with the series
		'''

	def fetch_dcminstances(self, **kwargs):
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
		return self.server._init_dataclass(
			self.dcminstance_modelcollection_class, r, pacs=self.pacs, series=self, **kwargs)

	def dcminstances_from_json(self, **kwargs):
		'''	Retrieve details for slices in the series

			@returns collection of DICOM instances
		'''
		return self.server._init_dataclass_from_json(
			self.dcminstance_modelcollection_class, jdata, pacs=self.pacs, series=self, **kwargs)


class ImagingSeries(ImagingSeriesCoreResource):
	'''	Imaging series: set of grouped images
	'''
	@property
	def dcminstance_modelcollection_class(self): return DcmInstanceCollection

	@property
	@functools.lru_cache()
	def image_orientation_patient(self):
		'''	Retrieve the image orientation, which specifies the direction cosines of the first row
			and the first column with respect to the patient. Corresponds to the ImageOrientationPatient
			DCM header. If the header is not present, the returned value will be None.

			@returns  pair of tuples: row value for the x, y, z axis followed by column value for the x, y, z
				or None if the ImageOrientationPatient header is not present.
		'''
		return parse_image_orientation(self.dicomdata.get(DCMHEADER_IMAGE_ORIENTATION_PATIENT))

	@property
	def slices(self):
		'''	Retrieve instance UIDs for the series
		'''
		return self._objectdata.get('Instances')

	@property
	def slices_collection(self):
		'''	Cached property for retrieving the slice/image instances which belong to the series
		'''
		if getattr(self, '_slices', None) is None:
			setattr(self, '_slices', self.fetch_dcminstances())

		return self._slices

	@slices_collection.setter
	def slices_collection(self, instances_collection):
		'''	Set slice/image instances which belong to the series
		'''
		if not isinstance(instances_collection, self.dcminstance_modelcollection_class):
			raise ValueError("Input must be a instance of a DICOM instances collection")

		setattr(self, '_slices', instances_collection)
		
	@property
	@functools.lru_cache()
	def shape(self):
		'''	Cached property that retrieves the dimensions of the image volume
		'''
		dcm0 = self.slices_collection[0]
		return ImageStackShape(
			len(self.slices), dcm0.dcmfile(cache=True).Rows, dcm0.dcmfile(cache=True).Columns)

	@property
	@functools.lru_cache()
	def segmentations(self):
		'''	DICOM-SEG segmentations associated with the imaging series. Sorted with the most recent segmentations first.
		'''
		return sorted(
			[dcmseg for dcmseg in self.parent.seg_collection if self.series_uid in dcmseg.series_reference_uids],
			key=lambda dcmseg: dcmseg.ts if dcmseg.ts else datetime.datetime(year=1900, month=1, day=1),
			reverse=True)

	@property
	@functools.lru_cache()
	def annotations(self):
		'''	DICOM-SR annotations associated with the series with most recent reports first.
		'''
		return sorted(
			[dcmsr for dcmsr in self.parent.sr_collection if self.series_uid in dcmsr.series_reference_uids],
			key=lambda dcm: dcm.ts if dcm.ts else datetime.datetime(year=1900, month=1, day=1),
			reverse=True)



class ImagingSeriesBulkPopulateMixin:
	'''	Mixin class which adds methods to enable efficient population of related models 
		(parent study and patient) on instances within series collections.
	'''
	def bulkpopulate_related(self, *args, parent_study=True, parent_patient=True, sibling_series=True, **kwargs):
		'''	Populate models related to collection instances in the most efficient manner possible.

			@input parent_study (bool, default=True): bulk populate the "parent" attribute of 
				collection instances.
			@input parent_patient (bool, default=True): bulk populate the "model_patient" attribue
				of collection instances. Requires that parent_study option be True.
			@input sibling_series (bool, default=True): bulk populate the "series_collection" attribute
				of the parent study. Requires that parent_study option be True.
		'''
		# For populating patient or sibling data, ensure that the parent study will be available.
		if (parent_patient or sibling_series) and not parent_study:
			raise ValueError('Unable to populate patient or sibling data, options requires parent study.')

		# Retrieve paarent study data. Study instances are required to populate the patient and 
		# sibling series properties, and must be retreived first.
		if parent_study:
			bdata = self.pacs.fetch_bulk_content([sx.study for sx in self], *args, **kwargs)
			bdata_study = bdata.get(IMAGING_SERVER_RESOURCE_STUDY)
			
			# Unpack study data and add to series
			if bdata_study:
				for sx in self:
					s = bdata_study.get_modelinstance(sx.study)
					if s: sx.parent = sx.parent_from_json(s._objectdata)

		# Retrieve patient and sibling series data. Both types of resources can be retrieved
		# in a single request.
		if parent_patient or sibling_series:

			# Aggregate resource UIDs
			patient_uids = []
			sibling_uids = []

			for sx in self:
				if parent_patient: patient_uids.append(sx.parent.patient)
				if sibling_series: sibling_uids.extend(sx.parent.series)

			# Retrieve bulk resources and unpack
			bdata = self.pacs.fetch_bulk_content(patient_uids+sibling_uids, *args, **kwargs)
			bdata_patient = bdata.get(IMAGING_SERVER_RESOURCE_PATIENT)
			bdata_series = bdata.get(IMAGING_SERVER_RESOURCE_SERIES)

			# Unpack data
			for sx in self:

				# Patient
				if bdata_patient and bdata_patient.get_modelinstance(sx.parent.patient):
					sx.parent.parent = sx.parent.parent_from_json(
						bdata_patient.get_modelinstance(sx.parent.patient)._objectdata)

				# Sibling series, DICOM-SR, and DICOM-SEG attributes
				if bdata_series:
					sx.parent.series_collection = sx.parent.series_from_json(
						[bdata_series.get_modelinstance(sid)._objectdata for sid in sx.parent.series if bdata_series.get_modelinstance(sid)])
					sx.parent._populate_subcollections()


class ImagingSeriesCollection(ImagingSeriesBulkPopulateMixin, ImagingResourceBaseCollection):
	''' Collection of imaging series
	'''
	model = ImagingSeries

	def __init__(self, *args, **kwargs):

		# Retrieve parent of the collection, ensure that it is an imaging patient or study
		self.parent = kwargs.pop('study', None) or kwargs.pop('patient', None)
		if self.parent and not isinstance(self.parent, (ImagingPatient, ImagingStudy)):
			raise ValueError('Unable to initialize imaging series, invalid parent type: %s' % type(self.parent))
		
		super().__init__(*args, **kwargs)

	def _init_collection_models(self, **kwargs):
		if self.parent:

			# Determine which keyword should be used for the parent. ImagingSeriesCollections
			# can be initialized either from a study or patient.
			if isinstance(self.parent, ImagingPatient):
				kwargs['patient'] = self.parent
			elif isinstance(self.parent, ImagingStudy):
				kwargs['study'] = self.parent

		return super()._init_collection_models(**kwargs)




IMAGING_INSTANCE_OUTPUT_COLUMNS = OrderedDict((
		('series', 'Series'),
		('pk', 'Instance UID'),
		('sop_instance_uid', 'SOP Instance UID'),
	))


class DcmInstanceCoreResource(ImagingResourceCoreMixin, ImagingResourceParentMixin, ImagingServerChildBaseObject):
	'''	Model used for DCM instance data
	'''
	pk_attr = 'ID'
	fetch_endpoint = 'instances'

	def __init__(self, *args, **kwargs):
		self._parent = kwargs.pop('series', None)
		super().__init__(*args, **kwargs)

	@property
	def series(self):
		return self._objectdata.get('ParentSeries')

	@property
	def series_index(self):
		return self._objectdata.get('IndexInSeries')

	@property
	def sop_instance_uid(self):
		return self.dicomdata.get('SOPInstanceUID')

	@property
	def parent(self):
		'''	Retrieve the parent series for the instance
		'''
		if self._parent is None:
			self._parent = self.pacs.get_series(self.series)

			# Propagate cache lookup settings of current instance
			if getattr(self, 'resource_cache_lookup', None) is not None:
				setattr(self._parent, 'resource_cache_lookup', self.resource_cache_lookup)

		return self._parent

	@property
	def model_series(self):
		return self.parent

	@property
	def model_study(self):
		return self.model_series.parent

	@property
	def model_patient(self):
		return self.model_study.parent

	@property
	def resource_url(self):
		return posixpath.join(self.fetch_endpoint, self.pk)

	@property
	def tags(self):
		'''	Dictionary/JSON of all tags associated with the image
		'''
		if getattr(self, '_tags', None) is None:
			
			r = requests.get(
				self.pacs.orthanc_apiurl(posixpath.join(self.resource_url, 'simplified-tags'), query_params={ 'expand': True, }),
				headers=self.pacs.orthanc_request_headers())
			
			if not r.ok:
				request_client_error(
					'Unable to retrieve tags for DCM instance %s on server %s. Status code: %s.' % (self.pk, self.pacs.server_label, r.status_code),
					r)

			self._tags = r.json()

		return self._tags

	@property
	def dcmtags(self):
		'''	Dictionary/JSON of all DICOM tags including hexadecimal indexes and value type
		'''
		if getattr(self, '_dcmtags', None) is None:
			
			r = requests.get(
				self.pacs.orthanc_apiurl(posixpath.join(self.resource_url, 'tags'), query_params={ 'expand': True, }),
				headers=self.pacs.orthanc_request_headers())
			
			if not r.ok:

				request_client_error(
					'Unable to retrieve full DCM tags for DCM instance %s on server %s. Status code: %s.' % (self.pk, self.pacs.server_label, r.status_code),
					r)

			self._dcmtags = r.json()

		return self._dcmtags

	def _get_filedata(self, dcmresource_url, verify=None, headers=None):
		'''	Retrieve DICOM resource data

			@returns io.BytesIO stream
		'''
		if verify is None:
			verify = self.server.verify

		# Retrieve file data from Orthanc
		r = requests.get(
			self.pacs.orthanc_apiurl(dcmresource_url), headers=self.pacs.orthanc_request_headers(headers=headers), verify=verify)
		if not r.ok:
			request_client_error(
				'Unable to retrieve DICOM resource file data for %s (instance %s) on server %s. Status code: %s.'
					% (dcmresource_url, self.pk, self.pacs.server_label, r.status_code),
				r)

		# Initialize DICOM instance from request data, attach the raw content of the request
		return FileDataResponse(BytesIO(r.content), r)

	def dcmfile(self, cache=False, **kwargs):
		'''	Retrieve the raw DICOM data for the instance.

			@input cache (bool, default=False): Cache the data locally to speed up access.

			@returns pydicom.dataset.FileDataset
		'''
		# Retrieve cached copy of the file (if available)
		if getattr(self, '_dcmfile', None):
			return self._dcmfile
		
		fbuffer, _ = self._get_filedata(posixpath.join(self.resource_url, 'file'), **kwargs)
		dfile = pydicom.dcmread(fbuffer)
		setattr(dfile, 'raw', fbuffer)

		# Cache (if indicated)
		if cache:
			setattr(self, '_dcmfile', dfile)

		return dfile


class DcmInstance(DcmInstanceCoreResource):
	'''	DCM instance model used for imaging data
	'''

	def imgfile(self, stretch_dynamicrange=True, bitdepth=8, **kwargs):
		'''	Retrieve image file data from Orthanc

			@input stretch_dynamicrange (bool, default=True): When True, signed intger
				data stretched to the full dynamic range of the encoding type will be retrieved.
			@input bitdepth (iint, default=8): Bitdepth of the image

			@returns io.BytesIO
		'''
		# 8 bit stretched image where pixel data is set to [0..255]
		if stretch_dynamicrange and bitdepth == 8:
			dcmresource_url = posixpath.join(self.resource_url, 'preview')

		# 8 bit unsigned image where pixel data is left unmodified.
		# Pixel intensities are cropped to the maximal value encoded by the target image format.
		elif not stretch_dynamicrange and bitdepth == 8:
			dcmresource_url = posixpath.join(self.resource_url, 'image-uint8')

		# 16 bit unsigned image: pixel intensities are coppred to the maximal value encoded by the target image format.
		elif not stretch_dynamicrange and bitdepth == 16:
			dcmresource_url = posixpath.join(self.resource_url, 'image-uint16')

		# 16 bit signed image
		elif stretch_dynamicrange and bitdepth == 16:
			dcmresource_url = posixpath.join(self.resource_url, 'image-int16')

		fbuffer, _ = self._get_filedata(dcmresource_url, **kwargs)

		return fbuffer

	def pngfile(self, cache=False, **kwargs):
		'''	Retrieve a full-resolution PNG grayscale preview of the DCM file. Wraps imgfile

			@returns io.BytesIO
		'''
		if getattr(self, '_pngfile', None):
			return self._pngfile

		pbuffer = self.imgfile(**kwargs)

		# Cache (if indicated)
		if cache:
			setattr(self, '_pngfile', pbuffer)

		return pbuffer

	def jpegfile(self, cache=False, **kwargs):
		'''	Retrieve a full-resolution JPEG grayscale preview of the DCM file. Wraps imgfile.

			@returns io.BytesIO
		'''
		if getattr(self, '_jpegfile', None):
			return self._jpegfile

		headers = kwargs.get('headers') or {}
		headers['Accept'] = 'image/jpeg'

		jbuffer = self.imgfile(headers=headers, **kwargs)

		# Cache (if indicated)
		if cache:
			setattr(self, '_jpegfile', jbuffer)

		return jbuffer

	@property
	@functools.lru_cache()
	def image_position_patient(self):
		'''	Retrieve the image position in the MRI/patient coordinate system.
			Corresponds to the ImagePositionPatient header. The provided
			coordinate is for the upper left hand corner of the image, the center of the first
			voxel transmitted.

			@returns tuple (x, y, z) with image coordinates or None if the 
				header is not present
		'''
		# Retrieve patient position from the DICOM tags
		coord = self.tags.get(DCMHEADER_IMAGE_POSITION_PATIENT)

		# Split into x, y, z coordinates by delimiter. Try '\', ',', before
		# falling back to ' '
		if isinstance(coord, six.string_types):
			coord = coord.split('\\' if '\\' in coord 
				else ',' if ',' in coord
				else ' ')

		# Return values as coordinate or None
		return ImageCoord(*tuple(float(v) for v in coord)) if (coord and len(coord) == 3) \
			else coord

	@property
	@functools.lru_cache()
	def image_orientation_patient(self):
		'''	Retrieve the image orientation, which specifies the direction cosines of the first row
			and the first column with respect to the patient. Corresponds to the ImageOrientationPatient
			DCM header.

			@returns  pair of tuples: row value for the x, y, z axis followed by column value for the x, y, z
		'''
		return parse_image_orientation(
			self.tags.get(DCMHEADER_IMAGE_ORIENTATION_PATIENT))

	@property
	@functools.lru_cache()
	def slice_location(self):
		'''	Retrieve the slice location within the image volume. The location is taken from the SliceLocation header
			and will return None if the header is not present.
		@returns float or None
		'''
		zval = self.tags.get('SliceLocation')		
		return float(zval) if isinstance(zval, six.string_types) else zval

	@property
	@functools.lru_cache()
	def slice_thickness(self):
		'''	Retrieve the slice thinkess. The thickness is taken from the SliceThickness header and will return
			None if the header is not present.

			@returns float or None
		'''
		thickness = self.tags.get('SliceThickness')
		return float(thickness) if isinstance(thickness, six.string_types) else thickness

	@property
	@functools.lru_cache()
	def pixel_spacing(self):
		'''	Retrieve the pixel spacing for the slice. The spacing components are retrieved from the PixelSpacing
			header and will return None if the header is not present.

			@returns tuple or None
		'''
		spacing = self.tags.get('PixelSpacing')

		# Split the spacing into x and y components
		if isinstance(spacing, six.string_types):
			spacing = spacing.split('\\' if '\\' in spacing \
				else ',' if ',' in spacing 
				else '')

			# Ensure that all expected values are present
			if not len(spacing) == 2:
				raise ValueError('Invalid pixel spacing, unable to find x/y spacing components')

			# Unpack components of the tuple in x, y, and thickness
			spacing = ImageSpacing(float(spacing[0]), float(spacing[1]), self.slice_thickness)

		return spacing

	@property
	@functools.lru_cache()
	def plane_type(self):
		return int(self.tags.get('PlaneType')) if self.tags.get('PlaneType') \
			else self.tags.get('PlaneType')


class DcmInstanceCoreCollection(ImagingServerChildCollection):
	'''	Mixin object that provides convenience methods for working with collections of
		DICOM instances (imaging and DICOM-SR objects)
	'''
	def __init__(self, *args, **kwargs):
		self.parent = kwargs.pop('series', None)
		super().__init__(*args, **kwargs)

	def _init_collection_models(self, **kwargs):
		if self.parent:
			kwargs['series'] = self.parent

		# Return a sorted copy of the collection so that the instances are ordered
		# by their index
		return sorted(
			super()._init_collection_models(**kwargs),
			key=lambda i: i.series_index or 0)

	@property
	def dcmfiles(self):
		''' Iterable of all DICOM files instances in the collection. Implemented as 
			a generator which retrieves the DICOM file from the instances. A reference 
			to the DICOM file is stored on the individual slice.
		'''
		for dcm in self:
			yield dcm.dcmfile(cache=True)


class DcmInstanceCollection(DcmInstanceCoreCollection):
	'''	Collection of image instances
	'''
	model = DcmInstance
