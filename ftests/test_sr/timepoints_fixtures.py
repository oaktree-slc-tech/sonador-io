'''	Generator of the timepoint fixtures: a synthetic image and DICOM-SR documents with and without a series-level
	timepoint, for the Sonador IO tests (`sr_timepoints`, `sonador.ftests.test_dicom.dicom_timepoint`) and the Viewer's
	timepoint reader (oak-tree/medical-imaging/ohif-viewers#176). The encoding is the contract of
	oak-tree/medical-imaging/sonador-client#50.

	Every file is synthetic: no patient, study or imaging identifier of a real case. The documents are written by the
	client's writers with fixed UIDs, dates and times, so the output is reproducible byte for byte. A file changed
	after writing (the malformed items, the second revision of the conflict series) records the change in the manifest.

	First pack (what the Viewer's acceptance runs on): the image, a Comprehensive 3D SR document that references it
	with a timepoint and one Length measurement, and the same document without a timepoint. Second pack: a re-save, a
	series that joins a timepoint, a series whose revisions carry different timepoints, an image dataset that holds a
	Modality Performed Procedure Step item before and after a timepoint is written, a study of SR series over two
	timepoints, and malformed timepoint items.

	The manifest (`MANIFEST_FILE`) lists each file with its pack, the timepoint and the annotation identifiers expected
	from it, its SHA-256, the command below and the versions of the pydicom and highdicom distributions used. It also
	holds `order_cases`: timepoints in the order of the contract, for both clients to sort.

	Regenerate (from the directory containing the `sonador` package):

		python -m sonador.ftests.test_sr.timepoints_fixtures [output directory]

	The default output directory is `ftests/test_sr/fixtures/timepoints`.
'''
import copy, hashlib, json, logging, os, sys, uuid
from importlib import metadata
from collections import OrderedDict

import numpy as np
import pydicom
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.sequence import Sequence
from pydicom.sr.codedict import codes
from pydicom.uid import ExplicitVRLittleEndian, generate_uid

from ...apisettings.base import LengthMeasurement, Timepoint, SONADOR_TIMEPOINT_SOP_CLASS_UID
from ...imaging.helpers.sr import SonadorImagingMeasurementReport
from ...imaging.helpers.timepoint import dcm_set_timepoint, dcm_timepoint_items
from . import frames_fixtures as F

FIXTURE_DATE = '2026-1118'
FIXTURES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fixtures', 'timepoints')
MANIFEST_FILE = 'sonador-io.timepoints-manifest.%s.json' % FIXTURE_DATE
COMMAND = 'python -m sonador.ftests.test_sr.timepoints_fixtures'

MR_IMAGE_STORAGE = '1.2.840.10008.5.1.4.1.1.4'
MPPS_SOP_CLASS = '1.2.840.10008.3.1.2.3.3'


def _uid(name):
	return generate_uid(entropy_srcs=['sonador-client timepoints fixtures', name])


def _timepoint_uid(name):
	'''	A UUID-derived UID (`2.25.` and the UUID as a decimal number), fixed by its name '''
	return '2.25.%d' % uuid.uuid5(uuid.UUID('6f0a1a52-55a5-4c0b-9d0c-0d5f3f1c8a11'), name).int


def fixture_name(topic, extension='dcm'):
	return 'sonador-io.%s.%s.%s' % (topic, FIXTURE_DATE, extension)


# The synthetic study: one image series and its Frame of Reference
STUDY_UID = _uid('study')
IMAGE_SERIES_UID, IMAGE_SOP_UID, IMAGE_FOR = _uid('image/series'), _uid('image/sop'), _uid('image/frame')
MPPS_SERIES_UID, MPPS_SOP_UID, MPPS_ITEM_UID = _uid('mpps-image/series'), _uid('mpps-image/sop'), _uid('mpps-image/step')

