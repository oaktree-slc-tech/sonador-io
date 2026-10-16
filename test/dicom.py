'''	Offline unit tests for the DICOM encoding/decoding helpers of the client (pydicom 3 / highdicom fork).

	These tests do not require a Sonador server. Run from the directory containing the `sonador`
	package with:

		python -m unittest sonador.test.dicom -v
'''
import copy, unittest, warnings
from importlib import metadata
from io import BytesIO
from unittest import mock

import numpy as np

from pydicom import dcmread
from pydicom.data import get_testdata_file
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.sequence import Sequence
from pydicom.sr.codedict import codes
from pydicom.uid import UID
from pydicom.uid import generate_uid, ImplicitVRLittleEndian, ExplicitVRLittleEndian, ExplicitVRBigEndian, \
	SecondaryCaptureImageStorage, RLELossless

from highdicom.sr import Comprehensive3DSR, Scoord3DContentItem, GraphicTypeValues3D, \
	MeasurementsAndQualitativeEvaluations, Measurement, TrackingIdentifier
from highdicom.sr.enum import RelationshipTypeValues, ValueTypeValues
from highdicom.sr.utils import find_content_items

from .. import dcmcompat
from ..apisettings.base import DCMSR_UNITS_MM, DCMHEADER_SR_EVIDENCE_SEQUENCES, \
	DCMHEADER_SR_CURRENT_REQUESTED_PROCEDURE_EVIDENCE_SEQUENCE, DCMHEADER_SR_PERTINENT_OTHER_EVIDENCE_SEQUENCE, \
	DCMHEADER_SR_REF_SERIES_SEQ, DCMHEADER_SR_REF_SOP_SEQ, DCMHEADER_SR_REF_INSTANCE_UID, DCMHEADER_SERIES_INSTANCE_UID
from ..apisettings.sr import srencode_observation_context, DCMSR_SCOORD3D, DCMSR_REPORT_METADATA, \
	DCMSR_REPORT_VOLUME_MEAUREMENTS
from ..helpers.local import dcm_part10_backfill
from ..imaging.helpers.conversion import dcm_encode_filedata, array2dcmimg
from ..imaging.helpers.sr.encoding import SonadorSRMeasurementReport, ReportMetaGroup, VolumetricPointCollection
from ..imaging.orthanc.sr import DcmSRInstance, dcmsr_iter_series_references


def dataset2simplified(ds):
	'''	Convert a pydicom dataset to the "simplified" tag structure returned by the imaging server
		(keyword -> value, sequences as lists of dicts), which is what the Orthanc models read.
	'''
	simplified = {}
	for elem in ds:
		if elem.VR == 'SQ':
			simplified[elem.keyword] = [dataset2simplified(item) for item in elem.value]
		elif elem.keyword:
			simplified[elem.keyword] = str(elem.value) if elem.value is not None else None
	return simplified


def sr_instance_from_tags(tags):
	'''	DcmSRInstance bound to the provided tags, without a server connection
	'''
	instance = DcmSRInstance.__new__(DcmSRInstance)
	instance._tags = tags
	return instance


def ct_evidence():
	'''	Two CT evidence datasets from different studies
	'''
	ct1 = dcmread(get_testdata_file('CT_small.dcm'))
	ct2 = copy.deepcopy(ct1)
	ct2.StudyInstanceUID = generate_uid()
	ct2.SeriesInstanceUID = generate_uid()
	ct2.SOPInstanceUID = generate_uid()
	return ct1, ct2


def save_and_reload(ds):
	'''	Serialize the dataset with deprecation warnings treated as errors and read it back
	'''
	buf = BytesIO()
	with warnings.catch_warnings():
		warnings.simplefilter('error', DeprecationWarning)
		ds.save_as(buf)
	buf.seek(0)
	return dcmread(buf)


