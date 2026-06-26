import os, datetime, traceback, abc, copy, logging, requests, posixpath, itertools
from collections import namedtuple

from io import BytesIO
from time import sleep

from pydicom.uid import generate_uid

from client.errors import ClientOperationError
from client.utils.general import create_token

from ..helpers.local import dcmread_backfill
from ..apisettings import DCMHEADER_PATIENT_ID, DCMHEADER_SERIES_INSTANCE_UID, \
	DCMCODE_PATIENT_ID, DCMHEADER_PATIENT_NAME, DCMHEADER_STUDY_DESCRIPTION, \
	DCMHEADER_SERIES_DESCRIPTION, DCMHEADER_ACCESSION_NUMBER, DCMHEADER_MODALITY, DCM_MODALITY_MR, DCM_MODALITY_DX, \
	DCMHEADER_STUDY_DATE, DCMHEADER_STUDY_TIME, DCMHEADER_CONTENT_DATE, DCMHEADER_CONTENT_TIME, \
	DCMHEADER_SERIES_NUMBER

from .base import SonadorBaseTestCase, SonadorSeriesBaseTestCase

logger = logging.getLogger(__name__)


# Test users and groups
AclTestUser = namedtuple('AclTestUser', ('username', 'attrs'))

TESTGROUP01 = 'testgroup-acl01'
TESTGROUP02 = 'testgroup-acl02'
TESTGROUP03 = 'testgroup-acl03'
TESTGROUP04 = 'testgroup-acl04'
TESTGROUP05 = 'testgroup-acl05'

TESTUSER01_USERNAME = 'testuser-acl01'
TESTUSER01_ATTRS = {
	'email': '%s@example.com' % TESTUSER01_USERNAME, 'first_name': 'ACL Test 01', 'last_name': 'User',
	'is_supersuer': False, 'is_staff': False,
}
TESTUSER01 = AclTestUser(TESTUSER01_USERNAME, TESTUSER01_ATTRS)

TESTUSER02_USERNAME = 'testuser-acl02'
TESTUSER02_ATTRS = {
	'email': '%s@example.com' % TESTUSER02_USERNAME, 'first_name': 'ACL Test 02', 'last_name': 'User',
	'is_supersuer': False, 'is_staff': False,
}
TESTUSER02 = AclTestUser(TESTUSER02_USERNAME, TESTUSER02_ATTRS)

TESTUSER03_USERNAME = 'testuser-acl03'
TESTUSER03_ATTRS = {
	'email': '%s@example.com' % TESTUSER03_USERNAME, 'first_name': 'ACL Test 03', 'last_name': 'User',
	'is_supersuer': False, 'is_staff': False,
}
TESTUSER03 = AclTestUser(TESTUSER03_USERNAME, TESTUSER03_ATTRS)

TESTUSER04_USERNAME = 'testuser-acl04'
TESTUSER04_ATTRS = {
	'email': '%s@example.com' % TESTUSER04_USERNAME, 'first_name': 'ACL Test 04', 'last_name': 'User',
	'is_supersuer': False, 'is_staff': False,
}
TESTUSER04 = AclTestUser(TESTUSER04_USERNAME, TESTUSER04_ATTRS)

TESTUSER05_USERNAME = 'testuser-acl05'
TESTUSER05_ATTRS = {
	'email': '%s@example.com' % TESTUSER05_USERNAME, 'first_name': 'ACL Test 05', 'last_name': 'User',
	'is_supersuer': False, 'is_staff': False,
}
TESTUSER05 = AclTestUser(TESTUSER05_USERNAME, TESTUSER05_ATTRS)



TEST_DATA_SERIES_DESCRIPTION_TEMPLATE = 'ACL Test Data / Study%d / Series%d'


# Data structures for test data
AclTestFile = namedtuple('AclTestFile', ('filename', 'number'))
AclTestStudy = namedtuple('AclTestStudy', ('patient', 'patient_name', 'number', 'accession', 'description', 'files'))


