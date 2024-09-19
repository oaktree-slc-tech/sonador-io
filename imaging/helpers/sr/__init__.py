'''	Classes and methods for creating and parsing DICOM-SR data to Sonador resource types.
'''
import os, functools, abc, json, datetime, itertools
from typing import Optional, Sequence, Union

from pydicom.sequence import Sequence as DcmSequence
from pydicom.dataset import Dataset as DcmDataset
from pydicom.uid import generate_uid
from pydicom.sr.codedict import codes as dcmcodes

from highdicom.sr import Comprehensive3DSR, MeasurementReport as SRMeasurementReport
from highdicom.sr.templates import TrackingIdentifier

from client.errors import ConfigurationError
from client.utils.object import pick, omit

from ....apisettings.base import ImageTransformMatrix, ImageTransformMatrixCollection, \
	ImageCoord, ImageCoordCollection, Finding, FindingCollection, Measurement, MeasurementCollection, \
	ReportGroup, VolumePointCollectionGroup, ReportMetaGroup, ReportGroupCollection, SONADOR_SR, DCMTS_SERIES, \
	DCMHEADER_SERIES_DESCRIPTION, DCMHEADER_REGISTRATION_SEQUENCE
from ....apisettings.sr import dcm_encode_reference_frame_transform_matrix, srencode_observation_context,\
	DCMSR_SONADOR_DATA_REPORT
from ....serialization import ISO8601_DATETIME_FORMAT, DATETIME_FORMAT3, sonador_encode2str
from ...orthanc import ImagingSeries
from ...orthanc.base import ImagingResourceBaseCollection
from ...orthanc.sr import DcmSRSeries

from .encoding import SonadorSRMeasurementReport