# The timepoints of the fixtures
TIMEPOINTS = OrderedDict((
	('postop', Timepoint(_timepoint_uid('postop'), 'TP-POSTOP-6W', '20261104', '093000', 'Post-operative review, 6 weeks')),
	('postop-later', Timepoint(_timepoint_uid('postop'), 'TP-POSTOP-6W', '20261104', '101500', 'Post-operative review, 6 weeks')),
	('preop', Timepoint(_timepoint_uid('preop'), 'TP-PREOP', '20260901', '140000', 'Pre-operative planning')),
	('other', Timepoint(_timepoint_uid('other'), 'TP-OTHER', '20261201', None, 'Follow-up, date only')),
))

# Timepoints in the order of the contract, written into the manifest for both clients to sort: dated before undated,
# date, time padded to twelve digits, description by UTF-16 code unit, then UID. The two descriptions of the fifth and
# sixth entries are U+10000 and U+E000: by code unit the supplementary-plane character (surrogates D800 DC00) is
# first, where an order by code point would put it last.
ORDER_CASES = (
	Timepoint('2.25.9101', None, '20260101', '0930', 'b'),
	Timepoint('2.25.9102', None, '20260101', '093000', 'c'),
	Timepoint('2.25.9103', None, '20260101', '093000.5', 'a'),
	Timepoint('2.25.9104', None, '20260102', None, None),
	Timepoint('2.25.9106', None, '20260103', '120000', '\U00010000'),
	Timepoint('2.25.9105', None, '20260103', '120000', '\ue000'),
	Timepoint('2.25.9108', None, '20260104', '9:30', 'x'),
	Timepoint('2.25.9109', None, '20260104', '000000', 'x'),
	Timepoint('2.25.9107', None, None, None, None),
	Timepoint('2.25.9111', None, None, None, 'A'),
	Timepoint('2.25.9110', None, '2026-01-01', None, 'B'),
)

# Length measurements, one tracking UID per document
LENGTH_POINTS = ((0.0, 0.0, 0.0), (10.0, 0.0, 0.0))


def image_dataset(series_uid=IMAGE_SERIES_UID, sop_uid=IMAGE_SOP_UID, series_number=1):
	'''	A synthetic MR image: 16 by 16 pixels of a fixed ramp, with the geometry a viewer needs to place it.

		@returns pydicom.Dataset with file meta information
	'''
	ds = Dataset()
	ds.file_meta = FileMetaDataset()
	ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
	ds.file_meta.MediaStorageSOPClassUID = MR_IMAGE_STORAGE
	ds.file_meta.MediaStorageSOPInstanceUID = sop_uid
	ds.file_meta.ImplementationClassUID = pydicom.uid.PYDICOM_IMPLEMENTATION_UID
	ds.SpecificCharacterSet = 'ISO_IR 100'
	ds.ImageType = ['ORIGINAL', 'PRIMARY', 'OTHER']
	ds.SOPClassUID, ds.SOPInstanceUID = MR_IMAGE_STORAGE, sop_uid
	ds.StudyDate, ds.SeriesDate, ds.ContentDate = '20260901', '20260901', '20260901'
	ds.StudyTime, ds.SeriesTime, ds.ContentTime = '120000', '120500', '120500'
	ds.AccessionNumber, ds.Modality, ds.Manufacturer = '', 'MR', 'Sonador synthetic'
	ds.ReferringPhysicianName, ds.StudyDescription, ds.SeriesDescription = '', 'Synthetic timepoint study', 'Synthetic image'
	ds.PatientName, ds.PatientID, ds.PatientBirthDate, ds.PatientSex = 'Anonymous', 'ANONYMOUS', '', ''
	ds.ScanningSequence, ds.SequenceVariant, ds.ScanOptions, ds.MRAcquisitionType = 'SE', 'NONE', '', '2D'
	ds.SliceThickness, ds.RepetitionTime, ds.EchoTime, ds.EchoTrainLength = '1.0', '500.0', '10.0', '1'
	ds.PatientPosition = 'HFS'
	ds.StudyInstanceUID, ds.SeriesInstanceUID, ds.StudyID = STUDY_UID, series_uid, '1'
	ds.SeriesNumber, ds.InstanceNumber = series_number, 1
	ds.ImagePositionPatient, ds.ImageOrientationPatient = [0.0, 0.0, 0.0], [1.0, 0.0, 0.0, 0.0, 1.0, 0.0]
	ds.FrameOfReferenceUID, ds.PositionReferenceIndicator = IMAGE_FOR, ''
	ds.SamplesPerPixel, ds.PhotometricInterpretation = 1, 'MONOCHROME2'
	ds.Rows, ds.Columns, ds.PixelSpacing = 16, 16, [1.0, 1.0]
	ds.BitsAllocated, ds.BitsStored, ds.HighBit, ds.PixelRepresentation = 16, 12, 11, 0
	ds.PixelData = (np.arange(256, dtype=np.uint16).reshape(16, 16) * 8).tobytes()
	return ds


