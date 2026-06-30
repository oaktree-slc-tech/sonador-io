import os, posixpath, unittest, requests, logging, json, tempfile, zipfile, contextlib
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

	def test_comment_global_authorized_delete_standard(self, *args, **kwargs):
		'''	Standard API DELETE with comment_edit: True. Admin creates a comment,
			limited user attempts to delete it via the standard Orthanc endpoint.
			Isolates DELETE from the POST bug found in create tests.
		'''
		iserver, testgroup03, testuser03 = self.setupTestAuth(
			testuser_config=TESTUSER03, testgroup_name=TESTGROUP03, **kwargs)

		r_cx = self.fetchTestResource(self.nih_cxr_testdcm)

		with self.stageImageArchiveSeries(iserver, response2filearchive(r_cx)) as (test_sx, test_hache):

			testacl = iserver.admin_create_acl(testgroup03, {
				'resource': '*', 'view': True, 'comment_view': True, 'comment_edit': True, 'duration': 1
			})

			ctxt = 'Test comment: standard API delete grant probe'
			admin_comment = test_sx.create_comment(ctxt)

			with self.getLimitedImageServer(iserver, testuser03, object_data={'description': 'Standard DELETE grant'}) as iserver_test:

				test_sx_ltd = iserver_test.get_series(test_sx.pk)
				c_ltd = test_sx_ltd.get_comment(admin_comment.pk)
				c_ltd.delete()

				# Verify comment is gone
				remaining = test_sx.fetch_comments()
				self.assertTrue(all(ctxt != _c.text for _c in remaining),
					msg='Standard API DELETE with comment_edit: True did not remove the comment')

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

	def test_comment_global_authorized_delete_dicomweb(self, *args, **kwargs):
		'''	DICOMweb DELETE with comment_edit: True. Admin creates a comment,
			limited user deletes it via the DICOMweb endpoint.
		'''
		iserver, testgroup05, testuser05 = self.setupTestAuth(
			testuser_config=TESTUSER05, testgroup_name=TESTGROUP05, **kwargs)

		r_cx = self.fetchTestResource(self.nih_cxr_testdcm)

		with self.stageImageArchiveSeries(iserver, response2filearchive(r_cx)) as (test_sx, test_hache):

			testacl = iserver.admin_create_acl(testgroup05, {
				'resource': '*', 'view': True, 'comment_view': True, 'comment_edit': True, 'duration': 1
			})

			ctxt = 'Test comment: DICOMweb delete grant probe'
			admin_comment = test_sx.create_comment(ctxt)

			with self.getLimitedImageServer(iserver, testuser05, object_data={'description': 'DICOMweb DELETE grant'}) as iserver_test:

				test_sx_ltd = iserver_test.get_series(test_sx.pk)
				c_ltd = test_sx_ltd.get_comment(admin_comment.pk, dicomweb_api=True)
				c_ltd.delete()

				remaining = test_sx.fetch_comments()
				self.assertTrue(all(ctxt != _c.text for _c in remaining),
					msg='DICOMweb DELETE with comment_edit: True did not remove the comment')

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