class SonadorBaseSR(metaclass=abc.ABCMeta):
	'''	Sonador SR base class that can be used to create and parse DICOM-SR data.
	'''
	dcmsr_report_template = None
	dcmsr_document_template = None
	dcmsr_document_manufacturer = SONADOR_SR
	report_procedure = None

	def __init__(self, sonador_ref_series=None, sonador_series=None, dcm_evidence=None, 
			observation_uid=None, report_procedure=None, **kwargs):
		'''	Initialize the SR model

			@input sonador_ref_series (sonador.imaging.orthanc.ImagingSeries or iterable of
				ImagingSeriesCoreResource objects): imaging series associated with the DICOM-SR
				report. IMPORTANT: for existing DICOM-SR resources (where sonador_series is provided)
				the reference series will be populated dynamically. For new series instances,
				the reference series (or multiple reference series) will be used in the creation
				of the DICOM-SR report.
			@input sonador_series (sonador.imaging.orthanc.DcmSRSeries, default=None):
				DcmSRSeries associated with the report content. When provided in the
				constructor, the SR model class can be used to retrieve and parse SR data
				stored in Sonador/Orthanc to local SR data object. IMPORTANT: For new report 
				instances (yet to be generated) this argument should not be populated.

			@input dcmsr_report_template (highdicom.sr.Template): template instance to be used
				for rendering the report section of the document.
			@input dcmsr_document_template (highdicom.sr._SR subclass): template
				to be used for rending the DICOM-SR document container.

			@input observation_context_uid (str, default=new UID): UID to be used
				for identifying the document.
			@input observation_context_device_name (highdicom.CodedConcept, default=None): concept
				used to encode the name/type of device used to encode the data in the report
			@input observation_context_manufacturer (str, default=None): name of the manufacturer which 
				should be encoded within the device section of the SR document
			@input observation_context_model (str, default=None): name of the device/program used
				to record the data for the SR document
			@input observation_context_serial_number (str, default=None): device serial number
				used to record the deata for the SR document
			@input observation_context_device_role (highdicom.CodedConcept, default=None): concept used 
				to encode 
			@input observation_context_device_location (highdicom.CodedConcept, default=None): concept
				used to encode the physical location of the device
		'''
		self.series = sonador_series
		self.ref_series = sonador_ref_series
		self._evidence = dcm_evidence
		
		# DICOM-SR templates (report and document)
		self.dcmsr_report_template = kwargs.get('dcmsr_report_template', self.dcmsr_report_template)
		self.dcmsr_document_manufacturer = kwargs.get('dcmsr_document_manufacturer', self.dcmsr_document_manufacturer)
		self.dcmsr_document_template = kwargs.get('dcmsr_document_template', self.dcmsr_document_template)
		self.report_procedure = report_procedure or self.report_procedure
		
		# Observation context UID
		self._observation_context = kwargs.get('observeration_context')
		self.observation_context_uid = observation_uid
		self.observation_context_device_name = kwargs.get('observation_context_device_name') \
			or getattr(self, 'observation_context_device_name', None)
		self.observation_context_manufacturer = kwargs.get('observation_context_manufacturer') \
			or getattr(self, 'observation_context_manufacturer', None)
		self.observation_context_model = kwargs.get('observation_context_model') \
			or getattr(self, 'observation_context_model', None)
		self.observation_context_serial_number = kwargs.get('observation_context_serial_number') \
			or getattr(self, 'observation_context_serial_number', None)
		self.observation_context_device_role = kwargs.get('observation_context_device_role') \
			or getattr(self, 'observation_context_device_role', None)
		self.observation_context_device_location = kwargs.get('observation_context_device_location') \
			or getattr(self, 'observation_context_device_location', None)

		if not self.dcmsr_document_template:
			raise ConfigurationError(
				'Unable to initialize Sonador DICOM-SR model %s. No report template specified.' % type(self))
		if not self.dcmsr_document_template:
			raise ConfigurationError(
				'Unable to initialize Sonador DICOM-SR model %s. No document template specified.' % type(self))
		if not self.report_procedure:
			raise ConfigurationError(
				'Unable to initialize Sonador DICOM-SR model %s. No "reort_procedure" code or sequence provided.' % type(self))

	@abc.abstractmethod
	def create_sr(self):
		'''	Create an SR document from the data associated with the model using the document template

			@returns highdicom.sr instance
		'''

	@abc.abstractmethod
	def create_report_sr(self):
		'''	Create the SR report from the data associated with the model using the report template
		'''

	def dcmsr_series_headers(self, srdoc, ts:datetime.datetime, description=None, **kwargs):
		'''	Add identifying headers to the SR document: series date/time and description
		'''
		setattr(srdoc, DCMTS_SERIES.date_tag, ts.date())
		setattr(srdoc, DCMTS_SERIES.time_tag, ts.time())

		if description:
			setattr(srdoc, DCMHEADER_SERIES_DESCRIPTION, description)

		return srdoc

	@property
	@abc.abstractmethod
	def evidence(self):
		'''	DICOM dataset to be evidenced in the intance.
		'''
	@property
	@abc.abstractmethod
	def sr(self):
		'''	SR report instance. For existing Sonador series, pulls the DICOM-SR instance from Sonador/Orthanc.
			For new reports, creates a cached copy of the report by calling create_sr method for the class.
		'''
	
	def observation_context(self, **kwargs):
		'''	Create DICOM-SR observation context
		'''
		_kwargs = {
			'device_name': kwargs.get('observation_context_device_name') or self.observation_context_device_name,
			'manufacturer_name': kwargs.get('observation_context_manufacturer') or self.observation_context_manufacturer,
			'model_name': kwargs.get('observation_context_model') or self.observation_context_model,
			'serial_number': kwargs.get('observation_context_serial_number') or self.observation_context_serial_number,
			'role_in_procedure': kwargs.get('observation_context_device_role') or self.observation_context_device_role,
		}
		_kwargs.update(omit(kwargs, [k for k in kwargs.keys() if 'observation_context' in k]))

		return srencode_observation_context(context_uuid=self.observation_context_uid, **_kwargs)

	@property
	def series(self):
		return self._series

	@series.setter
	def series(self, val):
		if (not val is None) \
			or (val and not isinstance(val, DcmSRSeries)):
			raise TypeError('Sonador series reference must be a DcmSRSeries instance or None')

		self._series = val

	@property
	def ref_series(self):
		'''	Return the reference series associated with the DICOM-SR report
		'''
		# Dynamically populate reference series
		if self._ref_series is None and self.series:
			self.ref_series = self.series.imaging_series_collection

		return self._ref_series

	@ref_series.setter
	def ref_series(self, val):
		if val and not isinstance(val, (tuple, list, ImagingSeries)):
			raise TypeError('Invalid DICOM-SR reference series. Must be None, tuple, list, or ImagingSeries instance.')

		# Unpack reference series tuple/list
		if isinstance(val, (tuple, list)):

			# Set reference series according to what is linked against it.
			# * None for no links.
			# * ImagingSeries intance for one link.
			# * Iterable of ImagingSeries for more than one link.
			if len(val) == 0: self._ref_series = None
			elif len(val) == 1: self._ref_series = val[0]
			elif len(val) > 1: self._ref_series = val

		# Single value (ImagingSeries or None)
		elif val is None or isinstance(val, ImagingSeries):
			self._ref_series = val

		# Unsupported value
		else:
			raise TypeError('Unsupported DICOM-SR reference series. Type: "%s"' % type(val))
			