def mpps_image_dataset():
	'''	The synthetic image of another series that holds a Modality Performed Procedure Step item, with an attribute
		the SOP Instance Reference macro does not define, and series-level Performed Procedure Step values of its own.
	'''
	ds = image_dataset(series_uid=MPPS_SERIES_UID, sop_uid=MPPS_SOP_UID, series_number=2)
	item = Dataset()
	item.ReferencedSOPClassUID, item.ReferencedSOPInstanceUID = MPPS_SOP_CLASS, MPPS_ITEM_UID
	item.InstanceNumber = 7
	ds.ReferencedPerformedProcedureStepSequence = Sequence([item])
	ds.PerformedProcedureStepID, ds.PerformedProcedureStepDescription = 'EXISTING-ID', 'EXISTING DESCRIPTION'
	return ds


def report(timepoint=None, tracking_uid=None):
	'''	A measurement report on the synthetic image with one Length measurement.

		@input timepoint (Timepoint, default=None): timepoint of the document
		@input tracking_uid (str): tracking UID of the measurement
		@returns SonadorImagingMeasurementReport
	'''
	model = SonadorImagingMeasurementReport(report_procedure=codes.LN.CTUnspecifiedBodyRegion, dcm_evidence=[image_dataset()],
		person_observer=F.PERSON_OBSERVER, imaging_frames=OrderedDict(((IMAGE_SERIES_UID, IMAGE_FOR),)), primary_frame_uid=IMAGE_FOR,
		timepoint=timepoint)
	model.measurements = [LengthMeasurement([tuple(p) for p in LENGTH_POINTS], frame_of_reference_uid=IMAGE_FOR, tracking_uid=tracking_uid,
		description='Length on the synthetic image')]
	return model


def write(model, name, series=None, **kwargs):
	'''	The model's document with fixed UIDs, dates and times, serialised and read back.

		@input name (str): seeds the SOP Instance UID and the Device Observer UID
		@input series (str, default=`name`): seeds the Series Instance UID; None on a re-save keeps the parsed series
	'''
	if series is not False:
		kwargs.setdefault('dcmsr_series_uid', _uid((series or name) + '/series'))
	doc = model.create_sr(record_instances=True, dcmsr_instance_uid=_uid(name + '/sop'), device_observer_uid=_uid(name + '/device'), **kwargs)
	return F.fix_dates(F.roundtrip(doc))


def read(ds, **kwargs):
	return SonadorImagingMeasurementReport.from_sr(copy.deepcopy(ds), imaging_frames=OrderedDict(((IMAGE_SERIES_UID, IMAGE_FOR),)),
		primary_frame_uid=IMAGE_FOR, **kwargs)


def _document(name, timepoint_key=None, **kwargs):
	return write(report(TIMEPOINTS[timepoint_key] if timepoint_key else None, tracking_uid=_uid(name + '/length')), name, **kwargs)


def _content_time(ds, time):
	'''	Give a revision a later Content Time, so that the header order of the revisions is their order of writing '''
	ds.ContentTime = time
	return ds


def _set_item(ds, **values):
	'''	Change attributes of the first timepoint item; a value of None removes the attribute '''
	item = dcm_timepoint_items(ds)[0][1]
	for keyword, value in values.items():
		if value is None:
			if keyword in item:
				delattr(item, keyword)
		else:
			item[pydicom.datadict.tag_for_keyword(keyword)] = pydicom.DataElement(pydicom.datadict.tag_for_keyword(keyword),
				pydicom.datadict.dictionary_VR(keyword), value, validation_mode=pydicom.config.IGNORE)
	return ds


