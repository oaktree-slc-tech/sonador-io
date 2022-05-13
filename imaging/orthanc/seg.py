import functools
from collections import namedtuple, OrderedDict
from highdicom.seg.utils import iter_segments as dcmseg_iter_segments

from ...apisettings import DCMHEADER_SERIES_INSTANCE_UID, \
	DCMHEADER_SR_DERIVATION_IMAGE_SEQ, DCMHEADER_SR_SOURCE_IMAGE_SEQ, DCMHEADER_SR_REF_INSTANCE_UID, \
	DCMHEADER_SR_REF_SERIES_SEQ, DCMHEADER_SR_REF_INSTANCE_SEQ

from .base import DcmInstanceCoreResource, DcmInstanceCoreCollection
from .sr import DcmSRSeries, DcmSRSeriesCollection, DcmStructuredInstance, DcmStructuredInstanceCollection


# Data classes for working with image segments. Within Sonador, all image segment
# data is stored within DcmSegmentationInstance objects, which can require nested loops.
# ImageSegment provides an interface from the DcmSegmentationSeries to access segment data,
# masks, and labels.
ImageSegment = namedtuple('ImageSegment', ('dcm', 'meta', 'data'))

# ImageSegmentMeta objects contain the number and labels for segments.
ImageSegmentMeta = namedtuple('ImageSegmentMeta', ('number', 'label', 'series'))

# ImageSegmentData objects contain the pydicom.Dataset instances, frames/masks (numpy.ndarray),
# and DICOM frame descriptions (needed to match the segmentation labels and masks to the correct slices).
ImageSegmentData = namedtuple('ImageSegmentData', 
	('description','frames', 'frame_descriptions', 'resource_instance_uids'))



class DcmSegmentationSeries(DcmSRSeries):
	'''	DICOM-SEG: structured report instance containing image segmentations
	'''
	@property
	def dcminstance_modelcollection_class(self): return DcmSegmentationInstanceCollection

	@property
	def segments(self):
		''' Convenience method which can be used to iterate across all of the sgments
			within the segmentation series. In the case of multiple
			segmentation files, the segments will be parsed based on the DICOM-SEG file
			instance number.

			@iterator: returns a three member tuple (ImageSegment) including the 
				DcmSegmentationInstance, the image segment metadata (name/label), 
				and the image segment data  (DCM description, frames (numpy.ndarray), 
				and DCM frame descriptions)
		'''
		for dcm in self.instances_collection:
			for smeta, sdata in dcm.segments.items():
				yield ImageSegment(dcm, smeta, sdata)


class DcmSegmentationSeriesCollection(DcmSRSeriesCollection):
	'''	Collection of DICOM-SEG series
	'''
	model = DcmSegmentationSeries


class DcmSegmentationInstance(DcmStructuredInstance):
	'''	DCM Instance model used for DICOM-SEG instances
	'''
	def parse_segmentation(self, cache=False):
		'''	Retrieve a list of the DICOM instance UIDs linked to the segments in the segmentation.

			@returns  OrderedDict: contains a set of all UID references for each layer of
				the segmentation
		'''
		segments = OrderedDict()

		# Iterate through 
		for frames, fdescriptions, description in dcmseg_iter_segments(self.dcmfile(cache=cache)):

			# Unpack details of the image segment: number, label, description
			seg = ImageSegmentMeta(description.SegmentNumber, description.SegmentLabel, self.parent)
			segments[seg] = ImageSegmentData(description, frames, fdescriptions, set())

			# Unpack DICOM UID references
			for fitem in fdescriptions:

				# Image SOP instances are part of DerivationImageSequence.ReferencedSOPInstanceUID
				# DICOM packages the references as a sequences of sequences. This method unpacks
				# all unique IDs to a set.
				for ds in getattr(fitem, DCMHEADER_SR_DERIVATION_IMAGE_SEQ, []):
					for ref in getattr(ds, DCMHEADER_SR_SOURCE_IMAGE_SEQ):
						if hasattr(ref, DCMHEADER_SR_REF_INSTANCE_UID):
							segments[seg].resource_instance_uids.add(ref.ReferencedSOPInstanceUID)

		return segments

	@property
	@functools.lru_cache()
	def segments(self):
		'''	Cached property for retrieving the parsed components of the segmentation.
			@returns OrderedDict with the segmentation frames, frame descriptions, and resource instance UIDs
				indexed to the segment metadata.
		'''
		return self.parse_segmentation(cache=True)

	@property
	@functools.lru_cache()
	def segment_reference_uids(self):
		''' Cached property for retrieving the reference UIDs of images associated with each of the segments.
			@returns OrderedDict with the reference UIDs of instances grouped by the segment.
		'''
		return OrderedDict(
			(seg, segdata.resource_instance_uids) for seg, segdata in self.segments.items())

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


class DcmSegmentationInstanceCollection(DcmStructuredInstanceCollection):
	'''	Collection of DICOM-SEG instances
	'''
	model = DcmSegmentationInstance
