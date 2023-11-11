import os, logging

from client import apisettings as gapi
from client.utils.object import omit

from ..apisettings import SONADOR_APITOKEN, SONADOR_ACCESS_ID, SONADOR_SECRET_KEY, MASKED_VALUE_SEP
from ..test import SonadorBaseTestCase
from ..servers.auth import SonadorSecureApiCredential, SonadorApiToken


logger = logging.getLogger(__name__)


class SonadorAuthenticationCredentialApiTests(SonadorBaseTestCase):
	''' Tests for the Sonador credential management API
	'''
	user_credential_msg = 'Functional test suite credential'
	user_credential_msg_update_template = '%s (%s)'

	def test_apitoken_management(self, *args, **kwargs):
		'''	Test API token management endpoint

			1.	List all tokens for the uesr, if an API token was used to initialize
				the Sonador connection, ensure that it is present in the list.
			2.	Create a new API token
			3.	Update the API token description
			4.	Remove the API token from the server
		'''
		# Retrieve Sonador connection variables from env, retrieve access tokens for the current user
		sconn = self.getSonadorConnection(*args, **kwargs)

		# Ensure that the user token used for authentication (if a token was used for auth) is present in the list
		# retrieved from the token endpoint. Because tokens are truncated by the API, only the first and last four
		# digits of the token are checked.
		if os.environ.get(SONADOR_APITOKEN):
			authtoken = os.environ.get(SONADOR_APITOKEN)
			self.assertTrue(any(authtoken[:4] in t.pk and authtoken[-4:] in t.pk for t in sconn.fetch_user_apitokens()))

		# Create a new API access credential, ensure that the operation was successful
		rdata = sconn.create_user_apitoken(object_data={ 'description': self.user_credential_msg })
		self.assertEqual(rdata.get(gapi.STATUS), gapi.SUCCESS, 
			msg='Creation of API token failed with incorrect status. Expected: %s. Result: %s.' % (gapi.SUCCESS, rdata.get(gapi.OPRESULT)))

		# Retrieve object data and check that description was set correctly
		cdata = rdata.get(gapi.OBJECT_DATA) 
		ctoken = SonadorApiToken(sconn, cdata or {})
		self.assertTrue(cdata is not None and isinstance(cdata, dict) and cdata.get('token') is not None,
			msg='Credential data not returned in the `object-data` key of the response or unable to find token value.')
		self.assertEqual(cdata.get('description'), self.user_credential_msg,
			msg='Credential response has the wrong description. Expected: "%s". Result: "%s".' % (
				self.user_credential_msg, cdata.get('description'),
			))

		# Update 1: token description using full token for object lookup
		umsg = self.user_credential_msg_update_template % ( self.user_credential_msg, 'r1')
		ctoken.update({ 'description': umsg })
		self.assertTrue(
			any(ctoken.token[:4] in t.pk and ctoken.token[-4:] in t.pk and umsg == t.description for t in sconn.fetch_user_apitokens()),
			msg='Update of token description failed. Expected: "%s".' % umsg)

		# Update 2: token description using masked value for object lookup
		umsg = self.user_credential_msg_update_template % (self.user_credential_msg, 'r2')
		SonadorApiToken(sconn, {
				'token': MASKED_VALUE_SEP.join(( ctoken.pk[:4], ctoken.pk[-4:])),
				'description': ctoken.description
			}).update({ 'description': umsg })
		self.assertTrue(
			any(ctoken.token[:4] in t.pk and ctoken.token[-4:] in t.pk and umsg == t.description for t in sconn.fetch_user_apitokens()),
			msg='Update of token description failed. Expected: "%s".' % umsg)

		# Remove the otken from the server
		ctoken.delete()
		self.assertTrue(all(not ctoken.token[:4] in t.pk for t in sconn.fetch_user_apitokens()),
			msg='Token present on server after making DELETE call')

	def test_apitoken_management_masked(self, *args, **kwargs):
		'''	Ensure that it is possible to perform token management operations with a masked token

			1. Create a new token for the user.
			2. Update the API token description using a masked value
			3. Remove the API token from the server using a masked value
		'''
		# Retrieve Sonador connection variables from env, retrieve access tokens for current user
		sconn = self.getSonadorConnection(*args, **kwargs)

		# Create a new access credential, ensure operation was successful
		rdata = sconn.create_user_apitoken(object_data={ 'description': self.user_credential_msg })
		cdata = omit(rdata.get(gapi.OBJECT_DATA, {}), (SonadorApiToken.pk_attr,))
		self.assertEqual(rdata.get(gapi.STATUS), gapi.SUCCESS, 
			msg='Creation of API token failed with incorrect status. Expected: %s. Result: %s.' % (gapi.SUCCESS, rdata.get(gapi.OPRESULT)))
		self.assertTrue(cdata is not None and isinstance(cdata, dict) and rdata.get(gapi.OBJECT_DATA).get(SonadorApiToken.pk_attr) is not None,
			msg='Credential data not returned in the `object-data` key of the response or unable to find token value.')

		# Retrieve token value from response
		ctoken_str = rdata.get(gapi.OBJECT_DATA, {}).get(SonadorApiToken.pk_attr)
		self.assertTrue(ctoken_str is not None, msg='Unable to retrieve token value from POST response data.')

		# Create token model instance using a masked token
		
		cdata[SonadorApiToken.pk_attr] = MASKED_VALUE_SEP.join((ctoken_str[:4], ctoken_str[-4:]))
		ctoken = SonadorApiToken(sconn, cdata)

		# Update the credential instance using a masked value for the model's PK
		umsg = self.user_credential_msg_update_template % (self.user_credential_msg, 'r1')
		ctoken.update({ 'description': umsg })
		self.assertTrue(
			any(ctoken.token[:4] in t.pk and ctoken.token[-4:] in t.pk and umsg == t.description for t in sconn.fetch_user_apitokens()),
			msg='Update of token description failed. Expected: "%s".' % umsg)

		# Remove the credential instance using masked value verify
		ctoken.delete()
		self.assertTrue(all(not ctoken.token[:4] in t.pk for t in sconn.fetch_user_apitokens()),
			msg='Token present on server after making DELETE call')

	def test_apicred_management(self, *args, **kwargs):
		'''	Test API credential management endpoint

			1.	List API access credentials for the user. If an API access credential was used
				to initialize the Sonador connection, ensure that it is present in the list.
			2.	Create a new access ID
			3.	Update the access ID description
			4.	Remove the access ID from the server
		'''
		# Retrieve Sonador connection variables from env
		sconn = self.getSonadorConnection(*args, **kwargs)

		# Ensure that the access ID used for authentication (if an access ID was used for auth)
		# is present in the list retrieved from the credential management endpoint.
		if os.environ.get(SONADOR_ACCESS_ID):

			# Retrieve access ID and secret from environment
			access_id = os.environ.get(SONADOR_ACCESS_ID)
			secret_key = os.environ.get(SONADOR_SECRET_KEY)

			# Check that access ID and secret are present in the keys from the server
			self.assertTrue(
				any(access_id == c.pk and secret_key[:4] in c.secret_key and secret_key[-4:] in c.secret_key for c in sconn.fetch_user_apiaccess_credentials()), 
				msg='Unable to locate the access ID used for authentication in the credentials for the user.')

		# Create a new access credential, ensure that the operation was successful
		rdata = sconn.create_user_apiaccess_credential(object_data={ 'description': self.user_credential_msg })
		self.assertEqual(rdata.get(gapi.STATUS), gapi.SUCCESS, 
			msg='Creation of API token failed with incorrect status. Expected: %s. Result: %s.' % (gapi.SUCCESS, rdata.get(gapi.OPRESULT)))

		# Retrieve object data and check that description was set correctly
		cdata = rdata.get(gapi.OBJECT_DATA)
		cred = SonadorSecureApiCredential(sconn, cdata)
		self.assertTrue(cdata is not None and isinstance(cdata, dict) and cdata.get(SonadorSecureApiCredential.pk_attr) is not None,
			msg='Credential data not returned in the `object-data` key of the response or unable to find access_id.')
		self.assertEqual(cdata.get('description'), self.user_credential_msg,
			msg='Credential response has the wrong description. Exepcted: "%s". Result: "%s".' % (
				self.user_credential_msg, cdata.get('description'),
			))
		self.assertTrue(
			any(cred.access_id == t.pk and MASKED_VALUE_SEP in t.secret_key and cred.secret_key[:4] in t.secret_key and cred.secret_key[-4:]
				for t in sconn.fetch_user_apiaccess_credentials()),
			msg='Unable to find a masked version of the credential for the user.')

		# Update credential description
		umsg = self.user_credential_msg_update_template % ( self.user_credential_msg, 'r1')
		cred.update({ 'description': umsg })
		self.assertTrue(
			any(cred.access_id == t.pk and umsg == t.description for t in sconn.fetch_user_apiaccess_credentials()),
			msg='Update of credential description failed. Expected: "%s".' % umsg)

		# Remove the credential
		cred.delete()
		self.assertTrue(all(not cred.pk == t.pk for t in sconn.fetch_user_apiaccess_credentials()),
			msg='Credential present on server after making DELETE call')
