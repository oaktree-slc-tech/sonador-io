import os, logging, traceback, copy, requests, posixpath, json
from time import sleep

from io import BytesIO

from client import apisettings as gapi
from client.utils.general import first, create_token
from client.utils.object import omit
from client.errors import ClientOperationError
from client.remote import request_client_error

from ...apisettings import IMAGING_SERVER_RESOURCE_PATIENT, IMAGING_SERVER_RESOURCE_STUDY, \
	IMAGING_SERVER_RESOURCE_SERIES, IMAGING_SERVER_RESOURCE_IMAGE
from ...helpers import response2filearchive
from ...test.acl import AclBaseTestCase, TESTGROUP01, TESTGROUP02, TESTGROUP03, \
	TESTUSER01_USERNAME, TESTUSER01_ATTRS, TESTUSER01, TESTUSER02_USERNAME, TESTUSER02_ATTRS, \
	TESTUSER03_USERNAME, TESTUSER03_ATTRS

from .base import SonadorBulkContentApiTestCase

logger = logging.getLogger(__name__)


class SonadorBulkContentApiTests(SonadorBulkContentApiTestCase):
	'''	Tests for Sonador/Orthanc bulk content API calls

		1. Cache provided bulk resource fetch/serialization
		2. ACL filtering for users without direct `query` access to the server
	'''
	def test_bulk_content_query_perm(self, *args, **kwargs):
		'''	Check /tools/bulk-content endpoint to ensure that server query permissions allow for full access
			to resources stored on the system. Test procedure:

			1. 	Create a test user and group
			2. 	Create an ACL policy with no server permissions to allow for the test user to access
				resources stored on the server.
			3.	Stage a test chest x-ray to the server.
			4.	Attempt to retrieve resource definitions (patient, study, series) using the admin user.
				Expected: resource definitions provided.
			5.	Attempt to retrieve resource definitions (patient, study, series) using the test user via cache interface.
				Exected: empty response.
			6.	Attempt to retrieve resoure definitions (patient, study, series) using the database interface.
				Expected: 403 error.
			7.	Update the ACL policy to include a query permission.
			8.	Repeat attempts to retrieve data from the endpoint using the test user, validate that resources are
				returned via the cache and database interfaces.
			9.	Remove the query policy, ensure that the test user receives an empty response and a 403 for the
				cache and database interfaces (respectively).
		'''
		iserver, testgroup01, testuser01 = self.setupTestAuth(
			testuser_config=TESTUSER01, testgroup_name=self.testgroup01, **kwargs)

		# Create access policy to allow test user to access the server
		server_acl = iserver.admin_create_acl(testgroup01, { 'resource': '*', 'duration': 1 })

		with self.getLimitedImageServer(iserver, testuser01, object_data={ 'description': 'ACL API testing' }) as iserver_test:

			# Download test series
			r_cx = requests.get(self.nih_cxr_testdcm)
			if not r_cx.ok:
				raise ValueError('Unable to retrieve test data due to an error. Status code: %s' % r_cx.status_code)

			# Stage tst files to the imaging server
			with self.stageImageArchiveSeries(iserver, response2filearchive(r_cx)) as (test_sx, test_hcache):

				# Ensure that the test series and parents have been indexed before proceeding with the test
				sleep(0.15)
				test_sx.model_patient.index()
				test_sx.parent.index()
				test_sx.index()

				# Ensure that the series can be retrieved via the bulk content endpoint (direct request by admin user)
				resources = iserver.fetch_bulk_content([test_sx.pk, test_sx.parent.pk, test_sx.model_patient.pk], rapid_lookup=False)
				self._verifySeriesSecureBulkContentRequest(resources, test_sx)

				# Ensure that the series can be retrieve via the bulk content endpoint via rapid lookup
				resources = iserver.fetch_bulk_content([test_sx.pk, test_sx.parent.pk, test_sx.model_patient.pk], rapid_lookup=True)
				self._verifySeriesSecureBulkContentRequest(resources, test_sx)

				# Ensure that an attempt to retrieve the bulk content endpoint with the test user returns an empty response
				resources = iserver_test.fetch_bulk_content([test_sx.pk, test_sx.parent.pk, test_sx.model_patient.pk], rapid_lookup=True)
				self._verifyEmptyBulkContentResponse(resources)

				# Ensure that an attempt to retrieve bulk content via database interface without a query permission triggers a 403
				self.assertRaises(ClientOperationError, 
					lambda: iserver_test.fetch_bulk_content([test_sx.pk, test_sx.parent.pk, test_sx.model_patient.pk], rapid_lookup=False))

				# Update ACL policy to include a query permission
				server_acl.update({ 'query': True })

				# Ensure that the test user can retrieve resource definitions via both cache and database interfaces
				resources = iserver_test.fetch_bulk_content([test_sx.pk, test_sx.parent.pk, test_sx.model_patient.pk], rapid_lookup=True)
				self._verifySeriesSecureBulkContentRequest(resources, test_sx)
				resources = iserver_test.fetch_bulk_content([test_sx.pk, test_sx.parent.pk, test_sx.model_patient.pk], rapid_lookup=False)
				self._verifySeriesSecureBulkContentRequest(resources, test_sx)

				# Update ACL policy to remove the query permission
				server_acl.update({ 'query': False })

				# Ensure empty and 403 responses for the test user
				resources = iserver_test.fetch_bulk_content([test_sx.pk, test_sx.parent.pk, test_sx.model_patient.pk], rapid_lookup=True)
				self._verifyEmptyBulkContentResponse(resources)
				self.assertRaises(ClientOperationError, 
					lambda: iserver_test.fetch_bulk_content([test_sx.pk, test_sx.parent.pk, test_sx.model_patient.pk], rapid_lookup=False))

	def test_bulk_content_acl(self, *args, **kwargs):
		'''	Check /tools/bulk-content endpoint to ensure that ACL filtering works as expected. Test procedure:

			1. 	Create a test user and group
			2. 	Create an ACL policy with no server permissions to allow for the test user to access
				resources stored on the server.
			3.	Stage a test chest x-ray to the server.
			4.	Retrieve series, study, and patient records using the administrative user to check
				endpoint functionality using both the cache (`rapid_lookup=True`) and 
				database (`rapid_lookup=True`) options.
			5.	Attempt to retrieve resource definitions for test series using the limited/test user.
				Expected: empty response.
			6.	Create a local series ACL policies, attempt to retrieve resource definitions using
				limited/test user. Expected: series record, but no study or patient. User policy tested
				first and then group policy.
			7.	Clear series ACL policies and ensure that the endpont returns an empty response.
				Note: this step is executed simultaneously alongside step 6. The user policy is cleared 
				at the end of the user test and before executing the group ACL creation/test. The group ACL
				is then cleared before proceeding to the next step.
			8.	Create local study ACL policies, attempt to retrieve resource definitions using
				limited/test user. Expected: series and study records, but no patient. User policy tested
				first and then group policy.
			9.	Clear study ACL policies and ensure that the endpoint returns an empty response. (The 
				ACL removal is also executed alongside study ACL testing. Refer to previous note for more
				details.)
			10.	Create local patient ACL policies, attempt to retrieve resource definitions using
				limited/test user. Expected: series, study, and patient records. 
			11. Clear patient ACL policies and ensure that the endpoint returns an empty response.

		'''
		iserver, testgroup01, testuser01 = self.setupTestAuth(
			testuser_config=TESTUSER01, testgroup_name=self.testgroup01, **kwargs)

		# Create access policy to allow test user to access the server
		server_acl = iserver.admin_create_acl(testgroup01, { 'resource': '*', 'duration': 1 })

		with self.getLimitedImageServer(iserver, testuser01, object_data={ 'description': 'ACL API testing' }) as iserver_test:

			# Download test series
			r_cx = requests.get(self.nih_cxr_testdcm)
			if not r_cx.ok:
				raise ValueError('Unable to retrieve test data due to an error. Status code: %s' % r_cx.status_code)

			# Stage test files to imaging server
			with self.stageImageArchiveSeries(iserver, response2filearchive(r_cx)) as (test_sx, test_hcache):

				# Ensure that the test series and parents have been indexed before setting ACL
				sleep(0.15)
				test_sx.model_patient.index()
				test_sx.parent.index()
				test_sx.index()

				# Remove all user/group policies associated with the series
				self.clearSeriesTestAcl(test_sx)

				# Ensure that the series can be retrieved via the bulk content endpoint (direct request by admin user)
				resources = iserver.fetch_bulk_content([test_sx.pk, test_sx.parent.pk, test_sx.model_patient.pk], rapid_lookup=False)
				self._verifySeriesSecureBulkContentRequest(resources, test_sx)

				# Ensure that the series can be retrieve via the bulk content endpoint via rapid lookup
				resources = iserver.fetch_bulk_content([test_sx.pk, test_sx.parent.pk, test_sx.model_patient.pk], rapid_lookup=True)
				self._verifySeriesSecureBulkContentRequest(resources, test_sx)

				# Execute resource lookup via limited account to ensure that endpoint is accessible for non-privileged
				# users and filtering results based on ACL grants
				with self.getLimitedImageServer(iserver, testuser01, object_data={ 'description': 'User ACL API testing'}) as iserver_test:

					# Retrieve resources via limited account lookup, ensure that results are empty
					resources = iserver_test.fetch_bulk_content([test_sx.pk, test_sx.parent.pk, test_sx.model_patient.pk], rapid_lookup=True)
					self._verifyEmptyBulkContentResponse(resources)
					
					# Create series user access access control policy (authorizes access to the series, but not to the study and patient)
					# Retrieve resource list and ensure that it only contains a series entry. 
					testacl01_sx_local_user = test_sx.create_user_acl(testuser01, {
							'View': True, 'Modify': False, 'Remove': False, 'CommentEdit': True, 'CommentView': True, 'ACL': False
						})
					resources = iserver_test.fetch_bulk_content([test_sx.pk, test_sx.parent.pk, test_sx.model_patient.pk], rapid_lookup=True)
					self._verifySeriesSecureBulkContentRequest(resources, test_sx,
						verify_study=False, verify_patient=False)
					self._verifyEmptyBulkContentResponse(resources, verify_series=False)
					self._clearAclVerifyEmpty(testacl01_sx_local_user, test_sx, iserver_test, *args, **kwargs)

					# Create series group access control policy (authorizes access to the series, but not to the study)
					# Retrieve resource list and ensure that it contains a series entry.
					testacl01_sx_local_group = test_sx.create_group_acl(testgroup01, {
							'View': True, 'Modify': False, 'Remove': False, 'CommentEdit': True, 'CommentView': True, 'ACL': False,
						})
					resources = iserver_test.fetch_bulk_content([test_sx.pk, test_sx.parent.pk, test_sx.model_patient.pk], rapid_lookup=True)
					self._verifySeriesSecureBulkContentRequest(resources, test_sx,
						verify_study=False, verify_patient=False)
					self._verifyEmptyBulkContentResponse(resources, verify_series=False)
					self._clearAclVerifyEmpty(testacl01_sx_local_group, test_sx, iserver_test)

					# Create study user ACL policy (authorizes access to the study and series, but not to the patient)
					# Retrieve resource list and ensure that it contains study/series, but not patient.
					testacl01_s_local_user = test_sx.parent.create_user_acl(testuser01, {
							'View': True, 'Modify': False, 'Remove': False, 'ACL': False,
						})
					resources = iserver_test.fetch_bulk_content([test_sx.pk, test_sx.parent.pk, test_sx.model_patient.pk], rapid_lookup=True)
					self._verifySeriesSecureBulkContentRequest(resources, test_sx, verify_patient=False)
					self._verifyEmptyBulkContentResponse(resources, verify_series=False, verify_study=False)
					self._clearAclVerifyEmpty(testacl01_s_local_user, test_sx, iserver_test)

					# Create study group ACL policy (authorizes access to the study and series, but not to the patient)
					# Retrieve resource list and ensure that it contains study/series, but not patient.
					testacl01_s_local_group = test_sx.parent.create_group_acl(testgroup01, {
							'View': True, 'Modify': False, 'Remove': False, 'ACL': False,
						})
					resources = iserver_test.fetch_bulk_content([test_sx.pk, test_sx.parent.pk, test_sx.model_patient.pk], rapid_lookup=True)
					self._verifySeriesSecureBulkContentRequest(resources, test_sx, verify_patient=False)
					self._verifyEmptyBulkContentResponse(resources, verify_series=False, verify_study=False)
					self._clearAclVerifyEmpty(testacl01_s_local_group, test_sx, iserver_test)

					# Create patient user ACL policy (authorizes access to all resources)
					testacl01_p_local_user = test_sx.model_patient.create_user_acl(testuser01, {
							'View': True, 'Modify': False, 'Remove': False, 'ACL': False,
						})
					resources = iserver_test.fetch_bulk_content([test_sx.pk, test_sx.parent.pk, test_sx.model_patient.pk], rapid_lookup=True)
					self._verifySeriesSecureBulkContentRequest(resources, test_sx)
					self._clearAclVerifyEmpty(testacl01_p_local_user, test_sx, iserver_test)

					# Create patient group ACL policy (authorizes access to all resources)
					testacl01_p_local_group = test_sx.model_patient.create_group_acl(testgroup01, {
							'View': True, 'Modify': False, 'Remove': False, 'ACL': False,
						})
					resources = iserver_test.fetch_bulk_content([test_sx.pk, test_sx.parent.pk, test_sx.model_patient.pk], rapid_lookup=True)
					self._verifySeriesSecureBulkContentRequest(resources, test_sx)
					self._clearAclVerifyEmpty(testacl01_p_local_group, test_sx, iserver_test)

				