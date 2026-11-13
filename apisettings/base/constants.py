'''	Base DICOM constants and encoded concepts for Sonador (`sonador.apisettings.base.constants`).
	The SR data classes live in `sonador.apisettings.base.sr`; both are re-exported by `sonador.apisettings.base`.
	
	NOTE: `highdicom` provides both `Code` and `CodedConcept` components. Within
	Sonador the convention is to use `CodedConcept` to refer to the names of categories of values
	and `Code` to refer to the values in a specific context group.

	Sonador scheme versions (newer versions, unless explicitly noted, extend older revisions):
	* 0.1 (2022): basic encoding structures including "Sonador-SEG" and "Sonador-SR" structures
	* 0.2 (2023-0828): spatial SR constructs (2D/3D point concepts) used for creating SR documents 
		that can be used for machine learning applications.
	* 0.3 (2026-1030): Sonador Viewer exchange profile for Series Tags: the extended measurement meta
		attributes (description, text, location) and the display attributes (colour roles) carried by
		a finding (`apisettings.sr.seriestag`). Readers match concepts by code value and designator; a
		concept carrying a scheme version other than the registered one is read as the same concept,
		with a warning.

	Architectural notes:
	* The Sonador client library works with three types of data objects:
		-	DICOM (DCM) meta-key and metadata objects. DCM objects follow the standard and provide interfaces
			for working with data retrieved from Orthanc (primarily JSON based) and parsed 
			using pydicom (pydicom.Dataset).
		-	Structured Reporting (SR) data objects. SR objects provide support for
			local/Python operations and a class structure for working with complex structured
			data. SR objects are incorporated into the `sr` modules 
			of the Sonador client library, provide the integration interface for many of the
			Python modules with which Sonador integrates, and have support for persisting data
			to DICOM-SR files (that can be written to disk via pydicom or uploaded to Sonador/Orthanc).
		-	Remote models and data objects. Remote objects provide the interface for communicating
			with the Sonador and Orthanc server applications and implement logic for data management.
			Remote models often use SR objects for their internal data representation and return them
			as results.
	* `apisettings` defines the core constants and data classes needed for working with local data.
	* `remote` defines the core classes used for communicating with the Sonador web application and Orthanc.
		-	The `servers` module, `servers.SonadorServer`, and `servers.ImagingServer` implement 
			the core logic managing remote data exchange.

	Implementation notes:
	* 	When defining new codes or coded concepts that require a `scheme_designator`, the version should
		be referenced explicitly rather than using the "most recent" version variable.
'''
import abc, re, functools, datetime, copy, logging, numbers, pathlib
from collections import namedtuple, OrderedDict
from importlib import metadata as importlib_metadata
from typing import Sequence, Union

import numpy as np

from pydicom import Dataset as DcmDataset, Sequence as DcmSequence
from pydicom.sr.codedict import codes as dcmcodes
from pydicom.uid import generate_uid

from highdicom.version import __version__ as HIGHDICOM_VERSION
from highdicom.sr.templates import Code, CodedConcept, MeasurementsAndQualitativeEvaluations, \
	DEFAULT_LANGUAGE as DCMSR_DEFAULT_LANGUAGE
from highdicom.sr.value_types import ContainerContentItem, CodeContentItem, TextContentItem, NumContentItem

from client.remote.serialization import DCM_DATETIME_STRFORMAT, DCM_DATETIME_STRFORMAT_ALT1
from client.utils.object import pick, omit, gextend
from client.utils.colors import RGB
from client.utils.microservices import JsonBaseObject, JsonObjectCollection

logger = logging.getLogger(__name__)


SONADOR_MANUFACTURER = 'Sonador'
HIGHDICOM_MANUFACTURER = 'HIGHDICOM'
SONADOR_DEVELOPMENT_TEAM = '%s Development Team' % SONADOR_MANUFACTURER
HIGHDICOM_DEVELOPMENT_TEAM = '%s Development Team' % HIGHDICOM_MANUFACTURER

MASKED_VALUE_SEP = '-...-'

SONADOR_CLIENT = '%s-Client' % SONADOR_MANUFACTURER
SONADOR_CLIENT_DESCRIPTION = 'Client library for working with medical imaging data stored in Sonador/Orthanc.'
SONADOR_SCHEME_VERSION_01 = '0.1'		# 2022
SONADOR_SCHEME_VERSION_02 = '0.2'		# 2023-0828
SONADOR_SCHEME_VERSION_03 = '0.3'		# 2026-1030
SONADOR_SCHEME_VERSION = SONADOR_SCHEME_VERSION_03

# Equipment profile of the programs writing Sonador DICOM-SR documents:
# Manufacturer `Sonador`, model name per program. The Python library reports the installed `sonador`
# distribution version, `dev` when it runs from a source checkout.
SONADOR_IO = '%s-IO' % SONADOR_MANUFACTURER
SONADOR_VIEWER = '%s-Viewer' % SONADOR_MANUFACTURER
SONADOR_DISTRIBUTION = 'sonador'
SONADOR_SOFTWARE_VERSION_DEV = 'dev'


def sonador_software_version(distribution=SONADOR_DISTRIBUTION, default=SONADOR_SOFTWARE_VERSION_DEV):
	'''	Version of the installed `sonador` distribution (importlib.metadata), `dev` when the library is
		not an installed distribution: no distribution metadata, or the imported package is a source
		checkout rather than the files the distribution installed.
	'''
	try:
		dist = importlib_metadata.distribution(distribution)
	except importlib_metadata.PackageNotFoundError:
		return default

	package_dir = pathlib.Path(__file__).resolve().parents[2]
	installed = dist.locate_file(distribution)
	try:
		if pathlib.Path(str(installed)).resolve() != package_dir:
			return default
	except OSError:
		return default

	return dist.version or default

SONADOR_SEG = 'Sonador-SEG'
SONADOR_SEG_DESCRIPTION = '%s implements tools for working with segmentation data' % SONADOR_SEG
DCMSR_SONADOR_SEG = Code(SONADOR_SEG, SONADOR_CLIENT, SONADOR_SEG_DESCRIPTION,
	scheme_version=SONADOR_SCHEME_VERSION_01)

SONADOR_SR = 'Sonador-SR'
SONADOR_SR_DESCRIPTION = '%s implements tools for working with structured reporting data' % SONADOR_SR
DCMSR_SONADOR_SR = Code(SONADOR_SR, SONADOR_CLIENT, SONADOR_SR_DESCRIPTION,
	scheme_version=SONADOR_SCHEME_VERSION_01)

DCM_SR_DCM = 'DCM'
DCM_SR_DCM_DESCRIPTION = 'SR encoded constants provided by the DICOM standard'
DCMSR_SR_DCM = Code(DCM_SR_DCM, 'DCM', DCM_SR_DCM_DESCRIPTION)


# Header cache constants and data classes
DCM_CONTENT_TYPE = 'application/octet-stream'
DCM_DICOM_CONTENT_TYPE = 'application/dicom'
DCM_JSON_MIMETYPE = 'application/json'


# DICOM Metadata key/value data structures
DicomMetaKey = namedtuple('DicomMetaKey', ('resource', 'header', 'uid'))


class DicomMeta:
	'''	Helper data class for working with DICOM metadata

		@input description (str): description of the resource
		@input modality (str): modality
		@input meta (DicomMetaKey, default=None): reference to the DICOM key
			associated with the meta instance.
		@input attrs (dict, default=None): attributes associated with the DICOM instance
	'''
	def __init__(self, description, modality, *args, meta=None, attrs=None, **kwargs):
		self.description = description
		self.modality = modality
		self.meta = meta
		self.attrs = attrs

	def __str__(self):
		return "%s(description='%s', modality='%s')" % (type(self).__name__, self.description, self.modality)

	def __repr__(self):
		return str(self)


# DICOM Header Definition
class DicomHeaderData:
	'''	Helper data class for working with DICOM tag definitions.
	'''
	def __init__(self, header, dcm_hexcode, dcm_int, dtype, private=False, private_creator=None):
		self.header = header
		self.hex = dcm_hexcode
		self.int = dcm_int
		self.dtype = dtype

		# Private tag fields		
		self.private = private
		self.private_creator = private_creator

		if self.private and not self.private_creator:
			raise ValueError('Invalid private tag configuration. Private tags must specify '
				+ 'a private creator value.')

	def __hash__(self):
		return hash((self.header, self.hex, self.int, self.dtype))


# Private header definition
DicomPrivateHeaderData = DicomHeaderData


# DICOM date/time data structures
DicomDatetimePairKey = namedtuple('DicomDatetimePairKey', ('resource', 'date_tag', 'time_tag'))

class DicomDatetimePair:
	'''	Helper data class for working with DICOM date/time pairs: parsing data from string
		to Python representations and combining date and time components to a single
		unified timestamp (datetime.datetime) representation.
	'''
	def __init__(self, date_value, time_value, *args, meta: DicomDatetimePairKey=None, **kwargs):
		self._dvalue = date_value
		self._tvalue = time_value
		self.meta = meta

	@property
	@functools.lru_cache()
	def date_value(self):
		from ...serialization import dcm_str2date
		return dcm_str2date(self._dvalue) if self._dvalue else self._dvalue

	@property
	@functools.lru_cache()
	def time_value(self):
		from ...serialization import dcm_str2time
		return dcm_str2time(self._tvalue) if self._tvalue else self._tvalue

	@property
	@functools.lru_cache()
	def ts(self):
		'''	Datetime created from the provided date and time values.

			@returns datetime.datetime or None (if no date value specified)
		'''
		# Group date/time
		if self.date_value:
			return datetime.datetime.combine(
				self.date_value, self.time_value or datetime.time(0,0,0))


