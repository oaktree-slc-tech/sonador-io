'''	Offline tests of the coordinate system and units of M3D models: the frame helpers, the
	M3D instance model (fallback to the referenced series), and the STL encoder.

	Run from the directory containing the `sonador` package: python -m unittest sonador.test.m3d
'''
import unittest
from io import BytesIO
from types import SimpleNamespace
from unittest import mock

from pydicom import dcmread
from pydicom.dataset import Dataset
from pydicom.sequence import Sequence

from ..apisettings import DCMHEADER_FRAME_OF_REFERENCE_UID, DCMHEADER_MEASUREMENT_UNITS_CODE_SEQUENCE, \
	DCMHEADER_POSITION_REFERENCE_INDICATOR, DCMHEADER_PATIENT_POSITION, DCMHEADER_SR_REF_SERIES_SEQ, \
	DCMHEADER_SERIES_INSTANCE_UID, DCMHEADER_CODE_VALUE, DCMHEADER_CODING_SCHEME_DESIGNATOR, DCMHEADER_CODE_MEANING, \
	DCMHEADER_ENCAPSULATED_DOCUMENT, DCMHEADER_ENCAPSULATED_DOCUMENT_MIMETYPE, DCMHEADER_MODALITY, DCMHEADER_SOP_CLASS_UID
from ..apisettings.media import DCMEDIA_ENCAPSULATED_STL_SOP_CLASS, DCMEDIA_STL_MIMETPYE
from ..apisettings.m3d import M3D_UNIT_MM, M3D_UNIT_CM, M3D_UNIT_M, M3D_UNIT_UM, M3D_UNIT_SCALES, M3D_UNIT_CODING_SCHEME, \
	M3D_COORDINATE_SYSTEM_LPS, M3D_COORDINATE_SYSTEM_RAS, \
	M3D_FRAME_SOURCE_INSTANCE, M3D_FRAME_SOURCE_REFERENCE, M3D_FRAME_SOURCE_DEFAULT, M3D_DCM_SERIES_TAGS
from ..imaging.helpers.m3d import M3DFrame, dcm_header_value, dcm_header_uid, dcm_referenced_series_uids, \
	dcm_m3d_units, dcm_encode_m3d_units, dcm_m3d_frame_from_headers, dcm_m3d_frame
from ..imaging.orthanc.base import DcmInstance
from ..imaging.orthanc.m3d import DcmM3DInstance
from ..tasks.media import dcm_encode_stl


FOR_MODEL, FOR_CT, SX_CT, SX_SEG = '1.2.826.0.1.3680043.8.498.1', '1.2.826.0.1.3680043.8.498.2', '1.2.3.ct', '1.2.3.seg'


def units_item(code, scheme=M3D_UNIT_CODING_SCHEME):
	'''	Item of the Measurement Units Code Sequence as a dictionary of simplified tags
	'''
	return {DCMHEADER_CODE_VALUE: code, DCMHEADER_CODING_SCHEME_DESIGNATOR: scheme, DCMHEADER_CODE_MEANING: code}


def dataset_from_tags(tags):
	'''	pydicom dataset equivalent of a dictionary of simplified tags (sequences as lists of dicts)
	'''
	ds = Dataset()
	for key, value in tags.items():
		setattr(ds, key, Sequence([dataset_from_tags(item) for item in value]) if isinstance(value, list) else value)
	return ds


def model_tags(frame_uid=None, units=None, references=(SX_SEG, SX_CT), **extra):
	tags = {DCMHEADER_SR_REF_SERIES_SEQ: [{DCMHEADER_SERIES_INSTANCE_UID: uid} for uid in references]}
	if frame_uid:
		tags[DCMHEADER_FRAME_OF_REFERENCE_UID] = frame_uid
	if units:
		tags[DCMHEADER_MEASUREMENT_UNITS_CODE_SEQUENCE] = [units_item(units)]
	tags.update(extra)
	return tags


