import six, logging
from collections import OrderedDict

from client.utils.object import pick, omit

from ..apisettings import IMAGING_SERVER_RESOURCE_PATIENT, IMAGING_SERVER_RESOURCE_STUDY, IMAGING_SERVER_RESOURCE_SERIES, \
	DCMHEADER_STUDY_INSTANCE_UID, DCMHEADER_SERIES_INSTANCE_UID, DCMHEADER_SEQUENCE_NAME, DCMHEADER_MODALITY, \
	DCMHEADER_SERIES_DESCRIPTION, DCMHEADER_SERIES_NUMBER, DCMHEADER_BODY_PART_EXAMINED, \
	DCMHEADER_ACCESSION_NUMBER, DCMHEADER_STUDY_DATE, DCMHEADER_STUDY_TIME, DCMHEADER_REFERRING_PHYSICIAN, \
	DCMHEADER_PATIENT_SEX, DCMHEADER_PATIENT_ID, DCMHEADER_PATIENT_NAME, DICOM_VR_PN, \
	DICOMWEB_TAG_ATTR, DICOMWEB_VALUE_ATTR, DICOMWEB_VALUE_REP, DICOMWEB_VR_PN_ALPHABETIC
from ..remote import SonadorBaseObject, SonadorObjectCollection

from .helpers.conversion import dcmhexcode2tagname

logger = logging.getLogger(__name__)


def dicomweb_tag_name(dcmweb_code, dcmweb_val, 
		name_attr=DICOMWEB_TAG_ATTR, value_attr=DICOMWEB_VALUE_ATTR, cache_dcm_tags=None):
	'''	Translate the provided DICOMweb code to an attribute name.

		@returns human readable attribute (tag) name
	'''
	# Attemptt to retrieve header from the provided DICOMweb value array
	if isinstance(dcmweb_val, dict) and name_attr in dcmweb_val:
		return dcmweb_val[name_attr]

	# Ensure that the DICOMweb code is a tuple, split string into a tuple for lookup
	if not isinstance(dcmweb_code, tuple) and isinstance(dcmweb_code, str) and len(dcmweb_code) == 8:
		dcmweb_code = (dcmweb_code[:4], dcmweb_code[-4:])

	# Invalid DICOMweb attribute code
	if not isinstance(dcmweb_code, tuple):
		return None

	# Check cache DICOM tags for code
	if cache_dcm_tags and cache_dcm_tags.get(dcmweb_code):
		_,tag = cache_dcm_tags.get(dcmweb_code)
		return tag.header

	# Check pydicom dictionary for tag
	return dcmhexcode2tagname(dcmweb_code)


def dicomweb_tag_keys(dicomweb_raw, 
		name_attr=DICOMWEB_TAG_ATTR, value_attr=DICOMWEB_VALUE_ATTR, attrs=None, cache_dcm_tags=None):
	''' Retrieve the DCM key names from the provided dicomweb_raw data dictionary and translate them
		to their tag names.

		@returns iterable of DCM tag names present in the response. Example: ["PatientID", "PatientName", ...]
	'''
	attrs = attrs or []

	# Iterate through DICOMweb JSON object and convert DICOM hexcodes to tag names
	for dk, dv in six.iteritems(dicomweb_raw):
		dcm_attr = dicomweb_tag_name(dk, dv, cache_dcm_tags=cache_dcm_tags, name_attr=name_attr, value_attr=value_attr)
		if dcm_attr:
			attrs.append(dcm_attr)

	return attrs


def dicomweb_code_keys(dicomweb_raw,
		name_attr=DICOMWEB_TAG_ATTR, value_attr=DICOMWEB_VALUE_ATTR, attrs=None, cache_dcm_tags=None):
	'''	Retrieve the DCM code keys from the provided dicomweb_raw data dictionary. The resulting iterable
		can be used for JSON transforms or data conversions.

		@returns iterable of DCM code keys present in the response. Example: ["00100010", "00080020"]
	'''
	attrs = attrs or []

	# Iterate through DICOmweb JSON object and aggregate all DICOM hexcodes present in the response
	for dk, dv in six.iteritems(dicomweb_raw):
		dcm_attr = dicomweb_tag_name(dk, dv, cache_dcm_tags=cache_dcm_tags, name_attr=name_attr, value_attr=value_attr)
		if dcm_attr:
			attrs.append(dk)

	return attrs