# DICOM Header Definitions

DCMCODE_CODE_VALUE = ('0008', '0100')
DCMHEADER_CODE_VALUE = 'CodeValue'

DCMCODE_CODING_SCHEME_DESIGNATOR = ('0008', '0102')
DCMHEADER_CODING_SCHEME_DESIGNATOR = 'CodingSchemeDesignator'

DCMCODE_CODING_SCHEME_VERSION = ('0008', '0103')
DCMHEADER_CODING_SCHEME_VERSION = 'CodingSchemeVersion'

DCMCODE_CODE_MEANING = ('0008', '0104')
DCMHEADER_CODE_MEANING = 'CodeMeaning'

DCMCODE_CONCEPT_CODE_SEQUENCE = ('0040', 'A043')
DCMHEADER_CONCEPT_CODE_SEQUENCE = 'ConceptNameCodeSequence'

DCMCODE_MAPPING_RESOURCE = ('0008', '0104')
DCMHEADER_MAPPING_RESOURCE = 'MappingResource'

DCMCODE_MAPPING_RESOURCE_UID = ('0008', '0118')
DCMHEADER_MAPPING_RESOURCE_UID = 'MappingResourceUID'

DCMCODE_LONG_CODE_VALUE = ('0008', '0119')
DCMHEADER_LONG_CODE_VALUE = 'LongCodeValue'

DCMCODE_URN_CODE_VALUE = ('0008', '0120')
DCMHEADER_URN_CODE_VALUE = 'URNCodeValue'

DCMCODE_MAPPING_RESOURCE_NAME = ('0008', '0122')
DCMHEADER_MAPPING_RESOURCE_NAME = 'MappingResourceName'

DCMCODE_CONTEXT_GROUP_VERSION = ('0008', '0106')
DCMHEADER_CONTEXT_GROUP_VERSION = 'ContextGroupVersion'

DCMCODE_CONTEXT_UID = ('0008', '0117')
DCMHEADER_CONTEXT_UID = 'ContextUID'

IMAGING_SERVER_LEVEL = 'Level'
IMAGING_SERVER_WILDCARD = '*'
IMAGING_SERVER_LABELS = 'Labels'

IMAGING_SERVER_RESOURCE_PATIENT = 'Patient'
IMAGING_SERVER_RESOURCE_STUDY = 'Study'
IMAGING_SERVER_RESOURCE_SERIES = 'Series'
IMAGING_SERVER_RESOURCE_IMAGE = 'Instance'
IMAGING_SERVER_RESOURCE_REPORT = 'Report'

IMAGING_SERVER_DCM_TAG = 'DcmTag'
IMAGING_SERVER_DCM_TAG_VALUE = 'DcmTagValue'

IMAGING_SERVER_UID_REGEX_STR = r'(.+)?(?P<uid>([0-9a-fA-F]{8}\-?){5})(.+)?'
IMAGING_SERVER_UID_REGEX = re.compile(IMAGING_SERVER_UID_REGEX_STR)
DICOM_UID_REGEX_STR = r'(?P<uid>(\b[0-9]+(\.[0-9]+)+\b))'
DICOM_UID_REGEX = re.compile(DICOM_UID_REGEX_STR)
IMAGING_SERVER_GROUP_UID_REGEX_STR = r'.*?(?P<group>\d+).*?'
IMAGING_SERVER_GROUP_UID_REGEX = re.compile(IMAGING_SERVER_GROUP_UID_REGEX_STR)

IMAGING_SERVER_RESOURCE_SUPPORTED = (
	IMAGING_SERVER_RESOURCE_PATIENT, IMAGING_SERVER_RESOURCE_STUDY, IMAGING_SERVER_RESOURCE_SERIES,
	IMAGING_SERVER_RESOURCE_IMAGE)
IMAGING_SERVER_RESOURCE_LEVEL = {
	IMAGING_SERVER_RESOURCE_PATIENT: 0,
	IMAGING_SERVER_RESOURCE_STUDY: 1,
	IMAGING_SERVER_RESOURCE_SERIES: 2,
	IMAGING_SERVER_RESOURCE_IMAGE: 3,
}
IMAGING_SERVER_AUTHREQUEST_LEVEL_LOOKUP = {
	IMAGING_SERVER_RESOURCE_PATIENT.lower(): IMAGING_SERVER_RESOURCE_PATIENT,
	IMAGING_SERVER_RESOURCE_STUDY.lower(): IMAGING_SERVER_RESOURCE_STUDY,
	IMAGING_SERVER_RESOURCE_SERIES.lower(): IMAGING_SERVER_RESOURCE_SERIES,
}

IMAGING_SERVER_LAST_UPDATE = 'LastUpdate'
IMAGING_SERVER_CREATED = 'Created'
IMAGING_SERVER_MODIFIED = 'Modified'
IMAGING_SERVER_STABLE = 'IsStable'
IMAGING_SERVER_MAINDICOM = 'MainDicomTags'
IMAGING_SERVER_METADATA = 'Metadata'
IMAGING_SERVER_PATIENT_MAINDICOM = 'PatientMainDicomTags'
IMAGING_SERVER_DICOMTAGS_SIGNATURE = 'MainDicomTagsSignature'
IMAGING_SERVER_PARENT_PATIENT = 'ParentPatient'
IMAGING_SERVER_PARENT_STUDY = 'ParentStudy'
IMAGING_SERVER_PARENT_SERIES = 'ParentSeries'
IMAGING_SERVER_REQUESTED_TAGS = 'RequestedTags'
IMAGING_SERVER_INCLUDE_INSTANCES = 'IncludeInstances'

IMAGING_SERVER_RECEPTION_DATE = 'ReceptionDate'
IMAGING_SERVER_SERIES_INDEX = 'IndexInSeries'
IMAGING_SERVER_FILE_SIZE = 'FileSize'
IMAGING_SERVER_FILE_UUID = 'FileUuid'


# DICOM Versions

DCM_VERSION_2008 = '2008'
DCM_VERSION_2017c = '2017c'
DCM_VERSION_2021b = '2021b'


# DICOM Query Constants (Sonador extension)

DCM_QUERY_ALLFIELDS = 'allFields'
DCM_QUERY_NULL = '(null)'
DCM_QUERY_NOT_NULL = '!%s' % DCM_QUERY_NULL


# DICOM Code Enumerated Values

DCM_YES = 'YES'
DCM_NO = 'NO'


# DICOM Header Definitions
DCM_PREAMBLE = b'\0'*128

DCMCODE_SPECIFIC_CHARSET = ('0008', '0005')
DCMHEADER_SPECIFIC_CHARSET = 'SpecificCharacterSet'

DCMCODE_QUERY_RETRIEVE_LEVEL = ('0008', '0052')
DCMHEADER_QUERY_RETRIEVE_LEVEL = 'QueryRetrieveLevel'

DCMCODE_SOP_CLASS_UID = ('0008', '0016')
DCMHEADER_SOP_CLASS_UID = 'SOPClassUID'

DCMCODE_SOP_INSTANCE_UID = ('0008', '0018')
DCMHEADER_SOP_INSTANCE_UID = 'SOPInstanceUID'

DCMHEADER_MEDIA_STORAGE_SOP_CLASS_UID = 'MediaStorageSOPClassUID'

# Secondary capture specifies images that are converted from anon-DICOM format to a modality independent DICOM Format.
# Refer to https://dicom.nema.org/dicom/2013/output/chtml/part03/sect_A.8.html.
DCM_MEDIA_STORAGE_SECONDARY_CAPTURE = '1.2.840.10008.5.1.4.1.1.7'

DCMHEADER_MEDIA_STORAGE_SOP_INSTANCE_UID = 'MediaStorageSOPInstanceUID'

DCMHEADER_IMPLEMENTATION_CLASS_UID = 'ImplementationClassUID'

DCMHEADER_TRANSFER_SYNTAX_UID = 'TransferSyntaxUID'


# DICOM Image Profiles

DCMHEADER_PHOTOMETRIC_INTERPRETATION = 'PhotometricInterpretation'
DCM_PHOTOMETRIC_INTERPRETATION_MONOCHROME = 'MONOCHROME2'
DCM_PHOTOMETRIC_INTERPRETATION_RGB = 'RGB'

DCMHEADER_PIXEL_REPRESENTATION = 'PixelRepresentation'
DCMHEADER_PLANAR_CONFIGURATION = 'PlanarConfiguration'
DCMHEADER_SAMPLES_PER_PIXEL = 'SamplesPerPixel'
DCMHEADER_HIGH_BIT = 'HighBit'
DCMHEADER_BITS_STORED = 'BitsStored'
DCMHEADER_BITS_ALLOCATED = 'BitsAllocated'

DCMHEADER_IMG_PIXELDATA = 'PixelData'
DCMHEADER_IMG_ROWS = 'Rows'
DCMHEADER_IMG_COLS = 'Columns'