class HeaderAccessTest(unittest.TestCase):
	'''	Headers are read alike from tag dictionaries and pydicom datasets
	'''
	def test_header_value(self):
		for headers in (model_tags(FOR_MODEL, M3D_UNIT_CM), dataset_from_tags(model_tags(FOR_MODEL, M3D_UNIT_CM))):
			self.assertEqual(dcm_header_uid(headers, DCMHEADER_FRAME_OF_REFERENCE_UID), FOR_MODEL)
			self.assertIsNone(dcm_header_value(headers, DCMHEADER_PATIENT_POSITION))
			self.assertEqual(dcm_header_value(headers, DCMHEADER_PATIENT_POSITION, 'HFS'), 'HFS')
			self.assertEqual(dcm_referenced_series_uids(headers), [SX_SEG, SX_CT])
			self.assertEqual(dcm_m3d_units(headers), M3D_UNIT_CM)

		# Empty values count as absent
		self.assertIsNone(dcm_header_uid({DCMHEADER_FRAME_OF_REFERENCE_UID: ' '}, DCMHEADER_FRAME_OF_REFERENCE_UID))
		self.assertIsNone(dcm_header_value(None, DCMHEADER_FRAME_OF_REFERENCE_UID))
		self.assertEqual(dcm_referenced_series_uids({}), [])

	def test_units(self):
		self.assertIsNone(dcm_m3d_units({}))
		self.assertEqual(dcm_m3d_units({}, default=M3D_UNIT_MM), M3D_UNIT_MM)
		self.assertIsNone(dcm_m3d_units({DCMHEADER_MEASUREMENT_UNITS_CODE_SEQUENCE: []}))
		for code in M3D_UNIT_SCALES:
			self.assertEqual(dcm_m3d_units({DCMHEADER_MEASUREMENT_UNITS_CODE_SEQUENCE: [units_item(code)]}), code)

		# Units outside CID 7063, or of another coding scheme, are rejected rather than guessed
		with self.assertRaises(ValueError):
			dcm_m3d_units({DCMHEADER_MEASUREMENT_UNITS_CODE_SEQUENCE: [units_item('in')]})
		with self.assertRaises(ValueError):
			dcm_m3d_units({DCMHEADER_MEASUREMENT_UNITS_CODE_SEQUENCE: [units_item(M3D_UNIT_MM, scheme='SCT')]})

	def test_encode_units(self):
		seq = dcm_encode_m3d_units(M3D_UNIT_UM)
		self.assertEqual(len(seq), 1)
		self.assertEqual((seq[0].CodeValue, seq[0].CodingSchemeDesignator, seq[0].CodeMeaning), ('um', 'UCUM', 'um'))
		self.assertEqual(dcm_m3d_units({DCMHEADER_MEASUREMENT_UNITS_CODE_SEQUENCE: seq}), M3D_UNIT_UM)
		with self.assertRaises(ValueError):
			dcm_encode_m3d_units('inch')


