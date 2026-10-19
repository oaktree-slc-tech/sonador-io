import functools
from collections import namedtuple, OrderedDict

from ...apisettings import DCMHEADER_SERIES_INSTANCE_UID, \
	DCMHEADER_SR_DERIVATION_IMAGE_SEQ, DCMHEADER_SR_SOURCE_IMAGE_SEQ, DCMHEADER_SR_REF_INSTANCE_UID, \
	DCMHEADER_SR_REF_SERIES_SEQ, DCMHEADER_SR_REF_INSTANCE_SEQ

from ..helpers.m3d import M3DFrame, dcm_m3d_referenced_series, dcm_m3d_source_frame_headers, dcm_m3d_frame

from .base import DcmInstanceCoreResource, DcmInstanceCoreCollection, ImagingSeriesBulkPopulateMixin
from .sr import DcmSRSeries, DcmSRSeriesCollection, DcmStructuredInstance, DcmStructuredInstanceCollection
from .media import DcmEncapsulatedDocumentSeries, DcmEncapsulatedDocumentSeriesCollection, \
	DcmEncapsulatedDocumentInstance, DcmEncapsulatedDocumentInstanceCollection


class DcmM3DSeries(DcmEncapsulatedDocumentSeries):
	''' Model representation of DICOM encoded 3D models (STL/GLB)
	'''
	@property
	def dcminstance_modelcollection_class(self): return DcmM3DInstanceCollection

	@property
	def frame(self):
		'''	Frame of reference and units of the models of the series, resolved from the first
			instance (DcmM3DInstance.frame). None if the series has no instances.

			@returns sonador.imaging.helpers.m3d.M3DFrame or None
		'''
		instances = self.instances_collection
		return instances[0].frame if len(instances) else None


class DcmM3DSeriesCollection(DcmEncapsulatedDocumentSeriesCollection):
	'''	Collection of M3D models
	'''
	model = DcmM3DSeries


class DcmM3DInstance(DcmEncapsulatedDocumentInstance):
	'''	DCM instance model used for 3D models (STL/GLB)

		@property source_series: series referenced by the model (source imaging and segmentations)
		@property frame: frame of reference and units of the model
	'''
	def referenced_series(self, headers=None):
		'''	Series of the parent study referenced by the model (ReferencedSeriesSequence): the imaging
			and segmentation series the model was derived from, image series first
			(sonador.imaging.helpers.m3d.dcm_m3d_referenced_series).

			@input headers (pydicom.dataset.Dataset or dict, default=instance tags): headers of the
				model from which the references are read

			@returns list of sonador.imaging.orthanc.base.ImagingSeries
		'''
		return dcm_m3d_referenced_series(self, headers)

	@property
	def source_series(self):
		'''	Cached series referenced by the model (referenced_series, read from the instance tags)

			@returns list of sonador.imaging.orthanc.base.ImagingSeries
		'''
		if getattr(self, '_source_series', None) is None:
			self._source_series = self.referenced_series()

		return self._source_series

	def source_frame_headers(self, headers=None):
		'''	Headers of the referenced series from which a frame of reference may be inherited, as
			(series UID, headers) pairs in order of preference
			(sonador.imaging.helpers.m3d.dcm_m3d_source_frame_headers)

			@returns generator of (str, dict)
		'''
		return dcm_m3d_source_frame_headers(self, headers)

	def resolve_frame(self, headers=None, fallback=True, **kwargs):
		'''	Resolve the frame of reference and units of the model
			(sonador.imaging.helpers.m3d.dcm_m3d_frame): the model headers first, with a fallback to
			the headers of the referenced series.

			@input headers (pydicom.dataset.Dataset or dict, default=instance tags): headers of the
				model. A retrieved DICOM file (`dcmfile`) may be given so that no tags request is made.
			@input fallback (bool, default=True): consult the referenced series when the model does
				not declare a frame of reference
			@input kwargs: forwarded to dcm_m3d_frame_from_headers (default_units, coordinate_system)

			@returns sonador.imaging.helpers.m3d.M3DFrame
		'''
		return dcm_m3d_frame(self, headers=headers, fallback=fallback, **kwargs)

	@property
	def frame(self):
		'''	Cached frame of reference and units of the model (resolve_frame with the defaults)

			@returns sonador.imaging.helpers.m3d.M3DFrame
		'''
		if getattr(self, '_frame', None) is None:
			self._frame = self.resolve_frame()

		return self._frame


class DcmM3DInstanceCollection(DcmEncapsulatedDocumentInstanceCollection):
	'''	Collection of 3D model instances	
	'''
	model = DcmM3DInstance