# VOI LUT module (PS3.3 C.11.2): display window (center, width) applied to the pixel values after the
# Modality LUT or Rescale Slope/Intercept transformation, so in the output units of that step
DCMCODE_WINDOW_CENTER = ('0028', '1050')
DCMHEADER_WINDOW_CENTER = 'WindowCenter'
DCMCODE_WINDOW_WIDTH = ('0028', '1051')
DCMHEADER_WINDOW_WIDTH = 'WindowWidth'

# DICOM Image Profiles
DCM_IMG_PROFILE_UNSIGNED_INT = { DCMHEADER_PIXEL_REPRESENTATION: 0}
DCM_IMG_PROFILE_SIGNED_INT = { DCMHEADER_PIXEL_REPRESENTATION: 1 }
DCM_IMG_PROFILE_8BIT = { DCMHEADER_HIGH_BIT: 7, DCMHEADER_BITS_STORED: 8, DCMHEADER_BITS_ALLOCATED: 8 }
DCM_IMG_PROFILE_16BIT = { DCMHEADER_HIGH_BIT: 15, DCMHEADER_BITS_STORED: 16, DCMHEADER_BITS_ALLOCATED: 16 }
DCM_IMG_PROFILE_32BIT = { DCMHEADER_HIGH_BIT: 31, DCMHEADER_BITS_STORED: 32, DCMHEADER_BITS_ALLOCATED: 32 }
DCM_IMG_PROFILE_64BIT = { DCMHEADER_HIGH_BIT: 63, DCMHEADER_BITS_STORED: 64, DCMHEADER_BITS_ALLOCATED: 64 }

DCM_SUPPORTED_IMG_DTYPES = {
	'uint8': gextend({}, DCM_IMG_PROFILE_UNSIGNED_INT, DCM_IMG_PROFILE_8BIT),
	'uint16': gextend({}, DCM_IMG_PROFILE_UNSIGNED_INT, DCM_IMG_PROFILE_16BIT),
	'uint32': gextend({}, DCM_IMG_PROFILE_UNSIGNED_INT, DCM_IMG_PROFILE_32BIT),
	'uint64': gextend({}, DCM_IMG_PROFILE_UNSIGNED_INT, DCM_IMG_PROFILE_64BIT),
	'int8': gextend({}, DCM_IMG_PROFILE_SIGNED_INT, DCM_IMG_PROFILE_8BIT),
	'int16': gextend({}, DCM_IMG_PROFILE_SIGNED_INT, DCM_IMG_PROFILE_16BIT),
	'int32': gextend({}, DCM_IMG_PROFILE_SIGNED_INT, DCM_IMG_PROFILE_32BIT),
	'int64': gextend({}, DCM_IMG_PROFILE_SIGNED_INT, DCM_IMG_PROFILE_64BIT),
}


# DICOM Patient Meta

DCMCODE_PATIENT_ID = ('0010', '0020')
DCMHEADER_PATIENT_ID = 'PatientID'

DCMCODE_PATIENT_NAME = ('0010','0010')
DCMHEADER_PATIENT_NAME = 'PatientName'

DCMHEADER_PATIENT_SEX = 'PatientSex'
DCM_PATIENT_SEX_MALE = 'M'
DCM_PATIENT_SEX_FEMALE = 'F'
DCM_PATIENT_SEX_OTHER = 'O'
DCM_PATIENT_SEX_UNKNOWN = 'U'

DCMHEADER_PATIENT_WEIGHT = 'PatientWeight'

DCMCODE_PATIENT_BIRTHDATE = ('0010', '0030')
DCMHEADER_PATIENT_BIRTHDATE = 'PatientBirthDate'

DCMHEADER_PATIENT_COMMENTS = 'PatientComments'

DCMHEADER_PATIENT_OTHERIDS = 'OtherPatientIDs'

DCMHEADER_RESPONSIBLE_PERSON = 'ResponsiblePerson'
DCMHEADER_RESPONSIBLE_PERSON_ROLE = 'ResponsiblePersonRole'


# Encounter/Service Episode

DCMCODE_SERVICE_EPISODE_ID = ('0038', '0060')
DCMHEADER_SERVICE_EPISODE_ID = 'ServiceEpisodeID'


# DICOM Study Meta

DCMCODE_STUDY_INSTANCE_UID = ('0020', '000D')
DCMHEADER_STUDY_INSTANCE_UID = 'StudyInstanceUID'

DCMCODE_STUDY_ID = ('0020', '0010')
DCMHEADER_STUDY_ID = 'StudyID'

DCMCODE_STUDY_DATE = ('0008', '0020')
DCMHEADER_STUDY_DATE = 'StudyDate'

DCMCODE_STUDY_TIME = ('0008', '0030')
DCMHEADER_STUDY_TIME = 'StudyTime'

DCMTS_STUDY = DicomDatetimePairKey(
	IMAGING_SERVER_RESOURCE_STUDY, DCMHEADER_STUDY_DATE, DCMHEADER_STUDY_TIME)

DCMCODE_ACCESSION_NUMBER = ('0008', '0050')
DCMHEADER_ACCESSION_NUMBER = 'AccessionNumber'

DCMCODE_STUDY_DESCRIPTION = ('0008', '1030')
DCMHEADER_STUDY_DESCRIPTION = 'StudyDescription'


# DICOM Series Meta

DCMCODE_SERIES_INSTANCE_UID = ('0020', '000e')
DCMHEADER_SERIES_INSTANCE_UID = 'SeriesInstanceUID'

DCMCODE_SERIES_DATE = ('0008', '0021')
DCMHEADER_SERIES_DATE = 'SeriesDate'

DCMCODE_SERIES_TIME = ('0008', '0031')
DCMHEADER_SERIES_TIME = 'SeriesTime'

DCMTS_SERIES = DicomDatetimePairKey(
	IMAGING_SERVER_RESOURCE_SERIES, DCMHEADER_SERIES_DATE, DCMHEADER_SERIES_TIME)

DCMCODE_SERIES_DESCRIPTION = ('0008', '103e')
DCMHEADER_SERIES_DESCRIPTION = 'SeriesDescription'

DCMCODE_SERIES_DESCRIPTION_CODE_SEQUENCE = ('0008', '103f')
DCMHEADER_SERIES_DESCRIPTION_CODE_SEQUENCE = 'SeriesDescriptionCodeSequence'

# Image Type (PS3.3 C.7.6.1.1.2) is multi-valued: value 1 gives the pixel data characteristics
# (ORIGINAL: acquired by the device; DERIVED: computed from other images), value 2 the patient
# examination characteristics (PRIMARY: created as a direct result of the examination; SECONDARY:
# created afterwards). Writers compose the attribute from the values, for example
# `[DCM_IMAGE_TYPE_DERIVED, DCM_IMAGE_TYPE_SECONDARY]` for a computed secondary capture.
DCMCODE_IMAGE_TYPE = ('0008', '0008')
DCMHEADER_IMAGE_TYPE = 'ImageType'

DCM_IMAGE_TYPE_ORIGINAL = 'ORIGINAL'
DCM_IMAGE_TYPE_DERIVED = 'DERIVED'
DCM_IMAGE_TYPE_PRIMARY = 'PRIMARY'
DCM_IMAGE_TYPE_SECONDARY = 'SECONDARY'
# Value 1 of an Enhanced (multi-frame) image whose frames are ORIGINAL and DERIVED in part (PS3.3
# C.8.16.1.1, Table C.8-127): some of its pixel data is computed, so readers treat it as non-primary
DCM_IMAGE_TYPE_MIXED = 'MIXED'

# Derivation Description (General Image module): how a DERIVED image was computed from its sources
DCMCODE_DERIVATION_DESCRIPTION = ('0008', '2111')
DCMHEADER_DERIVATION_DESCRIPTION = 'DerivationDescription'

# Separator of the values of a multi-valued attribute in the simplified tags of the imaging server
# (Orthanc returns `ImageType` as one backslash-separated string); see `imaging.helpers.imagetype`
DCM_MULTIVALUE_SEPARATOR = '\\'

# What a series classification does with a series whose tags cannot be read (an instance request
# failed after the retries): stop with an error, leave the series out of the result, or keep it. Used
# by the Image Type readers (`imaging.helpers.imagetype`), where an unclassified series stops the
# listing by default so that a derived series is never passed off as primary imaging.
SERIES_UNKNOWN_ERROR = 'error'
SERIES_UNKNOWN_SKIP = 'skip'
SERIES_UNKNOWN_KEEP = 'keep'
SERIES_UNKNOWN_OPTIONS = (SERIES_UNKNOWN_ERROR, SERIES_UNKNOWN_SKIP, SERIES_UNKNOWN_KEEP)

DCMHEADER_INSTITUTION_NAME = 'InstitutionName'
DCMHEADER_INSTITUTION_ADDRESS = 'InstitutionAddress'
DCMHEADER_INSTITUTION_DEPARTMENT = 'InstitutionalDepartmentName'

DCMHEADER_REQUESTING_PHYSICIAN = 'RequestingPhysician'
DCMHEADER_REFERRING_PHYSICIAN = 'ReferringPhysicianName'
DCMHEADER_PHYSICIANS_OF_RECORD = 'PhysiciansOfRecord'

DCMCODE_MANUFACTURER = ('0008', '0070')
DCMHEADER_MANUFACTURER = 'Manufacturer'
DCMCODE_MANUFACTER_MODEL_NAME = ('0008', '1090')
DCMHEADER_MANUFACTER_MODEL_NAME = 'ManufacturerModelName'
DCMHEADER_STATION_NAME = 'StationName'

