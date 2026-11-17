'''	Offline tests of the retrieval path and request budget of the findings API against a mocked
	imaging server.

	A study with K series (one CT, M=2 SR series holding N=3 SR instances) is served from canned
	JSON through the bulk-content endpoint and from in-memory documents through the instance `file`
	endpoint. Every request the models make is recorded, so the tests assert the budget: one `file`
	request per SR instance parsed (the current document of each SR series) and no other request,
	none on a second access, the requests inside the bulk call with `parse_sr=True`, parents wired
	without a study GET, and evidence for a new Series Tag document synthesised without downloads.
	The evidence of a second reference series whose instance record lacks a SOP UID is resolved from
	the record, then the simplified tags, then the file, with the request each step costs.

	Run from the directory containing the `sonador` package: python -m unittest sonador.ftests.test_sr.sr_retrieval
'''
import io, json, logging, unittest
from collections import OrderedDict
from types import SimpleNamespace

import pydicom
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.sr.codedict import codes
from pydicom.uid import ExplicitVRLittleEndian

from highdicom.sr import CodedConcept

from ...servers import SonadorServer
from ...apisettings.base import Finding, ImageReference
from ...apisettings.sr.seriestag import SONADOR_MEASUREMENT_TYPE_SERIES_TAG
from ...imaging.orthanc import ImagingStudyCollection, ImagingSeriesCollection, ImagingStudy, ImagingSeries
from ...imaging.orthanc.base import DcmInstance
from ...imaging.orthanc.sr import DcmSRSeriesCollection, DcmSRSeries, DcmSRInstance
from ...imaging.helpers.sr import SonadorComprehensiveSR3D, SonadorImagingMeasurementReport
from ...test.fakes import IMAGESERVER_JSON, FakeResponse
from .sr_seriestag import new_model, synthetic_reference, roundtrip, tobytes, TAG_REJECT, TAG_METAL, TAG_ACCEPT, SYNTHETIC, PROCEDURE, \
	MR_IMAGE_STORAGE


__all__ = ['RetrievalBudgetTestCase', 'EvidenceUidBudgetTestCase']


def setUpModule():
	logging.getLogger('sonador').setLevel(logging.CRITICAL)


def tearDownModule():
	logging.getLogger('sonador').setLevel(logging.NOTSET)


class FakeBytesResponse:
	'''	Stand-in for requests.Response carrying binary content
	'''
	def __init__(self, content, url):
		self.content = content
		self.status_code = 200
		self.ok = True
		self.headers = {'Content-Type': 'application/dicom'}
		self.request = SimpleNamespace(url=url)


PATIENT_TAGS = {'PatientID': SYNTHETIC['PatientID'], 'PatientName': SYNTHETIC['PatientName'], 'PatientBirthDate': '', 'PatientSex': ''}
STUDY_TAGS = {'StudyInstanceUID': SYNTHETIC['StudyInstanceUID'], 'StudyID': '1', 'StudyDate': '20260101', 'StudyTime': '090000',
	'AccessionNumber': '', 'StudyDescription': 'Synthetic study'}

CT_SERIES_UID, SR_A_UID, SR_B_UID = SYNTHETIC['SeriesInstanceUID'], '2.25.500', '2.25.600'
X, Y, Z = '2.25.1', '2.25.2', '2.25.3'

# Second reference series of the evidence tests: one instance with its Frame of Reference UID in the main record
MR_SERIES_UID, MR_SOP_UID, MR_FRAME_UID = '2.25.700', '2.25.7001', '2.25.7000'
ENHANCED_MR_IMAGE_STORAGE = '1.2.840.10008.5.1.4.1.1.4.1'
SECONDARY_CAPTURE_STORAGE = '1.2.840.10008.5.1.4.1.1.7'


def instance_file(sop_class, sop_uid, series_uid, modality, frame_uid=None):
	'''	Bytes of a synthetic image instance of the study, without pixel data. A None `sop_class` or
		`sop_uid` leaves that UID out of the dataset and of the file meta information.

		@input sop_class (str), sop_uid (str), series_uid (str), modality (str): its identity
		@input frame_uid (str, default=None): Frame of Reference UID
		@returns bytes
	'''
	ds = Dataset()
	ds.file_meta = FileMetaDataset()
	ds.file_meta.MediaStorageSOPClassUID, ds.file_meta.MediaStorageSOPInstanceUID = sop_class, sop_uid
	ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
	ds.SOPClassUID, ds.SOPInstanceUID = sop_class, sop_uid
	ds.PatientName, ds.PatientID, ds.PatientBirthDate, ds.PatientSex = SYNTHETIC['PatientName'], SYNTHETIC['PatientID'], '', ''
	ds.StudyInstanceUID, ds.StudyID, ds.StudyDate, ds.StudyTime = SYNTHETIC['StudyInstanceUID'], '1', '20260101', '090000'
	ds.AccessionNumber, ds.ReferringPhysicianName = '', ''
	ds.SeriesInstanceUID, ds.Modality = series_uid, modality
	if frame_uid:
		ds.FrameOfReferenceUID = frame_uid
	for name, value in (('SOPClassUID', sop_class), ('SOPInstanceUID', sop_uid)):
		if value is None:
			delattr(ds, name)
			delattr(ds.file_meta, 'MediaStorage' + name)
	buf = io.BytesIO()
	if sop_class and sop_uid:
		ds.save_as(buf, enforce_file_format=True)
	else:
		# The file meta information lacks a required element: written as is, with the preamble
		ds.preamble = b'\0' * 128
		ds.save_as(buf, enforce_file_format=False)
	return buf.getvalue()