class FrameResolutionTest(unittest.TestCase):
	'''	Frame of reference and units: model headers first, then the referenced series, then defaults
	'''
	def test_declared_by_model(self):
		frame = dcm_m3d_frame_from_headers(model_tags(FOR_MODEL, M3D_UNIT_CM, PositionReferenceIndicator='STERNUM'),
			references=[(SX_CT, {DCMHEADER_FRAME_OF_REFERENCE_UID: FOR_CT})])
		self.assertEqual(frame.frame_of_reference_uid, FOR_MODEL)
		self.assertEqual((frame.units, frame.scale), (M3D_UNIT_CM, 0.01))
		self.assertEqual((frame.frame_source, frame.units_source), (M3D_FRAME_SOURCE_INSTANCE, M3D_FRAME_SOURCE_INSTANCE))
		self.assertEqual(frame.position_reference_indicator, 'STERNUM')
		self.assertIsNone(frame.source_series_uid)
		self.assertEqual(frame.coordinate_system, M3D_COORDINATE_SYSTEM_LPS)
		self.assertFalse(frame.is_default)
		self.assertEqual(frame.describe(), 'cm, LPS, frame of reference %s (declared by the model)' % FOR_MODEL)

		# A model that declares a frame of reference but no units lies in the patient coordinate system
		frame = dcm_m3d_frame_from_headers(dataset_from_tags(model_tags(FOR_MODEL)))
		self.assertEqual((frame.units, frame.units_source), (M3D_UNIT_MM, M3D_FRAME_SOURCE_DEFAULT))
		self.assertEqual(frame.frame_source, M3D_FRAME_SOURCE_INSTANCE)

	def test_inherited_from_reference(self):
		'''	No frame of reference on the model: the first referenced series that declares one
			provides it, and the units are the millimetres of the patient coordinate system
		'''
		consulted = []
		def references():
			for uid, headers in ((SX_SEG, {}), (SX_CT, {DCMHEADER_FRAME_OF_REFERENCE_UID: FOR_CT, DCMHEADER_PATIENT_POSITION: 'HFS'}),
					('1.2.3.other', {DCMHEADER_FRAME_OF_REFERENCE_UID: '9.9'})):
				consulted.append(uid)
				yield uid, headers

		frame = dcm_m3d_frame_from_headers(model_tags(), references=references())
		self.assertEqual(frame.frame_of_reference_uid, FOR_CT)
		self.assertEqual((frame.frame_source, frame.source_series_uid), (M3D_FRAME_SOURCE_REFERENCE, SX_CT))
		self.assertEqual((frame.units, frame.units_source, frame.scale), (M3D_UNIT_MM, M3D_FRAME_SOURCE_REFERENCE, 0.001))
		self.assertEqual(frame.patient_position, 'HFS')
		self.assertFalse(frame.is_default)
		# Lazy: series after the first match are not consulted
		self.assertEqual(consulted, [SX_SEG, SX_CT])
		self.assertEqual(frame.describe(),
			'mm (patient coordinate system), LPS, frame of reference %s (from referenced series %s)' % (FOR_CT, SX_CT))

		# Units declared by the model are kept when the frame is inherited
		frame = dcm_m3d_frame_from_headers(model_tags(units=M3D_UNIT_M),
			references=[(SX_CT, dataset_from_tags({DCMHEADER_FRAME_OF_REFERENCE_UID: FOR_CT}))])
		self.assertEqual((frame.units, frame.units_source), (M3D_UNIT_M, M3D_FRAME_SOURCE_INSTANCE))
		self.assertEqual(frame.frame_source, M3D_FRAME_SOURCE_REFERENCE)

	def test_default(self):
		frame = dcm_m3d_frame_from_headers(model_tags(), references=[(SX_CT, {}), (SX_SEG, {})])
		self.assertIsNone(frame.frame_of_reference_uid)
		self.assertEqual((frame.units, frame.scale), (M3D_UNIT_MM, 0.001))
		self.assertEqual((frame.frame_source, frame.units_source), (M3D_FRAME_SOURCE_DEFAULT, M3D_FRAME_SOURCE_DEFAULT))
		self.assertTrue(frame.is_default)
		self.assertEqual(frame.describe(), 'mm (assumed), LPS (assumed), no frame of reference')

		frame = dcm_m3d_frame_from_headers({}, default_units=M3D_UNIT_M)
		self.assertEqual((frame.units, frame.scale), (M3D_UNIT_M, 1.0))
		with self.assertRaises(ValueError):
			dcm_m3d_frame_from_headers({}, default_units='ft')

	def test_conversions(self):
		frame = dcm_m3d_frame_from_headers(model_tags(FOR_MODEL, M3D_UNIT_CM))
		self.assertAlmostEqual(frame.scale_to(M3D_UNIT_M), 0.01)
		self.assertAlmostEqual(frame.scale_to(M3D_UNIT_MM), 10.0)
		self.assertAlmostEqual(frame.scale_to(M3D_UNIT_UM), 10000.0)
		self.assertEqual(frame.axis_signs(M3D_COORDINATE_SYSTEM_LPS), (1, 1, 1))
		self.assertEqual(frame.axis_signs(M3D_COORDINATE_SYSTEM_RAS), (-1, -1, 1))
		with self.assertRaises(ValueError):
			frame.scale_to('ft')
		with self.assertRaises(ValueError):
			frame.axis_signs('LAS')


def series_stub(uid, modality, dicomdata=None, instance_tags=None):
	'''	Stand-in for an ImagingSeries of the parent study: main DICOM tags, and the tags of the first
		instance (retrieved on demand, counted in `tag_requests`)
	'''
	sx = SimpleNamespace(series_uid=uid, modality=modality, dicomdata=dict(dicomdata or {}), tag_requests=0)
	sx.dicomdata.setdefault(DCMHEADER_SERIES_INSTANCE_UID, uid)
	sx.dicomdata.setdefault(DCMHEADER_MODALITY, modality)

	class Dcm0:
		@property
		def tags(self):
			sx.tag_requests += 1
			return dict(instance_tags or {})

	sx.instances_collection = [Dcm0()] if instance_tags is not None else []
	sx.dcm0 = sx.instances_collection[0] if sx.instances_collection else None
	return sx