DCMHEADER_REQUESTED_PROCEDURE_ID = 'RequestedProcedureID'
DCMHEADER_REQUESTED_PROCEDURE_DESCRIPTION = 'RequestedProcedureDescription'
DCMHEADER_REASON_FOR_REQUESTED_PROCEDURE = 'ReasonForTheRequestedProcedure'


# Clinical Trial and Data Headers

DCMHEADER_CLINICAL_TRIAL_SERIES_ID = 'ClinicalTrialSeriesID'
DCMHEADER_CLINICAL_TRIAL_PROTOCOLID = 'ClinicalTrialProtocolID'
DCMHEADER_CLINICAL_TRIAL_PROTOCOL_NAME = 'ClinicalTrialProtocolName'
DCMHEADER_CLINICAL_TRIAL_SUBJECTID = 'ClinicalTrialSubjectID'
DCMHEADER_CLINICAL_TRIAL_SPONSOR = 'ClinicalTrialSponsorName'
DCMHEADER_CLINICAL_TRIAL_COORDINATING_CENTER_NAME = 'ClinicalTrialCoordinatingCenterName'
DCMHEADER_CLINICAL_TRIAL_TIMEPOINT_ID = 'ClinicalTrialTimePointID'
DCMHEADER_CLINICAL_TRIAL_SITE_ID = 'ClinicalTrialSiteID'
DCMHEADER_CLINICAL_TRIAL_SITE_NAME = 'ClinicalTrialSiteName'

DCMCODE_SYNTHETIC_DATA = ('0008', '001C')
DCMHEADER_SYNTHETIC_DATA = 'SyntheticData'


# Coordinate System and Spatial Headers

DCMCODE_REFERENCE_FRAME = ('0020', '0052')
DCMHEADER_REFERENCE_FRAME = 'FrameOfReferenceUID'

DCMCODE_IMAGE_POSITION_PATIENT = ('0020', '0032')
DCMHEADER_IMAGE_POSITION_PATIENT = 'ImagePositionPatient'

DCMCODE_IMAGE_ORIENTATION_PATIENT = ('0020', '0037')
DCMHEADER_IMAGE_ORIENTATION_PATIENT = 'ImageOrientationPatient'

DCMHEADER_PATIENT_POSITION = 'PatientPosition'

# Frame of Reference module (PS3.3 C.7.4.1): part of the imaging target used as a reference
# (annotation only, not a mathematical spatial reference)
DCMCODE_POSITION_REFERENCE_INDICATOR = ('0020', '1040')
DCMHEADER_POSITION_REFERENCE_INDICATOR = 'PositionReferenceIndicator'

DCMHEADER_SLICE_THICKNESS = 'SliceThickness'
DCMHEADER_SLICE_LOCATION = 'SliceLocation'
DCMHEADER_PIXEL_SPACING = 'PixelSpacing'


DCMCODE_REGISTRATION_SEQUENCE = ('0070','0308')
DCMHEADER_REGISTRATION_SEQUENCE = 'RegistrationSequence'

DCMCODE_MATRIX_REGISTRATION_SEQUENCE = ('0070', '0309')
DCMHEADER_MATRIX_REGISTRATION_SEQUENCE = 'MatrixRegistrationSequence'

DCMCODE_MATRIX_REGISTRATION = ('0070','0309')
DCMHEADER_MATRIX_REGISTRATION = 'MatrixSequence'

DCMCODE_MATRIX_TRANSFORMATION_TYPE = ('0070','030C')
DCMHEADER_MATRIX_TRANSFORMATION_TYPE = 'FrameOfReferenceTransformationMatrixType'

# Type of transformation matrix.
# Refer to https://dicom.nema.org/medical/dicom/current/output/chtml/part17/chapter_P.html#chapter_P

# RIGID: Registration involving only translations and rotations. Matrix is constrained to be orthonormal
# and describes six degrees of freedom: three translations and three rorations.
DCM_MATRIX_RIGID = 'RIGID'

# RIGID_SCALE: Registration involving only translations, rotations, and sacling.
# Matrix is constrained to be orthonormal and describes nine degrees of freedom: three translations,
# hree rotations, and three scales. Sometimes used in atlas mapping.
DCM_MATRIX_RIGID_SCALE = 'RIGID_SCALE'

# AFFINE: Registration involving translations, rotations, scaling, and shearing.
# There are no constraints on the element of the frame of regference tranformation other than the
# last row should be [0,0,0,1] to preserve the homogenoues coordinates. Transform decribes
# twelve degrees of freedom. Sometimes used in atlas mapping.
DCM_MATRIX_AFFINE = 'AFFINE'

DCM_MATRIX_TRANSFORM_SUPPORTED = set((DCM_MATRIX_RIGID, DCM_MATRIX_RIGID_SCALE, DCM_MATRIX_AFFINE))

DCMCODE_REFERENCE_FRAME_MATRIX = ('3006','00C6')
DCMHEADER_REFERENCE_FRAME_MATRIX = 'FrameOfReferenceTransformationMatrix'

DCMCODE_REFERENCE_FRAME_TRANFORM_COMMENT = ('3006', '00C8')
DCMHEADER_REFERENCE_FRAME_TRANFORM_COMMENT = 'FrameOfReferenceTransformationComment'

DCMCODE_MATRIX_USED_FIDUCIALS = ('0070','0314')
DCMHEADER_MATRIX_USED_FIDUCIALS = 'UsedFiducialsSequence'


# Modality and Protocol

DCMCODE_MODALITY = ('0008', '0060')
DCMHEADER_MODALITY = 'Modality'

DCMCODE_MODALITIES_IN_STUDY = ('0008', '0061')
DCMHEADER_MODALITIES_IN_STUDY = 'ModalitiesInStudy'

DCMCODE_PROTOCOL_NAME = ('0018', '1030')
DCMHEADER_PROTOCOL_NAME = 'ProtocolName'

DCMHEADER_ENCAPSULATED_DOCUMENT = 'EncapsulatedDocument'

DCMHEADER_ENCAPSULATED_DOCUMENT_MIMETYPE = 'MIMETypeOfEncapsulatedDocument'

DCMCODE_ENCAPSULATED_DOCUMENT_TITLE = ('0042', '0010')
DCMHEADER_ENCAPSULATED_DOCUMENT_TITLE = 'DocumentTitle'

# Instances from which an encapsulated document was derived (Encapsulated Document module, PS3.3 C.24.2)
DCMCODE_SOURCE_INSTANCE_SEQUENCE = ('0042', '0013')
DCMHEADER_SOURCE_INSTANCE_SEQUENCE = 'SourceInstanceSequence'

# Manufacturing 3D Model module (PS3.3 C.35.1): units of distance of the coordinate system of the
# encapsulated model file (Type 1, a single item of CID 7063 "Model Scale Unit"), and the model
# provenance flags
DCMCODE_MEASUREMENT_UNITS_CODE_SEQUENCE = ('0040', '08EA')
DCMHEADER_MEASUREMENT_UNITS_CODE_SEQUENCE = 'MeasurementUnitsCodeSequence'

DCMCODE_MODEL_MODIFICATION = ('0068', '7001')
DCMHEADER_MODEL_MODIFICATION = 'ModelModification'

DCMCODE_MODEL_MIRRORING = ('0068', '7002')
DCMHEADER_MODEL_MIRRORING = 'ModelMirroring'

DCMCODE_MODEL_GROUP_UID = ('0068', '7004')
DCMHEADER_MODEL_GROUP_UID = 'ModelGroupUID'

DCMCODE_BODY_PART_EXAMINED = ('0018', '0015')
DCMHEADER_BODY_PART_EXAMINED = 'BodyPartExamined'

DCMCODE_INSTANCE_NUMBER = ('0020', '0013')
DCMHEADER_INSTANCE_NUMBER = 'InstanceNumber'

DCMHEADER_CONTENT_CREATOR = 'ContentCreatorName'

DCMCODE_CONTENT_DATE = ('0008', '0023')
DCMHEADER_CONTENT_DATE = 'ContentDate'

DCMCODE_CONTENT_TIME = ('0008', '0033')
DCMHEADER_CONTENT_TIME = 'ContentTime'

DCMTS_CONTENT = DicomDatetimePairKey(
	IMAGING_SERVER_RESOURCE_IMAGE, DCMHEADER_CONTENT_DATE, DCMHEADER_CONTENT_TIME)

DCMCODE_CONTENT_DESCRIPTION = ('0070', '0081')
DCMHEADER_CONTENT_DESCRIPTION = 'ContentDescription'

DCMCODE_INSTANCE_CREATION_DATE = ('0008', '0012')
DCMHEADER_INSTANCE_CREATION_DATE = 'InstanceCreationDate'

DCMCODE_INSTANCE_CREATION_TIME = ('0008', '0013')
DCMHEADER_INSTANCE_CREATION_TIME = 'InstanceCreationTime'

DCMCODE_SCANNING_SEQUENCE = ('0018', '0020')
DCMHEADER_SCANNING_SEQUENCE = 'ScanningSequence'

DCMCODE_SEQUENCE_VARIANT = ('0018', '0021')
DCMHEADER_SEQUENCE_VARIANT = 'SequenceVariant'