class MockPacs:
	'''	Canned Orthanc resources of one patient / study and the requests the models make
	'''
	def __init__(self, iserver):
		self.iserver = iserver
		self.requests = []
		self.files = {}
		self.resources = OrderedDict()

		# Documents: SR series A with two revisions (X+Y, then X), independent SR series B (Z)
		docs = {
			'sr-a1': self.document([self.finding(TAG_REJECT, X), self.finding(TAG_METAL, Y)], SR_A_UID, '100000', '2.25.5001'),
			'sr-a2': self.document([self.finding(TAG_REJECT, X)], SR_A_UID, '110000', '2.25.5002'),
			'sr-b1': self.document([self.finding(TAG_ACCEPT, Z)], SR_B_UID, '100000', '2.25.6001'),
		}
		self.add('Patient', 'pat-1', PATIENT_TAGS, Studies=['study-1'])
		self.add('Study', 'study-1', STUDY_TAGS, PatientMainDicomTags=PATIENT_TAGS, ParentPatient='pat-1',
			Series=['sx-ct', 'sx-sra', 'sx-srb'])
		self.add('Series', 'sx-ct', {'SeriesInstanceUID': CT_SERIES_UID, 'Modality': 'CT', 'SeriesNumber': '1'},
			ParentStudy='study-1', Instances=['dcm-ct-1', 'dcm-ct-2'])
		self.add('Series', 'sx-sra', {'SeriesInstanceUID': SR_A_UID, 'Modality': 'SR', 'SeriesNumber': '44'},
			ParentStudy='study-1', Instances=['sr-a1', 'sr-a2'])
		self.add('Series', 'sx-srb', {'SeriesInstanceUID': SR_B_UID, 'Modality': 'SR', 'SeriesNumber': '45'},
			ParentStudy='study-1', Instances=['sr-b1'])
		for idx, (pk, sop) in enumerate((('dcm-ct-1', SYNTHETIC['SOPInstanceUID']), ('dcm-ct-2', '2.25.1004')), 1):
			self.add('Instance', pk, {'SOPInstanceUID': sop, 'SOPClassUID': SYNTHETIC['SOPClassUID'], 'InstanceNumber': str(idx)},
				ParentSeries='sx-ct', IndexInSeries=idx)
			self.files[pk] = instance_file(SYNTHETIC['SOPClassUID'], sop, CT_SERIES_UID, 'CT')
		for pk, series in (('sr-a1', 'sx-sra'), ('sr-a2', 'sx-sra'), ('sr-b1', 'sx-srb')):
			doc = docs[pk]
			self.add('Instance', pk, {'SOPInstanceUID': str(doc.SOPInstanceUID), 'SOPClassUID': str(doc.SOPClassUID),
				'ContentDate': doc.ContentDate, 'ContentTime': doc.ContentTime, 'InstanceNumber': '1'},
				ParentSeries=series, IndexInSeries=1 if pk != 'sr-a2' else 2)
			self.files[pk] = tobytes(doc)

		# Simplified tags served per instance; instances in `failing_tags` answer with a server error
		self.tags = {}
		self.failing_tags = set()

		iserver._request_get = self.get
		iserver._request_post = self.post

	def add_second_series(self, record, tags=None, sop_class=MR_IMAGE_STORAGE, sop_uid=MR_SOP_UID):
		'''	Add an MR series with one instance to the study: its main record holds the headers in
			`record`, its simplified tags (when given) those in `tags`, and its file all of them
			(a None `sop_class` or `sop_uid` leaves that UID out of the file)
		'''
		self.resources['study-1']['Series'].append('sx-mr')
		self.add('Series', 'sx-mr', {'SeriesInstanceUID': MR_SERIES_UID, 'Modality': 'MR', 'SeriesNumber': '2'},
			ParentStudy='study-1', Instances=['dcm-mr-1'])
		self.add('Instance', 'dcm-mr-1', dict(record, InstanceNumber='1'), ParentSeries='sx-mr', IndexInSeries=1)
		if tags is not None:
			self.tags['dcm-mr-1'] = dict(tags)
		self.files['dcm-mr-1'] = instance_file(sop_class, sop_uid, MR_SERIES_UID, 'MR', MR_FRAME_UID)

	@staticmethod
	def finding(tag, uid):
		return Finding.series_tag(tag, reference=synthetic_reference(), tracking_uid=uid)

	@staticmethod
	def document(findings, series_uid, content_time, sop_uid):
		doc = new_model(findings).create_sr(record_instances=True, dcmsr_series_uid=series_uid, dcmsr_instance_uid=sop_uid, previous=None)
		doc.ContentDate, doc.ContentTime = '20260101', content_time
		return roundtrip(doc)

	def add(self, rtype, pk, tags, **extra):
		resource = {'ID': pk, 'Type': rtype, 'MainDicomTags': dict(tags), 'IsStable': True}
		resource.update(extra)
		self.resources[pk] = resource

	# Request handlers

	def get(self, url, error_msg=None, headers=None, verify=None, **kwargs):
		self.requests.append(('GET', url))
		path = url.split('://', 1)[-1].split('/', 1)[-1].split('?')[0]
		parts = path.strip('/').split('/')
		if len(parts) == 3 and parts[0] == 'instances' and parts[2] == 'file':
			return FakeBytesResponse(self.files[parts[1]], url)
		if len(parts) == 2 and parts[0] in ('patients', 'studies', 'series', 'instances'):
			return FakeResponse(self.resources[parts[1]], url=url)
		if len(parts) == 3 and parts[2] == 'simplified-tags':
			if parts[1] in self.failing_tags:
				r = FakeResponse({'Message': 'Unavailable'}, url=url)
				r.status_code, r.ok = 503, False
				error_msg(r)
				raise AssertionError('The failed tag request for %s raised no error' % parts[1])
			if parts[1] in self.tags:
				return FakeResponse(self.tags[parts[1]], url=url)
			raise AssertionError('Unexpected simplified-tags request for %s' % parts[1])
		raise AssertionError('Unexpected GET %s' % url)

	def post(self, url, error_msg=None, headers=None, verify=None, json=None, **kwargs):
		self.requests.append(('POST', url))
		if url.rstrip('/').endswith('/tools/bulk-content'):
			found = []
			for pk in json.get('Resources', []):
				resource = self.resources.get(pk)
				if resource is None:
					continue
				found.append(resource)
				if json.get('IncludeInstances') and resource['Type'] == 'Series':
					found.extend(self.resources[i] for i in resource['Instances'])
			return FakeResponse(found, url=url)
		raise AssertionError('Unexpected POST %s' % url)

	def count(self, method=None, suffix=None):
		return len([r for r in self.requests
			if (method is None or r[0] == method) and (suffix is None or r[1].split('?')[0].endswith(suffix))])

	def file_requests(self):
		return [r[1].split('/')[-2] for r in self.requests if r[0] == 'GET' and r[1].split('?')[0].endswith('/file')]


