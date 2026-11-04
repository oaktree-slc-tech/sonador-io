'''	Tests of the server timestamps of the Orthanc models, offline: `ctime` (the time a resource
	appeared on the imaging server), `mtime` (its last modification there) and `ts` (the date/time
	encoded in the DICOM headers) are three different facts, and the first two cost no request when
	the server already sent them.

	Run from the directory containing the `sonador` package:
		python -m unittest sonador.ftests.test_models.models_timestamps
'''
import datetime, unittest

from sonador.servers import SonadorServer
from sonador.imaging.orthanc import ImagingStudyCollection, ImagingPatientCollection
from sonador.imaging.orthanc.ext import ResourceComment, ext_str2datetime
from sonador.imaging.orthanc.sr import DcmSRSeries, DcmSRSeriesCollection

from ...test.fakes import IMAGESERVER_JSON, FakeResponse

__all__ = ['OrthancResourceTimestampsTestCase', 'ExtensionModelTimestampsTestCase']


RECEIVED_1 = '20260301T090000'
RECEIVED_2 = '20260301T093000'
UPDATED = '20260302T100000'


def instance_json(pk, series, received=None, **tags):
	data = {'ID': pk, 'Type': 'Instance', 'ParentSeries': series, 'IndexInSeries': 1,
		'MainDicomTags': dict({'SOPInstanceUID': '2.25.%s' % pk, 'ContentDate': '20260101', 'ContentTime': '101010'}, **tags)}
	if received:
		data['Metadata'] = {'ReceptionDate': received}
	return data


def series_json(pk, study, instances, modality='CT', **extra):
	return dict({'ID': pk, 'Type': 'Series', 'ParentStudy': study, 'Instances': list(instances), 'IsStable': True,
		'MainDicomTags': {'SeriesInstanceUID': '2.25.%s' % pk, 'Modality': modality, 'SeriesNumber': '1'}}, **extra)


def study_json(pk, series, **extra):
	return dict({'ID': pk, 'Type': 'Study', 'ParentPatient': 'pat-1', 'Series': list(series), 'IsStable': True,
		'MainDicomTags': {'StudyInstanceUID': '2.25.%s' % pk, 'StudyDate': '20260101', 'StudyTime': '101010'},
		'PatientMainDicomTags': {'PatientID': 'MRN-1'}}, **extra)