def dicomweb_value(dicomweb_val, value_attr=DICOMWEB_VALUE_ATTR, valuerep_attr=DICOMWEB_VALUE_REP):
	''' Retrieve, flatten, format, and convert the DICOMweb value from the provided dictionary.

		@returns formatted value
	'''
	# For DICOMweb encoded values, flatten and transform
	if isinstance(dicomweb_val, dict) and dicomweb_val.get(value_attr):
		_val = dicomweb_val.get(value_attr)
		_vr = dicomweb_val.get(valuerep_attr)

		# Flatten nested values
		if _val and isinstance(_val, (list, tuple)) and len(_val) == 1:
			_val = _val[0]

		# Person Name: flatten to string
		if isinstance(_val, dict) and _val.get(DICOMWEB_VR_PN_ALPHABETIC):
			_val = _val.get(DICOMWEB_VR_PN_ALPHABETIC)

	else:
		_val = dicomweb_val

	return _val


def dicomweb2keyval(dicomweb_raw, 
		odata=None, name_attr=DICOMWEB_TAG_ATTR, value_attr=DICOMWEB_VALUE_ATTR, cache_dcm_tags=None):
	'''	Convert a raw dicomweb data response to a structure mapped to key/value pairs
	'''
	odata = odata or {}

	# Iterate through all DICOMweb attributes and extract 'Name' and 'Value' elements.
	# Re-map in a new dictionary
	for dk, dv in six.iteritems(dicomweb_raw):

		# Retrieve DICOM tag name, add to object data
		tag_name = dicomweb_tag_name(dk, dv, name_attr=name_attr, value_attr=value_attr, cache_dcm_tags=cache_dcm_tags)
		if tag_name:
			odata[tag_name] = dicomweb_value(dv, value_attr=value_attr)

	return odata


def dcmjson2orthanc(dcm_json, model_class, cache_dcm_tags, odata=None):
	'''	Translate the provided DICOM JSON structure to the model structure specified by 
		the Orthanc model class.

		@input dcm_json (JSON object): dictionary of DICOM key/value pairs
		@input model_class (sonador.imaging.orthanc.base.ImagingResource class): Orthanc
			object class to which the provided DCM json should be translated.
	'''
	odata = odata or {}

	if not hasattr(model_class, 'resource_level'):
		raise ValueError('Unable to translate DICOM structure. Model "%s" does not have a "resource_level" property'
			% model_class.__name__)

	if not hasattr(model_class, 'main_dcmtags_attr'):
		raise ValueError('Unable to translate DICOM structure. Model "%s" does not have a "main_dcmtags_attr" property')

	# Add DICOM tags to MainDicomTags
	odata[model_class.main_dcmtags_attr] = pick(dcm_json, 
		[_dcm.header for (_level,_dcm) in cache_dcm_tags.values() if _level == model_class.resource_level])

	# Add PatientMainDicomTags to the object data
	if hasattr(model_class, 'parent_class') \
		and getattr(model_class.parent_class, 'resource_level', None) == IMAGING_SERVER_RESOURCE_PATIENT:
		odata[model_class.parent_class.patient_dcmtags_attr] = pick(dcm_json,
			[_dcm.header for (_level,_dcm) in cache_dcm_tags.values() if _level == IMAGING_SERVER_RESOURCE_PATIENT])
	
	return odata


class DcmWebImagingBaseObject(SonadorBaseObject):
	'''	Data object associated with a response from a DICOMweb API endpoint.
	'''
	def __init__(self, server, dicomweb_raw, *args, object_data=None, tags=None, **kwargs):
		self.dicomweb_raw = dicomweb_raw

		super().__init__(server, dicomweb2keyval(dicomweb_raw, odata=object_data, cache_dcm_tags=tags), *args, **kwargs)


class RemoteImagingBaseObject(DcmWebImagingBaseObject):
	'''	Data object associated with a DICOMCweb remote. Includes a reference to the 
		DICOMweb server from which the object came.
	'''
	def __init__(self, server, dicomweb_raw, *args, **kwargs):
		self.dicomweb = kwargs.pop('dicomweb', None)

		super().__init__(server, dicomweb_raw, *args, **kwargs)


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