class SonadorComprehensiveSR3D(SonadorBaseSR):
	'''	Wrapper class for writing and reading measurements, findings, and other structured data 
		to and from DICOM-SR documents.

		@property reference_frames (ImageTransformMatrixCollection): reference frames
			(encoded as transform matrixes) associated with the report.
		@property measurements (MeasurementCollection): primary measurement collection for the report.
		@property fidnings (FindingsCollection): primary findings collection for the report.
		@property groups (ReportGroupCollection): secondary report groups that contain
			their own set of measurements and qualitative findings.
	'''
	report_title = DCMSR_SONADOR_DATA_REPORT
	dcmsr_report_template = SonadorSRMeasurementReport
	dcmsr_document_template = Comprehensive3DSR

	primary_group_identifier = DCMSR_SONADOR_DATA_REPORT.value

	def __init__(self, reference_frames:Union[Sequence[ImageTransformMatrix],ImageTransformMatrixCollection]=None, 
			points:Union[Sequence[ImageCoord],ImageCoordCollection]=None,
			measurements:Union[Sequence[Measurement],MeasurementCollection]=None,
			findings:Union[Sequence[Finding],FindingCollection]=None,
			groups:Union[Sequence[ReportGroup],ReportGroupCollection]=None,
			**kwargs):
		'''	Initialize comprehensive SR instance

			@input reference_frames (iterable of ImageTransformMatrix): transform matrices (representing DICOM
				reference frames) associated with the report. Added to the `RegistrationSequence` DICOM header
				of the encoded SR document. Refer to:
				https://dicom.nema.org/medical/Dicom/2016c/output/chtml/part03/sect_C.20.2.html
			@input measurements (iterable of findings/measurements): 
		'''
		# Report components
		self.report_title = kwargs.get('report_title', self.report_title)

		# Report data
		self.reference_frames = reference_frames
		self.measurements = measurements
		self.findings = findings
		self.groups = groups

		# Primary measurement group attributes
		self.primary_group_identifier = kwargs.get('primary_group_identifier') or self.primary_group_identifier
		self.primary_group_uid = kwargs.get('primary_group_uid') \
			or getattr(self, 'primary_group_uid', None)
		self.primary_group_method = kwargs.get('primary_group_method') \
			or getattr(self, 'primary_group_method', None)
		self.primary_group_finding_sites = kwargs.get('primary_group_finding_site') \
			or getattr(self, 'primary_group_finding_site', None)
		self.primary_group_algorithm = kwargs.get('primary_group_algorithm') \
			or getattr(self, 'primary_group_algorithm', None)
		self.primary_group_finding_category = kwargs.get('primary_group_finding_category') \
			or getattr(self, 'primary_group_finding_category', None)

		# Initialize base class
		super().__init__(**kwargs)

	@property
	def reference_frames(self):
		return self._reference_frames

	@reference_frames.setter
	def reference_frames(self, val):
		''' Add reference frame (transform matrix instnces) to report model
		'''
		# Convert to collection
		if val and not isinstance(val, ImageTransformMatrixCollection):
			val = ImageTransformMatrixCollection([tx for tx in val])

		self._reference_frames = val

	@property
	def measurements(self):
		'''	Add measurements to report model
		'''
		return self._measurements

	@measurements.setter
	def measurements(self, val):
		'''	Add measurements to report model
		'''
		# Convert to collection
		if val and not isinstance(val, MeasurementCollection):
			val = MeasurementCollection([m for m in val])

		self._measurements = val

	@property
	def findings(self):
		return self._findings

	@findings.setter
	def findings(self, val):
		'''	Add findings to report model
		'''
		# Convert to collection
		if val and not isinstance(val, FindingCollection):
			val = FindingCollection([m for m in val])

		self._findings = val

	@property
	def groups(self):
		return self._groups

	@groups.setter
	def groups(self, val):
		'''	Add groups to report model
		'''
		# Convert to collection
		if val and not isinstance(val, ReportGroupCollection):
			val = ReportGroupCollection([g for g in val])

		self._groups = val

	def _imaging_measurement_groups(self, **kwargs):
		'''	Iterable of all measurement groups (measurement and point) associated with the report.
			If the SR model measurements or findings collections are populated, a "primary" group is populated
			and placed first in the sequence.

			Note: options for the primary group can be passed to the instance by prefixing the keyword
			arguments with a `primary_group_*` prefix.
		'''
		groups = kwargs.get('groups', [])
		if self.measurements or self.findings:
			_pgroup_kwargs = {
				'tracking_id': kwargs.get('primary_group_tacking_id') or TrackingIdentifier(
					uid=kwargs.get('primary_group_uid') or self.primary_group_uid, 
					identifier=kwargs.get('primary_group_identifier') or self.primary_group_identifier),
				'findings': self.findings, 'measurements': self.measurements,
				'finding_sites': kwargs.get('primary_site_finding_sites') or self.primary_group_finding_sites,
				'method': kwargs.get('primary_group_method') or self.primary_group_method,
				'algorithm': kwargs.get('primary_group_algorithm') or self.primary_group_algorithm,
				'finding_category': kwargs.get('primary_group_finding_category') or self.primary_group_finding_category,
				'finding_type': kwargs.get('primary_group_finding_category') or self.primary_group_finding_category,
			}
			_pgroup_kwargs.update(omit(kwargs, [k for k in kwargs.keys() if 'primary_group' in k]))

			# Filter out null or empty arguments
			groups.append(ReportGroup(**pick(_pgroup_kwargs, _pgroup_kwargs.keys())))

		groups.extend(g for g in (self.groups or []) if (isinstance(g, ReportGroup) and not isinstance(g, VolumePointCollectionGroup)))
		return groups

	def _meta_groups(self, **kwargs):
		'''	Iterable of all meta groups associated with the report
		'''
		return [g for g in (self.groups or []) if isinstance(g, ReportMetaGroup)]

	def _volume_measurement_groups(self, **kwargs):
		'''	Iterable of all volume groups associated with the report
		'''
		return [g for g in (self.groups or []) if isinstance(g, VolumePointCollectionGroup)]

	def create_report_sr(self, **kwargs):
		'''	Create DICOM-SR report structure from model data
		'''
		# Replace report_ in report key names.
		for kw in kwargs.keys():
			if 'report_' in kw:
				kwargs[kw.replace('report_', '')] = kwargs.pop(kw)

		return self.dcmsr_report_template(
			title=self.report_title, procedure_reported=self.report_procedure,
			observation_context=self._observation_context or self.observation_context(**pick(kwargs, tuple(k for k in kwargs.keys() if 'observation_context' in k))),
			imaging_measurements=[g.sr for g in self._imaging_measurement_groups(**pick(kwargs, tuple(k for k in kwargs.keys() if 'primary_group' in k)))],
			meta_groups=[g.sr for g in self._meta_groups()], 
			volume_measurements=[g.sr for g in self._volume_measurement_groups()],
			**kwargs)

	@property
	def evidence(self):

		if getattr(self, '_evidence', None) is None and self.ref_series:

			# Reference series is a single instance
			if isinstance(self.ref_series, ImagingSeries):				
				self._evidence = [dcm for dcm in self.ref_series.instances_collection.dcmfiles]

			# Reference series is an iterable (list, tuple, or collection)
			elif isinstance(self.ref_series, (list, tuple, ImagingResourceBaseCollection)):
				self._evidence = list(itertools.chain(*tuple(tuple(dcm for dcm in sx.instances_collection.dcmfiles) for sx in self.ref_series)))

			# Unsupported reference series type
			else:
				raise NotImplementedError('Unable to retrieve DICOM series for the provided reference series')

		return getattr(self, '_evidence', None)

	def create_sr(self, dcmsr_series_uid=None, dcmsr_instance_uid=None,
			dcmsr_series_number=1, dcmsr_instance_number=1, ts=None, is_complete=True,
			is_final=False, is_verified=False, series_headers_kwargs=None, **kwargs):
		'''	Create DICOM-SR document from model data
		'''
		if not getattr(self, '_evidence', None) and not self.ref_series:
			raise ValueError('Unable to create DICOM-SR instance. No `evidence` avaialble, '
				+ 'and no reference series for the model has been defined.')

		ts = ts or datetime.datetime.utcnow()

		# Initialize SR document instance
		_srdoc = self.dcmsr_document_template(
			evidence=self.evidence,
			content=self.create_report_sr(**pick(kwargs, tuple(k for k in kwargs.keys() if 'report' in k))),
			series_instance_uid=dcmsr_series_uid or generate_uid(), sop_instance_uid=dcmsr_instance_uid or generate_uid(),
			series_number=dcmsr_series_number, instance_number=dcmsr_instance_number, 
			manufacturer=self.dcmsr_document_manufacturer, is_complete=is_complete, is_final=is_final, is_verified=is_verified,
			**omit(kwargs, tuple(k for k in kwargs.keys() if not 'report' in k)))

		# Apply series headers to report instance
		self.dcmsr_series_headers(_srdoc, ts, **(series_headers_kwargs or {}))
		return _srdoc

	def dcmsr_series_headers(self, srdoc, *args, **kwargs):
		'''	Add reference frames and other headers to SR document instance
		'''
		# Add headers from parent instance
		srdoc = super().dcmsr_series_headers(srdoc, *args, **kwargs)

		# Add reference frames
		if self.reference_frames:
			setattr(srdoc, DCMHEADER_REGISTRATION_SEQUENCE, DcmSequence([rf.sr for rf in self.reference_frames]))

		return srdoc

	@property
	def sr(self):
		raise NotImplementedError('Add support for retrieving the most recent SR version!')