DCMCODE_SCAN_OPTIONS = ('0018', '0022')
DCMHEADER_SCAN_OPTIONS = 'ScanOptions'

DCMCODE_MR_ACQUISITION_TYPE = ('0018', '0023')
DCMHEADER_MR_ACQUISITION_TYPE = 'MRAcquisitionType'

DCMCODE_SEQUENCE_NAME = ('0018', '0024')
DCMHEADER_SEQUENCE_NAME = 'SequenceName'

DCMCODE_ANGIO_FLAG = ('0018', '0025')
DCMHEADER_ANGIO_FLAG = 'AngioFlag'

DCMCODE_LATERALITY = ('0020', '0060')
DCMHEADER_LATERALITY = 'Laterality'

DCM_LATERALITY_RIGHT = 'R'
DCM_LATERALITY_LEFT = 'L'
LATERALITY_RIGHT_LABEL = 'right'
LATERALITY_LEFT_LABEL = 'left'

PROXIMAL_LABEL = 'proximal'
DISTAL_LABEL = 'distal'

DCMCODE_SERIES_NUMBER = ('0020', '0011')
DCMHEADER_SERIES_NUMBER = 'SeriesNumber'
DCMHEADER_SERIES_TYPE = 'SeriesType'
DCMHEADER_OPERATORS_NAME = 'OperatorsName'

DCMCODE_SOFTWARE_VERSIONS = ('0018', '1020')
DCMHEADER_SOFTWARE_VERSIONS = 'SoftwareVersions'

DCMHEADER_IMAGES_IN_ACQUISITION = 'ImagesInAcquisition'
DCMHEADER_CARDIAC_NUMBER_OF_IMAGES = 'CardiacNumberOfImages'
DCMHEADER_NUMBER_OF_TEMPORAL_POSITIONS = 'NumberOfTemporalPositions'
DCMHEADER_NUMBER_OF_SLICES = 'NumberOfSlices'
DCMHEADER_NUMBER_OF_TIME_SLICES = 'NumberOfTimeSlices'

DCMHEADER_PERFORMED_PROCEDURE_STEP_DESCRIPTION = 'PerformedProcedureStepDescription'

DCMHEADER_SCHEDULED_PROCEDURE_STEP_START_DATE = 'ScheduledProcedureStepStartDate'
DCMHEADER_SCHEDULED_PROCEDURE_STEP_START_TIME = 'ScheduledProcedureStepStartTime'
DCMHEADER_SCHEDULED_PROCEDURE_STEP_END_DATE = 'ScheduledProcedureStepEndDate'
DCMHEADER_SCHEDULED_PROCEDURE_STEP_END_TIME = 'ScheduledProcedureStepEndTime'
DCMHEADER_SCHEDULED_PROCEDURE_STEP_ID = 'ScheduledProcedureStepID'
DCMHEADER_SCHEDULED_PROCEDURE_STEP_DESCRIPTION = 'ScheduledProcedureStepDescription'

DCMHEADER_ACQUISITION_DEVICE_PROCESSING_DESCRIPTION = 'AcquisitionDeviceProcessingDescription'
DCMHEADER_CONTRAST_BOLUS_AGENT = 'ContrastBolusAgent'


DCMCODE_SR_VALUE_TYPE = ('0040', 'A040')
DCMHEADER_SR_VALUE_TYPE = 'ValueType'

DCMSR_VALUE_TYPE_SCOORD3D = 'SCOORD3D'
DCMSR_VALUE_TYPE_SCOORD = 'SCOORD'
DCMSR_VALUE_TYPE_POINTS_SUPPORTED = set((DCMSR_VALUE_TYPE_SCOORD3D, DCMSR_VALUE_TYPE_SCOORD))

DCMSR_GEOMETRIC_PURPOSE = Code(value='130400', meaning='Geometric purpose data', scheme_designator=DCM_SR_DCM)
DCMSR_GEOMETRIC_PURPOSE_CENTER = Code(value='111010', meaning='Center', scheme_designator=DCM_SR_DCM)
DCMSR_GEOMETRIC_PURPOSE_CENTERPOINT = Code(value='128137', meaning='Geometric Centerpoint', scheme_designator=DCM_SR_DCM)
DCMSR_GEOMETRIC_PURPOSE_OUTLINE = Code(value='111041', meaning='Outline', scheme_designator=DCM_SR_DCM)
DCMSR_GEOMETRIC_PURPOSE_CENTERLINE = Code(value='130490', meaning='Centerline', scheme_designator=DCM_SR_DCM)
DCMSR_GEOMETRIC_PURPOSE_SEED_POINT = Code(value='128139', meaning='Seed Point', scheme_designator=DCM_SR_DCM)
DCMSR_GEOMETRIC_SUPPORTED = OrderedDict((
	(DCMSR_GEOMETRIC_PURPOSE_CENTER.value, DCMSR_GEOMETRIC_PURPOSE_CENTER),
	(DCMSR_GEOMETRIC_PURPOSE_CENTERPOINT.value, DCMSR_GEOMETRIC_PURPOSE_CENTERPOINT),
	(DCMSR_GEOMETRIC_PURPOSE_OUTLINE.value, DCMSR_GEOMETRIC_PURPOSE_OUTLINE),
	(DCMSR_GEOMETRIC_PURPOSE_CENTERLINE.value, DCMSR_GEOMETRIC_PURPOSE_CENTERLINE),
	(DCMSR_GEOMETRIC_PURPOSE_SEED_POINT.value, DCMSR_GEOMETRIC_PURPOSE_SEED_POINT),
))
DCMSR_GEOMETRIC_SUPPORTED_MEANING = OrderedDict((
	(DCMSR_GEOMETRIC_PURPOSE_CENTER.meaning, DCMSR_GEOMETRIC_PURPOSE_CENTER),
	(DCMSR_GEOMETRIC_PURPOSE_CENTERPOINT.meaning, DCMSR_GEOMETRIC_PURPOSE_CENTERPOINT),
	(DCMSR_GEOMETRIC_PURPOSE_OUTLINE.meaning, DCMSR_GEOMETRIC_PURPOSE_OUTLINE),
	(DCMSR_GEOMETRIC_PURPOSE_CENTERLINE.meaning, DCMSR_GEOMETRIC_PURPOSE_CENTERLINE),
	(DCMSR_GEOMETRIC_PURPOSE_SEED_POINT.meaning, DCMSR_GEOMETRIC_PURPOSE_SEED_POINT),
))


# pydicom 3 keys the UCUM dictionary by keyword ("Millimeter"); the code value is still "mm"
DCMSR_UNITS_MM = dcmcodes.UCUM.Millimeter
DCMSR_UNITS_MM2 = dcmcodes.UCUM.SquareMillimeter
DCMSR_UNITS_ANGLE_DEGREE = dcmcodes.UCUM.Degree
DCMSR_UNITS_ANGLE_RADIAN = dcmcodes.UCUM.Radian

# Measurement types the tracking identifier of a measurement group routes to
SONADOR_MEASUREMENT_TYPE_SERIES_TAG = 'series-tag'
SONADOR_MEASUREMENT_TYPE_LENGTH = 'length'
SONADOR_MEASUREMENT_TYPE_ANGLE = 'angle'
SONADOR_MEASUREMENT_TYPE_COBB_ANGLE = 'cobb-angle'
SONADOR_MEASUREMENT_TYPE_CIRCLE = 'circle'
SONADOR_MEASUREMENT_TYPE_BIDIRECTIONAL = 'bidirectional'
SONADOR_MEASUREMENT_TYPE_ARROW = 'arrow'			# reserved: routed but not parsed
SONADOR_MEASUREMENT_TYPE_GENERIC = 'generic'		# a NUM group with an unknown tracking identifier

DCMHEADER_SR_CONTENT_SEQUENCE = 'ContentSequence'
DCMHEADER_SR_PERTINENT_OTHER_EVIDENCE_SEQUENCE = 'PertinentOtherEvidenceSequence'
DCMHEADER_SR_CURRENT_REQUESTED_PROCEDURE_EVIDENCE_SEQUENCE = 'CurrentRequestedProcedureEvidenceSequence'

# Sequences in which a DICOM-SR document records its evidence. highdicom >= 0.23 files evidence
# from the report's own study in the Current Requested Procedure Evidence Sequence and evidence
# from other studies in the Pertinent Other Evidence Sequence; reports written with older
# releases hold all evidence in the latter. Readers must consult both.
DCMHEADER_SR_EVIDENCE_SEQUENCES = (
	DCMHEADER_SR_CURRENT_REQUESTED_PROCEDURE_EVIDENCE_SEQUENCE,
	DCMHEADER_SR_PERTINENT_OTHER_EVIDENCE_SEQUENCE,
)
DCMHEADER_SR_REF_SERIES_SEQ = 'ReferencedSeriesSequence'
DCMHEADER_SR_REF_INSTANCE_SEQ = 'ReferencedInstanceSequence'
DCMHEADER_SR_REF_IMAGES_SEQ = 'ReferencedImageSequence'
DCMHEADER_SR_REF_SOP_SEQ = 'ReferencedSOPSequence'
DCMHEADER_SR_DERIVATION_IMAGE_SEQ = 'DerivationImageSequence'
DCMHEADER_SR_SOURCE_IMAGE_SEQ = 'SourceImageSequence'

