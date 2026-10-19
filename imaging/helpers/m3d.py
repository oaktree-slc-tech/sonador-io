'''	Coordinate system and units of medical 3D (M3D) models.

	The STL format carries neither units nor a frame of reference. The DICOM encapsulation
	provides both (PS3.3 A.85, Encapsulated STL IOD):

	*	Frame of Reference UID (0020,0052), Frame of Reference module (C.7.4.1): identifies the
		origin and axes implicit in the model data. Instances that share a Frame of Reference UID are
		spatially related, so a model derived from imaging and placed in the coordinate system of its
		source images carries the Frame of Reference UID of those images: the patient-based
		coordinate system of C.7.6.2.1.1 (x towards the patient's left, y towards the posterior, z
		towards the head, in millimetres).
	*	Measurement Units Code Sequence (0040,08EA), Manufacturing 3D Model module (C.35.1): units of
		distance of the coordinate system of the encapsulated model file (CID 7063, UCUM m/cm/mm/um).

	`dcm_m3d_frame_from_headers` resolves the frame of an M3D instance from its own headers first,
	with a fallback to the headers of the series it references (ReferencedSeriesSequence, the source
	imaging and segmentation series recorded by the Sonador encoders): a model that does not declare
	a frame of reference but is derived from a series that has one is taken to lie in that frame,
	in millimetres. Headers may be given as pydicom datasets or as dictionaries of (Orthanc
	simplified) tags, so that the frame can be resolved from the tags index without retrieving files.

	@fn dcm_header_value: read a header from a dataset or tag dictionary
	@fn dcm_referenced_series_uids: series referenced by an instance (ReferencedSeriesSequence)
	@fn dcm_m3d_units: units declared by the Measurement Units Code Sequence
	@fn dcm_encode_m3d_units: encode the Measurement Units Code Sequence
	@fn dcm_m3d_frame_from_headers: resolve the frame of reference and units of a model from headers
	@fn dcm_m3d_referenced_series: series of the parent study referenced by a model instance
	@fn dcm_m3d_frame: resolve the frame of a model instance, with the fallback to its referenced series
'''
from collections import namedtuple
from collections.abc import Mapping

from pydicom.dataset import Dataset
from pydicom.sequence import Sequence

from ...apisettings import DCMHEADER_FRAME_OF_REFERENCE_UID, DCMHEADER_POSITION_REFERENCE_INDICATOR, \
	DCMHEADER_PATIENT_POSITION, DCMHEADER_MEASUREMENT_UNITS_CODE_SEQUENCE, DCMHEADER_SERIES_INSTANCE_UID, \
	DCMHEADER_SR_REF_SERIES_SEQ, DCMHEADER_CODE_VALUE, DCMHEADER_CODING_SCHEME_DESIGNATOR, DCMHEADER_CODE_MEANING
from ...apisettings.m3d import M3D_UNIT_SCALES, M3D_UNIT_DEFAULT, M3D_UNIT_MM, M3D_UNIT_M, M3D_UNIT_CODING_SCHEME, \
	M3D_COORDINATE_SYSTEM_LPS, M3D_COORDINATE_SYSTEM_RAS, M3D_COORDINATE_SYSTEMS, \
	M3D_FRAME_SOURCE_INSTANCE, M3D_FRAME_SOURCE_REFERENCE, M3D_FRAME_SOURCE_DEFAULT, M3D_DERIVED_MODALITIES


def dcm_header_value(headers, name, default=None):
	'''	Retrieve a header value from a pydicom dataset or from a dictionary of tags (e.g. the
		simplified tags of an Orthanc instance, or the main DICOM tags of a series). Empty values
		are returned as the default.

		@input headers (pydicom.dataset.Dataset or Mapping): headers
		@input name (str): DICOM keyword of the header
		@input default: value returned when the header is absent or empty

		@returns header value or default
	'''
	if headers is None:
		return default

	value = headers.get(name, None) if isinstance(headers, Mapping) else getattr(headers, name, None)
	if value is None or (isinstance(value, str) and not value.strip()):
		return default

	return value


def dcm_header_uid(headers, name):
	'''	Retrieve a UID header as a string (stripped), or None if absent or empty
	'''
	value = dcm_header_value(headers, name)
	return str(value).strip() or None if value is not None else None


