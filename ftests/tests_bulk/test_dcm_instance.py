import os, logging, traceback, copy, requests, posixpath, json
from time import sleep

from io import BytesIO

from client import apisettings as gapi
from client.utils.general import first, create_token
from client.utils.object import omit
from client.errors import ClientOperationError
from client.remote import request_client_error

from ...apisettings import IMAGING_SERVER_RESOURCE_PATIENT, IMAGING_SERVER_RESOURCE_STUDY, \
	IMAGING_SERVER_RESOURCE_SERIES, IMAGING_SERVER_RESOURCE_IMAGE, IMAGING_SERVER_INCLUDE_INSTANCES, \
	DCMHEADER_STUDY_INSTANCE_UID, DCMHEADER_SERIES_INSTANCE_UID
from ...helpers import response2filearchive
from ...test.acl import AclBaseTestCase, TESTGROUP01, TESTGROUP02, TESTGROUP03, \
	TESTUSER01_USERNAME, TESTUSER01_ATTRS, TESTUSER01, TESTUSER02_USERNAME, TESTUSER02_ATTRS, \
	TESTUSER03_USERNAME, TESTUSER03_ATTRS

from .base import SonadorBulkContentApiTestCase


class SonadorBulkContentApiDcmInstanceTests(SonadorBulkContentApiTestCase):
	'''	Tests for Sonador/Orthanc bulk content API calls. Provides checks for Sonador implementation
		of the bulk content API endpoint and bulkpopulate related methods for Sonador imaging models/collections.
	'''
	def test_bulk_content_instance_request(self, *args, **kwargs):
		'''	Check tools/bulk-content to ensure that the server returns instance data when include instances is true.
		'''
		iserver, testgroup01, testuser01 = self.setupTestAuth(
			testuser_config=TESTUSER01, testgroup_name=self.testgroup01, **kwargs)

		# Create access policy to allow access the server
		server_acl = iserver.admin_create_acl(testgroup01, { 'resource': '*', 'duration': 1 })

		# Download test series
		r_cx = self.fetchTestResource(self.nih_cxr_testdcm)

		# Stage tst files to the imaging server
		with self.stageImageArchiveSeries(iserver, response2filearchive(r_cx)) as (test_sx, test_hcache):

			# Ensure that the test series and parents have been indexed before proceeding with the test
			sleep(0.15)
			test_sx.model_patient.index()
			test_sx.parent.index()
			test_sx.index()

			for dcm in test_sx.instances_collection:
				dcm.index()

			# Ensure that the series and instance JSON can be retrieved via the bulk cotnent endpoint
			resources = iserver.fetch_bulk_content([test_sx.pk, test_sx.parent.pk, test_sx.model_patient.pk], 
				rapid_lookup=True, include_instances=True)

			self.assertTrue(IMAGING_SERVER_RESOURCE_IMAGE in resources, msg='Response does not include an instances collection')
			self.assertTrue(len(resources.get(IMAGING_SERVER_RESOURCE_IMAGE, [])) == 1,
				msg='Instances collection in response does not include any models')
			self.assertEqual(
				set([dcm.pk for dcm in resources.get(IMAGING_SERVER_RESOURCE_IMAGE, [])]), set(test_sx.slices),
				msg='Mismatch between the series instance UIDs returned by the bulk content request and those which should be present '
					+ 'in the series.')

			# Ensure that instances ARE NOT included when include_instances is False
			resources = iserver.fetch_bulk_content([test_sx.pk, test_sx.parent.pk, test_sx.model_patient.pk],
				rapid_lookup=True, include_instances=False)

			self.assertFalse(IMAGING_SERVER_RESOURCE_IMAGE in resources, 
				msg='Response returned instances collection even though it was not requested')

	def test_bulkpopulate_related_patient(self, *args, **kwargs):
		'''	Check bulkpopulate_related methods of client to ensure that patient data loads correctly
		'''
		iserver, testgroup01, testuser01 = self.setupTestAuth(
			testuser_config=TESTUSER01, testgroup_name=self.testgroup01, **kwargs)

		# Create access policy to allow access to the server
		server_acl = iserver.admin_create_acl(testgroup01, { 'resource': '*', 'duration': 1 })

		# Download test series
		r_cx = self.fetchTestResource(self.nih_cxr_testdcm)

		# Stage test files to the imaging server
		with self.stageImageArchiveSeries(iserver, response2filearchive(r_cx)) as (test_sx, test_hcache):

			# Ensure that the test series and parents have been indexed before proceeding with the test
			sleep(0.15)
			test_sx.model_patient.index()
			test_sx.parent.index()
			test_sx.index()

			for dcm in test_sx.instances_collection:
				dcm.index()

			# Retrieve patient results
			results = iserver.query_patient({
				DCMHEADER_STUDY_INSTANCE_UID: test_sx.parent.study_uid
			}, rapid_lookup=True)

			self.assertTrue(len(results) == 1,
				msg=('Unable to locate patient instance matching upload, or search retrieved '
					+ 'more than one match: expected=%s results=%s') % (1, len(results)))
			self.assertTrue(results[0].pk == test_sx.model_patient.pk,
				msg='Lookup returned the wrong patient record')

			# Retrieve child and parent information for patients in the collection
			results.bulkpopulate_related(child_instances=True)

			# Check child studies (populated by bulkpopulate_related) to ensure that the attributes have
			# been added by the method. The private _studies property is used to ensure that fetch_studies
			# is not invoked by accident.
			self.assertTrue(len(getattr(results[0], '_studies', [])) > 0,
				msg='No study instances added to patient during bulkpopulate_related')
			self.assertEqual(
				set([s._objectdata.get('ID') for s in getattr(results[0], '_studies', [])]), set([test_sx.parent.pk]),
				msg='Child studies collection includes unexpected study.')

			# Check child series of the studies collection (populated by bulkpopulate_related) to ensure that 
			# attributes were populated recursively. The private _series property is used to ensure that child
			# objects are not populated dynamically.
			self.assertTrue(len(getattr(results[0]._studies[0], '_series', [])) > 0,
				msg='Child series collection not populated during bulkpopulate_related')
			self.assertIn(
				test_sx.pk,
				set([sx._objectdata.get('ID') for sx in results[0]._studies[0]._series]),
				msg='Child series collection does not include expected series.')

			# Check child DICOM instances of the series. As in other tests, private accessor attributes are
			# used for testing to prevent dynamic population of the child instances.
			_target_sx = next(
				(sx for sx in results[0]._studies[0]._series if sx._objectdata.get('ID') == test_sx.pk), None)
			self.assertIsNotNone(_target_sx,
				msg='Test series not found in child series collection after bulkpopulate_related.')
			self.assertTrue(len(getattr(_target_sx, '_slices', [])) > 0,
				msg='Child series does not include DICOM instances populated by bulkpopulate_related.')
			self.assertEqual(
				set([dcm._objectdata.get('ID') for dcm in _target_sx._slices]), set([dcm.pk]),
				msg='Child DICOM instance collection includes unexpected instances.')

	def test_bulkpopulate_related_study(self, *args, **kwargs):
		'''	Check bulkpopulate_related methods of client to ensure that study data loads correctly.
		'''
		iserver, testgroup01, testuser01 = self.setupTestAuth(
			testuser_config=TESTUSER01, testgroup_name=self.testgroup01, **kwargs)

		# Create access policy to allow access to the server
		server_acl = iserver.admin_create_acl(testgroup01, { 'resource': '*', 'duration': 1 })

		# Download test series
		r_cx = self.fetchTestResource(self.nih_cxr_testdcm)

		# Stage test files to the imaging server
		with self.stageImageArchiveSeries(iserver, response2filearchive(r_cx)) as (test_sx, test_hcache):

			# Ensure that the test series and parents have been indexed before proceeding with the test
			sleep(0.15)
			test_sx.model_patient.index()
			test_sx.parent.index()
			test_sx.index()

			for dcm in test_sx.instances_collection:
				dcm.index()

			# Retrieve series results
			results = iserver.query_study({
				DCMHEADER_STUDY_INSTANCE_UID: test_sx.parent.study_uid
			}, rapid_lookup=True)

			self.assertTrue(len(results) == 1, 
				msg=('Unable to locate study instance matching upload, or search retrieved more ' 
					+ 'than one match: expected=%s results=%s' % (1, len(results))))
			self.assertTrue(results[0].pk == test_sx.parent.pk,
				msg='Lookup returned the wrong result')

			# Retrieve child and parent information for studies in the collection
			results.bulkpopulate_related(child_instances=True)

			# Check child series instances (populated by bulkpopulate_related) to ensure that the attributes
			# have been added by the method. The private _series property to ensure that fetch_series 
			# is not invoked by accident.
			self.assertIn(
				test_sx.pk,
				set([_o._objectdata.get('ID') for _o in getattr(results[0], '_series', [])]),
				msg='Child series collection does not include expected series.')

			# Check study parent (patient) to ensure that it was populated by bulkpopulate_related.
			# Again, the private _parent is inspected to prevent dynamic fetching.
			self.assertTrue(getattr(results[0], '_parent', None) is not None,
				msg='Study patient not populated by bulkpopulate_related method.')
			self.assertEqual(getattr(results[0], '_parent').pk, test_sx.model_patient.pk,
				msg='Study patient does not match upload result')

	def test_bulkpopulate_related_series(self, *args, **kwargs):
		'''	Check bulkpopulate_related methods of client to ensure that series data loads correctly
		'''
		iserver, testgroup01, testuser01 = self.setupTestAuth(
			testuser_config=TESTUSER01, testgroup_name=self.testgroup01, **kwargs)

		# Create access policy to allow access to the server
		server_acl = iserver.admin_create_acl(testgroup01, { 'resource': '*', 'duration': 1 })

		# Download test series
		r_cx = self.fetchTestResource(self.nih_cxr_testdcm)

		# Stage test files to the imaging server
		with self.stageImageArchiveSeries(iserver, response2filearchive(r_cx)) as (test_sx, test_hcache):

			# Ensure that the test series and parents have been indexed before proceeding with the test
			sleep(0.15)
			test_sx.model_patient.index()
			test_sx.parent.index()
			test_sx.index()

			for dcm in test_sx.instances_collection:
				dcm.index()

			# Retrieve uploaded series
			results = iserver.query_series({
				DCMHEADER_SERIES_INSTANCE_UID: test_sx.series_uid
			}, rapid_lookup=True)

			self.assertTrue(len(results) == 1, 
				msg=('Unable to locate series instance matching upload, or search retrieved more '
					+ 'than one match: expected=%s results=%s') % (1, len(results)))
			self.assertTrue(results[0].pk == test_sx.pk,
				msg='Lookup returned the wrong result')

			# Retrieve child and parent information for studies in the collection
			results.bulkpopulate_related(child_instances=True)

			# Check child DICOM instances (populated by bulkpopulate_related) to ensure that the 
			# attributes have been added by the method. The private _slices method is used to ensure
			# that fetch_dcminstances is not triggered.
			self.assertEqual(
				set([_dcm._objectdata.get('ID') for _dcm in getattr(results[0], '_slices', [])]), set([dcm.pk]),
				msg='Child DICOM instances includes unexpected DICOM instances.')

			# Check series study instance (populated by bulkpopulate_related). The _parent private
			# attribute is used to prevent dynamic fetch.
			self.assertTrue(getattr(results[0], '_parent', None) is not None,
				msg='Parent attribute of the series result not populated by bulkpopulate_related')
			self.assertEqual(getattr(results[0], '_parent', None).pk, test_sx.parent.pk,
				msg='Parent attribute of the series populated with the wrong study reference')

			# Check patient instance
			self.assertTrue(getattr(results[0]._parent, '_parent', None) is not None,
				msg='Patient attribute of study result not populated by bulkpopulate_related')
			self.assertEqual(getattr(results[0]._parent, '_parent', None).pk, test_sx.model_patient.pk,
				msg='Patient attribute of series populated with the wrong patient reference')

			# Check sibling references of study and DICOM instances of those models
			self.assertTrue(len(getattr(results[0]._parent, '_series', [])) > 0,
				msg='Study does not include a populated series collection')
			self.assertIn(
				test_sx.pk,
				set([_sx._objectdata.get('ID') for _sx in getattr(results[0]._parent, '_series', [])]),
				msg='Series collection of parent study does not include expected series reference')
			_target_sx = next(
				(sx for sx in getattr(results[0]._parent, '_series', []) if sx._objectdata.get('ID') == test_sx.pk), None)
			self.assertIsNotNone(_target_sx,
				msg='Test series not found in parent study series collection after bulkpopulate_related.')
			self.assertEqual(
				set([_dcm._objectdata.get('ID') for _dcm in getattr(_target_sx, '_slices', [])]), set([dcm.pk]),
				msg='Sibling (nested) collection has unexpected DICOM instances')