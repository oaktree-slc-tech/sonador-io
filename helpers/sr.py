import numpy as np

from ..apisettings.base import ImageTransformMatrix, ImageCoordCollection, ImageCoord, \
	DCMHEADER_MATRIX_REGISTRATION_SEQUENCE, DCMHEADER_MATRIX_REGISTRATION, DCMHEADER_MATRIX_TRANSFORMATION_TYPE, \
	DCMHEADER_REFERENCE_FRAME_MATRIX, DCMHEADER_REFERENCE_FRAME_TRANFORM_COMMENT, \
	DCMHEADER_REFERENCE_FRAME, DCMHEADER_SR_VALUE_TYPE, DCMHEADER_SR_REF_REFERENCE_FRAME, \
	DCMHEADER_SR_GRAPHIC_DATA, DCMHEADER_SR_GRAPHIC_TYPE, \
	DCMSR_VALUE_TYPE_SCOORD3D, DCMSR_VALUE_TYPE_SCOORD, DCMSR_VALUE_TYPE_POINTS_SUPPORTED, \
	DCMHEADER_CONCEPT_CODE_SEQUENCE
from ..apisettings.sr import mtx4x4, srencode_procedure_reported, \
	dcm_encode_reference_frame_transform_matrix, points2array, srencode_coord3d, dataset2srcode, \
	SONADOR_SCOORD3D_TYPE_POINT_POINT, SONADOR_SCOORD3D_TYPE_POINT_MULTIPOINT, \
	SONADOR_SCOORD3D_TYPE_SUPPORTED


def dcm2txmatrix(dcm):
	'''	Parse the provided DICOM data to a transform matrix (reference frame)
	'''
	# Retrieve UID
	_uid = getattr(dcm, DCMHEADER_REFERENCE_FRAME, None)
	if not _uid:
		raise ValueError('Unable to parse DICOM reference frame. Invalid "%s" header.'
			% DCMHEADER_REFERENCE_FRAME)

	# Retrieve matrix sequence
	matrix_registration_sequence = getattr(dcm, DCMHEADER_MATRIX_REGISTRATION_SEQUENCE, None)
	if not matrix_registration_sequence:
		raise ValueError('Unable to parse DICOM reference frame. Invalid "%s" header.'
			% DCMHEADER_MATRIX_REGISTRATION_SEQUENCE)
	if len(matrix_registration_sequence) > 1:
		raise NotImplementedError(('Unable to parse DICOM reference frame. Sonador does not support '
			+ '"%s" with more than a single element.') % DCMHEADER_MATRIX_REGISTRATION_SEQUENCE)

	# Transform matrix metadata
	dcm_matrix_registration_mdata = matrix_registration_sequence[0]

	# Matrix sequence
	matrix_sequence = getattr(dcm_matrix_registration_mdata, DCMHEADER_MATRIX_REGISTRATION, [])
	if not matrix_sequence:
		raise ValueError('Unable to parse DICOM reference frame. Invalid "%s" header.'
			% (DCMHEADER_MATRIX_REGISTRATION))
	if len(matrix_sequence) > 1:
		raise NotImplementedError(('Unable to parse DICOM reference frame. Sonador does not support '
			+ '"%s" with more than a single element.') % DCMHEADER_MATRIX_REGISTRATION)

	dcm_matrix_mdata = matrix_sequence[0]
	_txmtx_type = getattr(dcm_matrix_mdata, DCMHEADER_MATRIX_TRANSFORMATION_TYPE)
	_txmtx = getattr(dcm_matrix_mdata, DCMHEADER_REFERENCE_FRAME_MATRIX)
	if not _txmtx:
		raise ValueError('Invalid "%s" value: "%s"' % (DCMHEADER_REFERENCE_FRAME_MATRIX, _txmtx))

	# Transform comment
	_comment = getattr(dcm_matrix_registration_mdata, DCMHEADER_REFERENCE_FRAME_TRANFORM_COMMENT, None)

	# Parse DICOM-SR codes
	if getattr(dcm_matrix_registration_mdata, DCMHEADER_CONCEPT_CODE_SEQUENCE, []):
		codes = [dataset2srcode(cc) for cc in getattr(dcm_matrix_registration_mdata, DCMHEADER_CONCEPT_CODE_SEQUENCE)]
	else: codes = None

	return ImageTransformMatrix(mtx4x4(np.array(_txmtx)), uid=_uid, 
		transform_type=_txmtx_type, comment=_comment, codes=codes)


