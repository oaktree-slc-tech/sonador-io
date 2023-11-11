'''	DICOM-SR constants, data structures, and encoded concepts for Sonador.
	Provides utilities and tools to simplify the process of encoding and parsing DICOM-SR data.
'''
import re, numbers
from collections import namedtuple
from typing import Union, Sequence

import numpy as np

from pydicom import Dataset as DcmDataset, Sequence as DcmSequence
from pydicom.uid import generate_uid
from pydicom.sr.codedict import codes as dcmcodes

from highdicom.version import __version__ as HIGHDICOM_VERSION
from highdicom.sr.templates import Code as DcmCode, Template as DcmTemplate, CodedConcept as DcmCodedConcept, \
	CodeContentItem, ContentSequence, ObserverContext, ObservationContext, DeviceObserverIdentifyingAttributes, \
	LanguageOfContentItemAndDescendants, MeasurementsAndQualitativeEvaluations, TrackingIdentifier, \
	DEFAULT_LANGUAGE as DCMSR_DEFAULT_LANGUAGE
from highdicom.sr import Scoord3DContentItem, GraphicTypeValues3D, Measurement as DcmMeasurement, \
	QualitativeEvaluation as DcmQualitativeEvaluation
from highdicom.sr.enum import RelationshipTypeValues
from highdicom.uid import UID as DcmUid

from client.utils.object import pick


from .base import ImageCoord, ImageCoordCollection, ImageTransformMatrix, \
	Measurement, MeasurementCollection, Finding, FindingCollection, \
	DCMHEADER_CODE_VALUE, DCMHEADER_CODING_SCHEME_DESIGNATOR, \
	DCMHEADER_CODING_SCHEME_VERSION, DCMHEADER_CODE_MEANING, \
	DCMHEADER_MAPPING_RESOURCE, DCMHEADER_MAPPING_RESOURCE_UID, \
	DCMHEADER_LONG_CODE_VALUE, DCMHEADER_MAPPING_RESOURCE_NAME, \
	DCMHEADER_CONTEXT_GROUP_VERSION, DCMHEADER_CONTEXT_UID, \
	DCMSR_SONADOR_SR, SONADOR_SCHEME_VERSION_02, DCMHEADER_REFERENCE_FRAME, DCMHEADER_MATRIX_REGISTRATION_SEQUENCE, \
	DCMHEADER_MATRIX_REGISTRATION, DCMHEADER_MATRIX_TRANSFORMATION_TYPE, DCMHEADER_REFERENCE_FRAME_MATRIX, \
	DCM_MATRIX_RIGID, DCM_MATRIX_RIGID_SCALE, DCM_MATRIX_AFFINE, DCM_MATRIX_TRANSFORM_SUPPORTED, \
	DCMHEADER_REFERENCE_FRAME_TRANFORM_COMMENT, DCMHEADER_MATRIX_USED_FIDUCIALS, DCMHEADER_SR_REF_IMAGES_SEQ, \
	SONADOR_SCHEME_VERSION_02, DCMHEADER_CONCEPT_CODE_SEQUENCE

DCM_CODED_CONCEPT_HEADERS = (DCMHEADER_CODE_VALUE, DCMHEADER_CODING_SCHEME_DESIGNATOR,
	DCMHEADER_CODE_MEANING, DCMHEADER_CODING_SCHEME_VERSION)
DCM_CODED_CONCEPT_MAPPING = {
	'value': DCMHEADER_CODE_VALUE,
	'scheme_designator': DCMHEADER_CODING_SCHEME_DESIGNATOR,
	'meaning': DCMHEADER_CODE_MEANING,
	'scheme_version': DCMHEADER_CODING_SCHEME_VERSION,
}