def build_all():
	'''	Every fixture, in manifest order.

		@returns OrderedDict: topic -> dict(pack, dataset, role, timepoint, change)
	'''
	found = OrderedDict()

	def add(topic, pack, dataset, role, timepoint=None, change=None):
		found[topic] = OrderedDict((('pack', pack), ('dataset', dataset), ('role', role), ('timepoint', timepoint), ('change', change)))
		return dataset

	# First pack
	add('timepoint-image', 1, image_dataset(), 'Synthetic MR image the documents reference')
	first = add('timepoint-length', 1, _document('length', 'postop'), 'Comprehensive 3D SR with a timepoint and one Length measurement',
		TIMEPOINTS['postop'])
	add('timepoint-length-none', 1, _document('length-none'), 'The same document without a timepoint')

	# Second pack: re-save of the first document (same series, the first document its predecessor)
	add('timepoint-resave', 2, _content_time(write(read(first), 'resave', series=False), '120100'),
		'Re-save of timepoint-length: same series and timepoint', TIMEPOINTS['postop'])

	# A series that joins a timepoint in its second revision
	join_first = add('timepoint-join-r1', 2, _document('join-r1', series='join'), 'Joining series, first revision: no timepoint')
	joining = read(join_first)
	joining.timepoint = TIMEPOINTS['postop']
	add('timepoint-join-r2', 2, _content_time(write(joining, 'join-r2', series=False), '120100'),
		'Joining series, second revision: joins the timepoint', TIMEPOINTS['postop'])

	# A series whose revisions carry different timepoint UIDs: a writer refuses this, so the item is changed after writing
	conflict_first = add('timepoint-conflict-r1', 2, _document('conflict-r1', 'postop', series='conflict'),
		'Conflicting series, first revision', TIMEPOINTS['postop'])
	conflict_second = _content_time(write(read(conflict_first), 'conflict-r2', series=False), '120100')
	_set_item(conflict_second, ReferencedSOPInstanceUID=TIMEPOINTS['preop'].uid)
	add('timepoint-conflict-r2', 2, conflict_second, 'Conflicting series, second revision: another timepoint UID',
		TIMEPOINTS['postop']._replace(uid=TIMEPOINTS['preop'].uid),
		change='Referenced SOP Instance UID of the timepoint item replaced after writing')

	# An image dataset with a Modality Performed Procedure Step item, before and after a timepoint is written
	add('timepoint-image-mpps-before', 2, mpps_image_dataset(),
		'Synthetic image with a Modality Performed Procedure Step item and series-level values of its own')
	add('timepoint-image-mpps-after', 2, dcm_set_timepoint(mpps_image_dataset(), TIMEPOINTS['postop']),
		'The same image after dcm_set_timepoint: the item appended, the other item and the series-level values unchanged',
		TIMEPOINTS['postop'])

	# A study of SR series over two timepoints: A and A-time are members of one timepoint and differ only in time
	add('timepoint-study-a', 2, _document('study-a', 'postop'), 'Study series A: timepoint postop', TIMEPOINTS['postop'])
	add('timepoint-study-a-time', 2, _document('study-a-time', 'postop-later'),
		'Study series A-time: timepoint postop with another time', TIMEPOINTS['postop-later'])
	add('timepoint-study-b', 2, _document('study-b', 'preop'), 'Study series B: timepoint preop', TIMEPOINTS['preop'])
	add('timepoint-study-none', 2, _document('study-none'), 'Study series without a timepoint')

	# Malformed timepoint items, changed after writing
	several = _document('multiple-items', 'postop')
	second = copy.deepcopy(dcm_timepoint_items(several)[0][1])
	second.ReferencedSOPInstanceUID = TIMEPOINTS['other'].uid
	several.ReferencedPerformedProcedureStepSequence.append(second)
	add('timepoint-multiple-items', 2, several, 'Two timepoint items: the first is used', TIMEPOINTS['postop'],
		change='A second timepoint item with another UID appended after writing')
	add('timepoint-missing-uid', 2, _set_item(_document('missing-uid', 'postop'), ReferencedSOPInstanceUID=None),
		'A timepoint item without a Referenced SOP Instance UID: no timepoint',
		change='Referenced SOP Instance UID of the timepoint item removed after writing')
	add('timepoint-invalid-date', 2, _set_item(_document('invalid-date', 'postop'), PerformedProcedureStepStartDate='2026-11-04'),
		'A timepoint date that is not eight digits: the text is kept', TIMEPOINTS['postop']._replace(date='2026-11-04'),
		change='Performed Procedure Step Start Date of the timepoint item replaced after writing')
	add('timepoint-invalid-time', 2, _set_item(_document('invalid-time', 'postop'), PerformedProcedureStepStartTime='9:30'),
		'A timepoint time that is not a TM value: the text is kept', TIMEPOINTS['postop']._replace(time='9:30'),
		change='Performed Procedure Step Start Time of the timepoint item replaced after writing')
	return found


