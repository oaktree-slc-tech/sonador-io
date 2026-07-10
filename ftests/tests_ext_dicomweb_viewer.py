import posixpath, logging, requests
from time import sleep

from client import apisettings as gapi
from client.errors import ClientOperationError

from ..helpers import response2filearchive
from ..test.acl import AclBaseTestCase, TESTGROUP01, TESTUSER01

logger = logging.getLogger(__name__)

# DICOM tag (0020,000D) StudyInstanceUID, as it appears in a DICOMweb-format JSON response
DCMWEB_TAG_STUDY_INSTANCE_UID = '0020000D'


class SonadorDicomWebViewerEndpointTests(AclBaseTestCase):
	'''	Permission-boundary tests for the DICOMweb endpoints the viewer's study browser and
		image loader depend on directly: the ACL-filtered study search list and the
		WADO-RS-equivalent series/study metadata, instance, and frame retrieval routes.
		Neither had a client-library wrapper or any test coverage before this addition, so
		requests are issued directly (mirroring the pattern already used for archive download
		in tests_download.py). See sonador-ftests#4.
	'''
	def tearDown(self):
		'''	Remove server policies associated with test data
		'''
		self.tearDownAcl()

	def _dcmweb_study_uids(self, results):
		'''	Extract the set of StudyInstanceUID values present in a DICOMweb study search response
		'''
		return set(item.get(DCMWEB_TAG_STUDY_INSTANCE_UID, {}).get('Value', [None])[0] for item in results)

	def test_dcmweb_study_search_visibility(self, *args, **kwargs):
		'''	Verify the DICOMweb study search endpoint (/dicom-web/studies) only returns studies the
			requesting user is authorized to view, and that visibility follows local ACL grant/revoke.
			Closes the "study no longer appears in search results" leg of the original sonador-ftests#4 scope.
		'''
		iserver, testgroup01, testuser01 = self.setupTestAuth(
			testuser_config=TESTUSER01, testgroup_name=TESTGROUP01, **kwargs)

		# Deliberately no `query` permission: per SecureResourceQueryViewMixin.apply_session_options
		# (orthanc-sonador), a user WITH `query` bypasses ACL-based filtering entirely and sees every
		# study (matches the documented tools/secure-find convention exercised by
		# tests_sonadoracl_query.py). Omitting `query` routes the request through apply_acl_queryfilter,
		# which is the code path this test is actually targeting.
		server_acl = iserver.admin_create_acl(testgroup01, { 'resource': '*', 'query': False, 'view': False, 'duration': 1 })

		r_cx = self.fetchTestResource(self.nih_cxr_testdcm)

		with self.getLimitedImageServer(iserver, testuser01, object_data={'description': 'DICOMweb study search visibility'}) as iserver_test:

			with self.stageImageArchiveSeries(iserver, response2filearchive(r_cx)) as (test_sx, test_hache):

				test_s = iserver.get_study(test_sx.parent.pk)

				# Allow the resource cache the DICOMweb search view reads from to index
				sleep(0.15)
				test_s.index()
				test_sx.index()

				url = posixpath.join(iserver.dicomweb_root, 'studies')

				def _search():
					r = requests.get(
						iserver_test.orthanc_apiurl(url, query_params={'StudyInstanceUID': test_s.study_uid}),
						headers=iserver_test.orthanc_request_headers(), verify=iserver_test.verify_ssl(), timeout=30)
					if not r.ok:
						raise Exception('DICOMweb study search returned non-200 response. Status-code: %s' % r.status_code)
					return self._dcmweb_study_uids(r.json())

				# No local grant yet: study must not appear in the search results
				self.assertTrue(test_s.study_uid not in _search(),
					msg='Study visible via DICOMweb search before any view grant was made')

				# Grant local view on the study
				testacl_local = test_s.create_group_acl(testgroup01, {
					'View': True, 'Modify': False, 'Remove': False, 'CommentEdit': False, 'CommentView': False, 'ACL': False,
				})
				sleep(0.5)

				self.assertTrue(test_s.study_uid in _search(),
					msg='Study not visible via DICOMweb search after view grant')

				# Revoke the grant
				testacl_local.delete()
				sleep(0.5)

				self.assertTrue(test_s.study_uid not in _search(),
					msg='Study still visible via DICOMweb search after view grant was revoked')

	def test_dcmweb_series_metadata_retrieval_permission_boundary(self, *args, **kwargs):
		'''	Verify the WADO-RS-equivalent series metadata endpoint
			(studies/{ StudyInstanceUID }/series/{ SeriesInstanceUID }/metadata) enforces `view`
			and follows grant/revoke. This is the metadata call the viewer's image loader issues
			before it retrieves instance/frame data.
		'''
		iserver, testgroup01, testuser01 = self.setupTestAuth(
			testuser_config=TESTUSER01, testgroup_name=TESTGROUP01, **kwargs)
		server_acl = iserver.admin_create_acl(testgroup01, { 'resource': '*', 'duration': 1 })

		r_cx = self.fetchTestResource(self.nih_cxr_testdcm)

		with self.getLimitedImageServer(iserver, testuser01, object_data={'description': 'DICOMweb series metadata boundary'}) as iserver_test:

			with self.stageImageArchiveSeries(iserver, response2filearchive(r_cx)) as (test_sx, test_hache):

				url = posixpath.join(iserver.dicomweb_root, 'studies', test_sx.parent.study_uid,
					'series', test_sx.series_uid, 'metadata')

				# Denied before any grant
				r = requests.get(iserver_test.orthanc_apiurl(url),
					headers=iserver_test.orthanc_request_headers(), verify=iserver_test.verify_ssl(), timeout=30)
				self.assertEqual(r.status_code, 403,
					msg='Expected 403 for series metadata retrieval without a view grant. Got: %s' % r.status_code)

				# Grant local view on the series
				testacl_local = test_sx.create_group_acl(testgroup01, {
					'View': True, 'Modify': False, 'Remove': False, 'CommentEdit': False, 'CommentView': False, 'ACL': False,
				})
				sleep(0.5)

				r = requests.get(iserver_test.orthanc_apiurl(url),
					headers=iserver_test.orthanc_request_headers(), verify=iserver_test.verify_ssl(), timeout=30)
				if not r.ok:
					raise Exception(
						'Unable to retrieve series metadata after view grant, server returned non-200 response. Status-code: %s' % r.status_code)
				self.assertTrue(isinstance(r.json(), list) and len(r.json()) > 0,
					msg='Series metadata response was not a non-empty list of instance metadata')

				# Revoke the grant
				testacl_local.delete()
				sleep(0.5)

				r = requests.get(iserver_test.orthanc_apiurl(url),
					headers=iserver_test.orthanc_request_headers(), verify=iserver_test.verify_ssl(), timeout=30)
				self.assertEqual(r.status_code, 403,
					msg='Expected 403 for series metadata retrieval after view grant was revoked. Got: %s' % r.status_code)

	def test_dcmweb_instance_retrieval_permission_boundary(self, *args, **kwargs):
		'''	Verify the WADO-RS-equivalent instance retrieval endpoint
			(studies/{ StudyInstanceUID }/series/{ SeriesInstanceUID }/instances/{ SOPInstanceUID })
			enforces `view` and follows grant/revoke.
		'''
		iserver, testgroup01, testuser01 = self.setupTestAuth(
			testuser_config=TESTUSER01, testgroup_name=TESTGROUP01, **kwargs)
		server_acl = iserver.admin_create_acl(testgroup01, { 'resource': '*', 'duration': 1 })

		r_cx = self.fetchTestResource(self.nih_cxr_testdcm)

		with self.getLimitedImageServer(iserver, testuser01, object_data={'description': 'DICOMweb instance retrieval boundary'}) as iserver_test:

			with self.stageImageArchiveSeries(iserver, response2filearchive(r_cx)) as (test_sx, test_hache):

				instance_uid = test_sx.slices_collection[0].sop_instance_uid

				url = posixpath.join(iserver.dicomweb_root, 'studies', test_sx.parent.study_uid,
					'series', test_sx.series_uid, 'instances', instance_uid)

				# Denied before any grant
				r = requests.get(iserver_test.orthanc_apiurl(url),
					headers=iserver_test.orthanc_request_headers(), verify=iserver_test.verify_ssl(), timeout=30)
				self.assertEqual(r.status_code, 403,
					msg='Expected 403 for instance retrieval without a view grant. Got: %s' % r.status_code)

				# Grant local view on the series
				testacl_local = test_sx.create_group_acl(testgroup01, {
					'View': True, 'Modify': False, 'Remove': False, 'CommentEdit': False, 'CommentView': False, 'ACL': False,
				})
				sleep(0.5)

				r = requests.get(iserver_test.orthanc_apiurl(url),
					headers=iserver_test.orthanc_request_headers(), verify=iserver_test.verify_ssl(), timeout=30)
				if not r.ok:
					raise Exception(
						'Unable to retrieve instance after view grant, server returned non-200 response. Status-code: %s' % r.status_code)
				self.assertTrue(len(r.content) > 0, msg='Instance retrieval response body was empty')

				# Revoke the grant
				testacl_local.delete()
				sleep(0.5)

				r = requests.get(iserver_test.orthanc_apiurl(url),
					headers=iserver_test.orthanc_request_headers(), verify=iserver_test.verify_ssl(), timeout=30)
				self.assertEqual(r.status_code, 403,
					msg='Expected 403 for instance retrieval after view grant was revoked. Got: %s' % r.status_code)

	def test_dcmweb_instance_frame_retrieval_permission_boundary(self, *args, **kwargs):
		'''	Verify the WADO-RS-equivalent frame retrieval endpoint
			(studies/{ StudyInstanceUID }/series/{ SeriesInstanceUID }/instances/{ SOPInstanceUID }/frames/{ n })
			enforces `view` and follows grant/revoke. This is the endpoint the viewer's cornerstone
			DICOMweb data source calls to retrieve pixel data for rendering.
		'''
		iserver, testgroup01, testuser01 = self.setupTestAuth(
			testuser_config=TESTUSER01, testgroup_name=TESTGROUP01, **kwargs)
		server_acl = iserver.admin_create_acl(testgroup01, { 'resource': '*', 'duration': 1 })

		r_cx = self.fetchTestResource(self.nih_cxr_testdcm)

		with self.getLimitedImageServer(iserver, testuser01, object_data={'description': 'DICOMweb frame retrieval boundary'}) as iserver_test:

			with self.stageImageArchiveSeries(iserver, response2filearchive(r_cx)) as (test_sx, test_hache):

				instance_uid = test_sx.slices_collection[0].sop_instance_uid

				url = posixpath.join(iserver.dicomweb_root, 'studies', test_sx.parent.study_uid,
					'series', test_sx.series_uid, 'instances', instance_uid, 'frames', '1')

				# Denied before any grant
				r = requests.get(iserver_test.orthanc_apiurl(url),
					headers=iserver_test.orthanc_request_headers(), verify=iserver_test.verify_ssl(), timeout=30)
				self.assertEqual(r.status_code, 403,
					msg='Expected 403 for frame retrieval without a view grant. Got: %s' % r.status_code)

				# Grant local view on the series
				testacl_local = test_sx.create_group_acl(testgroup01, {
					'View': True, 'Modify': False, 'Remove': False, 'CommentEdit': False, 'CommentView': False, 'ACL': False,
				})
				sleep(0.5)

				r = requests.get(iserver_test.orthanc_apiurl(url),
					headers=iserver_test.orthanc_request_headers(), verify=iserver_test.verify_ssl(), timeout=30)
				if not r.ok:
					raise Exception(
						'Unable to retrieve frame after view grant, server returned non-200 response. Status-code: %s' % r.status_code)
				self.assertTrue(len(r.content) > 0, msg='Frame retrieval response body was empty')

				# Revoke the grant
				testacl_local.delete()
				sleep(0.5)

				r = requests.get(iserver_test.orthanc_apiurl(url),
					headers=iserver_test.orthanc_request_headers(), verify=iserver_test.verify_ssl(), timeout=30)
				self.assertEqual(r.status_code, 403,
					msg='Expected 403 for frame retrieval after view grant was revoked. Got: %s' % r.status_code)
