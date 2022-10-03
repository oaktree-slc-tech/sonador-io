'''	Model classes associated with DICOM-SR documents. Provides tools for querying
	and inspecting the contents of reports with structured data and image segmentations.
'''
import functools, datetime, logging
from abc import ABCMeta, abstractmethod

from pydicom.dataset import Dataset
from pydicom.sequence import Sequence

from ...apisettings import IMAGING_SERVER_RESOURCE_REPORT, DCMHEADER_SERIES_INSTANCE_UID, \
	DCMHEADER_SR_PERTINENT_OTHER_EVIDENCE_SEQUENCE, DCMHEADER_SR_REF_SERIES_SEQ, \
	DCMHEADER_SR_REF_SOP_SEQ, DCMHEADER_SR_REF_INSTANCE_UID
from .base import ImagingSeriesCoreResource, ImagingSeriesCollection, ImagingServerChildCollection, \
	DcmInstanceCoreResource, DcmInstanceCoreCollection, ImagingSeriesBulkPopulateMixin

logger = logging.getLogger(__name__)


def dcmmencode_procedure_code(val, scheme_designator, meaning, scheme_version=None, 
		dataset=None):
	'''	Encodes a PyDicom procedure code entry (0008,1032) with the provided
		value, scheme designator

		@input dataset (pydicom Dataset instance, default=new instance): Dataset instance
			to which the value, scheme designator, meaning, and scheme version
			should be added.
	'''
	dataset = dataset or Dataset()
	dataset.CodeValue = val
	dataset.CodingSchemeDesignator = scheme_designator
	if scheme_version:
		dataset.CodingSchemeVersion = scheme_version

	return dataset


class DcmSRSeries(ImagingSeriesCoreResource):
	'''	DICOM-SR: sturctured report of medical imaging results
	'''
	@property
	def dcminstance_modelcollection_class(self): return DcmSRInstanceCollection

	@property
	def instances(self):
		'''	Retrieve instance UIDs for the series
		'''
		return self._objectdata.get('Instances')

	@property
	def instances_collection(self):
		'''	Cached property for retrieving the DICOM-SR instances which belong to the series
		'''
		if getattr(self, '_instances', None) is None:
			setattr(self, '_instances', self.fetch_dcminstances())

		return self._instances

	@property
	def series_reference_uids(self):
		'''	Cached property for retrieving the reference UIDs of image series associated the segmentation. 
		'''
		return self.instances_collection.series_reference_uids

	@property
	@functools.lru_cache()
	def imaging_series_collection(self):
		''' Imaging series that are referenced by the DICOM-SR instance with the most recent
			imaging series first.
		'''
		return sorted(
			[s for s in self.parent.series_collection if s.series_uid in self.series_reference_uids],
			key=lambda s: s.ts if s.ts else datetime.datetime(year=1900, month=1, day=1),
			reverse=True)


class DcmSRSeriesCollection(ImagingSeriesBulkPopulateMixin, ImagingServerChildCollection):
	model = DcmSRSeries

	def __init__(self, *args, **kwargs):
		self.parent = kwargs.pop('study', None)
		super(DcmSRSeriesCollection, self).__init__(*args, **kwargs)

	def _init_collection_models(self, **kwargs):
		if self.parent:
			kwargs['study'] = self.parent

		return super(DcmSRSeriesCollection, self)._init_collection_models(**kwargs)


class DcmStructuredInstance(DcmInstanceCoreResource):
	'''	DCM base class shared for DICOM-SR and DICOM-SEG documents
	'''
	@property
	@abstractmethod
	def instance_reference_uids(self):
		'''	Abstract property to retrieve the reference UIDs of all image instances associated
			with the structured DICOM resource.
			
			@returns set of resource UIDs
		'''	

	@property
	@abstractmethod
	def series_reference_uids(self):
		'''	Abstract property to retrieve the reference UIDs of all image series associated
			with the structured DICOM resource.
			
			@returns set resource UIDs
		'''


class DcmStructuredInstanceCollection(DcmInstanceCoreCollection):
	''' Collection base class used for managing DICOM-SR and DICOM-SEG instances
	'''
	@property
	@functools.lru_cache()
	def series_reference_uids(self):
		'''	Cached property for retrieving the reference UIDs of image series associated
			the segmentation instances in the collection. 
		'''
		return functools.reduce(lambda a,b: a.union(b), (dcm.series_reference_uids for dcm in self))


class DcmSRInstance(DcmStructuredInstance):
	'''	DCM Instance model used for DICOM-SR reports
	'''
	@property
	@functools.lru_cache()
	def instance_reference_uids(self):
		'''	Cached property for retrieving the reference UIDs of image instances associated with
			the structured report.
			@returns set of all unique instance UIDs referenced by the structed report instance
		'''
		instance_references = set()

		# Iterate through all references in the sequence, unpack reference UIDs
		for refset in self.tags.get(DCMHEADER_SR_PERTINENT_OTHER_EVIDENCE_SEQUENCE, []):
			for ref in refset.get(DCMHEADER_SR_REF_SERIES_SEQ, []):
				for dcm in ref.get(DCMHEADER_SR_REF_SOP_SEQ, []):
					if dcm.get(DCMHEADER_SR_REF_INSTANCE_UID):
						instance_references.add(dcm.get(DCMHEADER_SR_REF_INSTANCE_UID))

		return instance_references

	@property
	@functools.lru_cache()
	def series_reference_uids(self):
		'''	Cached property for retrieving the reference UIDs of image series associated with the
			structured report.
			@returns set of all unique series UIDs referenced by the structed report instance
		'''
		series_references = set()
		
		# Iterate through all series references in the report, unpack UIDs
		for refset in self.tags.get(DCMHEADER_SR_PERTINENT_OTHER_EVIDENCE_SEQUENCE, []):
			for ref in refset.get(DCMHEADER_SR_REF_SERIES_SEQ):
				if ref.get(DCMHEADER_SERIES_INSTANCE_UID):
					series_references.add(ref.get(DCMHEADER_SERIES_INSTANCE_UID))

		return series_references


class DcmSRInstanceCollection(DcmStructuredInstanceCollection):
	'''	Collection of DICOM-SR instances
	'''
	model = DcmSRInstance
	