class DcmCompatTestCase(unittest.TestCase):
	'''	dcmcompat: highdicom fork detection and transfer syntax helpers
	'''
	def test_ucum_millimeter(self):
		self.assertEqual(DCMSR_UNITS_MM.value, 'mm')
		self.assertEqual(DCMSR_UNITS_MM.scheme_designator, 'UCUM')

	def test_highdicom_is_oaktree_fork(self):
		self.assertTrue(dcmcompat.highdicom_is_oaktree_fork())
		self.assertIsNotNone(dcmcompat.highdicom_dist_version())
		dcmcompat.require_oaktree_highdicom()

	def test_fork_requirement_is_exact(self):
		'''	The requirement must select the published fork build only. `~=0.28.2.post1` would also
			admit stock 0.28.3+, which installs but lacks `record_instances`.
		'''
		try:
			from packaging.requirements import Requirement
		except ImportError:
			self.skipTest('packaging is not installed')

		requirement = Requirement(dcmcompat.HIGHDICOM_FORK_REQUIREMENT)
		self.assertEqual(requirement.name, dcmcompat.HIGHDICOM_DISTRIBUTION)
		self.assertTrue(requirement.specifier.contains(dcmcompat.HIGHDICOM_FORK_VERSION, prereleases=True))
		for stock in ('0.27.0', '0.28.2', '0.28.3', '0.28.9', '0.29.0', '1.0.0'):
			self.assertFalse(requirement.specifier.contains(stock, prereleases=True),
				msg='Requirement %s admits stock highdicom %s' % (dcmcompat.HIGHDICOM_FORK_REQUIREMENT, stock))
		# Later rebuilds of the same base are deliberately not admitted without a pin bump
		self.assertFalse(requirement.specifier.contains('0.28.2.post2', prereleases=True))

	def test_installed_highdicom_is_the_pinned_fork_release(self):
		'''	Provenance of the installed artifact: distribution version matches the pin, and the
			module exposes the fork extension.
		'''
		self.assertEqual(metadata.version('highdicom'), dcmcompat.HIGHDICOM_FORK_VERSION)
		self.assertTrue(dcmcompat.highdicom_dist_is_fork_release())
		self.assertTrue(dcmcompat.highdicom_is_oaktree_fork())
		with mock.patch.object(dcmcompat, 'highdicom_dist_version', return_value='0.28.3'):
			self.assertFalse(dcmcompat.highdicom_dist_is_fork_release())

	def test_require_oaktree_highdicom_error(self):
		with mock.patch.object(dcmcompat, 'highdicom_is_oaktree_fork', return_value=False):
			with self.assertRaises(ImportError) as ctx:
				dcmcompat.require_oaktree_highdicom()

		self.assertIn('record_instances', str(ctx.exception))
		self.assertIn(dcmcompat.HIGHDICOM_FORK_INDEX_URL, str(ctx.exception))
		self.assertIn(dcmcompat.HIGHDICOM_FORK_REQUIREMENT, str(ctx.exception))

	def test_transfer_syntax(self):
		self.assertEqual(dcmcompat.dcm_transfer_syntax(), ImplicitVRLittleEndian)
		self.assertEqual(dcmcompat.dcm_transfer_syntax(little_endian=True, implicit_vr=False), ExplicitVRLittleEndian)
		self.assertEqual(dcmcompat.dcm_transfer_syntax(little_endian=False, implicit_vr=False), ExplicitVRBigEndian)
		with self.assertRaises(ValueError):
			dcmcompat.dcm_transfer_syntax(little_endian=False, implicit_vr=True)

	def test_encoding_from_transfer_syntax(self):
		self.assertEqual(dcmcompat.dcm_encoding_from_transfer_syntax(ImplicitVRLittleEndian), (True, True))
		self.assertEqual(dcmcompat.dcm_encoding_from_transfer_syntax(ExplicitVRLittleEndian), (False, True))
		self.assertEqual(dcmcompat.dcm_encoding_from_transfer_syntax(ExplicitVRBigEndian), (False, False))
		self.assertEqual(dcmcompat.dcm_encoding_from_transfer_syntax(RLELossless), (False, True))
		# Private transfer syntax: DICOM default encoding
		self.assertEqual(dcmcompat.dcm_encoding_from_transfer_syntax(UID('1.2.826.0.1.3680043.9.9999.1')), (False, True))

	def test_set_original_encoding(self):
		ds = Dataset()
		ds.file_meta = FileMetaDataset()
		ds.file_meta.TransferSyntaxUID = ExplicitVRBigEndian
		self.assertEqual(dcmcompat.dcm_set_original_encoding(ds), (False, False))
		self.assertEqual(ds.original_encoding, (False, False))
		self.assertEqual(dcmcompat.dcm_set_original_encoding(ds, ImplicitVRLittleEndian), (True, True))
		self.assertEqual(ds.original_encoding, (True, True))
		with self.assertRaises(ValueError):
			dcmcompat.dcm_set_original_encoding(Dataset())

	def test_transfer_syntax_from_dataset(self):
		# In-memory dataset: no original encoding, default applies
		self.assertEqual(dcmcompat.dcm_transfer_syntax_from_dataset(Dataset()), ExplicitVRLittleEndian)
		self.assertEqual(
			dcmcompat.dcm_transfer_syntax_from_dataset(Dataset(), default=ImplicitVRLittleEndian), ImplicitVRLittleEndian)

		# Dataset read from a file: encoding is known
		ct = dcmread(get_testdata_file('CT_small.dcm'))
		self.assertEqual(dcmcompat.dcm_transfer_syntax_from_dataset(ct), ct.file_meta.TransferSyntaxUID)

		implicit = save_and_reload(array2dcmimg(np.zeros((4, 4), dtype=np.uint16)))
		self.assertEqual(dcmcompat.dcm_transfer_syntax_from_dataset(implicit), ImplicitVRLittleEndian)