def dcm_referenced_series_uids(headers):
	'''	Series Instance UIDs of the series referenced by the headers (ReferencedSeriesSequence), in
		the order of the sequence.

		@input headers (pydicom.dataset.Dataset or Mapping): headers of the instance
		@returns list of str
	'''
	uids = []
	for item in dcm_header_value(headers, DCMHEADER_SR_REF_SERIES_SEQ) or []:
		uid = dcm_header_uid(item, DCMHEADER_SERIES_INSTANCE_UID)
		if uid and uid not in uids:
			uids.append(uid)

	return uids


def dcm_m3d_units(headers, default=None):
	'''	Units of distance of the model coordinate system, as declared by the Measurement Units
		Code Sequence (0040,08EA) of the headers.

		@input headers (pydicom.dataset.Dataset or Mapping): headers of the M3D instance
		@input default: value returned when the sequence is absent or empty

		@returns UCUM unit code (one of M3D_UNIT_SCALES) or default
		@raises ValueError: the sequence declares a unit outside of CID 7063 (Model Scale Unit)
	'''
	sequence = dcm_header_value(headers, DCMHEADER_MEASUREMENT_UNITS_CODE_SEQUENCE)
	if not sequence:
		return default

	item = sequence[0]
	code = dcm_header_value(item, DCMHEADER_CODE_VALUE)
	scheme = dcm_header_value(item, DCMHEADER_CODING_SCHEME_DESIGNATOR)
	code = str(code).strip() if code is not None else None
	scheme = str(scheme).strip() if scheme is not None else None

	if code not in M3D_UNIT_SCALES or (scheme and scheme != M3D_UNIT_CODING_SCHEME):
		raise ValueError('Unsupported model scale unit "%s" (coding scheme "%s"). Supported units (CID 7063, %s): %s.' % (
			code, scheme, M3D_UNIT_CODING_SCHEME, ', '.join(M3D_UNIT_SCALES)))

	return code


def dcm_encode_m3d_units(units=M3D_UNIT_DEFAULT):
	'''	Create the Measurement Units Code Sequence (0040,08EA) declaring the units of the model
		coordinate system: a single item coded with CID 7063 (UCUM).

		@input units (str, default=M3D_UNIT_DEFAULT): UCUM unit code, one of M3D_UNIT_SCALES

		@returns pydicom.sequence.Sequence
	'''
	if units not in M3D_UNIT_SCALES:
		raise ValueError('Unsupported model scale unit "%s". Supported units (CID 7063, %s): %s.' % (
			units, M3D_UNIT_CODING_SCHEME, ', '.join(M3D_UNIT_SCALES)))

	item = Dataset()
	setattr(item, DCMHEADER_CODE_VALUE, units)
	setattr(item, DCMHEADER_CODING_SCHEME_DESIGNATOR, M3D_UNIT_CODING_SCHEME)
	setattr(item, DCMHEADER_CODE_MEANING, units)

	return Sequence([item])