def srcode2dataset(dcmcode: Union[DcmCode,DcmCodedConcept], dcm_mdata=None, codedconcept_mapping=DCM_CODED_CONCEPT_MAPPING):
	'''	Convert the provided DICOM-SR code or coded concept to a pydicom.dataset.Dataset instance.

		@input dcmcode (highdicom.sr.template.Code): code instance to convert to a Dataset.
		@input dcm_mdata (pydicom.dataset.Dataset, default=blank dataset instance): 
			dataset to which the code data should be added.

		@returns pydicom.dataset.Dataset
	'''
	# Initialize dataset instance
	dcm_mdata = dcm_mdata or DcmDataset()

	# Map values from Code/CodedConcept to Dataset
	for k,v in codedconcept_mapping.items():
		setattr(dcm_mdata, v, getattr(dcmcode, k, None))

	return dcm_mdata


def dataset2srcode(dcm:DcmDataset, codedconcept_mapping=DCM_CODED_CONCEPT_MAPPING, code_class=DcmCode):
	'''	Convert the provided DICOM dataset to a code or coded concept.

		@input dcm (pydicom.Dataset): dataset to convert to the code/coded concept

		@returns highdicom.Code or highdicom.CodedConcept
	'''
	# Map values from Dataset to Code/CodedConcept
	_kwargs = dict(
		(k, getattr(dcm, v, None)) for k,v in codedconcept_mapping.items())

	return code_class(**pick(_kwargs, _kwargs.keys()))


# Transform Coordinate Systems (Reference Frames)


SONADOR_COORDINATE_TRANSFORM = 'SCoord3DTx'
SONADOR_COORDINATE_TRANSFORM_DESCRIPTION = '3D spatial coordinate transformation'
DCMSR_COORDINATE_TRANSFORM = DcmCode(SONADOR_COORDINATE_TRANSFORM, DCMSR_SONADOR_SR.value, 
	SONADOR_COORDINATE_TRANSFORM_DESCRIPTION, scheme_version=SONADOR_SCHEME_VERSION_02)

SONADOR_COORDINATE_TRANSFORM_MORPHOLOGY = '%s-Morph' % SONADOR_COORDINATE_TRANSFORM
SONADOR_COORDINATE_TRANSFORM_MORPHOLOGY_DESCRIPTION = '3D coordinate transform to facilitate morphology analysis'
DCMSR_COORDINATE_TRANSFORM_MORPHOLOGY = DcmCode(SONADOR_COORDINATE_TRANSFORM_MORPHOLOGY, DCMSR_SONADOR_SR.value, 
	SONADOR_COORDINATE_TRANSFORM_MORPHOLOGY_DESCRIPTION, scheme_version=SONADOR_SCHEME_VERSION_02)


def mtx4x4(mtx: np.ndarray) -> np.ndarray:
	'''	Transform the matrix to a 4x4 representation

		@input mtx (np.ndarray): matrix to transform the 4x4 representation

		@returns np.ndarray
	'''
	Mtx = np.identity(4)

	# Rotational matrix: 3x3 (9 elements)
	if mtx.shape == (3,3):
		Mtx[:3,:3] = mtx

	# Single row rotational matrix
	elif mtx.shape == (9,):
		Mtx[:3,:3] = mtx.reshape((3,3))

	# Rational matrix and x,y,z translational offset (12 elements)
	elif mtx.shape == (3,4):
		Mtx[:3,:4] = mtx

	# Single row rotational matrix and x,y,z translational offet
	elif mtx.shape == (12,):
		Mtx[:3,:4] = mtx.reshape((3,4))

	# Single row 4x4 matrix
	elif mtx.shape == (16,):
		Mtx[:4,:4] = mtx.reshape((4,4))

	# 4x4 matrix
	elif mtx.shape == (4,4):
		Mtx[:4,:4] = mtx

	else:
		raise ValueError('Invalid transform matrix dimensions: %s' % str(mtx.shape))

	return Mtx