class DcmEncodeTestCase(unittest.TestCase):
	'''	dcm_encode_filedata / array2dcmimg under pydicom 3
	'''
	def test_filedata_default_transfer_syntax(self):
		ds = dcm_encode_filedata({'SOPClassUID': SecondaryCaptureImageStorage, 'PatientID': 'P1'})
		self.assertEqual(ds.file_meta.TransferSyntaxUID, ImplicitVRLittleEndian)
		self.assertEqual(ds.file_meta.MediaStorageSOPClassUID, SecondaryCaptureImageStorage)
		self.assertEqual(ds.file_meta.MediaStorageSOPInstanceUID, ds.SOPInstanceUID)
		self.assertEqual(ds.PatientID, 'P1')

		reloaded = save_and_reload(ds)
		self.assertEqual(reloaded.file_meta.TransferSyntaxUID, ImplicitVRLittleEndian)
		self.assertEqual(reloaded.SOPInstanceUID, ds.SOPInstanceUID)
		self.assertEqual(reloaded.PatientID, 'P1')

	def test_filedata_explicit_transfer_syntax(self):
		ds = dcm_encode_filedata({'SOPClassUID': SecondaryCaptureImageStorage}, dcm_implicit_vr=False)
		self.assertEqual(ds.file_meta.TransferSyntaxUID, ExplicitVRLittleEndian)
		self.assertEqual(save_and_reload(ds).file_meta.TransferSyntaxUID, ExplicitVRLittleEndian)

	def test_filedata_preserves_provided_transfer_syntax(self):
		meta = Dataset()
		meta.TransferSyntaxUID = ExplicitVRLittleEndian
		ds = dcm_encode_filedata({'SOPClassUID': SecondaryCaptureImageStorage}, dcmfile_meta=meta)
		self.assertEqual(ds.file_meta.TransferSyntaxUID, ExplicitVRLittleEndian)

	def assert_big_endian_roundtrip(self, ds):
		'''	A dataset selected as Explicit VR Big Endian is written in that encoding by a plain
			`save_as()` and the numeric values survive the round trip.
		'''
		self.assertEqual(ds.file_meta.TransferSyntaxUID, ExplicitVRBigEndian)
		self.assertEqual(ds.original_encoding, (False, False))

		# Representative numeric VRs: US, IS, DS, UL, FD
		ds.Rows = 4
		ds.Columns = 3
		ds.InstanceNumber = 7
		ds.SliceThickness = 1.5
		ds.add_new(0x00281201, 'UL', 258)
		ds.add_new(0x00189098, 'FD', [63.5, -1.25])

		reloaded = save_and_reload(ds)
		self.assertEqual(reloaded.file_meta.TransferSyntaxUID, ExplicitVRBigEndian)
		self.assertEqual(reloaded.original_encoding, (False, False))
		self.assertEqual(reloaded.SOPInstanceUID, ds.SOPInstanceUID)
		self.assertEqual(reloaded.Rows, 4)
		self.assertEqual(reloaded.Columns, 3)
		self.assertEqual(reloaded.InstanceNumber, 7)
		self.assertEqual(reloaded.SliceThickness, 1.5)
		self.assertEqual(reloaded[0x00281201].value, 258)
		self.assertEqual(list(reloaded[0x00189098].value), [63.5, -1.25])
		return reloaded

	def test_filedata_big_endian_via_arguments(self):
		ds = dcm_encode_filedata({'SOPClassUID': SecondaryCaptureImageStorage, 'PatientID': 'BE1'},
			dcm_little_endian=False, dcm_implicit_vr=False)
		self.assertEqual(self.assert_big_endian_roundtrip(ds).PatientID, 'BE1')

	def test_filedata_big_endian_via_file_meta(self):
		meta = FileMetaDataset()
		meta.TransferSyntaxUID = ExplicitVRBigEndian
		ds = dcm_encode_filedata({'SOPClassUID': SecondaryCaptureImageStorage, 'PatientID': 'BE2'}, dcmfile_meta=meta)
		self.assertEqual(self.assert_big_endian_roundtrip(ds).PatientID, 'BE2')

	def test_filedata_implicit_big_endian_rejected(self):
		with self.assertRaises(ValueError):
			dcm_encode_filedata({'SOPClassUID': SecondaryCaptureImageStorage}, dcm_little_endian=False)

	def test_array2dcmimg_big_endian_pixels(self):
		'''	Pixel data follows the byte order of the transfer syntax
		'''
		arr = np.array([[1, 256, 4660, 65535]], dtype=np.uint16)
		ds = array2dcmimg(arr, dcm_little_endian=False, dcm_implicit_vr=False)
		self.assertEqual(ds.file_meta.TransferSyntaxUID, ExplicitVRBigEndian)
		self.assertEqual(ds.PixelData, arr.astype('>u2').tobytes())

		reloaded = save_and_reload(ds)
		self.assertEqual(reloaded.file_meta.TransferSyntaxUID, ExplicitVRBigEndian)
		np.testing.assert_array_equal(reloaded.pixel_array, arr)

		# Signed 16-bit
		signed = np.array([[-2, -1, 0, 1]], dtype=np.int16)
		np.testing.assert_array_equal(
			save_and_reload(array2dcmimg(signed, dcm_little_endian=False, dcm_implicit_vr=False)).pixel_array, signed)

	def test_array2dcmimg_compressed_overrides_big_endian(self):
		'''	Encapsulated transfer syntaxes are little endian; a big endian request is superseded by
			the compressor and the dataset still saves.
		'''
		arr = np.arange(16, dtype=np.uint16).reshape((4, 4))
		reloaded = save_and_reload(array2dcmimg(arr, dcm_compression=RLELossless, dcm_little_endian=False,
			dcm_implicit_vr=False))
		self.assertEqual(reloaded.file_meta.TransferSyntaxUID, RLELossless)
		np.testing.assert_array_equal(reloaded.pixel_array, arr)

	def test_array2dcmimg_roundtrip(self):
		arr = np.arange(16, dtype=np.uint16).reshape((4, 4))
		reloaded = save_and_reload(array2dcmimg(arr, dcm_attrs={'PatientID': 'P2'}))
		self.assertEqual(reloaded.file_meta.TransferSyntaxUID, ImplicitVRLittleEndian)
		self.assertEqual(reloaded.SOPClassUID, SecondaryCaptureImageStorage)
		self.assertEqual(reloaded.Rows, 4)
		self.assertEqual(reloaded.Columns, 4)
		np.testing.assert_array_equal(reloaded.pixel_array, arr)

	def test_array2dcmimg_compressed(self):
		arr = np.arange(16, dtype=np.uint16).reshape((4, 4))
		reloaded = save_and_reload(array2dcmimg(arr, dcm_compression=RLELossless))
		self.assertEqual(reloaded.file_meta.TransferSyntaxUID, RLELossless)
		np.testing.assert_array_equal(reloaded.pixel_array, arr)


