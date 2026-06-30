import os, posixpath, logging, json, zipfile, pydicom, requests
from io import BytesIO
from time import sleep

from client import apisettings as gapi
from client.utils.general import first, create_token
from client.utils.object import each
from client.errors import ClientOperationError

from ..apisettings import SONADOR_IMAGING_SERVER, IMAGING_SERVER_RESOURCE_STUDY, IMAGING_SERVER_RESOURCE_SERIES, \
	DCMHEADER_STUDY_INSTANCE_UID, DCMHEADER_SERIES_INSTANCE_UID, DCM_FILE_DICOMDIR
from ..apisettings.worklists import SONADOR_WORKLIST_STATUS_SCHEDULED, SONADOR_WORKLIST_STATUS_INPROGRESS, \
	SONADOR_WORKLIST_STATUS_COMPLETED, SONADOR_WORKLIST_STATUS_CANCELLED
from ..helpers import response2filearchive
from ..servers import sonador_apitoken_fetch
from ..errors import soandor_clientexception_server_errors

from ..imaging.orthanc import ImagingStudy, ImagingSeries

from ..tasks.uploads import imageserver_upload_archive
from ..test import SonadorBaseTestCase, SonadorSeriesBaseTestCase
from ..test.acl import AclBaseTestCase, TESTGROUP01, TESTGROUP02, TESTGROUP03, \
	TESTUSER01_USERNAME, TESTUSER01_ATTRS, TESTUSER01, TESTUSER02, TESTUSER02_USERNAME, TESTUSER02_ATTRS, \
	TESTUSER03_USERNAME, TESTUSER03_ATTRS

logger = logging.getLogger(__name__)


