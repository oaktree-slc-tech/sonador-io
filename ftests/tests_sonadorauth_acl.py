import os, logging, traceback, copy, requests, posixpath, json
from time import sleep

from io import BytesIO

from client import apisettings as gapi
from client.utils.general import first, create_token
from client.utils.object import omit
from client.errors import ClientOperationError
from client.remote import request_client_error

from ..apisettings import IMAGING_SERVER_RESOURCE_STUDY, IMAGING_SERVER_RESOURCE_SERIES, \
	DCMCODE_PATIENT_ID, DCMHEADER_PATIENT_ID, DCMHEADER_SERIES_INSTANCE_UID
from ..helpers import response2filearchive, OAUTH_TOKEN_TYPE_BEARER, API_ACCESS_TOKEN
from ..test import SonadorBaseTestCase
from ..test.acl import AclBaseTestCase, TESTGROUP01, TESTGROUP02, TESTGROUP03, \
	TESTUSER01_USERNAME, TESTUSER01_ATTRS, TESTUSER01, TESTUSER02_USERNAME, TESTUSER02_ATTRS, \
	TESTUSER03_USERNAME, TESTUSER03_ATTRS

logger = logging.getLogger(__name__)


class SonadorAccessControlApiTests(AclBaseTestCase):
	'''	Tests for Sonador/Orthanc authoriztion and ACL APIs.

		1. Administrative endpoints for user management
		2. ACL endpoints for creating policies
		3. User and group search endpoints
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

	def test_acl_iserver_access(self, *args, **kwargs):
		''' Create a test user, test group, and test policy for the image server.
			Ensure that the user can only access the image server when attached to
			a policy.
		'''
		iserver = self.getImageServer(*args, **kwargs)

		# Create test groups and test user
		testgroup01 = iserver.server.admin_create_group(self.testgroup01)
		testgroup02 = iserver.server.admin_create_group(self.testgroup02)

		try:
			
			# Add test group 1 to user attributes. User should not be  member of test group 2 at this point.
			testuser_attrs = copy.deepcopy(self.testuser_attrs)
			testuser_attrs['groups'] = [testgroup01.pk]
			
			# Create user instance
			testuser = iserver.server.admin_create_user(self.testuser, create_token(), attrs=testuser_attrs)
		
		except Exception as err:
			self.logErrorDetails('Unable to create user due to an error.', err)

		# Ensure that the test user is not a member of group 2
		self.assertTrue(all(_g.get('name') != testgroup02.name for _g in getattr(testuser, 'groups', [])),
			msg='User created with unexpected group membership')

		# Remove any policies associated with test groups
		for _g in (testgroup01, testgroup02):

			_policy = first(iserver.fetch_acl(), key=lambda _p: _p.group == _g.pk)
			if _policy:
				_policy.delete()

		with self.getLimitedImageServer(iserver, testuser, object_data={'description': 'ACL integration testing' }) as iserver_test:

			# Attempt to retrieve imaging server and DICOM tags resource. If the user is not a member 
			# of an authorized group the client should throw an exception.
			self.assertRaises(ClientOperationError, lambda: iserver_test.server.get_imageserver(iserver.pk))
			self.assertRaises(ClientOperationError, lambda: iserver_test.cache_dcm_tags())

			# Create an access policy for group 2 and add the test user to the group
			testacl = iserver.admin_create_acl(testgroup02, { 'resource': '*', 'duration': 1 })
			testuser.update({ 'groups': [testgroup01.pk, testgroup02.pk ]})

			try: 
				# Retrieve image server
				iserver_test.server.get_imageserver(iserver.pk)

				# Retrieve DICOM tags (server user accessible endpoint)
				tags = iserver.cache_dcm_tags()
				self.assertTrue(tags is not None and len(tags) > 0, msg='Invalid tags array.')

			finally: testacl.delete()

			# Remove the policy and ensure that the an error is raised
			self.assertRaises(ClientOperationError, lambda: iserver_test.server.get_imageserver(iserver.pk))

	def test_token_introspection(self, *args, **kwargs):
		'''	Create a test user, test group, and test policy for the test image server.
			Ensure that Sonador is able to resolve a limited use token to the correct user
			from the global introspection endpoint. Ensure that the correct user profile is
			returned when the user is associated with the server and that a "badrequest" response 
			is generated if not.
		'''
		iserver = self.getImageServer(*args, **kwargs)

		# Create test group and test user
		testgroup01 = iserver.server.admin_create_group(self.testgroup01)

		try:

			# Add test group 1 to user attributes.
			testuser_attrs = copy.deepcopy(self.testuser_attrs)
			testuser_attrs['groups'] = [testgroup01.pk]

			# Create user instance
			testuser = iserver.server.admin_create_user(self.testuser, create_token(), attrs=testuser_attrs)

		except Exception as err:
			self.logErrorDetails('Unable to create user due to an error.', err)

		# Remove any policies on the server associated with test group one
		_policy = first(iserver.fetch_acl(), key=lambda _p: _p.group == testgroup01.pk)
		if _policy:
			_policy.delete()

		# Create temporary credentials
		with self.getUserToken(iserver.server, testuser) as _token:

			# Verify user token via administrative endpoint
			r = iserver.server.admin_verify_user_credentials(API_ACCESS_TOKEN, _token.token)
			self.assertTrue(isinstance(r, dict) and r.get('user') and r.get('user', {}).get('id') == testuser.pk,
				msg='Sonador admin introspect API returned wrong user instance. Expected: %s. Returned: %s.'
					% (testuser.pk, r.get('user', {}).get('id')))

			try:
				# Check user access to the imaging server: at this point in the test, the user has not yet been 
				# an authorization to the server, which should result in a 400 error and a message saying that the
				# user does not have access to the server.
				r = iserver.admin_verify_user_credentials(API_ACCESS_TOKEN, _token.token)
				self.failTest('Able to retrieve profile for a user who does not have access to the server.')

			except Exception as err:
				_details = getattr(err, 'details', {})
				self.assertEqual(_details.get(gapi.STATUS_CODE), 400, 
					msg='Server sent incorrect status code: %s. Expected: 400.'% _details.get(gapi.STATUS_CODE),)

				_errors = json.loads(_details.get(gapi.ERRORS)) if _details.get(gapi.ERRORS) else {}
				self.assertTrue(any('does not have permission' in _e.get('message', '') for _e in _errors.get(gapi.ERRORS_ALL)),
					msg='Server sent incorrect response for user not associated with server. Expected bad request '
						+ 'and received a valid user response.')
				
			# Create access policy for group and check user access to the server
			testacl = iserver.admin_create_acl(testgroup01, { 'resource': '*', 'duration': 1 })
			r = iserver.admin_verify_user_credentials(API_ACCESS_TOKEN, _token.token)
			self.assertEqual(isinstance(r, dict) and r.get('user') and r.get('user', {}).get('id') == testuser.pk, True,
				msg='Server sent incorrect response for user. Expected user instance and received incomplete response.')

	def test_user_search(self, *args, **kwargs):
		'''	Create a test user and test group. Utilize the search API to look for the test user.
		'''
		iserver = self.getImageServer(*args, **kwargs)

		# Create test group and test user
		testgroup01 = iserver.server.admin_create_group(self.testgroup01)

		try: 
			
			# Create test user
			testuser_attrs = copy.deepcopy(self.testuser_attrs)
			testuser_attrs['groups'] = [testgroup01.pk]
			testuser = iserver.server.admin_create_user(self.testuser, create_token(), attrs=testuser_attrs)

			# Create blank ACL policy for test group for the specified imaging server
			testacl01 = iserver.admin_create_acl(testgroup01, {
				'query': False, 'upload': False, 'resource': '*', 'duration': 1,
				'view': False, 'modify': False, 'remove': False, 'comment_edit': False, 'comment_view': False, 'acl': False
			})

		except Exception as err:
			self.logErrorDetails('Unable to create user due to an error.', err)

		# Execute user search and ensure that the user ID matches that returned by the management API
		results = iserver.user_query({ 'username': testuser.username })
		self.assertTrue(all(_u.username == testuser.username for _u in results),
			msg='User search API returned a result which did not match the input username.')

		# Execute user search for firstname
		self.assertTrue(all((testuser.first_name in _u.first_name for _u in iserver.user_query({ 'first_name': testuser.first_name }))),
			msg='User search API returned a result which did not match the input first_name.')
		self.assertTrue(all((testuser.last_name in _u.last_name for _u in iserver.user_query({ 'last_name': testuser.last_name }))),
			msg='User search API returned a result which did not match the input last_name.')

		# Remove ACL policy for the test user and ensure that no results are returned
		testacl01.delete()
		results = iserver.user_query({ 'username': testuser.username })
		self.assertEqual(len(results), 0, msg='User search API returned results for a group no longer associated with the server')

	def test_user_lookup_invalid(self, *args, **kwargs):
		'''	Ensure that user lookup provides relevant error messages
		'''

		iserver = self.getImageServer(*args, **kwargs)

		# Create test groupp and access control policy
		testgroup01 = iserver.server.admin_create_group(self.testgroup01)
		testacl01 = iserver.admin_create_acl(testgroup01, { 'resource': '*', 'duration': 1 })

		# Attempt to lookup invalid user instance
		try:
			_users = iserver.user_lookup([-1])
			self.failTest('Lookup of invalid user was successful, the lookup method should have thrown an exception.')
		
		# Ensure that server response includes expected error codes
		except Exception as err:
			_details = getattr(err, 'details', {})
			_errors = json.loads(_details.get(gapi.ERRORS)) if _details.get(gapi.ERRORS) else {}

			# Ensure that response from server includes an error list
			if not isinstance(_details, dict) or not _details.get('errors'):
				self.failTest('Invalid error error resposne from server "%s", expecting a JSON object' % _details)

			# Ensure that the status code is 400
			self.assertEqual(
				_details.get(gapi.STATUS_CODE), 400, msg='Invalid status code: %s. Expected: 400.' % _details.get(gapi.STATUS_CODE))
			self.assertTrue(any(_e.get('code') == 'invalid_choice' for _e in _errors.get('users', [])),
				msg='Unable to find expected error code for request.')

		# Send blank request
		try:
			_users = iserver.user_lookup([])
			self.failTest('Empty lookup request returned successful resposne, the lookup method should have thrown an exception.')

		except Exception as err:
			_details = getattr(err, 'details', {})
			_errors = json.loads(_details.get(gapi.ERRORS)) if _details.get(gapi.ERRORS) else {}

			# Ensure that the status code is 400
			self.assertEqual(
				_details.get(gapi.STATUS_CODE), 400, msg='Invalid status code: %s. Expected: 400.' % _details.get(gapi.STATUS_CODE))
			self.assertTrue(any(_e.get('code') == 'required' for _e in _errors.get('users', [])),
				msg='Unable to find exepected error code for request.')

	def test_user_lookup_valid(self, *args, **kwargs):
		'''	Test user lookup by UID. All test user instances belong to the same group.
		'''
		iserver = self.getImageServer(*args, **kwargs)

		# Create test group and access control policy
		testgroup01 = iserver.server.admin_create_group(self.testgroup01)
		testacl01 = iserver.admin_create_acl(testgroup01, { 'resource': '*', 'duration': 1 })

		try:

			# Create test user 1
			testuser01_attrs = copy.deepcopy(self.testuser_attrs)
			testuser01_attrs['groups'] = [testgroup01.pk]
			testuser01 = iserver.server.admin_create_user(self.testuser, create_token(), attrs=testuser01_attrs)

			# Create test user 2
			testuser02_attrs = copy.deepcopy(self.testuser02_attrs)
			testuser02_attrs['groups'] = [testgroup01.pk]
			testuser02 = iserver.server.admin_create_user(self.testuser02, create_token(), attrs=testuser02_attrs)

			# Create test user 3
			testuser03_attrs = copy.deepcopy(self.testuser03_attrs)
			testuser03_attrs['groups'] = [testgroup01.pk]
			testuser03 = iserver.server.admin_create_user(self.testuser03, create_token(), attrs=testuser03_attrs)

		except Exception as err:
			self.logErrorDetails('Unable to create test users due to an error.', err)

		# Lookup users
		_users = iserver.user_lookup([testuser01.pk, testuser02.pk, testuser03.pk])

		# Ensure that all test users are included in the test
		self.assertTrue(len(_users) == 3, msg='Lookup returned incorrect number of users')
		for _t in (testuser01, testuser02, testuser03):
			self.assertTrue(any(_t.pk == _u.pk for _u in _users), msg='Unable to locate user matching test-user=%s' % testuser01)

		# Remove ACL and ensure that user lookup fails, as the users are no longer associated with the server
		testacl01.delete()

		# Attempt lookup of users after ACL policy removed
		try:
			_users = iserver.user_lookup([testuser01.pk, testuser02.pk, testuser03.pk])
			self.failTest(
				'Lookup of users no longer associated with imaging server returned a successful response. An exception should have been thrown.')

		except Exception as err:
			_details = getattr(err, 'details', {})
			self.assertEqual(_details.get(gapi.STATUS_CODE), 400, 
				msg='Server returned non-valid status code: %s. Expected: 400.' % (_details.get(gapi.STATUS_CODE)))

			_errors = json.loads(_details.get(gapi.ERRORS)) if _details.get(gapi.ERRORS) else {}
			self.assertTrue(any(_e.get('code') == 'invalid_choice' for _e in _errors.get('users', [])),
				msg='Server returned wrong error code for request. Expected: "invalid_choice".')			

	def test_group_search(self, *args, **kwargs):
		'''	Create a test group and utilize search API to look for it.
		'''
		iserver = self.getImageServer()

		# Create test group and test user
		testgroup01 = iserver.server.admin_create_group(self.testgroup01)
		testgroup02 = iserver.server.admin_create_group(self.testgroup02)

		testacl01 = iserver.admin_create_acl(testgroup01, {
			'query': False, 'upload': False, 'resource': '*', 'duration': 1,
			'view': False, 'modify': False, 'remove': False, 'comment_edit': False, 
			'comment_view': False, 'acl': False
		})
		testacl02 = iserver.admin_create_acl(testgroup02, {
			'query': False, 'upload': False, 'resource': '*', 'duration': 1,
			'view': False, 'modify': False, 'remove': False, 'comment_edit': False, 
			'comment_view': False, 'acl': False
		})

		# Execute group search
		results = iserver.group_query({ 'name': 'testgroup' })
		self.assertTrue(len(results) >= 2, msg='Group search API not able to find all results in name query')
		self.assertTrue(all(('testgroup' in _g.name for _g in results)),
			msg='Group search API returned a result which did not match the input name term.')

		self.assertTrue(all((testgroup01.name in _g.name for _g in iserver.group_query({ 'name': testgroup01.name }))),
			msg='Group search API returned a result which did not match the input name term.')
		self.assertTrue(all((testgroup02.name in _g.name for _g in iserver.group_query({ 'name': testgroup02.name }))),
			msg='Group search API returned a result which did not match the input name term.')

		# Remove ACL policies
		testacl01.delete()
		testacl02.delete()

		# Ensure that the group names are not returned in search results
		self.assertEqual(len(iserver.group_query({ 'name': testgroup01.name })), 0,
			msg='Group search 1 returned results for group not associated with imaging server')
		self.assertEqual(len(iserver.group_query({ 'name': testgroup02.name })), 0,
			msg='Group search 2 returned results for group not associated with imaging server')

	def test_group_lookup_valid(self, *args, **kwargs):
		'''	Group lookup by UID. All groups are associated with the imaging server via ACL policy.
		'''
		iserver = self.getImageServer(*args, **kwargs)

		# Create test groups and access control policies
		testgroup01 = iserver.server.admin_create_group(self.testgroup01)
		testgroup02 = iserver.server.admin_create_group(self.testgroup02)
		testgroup03 = iserver.server.admin_create_group(self.testgroup03)

		# Create access control policies
		testacl01 = iserver.admin_create_acl(testgroup01, { 'resource': '*', 'duration': 1 })
		testacl02 = iserver.admin_create_acl(testgroup02, { 'resource': '*', 'duration': 1 })
		testacl03 = iserver.admin_create_acl(testgroup03, { 'resource': '*', 'duration': 1 })

		# Lookup groups
		_groups = iserver.group_lookup([testgroup01.pk, testgroup02.pk, testgroup03.pk ])
		self.assertEqual(len(_groups), 3, msg='Lookup returned the wrong number of groups')
		for _t in (testgroup01, testgroup02, testgroup03):
			self.assertTrue(any(_t.pk == _g.pk for _g in _groups))

		# Remove ACL
		for _acl in (testacl01, testacl02, testacl03):
			_acl.delete()

		# Ensure that lookup fails after removal of ACL policies
		try:
			_groups = iserver.group_lookup([testgroup01.pk, testgroup02.pk, testgroup03.pk])
			self.failTest('Able to execute group lookup even though groups are not associated with imaging server')

		except Exception as err:

			# Ensure request has a 400 status code
			_details = getattr(err, 'details', {})
			self.assertEqual(_details.get(gapi.STATUS_CODE), 400,
				msg='Server returned non-valid status code: %s. Expected: 400.' % _details.get(gapi.STATUS_CODE))

			# Ensure that the response includes an invalid_choice error
			_errors = json.loads(_details.get(gapi.ERRORS)) if _details.get(gapi.ERRORS) else {}
			self.assertTrue(any(_e.get('code') == 'invalid_choice' for _e in _errors.get('groups', [])),
				msg='Server returned wrong error code for request. Expected "invalid_choice".')

	def test_auth_unified_search(self, *args, **kwargs):
		''' Ensure that it is possible to retrieve user and group results from the auth API "unified search" endpoint
		'''
		_iserver, testgroup01, testuser01 = self.setupTestAuth(
			testuser_config=TESTUSER01, testgroup_name=TESTGROUP01, **kwargs)

		# Create blank access policy to allow user access to the server
		server_acl = _iserver.admin_create_acl(testgroup01, { 'resource': '*', 'duration': 1 })

		try:
		
			# Retrieve limited server 		
			with self.getLimitedImageServer(_iserver, testuser01, object_data={ 'description': 'User ACL API testing'}) as _iserver_test:

				# Create bearer token to check that both standing tokens and session tokens provide access
				_limited_bearer = _iserver_test.server.get_session_token()
				iserver_test = _iserver_test.with_credentials(apitoken=_limited_bearer.get(gapi.AUTH_ACCESS_TOKEN),
					apitoken_type=_limited_bearer.get(gapi.AUTH_TOKEN_TYPE))

				# Execute test search
				r_auth_search = requests.post(
					iserver_test.server.sonador_apiurl(posixpath.join(iserver_test.fetch_endpoint, iserver_test.pk, 'auth/search')),
					json={ 'term': testgroup01.name }, headers=iserver_test.server.request_headers())

				if not r_auth_search.ok:
					request_client_error('Unable to execute unified auth model search due to an error.', r_auth_search)

				# Parse result
				rdata_auth_search = r_auth_search.json()

				# Check search request to ensure that the test group was returned in the results
				self.assertTrue(any(_r.get('id') == testgroup01.pk and _r.get('result-type') == 'group' for _r in rdata_auth_search.get('results', [])),
					msg='Search results did not include the test group')
				self.assertTrue(any(_r.get('id') == testuser01.pk and _r.get('result-type') == 'user' for _r in rdata_auth_search.get('results', [])),
					msg='Search results did not include the test user')

		finally:
			server_acl.delete()	

	def test_user_acl_management(self, *args, **kwargs):
		'''	Ensure that it is possible to manage access to an Orthanc resource via user ACL policies

			1.	Create a test group and test user
			2.	Stage a test series to the image server
			3.	Create a series access policy for the test user, ensure that they are able to access the resoruce
			4.	Ensure that the limited user is able to see the resource in the tools/secure-find results (filtered results work)
			5.	Retrieve the series metadata (view works)
			6.	Delete the access policy via the DICOMweb API: ensure that the policies created via one API are visible
				via the other.
			7. (DICOMweb) Create a study permission, ensure that the user has access to the resource
			8. (DICOMweb) Ensure that the limited user sees the resource in the DICOMweb study list
			9.	Delete the access policy via the Orthanc internal API
			10. Ensure that the user is no longer able to access the resource
		'''
		iserver, testgroup01, testuser01 = self.setupTestAuth(
			testuser_config=TESTUSER01, testgroup_name=TESTGROUP01, **kwargs)

		# Create blank access policy to allow user access to the server
		server_acl = iserver.admin_create_acl(testgroup01, { 'resource': '*', 'duration': 1 })

		with self.getLimitedImageServer(iserver, testuser01, object_data={ 'description': 'User ACL API testing'}) as iserver_test:

			# Download test series
			r_cx = requests.get(self.nih_cxr_testdcm)
			if not r_cx.ok:
				raise ValueError('Unable to retrieve test data due to an error. Status code: %s' % r_cx.status_code)

			# Stage test files to imaging server
			with self.stageImageArchiveSeries(iserver, response2filearchive(r_cx)) as (test_sx, test_hache):

				# Ensure that test series and parents have been indexed before setting ACL
				sleep(0.15)
				test_sx.model_patient.index()
				test_sx.parent.index()
				test_sx.index()

				# Remove all user/group policies associated with the series
				self.clearSeriesTestAcl(test_sx)

				# Create series access control policy
				testacl01_sx_local = test_sx.create_user_acl(testuser01, {
						'View': True, 'Modify': False, 'Remove': False, 'CommentEdit': True, 'CommentView': True,  'ACL': False
					})

				# Ensure that the limited user is able to access the series instance
				self._verifySeriesLocalAcl(testacl01_sx_local, test_sx, iserver_test, verify_acl=True)

				# Retrieve a copy of the access policy via the DICOMweb API endpoint
				testacl01_sx_localdcm = test_sx.get_user_acl(testacl01_sx_local.pk, dicomweb_api=True)
				self.assertTrue(test_sx.dicomweb_resource_url in testacl01_sx_localdcm.url,
					msg='Series ACL URL (retrieved via DICOmweb API) does not include DICOMweb resource base')
				self.assertTrue(testacl01_sx_local.url != testacl01_sx_localdcm.url,
					msg='Series User ACL instance from internal Orthanc API and DICOMweb API have the same URL.')

				# Delete the access policy via the DICOMweb API
				testacl01_sx_localdcm.delete()
				sleep(1)

				# Attempt to retrieve the resource (which should result in a 403 error)
				try:
					_test_sx = iserver_test.get_series(test_sx.pk)
					self.fail('Test client able to retrieve series=%s after removing ACL authorizing access.')
				except ClientOperationError as err:
					_details = getattr(err, 'details', {})
					self.assertEqual(_details.get(gapi.STATUS_CODE), 403,
						msg='Orthanc returned a non-403 status code when unauthorized user attempted to access a resource.')

				# Create a policy via the DICOMweb API endpoint
				testacl01_s_localdcm = test_sx.parent.create_user_acl(testuser01, {
						'View': True, 'Modify': False, 'Remove': False, 'ACL': False 
					}, dicomweb_api=True)
				self.assertTrue(test_sx.parent.dicomweb_resource_url in testacl01_s_localdcm.url,
					msg='Study ACL URL does not include DICOMweb resource base.')

				# Verify that the study ACL policy continues to grant access to the test series
				self._verifySeriesLocalAcl(testacl01_s_localdcm, test_sx, iserver_test, verify_acl=False)

				# Execute query via DICOMweb endpoint, ensure test series appears in the results, and that the DICOMweb APi returned the correct response
				self._verifyDicomWebLocalAcl(testacl01_s_localdcm, test_sx, iserver_test)
				
				# Remove the study ACL via the internal API
				testacl01_s_local = test_sx.parent.get_user_acl(testacl01_s_localdcm.pk)
				self.assertTrue(testacl01_s_local.url != testacl01_s_localdcm.url,
					msg='Study User ACL instance from internal Orthanc API and DICOMweb API have the same URL.')

				# Delete the study access policy via the Orthanc internal API
				testacl01_s_local.delete()
				sleep(1)

				# Attempt to retrieve the series (which should result in a 403 error)
				try:
					_test_sx = iserver_test.get_series(test_sx.pk)
					self.fail('Test client able to retrieve series=%s after removing ACL authorizing access.')
				except ClientOperationError as err:
					_details = getattr(err, 'details', {})
					self.assertEqual(_details.get(gapi.STATUS_CODE), 403,
						msg='Orthanc returned a non-403 status code when unauthorized user attempt to access a resource.')

	def test_group_acl_management(self, *args, **kwargs):
		'''	Ensure that it is possible to manage access to an Orthanc resource via group ACL policies

			1.	Create a test group and test user
			2.	Stage a test series to the image server and iterate through all policies to ensure that the user
				does not have a direct policy.
			3.	Create a series access policy for the group, ensure that the user is able to access the resource
				(via group permission)
			4.	Ensure that the limited user is able to see the resource in the tools/secure-find results (filtered results work)
			5.	Retrieve the series metadata (view works)
			6.	Delete the access policy via the DICOMweb API: ensure that the policies created via one API are visible
				via the other.
			7. (DICOMweb) Create a group study permission, ensure that the user has access to the resource
			8. (DICOMweb) Ensure that the limited user sees the resource in the DICOMweb study list
			9.	Delete the access policy via the Orthanc internal API
			10. Ensure that the user is no longer able to access the resource
		'''
		iserver, testgroup01, testuser01 = self.setupTestAuth(
			testuser_config=TESTUSER01, testgroup_name=TESTGROUP01, **kwargs)

		# Create blank access policy to allow user access to the server
		server_acl = iserver.admin_create_acl(testgroup01, { 'resource': '*', 'duration': 1 })

		with self.getLimitedImageServer(iserver, testuser01, object_data={ 'description': 'Group ACL API testing'}) as iserver_test:

			# Download test series
			r_cx = requests.get(self.nih_cxr_testdcm)
			if not r_cx.ok:
				raise ValueError('Unable to retrieve test data due to an error. Status code: %s' % r_cx.status_code)

			# Stage test files to imaging server
			with self.stageImageArchiveSeries(iserver, response2filearchive(r_cx)) as (test_sx, test_hache):

				# Ensure that test series and parents have been indexed before setting ACL
				sleep(0.15)
				test_sx.model_patient.index()
				test_sx.parent.index()
				test_sx.index()

				# Remove all user policies associated with the user
				for _acl in test_sx.fetch_user_acl():
					_acl.delete()

				for _acl in test_sx.parent.fetch_user_acl():
					_acl.delete()

				for _acl in test_sx.model_patient.fetch_user_acl():
					_acl.delete()

				# Create series access control policy for the group
				try: 
					testacl01_sx_local = test_sx.create_group_acl(testgroup01, {
						'View': True, 'Modify': False, 'Remove': False, 'CommentEdit': True, 'CommentView': True,  'ACL': False
					})

				except Exception as err:
					self.logErrorDetails('Unable to create ACL policy due to an error.', err)

				# Ensure that the limited user is able to access the series instance
				self._verifySeriesLocalAcl(testacl01_sx_local, test_sx, iserver_test, verify_acl=False)

				# Retrieve a copy of the access policy via the DICOMweb API endpoint
				testacl01_sx_localdcm = test_sx.get_group_acl(testacl01_sx_local.pk, dicomweb_api=True)
				self.assertTrue(test_sx.dicomweb_resource_url in testacl01_sx_localdcm.url,
					msg='Series ACL URL (retrieved via DICOmweb API) does not include DICOMweb resource base')
				self.assertTrue(testacl01_sx_local.url != testacl01_sx_localdcm.url,
					msg='Series User ACL instance from internal Orthanc API and DICOMweb API have the same URL.')

				# Delete the access policy via the DICOMweb API
				testacl01_sx_localdcm.delete()
				sleep(1)

				# Attempt to retrieve the resource (should result in a 403 error)
				try:
					_test_sx = iserver_test.get_series(test_sx.pk)
					self.fail('Test client able to retrieve series=%s after removing ACL authorizing access.')
				except ClientOperationError as err:
					_details = getattr(err, 'details', {})
					self.assertEqual(_details.get(gapi.STATUS_CODE), 403,
						msg='Orthanc returned a non-403 status code when unauthorized user attempted to access a resource.')

				# Create a policy via the DICOMweb API endpoint
				testacl01_s_localdcm = test_sx.parent.create_group_acl(testgroup01, {
						'View': True, 'Modify': False, 'Remove': False, 'ACL': False 
					}, dicomweb_api=True)
				self.assertTrue(test_sx.parent.dicomweb_resource_url in testacl01_s_localdcm.url,
					msg='Study ACL URL does not include DICOMweb resource base.')

				# Verify that the study ACL policy continues to grant access to the test series. Skip validation of ACL policy.
				self._verifySeriesLocalAcl(testacl01_s_localdcm, test_sx, iserver_test, verify_acl=False)

				# Execute query via DICOMweb endpoint, ensure test series appears in the results, and that the DICOMweb APi returned the correct response
				self._verifyDicomWebLocalAcl(testacl01_s_localdcm, test_sx, iserver_test)
				
				# Remove the study ACL via the internal API
				testacl01_s_local = test_sx.parent.get_group_acl(testacl01_s_localdcm.pk)
				self.assertTrue(testacl01_s_local.url != testacl01_s_localdcm.url,
					msg='Study User ACL instance from internal Orthanc API and DICOMweb API have the same URL.')

				# Delete the study access policy via the Orthanc internal API
				testacl01_s_local.delete()
				sleep(1)

				# Attempt to retrieve the series (which should result in a 403 error)
				try:
					_test_sx = iserver_test.get_series(test_sx.pk)
					self.fail('Test client able to retrieve series=%s after removing ACL authorizing access.')
				except ClientOperationError as err:
					_details = getattr(err, 'details', {})
					self.assertEqual(_details.get(gapi.STATUS_CODE), 403,
						msg='Orthanc returned a non-403 status code when unauthorized user attempt to access a resource.')

	def test_group_acl_management_invalid(self, *args, **kwargs):
		'''	Ensure that data validation for group ACL management works as expected.

			1. 	Create a test group and test user
			2. 	Stage a test series to the image server and iterate through all policies to ensure that the user does
				not have a direct policy.
			3.	Create a series access policy for the group with an incorrect ID, ensure that the server returns
				a data validation error in the correct format.
		'''
		# Create test user and group
		iserver, testgroup01, testuser01 = self.setupTestAuth(
			testuser_config=TESTUSER01, testgroup_name=TESTGROUP01, **kwargs)

		# Create blank access policy which will then be deleted to ensure that the group
		# does not have a policy associated with the server. There can only be one
		# group global policy per image.
		server_acl = iserver.admin_create_acl(testgroup01, { 'resource': '*', 'duration': 1 })
		server_acl.delete()

		# Download test series
		r_cx = requests.get(self.nih_cxr_testdcm)
		if not r_cx.ok:
			raise ValueError('Unable to retrieve test data due to an error. Status code: %s' % r_cx.status_code)

		# Stage test files to imaging server
		with self.stageImageArchiveSeries(iserver, response2filearchive(r_cx)) as (test_sx, test_hache):

			# Ensure that the test series and parents have been indexed before setting ACL
			sleep(0.15)
			test_sx.model_patient.index()
			test_sx.parent.index()
			test_sx.index()

			# Create series access control policy for the group
			try:
				testacl01_sx_local = test_sx.create_group_acl(testgroup01, {
					'View': True, 'Modify': False, 'Remove': False, 'CommentEdit': True, 'CommentView': True, 'ACL': False
				})
				self.failTest('Able to create group access control policy for a group not associated with the server')

			except ClientOperationError as err:
				_details = getattr(err, 'details', None) or {}
				_errors = _details.get(gapi.ERRORS) if isinstance(_details.get(gapi.ERRORS), dict) \
					else json.loads(_details.get(gapi.ERRORS)) if isinstance(_details.get(gapi.ERRORS), (str, bytes)) \
					else {}

				self.assertTrue('Group' in _errors, msg='Error response does not contain "Group" entry.')
				self.assertTrue(any(_e.get(gapi.CODE) == gapi.VALIDATION_APICODE_INVALID for _e in _errors.get('Group', [])),
					msg='Unable to locate "%s" error for Group, despite using a group for the policy not associated with the server.' % gapi.VALIDATION_APICODE_INVALID)