class DcmPart10BackfillTestCase(unittest.TestCase):
	'''	dcm_part10_backfill under pydicom 3
	'''
	def test_in_memory_dataset(self):
		ds = Dataset()
		ds.SOPClassUID = SecondaryCaptureImageStorage
		ds.SOPInstanceUID = generate_uid()
		ds.PatientID = 'P3'

		ds, updated = dcm_part10_backfill(ds)
		self.assertTrue(updated)
		self.assertEqual(ds.file_meta.TransferSyntaxUID, ExplicitVRLittleEndian)
		self.assertEqual(ds.file_meta.MediaStorageSOPClassUID, SecondaryCaptureImageStorage)
		self.assertEqual(ds.file_meta.MediaStorageSOPInstanceUID, ds.SOPInstanceUID)
		self.assertTrue(ds.file_meta.ImplementationVersionName)

		# A conformant Part 10 file is produced without extra arguments to save_as
		reloaded = save_and_reload(ds)
		self.assertEqual(reloaded.PatientID, 'P3')
		self.assertEqual(reloaded.file_meta.TransferSyntaxUID, ExplicitVRLittleEndian)

	def test_dataset_read_from_file_without_meta(self):
		ct = dcmread(get_testdata_file('CT_small.dcm'))
		original_ts = ct.file_meta.TransferSyntaxUID
		del ct.file_meta

		ct, updated = dcm_part10_backfill(ct)
		self.assertTrue(updated)
		self.assertEqual(ct.file_meta.TransferSyntaxUID, original_ts)
		self.assertEqual(ct.file_meta.MediaStorageSOPInstanceUID, ct.SOPInstanceUID)
		self.assertEqual(save_and_reload(ct).SOPInstanceUID, ct.SOPInstanceUID)

	def test_complete_dataset_unchanged(self):
		ct = dcmread(get_testdata_file('CT_small.dcm'))
		ts = ct.file_meta.TransferSyntaxUID
		ct, updated = dcm_part10_backfill(ct)
		self.assertFalse(updated)
		self.assertEqual(ct.file_meta.TransferSyntaxUID, ts)


