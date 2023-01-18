import functools
from collections import namedtuple, OrderedDict

from ...apisettings import DCMHEADER_SERIES_INSTANCE_UID, \
	DCMHEADER_SR_DERIVATION_IMAGE_SEQ, DCMHEADER_SR_SOURCE_IMAGE_SEQ, DCMHEADER_SR_REF_INSTANCE_UID, \
	DCMHEADER_SR_REF_SERIES_SEQ, DCMHEADER_SR_REF_INSTANCE_SEQ

from .base import DcmInstanceCoreResource, DcmInstanceCoreCollection, ImagingSeriesBulkPopulateMixin
from .sr import DcmSRSeries, DcmSRSeriesCollection, DcmStructuredInstance, DcmStructuredInstanceCollection


class DcmM3DSeries(DcmSRSeries):
	''' Model representation of DICOM encoded 3D models (STL/GLB)
	'''
	@property
	def dcminstance_modelcollection_class(self): return DcmM3DInstanceCollection


class DcmM3DSeriesCollection(DcmSRSeriesCollection):
	'''	Collection of M3D models
	'''
	model = DcmM3DSeries


class DcmM3DInstance(DcmStructuredInstance):
	'''	DCM instance model used for 3D models (STL/GLB)
	'''
	@property
	@functools.lru_cache()
	def instance_reference_uids(self):
		'''	Cached property for retrieving the reference UIDs of image instances associated with the segmentation.
			@returns set of all unique instance UIDs referenced by the segmentation instance.
		'''
		instance_references = set()

		# Iterate through all references in the sequence, unpack reference UIDs
		for refset in self.tags.get(DCMHEADER_SR_REF_SERIES_SEQ, []):
			for ref in refset.get(DCMHEADER_SR_REF_INSTANCE_SEQ, []):
				if ref.get(DCMHEADER_SR_REF_INSTANCE_UID):
					instance_references.add(ref.get(DCMHEADER_SR_REF_INSTANCE_UID))
		
		return instance_references

	@property
	@functools.lru_cache()
	def series_reference_uids(self):
		'''	Cached property for retrieving the reference UIDs of image series associated with the segmentation.
			
			@returns set of all unique series UIDs referenced by the segmentation instance.
		'''
		return set([refset.get(DCMHEADER_SERIES_INSTANCE_UID) for refset in self.tags.get(DCMHEADER_SR_REF_SERIES_SEQ, [])
			if refset.get(DCMHEADER_SERIES_INSTANCE_UID)])


class DcmM3DInstanceCollection(DcmStructuredInstanceCollection):
	'''	Collection of 3D model instances	
	'''
	model = DcmM3DInstance