def dcm_encode_reference_frame_transform_matrix(txmatrix: np.array, reference_uid:str=None, 
		transform_type:str=None, transform_comment:str=None, fiducials=None, ref_images=None, 
		codes:Sequence[Union[DcmCode,DcmCodedConcept,DcmDataset]]=None,
		dcm_mdata:DcmDataset=None, dcm_matrix_mdata:DcmDataset=None):
	'''	Create a Frame Of Reference DICOM structure for the provided transformation matrix.
		Refer to https://dicom.nema.org/medical/Dicom/2016c/output/chtml/part03/sect_C.20.2.html.
	
		@input txmatrix (np.array): transform matrix to be encoded
		@input dcm_mdata (pydicom.Dataset, default=new dataset): Dataset to w`hich 
			the frame of reference headers should be added to. If no dataset is provided
			a new one will be created.
		@input reference_uid (str, default=new UID): DICOM UID to be used for the
			reference frame. If an existing DICOM meta dataset is provided that already 
			has a frame of reference UID, the previously existing UID is retained.
		@input dcm_matrix_mdata (pydicom.Dataset, default=new dataset): dataset to which matrix headers
			should be added. If not dataset is provided, a new one will be created.

		@returns pydicom.Dataset
	'''
	# Ensure that the provided transform matrix is 4x4
	txmatrix = mtx4x4(txmatrix)

	# Transform dataset and reference UID
	dcm_mdata = dcm_mdata or DcmDataset()
	if not getattr(dcm_mdata, DCMHEADER_REFERENCE_FRAME, None):
		setattr(dcm_mdata, DCMHEADER_REFERENCE_FRAME, reference_uid or generate_uid())

	# Matrix Registration Sequence (single member sequence which contains the matrix sequence)
	dcm_matrix_registration_seq = getattr(dcm_mdata, DCMHEADER_MATRIX_REGISTRATION_SEQUENCE, None) or [DcmDataset()]
	dcm_matrix_registration_mdata = dcm_matrix_registration_seq[0]

	# MatrixSequence dataset: Transform type and 4x4 matrix (added to transform dataset)
	dcm_matrix_mdata = dcm_matrix_mdata or DcmDataset()
	transform_type = transform_type or DCM_MATRIX_RIGID	
	if not transform_type in DCM_MATRIX_TRANSFORM_SUPPORTED:
		raise ValueError('Unsupported transform type: %s' % transform_type)

	setattr(dcm_matrix_mdata, DCMHEADER_MATRIX_TRANSFORMATION_TYPE, transform_type)
	setattr(dcm_matrix_mdata, DCMHEADER_REFERENCE_FRAME_MATRIX, [v for v in txmatrix.flatten()])

	# Add to transform dataset
	matrix_sequence = getattr(dcm_mdata, DCMHEADER_MATRIX_REGISTRATION, None) or []
	matrix_sequence.append(dcm_matrix_mdata)
	setattr(dcm_matrix_registration_mdata, DCMHEADER_MATRIX_REGISTRATION, DcmSequence(m for m in matrix_sequence))

	# Add comment
	if transform_comment:
		setattr(dcm_matrix_registration_mdata, DCMHEADER_REFERENCE_FRAME_TRANFORM_COMMENT, transform_comment)

	# Add fiducial references
	if fiducials:
		setattr(dcm_mdata, DCMHEADER_MATRIX_USED_FIDUCIALS, DcmSequence(f for f in fiducials))

	# Add image references
	if ref_images:
		setattr(dcm_mdata, DCMHEADER_SR_REF_IMAGES_SEQ, DcmSequence(i for i in ref_images))

	# Add coded concepts
	if codes:
		setattr(dcm_matrix_registration_mdata, DCMHEADER_CONCEPT_CODE_SEQUENCE, DcmSequence(
			c for c in map(lambda d: srcode2dataset(d) if isinstance(d, (DcmCodedConcept, DcmCode)) else d, codes)))

	# Add Matrix Registration Sequence to transform dataset
	setattr(dcm_mdata, DCMHEADER_MATRIX_REGISTRATION_SEQUENCE, DcmSequence(dcm_matrix_registration_seq))

	return dcm_mdata



# DICOM-SR Helpers

DCMSR_PROCEDURE_REPORTED = DcmCode(
	value='121058', meaning='Procedure Reported', scheme_designator='DCM')


