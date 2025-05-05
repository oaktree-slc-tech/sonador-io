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

logger = logging.getLogger(__name__)


class SonadorBulkContentApiTestCase(AclBaseTestCase):
	'''	Test case class for Sonador bulk tests. Provides methods to help facilitate data deployment
		and the assessment of bulk content requests.
	'''
	testgroup01 = TESTGROUP01
	testgroup02 = TESTGROUP02

	testuser01 = TESTUSER01_USERNAME
	testuser01_attrs = TESTUSER01_ATTRS

	testuser02 = TESTUSER02_USERNAME
	testuser02_attrs = TESTUSER02_ATTRS

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

	def _verifySeriesSecureBulkContentRequest(self, resources, test_sx,
			verify_series=True, verify_study=True, verify_patient=True, **kwargs):
		'''	Verify a "resources" responses from the Orthanc bulk-content API against
			a the test series that was used to create it.

			IMPORTANT: test requests verified by this method should be made for a series, 
			the parent study, and the patient. Example method call:

			resources = iserver.fetch_bulk_content(
				[test_sx.pk, test_sx.parent.pk, test_sx.model_patient.pk],
				rapid_lookup=True)

			Example resonse: 

			{
				'Series': <collection>,
				'Study': <collection>,
				'Patient': <collection>,
			}

			@input resources (dict): response from sonador.servers.PacsImagingServer.fetch_bulk_content.
				Dictionary of resource collections indexed to the resource type.
			@input test_sx (sonador.imaging.orthanc.ImagingSeries): series used to generate
				the test request.
			@input verify_series (bool, default=True): verify series collection against test series
			@input verify_study (bool, default=True): verify study collection against test study
			@input verify_patient (bool, default=True): verify patient collection against test patient
		'''
		if verify_series:
			self.assertTrue(resources.get(IMAGING_SERVER_RESOURCE_SERIES) \
					and len(resources.get(IMAGING_SERVER_RESOURCE_SERIES)) == 1 \
					and resources.get(IMAGING_SERVER_RESOURCE_SERIES).get_modelinstance(test_sx.pk).pk == test_sx.pk,
				msg='Direct /tools/bulk-content database request returned incorrect series')
		
		if verify_study:
			self.assertTrue(resources.get(IMAGING_SERVER_RESOURCE_STUDY) \
					and len(resources.get(IMAGING_SERVER_RESOURCE_STUDY)) == 1 \
					and resources.get(IMAGING_SERVER_RESOURCE_STUDY).get_modelinstance(test_sx.parent.pk).pk == test_sx.parent.pk,
				msg='Direct /tools/bulk-content database request returned incorrect study')

		if verify_patient:
			self.assertTrue(resources.get(IMAGING_SERVER_RESOURCE_PATIENT) \
					and len(resources.get(IMAGING_SERVER_RESOURCE_PATIENT)) == 1 \
					and resources.get(IMAGING_SERVER_RESOURCE_PATIENT).get_modelinstance(test_sx.model_patient.pk).pk == test_sx.model_patient.pk,
				msg='Direct /tools/bulk-content database request returned incorrect patient')

	def _verifyEmptyBulkContentResponse(self, resources,
			verify_series=True, verify_study=True, verify_patient=True, 
			msg='Invalid bulk-resources lookup. Results retrieved for user who is not authorized to view resource.', **kwargs):
		'''	
		'''
		if verify_patient:
			self.assertTrue(resources.get(IMAGING_SERVER_RESOURCE_PATIENT) is None, msg=msg)

		if verify_study:
			self.assertTrue(resources.get(IMAGING_SERVER_RESOURCE_STUDY) is None, msg=msg)
		
		if verify_series:
			self.assertTrue(resources.get(IMAGING_SERVER_RESOURCE_SERIES) is None, msg=msg)

	def _clearAclVerifyEmpty(self, acl, test_sx, iserver_test, *args, **kwargs):
		'''	Remove the ACL policy and ensure that resource list returned by the test server associated with the series
			returns empty.
		'''
		# Remove the ACL policy
		acl.delete()

		# Attempt to retrieve resources associated with the test series
		resources = iserver_test.fetch_bulk_content([test_sx.pk, test_sx.parent.pk, test_sx.model_patient.pk],
			rapid_lookup=True)
		self._verifyEmptyBulkContentResponse(resources)