class SRReportTemplateTestCase(unittest.TestCase):
	'''	SonadorSRMeasurementReport with and without imaging measurements
	'''
	def setUp(self):
		self.observation_context = srencode_observation_context(context_uuid=generate_uid(), device_name='Sonador Tests')
		self.procedure = codes.LN.CTUnspecifiedBodyRegion
		self.frame_of_reference_uid = generate_uid()

	def meta_group(self):
		from highdicom.sr.value_types import TextContentItem
		return ReportMetaGroup(
			tracking_identifier=TrackingIdentifier(uid=generate_uid(), identifier='Meta'),
			meta=[TextContentItem(name=codes.DCM.Comment, value='metadata only',
				relationship_type=RelationshipTypeValues.CONTAINS)])

	def volume_group(self):
		point = Scoord3DContentItem(
			name=DCMSR_SCOORD3D, graphic_type=GraphicTypeValues3D.POINT,
			graphic_data=np.array([[1.0, 2.0, 3.0]]), frame_of_reference_uid=self.frame_of_reference_uid,
			relationship_type=RelationshipTypeValues.CONTAINS)
		return VolumetricPointCollection(
			tracking_identifier=TrackingIdentifier(uid=generate_uid(), identifier='Points'), points=[point])

	def imaging_group(self):
		return MeasurementsAndQualitativeEvaluations(
			tracking_identifier=TrackingIdentifier(uid=generate_uid(), identifier='Measurements'),
			measurements=[Measurement(name=codes.SCT.Length, value=1.5, unit=DCMSR_UNITS_MM)])

	def containers(self, report, code):
		'''	Top-level CONTAINER items of the report whose concept name matches `code` (value and scheme).
			Sonador codes carry a scheme version, so `find_content_items(name=...)` would not match them.
		'''
		matched = []
		for item in report[0].ContentSequence:
			if item.ValueType != ValueTypeValues.CONTAINER.value:
				continue
			name = item.ConceptNameCodeSequence[0]
			value = name.get('CodeValue') or name.get('LongCodeValue') or name.get('URNCodeValue')
			if value == code.value and name.CodingSchemeDesignator == code.scheme_designator:
				matched.append(item)
		return matched

	def assert_report_structure(self, report, expect_imaging, expect_meta, expect_volume):
		root = report[0]
		self.assertEqual(root.ContentTemplateSequence[0].TemplateIdentifier, SonadorSRMeasurementReport.template_id)
		self.assertEqual(len(find_content_items(root, name=codes.DCM.ProcedureReported)), 1)
		self.assertEqual(len(self.containers(report, codes.DCM.ImagingMeasurements)), 1 if expect_imaging else 0)
		self.assertEqual(len(self.containers(report, DCMSR_REPORT_METADATA)), 1 if expect_meta else 0)
		self.assertEqual(len(self.containers(report, DCMSR_REPORT_VOLUME_MEAUREMENTS)), 1 if expect_volume else 0)

	def document(self, report, **kwargs):
		'''	Wrap the report in a Comprehensive 3D SR document and round-trip it through pydicom
		'''
		ct1, _ = ct_evidence()
		doc = Comprehensive3DSR(
			evidence=[ct1], content=report, series_instance_uid=generate_uid(), series_number=1,
			sop_instance_uid=generate_uid(), instance_number=1, manufacturer='Sonador Tests', **kwargs)
		return Comprehensive3DSR.from_dataset(save_and_reload(doc))

	def test_metadata_only(self):
		report = SonadorSRMeasurementReport(
			observation_context=self.observation_context, procedure_reported=self.procedure,
			imaging_measurements=[], meta_groups=[self.meta_group()])
		self.assert_report_structure(report, expect_imaging=False, expect_meta=True, expect_volume=False)
		doc = self.document(report)
		self.assertEqual(len(find_content_items(doc, name=codes.DCM.Comment, recursive=True)), 1)

	def test_volume_points_only(self):
		report = SonadorSRMeasurementReport(
			observation_context=self.observation_context, procedure_reported=self.procedure,
			volume_measurements=[self.volume_group()])
		self.assert_report_structure(report, expect_imaging=False, expect_meta=False, expect_volume=True)
		doc = self.document(report)
		self.assertEqual(len(find_content_items(doc, value_type=ValueTypeValues.SCOORD3D, recursive=True)), 1)

	def test_mixed_report(self):
		report = SonadorSRMeasurementReport(
			observation_context=self.observation_context, procedure_reported=self.procedure,
			imaging_measurements=[self.imaging_group()], meta_groups=[self.meta_group()],
			volume_measurements=[self.volume_group()])
		self.assert_report_structure(report, expect_imaging=True, expect_meta=True, expect_volume=True)
		self.assertEqual(len(report.get_image_measurement_groups()), 1)
		doc = self.document(report)
		self.assertEqual(len(find_content_items(doc, value_type=ValueTypeValues.SCOORD3D, recursive=True)), 1)
		self.assertEqual(len(find_content_items(doc, name=codes.SCT.Length, recursive=True)), 1)

	def test_imaging_measurements_only(self):
		report = SonadorSRMeasurementReport(
			observation_context=self.observation_context, procedure_reported=self.procedure,
			imaging_measurements=[self.imaging_group()])
		self.assert_report_structure(report, expect_imaging=True, expect_meta=False, expect_volume=False)

	def test_positional_arguments(self):
		# Mirrors the upstream MeasurementReport signature
		report = SonadorSRMeasurementReport(self.observation_context, self.procedure, [self.imaging_group()])
		self.assert_report_structure(report, expect_imaging=True, expect_meta=False, expect_volume=False)

	def test_empty_report_rejected(self):
		with self.assertRaises(ValueError):
			SonadorSRMeasurementReport(
				observation_context=self.observation_context, procedure_reported=self.procedure,
				imaging_measurements=[])

	def test_invalid_group_types_rejected(self):
		with self.assertRaises(TypeError):
			SonadorSRMeasurementReport(
				observation_context=self.observation_context, procedure_reported=self.procedure,
				meta_groups=[self.volume_group()])
		with self.assertRaises(TypeError):
			SonadorSRMeasurementReport(
				observation_context=self.observation_context, procedure_reported=self.procedure,
				volume_measurements=[self.meta_group()])


