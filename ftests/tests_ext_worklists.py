import os, posixpath, unittest, requests, logging, json, tempfile, zipfile, contextlib
from io import BytesIO
from time import sleep

from client import apisettings as gapi
from client.utils.general import first
from client.utils.object import each
from client.errors import ClientOperationError

from ..apisettings import SONADOR_IMAGING_SERVER, IMAGING_SERVER_RESOURCE_STUDY, IMAGING_SERVER_RESOURCE_SERIES, \
	DCMHEADER_SERIES_INSTANCE_UID
from ..apisettings.worklists import SONADOR_WORKLIST_STATUS_UNREAD, SONADOR_WORKLIST_STATUS_APPROVED, \
	SONADOR_WORKLIST_STATUS_REJECTED, SONADOR_WORKLIST_STATUS_REVIEWED
from ..helpers import response2filearchive
from ..servers import sonador_apitoken_fetch
from ..errors import soandor_clientexception_server_errors

from ..tasks.uploads import imageserver_upload_archive
from ..test import SonadorBaseTestCase, SonadorSeriesBaseTestCase
from ..test.acl import AclBaseTestCase, TESTGROUP01, TESTGROUP02, TESTGROUP03, \
	TESTUSER01_USERNAME, TESTUSER01_ATTRS, TESTUSER01, TESTUSER02, TESTUSER02_USERNAME, TESTUSER02_ATTRS, \
	TESTUSER03_USERNAME, TESTUSER03_ATTRS

logger = logging.getLogger(__name__)


