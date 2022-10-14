import six, logging
from collections import OrderedDict

from ..apisettings import IMAGING_SERVER_RESOURCE_PATIENT, IMAGING_SERVER_RESOURCE_STUDY, IMAGING_SERVER_RESOURCE_SERIES, \
	DCMHEADER_STUDY_INSTANCE_UID, DCMHEADER_SERIES_INSTANCE_UID, DCMHEADER_SEQUENCE_NAME, DCMHEADER_MODALITY, \
	DCMHEADER_SERIES_DESCRIPTION, DCMHEADER_SERIES_NUMBER, DCMHEADER_BODY_PART_EXAMINED, \
	DCMHEADER_ACCESSION_NUMBER, DCMHEADER_STUDY_DATE, DCMHEADER_STUDY_TIME, DCMHEADER_REFERRING_PHYSICIAN, \
	DCMHEADER_PATIENT_SEX, DCMHEADER_PATIENT_ID, DCMHEADER_PATIENT_NAME
from ..remote import SonadorBaseObject, SonadorObjectCollection

logger = logging.getLogger(__name__)


def dicomweb2keyval(dicomweb_raw, odata=None, name_attr='Name', value_attr='Value'):
	'''	Convert a raw dicomweb data response to a structure mapped to key/value pairs
	'''
	odata = odata or {}

	# Iterate through all DICOMweb attributes and extract 'Name' and 'Value' elements.
	# Re-map in a new dictionary
	for dk, dv in six.iteritems(dicomweb_raw):
		if name_attr in dv and value_attr in dv:
			odata[dv.get(name_attr)] = dv.get(value_attr)

	return odata


class RemoteImagingBaseObject(SonadorBaseObject):
	'''	Data object associated with a DICOMCweb remote. Includes a reference to the 
		DICOMweb server from which the object came.
	'''
	def __init__(self, server, dicomweb_raw, *args, **kwargs):
		self.dicomweb = kwargs.pop('dicomweb', None)
		self.dicomweb_raw = dicomweb_raw

		super(RemoteImagingBaseObject, self).__init__(server, dicomweb2keyval(dicomweb_raw), *args, **kwargs)


class RemoteImagingObjectCollection(SonadorObjectCollection):
	'''	Collection which can be used to work with data models associated with 
		remote DICOMweb instances
	'''

	def __init__(self, *args, **kwargs):
		self.dicomweb = kwargs.pop('dicomweb', None)
		super(RemoteImagingObjectCollection, self).__init__(*args, **kwargs)

	def _init_collection_models(self, **kwargs):
		if self.dicomweb:
			kwargs['dicomweb'] = self.dicomweb

		return super(RemoteImagingObjectCollection, self)._init_collection_models(**kwargs)


class RemoteImagingPatientDataMixin(object):
	'''	Mixin class which can be added to remote object models to provide
		convenience methods for patient data.
	'''
	@property
	def patient(self):
		return self._objectdata.get('ParentPatient')

	@property
	def patient_name(self):
		return self._objectdata.get(DCMHEADER_PATIENT_NAME)

	@property
	def patientid(self):
		return self._objectdata.get(DCMHEADER_PATIENT_ID)

	@property
	def patient_sex(self):
		return self._objectdata.get(DCMHEADER_PATIENT_SEX)


class RemoteImagingStudyMixin(object):
	'''	Mixin class which can be added to remote object models to provide convenience methods
		for study data.
	'''
	@property
	def accession_number(self):
		return self._objectdata.get(DCMHEADER_ACCESSION_NUMBER)

	@property
	def study_date(self):
		return self._objectdata.get(DCMHEADER_STUDY_DATE)

	@property
	def study_time(self):
		return self._objectdata.get(DCMHEADER_STUDY_TIME)

	@property
	def physician(self):
		return self._objectdata.get(DCMHEADER_REFERRING_PHYSICIAN)



# DICOMweb Imaging

