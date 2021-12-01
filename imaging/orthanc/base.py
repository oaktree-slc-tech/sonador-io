''' Model classes associated with Orthanc DICOM resources. Provides tools
	for representing, queryying, modifying, and removing core DICOM resource instances.
'''
import six, requests, json, csv, collections, logging, functools, posixpath, zipfile, pydicom, datetime
from io import BytesIO

from abc import ABCMeta, abstractmethod

from urllib.parse import urlencode

from collections import namedtuple
from collections import Iterable
from collections import OrderedDict

from tabulate import tabulate

from client import auth as guru_auth
from client.utils.urls import build_url
from client.utils.object import pick
from client.utils.microservices import server_controloperation_json_response, RemotePage

from ...apisettings import IMAGING_SERVER_RESOURCE_PATIENT, IMAGING_SERVER_RESOURCE_STUDY, IMAGING_SERVER_RESOURCE_SERIES, \
	DCMHEADER_PATIENT_ID, DCMHEADER_PATIENT_NAME, DCMHEADER_PATIENT_SEX, DCMHEADER_PATIENT_BIRTHDATE, \
	DCMHEADER_IMAGE_POSITION_PATIENT, DCMHEADER_IMAGE_ORIENTATION_PATIENT, DCM_DATE_STRFORMAT, DCM_TIME_STRFORMAT, \
	DCMHEADER_MODALITY, DCMHEADER_STUDY_INSTANCE_UID
from ...helpers import request_client_error, fetch_sonador_session_token
from ...serialization import json_datetime_parser, json_str2datetime, dcm_str2time
from ...remote import SonadorBaseObject, SonadorObjectCollection, fetch_sonador_data_collection
from ...servers import ImagingServerChildCollection, ImagingServerBaseObject

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
		return self.dicomdata.get(DCMHEADER_PATIENT_NAME)

	@property
	def patientid(self):
		return self.dicomdata.get(DCMHEADER_PATIENT_ID)

	@property
	def patient_sex(self):
		return self.dicomdata.get(DCMHEADER_PATIENT_SEX)

	@property
	def birth_date(self):
		return self.dicomdata.get(DCMHEADER_PATIENT_BIRTHDATE)

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
				'Unable to retrieve details for patient %s studies on server %s. Status code: %s.' % (self.pk, self.pacs.server_label, r.status_code),
				r)

		# Parse response and return collection
		return self.server._init_dataclass(ImagingStudyCollection, r, pacs=self.pacs, patient=self, **kwargs)

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

	@property
	def series_collection(self):
		'''	Cached property for retrieving the series instances associated with the patient
		'''
		if getattr(self, '_series', None) is None:
			setattr(self, '_series', self.fetch_series())

		return self._series


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


class ImagingStudy(ImagingResourceMixin, ImagingResourceParentMixin, ImagingServerBaseObject):
	'''	Imaging study: set of sequences/series/scans
	'''
	pk_attr = 'ID'
	tabulate_output_columns = IMAGING_STUDY_OUTPUT_COLUMNS
	fetch_endpoint = 'studies'

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
	def patient(self):
		return self._objectdata.get('ParentPatient')

	@property
	def parent(self):
		'''	Retrieve the parent patient for the study
		'''
		if getattr(self, '_parent', None) is None:
			self._parent = self.pacs.get_patient(self.patient)

		return self._parent

	@property
	def model_patient(self):
		return self.parent

	@property
	def study_uid(self):
		return self.dicomdata.get(DCMHEADER_STUDY_INSTANCE_UID)

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
		return self.server._init_dataclass(ImagingSeriesCollection, r, pacs=self.pacs, patient=self, **kwargs)

	@property
	def series_collection(self):
		'''	Series instances associated with the study
		'''
		if getattr(self, '_series', None) is None:
			setattr(self, '_series', self.fetch_series())

		return self._series

	def fetch_sr(self, **kwargs):
		'''	Fetch the DICOM-SR instances that are associated with the study
		'''
		return self.pacs.query_sr({ DCMHEADER_STUDY_INSTANCE_UID: self.study_uid, }, **kwargs)

	@property
	def sr_collection(self):
		'''	DICOM-SR instances associated with the study
		'''
		if getattr(self, '_sr', None) is None:
			setattr(self, '_sr', self.fetch_sr())

		return self._sr

	def fetch_seg(self, **kwargs):
		'''	Fetch the DICOM-SEG instances that are associated with the study
		'''
		return self.pacs.query_seg({ DCMHEADER_STUDY_INSTANCE_UID: self.study_uid }, **kwargs)

	@property
	def seg_collection(self):
		'''	DICOM-SEG instances associated with the study
		'''
		if getattr(self, '_seg', None) is None:
			setattr(self, '_seg', self.fetch_seg())

		return self._seg


class ImagingStudyCollection(ImagingServerChildCollection):
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


class ImagingSeriesCoreResource(ImagingResourceMixin, ImagingResourceParentMixin, ImagingServerBaseObject):
	'''	Imaging series: set of grouped images
	'''
	pk_attr = 'ID'
	tabulate_output_columns = IMAGING_SERIES_OUTPUT_COLUMNS
	fetch_endpoint = 'series'

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
	def study(self):
		return self._objectdata.get('ParentStudy')

	@property
	def parent(self):
		'''	Retrieve parent study for the series
		'''
		if getattr(self, '_parent', None) is None:
			self._parent = self.pacs.get_study(self.study)

		return self._parent

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
		return self.dicomdata.get('SeriesDescription')

	@property
	def series_number(self):
		return self.dicomdata.get('SeriesNumber')

	@property
	def series_datestr(self):
		'''	DICOM string representation of when the series was acquired. (Created from the SeriesDate header.)
		'''
		return self.dicomdata.get('SeriesDate')

	@property
	def series_date(self):
		'''	Date that the series was acquired. (Parsed from series_datestr.)
		'''
		if getattr(self, '_sdate', None) is None:
			if self.series_datestr:
				self._sdate = datetime.datetime.strptime(self.series_datestr, DCM_DATE_STRFORMAT).date()
			else: self._sdate = None

		return self._sdate

	@property
	def series_timestr(self):
		'''	DICOM string representation of when the series was acquired. (Created from the SeriesTime header.)
		'''
		return self.dicomdata.get('SeriesTime')

	@property
	def series_time(self):
		'''	Time that the series was acquired. (Parsed from series_timestr.)

			@returns datetime.time
		'''
		if getattr(self, '_stime', None) is None:
			if self.series_timestr:
				self._stime = dcm_str2time(self.series_timestr)
			else: self._stime = None
	
		return self._stime

	@property
	def ts(self):
		'''	Date/time that the series was acquired. (Created from series_date and series_time properties.)
		'''
		if getattr(self, '_ts', None) is None:

			# Create timestamp by grouping series date and series time
			if self.series_date and self.series_time:
				self._ts = datetime.datetime.combine(self.series_date, self.series_time)
			
			# Unable to create timestamp, set to None
			else: self._ts = None

		return self._ts

	@property
	def series_uid(self):
		return self.dicomdata.get('SeriesInstanceUID')

	@property
	def body_part(self):
		return self.dicomdata.get('BodyPartExamined')

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
		'''	DICOM-SEG segmentations associated with the series with most recent segmentations first.
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


class ImagingSeriesCollection(ImagingServerChildCollection):
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


class DcmInstanceCoreResource(ImagingResourceCoreMixin, ImagingResourceParentMixin, ImagingServerBaseObject):
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
		return float(thickness) if isinstance(thickness, six.string_types) else zval

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