class M3DInstanceFrameTest(unittest.TestCase):
	'''	DcmM3DInstance: referenced series of the parent study, and the frame fallback
	'''
	def make_instance(self, tags, study_series, model_class=DcmM3DInstance):
		'''	Model instance of a series of a study with the provided series; the instance tags are
			pre-loaded (no server requests)
		'''
		study = SimpleNamespace(series_collection=study_series)
		dcm = model_class(None, {'ID': 'm3d-i1', 'MainDicomTags': {}}, series=SimpleNamespace(parent=study))
		dcm._tags = tags
		return dcm

	def test_referenced_series_prefers_images(self):
		seg = series_stub(SX_SEG, 'SEG', instance_tags={DCMHEADER_FRAME_OF_REFERENCE_UID: FOR_CT})
		ct = series_stub(SX_CT, 'CT', instance_tags={DCMHEADER_FRAME_OF_REFERENCE_UID: FOR_CT})
		other = series_stub('1.2.3.mr', 'MR', instance_tags={DCMHEADER_FRAME_OF_REFERENCE_UID: '9.9'})
		dcm = self.make_instance(model_tags(references=(SX_SEG, SX_CT)), [other, seg, ct])

		self.assertEqual([sx.series_uid for sx in dcm.source_series], [SX_CT, SX_SEG])
		self.assertIs(dcm.source_series, dcm.source_series)

		# References read from provided headers (e.g. a retrieved file) instead of the instance tags
		self.assertEqual([sx.series_uid for sx in dcm.referenced_series(dataset_from_tags(model_tags(references=(SX_SEG,))))],
			[SX_SEG])

	def test_referenced_series_keep_reference_order(self):
		'''	Two referenced image series in different frames of reference: the first reference of the
			model provides the frame whichever order the study enumerates its series in
		'''
		ct_a = series_stub('1.2.3.ct.a', 'CT', instance_tags={DCMHEADER_FRAME_OF_REFERENCE_UID: '1.2.3.for.a'})
		ct_b = series_stub('1.2.3.ct.b', 'CT', instance_tags={DCMHEADER_FRAME_OF_REFERENCE_UID: '1.2.3.for.b'})
		seg = series_stub(SX_SEG, 'SEG', instance_tags={DCMHEADER_FRAME_OF_REFERENCE_UID: '1.2.3.for.b'})
		tags = model_tags(references=(SX_SEG, ct_a.series_uid, ct_b.series_uid, ct_a.series_uid))

		for study_series in ([ct_a, ct_b, seg], [ct_b, ct_a, seg], [seg, ct_b, ct_a]):
			dcm = self.make_instance(tags, study_series)
			self.assertEqual([sx.series_uid for sx in dcm.source_series], [ct_a.series_uid, ct_b.series_uid, SX_SEG])
			frame = dcm.frame
			self.assertEqual((frame.frame_of_reference_uid, frame.source_series_uid), ('1.2.3.for.a', ct_a.series_uid))
			self.assertEqual((ct_a.tag_requests, ct_b.tag_requests, seg.tag_requests), (1, 0, 0))
			ct_a.tag_requests = 0

	def test_frame_from_source_series_instance_tags(self):
		'''	The series main tags carry no frame of reference (Orthanc's default main tags do not), so
			the tags of the first instance of the preferred series are retrieved; the other series is
			not consulted
		'''
		seg = series_stub(SX_SEG, 'SEG', instance_tags={DCMHEADER_FRAME_OF_REFERENCE_UID: FOR_CT})
		ct = series_stub(SX_CT, 'CT', instance_tags={DCMHEADER_FRAME_OF_REFERENCE_UID: FOR_CT, DCMHEADER_PATIENT_POSITION: 'HFS'})
		dcm = self.make_instance(model_tags(), [seg, ct])

		frame = dcm.frame
		self.assertEqual((frame.frame_of_reference_uid, frame.source_series_uid), (FOR_CT, SX_CT))
		self.assertEqual((frame.units, frame.frame_source, frame.patient_position), (M3D_UNIT_MM, M3D_FRAME_SOURCE_REFERENCE, 'HFS'))
		self.assertEqual((ct.tag_requests, seg.tag_requests), (1, 0))
		self.assertIs(dcm.frame, frame)

	def test_frame_from_source_series_main_tags(self):
		'''	Main tags that carry the frame of reference are used without retrieving instance tags
		'''
		ct = series_stub(SX_CT, 'CT', dicomdata={DCMHEADER_FRAME_OF_REFERENCE_UID: FOR_CT}, instance_tags={})
		dcm = self.make_instance(model_tags(references=(SX_CT,)), [ct])

		frame = dcm.resolve_frame()
		self.assertEqual((frame.frame_of_reference_uid, frame.frame_source), (FOR_CT, M3D_FRAME_SOURCE_REFERENCE))
		self.assertEqual(ct.tag_requests, 0)

	def test_frame_from_model_or_default(self):
		ct = series_stub(SX_CT, 'CT', instance_tags={DCMHEADER_FRAME_OF_REFERENCE_UID: FOR_CT})
		dcm = self.make_instance(model_tags(FOR_MODEL, M3D_UNIT_CM), [ct])

		# Declared by the model: the referenced series are not consulted
		frame = dcm.frame
		self.assertEqual((frame.frame_of_reference_uid, frame.units, frame.frame_source), (FOR_MODEL, M3D_UNIT_CM, M3D_FRAME_SOURCE_INSTANCE))
		self.assertEqual(ct.tag_requests, 0)

		# Headers of a retrieved file take precedence over the instance tags; fallback can be disabled
		frame = dcm.resolve_frame(headers=dataset_from_tags(model_tags(references=(SX_CT,))), fallback=False)
		self.assertTrue(frame.is_default)
		self.assertEqual(ct.tag_requests, 0)
		frame = dcm.resolve_frame(headers=dataset_from_tags(model_tags(references=(SX_CT,))))
		self.assertEqual((frame.frame_of_reference_uid, frame.frame_source), (FOR_CT, M3D_FRAME_SOURCE_REFERENCE))

		# Referenced series that are not part of the study, or have no instances, yield no frame
		dcm = self.make_instance(model_tags(references=('1.2.3.gone',)), [series_stub(SX_CT, 'CT')])
		self.assertTrue(dcm.frame.is_default)

	def test_frame_of_generic_instance(self):
		'''	The series of a study are represented by ImagingSeries/DcmInstance whatever their modality
			(e.g. when browsing a study): the helper resolves the frame of a plain instance too
		'''
		ct = series_stub(SX_CT, 'CT', instance_tags={DCMHEADER_FRAME_OF_REFERENCE_UID: FOR_CT})
		dcm = self.make_instance(model_tags(), [ct], model_class=DcmInstance)
		self.assertFalse(hasattr(DcmInstance, 'resolve_frame'))

		frame = dcm_m3d_frame(dcm)
		self.assertEqual((frame.frame_of_reference_uid, frame.frame_source, frame.source_series_uid),
			(FOR_CT, M3D_FRAME_SOURCE_REFERENCE, SX_CT))
		frame = dcm_m3d_frame(dcm, headers=dataset_from_tags(model_tags(FOR_MODEL, M3D_UNIT_CM)))
		self.assertEqual((frame.frame_of_reference_uid, frame.units), (FOR_MODEL, M3D_UNIT_CM))
		self.assertTrue(dcm_m3d_frame(dcm, fallback=False).is_default)