class SREvidenceReaderTestCase(unittest.TestCase):
	'''	DcmSRInstance evidence readers: both evidence sequences, both `record_instances` modes
	'''
	def setUp(self):
		self.ct1, self.ct2 = ct_evidence()
		self.report = SonadorSRMeasurementReport(
			observation_context=srencode_observation_context(context_uuid=generate_uid(), device_name='Sonador Tests'),
			procedure_reported=codes.LN.CTUnspecifiedBodyRegion,
			imaging_measurements=[MeasurementsAndQualitativeEvaluations(
				tracking_identifier=TrackingIdentifier(uid=generate_uid(), identifier='Measurements'),
				measurements=[Measurement(name=codes.SCT.Length, value=1.5, unit=DCMSR_UNITS_MM)])])

	def document_tags(self, record_instances):
		doc = Comprehensive3DSR(
			evidence=[self.ct1, self.ct2], content=self.report, series_instance_uid=generate_uid(),
			series_number=1, sop_instance_uid=generate_uid(), instance_number=1, manufacturer='Sonador Tests',
			record_instances=record_instances)
		return dataset2simplified(save_and_reload(doc))

	def test_document_uses_both_evidence_sequences(self):
		tags = self.document_tags(record_instances=True)
		self.assertIn(DCMHEADER_SR_CURRENT_REQUESTED_PROCEDURE_EVIDENCE_SEQUENCE, tags)
		self.assertIn(DCMHEADER_SR_PERTINENT_OTHER_EVIDENCE_SEQUENCE, tags)
		self.assertEqual(len(list(dcmsr_iter_series_references(tags))), 2)

	def test_record_instances_true(self):
		instance = sr_instance_from_tags(self.document_tags(record_instances=True))
		self.assertEqual(instance.instance_reference_uids, {self.ct1.SOPInstanceUID, self.ct2.SOPInstanceUID})
		self.assertEqual(instance.series_reference_uids, {self.ct1.SeriesInstanceUID, self.ct2.SeriesInstanceUID})

	def test_record_instances_false(self):
		tags = self.document_tags(record_instances=False)
		for ref in dcmsr_iter_series_references(tags):
			self.assertNotIn(DCMHEADER_SR_REF_SOP_SEQ, ref)

		instance = sr_instance_from_tags(tags)
		self.assertEqual(instance.instance_reference_uids, set())
		self.assertEqual(instance.series_reference_uids, {self.ct1.SeriesInstanceUID, self.ct2.SeriesInstanceUID})

	def test_legacy_document_pertinent_other_only(self):
		# Reports written with older releases record all evidence in the Pertinent Other Evidence Sequence
		tags = {
			DCMHEADER_SR_PERTINENT_OTHER_EVIDENCE_SEQUENCE: [{
				'StudyInstanceUID': self.ct1.StudyInstanceUID,
				DCMHEADER_SR_REF_SERIES_SEQ: [{
					DCMHEADER_SERIES_INSTANCE_UID: self.ct1.SeriesInstanceUID,
					DCMHEADER_SR_REF_SOP_SEQ: [
						{DCMHEADER_SR_REF_INSTANCE_UID: self.ct1.SOPInstanceUID, 'ReferencedSOPClassUID': self.ct1.SOPClassUID},
						{DCMHEADER_SR_REF_INSTANCE_UID: self.ct1.SOPInstanceUID, 'ReferencedSOPClassUID': self.ct1.SOPClassUID},
					],
				}],
			}],
		}
		instance = sr_instance_from_tags(tags)
		self.assertEqual(instance.instance_reference_uids, {self.ct1.SOPInstanceUID})
		self.assertEqual(instance.series_reference_uids, {self.ct1.SeriesInstanceUID})

	def test_same_series_in_both_sequences_deduplicated(self):
		series_item = {
			DCMHEADER_SERIES_INSTANCE_UID: self.ct1.SeriesInstanceUID,
			DCMHEADER_SR_REF_SOP_SEQ: [{DCMHEADER_SR_REF_INSTANCE_UID: self.ct1.SOPInstanceUID}],
		}
		tags = {seq: [{'StudyInstanceUID': self.ct1.StudyInstanceUID, DCMHEADER_SR_REF_SERIES_SEQ: [series_item]}]
			for seq in DCMHEADER_SR_EVIDENCE_SEQUENCES}
		instance = sr_instance_from_tags(tags)
		self.assertEqual(instance.instance_reference_uids, {self.ct1.SOPInstanceUID})
		self.assertEqual(instance.series_reference_uids, {self.ct1.SeriesInstanceUID})

	def test_missing_sequences_tolerated(self):
		for tags in ({}, {DCMHEADER_SR_PERTINENT_OTHER_EVIDENCE_SEQUENCE: None},
				{DCMHEADER_SR_CURRENT_REQUESTED_PROCEDURE_EVIDENCE_SEQUENCE: [{'StudyInstanceUID': '1.2.3'}]},
				{DCMHEADER_SR_PERTINENT_OTHER_EVIDENCE_SEQUENCE: [{DCMHEADER_SR_REF_SERIES_SEQ: [{}]}]}):
			instance = sr_instance_from_tags(tags)
			self.assertEqual(instance.instance_reference_uids, set())
			self.assertEqual(instance.series_reference_uids, set())