class M3DFrame(namedtuple('M3DFrame', ('frame_of_reference_uid', 'units', 'scale', 'coordinate_system',
		'position_reference_indicator', 'patient_position', 'frame_source', 'units_source', 'source_series_uid'))):
	'''	Frame of reference and units of an M3D model.

		@attr frame_of_reference_uid (str or None): Frame of Reference UID the model coordinates
			are expressed in. None when neither the model nor a referenced series declares one.
		@attr units (str): UCUM code of the unit of distance of the model coordinates (CID 7063)
		@attr scale (float): length of one model unit in metres
		@attr coordinate_system (str): axis convention of the frame (M3D_COORDINATE_SYSTEM_LPS for
			the DICOM patient-based coordinate system)
		@attr position_reference_indicator (str or None): Position Reference Indicator of the frame
		@attr patient_position (str or None): Patient Position of the source imaging, when known
		@attr frame_source (str): where the frame of reference was found: M3D_FRAME_SOURCE_INSTANCE
			(the model's own headers), M3D_FRAME_SOURCE_REFERENCE (a referenced series), or
			M3D_FRAME_SOURCE_DEFAULT (none declared, patient coordinate system assumed)
		@attr units_source (str): where the units were found (same values)
		@attr source_series_uid (str or None): Series Instance UID of the referenced series that
			provided the frame of reference
	'''
	__slots__ = ()

	@property
	def is_default(self):
		'''	True if no frame of reference was declared by the model or a referenced series
		'''
		return self.frame_source == M3D_FRAME_SOURCE_DEFAULT

	def scale_to(self, units=M3D_UNIT_M):
		'''	Factor that converts model coordinates to the provided unit

			@input units (str, default=M3D_UNIT_M): target UCUM unit code, one of M3D_UNIT_SCALES
			@returns float
		'''
		if units not in M3D_UNIT_SCALES:
			raise ValueError('Unsupported unit "%s". Supported units: %s.' % (units, ', '.join(M3D_UNIT_SCALES)))

		return self.scale / M3D_UNIT_SCALES[units]

	def axis_signs(self, coordinate_system=M3D_COORDINATE_SYSTEM_LPS):
		'''	Signs to apply to the (x, y, z) model coordinates to express them in the requested axis
			convention. LPS and RAS differ by the sign of x and y.

			@input coordinate_system (str, default=M3D_COORDINATE_SYSTEM_LPS): target convention
			@returns tuple of three ints (1 or -1)
		'''
		if coordinate_system not in M3D_COORDINATE_SYSTEMS:
			raise ValueError('Unsupported coordinate system "%s". Supported: %s.' % (
				coordinate_system, ', '.join(M3D_COORDINATE_SYSTEMS)))

		return (1, 1, 1) if coordinate_system == self.coordinate_system else (-1, -1, 1)

	def describe(self):
		'''	Short description of the frame for logs and user interfaces, e.g.
			"mm, LPS, frame of reference 1.2.3 (from referenced series 1.2.4)"
		'''
		if self.is_default:
			return '%s (assumed), %s (assumed), no frame of reference' % (self.units, self.coordinate_system)

		origin = 'declared by the model' if self.frame_source == M3D_FRAME_SOURCE_INSTANCE \
			else 'from referenced series %s' % self.source_series_uid
		units = self.units if self.units_source == M3D_FRAME_SOURCE_INSTANCE else '%s (%s)' % (
			self.units, 'patient coordinate system' if self.units_source == M3D_FRAME_SOURCE_REFERENCE else 'assumed')

		return '%s, %s, frame of reference %s (%s)' % (units, self.coordinate_system, self.frame_of_reference_uid, origin)


def dcm_m3d_frame_from_headers(headers, references=(), default_units=M3D_UNIT_DEFAULT,
		coordinate_system=M3D_COORDINATE_SYSTEM_LPS):
	'''	Resolve the frame of reference and units of an M3D model from its headers, with a fallback
		to the headers of the series it references.

		1.	Units: the Measurement Units Code Sequence of the model headers.
		2.	Frame of reference: the Frame of Reference UID of the model headers.
		3.	If the model declares no frame of reference, the first referenced series that declares
			one provides it (together with its Position Reference Indicator and Patient Position).
			A model placed in the frame of its source images lies in the patient-based coordinate
			system, so its units are millimetres unless the model declared otherwise.
		4.	Without any frame of reference, the units default to `default_units`.

		@input headers (pydicom.dataset.Dataset or Mapping): headers of the M3D instance
		@input references (iterable of (series uid, headers)): headers of the referenced series,
			in order of preference. Evaluated lazily, so a generator may defer retrieval.
		@input default_units (str, default=M3D_UNIT_DEFAULT): units assumed when none are declared
		@input coordinate_system (str, default=M3D_COORDINATE_SYSTEM_LPS): axis convention of the
			frame of reference

		@returns M3DFrame
	'''
	units = dcm_m3d_units(headers)
	units_source = M3D_FRAME_SOURCE_INSTANCE if units else None

	frame_uid = dcm_header_uid(headers, DCMHEADER_FRAME_OF_REFERENCE_UID)
	frame_source = M3D_FRAME_SOURCE_INSTANCE if frame_uid else None
	reference_indicator = dcm_header_value(headers, DCMHEADER_POSITION_REFERENCE_INDICATOR)
	patient_position = dcm_header_value(headers, DCMHEADER_PATIENT_POSITION)
	source_series_uid = None

	# Fallback: frame of reference of the source imaging
	if not frame_uid:
		for series_uid, series_headers in references:
			series_frame_uid = dcm_header_uid(series_headers, DCMHEADER_FRAME_OF_REFERENCE_UID)
			if not series_frame_uid:
				continue

			frame_uid, frame_source, source_series_uid = series_frame_uid, M3D_FRAME_SOURCE_REFERENCE, series_uid
			reference_indicator = reference_indicator or dcm_header_value(series_headers, DCMHEADER_POSITION_REFERENCE_INDICATOR)
			patient_position = patient_position or dcm_header_value(series_headers, DCMHEADER_PATIENT_POSITION)
			if units is None:
				units, units_source = M3D_UNIT_MM, M3D_FRAME_SOURCE_REFERENCE
			break

	# No frame of reference: assume the default units
	if not frame_uid:
		frame_source = M3D_FRAME_SOURCE_DEFAULT
	if units is None:
		units, units_source = default_units, M3D_FRAME_SOURCE_DEFAULT

	if units not in M3D_UNIT_SCALES:
		raise ValueError('Unsupported unit "%s". Supported units: %s.' % (units, ', '.join(M3D_UNIT_SCALES)))

	return M3DFrame(frame_uid, units, M3D_UNIT_SCALES[units], coordinate_system,
		str(reference_indicator).strip() if reference_indicator is not None else None,
		str(patient_position).strip() if patient_position is not None else None,
		frame_source, units_source, source_series_uid)