DCMCODE_SR_REF_REFERENCE_FRAME = ('3006', '0024')
DCMHEADER_SR_REF_REFERENCE_FRAME = 'ReferencedFrameOfReferenceUID'

DCMCODE_SR_SOP_CLASS_UID = ('0008', '1150')
DCMHEADER_SR_SOP_CLASS_UID = 'ReferencedSOPClassUID'

DCMCODE_SR_REF_INSTANCE_UID = ('0008', '1155')
DCMHEADER_SR_REF_INSTANCE_UID = 'ReferencedSOPInstanceUID'

DCMCODE_SR_GRAPHIC_DATA = ('0070', '0022')
DCMHEADER_SR_GRAPHIC_DATA = 'GraphicData'

DCMCODE_SR_GRAPHIC_TYPE = ('0070', '0023')
DCMHEADER_SR_GRAPHIC_TYPE = 'GraphicType'

DCMHEADER_FRAME_OF_REFERENCE_UID = 'FrameOfReferenceUID'

DCMCODE_PRIMARY_ANATOMIC_STRUCTURE_SEQUENCE = ('0008', '2228')
DCMHEADER_PRIMARY_ANATOMIC_STRUCTURE_SEQUENCE = 'PrimaryAnatomicStructureSequence'

DCMHEADER_IMPLANT_NAME = 'ImplantName'
DCMHEADER_IMPLANT_PART_NUMBER = 'ImplantPartNumber'
DCMHEADER_IMPLANT_SIZE = 'ImplantSize'

DCM_PATIENT_POSITION_HFP = 'HFP'
DCM_PATIENT_POSITION_HFS = 'HFS'
DCM_PATIENT_POSITION_HFDR = 'HDFR'
DCM_PATIENT_POSITION_HFDL = 'HFDL'
DCM_PATIENT_POSITION_FFP = 'FFP'
DCM_PATIENT_POSITION_FFS = 'FFS'
DCM_PATIENT_POSITION_FFDR = 'FFDR'
DCM_PATIENT_POSITION_FFDL = 'FFDL'

DCM_PATIENT_POSITION_LABELS = {
	DCM_PATIENT_POSITION_HFP: 'Head First Prone',
	DCM_PATIENT_POSITION_HFS: 'Head First Supine',
	DCM_PATIENT_POSITION_HFDR: 'Head First Decubitus Right',
	DCM_PATIENT_POSITION_HFDL: 'Head First Decubitus Left',
	DCM_PATIENT_POSITION_FFP: 'Feet First Prone',
	DCM_PATIENT_POSITION_FFS: 'Feet First Supine',
	DCM_PATIENT_POSITION_FFDR: 'Feet First Decubitus Right',
	DCM_PATIENT_POSITION_FFDL: 'Feet First Decubitus Left',
}

DCM_DATE_STRFORMAT = '%Y%m%d'
DCM_DATE_STRFORMAT_ALT1 = '%Y-%m%d'
DCM_DATE_STRFORMAT_ALT2 = '%m/%d/%Y'
DCM_TIME_STRFORMAT = '%H%M%S.%f'
DCM_TIME_STRFORMAT_ALT1 = '%H%M%S'


DCM_FILE_EXTENSION = 'dcm'
DCM_FILE_DICOMDIR = 'DICOMDIR'
DCM_EXTENSIONS_DEFAULT = ['*.dcm', '*.DCM', '*.DICOM', '*.dicom', 'IM*']
DCM_EXTENSIONS_ALL_FILES = ['*']

DCM_MODALITY_AR = 'AR'
DCM_MODALITY_ASMT = 'ASMT'
DCM_MODALITY_AU = 'AU'
DCM_MODALITY_BDUS = 'BDUS'
DCM_MODALITY_BI = 'BI'
DCM_MODALITY_BMD = 'BMD'
DCM_MODALITY_CR = 'CR'
DCM_MODALITY_CT = 'CT'
DCM_MODALITY_CR = 'CR'
DCM_MODALITY_CTPROTOCOL = 'CTPROTOCOL'
DCM_MODALITY_DG = 'DG'
DCM_MODALITY_DOC = 'DOC'
DCM_MODALITY_DX = 'DX'
DCM_MODALITY_DR = 'DR'
DCM_MODALITY_ECG = 'ECG'
DCM_MODALITY_EPS = 'EPS'
DCM_MODALITY_ES = 'ES'
DCM_MODALITY_FID = 'FID'
DCM_MODALITY_GM = 'GM'
DCM_MODALITY_HC = 'HC'
DCM_MODALITY_HD = 'HD'
DCM_MODALITY_IO = 'IO'
DCM_MODALITY_IOL = 'IOL'
DCM_MODALITY_IVOCT = 'IVOCT'
DCM_MODALITY_IVUS = 'IVUS'
DCM_MODALITY_KER = 'KER'
DCM_MODALITY_KO = 'KO'
DCM_MODALITY_LEN = 'LEN'
DCM_MODALITY_LS = 'LS'
DCM_MODALITY_MG = 'MG'
DCM_MODALITY_M3D = 'M3D'
DCM_MODALITY_MR = 'MR'
DCM_MODALITY_NM = 'NM'
DCM_MODALITY_OAM = 'OAM'
DCM_MODALITY_OCT = 'OCT'
DCM_MODALITY_OP = 'OP'
DCM_MODALITY_OPM = 'OPM'
DCM_MODALITY_OPT = 'OPT'
DCM_MODALITY_OPTBSV = 'OPTBSV'
DCM_MODALITY_OPTENF = 'OPTENF'
DCM_MODALITY_OPV = 'OPV'
DCM_MODALITY_OSS = 'OSS'
DCM_MODALITY_OT = 'OT'
DCM_MODALITY_PLAN = 'PLAN'
DCM_MODALITY_PR = 'PR'
DCM_MODALITY_PT = 'PT'
DCM_MODALITY_PX = 'PX'
DCM_MODALITY_REG = 'REG'
DCM_MODALITY_RESP = 'RESP'
DCM_MODALITY_RF = 'RF'
DCM_MODALITY_RG = 'RG'
DCM_MODALITY_RTDOSE = 'RTDOSE'
DCM_MODALITY_RTIMAGE = 'RTIMAGE'
DCM_MODALITY_RTINTENT = 'RTINTENT'
DCM_MODALITY_RTPLAN = 'RTPLAN'
DCM_MODALITY_RTRAD = 'RTRAD'
DCM_MODALITY_RTRECORD = 'RTRECORD'
DCM_MODALITY_RTSEGANN = 'RTSEGANN'
DCM_MODALITY_RTSTRUCT = 'RTSTRUCT'
DCM_MODALITY_RWV = 'RWV'
DCM_MODALITY_SEG = 'SEG'
DCM_MODALITY_SM = 'SM'
DCM_MODALITY_SMR = 'SMR'
DCM_MODALITY_SR = 'SR'
DCM_MODALITY_SRF = 'SRF'
DCM_MODALITY_STAIN = 'STAIN'
DCM_MODALITY_TEXTUREMAP = 'TEXTUREMAP'
DCM_MODALITY_TG = 'TG'
DCM_MODALITY_US = 'US'
DCM_MODALITY_VA = 'VA'
DCM_MODALITY_XA = 'XA'
DCM_MODALITY_XC = 'XC'
DCM_MODALITY_XR = 'XR'

DCM_MODALITIES_MRI = [DCM_MODALITY_MR, 'MRI', 'MR\\SD']
DCM_MODALITIES_CT = [DCM_MODALITY_CT, DCM_MODALITY_IVOCT, DCM_MODALITY_CTPROTOCOL]
DCM_MODALITIES_XRAY = [
	DCM_MODALITY_CR, DCM_MODALITY_DX, DCM_MODALITY_DR, DCM_MODALITY_OT, DCM_MODALITY_PX, DCM_MODALITY_XR]

DCM_MODALITIES = (
	DCM_MODALITY_AR, DCM_MODALITY_ASMT, DCM_MODALITY_AU,
	DCM_MODALITY_BDUS, DCM_MODALITY_BI, DCM_MODALITY_BMD, 
	DCM_MODALITY_CR, DCM_MODALITY_CT, DCM_MODALITY_CTPROTOCOL, 
	DCM_MODALITY_DG, DCM_MODALITY_DOC, DCM_MODALITY_DX, 
	DCM_MODALITY_ECG, DCM_MODALITY_EPS, DCM_MODALITY_ES, 
	DCM_MODALITY_FID, 
	DCM_MODALITY_GM, 
	DCM_MODALITY_HC, DCM_MODALITY_HD, 
	DCM_MODALITY_IO, DCM_MODALITY_IOL, DCM_MODALITY_IVOCT, DCM_MODALITY_IVUS, 
	DCM_MODALITY_KER, 
	DCM_MODALITY_LEN, DCM_MODALITY_LS, 
	DCM_MODALITY_MG, DCM_MODALITY_M3D, DCM_MODALITY_MR, 
	DCM_MODALITY_NM, 
	DCM_MODALITY_OAM, DCM_MODALITY_OCT, DCM_MODALITY_OP, DCM_MODALITY_OPM, DCM_MODALITY_OPT, 
		DCM_MODALITY_OPTBSV, DCM_MODALITY_OPTENF, DCM_MODALITY_OPV, DCM_MODALITY_OSS, DCM_MODALITY_OT, 
	DCM_MODALITY_PLAN, DCM_MODALITY_PR, DCM_MODALITY_PT, DCM_MODALITY_PX, 
	DCM_MODALITY_REG, DCM_MODALITY_RESP, DCM_MODALITY_RF, DCM_MODALITY_RG, DCM_MODALITY_RTDOSE, DCM_MODALITY_RTIMAGE, 
		DCM_MODALITY_RTINTENT, DCM_MODALITY_RTPLAN, DCM_MODALITY_RTRAD, DCM_MODALITY_RTRECORD, DCM_MODALITY_RTSEGANN, 
		DCM_MODALITY_RTSTRUCT, DCM_MODALITY_RWV,
	DCM_MODALITY_SEG, DCM_MODALITY_SM, DCM_MODALITY_SMR, DCM_MODALITY_SR, DCM_MODALITY_SRF, DCM_MODALITY_STAIN, 
	DCM_MODALITY_TEXTUREMAP, DCM_MODALITY_TG,
	DCM_MODALITY_US,
	DCM_MODALITY_VA,
	DCM_MODALITY_XA, DCM_MODALITY_XC
)