class STLEncoderUnitsTest(unittest.TestCase):
	'''	Encapsulated STL instances declare the units of their coordinate system
	'''
	def encode(self, attrs, **kwargs):
		ds = dcm_encode_stl(dict(attrs), BytesIO(b'solid x\nendsolid x\n'), **kwargs)
		stream = BytesIO()
		ds.save_as(stream)
		stream.seek(0)
		return dcmread(stream)

	def test_default_units(self):
		ds = self.encode({DCMHEADER_FRAME_OF_REFERENCE_UID: FOR_CT})
		self.assertEqual(getattr(ds, DCMHEADER_SOP_CLASS_UID), DCMEDIA_ENCAPSULATED_STL_SOP_CLASS)
		self.assertEqual(getattr(ds, DCMHEADER_ENCAPSULATED_DOCUMENT_MIMETYPE), DCMEDIA_STL_MIMETPYE)
		self.assertEqual(dcm_m3d_units(ds), M3D_UNIT_MM)

		frame = dcm_m3d_frame_from_headers(ds)
		self.assertEqual((frame.frame_of_reference_uid, frame.units, frame.frame_source, frame.units_source),
			(FOR_CT, M3D_UNIT_MM, M3D_FRAME_SOURCE_INSTANCE, M3D_FRAME_SOURCE_INSTANCE))

	def test_explicit_units(self):
		self.assertEqual(dcm_m3d_units(self.encode({'PatientID': 'p1'}, stl_units=M3D_UNIT_CM)), M3D_UNIT_CM)
		# Attributes that provide the sequence are kept
		ds = self.encode({DCMHEADER_MEASUREMENT_UNITS_CODE_SEQUENCE: dcm_encode_m3d_units(M3D_UNIT_M)}, stl_units=M3D_UNIT_CM)
		self.assertEqual(dcm_m3d_units(ds), M3D_UNIT_M)
		with self.assertRaises(ValueError):
			self.encode({'PatientID': 'p1'}, stl_units='ft')

	def test_series_tags_whitelist(self):
		'''	The frame of reference headers are copied from the source series to the models
		'''
		self.assertIn(DCMHEADER_FRAME_OF_REFERENCE_UID, M3D_DCM_SERIES_TAGS)
		self.assertIn(DCMHEADER_POSITION_REFERENCE_INDICATOR, M3D_DCM_SERIES_TAGS)


if __name__ == '__main__':
	unittest.main()