class AclTestBaseData(abc.ABC):
	'''	Class which can be used to manage data staging for ACL tests

		@property server: imaging server used for the tests
		@property meta: AclTestStudy instance used for deploying the data
		@property data: data 
	'''
	series_description_template = TEST_DATA_SERIES_DESCRIPTION_TEMPLATE

	@property
	@abc.abstractmethod
	def resource_dir(self):
		'''	Directory from which test data should be read during deployment
		'''

	def __init__(self, iserver, meta):
		self.iserver = iserver
		self.meta = meta
		self._data = None

	@property
	def data(self):
		if self._data is None:
			self.deploy()

		return self._data

	@classmethod
	def _dcmread_testfile(cls, meta, tfile, study_uid=None, test_ts=None, series_uid=None):
		''' Load the provided test file back-filled with the properties specified by the study
			and test file meta.

			@returns pydicom.FileDataset
		'''
		study_uid = study_uid or generate_uid()
		test_ts = test_ts or datetime.datetime.utcnow()
		series_uid = series_uid or generate_uid()

		return dcmread_backfill(os.path.join(cls.resource_dir, tfile.filename),
			patient_id=meta.patient, study_uid=study_uid, series_uid=series_uid, study_ts=test_ts, attrs={
				DCMHEADER_PATIENT_NAME: meta.patient_name,
				DCMHEADER_ACCESSION_NUMBER: meta.accession,
				DCMHEADER_STUDY_DESCRIPTION: meta.description,
				DCMHEADER_SERIES_DESCRIPTION: cls.series_description_template % (meta.number, tfile.number),
				DCMHEADER_MODALITY: DCM_MODALITY_DX,
				DCMHEADER_SERIES_NUMBER: tfile.number,
			})

	@classmethod
	def _upload_dcmfile(cls, iserver, test_dcm, fetch_sx=True):
		'''	Upload the the provided DICOM test file to the specified image server

			@returns series reference of uploaded file
		'''
		# Save test file to byte stream
		test_bstream = BytesIO()
		test_dcm.save_as(test_bstream)
		test_bstream.seek(0)

		# Upload to Sonador
		iserver.upload_image(test_bstream)

		if fetch_sx:
			return cls._fetch_sx(iserver, test_dcm)

	@classmethod
	def _fetch_sx(cls, iserver, test_dcm):
		'''	Retrieve the series reference for the provided series
		'''
		# Retrieve image series
		_uid = getattr(test_dcm, DCMHEADER_SERIES_INSTANCE_UID)
		_results = iserver.query({ DCMHEADER_SERIES_INSTANCE_UID: _uid }, rapid_lookup=False)
		if not len(_results):
			raise ValueError('Unable to retrieve series reference for uploaded file dcm-uid=%s' % _uid)

		return _results[0]

	def deploy(self, study_uid=None, test_ts=None):
		''' Deploy the files and other resources described by the metadata. The class returns 
			a copy of self to allow for data chaining.

			@returns self
		'''
		data = {}

		# Create timestamps for dataset
		test_ts = test_ts or datetime.datetime.utcnow()
		study_uid = study_uid or generate_uid()

		# Iterate through files and deploy, create one series per file
		for tfile in self.meta.files:

			# Create test DICOM
			test_dcm = self._dcmread_testfile(self.meta, tfile, study_uid=study_uid, test_ts=test_ts)

			# Add file meta and series reference to data
			data[tfile] = (test_dcm, self._upload_dcmfile(self.iserver, test_dcm))

		self._data = data
		return self


