import os, posixpath, unittest, requests, logging, json, tempfile, zipfile, contextlib
import pydicom
from pydicom.uid import generate_uid
from io import BytesIO
from time import sleep

from client import apisettings as gcapi
from client.utils.general import first
from client.utils.object import each
from client.utils.general import create_token
from client.errors import ClientOperationError

from ..helpers import initenv_sonador_server, response2filearchive
from ..servers import sonador_apitoken_fetch
from ..apisettings import SONADOR_IMAGING_SERVER, IMAGING_SERVER_RESOURCE_STUDY, IMAGING_SERVER_RESOURCE_SERIES, \
	DCMHEADER_SERIES_INSTANCE_UID, DCMHEADER_STUDY_INSTANCE_UID

from ..tasks.uploads import imageserver_upload_archive
from ..test import SonadorBaseTestCase, SonadorSeriesBaseTestCase, SonadorStudyBaseTestCase
from ..test.acl import AclBaseTestCase, \
	TESTGROUP01, TESTGROUP02, TESTGROUP03, TESTGROUP04, TESTGROUP05, \
	TESTUSER01, TESTUSER02, TESTUSER03, TESTUSER04, TESTUSER05

logger = logging.getLogger(__name__)


class SonadorResourceCommentTests(SonadorSeriesBaseTestCase):
	'''	Ensure that resource comments function as expected
	'''
	def test_resource_comment_valid(self, *args, **kwargs):
		'''	Ensure that the test runner is able to connect to Sonador, upload an imaging
			series, create comments, update a comment, and remove the comment.
		'''
		# Retrieve imaging server to be used by the test
		iserver = self.getImageServer(*args, **kwargs)

		# Retrieve and upload CT data
		ctr = self.fetchTestResource('https://oak-tree.tech/documents/156/example.lung-ct.volume-3d.zip')

		# Temporarily stage data to Sonador to fun the test
		with self.stageImageArchiveSeries(iserver, zipfile.ZipFile(BytesIO(ctr.content))) as (series, hcache):

			# Clear any existing comments before running tests
			each(lambda c: c.delete(), series.fetch_comments())

			# Create comment instance
			ctxt = 'Test comment: CT lung scan series downloaded by functional test runner'
			utxt = '%s (r1)' % ctxt
			series.create_comment(ctxt)
			self.assertTrue(any(ctxt == c.text for c in series.fetch_comments()), msg='Series comment did not persist to Orthanc')

			# Retrieve comment instance from server, update text, and ensure that the update was persisted
			c = first(series.fetch_comments(), key=lambda c: c.text == ctxt)
			self.assertTrue(c is not None, msg='Unable to retrieve comment instance from Sonador')
			c.update({ 'Text': utxt })
			self.assertTrue(any(utxt == c.text for c in series.fetch_comments()), msg='Comment update failed to persist to Orthanc')

			# Retrieve comment using series.get_comment server, delete the comment, and verify that it was deleted
			c = series.get_comment(c.pk)				
			c.delete()
			self.assertTrue(len(series.fetch_comments()) == 0, msg='Comment should have been removed from the series, but was not.')
	
	def test_resource_comment_dicomweb(self, *args, **kwargs):
		'''	Ensure that the test runner is able to connect to Sonador, upload an imaging series, create
			comments via the DICOMweb interface, update a comment via the DICOmweb interface, and remove
			the comment via the DICOmweb interface.
		'''
		# Retrieve the imaging server to be used by the test
		iserver = self.getImageServer(*args, **kwargs)

		# Retrieve and upload MRI data
		ctr = self.fetchTestResource('https://www.oak-tree.tech/documents/331/nih-cxr.patient-30775.zip')

		# Temporarily stage data to Sonador to run the test
		with self.stageImageArchiveSeries(iserver, zipfile.ZipFile(BytesIO(ctr.content)), rapid_lookup=False) as (series, hcache):

			# Allow time for CXR instance to index
			sleep(0.25)

			# Clear any existing comments before running tests
			each(lambda c: c.delete(), series.fetch_comments())

			# Create comment instance
			ctxt = 'Test comment: chest x-ray series uploaded by functional test runner. Series-UID="%s"' % series.series_uid
			utxt = '%s (r1)' % ctxt
			c0 = series.create_comment(ctxt, dicomweb_api=True, cache_response=True)
			_comments0 = series.fetch_comments(dicomweb_api=True, cache_response=True)

			# Ensure that the API request was sent via the DICOMweb API
			self.assertTrue(
				getattr(c0, 'http_response', None) is not None and iserver.dicomweb_root in c0.http_response.url,
				msg='Creation request not sent to DICOMweb API endpoint')
			self.assertTrue(
				getattr(_comments0, 'http_response', None) is not None and iserver.dicomweb_root in _comments0.http_response.url,
				msg='Fetch comments request not sent to DICOMweb API endpoint')

			# Ensure that the comment series was created correctly
			self.assertTrue(
				any(ctxt == _c.text and _c._objectdata.get(DCMHEADER_SERIES_INSTANCE_UID) == series.series_uid for _c in _comments0), 
				msg='Series comment did not persist to Orthanc')

			# Retrieve comment via API call
			c1 = series.get_comment(c0.pk, dicomweb_api=True, cache_response=True)
			self.assertTrue(getattr(c1, 'http_response', None) is not None and iserver.dicomweb_root in c1.http_response.url,
				msg='Fetch model instance not sent to DICOMweb endpoint')
			self.assertTrue(ctxt == c1.text, msg='Commment instance retrieved via DICOMweb API does not match text')

			# Update comment, ensure that the request was sent to the DICOMweb API, and was committed successfully.
			r_update = c1.update({ 'Text': utxt }, cache_response=True)
			self.assertTrue(r_update.response.status_code == 200 and iserver.dicomweb_root in r_update.response.url,
				msg='Update request did not complete successfully or was sent to the wrong endpoint')

			# Fetch updated comment and ensure it contains the revised text
			c2 = series.get_comment(c0.pk, dicomweb_api=True, cache_response=True)
			self.assertTrue(c2.text == utxt and iserver.dicomweb_root in c2.http_response.url,
				msg='Updated text not persisted to Orthanc.')

			# Remove comment from server
			r_del = c2.delete()
			self.assertTrue(len(series.fetch_comments(dicomweb_api=True)) == 0 and iserver.dicomweb_root in r_del.url,
				msg='Comment should have been removed from the series, but was not.')

	def test_resource_comment_kafka(self, *args, **kwargs):
		'''	Ensure that the test runner is able to connect to Sonador, upload an imaging
			series, create comments, update a comment, and remove the comment.
		'''
		# Retrieve imaging server to be used by the test
		iserver = self.getImageServer(*args, **kwargs)

		# Retrieve and upload CT data
		ctr = self.fetchTestResource('https://www.oak-tree.tech/documents/331/nih-cxr.patient-30775.zip')

		# Temporarily stage data to Sonador to fun the test
		with self.stageImageArchiveSeries(iserver, zipfile.ZipFile(BytesIO(ctr.content))) as (series, hcache):

			# Create token to uniquely identify the test comment
			_token = create_token()

			# Create comment instance
			ctxt = 'Test comment: CT lung scan series downloaded by functional test runner. Token: %s' % _token
			c = series.create_comment(ctxt)
			self.assertTrue(any(ctxt == c.text for c in series.fetch_comments()), 
				msg='Series comment did not persist to Orthanc')

			# Retrieve Kafka export from Orthanc
			_kafka = c.fetch_kafka_data()
			self.assertTrue(_token in _kafka.get('Text'),
				msg='Test comment exported by Kafka endpoint did not match the comment created during the test.')

			# Trigger manual export of Kafka data
			_kafka = c.kafka_export()
			self.assertEqual(_kafka.get(gcapi.STATUS), gcapi.SUCCESS, msg='Unable to export data to Kafka topic.')