def srencode_procedure_reported(procedure, name:DcmCode=DCMSR_PROCEDURE_REPORTED, 
		relationship_type=RelationshipTypeValues.HAS_CONCEPT_MOD, sr_template=CodeContentItem):
	'''	DICOM-SR encode procedure reported value

		@returns CodeContentItem
	'''
	return sr_template(name=name, value=procedure, relationship_type=relationship_type)


def srencode_observation_context(context_uuid=None, device_name=None, sr_template=ObservationContext, **kwargs):
    ''' Encode the observation context for the Segaway/Score Keeper instance
    '''
    observer_device = ObserverContext(
        observer_type=dcmcodes.DCM.Device,
        observer_identifying_attributes=DeviceObserverIdentifyingAttributes(
        	uid=context_uuid or generate_uid(), name=device_name, **kwargs))

    return sr_template(observer_device_context=observer_device)



# DICOM-SR Spatial Data Structures

# 3D Coordinates

SONADOR_SCOORD3D = 'Sonador-Coord3D'
SONADOR_SCOORD3D_DESCRIPTION = '3D spatial point (x,y,z)'
DCMSR_SCOORD3D = DcmCode(SONADOR_SCOORD3D, DCMSR_SONADOR_SR.value, SONADOR_SCOORD3D_DESCRIPTION,
	scheme_version=SONADOR_SCHEME_VERSION_02)

SONADOR_SCOORD3D_ANTOMIC = '%s-Anatomic' % SONADOR_SCOORD3D
SONADOR_SCOORD3D_ANTOMIC_DESCRIPTION = '3D coordinate (x,y,z) describing an antomic landmark (or landmarks).'
DCMSR_SCOORD3D_ANATOMIC = DcmCode(SONADOR_SCOORD3D_ANTOMIC, DCMSR_SONADOR_SR.value, SONADOR_SCOORD3D_ANTOMIC_DESCRIPTION,
	scheme_version=SONADOR_SCHEME_VERSION_02)

SONADOR_SCOORD3D_LANDMARK = '%s-Landmark' % SONADOR_SCOORD3D
SONAODR_SCOORD3D_LANDMARK_DESCRIPTION = '3D coordinate (x,y,z) describing a non-anatomic landmark (or landmarks)'
DCMSR_SCOORD3D_LANDMARK = DcmCode(SONADOR_SCOORD3D_LANDMARK, DCMSR_SONADOR_SR.value, SONAODR_SCOORD3D_LANDMARK_DESCRIPTION,
	scheme_version=SONADOR_SCHEME_VERSION_02)

SONADOR_SCOORD3D_REGISTRATION = '%s-Registration' % SONADOR_SCOORD3D
SONADOR_SCOORD3D_REGISTRATION_DESCRIPTION = '3D coordinate describing a registration point (or points).'
DCMSR_SCOORD3D_REGISTRATION = DcmCode(SONADOR_SCOORD3D_REGISTRATION, DCMSR_SONADOR_SR.value, SONADOR_SCOORD3D_REGISTRATION_DESCRIPTION,
	scheme_version=SONADOR_SCHEME_VERSION_02)

SONADOR_SCOORD3D_TYPE_POINT_ELLIPSE = 'ELLIPSE'
SONADOR_SCOORD3D_TYPE_POINT_ELLIPSOID = 'ELLIPSOID'
SONADOR_SCOORD3D_TYPE_POINT_MULTIPOINT = 'MULTIPOINT'
SONADOR_SCOORD3D_TYPE_POINT_POINT = 'POINT'
SONADOR_SCOORD3D_TYPE_POINT_POLYGON = 'POLYGON'
SONADOR_SCOORD3D_TYPE_POINT_POLYLINE = 'POLYLINE'