class SRModelTestCase(unittest.TestCase):
	'''	SonadorComprehensiveSR3D model -> DICOM-SR document, with evidence passed directly (no server)
	'''
	def setUp(self):
		from highdicom.sr.value_types import TextContentItem
		from ..apisettings.base import ReportMetaGroup as ReportMetaGroupModel
		self.ct1, self.ct2 = ct_evidence()
		self.meta_group = ReportMetaGroupModel(
			meta=[TextContentItem(name=codes.DCM.Comment, value='metadata only',
				relationship_type=RelationshipTypeValues.CONTAINS)],
			tracking_id='meta-1')

	def create_document(self, record_instances):
		from ..imaging.helpers.sr import SonadorComprehensiveSR3D
		model = SonadorComprehensiveSR3D(
			dcm_evidence=[self.ct1, self.ct2], report_procedure=codes.LN.CTUnspecifiedBodyRegion,
			groups=[self.meta_group], observation_context_device_name='Sonador Tests')
		return save_and_reload(model.create_sr(record_instances=record_instances))

	def test_metadata_only_report(self):
		for record_instances in (True, False):
			with self.subTest(record_instances=record_instances):
				doc = self.create_document(record_instances)

				# Metadata container is present, no imaging measurements container was required
				self.assertEqual(len(find_content_items(doc, name=codes.DCM.Comment, recursive=True)), 1)
				self.assertEqual(len(find_content_items(doc, name=codes.DCM.ImagingMeasurements, recursive=True)), 0)

				# Same-study and other-study evidence are recorded in their respective sequences
				self.assertEqual(len(doc.CurrentRequestedProcedureEvidenceSequence), 1)
				self.assertEqual(len(doc.PertinentOtherEvidenceSequence), 1)

				# Readers see both series; instances only when they were recorded
				instance = sr_instance_from_tags(dataset2simplified(doc))
				self.assertEqual(instance.series_reference_uids, {self.ct1.SeriesInstanceUID, self.ct2.SeriesInstanceUID})
				if record_instances:
					self.assertEqual(instance.instance_reference_uids, {self.ct1.SOPInstanceUID, self.ct2.SOPInstanceUID})
				else:
					self.assertEqual(instance.instance_reference_uids, set())


if __name__ == '__main__':
	unittest.main()