class SonadorDcmDownloadEndpointTestCase(AclBaseTestCase):
	'''	Test download of DICOM zip archive data from /dicom-web/ endpoints.

		1. Test administrative download
		2. Test limited user account download
	'''

	def tearDown(self):
		'''	Remove server policies associated with test data
		'''
		self.tearDownAcl()

	def verifyDcmFiles(self, r, farchive):
		'''	Verify that the provided file archive is associated with the provided series instance.
		'''
		for zpath in farchive.namelist():
			if not DCM_FILE_DICOMDIR in zpath:
				
				# Open DICOM file and verify that unique identifier aligns with the study or series
				with farchive.open(zpath) as f:
					dcm = pydicom.dcmread(BytesIO(f.read()))

					if isinstance(r, ImagingStudy):
						self.assertEqual(getattr(dcm, DCMHEADER_STUDY_INSTANCE_UID, create_token()), r.study_uid,
							msg='Study instance UID does not match uploaded resource. Expected: %s. Received: %s.' % (
									r.study_uid, getattr(dcm, DCMHEADER_STUDY_INSTANCE_UID),
								))

					elif isinstance(r, ImagingSeries):
						self.assertEqual(getattr(dcm, DCMHEADER_SERIES_INSTANCE_UID, create_token()), r.series_uid,
							msg='Series instance UID does not match uploaded resource. Expected: %s. Received: %s.' % (
									r.series_uid, getattr(dcm, DCMHEADER_SERIES_INSTANCE_UID),
								))

	def test_dcmweb_download_study(self, *args, **kwargs):
		'''	Verify DICOMweb study download endpoint
		'''
		iserver, testgroup01, testuser01 = self.setupTestAuth(
			testuser_config=TESTUSER01, testgroup_name=TESTGROUP01, **kwargs)

		# Download test series
		r_cx = self.fetchTestResource(self.nih_cxr_testdcm)

		# Stage test files to imaging server
		with self.stageImageArchiveSeries(iserver, response2filearchive(r_cx)) as (test_sx, test_hache):

			# Create DICOMweb API URL for study: /dicom-web/{ StudyInstanceUID }/archive
			test_s = iserver.get_study(test_sx.parent.pk)
			url = posixpath.join(iserver.dicomweb_root, 'studies', test_sx.parent.study_uid, "archive")

			# Retrieve archive via DICOMweb endpoint
			r = requests.get(iserver.orthanc_apiurl(url),
				headers=iserver.orthanc_request_headers(), verify=iserver.verify_ssl(), timeout=30)
			if not r.ok:
				raise Exception('Unable to retrieve zip archive from Orthanc via study endpoint, server returned non-200 response.')

			# Convert response to file archive instance
			rarchive = response2filearchive(r)

			# Ensure that the data in the file archive came from the study
			self.verifyDcmFiles(test_s, rarchive)

	def test_dcmweb_download_study_acl_ltd(self, *args, **kwargs):
		'''	Attempt to retrieve study archive via DICOMweb study download endpoint
		'''

		# Setup test authentication: create user, group, and generate a blank ACL policy to associate the group with the server
		iserver, testgroup01, testuser01 = self.setupTestAuth(
			testuser_config=TESTUSER01, testgroup_name=TESTGROUP01, **kwargs)
		server_acl = iserver.admin_create_acl(testgroup01, { 'resource': '*', 'duration': 1 })

		with self.getLimitedImageServer(iserver, testuser01, object_data={'description': 'ACL integration testing'}) as iserver_test:

			# Download test series
			r_cx = self.fetchTestResource(self.nih_cxr_testdcm)

			# Stage test files to imaging server
			with self.stageImageArchiveSeries(iserver, response2filearchive(r_cx)) as (test_sx, test_hache):

				# Create group ACL authorizing access to the download endpoint for the test series.
				# A series View permission also confers a limited study view permission, which should
				# authorize access to the download endpoint.
				testacl01_sx_local = test_sx.create_group_acl(testgroup01, {
					'View': True, 'Modify': False, 'Remove': False, 'CommentEdit': True, 'CommentView': True, 'ACL': False,
				})
				
				# Create DICOMweb API URL for study: /dicom-web/{ StudyInstanceUID }/archive
				test_s = iserver.get_study(test_sx.parent.pk)
				url = posixpath.join(iserver.dicomweb_root, 'studies', test_sx.parent.study_uid, "archive")

				# Retrieve archive via DICOMweb endpoint
				r = requests.get(iserver_test.orthanc_apiurl(url),
					headers=iserver_test.orthanc_request_headers(), verify=iserver_test.verify_ssl(), timeout=30)
				if not r.ok:
					raise Exception(
						'Unable to retrieve zip archive from Orthanc via study endpoint, server returned non-200 response. Status-code: %s' % r.status_code)

				# Convert response to file archive instance
				rarchive = response2filearchive(r)

				# Ensure that the data in the file archive came from the study
				self.verifyDcmFiles(test_s, rarchive)
		
	def test_dcmweb_download_series(self, *args, **kwargs):
		'''	Verify DICOMweb series download endpoint
		'''
		iserver, testgroup01, testuser01 = self.setupTestAuth(
			testuser_config=TESTUSER01, testgroup_name=TESTGROUP01, **kwargs)

		# Download test series
		r_cx = self.fetchTestResource(self.nih_cxr_testdcm)

		# Stage test files to imaging server
		with self.stageImageArchiveSeries(iserver, response2filearchive(r_cx)) as (test_sx, test_hache):

			# Create DICOMweb API URL for series: /dicom-web/{ SeriesInstanceUID }/archive
			url = posixpath.join(iserver.dicomweb_root, 'series', test_sx.series_uid, 'archive')

			# Retrieve archive via DICOMweb endpoint
			r = requests.get(iserver.orthanc_apiurl(url),
				headers=iserver.orthanc_request_headers(), verify=iserver.verify_ssl(), timeout=30)
			if not r.ok:
				raise Exception('Unable to retrieve zip archive from Orthanc via series endpoint, server returned non-200 response.')

			# Convert response to file archive instance
			rarchive = response2filearchive(r)

			# Ensure that the data in the file archive came from the study
			self.verifyDcmFiles(test_sx, rarchive)

	def test_dcmweb_download_series_acl_ltd(self, *args, **kwargs):
		'''	Verify DICOMweb series download endpoint
		'''
		# Setup test authentication: create user, group, and generate a blank ACL policy to associate the group with the server
		iserver, testgroup01, testuser01 = self.setupTestAuth(
			testuser_config=TESTUSER01, testgroup_name=TESTGROUP01, **kwargs)
		server_acl = iserver.admin_create_acl(testgroup01, { 'resource': '*', 'duration': 1 })

		# Download test series
		r_cx = self.fetchTestResource(self.nih_cxr_testdcm)

		with self.getLimitedImageServer(iserver, testuser01, object_data={'description': 'ACL integration testing'}) as iserver_test:

			# Stage test files to imaging server
			with self.stageImageArchiveSeries(iserver, response2filearchive(r_cx)) as (test_sx, test_hache):

				# Create group ACL authorizing access to the download endpoint for the test series.
				# A series View permission also confers a limited study view permission, which should
				# authorize access to the download endpoint.
				testacl01_sx_local = test_sx.create_group_acl(testgroup01, {
				 	'View': True, 'Modify': False, 'Remove': False, 'CommentEdit': True, 'CommentView': True, 'ACL': False,
				})

				# Create DICOMweb API URL for series: /dicom-web/{ SeriesInstanceUID }/archive
				url = posixpath.join(iserver.dicomweb_root, 'series', test_sx.series_uid, 'archive')

				# Retrieve archive via DICOMweb endpoint
				r = requests.get(iserver_test.orthanc_apiurl(url),
					headers=iserver_test.orthanc_request_headers(), verify=iserver_test.verify_ssl(), timeout=30)
				if not r.ok:
					raise Exception(
						'Unable to retrieve zip archive from Orthanc via series endpoint, server returned non-200 response. Status-code: %s' % r.status_code)

				# Convert response to file archive instance
				rarchive = response2filearchive(r)

				# Ensure that the data in the file archive came from the study
				self.verifyDcmFiles(test_sx, rarchive)