DCM_ORIGINAL_ATTRIBUTES_SEQUENCE = 'OriginalAttributesSequence'
DCM_MODIFIED_ATTRIBUTES_SEQUENCE = 'ModifiedAttributesSequence'
DCM_ATTRIBUTE_MOD_DATETIME = 'AttributeModificationDateTime'
DCM_MODIFYING_SYSTEM = 'ModifyingSystem'
DCM_SOURCE_PREVIOUS_VALUES = 'SourceOfPreviousValues'
DCM_MODIFICATION_REASON = 'ReasonForTheAttributeModification'

DCM_MODIFY_CODE_COERCE = 'COERCE'
DCM_MODIFY_CODE_CORRECT = 'CORRECT'


# Environment variable names
SONADOR_ACCESS_ID = 'SONADOR_ACCESS_ID'
SONADOR_SECRET_KEY = 'SONADOR_SECRET_KEY'
SONADOR_URL = 'SONADOR_URL'
SONADOR_APITOKEN = 'SONADOR_APITOKEN'
SONADOR_INTERNAL_DNS = 'SONADOR_INTERNAL_DNS'
SONADOR_VERIFY_SSL = 'SONADOR_VERIFY_SSL'
SONADOR_IMAGING_SERVER = 'SONADOR_IMAGING_SERVER'
SONADOR_SERVICE_CLIENT_ID = 'SONADOR_SERVICE_CLIENT_ID'


# Functional testing globals
from client.utils.logs import LOGGING_LEVELS

TESTING_VERBOSITY = {
	'debug': 3,
	'info': 2,
	'warning': 1,
	'error': 0,
	'critical': 0,
}



ORTHANC_JOB_STATUS_SUCCESS = 'Success'
ORTHANC_JOB_STATUS_FAILED = 'Failed'


ImageSpacing = namedtuple('ImageSpacing', ('x', 'y', 'thickness'))
ImageOrientation = namedtuple('ImageOrientation', ('row', 'col'))
ImageStackShape = namedtuple('ImageStackShape', ('slices', 'rows', 'cols'))



# DICOM Color Representations
RGBColor = RGB
LABColor = namedtuple('LABColor', ('L', 'a', 'b'))
XYZColor = namedtuple('XYZColor', ('x', 'y', 'z'))



# DICOM VR
ValueRepresentationData = namedtuple('ValueRepresentationData', ('code', 'name', 'description'))

# DICOM Value Representation Codes.
# Refer to https://dicom.nema.org/medical/dicom/current/output/chtml/part05/sect_6.2.html

DICOM_VR_AE = 'AE'
DICOM_VR_AS = 'AS'
DICOM_VR_AT = 'AT'
DICOM_VR_CS = 'CS'
DICOM_VR_DA = 'DA'
DICOM_VR_DS = 'DS'
DICOM_VR_DT = 'DT'
DICOM_VR_FL = 'FL'
DICOM_VR_FD = 'FD'
DICOM_VR_IS = 'IS'
DICOM_VR_LO = 'LO'
DICOM_VR_LT = 'LT'
DICOM_VR_OB = 'OB'
DICOM_VR_OD = 'OD'
DICOM_VR_OF = 'OF'
DICOM_VR_OL = 'OL'
DICOM_VR_OV = 'OV'
DICOM_VR_OW = 'OW'
DICOM_VR_PN = 'PN'
DICOM_VR_SH = 'SH'
DICOM_VR_SL = 'SL'
DICOM_VR_SQ = 'SQ'
DICOM_VR_SS = 'SS'
DICOM_VR_ST = 'ST'
DICOM_VR_SV = 'SV'
DICOM_VR_TM = 'TM'
DICOM_VR_UC = 'UC'
DICOM_VR_UI = 'UI'
DICOM_VR_UL = 'UL'
DICOM_VR_UN = 'UN'
DICOM_VR_UR = 'UR'
DICOM_VR_US = 'US'
DICOM_VR_UT = 'UT'
DICOM_VR_UV = 'UV'