REMOTE_IMAGING_STUDY_OUTPUT_COLUMNS = OrderedDict((
		('pk', 'Study ID'),
		('patient_name', 'Patient Name'),
		('patientid', 'MRN'),
		('accession_number', 'Accession#'),
		('study_date', 'Study Date'),
		('physician', 'Requesting Physician'),
		('description', 'Description'),
	))


class RemoteImagingStudy(RemoteImagingStudyMixin, RemoteImagingPatientDataMixin, RemoteImagingBaseObject):
	'''	Imaging study: set of sequeneces/series/scans
	'''
	pk_attr = DCMHEADER_STUDY_INSTANCE_UID
	tabulate_output_columns = REMOTE_IMAGING_STUDY_OUTPUT_COLUMNS

	@property
	def description(self):
		return self._objectdata.get('StudyDescription')

	def fetch_series(self, **kwargs):
		'''	Retrieve details of series on the DICOMweb remote associated with the study

			@returns RemoteImagingSeriesCollection: collection of DICOM series models associated with the study.
		'''
		# Create query structure
		query = kwargs.get('query') or {}
		query.update({ self.pk_attr: self.pk })

		# Ensure that the resource type is "Series"
		kwargs.update({ 'resource': IMAGING_SERVER_RESOURCE_SERIES })

		# Retrieve imaging series collection
		return self.dicomweb.remote_query(query, **kwargs)

	@property
	def series_collection(self):
		'''	Series instances associated with the study
		'''
		if getattr(self, '_series', None) is None:
			setattr(self, '_series', self.fetch_series())			

		return self._series


class RemoteImagingStudyCollection(RemoteImagingObjectCollection):
	'''	Remote collection of imaging studies
	'''
	model = RemoteImagingStudy


IMAGING_SERIES_OUTPUT_COLUMNS = OrderedDict((
		('study', 'Parent Study'),
		('pk', 'Series ID'),
		('modality', 'Modality'),
		('series_date', 'Date'),
		('series_time', 'Time'),
		('patientid', 'Study MRN'),
		('patient_name', 'Patient Name'),
		('description', 'Description'),
	))


class RemoteImagingSeries(
		RemoteImagingStudyMixin, RemoteImagingPatientDataMixin, RemoteImagingBaseObject):
	'''	Remote imaging series: set of grouped images
	'''
	pk_attr = DCMHEADER_SERIES_INSTANCE_UID
	tabulate_output_columns = IMAGING_SERIES_OUTPUT_COLUMNS

	@property
	def sequence_name(self):
		return self._objectdata.get(DCMHEADER_SEQUENCE_NAME)

	@property
	def modality(self):
		return self._objectdata.get(DCMHEADER_MODALITY)

	@property
	def description(self):
		return self._objectdata.get(DCMHEADER_SERIES_DESCRIPTION)

	@property
	def study(self):
		return self._objectdata.get(DCMHEADER_STUDY_INSTANCE_UID)

	@property
	def series_number(self):
		return self._objectdata.get(DCMHEADER_SERIES_NUMBER)

	@property
	def series_date(self):
		return self._objectdata.get('PerformedProcedureStepStartDate')

	@property
	def series_time(self):
		return self._objectdata.get('PerformedProcedureStepStartTime')

	@property
	def body_part(self):
		return self._objectdata.get(DCMHEADER_BODY_PART_EXAMINED)


class RemoteImagingSeriesCollection(RemoteImagingObjectCollection):
	''' Remote collection of imaging series
	'''
	model = RemoteImagingSeries


REMOTE_IMAGING_SERVER_RESOURCE_DATAMODEL_COLLECTIONTYPES = OrderedDict((
		(IMAGING_SERVER_RESOURCE_STUDY, RemoteImagingStudyCollection),
		(IMAGING_SERVER_RESOURCE_SERIES, RemoteImagingSeriesCollection),
	))


REMOTE_DICOMWEB_RESOURCE_TYPE = OrderedDict((
		(IMAGING_SERVER_RESOURCE_STUDY, '/studies'),
		(IMAGING_SERVER_RESOURCE_SERIES, '/series'),
	))