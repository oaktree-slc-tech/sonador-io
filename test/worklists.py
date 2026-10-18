'''	Tests of the worklist models which need no server connection.

	Run from the directory containing the `sonador` package: python -m unittest sonador.test.worklists
'''
import json, unittest
from types import SimpleNamespace

from sonador.servers import SonadorServer, SonadorImagingServer
from sonador.apisettings import DicomHeaderData, IMAGING_SERVER_RESOURCE_STUDY, IMAGING_SERVER_RESOURCE_PATIENT
from sonador.imaging.orthanc import ImagingStudy
from sonador.imaging.orthanc.worklists import ReviewerStudyWorklistItem

IMAGESERVER_JSON = {
	'pk': 'srv-1', 'name': 'Test PACS', 'hostname': 'pacs.invalid', 'scheme': 'https', 'port': 443,
	'internal_hostname': 'pacs.internal.invalid', 'internal_scheme': 'http', 'internal_port': 8042,
	'dicomweb_root': '/dicom-web', 'wado_root': '/wado', 'token': 'abc', 'active': True, 'default': True,
	'description': '', 'image_rendering_method': '', 'thumbnail_rendering_method': '', 'qido_supports_include': False,
}

# DICOMweb worklist item as the browse endpoint returns it: worklist fields plus the study's DICOM tags
WORKLIST_ITEM_DICOMWEB = {
	'ID': 'w-1', 'State': 'Scheduled', 'Created': '2026-10-18T10:00:00Z', 'LastUpdate': '2026-10-18T10:00:00Z',
	'Study': 'study-1',
	'User': {'id': 1, 'username': 'alice', 'name': 'Alice Adams'},
	'Group': {'id': 10, 'name': 'Radiology'},
	'Meta': {'RequestedProcedure': {
		'ReasonForTheRequestedProcedure': 'Review fracture', 'RequestedProcedureDescription': 'Segmentation'}},
	'00080020': {'vr': 'DA', 'Value': ['20260101']},
	'00080030': {'vr': 'TM', 'Value': ['101010']},
	'00081030': {'vr': 'LO', 'Value': ['CT head']},
	'0020000D': {'vr': 'UI', 'Value': ['1.2.3.4']},
	'00100010': {'vr': 'PN', 'Value': [{'Alphabetic': 'Pat^A'}]},
	'00100020': {'vr': 'LO', 'Value': ['MRN-1']},
}


def tag(level, header, group, element, vr):
	return (group, element), (level, DicomHeaderData(header, (group, element), int(element, 16), vr))


CACHE_DCM_TAGS = dict([
	tag(IMAGING_SERVER_RESOURCE_STUDY, 'StudyDate', '0008', '0020', 'DA'),
	tag(IMAGING_SERVER_RESOURCE_STUDY, 'StudyTime', '0008', '0030', 'TM'),
	tag(IMAGING_SERVER_RESOURCE_STUDY, 'StudyDescription', '0008', '1030', 'LO'),
	tag(IMAGING_SERVER_RESOURCE_STUDY, 'StudyInstanceUID', '0020', '000D', 'UI'),
	tag(IMAGING_SERVER_RESOURCE_PATIENT, 'PatientName', '0010', '0010', 'PN'),
	tag(IMAGING_SERVER_RESOURCE_PATIENT, 'PatientID', '0010', '0020', 'LO'),
])


class FakeResponse:
	'''	Minimal stand-in for requests.Response
	'''
	def __init__(self, data, url='https://pacs.invalid/x'):
		self._data = data
		self.status_code = 200
		self.ok = True
		self.headers = {'Content-Type': 'application/json'}
		self.text = json.dumps(data)
		self.content = self.text.encode()
		self.request = SimpleNamespace(url=url)

	def json(self, **kwargs):
		return json.loads(self.text, **kwargs)


class WorklistParentStudyTestCase(unittest.TestCase):
	'''	The study behind a worklist item must be a regular study of the imaging server: Sonador connection as
		`server`, imaging server as `pacs`, so that the lazy `series_collection` (and everything else that
		reaches `server.verify`) works like it does for search results.
	'''
	def setUp(self):
		self.server = SonadorServer('https://sonador.invalid', apitoken='token', verify=False)
		self.iserver = self.server.imageserver_modelinstance_from_json(dict(IMAGESERVER_JSON))
		self.requests = []

	def _answer(self, data):
		'''	Patch the imaging server's GET with a canned JSON answer and record the calls
		'''
		def _request_get(resource_endpoint, error_msg=None, headers=None, verify=None, **kwargs):
			self.requests.append({'url': resource_endpoint, 'verify': verify})
			return FakeResponse(data, url=resource_endpoint)
		self.iserver._request_get = _request_get

	def test_worklist_parent_study_is_built_like_a_server_study(self):
		self._answer([WORKLIST_ITEM_DICOMWEB])
		items = list(self.iserver.fetch_user_dcmweb_worklist(query_params={'State': 'Open'}, cache_dcm_tags=CACHE_DCM_TAGS))
		self.assertEqual(len(items), 1)
		self.assertIn('State=Open', self.requests[0]['url'])
		item = items[0]
		self.assertIsInstance(item, ReviewerStudyWorklistItem)
		self.assertEqual((item.pk, item.state, item.user_displayname, item.group_name), ('w-1', 'Scheduled', 'Alice Adams', 'Radiology'))
		self.assertIs(item.server, self.server)				# the item belongs to the Sonador connection as well
		self.assertEqual((item.reason_for_review, item.requested_procedure_description), ('Review fracture', 'Segmentation'))

		study = item.parent
		self.assertIsInstance(study, ImagingStudy)
		self.assertIs(study.server, self.server)			# the Sonador connection, as for search results
		self.assertIs(study.pacs, self.iserver)
		self.assertEqual(study.pk, 'study-1')
		self.assertEqual(study.description, 'CT head')
		self.assertEqual(str(study.patient_name), 'Pat^A')

		# The lazy series collection fetches from the study's series endpoint with the connection's SSL setting
		self._answer([])
		series = study.series_collection
		self.assertEqual(len(series), 0)
		self.assertTrue(self.requests[-1]['url'].endswith('/studies/study-1/series'), self.requests[-1]['url'])
		self.assertIs(self.requests[-1]['verify'], False)

		# The patient (absent from DICOMweb data) is retrieved from the study's Orthanc resource once
		self._answer({'ID': 'study-1', 'ParentPatient': 'patient-1', 'MainDicomTags': {}, 'PatientMainDicomTags': {}})
		self.assertEqual(study.patient, 'patient-1')
		self.assertTrue(self.requests[-1]['url'].endswith('/studies/study-1'), self.requests[-1]['url'])
		nrequests = len(self.requests)
		self.assertEqual(study.patient, 'patient-1')
		self.assertEqual(len(self.requests), nrequests)

	def test_fetch_series_works_with_the_imaging_server_as_server(self):
		'''	Studies constructed with the imaging server as `server` (older callers) must still fetch their series
		'''
		study = ImagingStudy(self.iserver, {'ID': 'study-2', 'MainDicomTags': {}, 'PatientMainDicomTags': {}})
		self.assertIs(study.server, self.iserver)
		self._answer([])
		self.assertEqual(len(study.series_collection), 0)
		self.assertTrue(self.requests[-1]['url'].endswith('/studies/study-2/series'))
		self.assertIs(self.requests[-1]['verify'], False)


if __name__ == '__main__':
	unittest.main()