DICOM_VR_DESCRIPTION = OrderedDict((
	(DICOM_VR_AE, ValueRepresentationData(
		DICOM_VR_AE, 'Application Entity', 'A string of characters that identifies an Application entity.')),
	(DICOM_VR_AS, ValueRepresentationData(
		DICOM_VR_AS, 'Age String', '''A string of characters with one of the following formats -- nnnD, nnnW, nnnM, nnnY; 
			where nnn shall contain the number of days for D, weeks for W, months for M, or years for Y.
			Example: "018M" would represent an age of 18 months.'''.replace('\n', '').replace('\t', ''))),
	(DICOM_VR_AT, ValueRepresentationData(
		DICOM_VR_AT, 'Attribute Tag', '''Ordered pair of 16-bit unsigned integers that is the Value of a Data Element Tag. Example: 
			A Data Element Tag of (0018,00FF) would be encoded as a series of 4 bytes in a Little-Endian Transfer 
			Syntax as 18H,00H,FFH,00H.'''.replace('\n', '').replace('\t', ''))),
	(DICOM_VR_CS, ValueRepresentationData(
		DICOM_VR_CS, 'Code String', '''A string of characters identifying a controlled concept. 
			Leading or trailing spaces (20H) are not significant. Alternatively, in the context of a Query with Empty 
			Value Matching (see PS3.4), a string of two QUOTATION MARK characters, representing 
			an empty key Value.'''.replace('\n', '').replace('\t', ''))),
	(DICOM_VR_DA, ValueRepresentationData(
		DICOM_VR_DA, 'Date', '''A string of characters of the format YYYYMMDD; where YYYY shall contain year, 
			MM shall contain the month, and DD shall contain the day, interpreted as a date of the Gregorian 
			calendar system. Example: "19930822" would represent August 22, 1993.'''.replace('\n', '').replace('\t', ''))),
	(DICOM_VR_DS, ValueRepresentationData(
		DICOM_VR_DS, 'Decimal String', '''A string of characters representing either a fixed point number or a floating point number. 
			A fixed point number shall contain only the characters 0-9 with an optional leading "+" or "-" and an optional "." 
			to mark the decimal point. A floating point number shall be conveyed as defined in ANSI X3.9, with an "E" or "e" 
			to indicate the start of the exponent. Decimal Strings may be padded with leading or trailing spaces. 
			Embedded spaces are not allowed.'''.replace('\n', '').replace('\t', ''))),
	(DICOM_VR_DT, ValueRepresentationData(
		DICOM_VR_DT, 'Date Time', '''A concatenated date-time character string in the format: YYYYMMDDHHMMSS.FFFFFF&ZZXX.
			The components of this string, from left to right, are YYYY = Year, MM = Month, DD = Day, HH = Hour (range "00" - "23"), 
			MM = Minute (range "00" - "59"), SS = Second (range "00" - "60").'''.replace('\n', '').replace('\t', ''))),
	(DICOM_VR_FL, ValueRepresentationData(
		DICOM_VR_FL, 'Floating Single Point', '''Single precision binary floating point number represented in IEEE 
			754:1985 32-bit Floating Point Number Format.'''.replace('\n', '').replace('\t', ''))),
	(DICOM_VR_FD, ValueRepresentationData(
		DICOM_VR_FD, 'Floating Point Double', '''Single precision binary floating point number represented in IEEE 
			754:1985 32-bit Floating Point Number Format.'''.replace('\n', '').replace('\t', ''))),
	(DICOM_VR_IS, ValueRepresentationData(
		DICOM_VR_IS, 'Integer String', '''A string of characters representing an Integer in base-10 (decimal), shall contain 
			only the characters 0 - 9, with an optional leading "+" or "-". It may be padded with leading and/or 
			trailing spaces. Embedded spaces are not allowed.'''.replace('\n', '').replace('\t', ''))),
	(DICOM_VR_LO, ValueRepresentationData(
		DICOM_VR_LO, 'Long String', '''A string of characters representing an Integer in base-10 (decimal), shall contain 
			only the characters 0 - 9, with an optional leading "+" or "-". It may be padded with leading and/or 
			trailing spaces. Embedded spaces are not allowed.'''.replace('\n', '').replace('\t', ''))),
	(DICOM_VR_LT, ValueRepresentationData(
		DICOM_VR_LT, 'Long Text', '''A character string that may contain one or more paragraphs. It may contain the Graphic 
			Character set and the Control Characters, CR, LF, FF, and ESC. It may be padded with trailing spaces, 
			which may be ignored, but leading spaces are considered to be significant. Data Elements with this VR shall 
			not be multi-valued and therefore character code 5CH (the BACKSLASH "\\" in ISO-IR 6) 
			may be used.'''.replace('\n', '').replace('\t', ''))),
	(DICOM_VR_OB, ValueRepresentationData(
		DICOM_VR_OB, 'Other Byte', '''An octet-stream where the encoding of the contents is specified by the 
			negotiated Transfer Syntax. OB is a VR that is insensitive to byte ordering (see Section 7.3). 
			The octet-stream shall be padded with a single trailing NULL byte value (00H) when necessary 
			to achieve even length.'''.replace('\n', '').replace('\t', ''))),
	(DICOM_VR_OD, ValueRepresentationData(
		DICOM_VR_OD, 'Other Double', '''A stream of 64-bit IEEE 754:1985 floating point words. OD is a VR that 
			requires byte swapping within each 64-bit word when changing byte ordering 
			(see Section 7.3).'''.replace('\n', '').replace('\t', ''))),
	(DICOM_VR_OF, ValueRepresentationData(
		DICOM_VR_OF, 'Other Float', '''A stream of 32-bit IEEE 754:1985 floating point words. OF is a VR that 
			requires byte swapping within each 32-bit word when changing byte ordering 
			(see Section 7.3).'''.replace('\n', '').replace('\t', ''))),
	(DICOM_VR_OL, ValueRepresentationData(
		DICOM_VR_OL, 'Other Long', '''A stream of 32-bit words where the encoding of the contents is specified 
			by the negotiated Transfer Syntax. OL is a VR that requires byte swapping within 
			each word when changing byte ordering (see Section 7.3).'''.replace('\n', '').replace('\t', ''))),
	(DICOM_VR_OV, ValueRepresentationData(
		DICOM_VR_OV, 'Other 64-bit Very Long', '''A stream of 64-bit words where the encoding of the contents is specified 
			by the negotiated Transfer Syntax. OV is a VR that requires byte 
			swapping within each word when changing byte ordering (see Section 7.3).'''.replace('\n', '').replace('\t', ''))),
	(DICOM_VR_OW, ValueRepresentationData(
		DICOM_VR_OW, 'Other Word', '''A stream of 16-bit words where the encoding of the contents is specified by the negotiated 
			Transfer Syntax. OW is a VR that requires byte swapping within each word when changing 
			byte ordering (see Section 7.3).'''.replace('\n', '').replace('\t', ''))),
	(DICOM_VR_PN, ValueRepresentationData(
		DICOM_VR_PN, 'Person Name', '''A character string encoded using a 5 component convention. The character code 
			5CH (the BACKSLASH "\\" in ISO-IR 6) shall not be present, as it is used as the delimiter between Values in multi-valued Data 
			Elements. The string may be padded with trailing spaces. For human use, the five components in their order of occurrence are: 
			family name complex, given name complex, middle name, name prefix, name suffix.'''.replace('\n', '').replace('\t', ''))),
	(DICOM_VR_SH, ValueRepresentationData(
		DICOM_VR_SH, 'Short String', '''A character string that may be padded with leading and/or trailing spaces. The character code 05CH 
			(the BACKSLASH "\\" in ISO-IR 6) shall not be present, as it is used as the delimiter between Values for multi-valued Data Elements. 
			The string shall not have Control Characters except ESC.'''.replace('\n', '').replace('\t', ''))),
	(DICOM_VR_SL, ValueRepresentationData(
		DICOM_VR_SL, 'Signed Long', '''Signed binary integer 32 bits long in 2's complement form. Represents an integer, n, in the range:
			-2**31 <= n <= 2**31 - 1.'''.replace('\n', '').replace('\t', ''))),
	(DICOM_VR_SQ, ValueRepresentationData(
		DICOM_VR_SQ, 'Sequence of Items', '''Value is a Sequence of zero or more Items, as defined in Section 7.5.'''.replace('\n', '').replace('\t', ''))),
	(DICOM_VR_SS, ValueRepresentationData(
		DICOM_VR_SS, 'Signed Short', '''Signed binary integer 16 bits long in 2's complement form. Represents an integer 
			n in the range: -2**15 <= n <= 2**15 - 1.'''.replace('\n', '').replace('\t', ''))),
	(DICOM_VR_ST, ValueRepresentationData(
		DICOM_VR_ST, 'Short Text', '''A character string that may contain one or more paragraphs. It may contain the Graphic Character 
			set and the Control Characters, CR, LF, FF, and ESC. It may be padded with trailing spaces, which may be ignored, but 
			leading spaces are considered to be significant. Data Elements with this VR shall not be multi-valued and therefore character 
			code 5CH (the BACKSLASH "\\" in ISO-IR 6) may be used.'''.replace('\n', '').replace('\t', ''))),
	(DICOM_VR_SV, ValueRepresentationData(
		DICOM_VR_SV, 'Signed 64-bit Very Long', '''Signed binary integer 64 bits long. Represents an integer n in the range: 
			-2**63 <= n <= 2**63 - 1.'''.replace('\n', '').replace('\t', ''))),
	(DICOM_VR_TM, ValueRepresentationData(
		DICOM_VR_TM, 'Time', '''A string of characters of the format HHMMSS.FFFFFF; where HH contains hours (range "00" - "23"), MM 
			contains minutes (range "00" - "59"), SS contains seconds (range "00" - "60"), and FFFFFF contains a fractional part of 
			a second as small as 1 millionth of a second (range "000000" - "999999"). A 24-hour clock is used. 
			Midnight shall be represented by only "0000" since "2400" would violate the hour range. The string may be padded with 
			trailing spaces. Leading and embedded spaces are not allowed.'''.replace('\n', '').replace('\t', ''))),
	(DICOM_VR_UC, ValueRepresentationData(
		DICOM_VR_UC, 'Unlimited Characters', '''A character string that may be of unlimited length that may be padded with trailing spaces. 
			The character code 5CH (the BACKSLASH "\\" in ISO-IR 6) shall not be present, as it is used as the delimiter between Values 
			in multi-valued Data Elements. The string shall not have Control Characters except for ESC.'''.replace('\n', '').replace('\t', ''))),
	(DICOM_VR_UI, ValueRepresentationData(
		DICOM_VR_UI, 'Unique Identifier (UID)', '''A character string containing a UID that is used to uniquely identify a wide variety 
			of items. The UID is a series of numeric components separated by the period "." character. If a Value Field containing 
			one or more UIDs is an odd number of bytes in length, the Value Field shall be padded with a single trailing NULL (00H) 
			character to ensure that the Value Field is an even number of bytes in length. See Section 9 and Annex B for a 
			complete specification and examples.'''.replace('\n', '').replace('\t', ''))),
	(DICOM_VR_UL, ValueRepresentationData(
		DICOM_VR_UL, 'Unsigned Long', '''Unsigned binary integer 32 bits long. Represents an integer n in the \
			range: 0 <= n < 2**32.'''.replace('\n', '').replace('\t', ''))),
	(DICOM_VR_UN, ValueRepresentationData(
		DICOM_VR_UN, 'Unkown', '''An octet-stream where the encoding of the contents is unknown (see Section 6.2.2).''')),
	(DICOM_VR_UR, ValueRepresentationData(
		DICOM_VR_UR, 'Universal Resource Identifier or Universal Resource Locator (URI/URL)', '''A string of characters that identifies a URI or a 
			URL as defined in [RFC3986]. Leading spaces are not allowed. Trailing spaces shall be ignored. Data Elements with this 
			VR shall not be multi-valued.'''.replace('\n', '').replace('\t', ''))),
	(DICOM_VR_US, ValueRepresentationData(
		DICOM_VR_US, 'Unsigned Short', '''Unsigned binary integer 16 bits long. Represents integer n in the range: 0 <= n < 2**16.'''.replace('\n', '').replace('\t', ''))),
	(DICOM_VR_UT, ValueRepresentationData(
		DICOM_VR_UT, 'Unlimited Text', '''A character string that may contain one or more paragraphs. It may contain the Graphic Character 
			set and the Control Characters, CR, LF, FF, and ESC. It may be padded with trailing spaces, which may be ignored, but leading 
			paces are considered to be significant. Data Elements with this VR shall not be multi-valued and therefore character code 
			5CH (the BACKSLASH "\\" in ISO-IR 6) may be used.'''.replace('\n', '').replace('\t', ''))),
	(DICOM_VR_UV, ValueRepresentationData(
		DICOM_VR_UV, 'Unsigned 64-bit Very Long', 
			'''Unsigned binary integer 64 bits long. Represents an integer n in the range: 0 <= n < 2**64.'''.replace('\n', '').replace('\t', ''))),
))



IMG_FORMAT_JPEG = 'jpeg'
IMG_FORMAT_TIFF = 'tiff'
IMG_FORMAT_PNG = 'png'


# DICOMweb
DICOMWEB_TAG_ATTR = 'Name'
DICOMWEB_VALUE_ATTR = 'Value'
DICOMWEB_VALUE_REP = 'vr'
DICOMWEB_VR_PN_ALPHABETIC = 'Alphabetic'
