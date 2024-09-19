import os, requests, logging, json, tempfile, zipfile, copy

from client import apisettings as gapi
from client.utils.general import create_token

from ..test import SonadorBaseTestCase
from ..test.acl import AclBaseTestCase, TESTGROUP01, TESTGROUP02, TESTGROUP03, \
	TESTUSER01_USERNAME, TESTUSER01_ATTRS, TESTUSER01


class SonadorBearerTokenAuthenticationTests(AclBaseTestCase):
	'''	Sonador authentication tests:

		1.	Bearer tokens: ensure that session tokens sent to Sonador web application endpoints
			return valid responses.
		2. 	Bearer tokens when combined with PUT/POST do not trigger CSRF errors.
		3.	Session/bearer tokens return the same response as API access credentials and standing
			API tokens.
	'''
	testgroup01 = TESTGROUP01
	testgroup02 = TESTGROUP02
	testuser = TESTUSER01_USERNAME
	testuser_attrs = TESTUSER01_ATTRS

	def _check_bearer_credentials(self, sconn, sconn_test, *args, **kwargs):
		'''	Check the credentials of the connection against those of the test connection to ensure
			that they are for the same user and have type "Bearer".
		'''
		self.assertEqual(sconn_test.apitoken_type, gapi.AUTH_TOKEN_BEARER,
			msg='Connection token type should be %s' % gapi.AUTH_TOKEN_BEARER)
		self.assertNotEqual(sconn.apitoken, sconn_test.apitoken,
			msg='Bearer connection instance and environment instance have the same connection credentials')

		# Ensure that the session and API access token introspect to the same user instance
		_user_bearer = sconn.admin_verify_user_credentials(sconn_test.apitoken_type, sconn_test.apitoken)
		_user_env = sconn.admin_verify_user_credentials(sconn.apitoken_type, sconn.apitoken)
		self.assertTrue(_user_bearer.get('user', {}).get('id') is not None 
				and _user_bearer.get('user', {}).get('id') == _user_env.get('user', {}).get('id'),
			msg='User IDs for Bearer and environment tokens do not match')

	def test_sonador_bearer_token_auth_valid(self, *args, **kwargs):
		'''	Ensure that it is possible to retrieve data from the Sonador web auth using session (Bearer) tokens.
		'''
		# Retrieve session token using environment credentials
		_sconn = self.getSonadorConnection(*args, **kwargs)
		_bearer_token = _sconn.get_session_token()

		# Initialize new token with Bearer credentials
		sconn = _sconn.with_credentials(apitoken=_bearer_token.get(gapi.AUTH_ACCESS_TOKEN), 
			apitoken_type=_bearer_token.get(gapi.AUTH_TOKEN_TYPE))	
		self.assertEqual(sconn.apitoken, _bearer_token.get(gapi.AUTH_ACCESS_TOKEN),
			msg='Connection access token different than session token')

		# Check bearer credentials
		self._check_bearer_credentials(_sconn, sconn)

		# Retrieve image server list for user
		self.assertEqual(len(sconn.fetch_imageservers()), len(_sconn.fetch_imageservers()),
			msg='Bearer token instance retrieved the wrong number of image servers.')		
	
	def test_sonador_bearer_token_user_search(self, *args, **kwargs):
		'''	Ensure that it is possible to search for users using session (Bearer) tokens.
		'''
		# Retrieve session token using environment credentials
		_iserver = self.getImageServer(*args, **kwargs)
		_bearer_token = _iserver.server.get_session_token()

		# Initialize new image server with Bearer credentials
		iserver = _iserver.with_credentials(apitoken=_bearer_token.get(gapi.AUTH_ACCESS_TOKEN), 
			apitoken_type=_bearer_token.get(gapi.AUTH_TOKEN_TYPE))
		self._check_bearer_credentials(_iserver.server, iserver.server)

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

	def test_sonador_bearer_token_group_search(self, *args, **kwargs):
		'''	Ensure that it is possible to search for users using session (Bearer) tokens.
		'''
		# Retrieve session token using environment credentials
		_iserver = self.getImageServer(*args, **kwargs)
		_bearer_token = _iserver.server.get_session_token()

		# Initialize new image server with Bearer credentials
		iserver = _iserver.with_credentials(apitoken=_bearer_token.get(gapi.AUTH_ACCESS_TOKEN), 
			apitoken_type=_bearer_token.get(gapi.AUTH_TOKEN_TYPE))
		self._check_bearer_credentials(_iserver.server, iserver.server)

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