class OrthancResourceTimestampsTestCase(unittest.TestCase):
	'''	`ctime` / `mtime` / `ts` of patients, studies, series and instances
	'''
	def setUp(self):
		self.server = SonadorServer('https://sonador.invalid', apitoken='token', verify=False)
		self.iserver = self.server.imageserver_modelinstance_from_json(dict(IMAGESERVER_JSON))
		self.requests = []
		self.metadata = {}			# resource pk -> metadata document answered by GET .../metadata
		self.bulk = []				# instance JSON answered by POST /tools/bulk-content
		self.iserver._request_get = self._get
		self.iserver._request_post = self._post

	def _get(self, url, error_msg=None, headers=None, verify=None, **kwargs):
		self.requests.append(('GET', url))
		parts = url.split('://', 1)[-1].split('/', 1)[-1].split('?')[0].strip('/').split('/')
		if len(parts) == 3 and parts[2] == 'metadata':
			return FakeResponse(self.metadata[parts[1]], url=url)
		raise AssertionError('Unexpected GET %s' % url)

	def _post(self, url, error_msg=None, headers=None, verify=None, json=None, **kwargs):
		self.requests.append(('POST', url, json))
		if url.rstrip('/').endswith('/tools/bulk-content'):
			return FakeResponse(self.bulk, url=url)
		raise AssertionError('Unexpected POST %s' % url)

	def study(self, **extra):
		'''	A study as a query result, before any population
		'''
		return self.server._init_dataclass_from_json(ImagingStudyCollection, [study_json('study-1', ['sx-1'], **extra)], pacs=self.iserver)[0]

	def series(self, study, pk='sx-1', instances=('i-1', 'i-2'), **extra):
		sx = study.series_from_json([series_json(pk, study.pk, instances, **extra)])
		study.series_collection = sx
		return sx[0]

	# mtime: the Orthanc LastUpdate metadata

	def test_study_mtime_from_the_resource_json(self):
		'''	Expanded resources carry `LastUpdate`: no request, and `ts` stays the DICOM study time
		'''
		study = self.study(LastUpdate=UPDATED)
		self.assertEqual(study.mtime, datetime.datetime(2026, 3, 2, 10, 0, 0))
		self.assertEqual(study.lastupdate, study.mtime)
		self.assertEqual(study.ts, datetime.datetime(2026, 1, 1, 10, 10, 10))
		self.assertEqual(self.requests, [])

	def test_study_mtime_from_the_metadata_request(self):
		'''	Without `LastUpdate` in the JSON the resource metadata is read once (one request for every key)
		'''
		study = self.study()
		self.metadata['study-1'] = {'LastUpdate': UPDATED, 'MainDicomTagsSignature': '0008,0020'}
		self.assertEqual(study.mtime, datetime.datetime(2026, 3, 2, 10, 0, 0))
		self.assertEqual(study.mtime, datetime.datetime(2026, 3, 2, 10, 0, 0))
		self.assertEqual(len(self.requests), 1)
		self.assertTrue(self.requests[0][1].split('?')[0].endswith('/studies/study-1/metadata'))

	def test_series_mtime_from_the_bulk_metadata_document(self):
		'''	Bulk-content answers carry the metadata as a `Metadata` document
		'''
		study = self.study()
		sx = self.series(study, Metadata={'LastUpdate': UPDATED})
		self.assertEqual(sx.mtime, datetime.datetime(2026, 3, 2, 10, 0, 0))
		self.assertEqual(self.requests, [])

	# ctime of instances: the Orthanc ReceptionDate metadata

	def test_instance_ctime_from_the_bulk_metadata_document(self):
		'''	`ctime` is the reception date; instances are immutable, so `mtime` is the same value; `ts`
			stays the DICOM content time
		'''
		study = self.study()
		sx = self.series(study)
		dcm = sx.dcminstances_from_json([instance_json('i-1', 'sx-1', received=RECEIVED_1)])[0]
		self.assertEqual(dcm.ctime, datetime.datetime(2026, 3, 1, 9, 0, 0))
		self.assertEqual(dcm.mtime, dcm.ctime)
		self.assertEqual(dcm.lastupdate, dcm.ctime)
		self.assertEqual(dcm.ts, datetime.datetime(2026, 1, 1, 10, 10, 10))
		self.assertEqual(self.requests, [])

	def test_instance_ctime_from_the_metadata_request(self):
		study = self.study()
		sx = self.series(study)
		dcm = sx.dcminstances_from_json([instance_json('i-1', 'sx-1')])[0]
		self.metadata['i-1'] = {'ReceptionDate': RECEIVED_1, 'Origin': 'RestApi'}
		self.assertEqual(dcm.ctime, datetime.datetime(2026, 3, 1, 9, 0, 0))
		self.assertEqual(dcm.mtime, datetime.datetime(2026, 3, 1, 9, 0, 0))
		self.assertEqual(len(self.requests), 1)
		self.assertTrue(self.requests[0][1].split('?')[0].endswith('/instances/i-1/metadata'))

	def test_instance_without_reception_date(self):
		study = self.study()
		sx = self.series(study)
		dcm = sx.dcminstances_from_json([instance_json('i-1', 'sx-1')])[0]
		self.metadata['i-1'] = {'Origin': 'RestApi'}
		self.assertIsNone(dcm.ctime)

	# ctime of parents: the earliest reception among the instances

	def test_series_ctime_from_the_cached_instances(self):
		'''	Instances at hand with their reception dates: the earliest one, no request
		'''
		study = self.study()
		sx = self.series(study)
		sx.instances_collection = sx.dcminstances_from_json([
			instance_json('i-2', 'sx-1', received=RECEIVED_2), instance_json('i-1', 'sx-1', received=RECEIVED_1)])
		self.assertEqual(sx.ctime, datetime.datetime(2026, 3, 1, 9, 0, 0))
		self.assertEqual(self.requests, [])

	def test_series_ctime_with_one_bulk_request(self):
		'''	Instances not cached (or cached without their metadata): one bulk-content request at the
			instance level, nothing per instance
		'''
		study = self.study()
		sx = self.series(study)
		self.bulk = [instance_json('i-1', 'sx-1', received=RECEIVED_2), instance_json('i-2', 'sx-1', received=RECEIVED_1)]
		self.assertEqual(sx.ctime, datetime.datetime(2026, 3, 1, 9, 0, 0))
		self.assertEqual(sx.ctime, datetime.datetime(2026, 3, 1, 9, 0, 0))
		self.assertEqual([r[0] for r in self.requests], ['POST'])
		self.assertEqual(self.requests[0][2]['Resources'], ['sx-1'])
		self.assertEqual(self.requests[0][2]['Level'], 'Instance')
		self.assertIsNone(sx.__dict__.get('_slices'))			# the instances were not cached as a side effect

	def test_series_ctime_cached_instances_without_metadata_use_the_bulk_request(self):
		study = self.study()
		sx = self.series(study)
		sx.instances_collection = sx.dcminstances_from_json([instance_json('i-1', 'sx-1'), instance_json('i-2', 'sx-1')])
		self.bulk = [instance_json('i-1', 'sx-1', received=RECEIVED_1), instance_json('i-2', 'sx-1', received=RECEIVED_2)]
		self.assertEqual(sx.ctime, datetime.datetime(2026, 3, 1, 9, 0, 0))
		self.assertEqual([r[0] for r in self.requests], ['POST'])

	def test_study_ctime_walks_the_cached_series(self):
		study = self.study()
		sx = self.series(study)
		sx.instances_collection = sx.dcminstances_from_json([
			instance_json('i-1', 'sx-1', received=RECEIVED_2), instance_json('i-2', 'sx-1', received=RECEIVED_1)])
		self.assertEqual(study.ctime, datetime.datetime(2026, 3, 1, 9, 0, 0))
		self.assertEqual(self.requests, [])

	def test_study_ctime_with_one_bulk_request(self):
		study = self.study()
		self.bulk = [instance_json('i-1', 'sx-1', received=RECEIVED_1)]
		self.assertEqual(study.ctime, datetime.datetime(2026, 3, 1, 9, 0, 0))
		self.assertEqual([r[0] for r in self.requests], ['POST'])
		self.assertEqual(self.requests[0][2]['Resources'], ['study-1'])
		self.assertEqual(self.requests[0][2]['Level'], 'Instance')

	def test_patient_ctime_walks_the_cached_studies(self):
		patient = self.server._init_dataclass_from_json(ImagingPatientCollection,
			[{'ID': 'pat-1', 'Type': 'Patient', 'Studies': ['study-1'], 'MainDicomTags': {'PatientID': 'MRN-1'}, 'LastUpdate': UPDATED}],
			pacs=self.iserver)[0]
		patient.studies_collection = patient.studies_from_json([study_json('study-1', ['sx-1'])])
		study = patient.studies_collection[0]
		sx = self.series(study)
		sx.instances_collection = sx.dcminstances_from_json([instance_json('i-1', 'sx-1', received=RECEIVED_1)])
		self.assertEqual(patient.ctime, datetime.datetime(2026, 3, 1, 9, 0, 0))
		self.assertEqual(patient.mtime, datetime.datetime(2026, 3, 2, 10, 0, 0))
		self.assertEqual(self.requests, [])

	def test_series_without_instances(self):
		study = self.study()
		sx = self.series(study, instances=())
		sx.instances_collection = sx.dcminstances_from_json([])
		self.assertIsNone(sx.ctime)
		self.assertEqual(self.requests, [])

	# SR documents: the same server timestamps, next to the DICOM ones

	def test_sr_instance_timestamps(self):
		'''	An SR instance has `ctime` (reception), `ts` (Content Date/Time) and `instance_creation_ts`
			(Instance Creation Date/Time) as three different values
		'''
		study = self.study()
		series = self.server._init_dataclass_from_json(DcmSRSeriesCollection,
			[series_json('sx-sr', 'study-1', ['sr-1'], modality='SR', Metadata={'LastUpdate': UPDATED})], pacs=self.iserver, study=study)[0]
		self.assertIsInstance(series, DcmSRSeries)
		dcm = series.dcminstances_from_json([instance_json('sr-1', 'sx-sr', received=RECEIVED_2,
			InstanceCreationDate='20260102', InstanceCreationTime='080000')])[0]
		series.instances_collection = series.dcminstances_from_json([dcm._objectdata])
		self.assertEqual(dcm.ctime, datetime.datetime(2026, 3, 1, 9, 30, 0))
		self.assertEqual(dcm.mtime, dcm.ctime)
		self.assertEqual(dcm.ts, datetime.datetime(2026, 1, 1, 10, 10, 10))
		self.assertEqual(dcm.instance_creation_ts, datetime.datetime(2026, 1, 2, 8, 0, 0))
		self.assertEqual(series.ctime, datetime.datetime(2026, 3, 1, 9, 30, 0))
		self.assertEqual(series.mtime, datetime.datetime(2026, 3, 2, 10, 0, 0))
		self.assertEqual(self.requests, [])