SONADOR_SCOORD3D_TYPE_SUPPORTED = {
	SONADOR_SCOORD3D_TYPE_POINT_ELLIPSE: GraphicTypeValues3D.ELLIPSE,
	SONADOR_SCOORD3D_TYPE_POINT_ELLIPSOID: GraphicTypeValues3D.ELLIPSOID,
	SONADOR_SCOORD3D_TYPE_POINT_MULTIPOINT: GraphicTypeValues3D.MULTIPOINT,
	SONADOR_SCOORD3D_TYPE_POINT_POINT: GraphicTypeValues3D.POINT,
	SONADOR_SCOORD3D_TYPE_POINT_POLYGON: GraphicTypeValues3D.POLYGON,
	SONADOR_SCOORD3D_TYPE_POINT_POLYLINE: GraphicTypeValues3D.POLYLINE,
}


def points2array(pts: Union[ImageCoord,Sequence[ImageCoord],ImageCoordCollection,np.ndarray]) -> np.ndarray:
	'''	Convert the provided sequence of points to numpy array.
	'''
	# Image coordinate instance (single point)
	if isinstance(pts, ImageCoord):
		_p = np.array([*pts])

	# Sequence of points
	elif isinstance(pts, ImageCoordCollection) \
		or (isinstance(pts, (tuple, list)) and len(pts) and isinstance(pts[0], ImageCoord)):
		_p = np.array([[*p] for p in pts])

	# List/tuple encoding x,y,z data
	elif isinstance(pts, (tuple, list)) and len(pts) == 3 and isinstance(pts[0], numbers.Number):
		_p = np.array(pts)

	# Sequence of list/tuple encoding x,y,z data
	elif isinstance(pts, (tuple, list)) and len(pts) and all(
		[(len(p) == 3 and isinstance(p[0], numbers.Number)) for p in pts]):
		_p = np.array([p for p in pts])
	
	# NumPy array
	elif isinstance(pts, np.ndarray):
		_p = pts

	else:
		raise ValueError(
			'Unable to encode points, unsupported data type: "%s"' % type(pts))

	return _p


def srencode_coord3d(
		reference_frame: Union[str,DcmUid], 
		coord3d: Union[ImageCoord,Sequence[ImageCoord],ImageCoordCollection,ImageCoord,np.ndarray], 
		name: Union[DcmCode]=None, point_type: Union[GraphicTypeValues3D,str]=None, 
		sr_template=Scoord3DContentItem, relationship_type=RelationshipTypeValues.CONTAINS, **kwargs) -> Scoord3DContentItem:
	'''	DICOM-SR encode the provided 3D spatial coordinates (SCOORD3D)
		
		@input reference_frame (str or highdicom.uid.UID): UID of the coordinate system 
			(frame of reference) to use for the spatial data.
		@input coord3d (ImageCoord, iterable of ImageCoord, np.ndarray): spatial data to be encoded
		@input name (highdicom.sr.CodedConcent, default=SONADOR_SCOORD_3D): encoded point type/concept
			to use in SR document.
		@input point_type (str or highdicom.sr.GraphicTypeValue3D, 
			default=inferred point type based on structure of spatial_data): type of 3D point which the spatial data
			should be encoded to. If a point type is not explicitly declared, the method will attempt to determine
			the type of data (POINT or MULTIPOINT). For more specialized types of point data

		@returns highdicom.sr.Scoord3DContentItem
	'''
	# Add default name
	if name is None:
		name = DCMSR_SCOORD3D

	# Coerce spatial data to numpy array
	_p = points2array(coord3d)

	# Ensure that the spatial data array includes x,y,z dimensions.
	if (len(_p.shape) == 1 and _p.shape[0] != 3) \
		or (len(_p.shape) == 2 and _p.shape[1] != 3):
		_cidx = 1 if len(_p.shape) == 2 else 0
		raise ValueError(('Unable to encode spatial coordinates, incorrect number of columns: "%s". '
			+ 'Point instances must possess 3 columns of data (x,y,z).') % len(_p.shape[_cidx]))

	# Determine the type of point from the size of the spatial_data: POINT or MULTIPOINT.
	# For more specialized forms of point data (ELLIPSE, ELLIPSOID, POLYGON, POLYLINE),
	# the point type must be explicitly declared.
	if not point_type:
		point_type = GraphicTypeValues3D.MULTIPOINT if len(_p.shape) == 2 \
			else GraphicTypeValues3D.POINT if len(_p.shape) == 1 \
			else None		

	if not point_type in SONADOR_SCOORD3D_TYPE_SUPPORTED.keys() \
		and not point_type in SONADOR_SCOORD3D_TYPE_SUPPORTED.values():
		raise ValueError('Unsupported point type: %s' % point_type)

	# highdicom requires that individual points be formatted as a single row
	# in a (1,3) structure.
	if len(_p.shape) == 1: _p = _p.reshape((1,3))

	return sr_template(name=name, graphic_type=point_type, graphic_data=_p,
		frame_of_reference_uid=reference_frame, relationship_type=relationship_type, **kwargs)