class RetrievalBudgetTestCase(unittest.TestCase):
	def setUp(self):
		self.server = SonadorServer('https://sonador.invalid', apitoken='token', verify=False)
		self.iserver = self.server.imageserver_modelinstance_from_json(dict(IMAGESERVER_JSON))
		self.pacs = MockPacs(self.iserver)

	def studies(self):
		'''	The study as a query result (one study collection), before bulk population
		'''
		return self.server._init_dataclass_from_json(ImagingStudyCollection, [dict(self.pacs.resources['study-1'])], pacs=self.iserver)

	def assert_findings(self, study):
		findings = {}
		for series in study.series_collection:
			findings[series.pk] = series.findings(SONADOR_MEASUREMENT_TYPE_SERIES_TAG)
		self.assertEqual(sorted(f.tracking_uid for f in findings['sx-ct']), sorted([X, Z]))
		self.assertEqual(len(findings['sx-sra']), 0)
		self.assertEqual(len(findings['sx-srb']), 0)
		return findings

	def test_study_downward_budget(self):
		'''	After bulk population, findings for every series of the study cost one `file` request per
			SR series (its current instance) and nothing else; a second access costs nothing
		'''
		studies = self.studies()
		studies.bulkpopulate_related(child_instances=True)
		self.assertEqual([r[0] for r in self.pacs.requests], ['POST'])
		study = studies[0]
		self.assertIs(study.sr_collection.parent, study)
		self.assertEqual([sx.pk for sx in study.sr_collection], ['sx-sra', 'sx-srb'])

		findings = self.assert_findings(study)
		self.assertEqual(sorted(self.pacs.file_requests()), ['sr-a2', 'sr-b1'])
		self.assertEqual(self.pacs.count('POST'), 1)
		self.assertEqual(self.pacs.count('GET'), 2)

		# Second access: nothing
		before = len(self.pacs.requests)
		self.assert_findings(study)
		study.findings()
		self.assertEqual(len(self.pacs.requests), before)

		# Links on the data classes: series, instance and SR instance resolved from the cache
		for finding in findings['sx-ct']:
			self.assertIsInstance(finding.series, ImagingSeries)
			self.assertEqual(finding.series.pk, 'sx-ct')
			self.assertEqual(finding.instance.pk, 'dcm-ct-1')
			self.assertIsInstance(finding.sr_instance, DcmSRInstance)
			self.assertIs(finding.series.parent, study)
		self.assertEqual(len(self.pacs.requests), before)

		# Study-level shapes
		by_series = study.findings()
		self.assertEqual(list(by_series.keys()), [CT_SERIES_UID])
		self.assertEqual(sorted(f.tracking_uid for f in study.findings(CT_SERIES_UID, 'series-tag')), sorted([X, Z]))
		self.assertEqual(study.sr_collection.diagnostics, [])

	def test_parse_sr_inside_the_bulk_call(self):
		'''	Eager parsing follows the current-document contract: one `file` request per SR series
			(the superseded revision is not fetched), nothing on later findings access; a revision
			history is a further explicit request per superseded instance
		'''
		studies = self.studies()
		studies.bulkpopulate_related(child_instances=True, parse_sr=True)
		self.assertEqual(sorted(self.pacs.file_requests()), ['sr-a2', 'sr-b1'])
		before = len(self.pacs.requests)
		self.assert_findings(studies[0])
		self.assertEqual(len(self.pacs.requests), before)
		# The history is reachable; the superseded revision costs its one request, once
		findings = studies[0].findings(CT_SERIES_UID, 'series-tag', all_revisions=True)
		self.assertEqual(sorted(f.tracking_uid for f in findings), sorted([X, X, Y, Z]))
		self.assertEqual(sorted(self.pacs.file_requests()), ['sr-a1', 'sr-a2', 'sr-b1'])
		after = len(self.pacs.requests)
		studies[0].findings(CT_SERIES_UID, 'series-tag', all_revisions=True)
		self.assertEqual(len(self.pacs.requests), after)
		# `parse_sr(all_revisions=True)` is the explicit way to load the history eagerly
		studies[0].parse_sr(all_revisions=True)
		self.assertEqual(len(self.pacs.requests), after)

	def test_series_upward_budget(self):
		'''	A series obtained by query populates its parent study and siblings with the two bulk
			requests; the parent then serves the findings from the cache
		'''
		series = self.server._init_dataclass_from_json(ImagingSeriesCollection, [dict(self.pacs.resources['sx-ct'])], pacs=self.iserver)
		series.bulkpopulate_related(child_instances=True)
		self.assertEqual([r[0] for r in self.pacs.requests], ['POST', 'POST'])
		sx = series[0]
		self.assertIsInstance(sx.parent, ImagingStudy)
		self.assertIs(sx.parent.sr_collection.parent, sx.parent)
		findings = sx.findings('series-tag')
		self.assertEqual(sorted(f.tracking_uid for f in findings), sorted([X, Z]))
		self.assertEqual(sorted(self.pacs.file_requests()), ['sr-a2', 'sr-b1'])
		self.assertEqual(self.pacs.count('POST'), 2)
		self.assertEqual(self.pacs.count('GET'), 2)
		before = len(self.pacs.requests)
		sx.parent.findings(sx.series_uid)
		self.assertEqual(len(self.pacs.requests), before)

	def test_typed_collection_takes_a_study_parent(self):
		'''	A DcmSRSeriesCollection rebuilt from a query result with `study=` never triggers a study GET
		'''
		studies = self.studies()
		studies.bulkpopulate_related(child_instances=True)
		study = studies[0]
		rebuilt = self.server._init_dataclass_from_json(DcmSRSeriesCollection,
			[sx._objectdata for sx in study.sr_collection], pacs=self.iserver, study=study)
		before = len(self.pacs.requests)
		for sx in rebuilt:
			self.assertIs(sx.parent, study)
		self.assertEqual(len(self.pacs.requests), before)

	def test_report_model_binds_to_an_existing_series(self):
		'''	`SonadorBaseSR(sonador_series=<DcmSRSeries>)` binds without error and `ref_series` resolves
			from the parent's cached series collection
		'''
		studies = self.studies()
		studies.bulkpopulate_related(child_instances=True)
		study = studies[0]
		sr_series = study.sr_collection.get_modelinstance('sx-sra')
		study.findings()		# parses the current documents
		before = len(self.pacs.requests)
		model = SonadorComprehensiveSR3D(sonador_series=sr_series, report_procedure=PROCEDURE)
		self.assertIs(model.sr, sr_series)
		self.assertEqual([sx.pk for sx in model.ref_series_list], ['sx-ct'])
		self.assertEqual(len(self.pacs.requests), before)
		# The parsed report of the current instance is bound to the series as well
		report = sr_series.instances_collection.current_instance().report
		self.assertIs(report.series, sr_series)
		self.assertEqual([sx.pk for sx in report.ref_series_list], ['sx-ct'])
		self.assertEqual(sorted(sr_series.reference_series_collection[0].series_uid for _ in [0]), [CT_SERIES_UID])

	def test_create_sr_without_file_requests(self):
		'''	`create_sr` for a Series Tag on a bulk-populated study synthesises the evidence from the
			cached JSON and makes no `file` request
		'''
		studies = self.studies()
		studies.bulkpopulate_related(child_instances=True)
		study = studies[0]
		ct = study.series_collection.get_modelinstance('sx-ct')
		before = len(self.pacs.requests)
		model = SonadorImagingMeasurementReport(sonador_ref_series=ct, findings=[Finding.series_tag(TAG_ACCEPT)],
			report_procedure=PROCEDURE, record_instances=False, person_observer='Observer^Test')
		doc = roundtrip(model.create_sr())
		self.assertEqual(len(self.pacs.requests), before)
		self.assertEqual(str(doc.StudyInstanceUID), SYNTHETIC['StudyInstanceUID'])
		self.assertEqual(doc.PatientID, SYNTHETIC['PatientID'])
		evidence = doc.CurrentRequestedProcedureEvidenceSequence[0].ReferencedSeriesSequence[0]
		self.assertEqual(str(evidence.SeriesInstanceUID), CT_SERIES_UID)
		self.assertEqual([str(s.ReferencedSOPInstanceUID) for s in evidence.ReferencedSOPSequence], [SYNTHETIC['SOPInstanceUID']])
		parsed = SonadorComprehensiveSR3D.from_sr(doc)
		finding = [f for g in parsed.groups for f in g.findings][0]
		self.assertEqual(finding.reference,
			ImageReference(SYNTHETIC['SOPClassUID'], SYNTHETIC['SOPInstanceUID'], None, CT_SERIES_UID, SYNTHETIC['StudyInstanceUID']))
		self.assertNotIn('PertinentOtherEvidenceSequence', doc)

	def test_invalidate_findings(self):
		studies = self.studies()
		studies.bulkpopulate_related(child_instances=True)
		study = studies[0]
		study.findings()
		self.assertEqual(self.pacs.count('GET', '/file'), 2)
		study.invalidate_findings()
		study.findings()
		# The files stay cached on the instances: a re-parse costs no request
		self.assertEqual(self.pacs.count('GET', '/file'), 2)


