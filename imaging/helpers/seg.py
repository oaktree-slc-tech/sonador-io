# io helper libraries
from io import BytesIO

# Numberical computing libraries
import numpy as np

# pydicom codes and helper methods
from pydicom.sr.codedict import codes as dcmcodes
from pydicom.uid import generate_uid
from pydicom.sequence import Sequence
from pydicom.dataset import Dataset

# highdicom components used for identifying and encoding segmentations
from highdicom.content import AlgorithmIdentificationSequence
from highdicom.seg.content import SegmentDescription
from highdicom.seg.enum import (
    SegmentAlgorithmTypeValues,
    SegmentationTypeValues
)
from highdicom.seg.sop import Segmentation

# highdicom Template components
from highdicom.sr.enum import GraphicTypeValues3D, \
    ValueTypeValues, RelationshipTypeValues
from highdicom.sr.templates import Code, CodedConcept

from ..orthanc.base import ImagingSeries
from ...apisettings import dcmcodes, SONADOR_DEVELOPMENT_TEAM, SONADOR_CLIENT, \
	HIGHDICOM_MANUFACTURER, HIGHDICOM_DEVELOPMENT_TEAM, HIGHDICOM_VERSION, \
	DCMSR_SONADOR_SEG, DCMSR_SONADOR_SR


def dcmseg_encode_segment_description(snumber: int, slabel: str, tracking_id: str, 
		property_category: Code, property_type: Code, 
		segment_attrs=None, tracking_uid=None, algorithm_type=SegmentAlgorithmTypeValues.MANUAL):
	'''	Encode DICOM-SEG segment description

		@input snumber (int): Segment number
		@input slabel (str): Segment label
		@input property_category: Category of data to which the segmentation belongs.
		@input property_type: Type of data associated with the segmentation.
		@input tracking_uid (uid, default=generate new): Tracking ID to be applied to
			the segmentation data.

		@returns highdicom.seg.content.SegmentDescription
	'''
	segment_attrs = segment_attrs or {}
	segment_attrs.update({
		'segment_number': snumber, 'segment_label': slabel, 'algorithm_type': algorithm_type,
		'tracking_uid': tracking_uid or generate_uid(), 'tracking_id': tracking_id, 
	})

	# Add optional parameters
	if property_category:
		segment_attrs['segmented_property_category'] = property_category
	if property_type:
		segment_attrs['segmented_property_type'] = property_type

	return SegmentDescription(**segment_attrs)


def dcmseg_encode_segmentation(series: ImagingSeries, pixel_array: np.ndarray, 
		descriptions, series_number: int, series_description: str,
		segmentation_attrs=None, series_instance_uid=None, 
		sop_instance_uid=None, instance_number=1,
		segmentation_type=SegmentationTypeValues.BINARY,
		manufacturer=HIGHDICOM_DEVELOPMENT_TEAM, manufacturer_model_name=HIGHDICOM_MANUFACTURER,
		software_versions=str(HIGHDICOM_VERSION), device_serial_number=None):
	'''	Encode DICOM-SEG document

		@input series (sonador.imaging.orthanc.ImagingSeries): ImagingSeries with which
			the segmentation should be associated.
		@input pixel_array (NumPy.ndarray): Segmentation masks
		@input descriptions (iterable of SegmentDescription objects): Descriptions for the
			segmentations.
		@input series_number (int): Number for the series
		@input series_description (str): Description to use for the series

		@returns highdicom.seg.Segmentation
	'''
	# Retrieve images
	source_dcmimages = [s.dcmfile(cache=True) for s in series.slices_collection]

	# Check DICOM images for fields required by the segmentation, backfill if needed.
	# 1. FrameOfReferenceUID: https://dicom.innolitics.com/ciods/mr-image/frame-of-reference/00200052
	if source_dcmimages and not getattr(source_dcmimages[0], 'FrameOfReferenceUID', None):

		# Generate a new frame of reference UID
		frameref_uid = generate_uid()

		for dcm in source_dcmimages:
			setattr(dcm, 'FrameOfReferenceUID', frameref_uid)

	# 2. SliceThickness: https://dicom.innolitics.com/ciods/rt-dose/image-plane/00180050
	if source_dcmimages and not getattr(source_dcmimages[0], 'SliceThickness', None):
		raise ValueError('Invalid source images: DICOM files missing SliceThickness attribute')

	segmentation_attrs = segmentation_attrs or {}
	segmentation_attrs.update({

		# Software manufacturer and versions
		'manufacturer': manufacturer,
        'manufacturer_model_name': manufacturer_model_name,
        'software_versions': software_versions,
        'device_serial_number': device_serial_number or generate_uid(),

        # Source images attributes contains the list of pydicom datasets for the source images
        'source_images': source_dcmimages,

        # Pixel arrays and segmentation descriptions
        'pixel_array': pixel_array,
        'segmentation_type': segmentation_type,
        'segment_descriptions': descriptions,

        # DICOM-SR identifiers uniquely identifying the report
        'series_instance_uid': series_instance_uid or generate_uid(),
        'sop_instance_uid': sop_instance_uid or generate_uid(),
        'series_number': series_number, 
        'series_description': series_description, 
        'instance_number': instance_number
	})

	return Segmentation(**segmentation_attrs)
