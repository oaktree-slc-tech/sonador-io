from collections import OrderedDict

from .base import parse_image_orientation, ImagingPatient, ImagingPatientCollection, \
	ImagingStudy, ImagingStudyCollection, ImagingSeries, ImagingSeriesCollection, \
	DcmInstance, ImagingPatientCollection, \
	FILEARCHIVE_TYPE_ZIPARCHIVE, FILEARCHIVE_TYPE_DICOMDIR, FILEARCHIVE_TYPE_SUPPORTED, \
	ImageCoord, ImageSpacing, ImageOrientation, IMAGING_INSTANCE_OUTPUT_COLUMNS, \
	IMAGING_SERVER_RESOURCE_PATIENT, IMAGING_SERVER_RESOURCE_STUDY, IMAGING_SERVER_RESOURCE_SERIES
from .sr import DcmSRSeries, DcmSRSeriesCollection, IMAGING_SERVER_RESOURCE_REPORT



IMAGING_SERVER_RESOURCE_DATAMODEL_COLLECTIONTYPES = OrderedDict((
		(IMAGING_SERVER_RESOURCE_PATIENT, ImagingPatientCollection),
		(IMAGING_SERVER_RESOURCE_STUDY, ImagingStudyCollection), 
		(IMAGING_SERVER_RESOURCE_SERIES, ImagingSeriesCollection),
	))