class ExtensionModelTimestampsTestCase(unittest.TestCase):
	'''	`ctime` / `mtime` of the extension models (comments, worklist items, tags): `Created` and
		`LastUpdate` of the extension APIs
	'''
	def setUp(self):
		self.server = SonadorServer('https://sonador.invalid', apitoken='token', verify=False)
		self.iserver = self.server.imageserver_modelinstance_from_json(dict(IMAGESERVER_JSON))
		self.study = self.server._init_dataclass_from_json(ImagingStudyCollection, [study_json('study-1', [])], pacs=self.iserver)[0]

	def comment(self, **data):
		return ResourceComment(self.server, dict({'ID': 'c-1', 'Text': 'note'}, **data), parent=self.study)

	def test_created_and_last_update(self):
		comment = self.comment(Created='2026-10-18T10:00:00Z', LastUpdate='2026-10-18T11:30:00.250000Z')
		self.assertEqual(comment.ctime, datetime.datetime(2026, 10, 18, 10, 0, 0))
		self.assertEqual(comment.mtime, datetime.datetime(2026, 10, 18, 11, 30, 0, 250000))

	def test_missing_timestamps(self):
		comment = self.comment()
		self.assertIsNone(comment.ctime)
		self.assertIsNone(comment.mtime)

	def test_offsets_are_read_as_utc(self):
		self.assertEqual(ext_str2datetime('2026-10-18T12:00:00+02:00'), datetime.datetime(2026, 10, 18, 10, 0, 0))
		self.assertEqual(ext_str2datetime('2026-10-18 10:00:00'), datetime.datetime(2026, 10, 18, 10, 0, 0))
		self.assertIsNone(ext_str2datetime('not a time'))
		self.assertIsNone(ext_str2datetime(''))
		self.assertEqual(ext_str2datetime(datetime.date(2026, 10, 18)), datetime.date(2026, 10, 18))

	def test_invalid_dates_in_recognised_forms_read_as_none(self):
		'''	A value in a recognised form whose calendar date is invalid is read as None, not raised:
			the ISO form with fractional seconds and Z, the offset form, the space-separated form and
			the Orthanc form, through the helper and through the model properties
		'''
		for value in ('2026-13-01T10:00:00.000000Z', '2026-02-30T10:00:00Z', '2026-02-30T10:00:00+02:00',
				'2026-02-30 10:00:00', '20261301T100000'):
			with self.assertLogs('sonador.imaging.orthanc.ext', level='WARNING'):
				self.assertIsNone(ext_str2datetime(value), value)
		comment = self.comment(Created='2026-13-01T10:00:00.000000Z', LastUpdate='2026-02-30 10:00:00')
		with self.assertLogs('sonador.imaging.orthanc.ext', level='WARNING'):
			self.assertIsNone(comment.ctime)
		with self.assertLogs('sonador.imaging.orthanc.ext', level='WARNING'):
			self.assertIsNone(comment.mtime)