class SonadorStudyResourceCommentTests(SonadorStudyBaseTestCase):
	'''	Ensure that study comments function as expected
	'''
	def test_resource_comment_valid(self, *args, **kwargs):
		'''	Ensure that the test runner is able to connect to Sonador, upload an imaging
			study, create comments, update a comment, and remove the comment.
		'''
		# Retrieve imaging server to be used by the test
		iserver = self.getImageServer(*args, **kwargs)

		# Retrieve and upload CT data
		ctr = self.fetchTestResource('https://oak-tree.tech/documents/156/example.lung-ct.volume-3d.zip')

		# Temporarily stage data to Sonador to fun the test
		with self.stageImageArchiveStudy(iserver, zipfile.ZipFile(BytesIO(ctr.content))) as (study, hcache):

			# Clear any existing comments before running tests
			each(lambda c: c.delete(), study.fetch_comments())

			# Create comment instance
			ctxt = 'Test comment: CT lung scan study downloaded by functional test runner'
			utxt = '%s (r1)' % ctxt
			study.create_comment(ctxt)
			self.assertTrue(any(ctxt == c.text for c in study.fetch_comments()), msg='Study comment did not persist to Orthanc')

			# Retrieve comment instance from server, update text, and ensure that the update was persisted
			c = first(study.fetch_comments(), key=lambda c: c.text == ctxt)
			self.assertTrue(c is not None, msg='Unable to retrieve comment instance from Sonador')
			c.update({ 'Text': utxt })
			self.assertTrue(any(utxt == c.text for c in study.fetch_comments()), msg='Comment update failed to persist to Orthanc')

			# Retrieve comment using study.get_comment server, delete the comment, and verify that it was deleted
			c = study.get_comment(c.pk)				
			c.delete()
			self.assertTrue(len(study.fetch_comments()) == 0, msg='Comment should have been removed from the study, but was not.')
	
	def test_resource_comment_dicomweb(self, *args, **kwargs):
		'''	Ensure that the test runner is able to connect to Sonador, upload an imaging study, create
			comments via the DICOMweb interface, update a comment via the DICOmweb interface, and remove
			the comment via the DICOmweb interface.
		'''
		# Retrieve the imaging server to be used by the test
		iserver = self.getImageServer(*args, **kwargs)

		# Retrieve and upload MRI data
		ctr = self.fetchTestResource('https://www.oak-tree.tech/documents/331/nih-cxr.patient-30775.zip')

		# Temporarily stage data to Sonador to run the test
		with self.stageImageArchiveStudy(iserver, zipfile.ZipFile(BytesIO(ctr.content)), rapid_lookup=False) as (study, hcache):

			# Allow time for CXR instance to index
			sleep(0.25)

			# Clear any existing comments before running tests
			each(lambda c: c.delete(), study.fetch_comments())

			# Create comment instance
			ctxt = 'Test comment: chest x-ray study uploaded by functional test runner. Study-UID="%s"' % study.study_uid
			utxt = '%s (r1)' % ctxt
			c0 = study.create_comment(ctxt, dicomweb_api=True, cache_response=True)
			_comments0 = study.fetch_comments(dicomweb_api=True, cache_response=True)

			# Ensure that the API request was sent via the DICOMweb API
			self.assertTrue(
				getattr(c0, 'http_response', None) is not None and iserver.dicomweb_root in c0.http_response.url,
				msg='Creation request not sent to DICOMweb API endpoint')
			self.assertTrue(
				getattr(_comments0, 'http_response', None) is not None and iserver.dicomweb_root in _comments0.http_response.url,
				msg='Fetch comments request not sent to DICOMweb API endpoint')

			# Ensure that the comment study was created correctly
			self.assertTrue(
				any(ctxt == _c.text and _c._objectdata.get(DCMHEADER_STUDY_INSTANCE_UID) == study.study_uid for _c in _comments0), 
				msg='Study comment did not persist to Orthanc')

			# Retrieve comment via API call
			c1 = study.get_comment(c0.pk, dicomweb_api=True, cache_response=True)
			self.assertTrue(getattr(c1, 'http_response', None) is not None and iserver.dicomweb_root in c1.http_response.url,
				msg='Fetch model instance not sent to DICOMweb endpoint')
			self.assertTrue(ctxt == c1.text, msg='Commment instance retrieved via DICOMweb API does not match text')

			# Update comment, ensure that the request was sent to the DICOMweb API, and was committed successfully.
			r_update = c1.update({ 'Text': utxt }, cache_response=True)
			self.assertTrue(r_update.response.status_code == 200 and iserver.dicomweb_root in r_update.response.url,
				msg='Update request did not complete successfully or was sent to the wrong endpoint')

			# Fetch updated comment and ensure it contains the revised text
			c2 = study.get_comment(c0.pk, dicomweb_api=True, cache_response=True)
			self.assertTrue(c2.text == utxt and iserver.dicomweb_root in c2.http_response.url,
				msg='Updated text not persisted to Orthanc.')

			# Remove comment from server
			r_del = c2.delete()
			self.assertTrue(len(study.fetch_comments(dicomweb_api=True)) == 0 and iserver.dicomweb_root in r_del.url,
				msg='Comment should have been removed from the study, but was not.')