class AclBaseTestCase(SonadorSeriesBaseTestCase):
	'''	Test case class with methods and properties to help facilitate access control (ACL) testing
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

	testuser04 = TESTUSER04_USERNAME
	testuser04_attrs = TESTUSER04_ATTRS

	testuser05 = TESTUSER05_USERNAME
	testuser05_attrsa = TESTUSER05_ATTRS

	nih_cxr_testdcm = 'https://www.oak-tree.tech/documents/331/nih-cxr.patient-30775.zip'

	def setupTestAuth(self, testuser_config, testgroup_name, *args, **kwargs):
		'''	Setup a test authentication structure:

			1. Retrieve image server reference
			2. Create primary test group
			3. Create test user
			
			@returns iserver, group, user 
		'''
		iserver = self.getImageServer(*args, **kwargs)

		# Create test group
		try: testgroup = iserver.server.admin_create_group(testgroup_name)
		except ClientOperationError as err:
			self.logErrorDetails('Unable to create group="%s" due to an error.' % testgroup_name, err)

		# Create test user
		try:
			# Add test user to the test group
			testuser_attrs = copy.deepcopy(testuser_config.attrs)
			testuser_attrs['groups'] = [testgroup.pk]
			testuser = iserver.server.admin_create_user(testuser_config.username, create_token(), attrs=testuser_attrs)
		
		except Exception as err:
			self.logErrorDetails('Unable to create user="%s" due to an error.' % testuser_config.username, err)

		return iserver, testgroup, testuser

	def clearSeriesTestAcl(self, sx, clear_parent=True):
		'''	Clear ACL policies for the provided test series

			@input test_sx (sonador.imaging.orthanc.ImagingSeries): series instance for which the ACL
				policies should be cleared.
			@input clear_parent (bool, default=True): clear ACL policies for the series parents 
				(study and patient)
		'''
		# Clear user/group policies for series
		for _acl in itertools.chain(sx.fetch_user_acl(), sx.fetch_group_acl()):
			_acl.delete()

		# Clear user/group policies for parent
		if clear_parent:

			for _acl in itertools.chain(
				sx.parent.fetch_user_acl(), sx.parent.fetch_group_acl(), sx.model_patient.fetch_user_acl(), sx.model_patient.fetch_group_acl()):
				_acl.delete()

	def _verifySeriesLocalAcl(self, testacl, sx, iserver_test, verify_acl=True):
		'''	Verify that the test server instance is able to query and view the provided test series.

			@input testacl: ACL instance authorizing access to the resource
			@input sx (sonador.imaging.orthanc.ImagingSeries): series instance to check for access permissions
			@input iserver_test (sonador.PacsImagingServer): imaging server instance to use for checking access
			@input verify_acl (bool, default=True): toggles whether the ACL resource should be checked
				against the provided series.
			
			Tests: 

			1.	Ensure that the provided ACL resource matches the provided series (if specified).
			2.	Ensure that the user is able to query the series via the tools/secure-find API endpoint.
			3.	If the "view" permission for the series is present, attempt to retrieve the data.
		'''		
		# Ensure that the provide ACL matches the series (if indicated)
		if verify_acl:
			self.assertEqual(testacl.Series, sx.pk, msg='Test ACL=%s does not match series=%s' % (testacl.pk, sx.pk))

		# Ensure that the limited user is able to see the resource in the tools/secure-find results
		_results = iserver_test.query_series({ DCMHEADER_SERIES_INSTANCE_UID: sx.series_uid })
		self.assertTrue(len(_results) == 1,
			msg='/tools/secure-search returned incorrect number of results. Expected: 1. Actual: %d' % len(_results))
		self.assertTrue(all(_sx.pk == sx.pk for _sx in _results),
			msg='Test series=%s not visible after creating user ACL authorizing access' % sx.pk)

		# Use limited server to retrieve the metadata (requires view permission)
		if getattr(testacl, 'View', True):

			try: _sx = iserver_test.get_series(sx.pk)
			except ClientOperationError as err:
				self.logErrorDetails('Unable to retrieve test series from imageserver.', err)

	def _verifyDicomWebLocalAcl(self, testacl, sx, iserver_test):
		'''	Verify that the test server instance is able to view and query the provided series instance
			via Orthanc's DICOMweb API.
		'''
		# Execute query via DICOMweb endpoint and ensure that the test series appears in the results
		_r_dcmweb_results = iserver_test._request_get(
			iserver_test.orthanc_apiurl(posixpath.join(iserver_test.dicomweb_root, 'studies'),
				query_params={ DCMHEADER_SERIES_INSTANCE_UID: sx.series_uid }),
			headers=iserver_test.orthanc_request_headers())
		_dcmweb_results = _r_dcmweb_results.json()

		# Ensure that the DICOMweb API returned the correct response
		self.assertTrue(len(_dcmweb_results) == 1, 
			msg='DICOMweb API search returned incorrect number of results. Expected: 1. Actual: %d.' % len(_dcmweb_results))
		self.assertTrue(
			all(_s.get(''.join(DCMCODE_PATIENT_ID), {}).get('Value', [])[0] == sx.model_patient.patientid for _s in _dcmweb_results),
			msg='DICOMweb API returned series instance with the wrong PatientID. Expected: %s.' % sx.model_patient.patientid)


	def tearDownAcl(self, *args, **kwargs):
		''' Remove server policies associated with test user or groups
		'''	
		iserver = self.getImageServer()

		# Remove all policies associated with test user or groups
		testgroup01 = iserver.server.admin_create_group(self.testgroup01)
		testgroup02 = iserver.server.admin_create_group(self.testgroup02)
		_group_ids = set((testgroup01.pk, testgroup02.pk))

		for _acl_policy in iserver.fetch_acl():
			if _acl_policy.group in _group_ids:
				_acl_policy.delete()