class SopPropertyInstance:
	'''	Instance adapter that supplies its UIDs through the public properties only: no main record,
		no tag accessor and no caches. Reading its file fails the test.
	'''
	def __init__(self, sop_class, sop_instance):
		self._sop = (sop_class, sop_instance)

	@property
	def sop_class_uid(self):
		return self._sop[0]

	@property
	def sop_instance_uid(self):
		return self._sop[1]

	def dcmfile(self, cache=False, **kwargs):
		raise AssertionError('The file of an instance whose properties hold both UIDs was read')


class ResolvedSopInstance(DcmInstance):
	'''	Orthanc instance whose UID properties resolve values its main record lacks
	'''
	resolved = {}

	@property
	def sop_class_uid(self):
		return self.resolved.get('SOPClassUID')

	@property
	def sop_instance_uid(self):
		return self.resolved.get('SOPInstanceUID')


class EvidenceUidBudgetTestCase(unittest.TestCase):
	'''	Evidence of a Series Tag document over two reference series written with
		`record_instances=False`, where the record of the second series' instance may lack a SOP UID:
		the UIDs come from the record, then the simplified tags, then the file, and each step costs
		exactly its request
	'''
	def setUp(self):
		self.server = SonadorServer('https://sonador.invalid', apitoken='token', verify=False)
		self.iserver = self.server.imageserver_modelinstance_from_json(dict(IMAGESERVER_JSON))
		self.pacs = MockPacs(self.iserver)

	def populate(self, record, tags=None, sop_class=MR_IMAGE_STORAGE, sop_uid=MR_SOP_UID):
		'''	Add the second series and bulk-populate the study

			@returns tuple (ImagingSeries, ImagingSeries): the CT and the MR series
		'''
		self.pacs.add_second_series(record, tags, sop_class=sop_class, sop_uid=sop_uid)
		studies = self.server._init_dataclass_from_json(ImagingStudyCollection, [dict(self.pacs.resources['study-1'])], pacs=self.iserver)
		studies.bulkpopulate_related(child_instances=True)
		study = studies[0]
		return study.series_collection.get_modelinstance('sx-ct'), study.series_collection.get_modelinstance('sx-mr')

	def write(self, ct, mr, record_instances=False, tag_mr=True):
		'''	Write a Series Tag on the CT series, and on the MR series unless `tag_mr` is false, with
			both as reference series and the evidence mode set on the model; serialize and re-read
			the document

			@returns tuple (pydicom.Dataset, list): the re-read document and the requests the write made
		'''
		before = len(self.pacs.requests)
		findings = [Finding.series_tag(TAG_ACCEPT, series_uid=CT_SERIES_UID)]
		if tag_mr:
			findings.append(Finding.series_tag(TAG_METAL, series_uid=MR_SERIES_UID))
		model = SonadorImagingMeasurementReport(sonador_ref_series=[ct, mr], findings=findings,
			report_procedure=PROCEDURE, record_instances=record_instances, person_observer='Observer^Test')
		doc = roundtrip(model.create_sr())
		return doc, self.pacs.requests[before:]

	def assert_mr_evidence(self, doc, sop_class=MR_IMAGE_STORAGE, frame=None, ct_instances=(SYNTHETIC['SOPInstanceUID'],)):
		'''	The re-read document records the MR instance with the correct SOP class and instance in
			its evidence, and the Series Tag on the MR series references it
		'''
		series = dict((str(item.SeriesInstanceUID), item)
			for item in doc.CurrentRequestedProcedureEvidenceSequence[0].ReferencedSeriesSequence)
		self.assertEqual(sorted(series), sorted([CT_SERIES_UID, MR_SERIES_UID]))
		self.assertEqual([(str(s.ReferencedSOPClassUID), str(s.ReferencedSOPInstanceUID)) for s in series[MR_SERIES_UID].ReferencedSOPSequence],
			[(sop_class, MR_SOP_UID)])
		self.assertEqual(sorted((str(s.ReferencedSOPClassUID), str(s.ReferencedSOPInstanceUID)) for s in series[CT_SERIES_UID].ReferencedSOPSequence),
			sorted((SYNTHETIC['SOPClassUID'], uid) for uid in ct_instances))
		parsed = SonadorComprehensiveSR3D.from_sr(doc)
		references = dict((f.reference.series_uid, f.reference) for g in parsed.groups for f in g.findings)
		self.assertEqual(references[MR_SERIES_UID], ImageReference(sop_class, MR_SOP_UID, frame, MR_SERIES_UID, SYNTHETIC['StudyInstanceUID']))

	@staticmethod
	def kinds(requests):
		return [r[1].split('?')[0].rstrip('/').split('/')[-1] for r in requests]

	def test_warm_tags_complete_the_record(self):
		'''	SOP Class UID only in simplified tags warmed on the same instance object: no request
		'''
		ct, mr = self.populate({'SOPInstanceUID': MR_SOP_UID, 'FrameOfReferenceUID': MR_FRAME_UID},
			tags={'SOPInstanceUID': MR_SOP_UID, 'SOPClassUID': MR_IMAGE_STORAGE, 'FrameOfReferenceUID': MR_FRAME_UID})
		self.assertEqual(mr.dcm0.tags['SOPClassUID'], MR_IMAGE_STORAGE)
		self.assertEqual(self.pacs.count('GET', '/simplified-tags'), 1)
		doc, requests = self.write(ct, mr)
		self.assertEqual(requests, [])
		self.assertEqual(self.pacs.file_requests(), [])
		self.assert_mr_evidence(doc)

	def test_cold_tags_cost_one_tag_request(self):
		'''	Tags not yet cached: one `simplified-tags` request in place of the file, cached afterwards
		'''
		ct, mr = self.populate({'SOPInstanceUID': MR_SOP_UID, 'FrameOfReferenceUID': MR_FRAME_UID},
			tags={'SOPInstanceUID': MR_SOP_UID, 'SOPClassUID': MR_IMAGE_STORAGE})
		doc, requests = self.write(ct, mr)
		self.assertEqual(self.kinds(requests), ['simplified-tags'])
		self.assertEqual(self.pacs.file_requests(), [])
		self.assert_mr_evidence(doc)
		self.assertEqual(mr.dcm0.tags['SOPClassUID'], MR_IMAGE_STORAGE)
		self.assertEqual(self.pacs.count('GET', '/simplified-tags'), 1)

	def test_uids_in_neither_record_nor_tags_read_the_file(self):
		'''	Tags that also lack the SOP Class UID: the file is read once, as before
		'''
		ct, mr = self.populate({'SOPInstanceUID': MR_SOP_UID}, tags={'SOPInstanceUID': MR_SOP_UID})
		doc, requests = self.write(ct, mr)
		self.assertEqual(self.kinds(requests), ['simplified-tags', 'file'])
		self.assertEqual(self.pacs.file_requests(), ['dcm-mr-1'])
		self.assert_mr_evidence(doc)

	def test_failed_tag_request_reads_the_file(self):
		'''	A tag request the server refuses falls back to the file, and the reference is correct
		'''
		ct, mr = self.populate({'FrameOfReferenceUID': MR_FRAME_UID})
		self.pacs.failing_tags.add('dcm-mr-1')
		doc, requests = self.write(ct, mr)
		self.assertEqual(self.kinds(requests), ['simplified-tags', 'file'])
		self.assertEqual(self.pacs.file_requests(), ['dcm-mr-1'])
		self.assert_mr_evidence(doc)

	def test_record_with_both_uids_makes_no_request(self):
		'''	A main record holding both UIDs: no request at all
		'''
		ct, mr = self.populate({'SOPInstanceUID': MR_SOP_UID, 'SOPClassUID': MR_IMAGE_STORAGE, 'FrameOfReferenceUID': MR_FRAME_UID})
		doc, requests = self.write(ct, mr)
		self.assertEqual(requests, [])
		self.assert_mr_evidence(doc)

	def test_multiframe_class_from_tags(self):
		'''	A multiframe SOP class read from the tags gives the reference frame number 1
		'''
		ct, mr = self.populate({'SOPInstanceUID': MR_SOP_UID, 'FrameOfReferenceUID': MR_FRAME_UID},
			tags={'SOPInstanceUID': MR_SOP_UID, 'SOPClassUID': ENHANCED_MR_IMAGE_STORAGE}, sop_class=ENHANCED_MR_IMAGE_STORAGE)
		doc, requests = self.write(ct, mr)
		self.assertEqual(self.kinds(requests), ['simplified-tags'])
		self.assert_mr_evidence(doc, sop_class=ENHANCED_MR_IMAGE_STORAGE, frame=1)


	def test_adapter_properties_make_no_request(self):
		'''	An instance adapter that supplies both UIDs through its properties, with no main record or
			caches: no request, and the reference is correct
		'''
		ct, mr = self.populate({})
		mr._dcm0 = SopPropertyInstance(MR_IMAGE_STORAGE, MR_SOP_UID)
		doc, requests = self.write(ct, mr)
		self.assertEqual(requests, [])
		self.assert_mr_evidence(doc)

	def test_instance_subclass_properties_make_no_request(self):
		'''	A DcmInstance subclass whose properties resolve both UIDs its main record lacks: no request
		'''
		ct, mr = self.populate({'FrameOfReferenceUID': MR_FRAME_UID})
		mr.dcm0.__class__ = type('BothResolved', (ResolvedSopInstance,),
			{'resolved': {'SOPClassUID': MR_IMAGE_STORAGE, 'SOPInstanceUID': MR_SOP_UID}})
		doc, requests = self.write(ct, mr)
		self.assertEqual(requests, [])
		self.assert_mr_evidence(doc)

	def test_partial_properties_are_completed(self):
		'''	Properties that resolve only the SOP Class UID: the tags supply the SOP Instance UID
			alone, and the class from the property is kept although the tags name another
		'''
		ct, mr = self.populate({'FrameOfReferenceUID': MR_FRAME_UID},
			tags={'SOPInstanceUID': MR_SOP_UID, 'SOPClassUID': SECONDARY_CAPTURE_STORAGE})
		mr.dcm0.__class__ = type('ClassResolved', (ResolvedSopInstance,), {'resolved': {'SOPClassUID': MR_IMAGE_STORAGE}})
		doc, requests = self.write(ct, mr)
		self.assertEqual(self.kinds(requests), ['simplified-tags'])
		self.assert_mr_evidence(doc)

	def test_full_evidence_reads_the_file_without_a_tag_request(self):
		'''	`record_instances=True` on the model: the SOP Class UID missing from the record comes from
			the file the evidence downloads anyway; every instance file is requested once and no
			tags are requested, as before the tag lookup existed
		'''
		ct, mr = self.populate({'SOPInstanceUID': MR_SOP_UID, 'FrameOfReferenceUID': MR_FRAME_UID},
			tags={'SOPInstanceUID': MR_SOP_UID, 'SOPClassUID': MR_IMAGE_STORAGE})
		doc, requests = self.write(ct, mr, record_instances=True)
		self.assertEqual(self.pacs.count('GET', '/simplified-tags'), 0)
		self.assertEqual(self.kinds(requests), ['file'] * 3)
		self.assertEqual(sorted(self.pacs.file_requests()), ['dcm-ct-1', 'dcm-ct-2', 'dcm-mr-1'])
		self.assert_mr_evidence(doc, ct_instances=(SYNTHETIC['SOPInstanceUID'], '2.25.1004'))

	def test_exchange_profile_on_the_same_input_requests_the_tags(self):
		'''	The same input with `record_instances=False` on the model: one tag request, no file
		'''
		ct, mr = self.populate({'SOPInstanceUID': MR_SOP_UID, 'FrameOfReferenceUID': MR_FRAME_UID},
			tags={'SOPInstanceUID': MR_SOP_UID, 'SOPClassUID': MR_IMAGE_STORAGE})
		doc, requests = self.write(ct, mr, record_instances=False)
		self.assertEqual(self.kinds(requests), ['simplified-tags'])
		self.assertEqual(self.pacs.file_requests(), [])
		self.assert_mr_evidence(doc)

	def test_incomplete_warm_tags_go_to_the_file(self):
		'''	Tags already cached that lack the SOP Class UID: the file is read, no further tag request
		'''
		ct, mr = self.populate({'SOPInstanceUID': MR_SOP_UID}, tags={'SOPInstanceUID': MR_SOP_UID})
		mr.dcm0.tags
		doc, requests = self.write(ct, mr)
		self.assertEqual(self.kinds(requests), ['file'])
		self.assert_mr_evidence(doc)

	def test_cached_file_with_cold_tags_makes_no_request(self):
		'''	A file already cached on the instance supplies the missing UID: no request
		'''
		ct, mr = self.populate({'SOPInstanceUID': MR_SOP_UID})
		mr.dcm0.dcmfile(cache=True)
		doc, requests = self.write(ct, mr)
		self.assertEqual(requests, [])
		self.assert_mr_evidence(doc)

	def test_missing_sop_instance_uid_is_refused(self):
		'''	A supplied reference series whose instance has no SOP Instance UID in its record, tags
			or file: the write is refused naming the field, rather than dropping the series from
			the evidence
		'''
		ct, mr = self.populate({'SOPClassUID': MR_IMAGE_STORAGE, 'FrameOfReferenceUID': MR_FRAME_UID},
			tags={'SOPClassUID': MR_IMAGE_STORAGE}, sop_uid=None)
		with self.assertRaisesRegex(ValueError, r'no SOP Instance UID in its record, its simplified tags or its file'):
			self.write(ct, mr, tag_mr=False)
		self.assertEqual(self.kinds(self.pacs.requests[-2:]), ['simplified-tags', 'file'])

	def test_missing_sop_class_uid_is_refused(self):
		'''	The same with no SOP Class UID anywhere: refused, rather than recording an empty
			Referenced SOP Class UID
		'''
		ct, mr = self.populate({'SOPInstanceUID': MR_SOP_UID, 'FrameOfReferenceUID': MR_FRAME_UID},
			tags={'SOPInstanceUID': MR_SOP_UID}, sop_class=None)
		with self.assertRaisesRegex(ValueError, r'no SOP Class UID in its record, its simplified tags or its file'):
			self.write(ct, mr, tag_mr=False)
		self.assertEqual(self.kinds(self.pacs.requests[-2:]), ['simplified-tags', 'file'])

	def test_two_sources_supply_the_two_uids_untagged_series(self):
		'''	Control for the refusals: the same untagged second series, with the SOP Instance UID from
			the record and the SOP Class UID from the tags, keeps both series in the evidence
		'''
		ct, mr = self.populate({'SOPInstanceUID': MR_SOP_UID, 'FrameOfReferenceUID': MR_FRAME_UID},
			tags={'SOPInstanceUID': MR_SOP_UID, 'SOPClassUID': MR_IMAGE_STORAGE})
		doc, requests = self.write(ct, mr, tag_mr=False)
		self.assertEqual(self.kinds(requests), ['simplified-tags'])
		series = dict((str(item.SeriesInstanceUID), item)
			for item in doc.CurrentRequestedProcedureEvidenceSequence[0].ReferencedSeriesSequence)
		self.assertEqual(sorted(series), sorted([CT_SERIES_UID, MR_SERIES_UID]))
		self.assertEqual([(str(s.ReferencedSOPClassUID), str(s.ReferencedSOPInstanceUID)) for s in series[MR_SERIES_UID].ReferencedSOPSequence],
			[(MR_IMAGE_STORAGE, MR_SOP_UID)])


if __name__ == '__main__':
	unittest.main()