class SonadorStudyReviewerWorklistTests(AclBaseTestCase):
	'''	Test create, read, update, and delete permissions for Sonador/Orthanc worklists
	'''
	testgroup01 = TESTGROUP01
	testgroup02 = TESTGROUP02
	testgroup03 = TESTGROUP03

	testuser = TESTUSER01_USERNAME
	testuser_attrs = TESTUSER01_ATTRS

	testuser02 = TESTUSER02_USERNAME
	testuser02_attrs = TESTUSER02_ATTRS

	testuser03 = TESTUSER03_USERNAME
	testuser03_attrs = TESTUSER03_ATTRS

	nih_cxr_testdcm = 'https://www.oak-tree.tech/documents/331/nih-cxr.patient-30775.zip'

	def tearDown(self):
		'''	Remove server policies associated with test data
		'''
		iserver = self.getImageServer()

		# Remove all policies associated with test user or groups
		testgroup01 = iserver.server.admin_create_group(self.testgroup01)
		testgroup02 = iserver.server.admin_create_group(self.testgroup02)
		_group_ids = set((testgroup01.pk, testgroup02.pk))

		for _acl_policy in iserver.fetch_acl():
			if _acl_policy.group in _group_ids:
				_acl_policy.delete()

	def test_resource_worklist_valid(self, *args, **kwargs):
		'''	Ensure that the test runner is able to upload a series to Sonador, create a worklist item,
			and update the worklist item with a state.
		'''
		# Setup test group and user for worklist
		iserver, testgroup01, testuser01 = self.setupTestAuth(
			testuser_config=TESTUSER01, testgroup_name=TESTGROUP01, **kwargs)

		# Download test series
		r_cx = requests.get(self.nih_cxr_testdcm)
		if not r_cx.ok:
			raise ValueError('Unable to retrieve test data due to an error. Status code: %s' % r_cx.status_code)

		# Stage test files to imaging server
		with self.stageImageArchiveSeries(iserver, response2filearchive(r_cx)) as (test_sx, test_hache):

			# Reference to parent instance
			test_s = iserver.get_study(test_sx.parent.pk)

			# Attempt to create reviewer worklist item for a group not associated with the server.
			# Request should fail with a 400 error.
			try:
				test_s.create_reviewer_worklist_item(testgroup01, testuser01, SONADOR_WORKLIST_STATUS_UNREAD)
				self.fail('Able to create a worklist item for a group and user not associated with the study')

			except Exception as err:

				# Attempt to retrieve errors from err instance
				_details = getattr(err, 'details', {})
				_errors = soandor_clientexception_server_errors(err) or {}
				self.assertEqual(_details.get(gapi.STATUS_CODE), 400,
					msg='Server sent incorrect status code: %s. Expected: 400.'% _details.get(gapi.STATUS_CODE))

				# Ensure that there is an error indicating the group is not associated with the server
				self.assertTrue(any('group instance not associated with server' in _e.get('message', '').lower() for _e in _errors.get('Group', [])),
					msg='Server sent incorrect response for group not associated with server. Expected bad request '
						+ 'and received a valid group response.')

			# Create ACL policy which associates the group with the server
			testacl = iserver.admin_create_acl(testgroup01, { 'resource': '*', 'duration': 1 })

			# Create reviewer worklist
			w01 = test_s.create_reviewer_worklist_item(testgroup01, testuser01, SONADOR_WORKLIST_STATUS_UNREAD)
			self.assertTrue(any(w01.pk == _w.pk for _w in test_s.fetch_reviewer_worklist()),
				msg='Worklist UID returned by request does not match UID of group instances retrieved by fetch method')

			# Retrieve worklist instance via direct fetch
			w01 = test_s.get_reviewer_worklist_item(w01.pk)

			# Verify payload of the worklist response, check user and group objects to ensure they are complete
			self.assertEqual(test_s.pk, w01.Study, msg='Study property of worklist does not match the study orthanc UID.')
			self.assertTrue(w01.user is not None and w01.user_id == testuser01.pk,
				msg='Worklist payload does not reference the correct user')
			self.assertTrue(w01.group is not None and w01.group_name == testgroup01.name and w01.group_id == testgroup01.pk,
				msg='Worklist payload does not reference the correct group')

			# Update worklist instance and ensure that the state was changed
			w01.update({ 'State': SONADOR_WORKLIST_STATUS_APPROVED })
			w01 = test_s.get_reviewer_worklist_item(w01.pk)
			self.assertEqual(w01.state, SONADOR_WORKLIST_STATUS_APPROVED,
				msg='Worklist has incorrect state. Expected: %s. Actual: %s' % (SONADOR_WORKLIST_STATUS_APPROVED, w01.state))

			# Remove worklist item and verify that it is no longer on the server
			w01.delete()
			self.assertTrue(all(w01.pk != _w.pk for _w in test_s.fetch_reviewer_worklist()),
				msg='Worklist item still registered with the imaging server after deletion')			
			
	def test_invalid_group_server(self, *args, **kwargs):
		'''	Ensure that the test runner will prevent action when an unassociated group is used to create a worklist item.
		'''
		# Setup test group and user for worklist
		iserver, testgroup02, testuser01 = self.setupTestAuth(
			testuser_config=TESTUSER01, testgroup_name=TESTGROUP02, **kwargs)

		# Download test series
		r_cx = requests.get(self.nih_cxr_testdcm)
		if not r_cx.ok:
			raise ValueError('Unable to retrieve test data due to an error. Status code: %s' % r_cx.status_code)

		# Stage test files to imaging server
		with self.stageImageArchiveSeries(iserver, response2filearchive(r_cx)) as (test_sx, test_hache):

			# Reference to parent instance
			test_s = iserver.get_study(test_sx.parent.pk)

			# Attempt to create reviewer worklist item for a group not associated with the server.
			# Request should fail with a 400 error.
			try:
				test_s.create_reviewer_worklist_item(testgroup02, testuser01, SONADOR_WORKLIST_STATUS_UNREAD)
				self.fail('Able to create a worklist item for a group and user not associated with the study')

			except Exception as err:

				# Attempt to retrieve errors from err instance
				_details = getattr(err, 'details', {})
				_errors = soandor_clientexception_server_errors(err) or {}
				self.assertEqual(_details.get(gapi.STATUS_CODE), 400,
					msg='Server sent incorrect status code: %s. Expected: 400.'% _details.get(gapi.STATUS_CODE))

				# Ensure that there is an error indicating the group is not associated with the server
				self.assertTrue(any('group instance not associated with server' in _e.get('message', '').lower() for _e in _errors.get('Group', [])),
					msg='Server sent incorrect response for group not associated with server. Expected bad request and invalid group response.')
				
	def test_invalid_user_server(self, *args, **kwargs):
		'''	Ensure that the test runner will prevent action when an unassociated user is used to create a worklist item.
		'''
		# Setup test group and user for worklist
		iserver, testgroup01, testuser01 = self.setupTestAuth(
			testuser_config=TESTUSER01, testgroup_name=TESTGROUP01, **kwargs)
		
		iserver02, testgroup02, testuser02 = self.setupTestAuth(
			testuser_config=TESTUSER02, testgroup_name=TESTGROUP02, **kwargs)

		# Download test series
		r_cx = requests.get(self.nih_cxr_testdcm)
		if not r_cx.ok:
			raise ValueError('Unable to retrieve test data due to an error. Status code: %s' % r_cx.status_code)

		# Stage test files to imaging server
		with self.stageImageArchiveSeries(iserver, response2filearchive(r_cx)) as (test_sx, test_hache):

			# Reference to parent instance
			test_s = iserver.get_study(test_sx.parent.pk)

			# Create ACL policy which associates the group with the server
			testacl = iserver.admin_create_acl(testgroup01, { 'resource': '*', 'duration': 1 })
			try:
				test_s.create_reviewer_worklist_item(testgroup01, testuser02, SONADOR_WORKLIST_STATUS_UNREAD)
				self.fail('Able to create a worklist item for a user not associated with the study')

			except Exception as err:

				# Attempt to retrieve errors from err instance
				_details = getattr(err, 'details', {})
				_errors = soandor_clientexception_server_errors(err) or {}
				self.assertEqual(_details.get(gapi.STATUS_CODE), 400,
					msg='Server sent incorrect status code: %s. Expected: 400.'% _details.get(gapi.STATUS_CODE))

				# Ensure that there is an error indicating the group is not associated with the server
				self.assertTrue(any('user does not exist or does not have access to the server' in _e.get('message', '').lower() for _e in _errors.get('User', [])),
                	msg='Server sent incorrect response for group not associated with server. Expected bad request and an invalid group response.')
				
	def test_modify_group_from_worklist(self, *args, **kwargs):
		'''	Ensure that the test runner is able to prevent a user from modifying the group associated with a worklist item.
		'''
		# Setup test group and user for worklist
		iserver, testgroup01, testuser01 = self.setupTestAuth(
			testuser_config=TESTUSER01, testgroup_name=TESTGROUP01, **kwargs)
		
		iserver02, testgroup02, testuser02 = self.setupTestAuth(
			testuser_config=TESTUSER02, testgroup_name=TESTGROUP02, **kwargs)

		# Ensure that test group 1 and 2 do not share the same primary key
		self.assertNotEqual(testgroup01.pk, testgroup02.pk, msg='Test group 1 and 2 have the same primary key.')

		# Download test series
		r_cx = requests.get(self.nih_cxr_testdcm)
		if not r_cx.ok:
			raise ValueError('Unable to retrieve test data due to an error. Status code: %s' % r_cx.status_code)

		# Stage test files to imaging server
		with self.stageImageArchiveSeries(iserver, response2filearchive(r_cx)) as (test_sx, test_hache):

			# Reference to parent instance
			test_s = iserver.get_study(test_sx.parent.pk)

			# Create ACL policy which associates the group with the server
			testacl = iserver.admin_create_acl(testgroup01, { 'resource': '*', 'duration': 1 })
			testacl = iserver.admin_create_acl(testgroup02, { 'resource': '*', 'duration': 1 })

			# Create reviewer worklist
			w01 = test_s.create_reviewer_worklist_item(testgroup01, testuser01, SONADOR_WORKLIST_STATUS_UNREAD)
			self.assertTrue(any(w01.pk == _w.pk for _w in test_s.fetch_reviewer_worklist()),
				msg='Worklist UID returned by request does not match UID of group instances retrieved by fetch method')

			# Retrieve worklist instance via direct fetch
			w01 = test_s.get_reviewer_worklist_item(w01.pk)

			# Verify payload of the worklist response, check user and group objects to ensure they are complete
			self.assertEqual(test_s.pk, w01.Study, msg='Study property of worklist does not match the study orthanc UID.')
			self.assertTrue(w01.user is not None and w01.user_id == testuser01.pk,
				msg='Worklist payload does not reference the correct user')
			self.assertTrue(w01.group is not None and w01.group_name == testgroup01.name and w01.group_id == testgroup01.pk,
				msg='Worklist payload does not reference the correct group')
			
			# Attempt to update group associated with worklist
			# Request should fail with a 400 error.
			try:
				r = w01.update({ 'Group': testgroup02.pk })				
				self.fail('Able to update group for worklist item. Worklist item groups cannot be modified once created.')

			except AssertionError as err:
				raise err

			except Exception as err:

				# Retrieve server response and error list from error
				_details = getattr(err, 'details', {})
				_errors = soandor_clientexception_server_errors(err) or {}

				# Attempt to retrieve errors from err instance
				self.assertEqual(_details.get(gapi.STATUS_CODE), 400,
					msg='Server sent incorrect status code: %s. Expected: 400.'% _details.get(gapi.STATUS_CODE))				

				# Ensure that there is an error indicating the group cannot be modified from exising worklist item
				self.assertTrue(any('invalid group' in _e.get('message', '').lower() for _e in _errors.get('Group', [])),
                	msg='Server sent incorrect response. Expected bad request and error message indicating invalid group.')

	def test_modify_completed_worklist(self, *args, **kwargs):
		'''	Ensure that the test runner is able to prevent a user from modifying a completed worklist item.
		'''
		# Setup test group and user for worklist
		iserver, testgroup01, testuser01 = self.setupTestAuth(
			testuser_config=TESTUSER01, testgroup_name=TESTGROUP01, **kwargs)

		# Download test series
		r_cx = requests.get(self.nih_cxr_testdcm)
		if not r_cx.ok:
			raise ValueError('Unable to retrieve test data due to an error. Status code: %s' % r_cx.status_code)

		# Stage test files to imaging server
		with self.stageImageArchiveSeries(iserver, response2filearchive(r_cx)) as (test_sx, test_hache):

			# Reference to parent instance
			test_s = iserver.get_study(test_sx.parent.pk)

			# Create ACL policy which associates the group with the server
			testacl = iserver.admin_create_acl(testgroup01, { 'resource': '*', 'duration': 1 })

			# Create reviewer worklist
			w01 = test_s.create_reviewer_worklist_item(testgroup01, testuser01, SONADOR_WORKLIST_STATUS_UNREAD, complete=True)
			self.assertTrue(any(w01.pk == _w.pk for _w in test_s.fetch_reviewer_worklist()),
				msg='Worklist UID returned by request does not match UID of group instances retrieved by fetch method')

			# Retrieve worklist instance via direct fetch
			w01 = test_s.get_reviewer_worklist_item(w01.pk)

			# Verify payload of the worklist response, check user and group objects to ensure they are complete
			self.assertEqual(test_s.pk, w01.Study, msg='Study property of worklist does not match the study orthanc UID.')
			self.assertTrue(w01.user is not None and w01.user_id == testuser01.pk,
				msg='Worklist payload does not reference the correct user')
			self.assertTrue(w01.group is not None and w01.group_name == testgroup01.name and w01.group_id == testgroup01.pk,
				msg='Worklist payload does not reference the correct group')
			
			# Attempt to update completed worklist
			# Request should fail with a 400 error.
			try:
				w01.update({ 'State': SONADOR_WORKLIST_STATUS_APPROVED })
				self.fail('Able to modify the state of an already completed worklist item.')

			except Exception as err:				

				# Attempt to retrieve errors from err instance
				_details = getattr(err, 'details', {})
				_errors = soandor_clientexception_server_errors(err) or {}
				self.assertEqual(_details.get(gapi.STATUS_CODE), 400,
					msg='Server sent incorrect status code: %s. Expected: 400.'% _details.get(gapi.STATUS_CODE))

				# Ensure that there is an error indicating the group cannot be modified from exising worklist item
				self.assertTrue(any('worklist items cannot be modified once they are set as complete' in _e.get('message', '').lower() for _e in _errors.get('Complete', [])),
                	msg='Server sent incorrect response. Expected bad request and invalid "Complete" error.')

	def test_invalid_group_server_dicomweb(self, *args, **kwargs):
		'''	Ensure that the test runner will prevent action when an 
			unassociated group is used to create a worklist item using dicomweb endpoint.
		'''
		# Setup test group and user for worklist
		iserver, testgroup02, testuser01 = self.setupTestAuth(
			testuser_config=TESTUSER01, testgroup_name=TESTGROUP02, **kwargs)

		# Download test series
		r_cx = requests.get(self.nih_cxr_testdcm)
		if not r_cx.ok:
			raise ValueError('Unable to retrieve test data due to an error. Status code: %s' % r_cx.status_code)

		# Stage test files to imaging server
		with self.stageImageArchiveSeries(iserver, response2filearchive(r_cx)) as (test_sx, test_hache):
			
			# Reference to parent instance
			test_s = iserver.get_study(test_sx.parent.pk)

			# Attempt to create reviewer worklist item for a group not associated with the server.
			# Request should fail with a 400 error.
			try:
				test_s.create_reviewer_worklist_item(testgroup02, testuser01, SONADOR_WORKLIST_STATUS_UNREAD, dicomweb_api=True)
				self.fail('Able to create a worklist item for a group and user not associated with the study')

			except Exception as err:

				# Attempt to retrieve errors from err instance
				_details = getattr(err, 'details', {})
				_errors = soandor_clientexception_server_errors(err) or {}
				self.assertEqual(_details.get(gapi.STATUS_CODE), 400,
					msg='Server sent incorrect status code: %s. Expected: 400.'% _details.get(gapi.STATUS_CODE))

				# Ensure that there is an error indicating the group is not associated with the server
				self.assertTrue(any('group instance not associated with server' in _e.get('message', '').lower() for _e in _errors.get('Group', [])),
					msg='Server sent incorrect response for group not associated with server. Expected bad request and invalid group response.')
				
	def test_invalid_user_server_dicomweb(self, *args, **kwargs):
		'''	Ensure that the test runner will prevent action when an unassociated 
			user is used to create a worklist item using dicomweb endpoint.
		'''
		# Setup test group and user for worklist
		iserver, testgroup01, testuser01 = self.setupTestAuth(
			testuser_config=TESTUSER01, testgroup_name=TESTGROUP01, **kwargs)
		
		iserver02, testgroup02, testuser02 = self.setupTestAuth(
			testuser_config=TESTUSER02, testgroup_name=TESTGROUP02, **kwargs)

		# Download test series
		r_cx = requests.get(self.nih_cxr_testdcm)
		if not r_cx.ok:
			raise ValueError('Unable to retrieve test data due to an error. Status code: %s' % r_cx.status_code)

		# Stage test files to imaging server
		with self.stageImageArchiveSeries(iserver, response2filearchive(r_cx)) as (test_sx, test_hache):
			
			# Reference to parent instance
			test_s = iserver.get_study(test_sx.parent.pk)

			# Create ACL policy which associates the group with the server
			testacl = iserver.admin_create_acl(testgroup01, { 'resource': '*', 'duration': 1 })
			try:
				test_s.create_reviewer_worklist_item(testgroup01, testuser02, SONADOR_WORKLIST_STATUS_UNREAD, dicomweb_api=True)
				self.fail('Able to create a worklist item for a user not associated with the server.')

			except Exception as err:

				# Attempt to retrieve errors from err instance
				_details = getattr(err, 'details', {})
				_errors = soandor_clientexception_server_errors(err) or {}
				self.assertEqual(_details.get(gapi.STATUS_CODE), 400,
					msg='Server sent incorrect status code: %s. Expected: 400.'% _details.get(gapi.STATUS_CODE))
				
				# Ensure that there is an error indicating the user is not associated with the server
				self.assertTrue(any('user does not exist or does not have access to the server' in _e.get('message', '').lower() for _e in _errors.get('User', [])),
                	msg='Server sent incorrect response for user not associated with server. Expected bad request and invalid user response.')
				
	def test_modify_group_from_worklist_dicomweb(self, *args, **kwargs):
		'''	Ensure that the test runner is able to prevent a user from modifying 
			the group associated with a worklist item using dicomweb ednpoint.
		'''
		# Setup test group and user for worklist
		iserver, testgroup01, testuser01 = self.setupTestAuth(
			testuser_config=TESTUSER01, testgroup_name=TESTGROUP01, **kwargs)
		
		iserver02, testgroup02, testuser02 = self.setupTestAuth(
			testuser_config=TESTUSER02, testgroup_name=TESTGROUP02, **kwargs)

		# Download test series
		r_cx = requests.get(self.nih_cxr_testdcm)
		if not r_cx.ok:
			raise ValueError('Unable to retrieve test data due to an error. Status code: %s' % r_cx.status_code)

		# Stage test files to imaging server
		with self.stageImageArchiveSeries(iserver, response2filearchive(r_cx)) as (test_sx, test_hache):
			
			# Reference to parent instance
			test_s = iserver.get_study(test_sx.parent.pk)

			# Create ACL policy which associates the group with the server
			testacl = iserver.admin_create_acl(testgroup01, { 'resource': '*', 'duration': 1 })
			testacl = iserver.admin_create_acl(testgroup02, { 'resource': '*', 'duration': 1 })

			# Create reviewer worklist
			w01 = test_s.create_reviewer_worklist_item(testgroup01, testuser01, SONADOR_WORKLIST_STATUS_UNREAD, dicomweb_api=True)
			self.assertTrue(any(w01.pk == _w.pk for _w in test_s.fetch_reviewer_worklist()),
				msg='Worklist UID returned by request does not match UID of group instances retrieved by fetch method')

			# Retrieve worklist instance via direct fetch
			w01 = test_s.get_reviewer_worklist_item(w01.pk, dicomweb_api=True)

			# Verify payload of the worklist response, check user and group objects to ensure they are complete
			self.assertEqual(test_s.pk, w01.Study, msg='Study property of worklist does not match the study orthanc UID.')
			self.assertTrue(w01.user is not None and w01.user_id == testuser01.pk,
				msg='Worklist payload does not reference the correct user')
			self.assertTrue(w01.group is not None and w01.group_name == testgroup01.name and w01.group_id == testgroup01.pk,
				msg='Worklist payload does not reference the correct group')
			
			# Attempt to update group associated with worklist
			# Request should fail with a 400 error.
			try:
				w01.update({ 'Group': testgroup02.pk })				
				self.fail('Able to update group for worklist item after creation.')

			except Exception as err:

				# Attempt to retrieve errors from err instance
				_details = getattr(err, 'details', {})
				_errors = soandor_clientexception_server_errors(err) or {}
				self.assertEqual(_details.get(gapi.STATUS_CODE), 400,
					msg='Server sent incorrect status code: %s. Expected: 400.'% _details.get(gapi.STATUS_CODE))

				# Ensure that there is an error indicating the group cannot be modified from exising worklist item
				self.assertTrue(any('it is not possible to change the value of group for an existing worklist item' in _e.get('message', '').lower() for _e in _errors.get('Group', [])), \
                	msg='Server sent incorrect response. Expected bad request and invalid "Group" error.')

	def test_modify_completed_worklist_dicomweb(self, *args, **kwargs):
		'''	Ensure that the test runner is able to prevent a user 
			from modifying a completed worklist item using dicomweb endpoint.
		'''
		# Setup test group and user for worklist
		iserver, testgroup01, testuser01 = self.setupTestAuth(
			testuser_config=TESTUSER01, testgroup_name=TESTGROUP01, **kwargs)

		# Download test series
		r_cx = requests.get(self.nih_cxr_testdcm)
		if not r_cx.ok:
			raise ValueError('Unable to retrieve test data due to an error. Status code: %s' % r_cx.status_code)

		# Stage test files to imaging server
		with self.stageImageArchiveSeries(iserver, response2filearchive(r_cx)) as (test_sx, test_hache):
			
			# Reference to parent instance
			test_s = iserver.get_study(test_sx.parent.pk)

			# Create ACL policy which associates the group with the server
			testacl = iserver.admin_create_acl(testgroup01, { 'resource': '*', 'duration': 1 })

			# Create reviewer worklist
			w01 = test_s.create_reviewer_worklist_item(testgroup01, testuser01, SONADOR_WORKLIST_STATUS_UNREAD, complete=True, dicomweb_api=True)
			self.assertTrue(any(w01.pk == _w.pk for _w in test_s.fetch_reviewer_worklist()),
				msg='Worklist UID returned by request does not match UID of group instances retrieved by fetch method')

			# Retrieve worklist instance via direct fetch
			w01 = test_s.get_reviewer_worklist_item(w01.pk, dicomweb_api=True)

			# Verify payload of the worklist response, check user and group objects to ensure they are complete
			self.assertEqual(test_s.pk, w01.Study, msg='Study property of worklist does not match the study orthanc UID.')
			self.assertTrue(w01.user is not None and w01.user_id == testuser01.pk,
				msg='Worklist payload does not reference the correct user')
			self.assertTrue(w01.group is not None and w01.group_name == testgroup01.name and w01.group_id == testgroup01.pk,
				msg='Worklist payload does not reference the correct group')
			
			# Attempt to update completed worklist
			# Request should fail with a 400 error.
			try:
				w01.update({ 'State': SONADOR_WORKLIST_STATUS_APPROVED })
				w01 = test_s.get_reviewer_worklist_item(w01.pk, dicomweb_api=True)
				self.failTest('Able to update group for worklist item.')

			except Exception as err:

				# Attempt to retrieve errors from err instance
				_details = getattr(err, 'details', {})
				_errors = soandor_clientexception_server_errors(err) or {}
				self.assertEqual(_details.get(gapi.STATUS_CODE), 400,
					msg='Server sent incorrect status code: %s. Expected: 400.'% _details.get(gapi.STATUS_CODE))

				# Ensure that there is an error indicating the group cannot be modified from exising worklist item
				self.assertTrue(any('worklist items cannot be modified once they are set as complete' in _e.get('message', '').lower() for _e in _errors.get('Complete', [])), \
                	msg='Server sent incorrect response. Expected bad request and invalid "Complete" error.')