def _tracking_uids(ds):
	'''	Tracking UIDs of the measurement groups of an SR document, in document order '''
	found = []

	def walk(items):
		for item in items or []:
			if item.get('ValueType') == 'UIDREF' and item.ConceptNameCodeSequence[0].get('CodeValue') == '112040':
				found.append(str(item.UID))
			walk(item.get('ContentSequence'))
	walk(ds.get('ContentSequence'))
	return found


def manifest(done, payloads):
	'''	The manifest of the packs.

		@input done (OrderedDict): result of `build_all`
		@input payloads (dict): topic -> bytes written
		@returns OrderedDict
	'''
	files = OrderedDict()
	for topic, entry in done.items():
		ds, timepoint = entry['dataset'], entry['timepoint']
		files[fixture_name(topic)] = OrderedDict((
			('pack', entry['pack']), ('role', entry['role']), ('modality', str(ds.Modality)),
			('study_uid', str(ds.StudyInstanceUID)), ('series_uid', str(ds.SeriesInstanceUID)), ('sop_instance_uid', str(ds.SOPInstanceUID)),
			('timepoint', OrderedDict(zip(Timepoint._fields, timepoint)) if timepoint is not None else None),
			('timepoint_items', len(dcm_timepoint_items(ds))),
			('tracking_uids', _tracking_uids(ds)),
			('changed_after_writing', entry['change']),
			('sha256', hashlib.sha256(payloads[topic]).hexdigest()),
		))
	return OrderedDict((
		('contract', 'oak-tree/medical-imaging/sonador-client#50'),
		('timepoint_class_uid', SONADOR_TIMEPOINT_SOP_CLASS_UID),
		('command', COMMAND),
		('versions', versions()),
		('order_cases', [OrderedDict(zip(Timepoint._fields, timepoint)) for timepoint in ORDER_CASES]),
		('files', files),
	))


def versions():
	'''	Versions of the installed pydicom and highdicom distributions. The distribution version is read, not the
		module's `__version__`: a release of the highdicom fork is told from the upstream release it is built on by its
		distribution version only.

		@returns OrderedDict
	'''
	return OrderedDict((name, metadata.version(name)) for name in ('pydicom', 'highdicom'))


def write_fixtures(directory=FIXTURES_DIR):
	'''	Write the packs and their manifest.

		@returns (OrderedDict, OrderedDict): the result of `build_all` and the manifest
	'''
	done = build_all()
	os.makedirs(directory, exist_ok=True)
	payloads = OrderedDict((topic, F.tobytes(entry['dataset'])) for topic, entry in done.items())
	for topic, payload in payloads.items():
		with open(os.path.join(directory, fixture_name(topic)), 'wb') as handle:
			handle.write(payload)
	listing = manifest(done, payloads)
	with open(os.path.join(directory, MANIFEST_FILE), 'w') as handle:
		json.dump(listing, handle, indent=2)
		handle.write('\n')
	return done, listing


if __name__ == '__main__':
	logging.getLogger('sonador').setLevel(logging.CRITICAL)
	target = sys.argv[1] if len(sys.argv) > 1 else FIXTURES_DIR
	_, listing = write_fixtures(target)
	print('%d files and %s written to %s' % (len(listing['files']), MANIFEST_FILE, target))