def dcm_m3d_referenced_series(instance, headers=None):
	'''	Series of the parent study referenced by a model instance (ReferencedSeriesSequence): the
		imaging and segmentation series the model was derived from. Image series are listed before
		derived series (SEG, SR, ...), as the preferred source of a frame of reference.

		@input instance (sonador.imaging.orthanc.base.DcmInstanceCoreResource): model instance (any
			instance of an M3D series, whichever model class represents it)
		@input headers (pydicom.dataset.Dataset or Mapping, default=instance tags): headers of the
			model from which the references are read

		@returns list of sonador.imaging.orthanc.base.ImagingSeries
	'''
	reference_uids = dcm_referenced_series_uids(instance.tags if headers is None else headers)
	series = [sx for sx in instance.model_study.series_collection if sx.series_uid in reference_uids] \
		if reference_uids else []

	return sorted(series, key=lambda sx: sx.modality in M3D_DERIVED_MODALITIES)


def dcm_m3d_source_frame_headers(instance, headers=None):
	'''	Headers of the referenced series from which a model may inherit a frame of reference, as
		(series UID, headers) pairs in order of preference. The main DICOM tags of a series are used
		when they carry the Frame of Reference UID; otherwise the tags of its first instance are
		retrieved. Lazy: series are only consulted while a frame is still missing.

		@input instance: model instance (see dcm_m3d_referenced_series)
		@input headers (pydicom.dataset.Dataset or Mapping, default=instance tags): headers of the model

		@returns generator of (str, dict)
	'''
	for sx in dcm_m3d_referenced_series(instance, headers):
		if dcm_header_uid(sx.dicomdata, DCMHEADER_FRAME_OF_REFERENCE_UID):
			yield sx.series_uid, sx.dicomdata
		elif len(sx.instances_collection):
			yield sx.series_uid, sx.dcm0.tags


def dcm_m3d_frame(instance, headers=None, fallback=True, **kwargs):
	'''	Resolve the frame of reference and units of a model instance (dcm_m3d_frame_from_headers):
		the model headers first, with a fallback to the headers of the series it references.

		@input instance: model instance (see dcm_m3d_referenced_series)
		@input headers (pydicom.dataset.Dataset or Mapping, default=instance tags): headers of the
			model. A retrieved DICOM file (`dcmfile`) may be given so that no tags request is made.
		@input fallback (bool, default=True): consult the referenced series when the model does not
			declare a frame of reference
		@input kwargs: forwarded to dcm_m3d_frame_from_headers (default_units, coordinate_system)

		@returns M3DFrame
	'''
	return dcm_m3d_frame_from_headers(instance.tags if headers is None else headers,
		references=dcm_m3d_source_frame_headers(instance, headers) if fallback else (), **kwargs)