# Measurements

SONADOR_LENGTH = dcmcodes.SCT.Length


def srencode_measurement(measurement:Measurement, name_default=SONADOR_LENGTH, sr_template=DcmMeasurement):
	'''	DICOM-SR encode the provided measurement
	'''
	mname = measurement.name or name_default

	if not mname or not isinstance(mname, (DcmCode, DcmCodedConcept)):
		raise ValueError(('Unable to DICOM-SR encode measurement instance. Invalid `name` value "%s". '
			'`name` is required property and must be a Code or `CodedConcept`.') % mname)

	return sr_template(name=mname, tracking_identifier=measurement.uid, algorithm_id=measurement.algorithm,
		referenced_images=measurement.ref_images, referenced_real_world_value_map=measurement.ref_value,
		**pick(measurement, ('value', 'unit', 'qualifier', 'derivation', 'finding_sites', 'method', 'properties')))


def srencode_finding(finding:Finding, sr_template=DcmQualitativeEvaluation):
	'''	DICOM-SR encode the provided measurement to the provided Template 
	'''
	return sr_template(finding.name, finding.finding)



# Reports

DCMSR_SONADOR_DATA_REPORT = DcmCode('Clinical-Report', DCMSR_SONADOR_SR.value,
	'Sonador Clinical Data Report', scheme_version=SONADOR_SCHEME_VERSION_02)



# Report Groups

DCMSR_REPORT_GROUP = DcmCode('125007', 'DCM', 'Report Group')
DCMSR_REPORT_METADATA = DcmCode('%s.Meta' % DCMSR_SONADOR_DATA_REPORT.value, DCMSR_SONADOR_SR.value,
	'Metadata associated with a clinical report', scheme_version=SONADOR_SCHEME_VERSION_02)
DCMSR_REPORT_VOLUME_MEAUREMENTS = DcmCode('%s.Volume-Measurements' % DCMSR_SONADOR_DATA_REPORT.value,
	DCMSR_SONADOR_SR.value, 'Imaging Volume Meaurements', scheme_version=SONADOR_SCHEME_VERSION_02)


def srencode_report_group(tracking_id:Union[TrackingIdentifier,str], 
		measurements:MeasurementCollection=None, findings:FindingCollection=None,
		sr_template=MeasurementsAndQualitativeEvaluations, **kwargs):
	'''	DICOM-SR encode the collection of measurements and findings using the provided template
	
		@input tracking_id (str or highdicom.sr.TrackingIdentifier): tracking identifier
			associated with the measurement group
		@input measurements (sonador.apisettings.MeasurementCollection, default=None): measurements
			to be encoded to the provided template.
		@input findings (sonador.apisettings.FindingCollection): findings to be encoded to the
			provided template.
	'''
	# Convert string value to DICOM-SR Tracking Identifier instance
	if isinstance(tracking_id, str):
		tracking_id = TrackingIdentifier(identifier=tracking_id)

	return sr_template(tracking_identifier=tracking_id, 
		measurements=[m.sr for m in measurements] if measurements else measurements, 
		qualitative_evaluations=[f.sr for f in findings] if findings else findings, **kwargs)



