import functools
from collections import namedtuple, OrderedDict

from ...apisettings import DCMHEADER_SERIES_INSTANCE_UID, \
	DCMHEADER_SR_DERIVATION_IMAGE_SEQ, DCMHEADER_SR_SOURCE_IMAGE_SEQ, DCMHEADER_SR_REF_INSTANCE_UID, \
	DCMHEADER_SR_REF_SERIES_SEQ, DCMHEADER_SR_REF_INSTANCE_SEQ

from .base import DcmInstanceCoreResource, DcmInstanceCoreCollection, ImagingSeriesBulkPopulateMixin
from .sr import DcmSRSeries, DcmSRSeriesCollection, DcmStructuredInstance, DcmStructuredInstanceCollection
from .media import DcmEncapsulatedDocumentSeries, DcmEncapsulatedDocumentSeriesCollection, \
	DcmEncapsulatedDocumentInstance, DcmEncapsulatedDocumentInstanceCollection


class DcmM3DSeries(DcmEncapsulatedDocumentSeries):
	''' Model representation of DICOM encoded 3D models (STL/GLB)
	'''
	@property
	def dcminstance_modelcollection_class(self): return DcmM3DInstanceCollection


class DcmM3DSeriesCollection(DcmEncapsulatedDocumentSeriesCollection):
	'''	Collection of M3D models
	'''
	model = DcmM3DSeries


class DcmM3DInstance(DcmEncapsulatedDocumentInstance):
	'''	DCM instance model used for 3D models (STL/GLB)
	'''

class DcmM3DInstanceCollection(DcmEncapsulatedDocumentInstanceCollection):
	'''	Collection of 3D model instances	
	'''
	model = DcmM3DInstance