class SonadorCommentPermissionTests(AclBaseTestCase):
	'''	Test comment permission enforcement for Sonador/Orthanc comments API.
		Validates that comment_edit ACL permission is enforced at the server-level
		(global/Sonador) and that CommentView/CommentEdit are enforced at the
		resource-level (local/Orthanc) ACL scope.

		Comment read access is implicitly granted when a user has View access to the
		resource at the server level. Granular CommentView control requires a local
		(resource-level) ACL.

		Each test uses a distinct user/group pair to prevent the Orthanc
		authorization cache from poisoning grant tests with stale denials:
		  TESTUSER01 — global read + deny create / local ACL deny / study deny
		  TESTUSER02 — global create denied / global update+delete denied
		  TESTUSER03 — global create authorized (positive CRUD)
		  TESTUSER04 — local ACL create authorized (positive CRUD)
		  TESTUSER05 — DICOMweb deny+grant
	'''
	def tearDown(self):
		'''	Remove server policies associated with test data
		'''
		self.tearDownAcl()

	def test_comment_global_read_and_deny_create(self, *args, **kwargs):
		'''	Verify that a limited user with view and comment_view at the global
			(server) level can read comments via both standard and DICOMweb APIs,
			and that comment_edit: False blocks write operations on both.
		'''
		iserver, testgroup01, testuser01 = self.setupTestAuth(
			testuser_config=TESTUSER01, testgroup_name=TESTGROUP01, **kwargs)

		# Download test series
		r_cx = self.fetchTestResource(self.nih_cxr_testdcm)

		# Stage test files to imaging server
		with self.stageImageArchiveSeries(iserver, response2filearchive(r_cx)) as (test_sx, test_hache):

			# Create server-level ACL: view + comment_view but no comment_edit
			testacl = iserver.admin_create_acl(testgroup01, {
				'resource': '*', 'view': True, 'comment_view': True, 'comment_edit': False, 'duration': 1
			})

			# Admin creates a comment
			ctxt = 'Test comment: read permission verification'
			admin_comment = test_sx.create_comment(ctxt)

			with self.getLimitedImageServer(iserver, testuser01, object_data={'description': 'Comment read permission testing'}) as iserver_test:

				test_sx_ltd = iserver_test.get_series(test_sx.pk)

				# Standard API: fetch comments
				comments = test_sx_ltd.fetch_comments()
				self.assertTrue(any(ctxt == c.text for c in comments),
					msg='Limited user unable to fetch comments via standard API')

				# Standard API: get specific comment
				c_ltd = test_sx_ltd.get_comment(admin_comment.pk)
				self.assertEqual(c_ltd.text, ctxt,
					msg='Limited user unable to get comment by ID via standard API')

				# DICOMweb API: fetch comments
				comments_dcm = test_sx_ltd.fetch_comments(dicomweb_api=True, cache_response=True)
				self.assertTrue(
					getattr(comments_dcm, 'http_response', None) is not None
						and iserver.dicomweb_root in comments_dcm.http_response.url,
					msg='Fetch response not routed through DICOMweb API endpoint')
				self.assertTrue(any(ctxt == c.text for c in comments_dcm),
					msg='Limited user unable to fetch comments via DICOMweb API')

				# Standard API: create should be denied
				try:
					test_sx_ltd.create_comment('Should not be created')
					self.fail('Limited user able to create comment via standard API without comment_edit permission')
				except ClientOperationError as err:
					_details = getattr(err, 'details', {})
					self.assertEqual(_details.get(gcapi.STATUS_CODE), 403,
						msg='Standard API create: incorrect status code: %s. Expected: 403.' % _details.get(gcapi.STATUS_CODE))

				# DICOMweb API: create should also be denied
				try:
					test_sx_ltd.create_comment('Should not be created via DICOMweb', dicomweb_api=True)
					self.fail('Limited user able to create comment via DICOMweb without comment_edit permission')
				except ClientOperationError as err:
					_details = getattr(err, 'details', {})
					self.assertEqual(_details.get(gcapi.STATUS_CODE), 403,
						msg='DICOMweb create: incorrect status code: %s. Expected: 403.' % _details.get(gcapi.STATUS_CODE))

	def test_comment_global_create_denied(self, *args, **kwargs):
		'''	Verify that a limited user without comment_edit at the global (server)
			level cannot create comments and that the denied comment is not
			persisted server-side.
		'''
		iserver, testgroup02, testuser02 = self.setupTestAuth(
			testuser_config=TESTUSER02, testgroup_name=TESTGROUP02, **kwargs)

		# Download test series
		r_cx = self.fetchTestResource(self.nih_cxr_testdcm)

		# Stage test files to imaging server
		with self.stageImageArchiveSeries(iserver, response2filearchive(r_cx)) as (test_sx, test_hache):

			# Create server-level ACL with view and comment_view but no comment_edit
			testacl = iserver.admin_create_acl(testgroup02, {
				'resource': '*', 'view': True, 'comment_view': True, 'comment_edit': False, 'duration': 1
			})

			ctxt = 'Test comment: should not be created without permission'

			with self.getLimitedImageServer(iserver, testuser02, object_data={'description': 'Comment create deny test'}) as iserver_test:

				test_sx_ltd = iserver_test.get_series(test_sx.pk)

				try:
					test_sx_ltd.create_comment(ctxt)
					self.fail('Limited user able to create comment without comment_edit permission')
				except ClientOperationError as err:
					_details = getattr(err, 'details', {})
					self.assertEqual(_details.get(gcapi.STATUS_CODE), 403,
						msg='Server sent incorrect status code: %s. Expected: 403.' % _details.get(gcapi.STATUS_CODE))

				# Verify no comment was created
				admin_comments = test_sx.fetch_comments()
				self.assertTrue(all(ctxt != c.text for c in admin_comments),
					msg='Comment was persisted despite permission being denied')

	def test_comment_global_create_authorized(self, *args, **kwargs):
		'''	Verify that a limited user with a server comment_edit can create, read, update,
			and delete comments via both the standard and DICOMweb APIs.
		'''
		iserver, testgroup03, testuser03 = self.setupTestAuth(
			testuser_config=TESTUSER03, testgroup_name=TESTGROUP03, **kwargs)

		# Download test series
		r_cx = self.fetchTestResource(self.nih_cxr_testdcm)

		# Stage test files to imaging server
		with self.stageImageArchiveSeries(iserver, response2filearchive(r_cx)) as (test_sx, test_hache):

			# Server-level ACL: enable comment feature flags for the group
			testacl = iserver.admin_create_acl(testgroup03, {
				'resource': '*', 'view': True, 'comment_view': True, 'comment_edit': True, 'duration': 1
			})

			with self.getLimitedImageServer(iserver, testuser03, object_data={'description': 'Comment CRUD with permission'}) as iserver_test:

				test_sx_ltd = iserver_test.get_series(test_sx.pk)

				# --- Standard API CRUD ---
				ctxt = 'Test comment: created via standard API by limited user'
				try: c = test_sx_ltd.create_comment(ctxt)
				except Exception as err:
					self.logErrorDetails('Unable to create comment due to an error.', err)
					raise err

				self.assertTrue(any(ctxt == _c.text for _c in test_sx.fetch_comments()),
					msg='Limited user unable to create comment via standard API')

				c_read = test_sx_ltd.get_comment(c.pk)
				self.assertEqual(c_read.text, ctxt,
					msg='Comment text does not match after standard API round-trip')

				utxt = '%s (updated)' % ctxt
				c_read.update({ 'Text': utxt })
				c_verify = test_sx_ltd.get_comment(c.pk)
				self.assertEqual(c_verify.text, utxt,
					msg='Comment update via standard API did not persist')

				c_verify.delete()
				self.assertTrue(all(utxt != _c.text for _c in test_sx.fetch_comments()),
					msg='Comment still present after standard API deletion')

				# --- DICOMweb API CRUD ---
				ctxt_dcm = 'Test comment: created via DICOMweb API by limited user'
				c_dcm = test_sx_ltd.create_comment(ctxt_dcm, dicomweb_api=True, cache_response=True)
				self.assertTrue(
					getattr(c_dcm, 'http_response', None) is not None
						and iserver.dicomweb_root in c_dcm.http_response.url,
					msg='DICOMweb create response not routed through DICOMweb endpoint')
				self.assertTrue(any(ctxt_dcm == _c.text for _c in test_sx.fetch_comments()),
					msg='Limited user unable to create comment via DICOMweb API')

				c_dcm_read = test_sx_ltd.get_comment(c_dcm.pk, dicomweb_api=True, cache_response=True)
				self.assertEqual(c_dcm_read.text, ctxt_dcm,
					msg='Comment text does not match after DICOMweb round-trip')
				self.assertTrue(iserver.dicomweb_root in c_dcm_read.http_response.url,
					msg='DICOMweb get response not routed through DICOMweb endpoint')

				utxt_dcm = '%s (updated)' % ctxt_dcm
				c_dcm_read.update({ 'Text': utxt_dcm })
				c_dcm_verify = test_sx_ltd.get_comment(c_dcm.pk, dicomweb_api=True)
				self.assertEqual(c_dcm_verify.text, utxt_dcm,
					msg='Comment update via DICOMweb did not persist')

				c_dcm_verify.delete()
				self.assertTrue(all(utxt_dcm != _c.text for _c in test_sx.fetch_comments()),
					msg='Comment still present after DICOMweb deletion')

	def test_comment_global_update_delete_denied(self, *args, **kwargs):
		'''	Verify that a limited user without comment_edit at the global (server)
			level cannot update or delete existing comments.
		'''
		iserver, testgroup02, testuser02 = self.setupTestAuth(
			testuser_config=TESTUSER02, testgroup_name=TESTGROUP02, **kwargs)

		# Download test series
		r_cx = self.fetchTestResource(self.nih_cxr_testdcm)

		# Stage test files to imaging server
		with self.stageImageArchiveSeries(iserver, response2filearchive(r_cx)) as (test_sx, test_hache):

			# Create server-level ACL with view and comment_view but no comment_edit
			testacl = iserver.admin_create_acl(testgroup02, {
				'resource': '*', 'view': True, 'comment_view': True, 'comment_edit': False, 'duration': 1
			})

			# Admin creates a comment
			ctxt = 'Test comment: update and delete permission enforcement'
			admin_comment = test_sx.create_comment(ctxt)

			with self.getLimitedImageServer(iserver, testuser02, object_data={'description': 'Comment permission testing'}) as iserver_test:

				test_sx_ltd = iserver_test.get_series(test_sx.pk)

				# Limited user can read the comment (has comment_view)
				c_ltd = test_sx_ltd.get_comment(admin_comment.pk)
				self.assertEqual(c_ltd.text, ctxt,
					msg='Limited user unable to read comment with comment_view permission')

				# Attempt to update comment — should be denied
				utxt = '%s (modified)' % ctxt
				try:
					c_ltd.update({ 'Text': utxt })
					self.fail('Limited user able to update comment without comment_edit permission')
				except ClientOperationError as err:
					_details = getattr(err, 'details', {})
					self.assertEqual(_details.get(gcapi.STATUS_CODE), 403,
						msg='Update: incorrect status code: %s. Expected: 403.' % _details.get(gcapi.STATUS_CODE))

				# Attempt to delete comment — should be denied
				try:
					c_ltd.delete()
					self.fail('Limited user able to delete comment without comment_edit permission')
				except ClientOperationError as err:
					_details = getattr(err, 'details', {})
					self.assertEqual(_details.get(gcapi.STATUS_CODE), 403,
						msg='Delete: incorrect status code: %s. Expected: 403.' % _details.get(gcapi.STATUS_CODE))

				# Verify comment is unchanged via admin server
				c_admin = test_sx.get_comment(admin_comment.pk)
				self.assertEqual(c_admin.text, ctxt,
					msg='Comment text was modified despite permission being denied')

	def test_comment_local_acl_deny_create(self, *args, **kwargs):
		'''	Verify that a local (Orthanc resource-level) ACL with CommentEdit: False
			denies comment creation when the global (server) ACL also denies it.
			The server ACL denies all comment permissions so the local ACL is the
			sole source of View/CommentView access.
		'''
		iserver, testgroup01, testuser01 = self.setupTestAuth(
			testuser_config=TESTUSER01, testgroup_name=TESTGROUP01, **kwargs)

		# Download test series
		r_cx = self.fetchTestResource(self.nih_cxr_testdcm)

		# Stage test files to imaging server
		with self.stageImageArchiveSeries(iserver, response2filearchive(r_cx)) as (test_sx, test_hache):

			# Server-level ACL: associate group with server, deny all permissions
			testacl = iserver.admin_create_acl(testgroup01, {
				'resource': '*', 'query': False, 'view': False, 'modify': False, 'remove': False,
				'acl': False, 'comment_view': False, 'comment_edit': False, 'duration': 5
			})

			# Admin creates a comment on the series
			ctxt = 'Test comment: local ACL deny enforcement'
			admin_comment = test_sx.create_comment(ctxt)

			# Local series ACL: grant View and CommentView, deny CommentEdit
			testacl_local = test_sx.create_group_acl(testgroup01, {
				'View': True, 'Modify': False, 'Remove': False,
				'CommentView': True, 'CommentEdit': False, 'ACL': False
			})

			with self.getLimitedImageServer(iserver, testuser01, object_data={'description': 'Comment local ACL deny testing'}) as iserver_test:

				test_sx_ltd = iserver_test.get_series(test_sx.pk)

				# Fetch comments — should succeed via local CommentView: True
				comments = test_sx_ltd.fetch_comments()
				self.assertTrue(any(ctxt == c.text for c in comments),
					msg='Limited user unable to view comments despite local ACL granting CommentView')

				# Create comment — should be denied (neither global nor local grants CommentEdit)
				try:
					test_sx_ltd.create_comment('Should not be created')
					self.fail('Limited user able to create comment despite local ACL denying CommentEdit')
				except ClientOperationError as err:
					_details = getattr(err, 'details', {})
					self.assertEqual(_details.get(gcapi.STATUS_CODE), 403,
						msg='Server sent incorrect status code: %s. Expected: 403.' % _details.get(gcapi.STATUS_CODE))

	def test_comment_local_acl_create_authorized(self, *args, **kwargs):
		'''	Verify that a local (Orthanc resource-level) ACL with CommentEdit: True
			grants comment creation even when the global (server) ACL denies
			comment_edit. Proves that a local policy matching the resource is
			sufficient to authorize the operation.
		'''
		iserver, testgroup04, testuser04 = self.setupTestAuth(
			testuser_config=TESTUSER04, testgroup_name=TESTGROUP04, **kwargs)

		# Download test series
		r_cx = self.fetchTestResource(self.nih_cxr_testdcm)

		# Stage test files to imaging server
		with self.stageImageArchiveSeries(iserver, response2filearchive(r_cx)) as (test_sx, test_hache):

			# Server-level ACL: associate group with server, deny all comment permissions
			testacl = iserver.admin_create_acl(testgroup04, {
				'resource': '*', 'query': False, 'view': False, 'modify': False, 'remove': False,
				'acl': False, 'comment_view': False, 'comment_edit': False, 'duration': 5
			})

			# Local series ACL: grant View, CommentView, and CommentEdit
			testacl_local = test_sx.create_group_acl(testgroup04, {
				'View': True, 'Modify': True, 'Remove': False,
				'CommentView': True, 'CommentEdit': True, 'ACL': False
			})

			with self.getLimitedImageServer(iserver, testuser04, object_data={'description': 'Comment local ACL allow testing'}) as iserver_test:

				test_sx_ltd = iserver_test.get_series(test_sx.pk)

				# Create comment — should succeed via local CommentEdit: True
				ctxt = 'Test comment: created via local ACL CommentEdit grant'
				c = test_sx_ltd.create_comment(ctxt)
				self.assertTrue(any(ctxt == _c.text for _c in test_sx.fetch_comments()),
					msg='Limited user unable to create comment despite local ACL granting CommentEdit')

				# Read back
				c_read = test_sx_ltd.get_comment(c.pk)
				self.assertEqual(c_read.text, ctxt,
					msg='Comment text does not match after round-trip')

				# Update
				utxt = '%s (updated)' % ctxt
				c_read.update({ 'Text': utxt })
				c_verify = test_sx_ltd.get_comment(c.pk)
				self.assertEqual(c_verify.text, utxt,
					msg='Comment update did not persist')

				# Delete
				c_verify.delete()
				self.assertTrue(all(utxt != _c.text for _c in test_sx.fetch_comments()),
					msg='Comment still present after deletion')

	def test_comment_global_deny_and_grant_dicomweb(self, *args, **kwargs):
		'''	Verify that comment_edit permission enforcement at the global (server)
			level applies to DICOMweb API endpoints. Tests both denial and grant
			phases. Uses TESTUSER05 (not used in other tests) to avoid cache
			contamination.
		'''
		iserver, testgroup05, testuser05 = self.setupTestAuth(
			testuser_config=TESTUSER05, testgroup_name=TESTGROUP05, **kwargs)

		# Download test series
		r_cx = self.fetchTestResource(self.nih_cxr_testdcm)

		# Stage test files to imaging server
		with self.stageImageArchiveSeries(iserver, response2filearchive(r_cx)) as (test_sx, test_hache):

			# Create server-level ACL with view but no comment_edit
			testacl = iserver.admin_create_acl(testgroup05, {
				'resource': '*', 'view': True, 'comment_view': True, 'comment_edit': False, 'duration': 1
			})

			# Admin creates a comment
			ctxt = 'Test comment: DICOMweb permission enforcement'
			admin_comment = test_sx.create_comment(ctxt)

			# Phase 1: Deny — both standard and DICOMweb creation blocked
			with self.getLimitedImageServer(iserver, testuser05, object_data={'description': 'Comment DICOMweb deny phase'}) as iserver_test:

				test_sx_ltd = iserver_test.get_series(test_sx.pk)

				# Standard API: create should be denied
				try:
					test_sx_ltd.create_comment('Should not be created via standard API')
					self.fail('Limited user able to create comment via standard API without comment_edit permission')
				except ClientOperationError as err:
					_details = getattr(err, 'details', {})
					self.assertEqual(_details.get(gcapi.STATUS_CODE), 403,
						msg='Standard API create: incorrect status code: %s. Expected: 403.' % _details.get(gcapi.STATUS_CODE))

				# DICOMweb API: create should also be denied
				try:
					test_sx_ltd.create_comment('Should not be created via DICOMweb', dicomweb_api=True)
					self.fail('Limited user able to create comment via DICOMweb without comment_edit permission')
				except ClientOperationError as err:
					_details = getattr(err, 'details', {})
					self.assertEqual(_details.get(gcapi.STATUS_CODE), 403,
						msg='DICOMweb create: incorrect status code: %s. Expected: 403.' % _details.get(gcapi.STATUS_CODE))

			# Grant comment_edit and allow propagation
			testacl.update({ 'comment_edit': True })
			testacl = iserver.get_acl(testacl.pk)
			sleep(1)

			# Phase 2: Grant — new session, verify both endpoints
			with self.getLimitedImageServer(iserver, testuser05, object_data={'description': 'Comment DICOMweb grant phase'}) as iserver_test2:

				test_sx_ltd2 = iserver_test2.get_series(test_sx.pk)

				# DICOMweb fetch — should succeed
				comments = test_sx_ltd2.fetch_comments(dicomweb_api=True, cache_response=True)
				self.assertTrue(
					getattr(comments, 'http_response', None) is not None
						and iserver.dicomweb_root in comments.http_response.url,
					msg='DICOMweb fetch response not routed through DICOMweb API endpoint')
				self.assertTrue(any(ctxt == c.text for c in comments),
					msg='Limited user unable to fetch comments via DICOMweb after permission granted')

				# DICOMweb create — should succeed
				new_ctxt = 'Test comment: created via DICOMweb after permission granted'
				c = test_sx_ltd2.create_comment(new_ctxt, dicomweb_api=True, cache_response=True)
				self.assertTrue(
					getattr(c, 'http_response', None) is not None
						and iserver.dicomweb_root in c.http_response.url,
					msg='DICOMweb create response not routed through DICOMweb API endpoint')

	def test_comment_global_deny_create_study(self, *args, **kwargs):
		'''	Verify that comment_edit permission at the global (server) level is
			enforced on study-level comments. Limited user can read study comments
			but cannot create them via either standard or DICOMweb API.
		'''
		iserver, testgroup01, testuser01 = self.setupTestAuth(
			testuser_config=TESTUSER01, testgroup_name=TESTGROUP01, **kwargs)

		# Download test series
		r_cx = self.fetchTestResource(self.nih_cxr_testdcm)

		# Stage test files to imaging server
		with self.stageImageArchiveSeries(iserver, response2filearchive(r_cx)) as (test_sx, test_hache):

			# Retrieve parent study reference
			test_s = iserver.get_study(test_sx.parent.pk)

			# Create server-level ACL with view but no comment_edit
			testacl = iserver.admin_create_acl(testgroup01, {
				'resource': '*', 'view': True, 'comment_view': True, 'comment_edit': False, 'duration': 1
			})

			# Admin creates a study-level comment
			ctxt = 'Test comment: study-level permission enforcement'
			admin_comment = test_s.create_comment(ctxt)
			self.assertTrue(any(ctxt == c.text for c in test_s.fetch_comments()),
				msg='Admin study comment did not persist to Orthanc')

			with self.getLimitedImageServer(iserver, testuser01, object_data={'description': 'Comment study permission testing'}) as iserver_test:

				test_s_ltd = iserver_test.get_study(test_sx.parent.pk)

				# Read should succeed (has view + comment_view)
				comments = test_s_ltd.fetch_comments()
				self.assertTrue(any(ctxt == c.text for c in comments),
					msg='Limited user unable to read study comments via standard API')

				# Standard API: create should be denied
				try:
					test_s_ltd.create_comment('Should not be created on study')
					self.fail('Limited user able to create study comment via standard API without comment_edit')
				except ClientOperationError as err:
					_details = getattr(err, 'details', {})
					self.assertEqual(_details.get(gcapi.STATUS_CODE), 403,
						msg='Study standard API create: incorrect status code: %s. Expected: 403.' % _details.get(gcapi.STATUS_CODE))

				# DICOMweb API: create should also be denied
				try:
					test_s_ltd.create_comment('Should not be created on study via DICOMweb', dicomweb_api=True)
					self.fail('Limited user able to create study comment via DICOMweb without comment_edit')
				except ClientOperationError as err:
					_details = getattr(err, 'details', {})
					self.assertEqual(_details.get(gcapi.STATUS_CODE), 403,
						msg='Study DICOMweb create: incorrect status code: %s. Expected: 403.' % _details.get(gcapi.STATUS_CODE))

	# -----------------------------------------------------------------------
	# Targeted method-level tests: isolate PUT and DELETE via each API path
	# to complete the coverage matrix for comment_edit enforcement.
	# Admin creates the comment; the limited user probes update/delete only.
	# -----------------------------------------------------------------------

	def test_comment_global_denied_update_dicomweb(self, *args, **kwargs):
		'''	DICOMweb PUT with comment_edit: False. Admin creates a comment,
			limited user attempts to update it via the DICOMweb endpoint.
		'''
		iserver, testgroup01, testuser01 = self.setupTestAuth(
			testuser_config=TESTUSER01, testgroup_name=TESTGROUP01, **kwargs)

		r_cx = self.fetchTestResource(self.nih_cxr_testdcm)

		with self.stageImageArchiveSeries(iserver, response2filearchive(r_cx)) as (test_sx, test_hache):

			testacl = iserver.admin_create_acl(testgroup01, {
				'resource': '*', 'view': True, 'comment_view': True, 'comment_edit': False, 'duration': 1
			})

			ctxt = 'Test comment: DICOMweb update deny probe'
			admin_comment = test_sx.create_comment(ctxt)

			with self.getLimitedImageServer(iserver, testuser01, object_data={'description': 'DICOMweb PUT deny'}) as iserver_test:

				test_sx_ltd = iserver_test.get_series(test_sx.pk)
				c_ltd = test_sx_ltd.get_comment(admin_comment.pk, dicomweb_api=True)

				try:
					c_ltd.update({ 'Text': '%s (modified)' % ctxt })
					self.fail('Limited user able to update comment via DICOMweb without comment_edit')
				except ClientOperationError as err:
					_details = getattr(err, 'details', {})
					self.assertEqual(_details.get(gcapi.STATUS_CODE), 403,
						msg='DICOMweb PUT deny: incorrect status code: %s. Expected: 403.' % _details.get(gcapi.STATUS_CODE))

	def test_comment_global_denied_delete_dicomweb(self, *args, **kwargs):
		'''	DICOMweb DELETE with comment_edit: False. Admin creates a comment,
			limited user attempts to delete it via the DICOMweb endpoint.
		'''
		iserver, testgroup01, testuser01 = self.setupTestAuth(
			testuser_config=TESTUSER01, testgroup_name=TESTGROUP01, **kwargs)

		r_cx = self.fetchTestResource(self.nih_cxr_testdcm)

		with self.stageImageArchiveSeries(iserver, response2filearchive(r_cx)) as (test_sx, test_hache):

			testacl = iserver.admin_create_acl(testgroup01, {
				'resource': '*', 'view': True, 'comment_view': True, 'comment_edit': False, 'duration': 1
			})

			ctxt = 'Test comment: DICOMweb delete deny probe'
			admin_comment = test_sx.create_comment(ctxt)

			with self.getLimitedImageServer(iserver, testuser01, object_data={'description': 'DICOMweb DELETE deny'}) as iserver_test:

				test_sx_ltd = iserver_test.get_series(test_sx.pk)
				c_ltd = test_sx_ltd.get_comment(admin_comment.pk, dicomweb_api=True)

				try:
					c_ltd.delete()
					self.fail('Limited user able to delete comment via DICOMweb without comment_edit')
				except ClientOperationError as err:
					_details = getattr(err, 'details', {})
					self.assertEqual(_details.get(gcapi.STATUS_CODE), 403,
						msg='DICOMweb DELETE deny: incorrect status code: %s. Expected: 403.' % _details.get(gcapi.STATUS_CODE))

				# Verify comment still exists
				c_admin = test_sx.get_comment(admin_comment.pk)
				self.assertEqual(c_admin.text, ctxt,
					msg='Comment was deleted despite permission being denied')

	def test_comment_global_authorized_update_standard(self, *args, **kwargs):
		'''	Standard API PUT with comment_edit: True. Limited user creates a comment
			then updates it via the standard Orthanc endpoint. Orthanc enforces
			authorship on PUT — only the comment creator can modify text — so the
			limited user must own the comment being updated.
		'''
		iserver, testgroup03, testuser03 = self.setupTestAuth(
			testuser_config=TESTUSER03, testgroup_name=TESTGROUP03, **kwargs)

		r_cx = self.fetchTestResource(self.nih_cxr_testdcm)

		with self.stageImageArchiveSeries(iserver, response2filearchive(r_cx)) as (test_sx, test_hache):

			testacl = iserver.admin_create_acl(testgroup03, {
				'resource': '*', 'view': True, 'comment_view': True, 'comment_edit': True, 'duration': 1
			})

			with self.getLimitedImageServer(iserver, testuser03, object_data={'description': 'Standard PUT grant'}) as iserver_test:

				test_sx_ltd = iserver_test.get_series(test_sx.pk)

				# Limited user creates their own comment, then updates it
				ctxt = 'Test comment: standard API update grant probe'
				c = test_sx_ltd.create_comment(ctxt)
				c_ltd = test_sx_ltd.get_comment(c.pk)

				utxt = '%s (updated by limited user)' % ctxt
				c_ltd.update({ 'Text': utxt })

				c_verify = test_sx.get_comment(c.pk)
				self.assertEqual(c_verify.text, utxt,
					msg='Standard API PUT with comment_edit: True did not persist')

	def test_comment_global_comment_edit_cannot_delete_other_user_standard(self, *args, **kwargs):
		'''	Standard API DELETE with comment_edit: True but remove: False. Admin creates a
			comment; the limited user, who is not its author, is refused (400, User field) and
			the comment is left in place. comment_edit covers a user's own comments only.
		'''
		iserver, testgroup03, testuser03 = self.setupTestAuth(
			testuser_config=TESTUSER03, testgroup_name=TESTGROUP03, **kwargs)

		r_cx = self.fetchTestResource(self.nih_cxr_testdcm)

		with self.stageImageArchiveSeries(iserver, response2filearchive(r_cx)) as (test_sx, test_hache):

			testacl = iserver.admin_create_acl(testgroup03, {
				'resource': '*', 'view': True, 'comment_view': True, 'comment_edit': True, 'remove': False, 'duration': 1
			})

			ctxt = 'Test comment: standard API delete of another user comment without remove'
			admin_comment = test_sx.create_comment(ctxt)

			with self.getLimitedImageServer(iserver, testuser03, object_data={'description': 'Standard DELETE other user, no remove'}) as iserver_test:

				test_sx_ltd = iserver_test.get_series(test_sx.pk)
				c_ltd = test_sx_ltd.get_comment(admin_comment.pk)
				self._assertRemovalRefused(c_ltd)

				c_admin = test_sx.get_comment(admin_comment.pk)
				self.assertEqual(c_admin.text, ctxt,
					msg='Comment was deleted by a non-author holding only comment_edit')

	def test_comment_global_remove_deletes_other_user_standard(self, *args, **kwargs):
		'''	Standard API DELETE with remove: True. Admin creates a comment; the limited user, who
			is not its author, removes it and the response names the comment and the series.
		'''
		iserver, testgroup03, testuser03 = self.setupTestAuth(
			testuser_config=TESTUSER03, testgroup_name=TESTGROUP03, **kwargs)

		r_cx = self.fetchTestResource(self.nih_cxr_testdcm)

		with self.stageImageArchiveSeries(iserver, response2filearchive(r_cx)) as (test_sx, test_hache):

			testacl = iserver.admin_create_acl(testgroup03, {
				'resource': '*', 'view': True, 'comment_view': True, 'comment_edit': True, 'remove': True, 'duration': 1
			})

			ctxt = 'Test comment: standard API delete of another user comment with remove'
			admin_comment = test_sx.create_comment(ctxt)

			with self.getLimitedImageServer(iserver, testuser03, object_data={'description': 'Standard DELETE other user, remove'}) as iserver_test:

				test_sx_ltd = iserver_test.get_series(test_sx.pk)
				c_ltd = test_sx_ltd.get_comment(admin_comment.pk)
				r_del = c_ltd.delete()

				self._assertRemovalResponse(r_del, admin_comment.pk, 'Series', test_sx.pk)

				remaining = test_sx.fetch_comments()
				self.assertTrue(all(ctxt != _c.text for _c in remaining),
					msg='Standard API DELETE with remove: True did not remove the comment')

	def test_comment_global_authorized_update_dicomweb(self, *args, **kwargs):
		'''	DICOMweb PUT with comment_edit: True. Limited user creates a comment
			via DICOMweb then updates it via the DICOMweb endpoint. Orthanc enforces
			authorship on PUT — only the comment creator can modify text — so the
			limited user must own the comment being updated.
		'''
		iserver, testgroup05, testuser05 = self.setupTestAuth(
			testuser_config=TESTUSER05, testgroup_name=TESTGROUP05, **kwargs)

		r_cx = self.fetchTestResource(self.nih_cxr_testdcm)

		with self.stageImageArchiveSeries(iserver, response2filearchive(r_cx)) as (test_sx, test_hache):

			testacl = iserver.admin_create_acl(testgroup05, {
				'resource': '*', 'view': True, 'comment_view': True, 'comment_edit': True, 'duration': 1
			})

			with self.getLimitedImageServer(iserver, testuser05, object_data={'description': 'DICOMweb PUT grant'}) as iserver_test:

				test_sx_ltd = iserver_test.get_series(test_sx.pk)

				# Limited user creates their own comment via DICOMweb, then updates it
				ctxt = 'Test comment: DICOMweb update grant probe'
				c = test_sx_ltd.create_comment(ctxt, dicomweb_api=True)
				c_ltd = test_sx_ltd.get_comment(c.pk, dicomweb_api=True)

				utxt = '%s (updated by limited user via DICOMweb)' % ctxt
				c_ltd.update({ 'Text': utxt })

				c_verify = test_sx.get_comment(c.pk)
				self.assertEqual(c_verify.text, utxt,
					msg='DICOMweb PUT with comment_edit: True did not persist')

	def test_comment_global_remove_deletes_other_user_dicomweb(self, *args, **kwargs):
		'''	DICOMweb DELETE with remove: True. Admin creates a comment; the limited user, who is
			not its author, removes it through the DICOMweb route. The same user is then refused
			on a second comment once remove is withdrawn.
		'''
		iserver, testgroup05, testuser05 = self.setupTestAuth(
			testuser_config=TESTUSER05, testgroup_name=TESTGROUP05, **kwargs)

		r_cx = self.fetchTestResource(self.nih_cxr_testdcm)

		with self.stageImageArchiveSeries(iserver, response2filearchive(r_cx)) as (test_sx, test_hache):

			testacl = iserver.admin_create_acl(testgroup05, {
				'resource': '*', 'view': True, 'comment_view': True, 'comment_edit': True, 'remove': True, 'duration': 1
			})

			ctxt = 'Test comment: DICOMweb delete of another user comment with remove'
			ctxt_kept = 'Test comment: DICOMweb delete of another user comment without remove'
			admin_comment = test_sx.create_comment(ctxt)
			admin_comment_kept = test_sx.create_comment(ctxt_kept)

			with self.getLimitedImageServer(iserver, testuser05, object_data={'description': 'DICOMweb DELETE other user, remove'}) as iserver_test:

				test_sx_ltd = iserver_test.get_series(test_sx.pk)
				c_ltd = test_sx_ltd.get_comment(admin_comment.pk, dicomweb_api=True)
				r_del = c_ltd.delete()

				self._assertRemovalResponse(r_del, admin_comment.pk, 'Series', test_sx.series_uid,
					dicomweb_root=iserver.dicomweb_root)

				remaining = test_sx.fetch_comments()
				self.assertTrue(all(ctxt != _c.text for _c in remaining),
					msg='DICOMweb DELETE with remove: True did not remove the comment')

			# Withdraw remove and allow propagation
			testacl.update({ 'remove': False })
			testacl = iserver.get_acl(testacl.pk)
			sleep(1)

			with self.getLimitedImageServer(iserver, testuser05, object_data={'description': 'DICOMweb DELETE other user, no remove'}) as iserver_test2:

				test_sx_ltd2 = iserver_test2.get_series(test_sx.pk)
				c_kept = test_sx_ltd2.get_comment(admin_comment_kept.pk, dicomweb_api=True)
				self._assertRemovalRefused(c_kept)

				c_admin = test_sx.get_comment(admin_comment_kept.pk)
				self.assertEqual(c_admin.text, ctxt_kept,
					msg='Comment was deleted through DICOMweb by a non-author holding only comment_edit')

	def test_comment_local_acl_authorized_update_delete(self, *args, **kwargs):
		'''	Local (Orthanc resource-level) ACL with CommentEdit: True. Limited user
			creates, updates, and deletes a comment via standard API. Server ACL
			denies all comment permissions; only the local ACL grants. Orthanc
			enforces authorship on PUT, so the limited user creates their own comment.
		'''
		iserver, testgroup04, testuser04 = self.setupTestAuth(
			testuser_config=TESTUSER04, testgroup_name=TESTGROUP04, **kwargs)

		r_cx = self.fetchTestResource(self.nih_cxr_testdcm)

		with self.stageImageArchiveSeries(iserver, response2filearchive(r_cx)) as (test_sx, test_hache):

			# Server ACL: deny all comment permissions
			testacl = iserver.admin_create_acl(testgroup04, {
				'resource': '*', 'query': False, 'view': False, 'modify': False, 'remove': False,
				'acl': False, 'comment_view': False, 'comment_edit': False, 'duration': 5
			})

			# Local ACL: grant everything
			testacl_local = test_sx.create_group_acl(testgroup04, {
				'View': True, 'Modify': True, 'Remove': False,
				'CommentView': True, 'CommentEdit': True, 'ACL': False
			})

			with self.getLimitedImageServer(iserver, testuser04, object_data={'description': 'Local ACL PUT+DELETE grant'}) as iserver_test:

				test_sx_ltd = iserver_test.get_series(test_sx.pk)

				# Limited user creates their own comment via local ACL grant
				ctxt = 'Test comment: local ACL update/delete grant probe'
				c = test_sx_ltd.create_comment(ctxt)
				c_ltd = test_sx_ltd.get_comment(c.pk)

				# Update (user owns the comment, local ACL grants CommentEdit)
				utxt = '%s (updated via local ACL)' % ctxt
				c_ltd.update({ 'Text': utxt })
				c_verify = test_sx.get_comment(c.pk)
				self.assertEqual(c_verify.text, utxt,
					msg='Local ACL PUT with CommentEdit: True did not persist')

				# Delete
				c_ltd_refresh = test_sx_ltd.get_comment(c.pk)
				c_ltd_refresh.delete()
				remaining = test_sx.fetch_comments()
				self.assertTrue(all(utxt != _c.text for _c in remaining),
					msg='Local ACL DELETE with CommentEdit: True did not remove the comment')

	# -----------------------------------------------------------------------
	# Comment removal (imaging-development-env#99). The server resolves the request user on
	# DELETE and permits removal to the author or to a holder of comment_edit on the resource;
	# the response identifies the removed comment and its parent.
	# -----------------------------------------------------------------------

	def _assertRemovalResponse(self, r_del, comment_pk, parent_key, parent_uid, dicomweb_root=None):
		'''	Shared assertions for a successful comment DELETE response.
		'''
		self.assertEqual(r_del.status_code, 200,
			msg='Comment DELETE returned status %s. Expected: 200.' % r_del.status_code)

		_json = r_del.json()
		self.assertEqual(_json.get('ID'), comment_pk,
			msg='Comment DELETE response does not identify the removed comment: %s' % _json)
		self.assertEqual(_json.get(gcapi.STATUS), gcapi.SUCCESS,
			msg='Comment DELETE response does not report success: %s' % _json)
		self.assertEqual(_json.get(parent_key), parent_uid,
			msg='Comment DELETE response does not identify the parent resource (%s): %s' % (parent_key, _json))

		if dicomweb_root:
			self.assertTrue(dicomweb_root in r_del.url,
				msg='Comment DELETE request not routed through DICOMweb API endpoint')

	def _assertRemovalRefused(self, comment):
		'''	Shared assertion for a DELETE the server refuses under the removal rule: a 400 with
			the User field in error.
		'''
		try:
			comment.delete()
			self.fail('Non-author without remove was able to delete the comment')
		except ClientOperationError as err:
			_details = getattr(err, 'details', {})
			self.assertEqual(_details.get(gcapi.STATUS_CODE), 400,
				msg='Removal refusal: incorrect status code: %s. Expected: 400.' % _details.get(gcapi.STATUS_CODE))

	def test_comment_global_remove_deletes_other_user_study_standard(self, *args, **kwargs):
		'''	Standard API DELETE on a study comment with remove: True. Admin creates the comment;
			the limited user (not the author) removes it and the response names the comment and
			the study.
		'''
		iserver, testgroup03, testuser03 = self.setupTestAuth(
			testuser_config=TESTUSER03, testgroup_name=TESTGROUP03, **kwargs)

		r_cx = self.fetchTestResource(self.nih_cxr_testdcm)

		with self.stageImageArchiveSeries(iserver, response2filearchive(r_cx)) as (test_sx, test_hache):

			test_s = iserver.get_study(test_sx.parent.pk)

			testacl = iserver.admin_create_acl(testgroup03, {
				'resource': '*', 'view': True, 'comment_view': True, 'comment_edit': True, 'remove': True, 'duration': 1
			})

			ctxt = 'Test comment: study standard API delete by remove holder'
			admin_comment = test_s.create_comment(ctxt)

			with self.getLimitedImageServer(iserver, testuser03, object_data={'description': 'Study standard DELETE grant'}) as iserver_test:

				test_s_ltd = iserver_test.get_study(test_sx.parent.pk)
				c_ltd = test_s_ltd.get_comment(admin_comment.pk)
				r_del = c_ltd.delete()

				self._assertRemovalResponse(r_del, admin_comment.pk, 'Study', test_s.pk)

				remaining = test_s.fetch_comments()
				self.assertTrue(all(ctxt != _c.text for _c in remaining),
					msg='Study comment still present after standard API DELETE')

	def test_comment_global_remove_deletes_other_user_study_dicomweb(self, *args, **kwargs):
		'''	DICOMweb DELETE on a study comment with remove: True. Admin creates the comment; the
			limited user removes it through the DICOMweb route and the response names the comment
			and the study's DICOM UID.
		'''
		iserver, testgroup05, testuser05 = self.setupTestAuth(
			testuser_config=TESTUSER05, testgroup_name=TESTGROUP05, **kwargs)

		r_cx = self.fetchTestResource(self.nih_cxr_testdcm)

		with self.stageImageArchiveSeries(iserver, response2filearchive(r_cx)) as (test_sx, test_hache):

			test_s = iserver.get_study(test_sx.parent.pk)

			testacl = iserver.admin_create_acl(testgroup05, {
				'resource': '*', 'view': True, 'comment_view': True, 'comment_edit': True, 'remove': True, 'duration': 1
			})

			ctxt = 'Test comment: study DICOMweb delete by remove holder'
			admin_comment = test_s.create_comment(ctxt)

			with self.getLimitedImageServer(iserver, testuser05, object_data={'description': 'Study DICOMweb DELETE grant'}) as iserver_test:

				test_s_ltd = iserver_test.get_study(test_sx.parent.pk)
				c_ltd = test_s_ltd.get_comment(admin_comment.pk, dicomweb_api=True)
				r_del = c_ltd.delete()

				self._assertRemovalResponse(r_del, admin_comment.pk, 'Study', test_s.study_uid,
					dicomweb_root=iserver.dicomweb_root)

				remaining = test_s.fetch_comments()
				self.assertTrue(all(ctxt != _c.text for _c in remaining),
					msg='Study comment still present after DICOMweb DELETE')

	def test_comment_global_denied_delete_study_standard(self, *args, **kwargs):
		'''	Standard API DELETE on a study comment with comment_edit: False is refused with a
			403 and the comment is left in place.
		'''
		iserver, testgroup01, testuser01 = self.setupTestAuth(
			testuser_config=TESTUSER01, testgroup_name=TESTGROUP01, **kwargs)

		r_cx = self.fetchTestResource(self.nih_cxr_testdcm)

		with self.stageImageArchiveSeries(iserver, response2filearchive(r_cx)) as (test_sx, test_hache):

			test_s = iserver.get_study(test_sx.parent.pk)

			testacl = iserver.admin_create_acl(testgroup01, {
				'resource': '*', 'view': True, 'comment_view': True, 'comment_edit': False, 'duration': 1
			})

			ctxt = 'Test comment: study delete deny probe'
			admin_comment = test_s.create_comment(ctxt)

			with self.getLimitedImageServer(iserver, testuser01, object_data={'description': 'Study DELETE deny'}) as iserver_test:

				test_s_ltd = iserver_test.get_study(test_sx.parent.pk)
				c_ltd = test_s_ltd.get_comment(admin_comment.pk)

				try:
					c_ltd.delete()
					self.fail('Limited user able to delete study comment without comment_edit')
				except ClientOperationError as err:
					_details = getattr(err, 'details', {})
					self.assertEqual(_details.get(gcapi.STATUS_CODE), 403,
						msg='Study DELETE deny: incorrect status code: %s. Expected: 403.' % _details.get(gcapi.STATUS_CODE))

				c_admin = test_s.get_comment(admin_comment.pk)
				self.assertEqual(c_admin.text, ctxt,
					msg='Study comment was deleted despite permission being denied')

	def test_comment_local_acl_remove_deletes_other_user(self, *args, **kwargs):
		'''	Local (Orthanc resource-level) ACL with CommentEdit and Remove lets the limited user
			remove a comment they did not write, on both the standard and the DICOMweb route. The
			server ACL denies every permission, so the local grant is the only thing authorizing
			the removal. With Remove withdrawn from the local policy the same user is refused.
		'''
		iserver, testgroup04, testuser04 = self.setupTestAuth(
			testuser_config=TESTUSER04, testgroup_name=TESTGROUP04, **kwargs)

		r_cx = self.fetchTestResource(self.nih_cxr_testdcm)

		with self.stageImageArchiveSeries(iserver, response2filearchive(r_cx)) as (test_sx, test_hache):

			testacl = iserver.admin_create_acl(testgroup04, {
				'resource': '*', 'query': False, 'view': False, 'modify': False, 'remove': False,
				'acl': False, 'comment_view': False, 'comment_edit': False, 'duration': 5
			})

			testacl_local = test_sx.create_group_acl(testgroup04, {
				'View': True, 'Modify': False, 'Remove': True,
				'CommentView': True, 'CommentEdit': True, 'ACL': False
			})

			ctxt_std = 'Test comment: local ACL removal of another user comment (standard)'
			ctxt_dcm = 'Test comment: local ACL removal of another user comment (DICOMweb)'
			ctxt_kept = 'Test comment: local ACL removal of another user comment without Remove'
			admin_comment_std = test_sx.create_comment(ctxt_std)
			admin_comment_dcm = test_sx.create_comment(ctxt_dcm)
			admin_comment_kept = test_sx.create_comment(ctxt_kept)

			with self.getLimitedImageServer(iserver, testuser04, object_data={'description': 'Local ACL DELETE of other user comment'}) as iserver_test:

				test_sx_ltd = iserver_test.get_series(test_sx.pk)

				c_std = test_sx_ltd.get_comment(admin_comment_std.pk)
				r_del_std = c_std.delete()
				self._assertRemovalResponse(r_del_std, admin_comment_std.pk, 'Series', test_sx.pk)

				c_dcm = test_sx_ltd.get_comment(admin_comment_dcm.pk, dicomweb_api=True)
				r_del_dcm = c_dcm.delete()
				self._assertRemovalResponse(r_del_dcm, admin_comment_dcm.pk, 'Series', test_sx.series_uid,
					dicomweb_root=iserver.dicomweb_root)

				remaining = [_c.text for _c in test_sx.fetch_comments()]
				self.assertTrue(ctxt_std not in remaining and ctxt_dcm not in remaining,
					msg='Comments still present after local-ACL authorized DELETE: %s' % remaining)

			# Withdraw Remove from the local policy; CommentEdit alone must not cover another user's comment
			testacl_local.update({ 'Remove': False })
			sleep(1)

			with self.getLimitedImageServer(iserver, testuser04, object_data={'description': 'Local ACL DELETE of other user comment, no Remove'}) as iserver_test2:

				test_sx_ltd2 = iserver_test2.get_series(test_sx.pk)
				c_kept = test_sx_ltd2.get_comment(admin_comment_kept.pk)
				self._assertRemovalRefused(c_kept)

				c_admin = test_sx.get_comment(admin_comment_kept.pk)
				self.assertEqual(c_admin.text, ctxt_kept,
					msg='Comment was deleted under a local policy granting CommentEdit but not Remove')

	# -----------------------------------------------------------------------
	# Permission matrix for comment writes (imaging-development-env#99, revised rule):
	#   comment_edit -> add; edit and remove OWN comments
	#   remove       -> remove ANY comment; no add, no edit
	#   modify       -> no comment write at all
	# -----------------------------------------------------------------------

	def _assertStatus(self, request_callable, status_code, msg):
		'''	Assert that the callable is refused with the given HTTP status.
		'''
		try:
			request_callable()
			self.fail(msg)
		except ClientOperationError as err:
			_details = getattr(err, 'details', {})
			self.assertEqual(_details.get(gcapi.STATUS_CODE), status_code,
				msg='%s: incorrect status code %s. Expected: %s.' % (msg, _details.get(gcapi.STATUS_CODE), status_code))

	def test_comment_remove_only_deletes_any_comment_without_add_or_edit(self, *args, **kwargs):
		'''	A user holding `remove` but not `comment_edit` may delete any comment on the resource,
			on the standard and the DICOMweb route, but may neither add a comment nor edit one.
		'''
		iserver, testgroup03, testuser03 = self.setupTestAuth(
			testuser_config=TESTUSER03, testgroup_name=TESTGROUP03, **kwargs)

		r_cx = self.fetchTestResource(self.nih_cxr_testdcm)

		with self.stageImageArchiveSeries(iserver, response2filearchive(r_cx)) as (test_sx, test_hache):

			testacl = iserver.admin_create_acl(testgroup03, {
				'resource': '*', 'view': True, 'comment_view': True, 'comment_edit': False, 'remove': True, 'duration': 1
			})

			ctxt_std = 'Test comment: removed by remove-only user (standard)'
			ctxt_dcm = 'Test comment: removed by remove-only user (DICOMweb)'
			ctxt_kept = 'Test comment: remove-only user may not edit this'
			admin_std = test_sx.create_comment(ctxt_std)
			admin_dcm = test_sx.create_comment(ctxt_dcm)
			admin_kept = test_sx.create_comment(ctxt_kept)

			with self.getLimitedImageServer(iserver, testuser03, object_data={'description': 'remove-only comment matrix'}) as iserver_test:

				test_sx_ltd = iserver_test.get_series(test_sx.pk)

				self._assertStatus(lambda: test_sx_ltd.create_comment('remove-only user must not add'), 403,
					'remove-only user was able to add a comment')
				self._assertStatus(lambda: test_sx_ltd.get_comment(admin_kept.pk).update({ 'Text': 'edited' }), 403,
					'remove-only user was able to edit a comment')

				r_del = test_sx_ltd.get_comment(admin_std.pk).delete()
				self._assertRemovalResponse(r_del, admin_std.pk, 'Series', test_sx.pk)

				r_del_dcm = test_sx_ltd.get_comment(admin_dcm.pk, dicomweb_api=True).delete()
				self._assertRemovalResponse(r_del_dcm, admin_dcm.pk, 'Series', test_sx.series_uid,
					dicomweb_root=iserver.dicomweb_root)

			remaining = [_c.text for _c in test_sx.fetch_comments()]
			self.assertTrue(ctxt_std not in remaining and ctxt_dcm not in remaining and ctxt_kept in remaining,
				msg='Unexpected comments after remove-only matrix: %s' % remaining)

	def test_comment_modify_only_has_no_comment_write(self, *args, **kwargs):
		'''	A user holding imaging-resource `modify` but no comment grant and no `remove` can read
			comments but neither add, edit nor delete them.
		'''
		iserver, testgroup01, testuser01 = self.setupTestAuth(
			testuser_config=TESTUSER01, testgroup_name=TESTGROUP01, **kwargs)

		r_cx = self.fetchTestResource(self.nih_cxr_testdcm)

		with self.stageImageArchiveSeries(iserver, response2filearchive(r_cx)) as (test_sx, test_hache):

			testacl = iserver.admin_create_acl(testgroup01, {
				'resource': '*', 'view': True, 'modify': True, 'remove': False,
				'comment_view': True, 'comment_edit': False, 'duration': 1
			})

			ctxt = 'Test comment: modify-only user must not touch this'
			admin_comment = test_sx.create_comment(ctxt)

			with self.getLimitedImageServer(iserver, testuser01, object_data={'description': 'modify-only comment matrix'}) as iserver_test:

				test_sx_ltd = iserver_test.get_series(test_sx.pk)
				self.assertTrue(any(ctxt == c.text for c in test_sx_ltd.fetch_comments()),
					msg='modify-only user with comment_view could not read comments')

				c_ltd = test_sx_ltd.get_comment(admin_comment.pk)
				self._assertStatus(lambda: test_sx_ltd.create_comment('modify-only user must not add'), 403,
					'modify-only user was able to add a comment')
				self._assertStatus(lambda: c_ltd.update({ 'Text': 'edited' }), 403,
					'modify-only user was able to edit a comment')
				self._assertStatus(lambda: c_ltd.delete(), 403,
					'modify-only user was able to delete a comment')

			self.assertEqual(test_sx.get_comment(admin_comment.pk).text, ctxt,
				msg='Comment changed by a modify-only user')

	def test_comment_edit_cannot_update_other_user_comment(self, *args, **kwargs):
		'''	Editing stays author-only: a comment manager, even one who also holds `remove`, is
			refused (400, User field) when changing another user's text, and the text is unchanged.
		'''
		iserver, testgroup03, testuser03 = self.setupTestAuth(
			testuser_config=TESTUSER03, testgroup_name=TESTGROUP03, **kwargs)

		r_cx = self.fetchTestResource(self.nih_cxr_testdcm)

		with self.stageImageArchiveSeries(iserver, response2filearchive(r_cx)) as (test_sx, test_hache):

			testacl = iserver.admin_create_acl(testgroup03, {
				'resource': '*', 'view': True, 'comment_view': True, 'comment_edit': True, 'remove': True, 'duration': 1
			})

			ctxt = 'Test comment: author-only editing'
			admin_comment = test_sx.create_comment(ctxt)

			with self.getLimitedImageServer(iserver, testuser03, object_data={'description': 'author-only edit'}) as iserver_test:

				test_sx_ltd = iserver_test.get_series(test_sx.pk)
				self._assertStatus(lambda: test_sx_ltd.get_comment(admin_comment.pk).update({ 'Text': 'edited by another user' }), 400,
					'Another user was able to edit the comment')
				self._assertStatus(lambda: test_sx_ltd.get_comment(admin_comment.pk, dicomweb_api=True).update({ 'Text': 'edited via DICOMweb' }), 400,
					'Another user was able to edit the comment through DICOMweb')

			self.assertEqual(test_sx.get_comment(admin_comment.pk).text, ctxt,
				msg='Comment text changed by a non-author')

	def test_comment_owner_loses_write_after_comment_edit_revoked(self, *args, **kwargs):
		'''	Authorship does not survive revocation: once `comment_edit` is withdrawn, the author can
			no longer edit or remove their own comment (403 from the outer authorization).
		'''
		iserver, testgroup05, testuser05 = self.setupTestAuth(
			testuser_config=TESTUSER05, testgroup_name=TESTGROUP05, **kwargs)

		r_cx = self.fetchTestResource(self.nih_cxr_testdcm)

		with self.stageImageArchiveSeries(iserver, response2filearchive(r_cx)) as (test_sx, test_hache):

			testacl = iserver.admin_create_acl(testgroup05, {
				'resource': '*', 'view': True, 'comment_view': True, 'comment_edit': True, 'remove': False, 'duration': 1
			})

			ctxt = 'Test comment: written before revocation'

			with self.getLimitedImageServer(iserver, testuser05, object_data={'description': 'before revocation'}) as iserver_test:
				own = iserver_test.get_series(test_sx.pk).create_comment(ctxt)

			testacl.update({ 'comment_edit': False })
			testacl = iserver.get_acl(testacl.pk)
			sleep(1)

			with self.getLimitedImageServer(iserver, testuser05, object_data={'description': 'after revocation'}) as iserver_test2:
				c_own = iserver_test2.get_series(test_sx.pk).get_comment(own.pk)
				self._assertStatus(lambda: c_own.update({ 'Text': 'edited after revocation' }), 403,
					'Author edited their comment after comment_edit was revoked')
				self._assertStatus(lambda: c_own.delete(), 403,
					'Author removed their comment after comment_edit was revoked')

			self.assertEqual(test_sx.get_comment(own.pk).text, ctxt,
				msg='Comment changed after revocation')

	def test_comment_wrong_parent_is_not_found(self, *args, **kwargs):
		'''	A comment addressed through a resource it does not belong to is not found (404), so a
			grant on one resource cannot reach comments of another. Uses a study comment addressed
			through the series route.
		'''
		iserver, testgroup03, testuser03 = self.setupTestAuth(
			testuser_config=TESTUSER03, testgroup_name=TESTGROUP03, **kwargs)

		r_cx = self.fetchTestResource(self.nih_cxr_testdcm)

		with self.stageImageArchiveSeries(iserver, response2filearchive(r_cx)) as (test_sx, test_hache):

			test_s = iserver.get_study(test_sx.parent.pk)
			testacl = iserver.admin_create_acl(testgroup03, {
				'resource': '*', 'view': True, 'comment_view': True, 'comment_edit': True, 'remove': True, 'duration': 1
			})

			ctxt = 'Test comment: study comment addressed through the series route'
			study_comment = test_s.create_comment(ctxt)

			with self.getLimitedImageServer(iserver, testuser03, object_data={'description': 'wrong parent'}) as iserver_test:
				test_sx_ltd = iserver_test.get_series(test_sx.pk)
				wrong_url = iserver_test.orthanc_apiurl(posixpath.join(test_sx_ltd.comments_url, study_comment.pk))
				headers = iserver_test.orthanc_request_headers()

				self.assertEqual(requests.get(wrong_url, headers=headers).status_code, 404)
				self.assertEqual(requests.delete(wrong_url, headers=headers).status_code, 404)

			self.assertEqual(test_s.get_comment(study_comment.pk).text, ctxt,
				msg='Study comment affected through the series route')

	# -----------------------------------------------------------------------
	# Parent binding, sibling isolation, mixed policies, and update preservation
	# -----------------------------------------------------------------------

	@contextlib.contextmanager
	def _siblingSeries(self, iserver, test_sx, r_archive):
		'''	Stage a second series in the same study as `test_sx`: the archive's first DICOM file,
			re-identified with a fresh SeriesInstanceUID and SOPInstanceUID under the study's UID.
			Removes the series on exit.
		'''
		with zipfile.ZipFile(BytesIO(r_archive.content)) as zf:
			name = next(n for n in zf.namelist() if not n.endswith('/'))
			ds = pydicom.dcmread(BytesIO(zf.read(name)))

		ds.StudyInstanceUID = test_sx.parent.study_uid
		ds.SeriesInstanceUID = generate_uid()
		ds.SOPInstanceUID = generate_uid()
		ds.SeriesDescription = 'Sibling series for isolation test'
		ds.SeriesNumber = 99

		stream = BytesIO()
		ds.save_as(stream)
		stream.seek(0)
		iserver.upload_image(stream)
		sleep(0.25)

		results = iserver.query({ DCMHEADER_SERIES_INSTANCE_UID: ds.SeriesInstanceUID }, rapid_lookup=False)
		self.assertEqual(len(results), 1, msg='Unable to retrieve the staged sibling series')
		sibling = results[0]

		try:
			yield sibling
		finally:
			try: sibling.delete()
			except Exception as err:
				logger.warning('Unable to remove sibling series %s: %s' % (sibling.pk, err))

	def test_comment_series_grant_does_not_reach_sibling_series(self, *args, **kwargs):
		'''	A local policy on series A (with Remove) lets the user remove A's comments but gives no
			access to sibling series B in the same study: B's comment is not reachable through B's
			own route (403), nor through A's route with B's comment id (404, same table), on the
			standard and DICOMweb routes. B's comment is unchanged.
		'''
		iserver, testgroup04, testuser04 = self.setupTestAuth(
			testuser_config=TESTUSER04, testgroup_name=TESTGROUP04, **kwargs)

		r_cx = self.fetchTestResource(self.nih_cxr_testdcm)

		with self.stageImageArchiveSeries(iserver, response2filearchive(r_cx)) as (series_a, test_hache):
			with self._siblingSeries(iserver, series_a, r_cx) as series_b:

				testacl = iserver.admin_create_acl(testgroup04, {
					'resource': '*', 'query': False, 'view': False, 'modify': False, 'remove': False,
					'acl': False, 'comment_view': False, 'comment_edit': False, 'duration': 5
				})
				testacl_local = series_a.create_group_acl(testgroup04, {
					'View': True, 'Modify': False, 'Remove': True,
					'CommentView': True, 'CommentEdit': True, 'ACL': False
				})

				ctxt_a = 'Test comment: on series A, removable through the local grant'
				ctxt_b = 'Test comment: on sibling series B, out of reach'
				comment_a = series_a.create_comment(ctxt_a)
				comment_b = series_b.create_comment(ctxt_b)

				with self.getLimitedImageServer(iserver, testuser04, object_data={'description': 'sibling isolation'}) as iserver_test:

					headers = iserver_test.orthanc_request_headers()
					a_ltd = iserver_test.get_series(series_a.pk)

					# A's own comment is reachable and removable
					self.assertEqual(a_ltd.get_comment(comment_a.pk).text, ctxt_a)

					# B through its own routes: no grant, so the outer authorization refuses
					b_std = iserver_test.orthanc_apiurl(posixpath.join(series_b.comments_url, comment_b.pk))
					b_dcm = iserver_test.orthanc_apiurl(posixpath.join(series_b.dicomweb_comments_url, comment_b.pk))
					for url in (b_std, b_dcm):
						self.assertEqual(requests.get(url, headers=headers).status_code, 403, msg=url)
						self.assertEqual(requests.put(url, headers=headers, json={ 'Text': 'x' }).status_code, 403, msg=url)
						self.assertEqual(requests.delete(url, headers=headers).status_code, 403, msg=url)

					# B's comment id through A's routes: same table, wrong parent, not found
					a_std = iserver_test.orthanc_apiurl(posixpath.join(series_a.comments_url, comment_b.pk))
					a_dcm = iserver_test.orthanc_apiurl(posixpath.join(series_a.dicomweb_comments_url, comment_b.pk))
					for url in (a_std, a_dcm):
						self.assertEqual(requests.get(url, headers=headers).status_code, 404, msg=url)
						self.assertEqual(requests.put(url, headers=headers, json={ 'Text': 'x' }).status_code, 404, msg=url)
						self.assertEqual(requests.delete(url, headers=headers).status_code, 404, msg=url)

					# and A's comment is removable by the local Remove grant
					r_del = a_ltd.get_comment(comment_a.pk).delete()
					self._assertRemovalResponse(r_del, comment_a.pk, 'Series', series_a.pk)

				self.assertEqual(series_b.get_comment(comment_b.pk).text, ctxt_b,
					msg="Sibling series' comment was changed")

	def test_comment_global_remove_survives_local_comment_edit_deny(self, *args, **kwargs):
		'''	Mixed policy: a global `remove` grant with a local policy that denies CommentEdit on the
			series. The user still removes another user's comment (the remove arm is evaluated
			independently) but cannot add or edit (the comment-management arm is denied locally).
		'''
		iserver, testgroup01, testuser01 = self.setupTestAuth(
			testuser_config=TESTUSER01, testgroup_name=TESTGROUP01, **kwargs)

		r_cx = self.fetchTestResource(self.nih_cxr_testdcm)

		with self.stageImageArchiveSeries(iserver, response2filearchive(r_cx)) as (test_sx, test_hache):

			testacl = iserver.admin_create_acl(testgroup01, {
				'resource': '*', 'view': True, 'comment_view': True, 'comment_edit': True, 'remove': True, 'duration': 1
			})
			testacl_local = test_sx.create_group_acl(testgroup01, {
				'View': True, 'Modify': False, 'Remove': False,
				'CommentView': True, 'CommentEdit': False, 'ACL': False
			})

			ctxt_std = 'Test comment: removed under global remove despite local CommentEdit deny (standard)'
			ctxt_dcm = 'Test comment: removed under global remove despite local CommentEdit deny (DICOMweb)'
			ctxt_kept = 'Test comment: not editable under local CommentEdit deny'
			admin_std = test_sx.create_comment(ctxt_std)
			admin_dcm = test_sx.create_comment(ctxt_dcm)
			admin_kept = test_sx.create_comment(ctxt_kept)

			with self.getLimitedImageServer(iserver, testuser01, object_data={'description': 'mixed policy'}) as iserver_test:

				test_sx_ltd = iserver_test.get_series(test_sx.pk)

				self._assertStatus(lambda: test_sx_ltd.create_comment('must not add under local deny'), 403,
					'User added a comment despite the local CommentEdit deny')
				self._assertStatus(lambda: test_sx_ltd.get_comment(admin_kept.pk).update({ 'Text': 'edited' }), 403,
					'User edited a comment despite the local CommentEdit deny')

				r_del = test_sx_ltd.get_comment(admin_std.pk).delete()
				self._assertRemovalResponse(r_del, admin_std.pk, 'Series', test_sx.pk)

				r_del_dcm = test_sx_ltd.get_comment(admin_dcm.pk, dicomweb_api=True).delete()
				self._assertRemovalResponse(r_del_dcm, admin_dcm.pk, 'Series', test_sx.series_uid,
					dicomweb_root=iserver.dicomweb_root)

			remaining = [_c.text for _c in test_sx.fetch_comments()]
			self.assertTrue(ctxt_std not in remaining and ctxt_dcm not in remaining and ctxt_kept in remaining,
				msg='Unexpected comments after mixed-policy removal: %s' % remaining)

	def test_comment_text_only_update_keeps_meta(self, *args, **kwargs):
		'''	Updating the text leaves the comment's metadata, author and creation time untouched and
			moves the modification time, on both routes.
		'''
		iserver, testgroup03, testuser03 = self.setupTestAuth(
			testuser_config=TESTUSER03, testgroup_name=TESTGROUP03, **kwargs)

		r_cx = self.fetchTestResource(self.nih_cxr_testdcm)

		with self.stageImageArchiveSeries(iserver, response2filearchive(r_cx)) as (test_sx, test_hache):

			testacl = iserver.admin_create_acl(testgroup03, {
				'resource': '*', 'view': True, 'comment_view': True, 'comment_edit': True, 'duration': 1
			})

			with self.getLimitedImageServer(iserver, testuser03, object_data={'description': 'text-only update'}) as iserver_test:

				test_sx_ltd = iserver_test.get_series(test_sx.pk)
				meta = { 'Tag': 'qc.accept', 'Score': 3 }

				for dicomweb_api in (False, True):
					c = test_sx_ltd.create_comment('original text', data={ 'Meta': meta }, dicomweb_api=dicomweb_api)
					before = test_sx_ltd.get_comment(c.pk, dicomweb_api=dicomweb_api)
					self.assertEqual(before.meta, meta)

					sleep(1.1)
					before.update({ 'Text': 'revised text' })

					after = test_sx_ltd.get_comment(c.pk, dicomweb_api=dicomweb_api)
					self.assertEqual(after.text, 'revised text')
					self.assertEqual(after.meta, meta, msg='Metadata lost on a text-only update (dicomweb=%s)' % dicomweb_api)
					self.assertEqual(after._objectdata.get('User', {}).get('id'), before._objectdata.get('User', {}).get('id'))
					self.assertEqual(after._objectdata.get('Created'), before._objectdata.get('Created'))
					self.assertNotEqual(after._objectdata.get('LastUpdate'), before._objectdata.get('LastUpdate'))