def sr2points(dcmsr, reference_frame:ImageTransformMatrix=None, collection_class=ImageCoordCollection):
	'''	Parse the provided DICOM-SR data to a set of point instances

		@returns iterable of ImageCoord instances
	'''
	# Retrieve DICOM-SR value type (representation)
	_vr = getattr(dcmsr, DCMHEADER_SR_VALUE_TYPE, None)

	# Ensure that a value type is preent and supported
	if not _vr:
		raise ValueError('Unable to parse points from DICOM-SR instance, invalid "%s" header.' 
			% DCMHEADER_SR_VALUE_TYPE)
	if not _vr in DCMSR_VALUE_TYPE_POINTS_SUPPORTED:
		raise ValueError('Unable to parse pints from DICOM-SR instance, unsupport "%s": "%s"'
			% (DCMHEADER_SR_VALUE_TYPE, _vr))

	# Verify that the provided reference frame matches that reference frame UID in the dataset.
	_uid = getattr(dcmsr, DCMHEADER_SR_REF_REFERENCE_FRAME, None)
	if reference_frame and reference_frame.uid and reference_frame.uid != _uid:
		raise ValueError('Invalid reference frame. %s=%s does not match %s=%s.'
			% (DCMHEADER_SR_REF_REFERENCE_FRAME, _uid, DCMHEADER_REFERENCE_FRAME, reference_frame.uid))

	# Retrieve points
	_pts = getattr(dcmsr, DCMHEADER_SR_GRAPHIC_DATA, [])
	_ptype = getattr(dcmsr, DCMHEADER_SR_GRAPHIC_TYPE, None)
	if not _pts:
		raise ValueError('Unable to parse points from DICOM-SR instance, invalid "%s" header.'
			% DCMHEADER_SR_GRAPHIC_DATA)
	if not _ptype or not _ptype in SONADOR_SCOORD3D_TYPE_SUPPORTED:
		raise ValueError('Unable to parse points from DICOM-SR instance. Invalid "%s": "%s"'
			% (DCMHEADER_SR_GRAPHIC_TYPE, _ptype))

	# Parse DICOM-SR codes
	if getattr(dcmsr, DCMHEADER_CONCEPT_CODE_SEQUENCE, []):
		codes = [dataset2srcode(cc) for cc in getattr(dcmsr, DCMHEADER_CONCEPT_CODE_SEQUENCE)]
	else: codes = None

	# Parse points to ImageCoord instances
	if _vr == DCMSR_VALUE_TYPE_SCOORD:

		# SCOORD points are 2D
		return ImageCoordCollection(
			tuple(collection_class.model(p[0],p[1],0, reference_frame=reference_frame, codes=codes, point_type=_ptype)
				for p in np.array(_pts).reshape(-1,2)),
			reference_frame=reference_frame, codes=codes, point_type=_ptype)

	elif _vr == DCMSR_VALUE_TYPE_SCOORD3D:

		# SCOORD points are 3D
		return ImageCoordCollection(
			tuple(collection_class.model(*p, reference_frame=reference_frame, codes=codes, point_type=_ptype)
				for p in np.array(_pts).reshape(-1,3)),
			reference_frame=reference_frame, codes=codes, point_type=_ptype)

	raise NotImplementedError('Unsupported "%s" and "%s". %s=%s %s=%s'
		% (DCMHEADER_SR_VALUE_TYPE, DCMHEADER_SR_GRAPHIC_TYPE, DCMHEADER_SR_VALUE_TYPE, _vr, DCMHEADER_SR_GRAPHIC_TYPE, _ptype))