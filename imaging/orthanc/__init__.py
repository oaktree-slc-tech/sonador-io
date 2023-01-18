from collections import OrderedDict

from .base import parse_image_orientation, ImagingPatient, ImagingPatientCollection, \
	ImagingStudy, ImagingStudyCollection, ImagingSeries, ImagingSeriesCollection, \
	DcmInstance, ImagingPatientCollection, DcmInstanceCollection, \
	FILEARCHIVE_TYPE_ZIPARCHIVE, FILEARCHIVE_TYPE_DICOMDIR, FILEARCHIVE_TYPE_SUPPORTED, \
	ImageCoord, ImageSpacing, ImageOrientation, IMAGING_INSTANCE_OUTPUT_COLUMNS, \
	IMAGING_SERVER_RESOURCE_PATIENT, IMAGING_SERVER_RESOURCE_STUDY, IMAGING_SERVER_RESOURCE_SERIES, IMAGING_SERVER_RESOURCE_IMAGE
from .sr import DcmSRSeries, DcmSRSeriesCollection, IMAGING_SERVER_RESOURCE_REPORT
from .jobs import OrthancJob, OrthancJobCollection, OrthancJobResult
from .m3d import DcmM3DSeries, DcmM3DSeriesCollection


IMAGING_SERVER_RESOURCE_DATAMODEL_COLLECTIONTYPES = OrderedDict((
		(IMAGING_SERVER_RESOURCE_PATIENT, ImagingPatientCollection),
		(IMAGING_SERVER_RESOURCE_STUDY, ImagingStudyCollection), 
		(IMAGING_SERVER_RESOURCE_SERIES, ImagingSeriesCollection),
		(IMAGING_SERVER_RESOURCE_IMAGE, DcmInstanceCollection)
	))

IMAGING_SERVER_RESOURCE_DATAMODEL_TYPES = OrderedDict((
		(IMAGING_SERVER_RESOURCE_PATIENT, ImagingPatient),
		(IMAGING_SERVER_RESOURCE_STUDY, ImagingStudy), 
		(IMAGING_SERVER_RESOURCE_SERIES, ImagingSeries),
		(IMAGING_SERVER_RESOURCE_IMAGE, DcmInstance)
	))