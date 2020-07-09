import six, logging
from collections import OrderedDict

from ..apisettings import IMAGING_SERVER_RESOURCE_PATIENT, IMAGING_SERVER_RESOURCE_STUDY, IMAGING_SERVER_RESOURCE_SERIES
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
		return self._objectdata.get('PatientName')

	@property
	def patientid(self):
		return self._objectdata.get('PatientID')

	@property
	def patient_sex(self):
		return self._objectdata.get('PatientSex')


class RemoteImagingStudyMixin(object):
	'''	Mixin class which can be added to remote object models to provide convenience methods
		for study data.
	'''
	@property
	def accession_number(self):
		return self._objectdata.get('AccessionNumber')

	@property
	def study_date(self):
		return self._objectdata.get('StudyDate')

	@property
	def study_time(self):
		return self._objectdata.get('StudyTime')

	@property
	def physician(self):
		return self._objectdata.get('ReferringPhysicianName')





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
	pk_attr = 'StudyInstanceUID'
	tabulate_output_columns = REMOTE_IMAGING_STUDY_OUTPUT_COLUMNS

	@property
	def description(self):
		return self._objectdata.get('StudyDescription')


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
	pk_attr = 'SeriesInstanceUID'
	tabulate_output_columns = IMAGING_SERIES_OUTPUT_COLUMNS

	@property
	def sequence_name(self):
		return self._objectdata.get('SequenceName')

	@property
	def modality(self):
		return self._objectdata.get('Modality')

	@property
	def description(self):
		return self._objectdata.get('SeriesDescription')

	@property
	def study(self):
		return self._objectdata.get('StudyInstanceUID')

	@property
	def series_number(self):
		return self._objectdata.get('SeriesNumber')

	@property
	def series_date(self):
		return self._objectdata.get('PerformedProcedureStepStartDate')

	@property
	def series_time(self):
		return self._objectdata.get('PerformedProcedureStepStartTime')

	@property
	def body_part(self):
		return self._objectdata.get('BodyPartExamined')


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