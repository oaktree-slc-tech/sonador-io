import os, logging, traceback

from highdicom.sr import CodedConcept

from client.errors import ResourceDoesNotExist, ClientOperationError

from ..apisettings.base import SONADOR_MANUFACTURER, DCMSR_SONADOR_SR, DCMSR_SONADOR_SEG, SONADOR_SCHEME_VERSION_02
from ..test import SonadorBaseTestCase
from ..test.acl import AclBaseTestCase, TESTGROUP01, TESTGROUP02, \
	TESTUSER01_USERNAME, TESTUSER01_ATTRS, TESTUSER01

logger = logging.getLogger(__name__)


TEST_IMG_ACCEPT = CodedConcept('img-qc.accept', DCMSR_SONADOR_SEG.value, 
	'Image accepted for Segmentation', scheme_version=SONADOR_SCHEME_VERSION_02)
TEST_IMG_REJECT = CodedConcept('img-qc.reject', DCMSR_SONADOR_SEG.value,
	'Image rejected for Segmentation', scheme_version=SONADOR_SCHEME_VERSION_02)


class SonadorTagsApiTests(AclBaseTestCase):
	'''	Tests for Sonador/Orthanc tags API

		1. Group tag management: create, review, update, and delete
	'''
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

		# Remove tags created by test methods
		for g in (testgroup01, testgroup02):

			try: 
				for _t in iserver.fetch_tags(g):
					_t.delete()
			
			except Exception as err:
				logger.warning('Unable to remove tags for group=%s due to an error. Error:\n%s' % (g.pk, err))

	def test_tag_management(self, *args, **kwargs):
		'''	Ensure that it is possible to create and manage DICOM tags

			1.	Create a tag via client API interface
			2.	Retrieve copy of the tag using the SOnadorImageServer.get_tag method.
			3.	Update tag `meaning` property and ensure that the changes were persisted
			4.	Remove the tag using `model.delete` method and verify that it was removed
				from the server instance.
		'''
		iserver, testgroup, testuser = self.setupTestAuth(
			testuser_config=TESTUSER01, testgroup_name=self.testgroup01, **kwargs)

		# Create server policy to associate the group with the image server
		testacl = iserver.admin_create_acl(testgroup, { 'resource': '*', 'duration': 1 })

		# Create tag via API
		try: tag0 = iserver.create_tag(testgroup, TEST_IMG_ACCEPT)
		except Exception as err:
			self.logErrorDetails('Unable to create tag due to an error.', err)		

		# Retrieve tags for the group and ensure that the new tag appears in the list
		self.assertTrue(tag0.pk in set(_t.pk for _t in iserver.fetch_tags(testgroup)),
			msg='Unable to locate tag "%s" in tags for group=%s' % (tag0.pk, str(testgroup.pk)))

		# Retrieve tag instance from the server and verify properites
		tag1 = iserver.get_tag(testgroup, tag0.pk)
		self.assertEqual(tag1.pk, tag0.pk,
			msg='Tag=%s instance fetched from server via via server.get_tag method includes a different UID' % tag0.pk)
		self.assertTrue(all((getattr(tag1, attr, None) is not None and getattr(tag1, attr) == getattr(tag0, attr)) for attr in ('value', 'meaning', 'scheme', 'scheme_version')),
			msg='Tag=%s instance fetched from server includes different values than the initial values.\nInitial: %s\nRetrieved: %s\n%s' % (
				tag1.pk, dict(tag0._objectdata), dict(tag1._objectdata), 
				'\n'.join('atrr="%s" "%s"' % (attr, '='.join(('%s' % str(getattr(tag0, attr)), str(getattr(tag1, attr))))) for attr in ('value', 'meaning', 'scheme', 'scheme_version'))
			))
		self.assertTrue(isinstance(tag1.concept, CodedConcept) and tag1.concept.value == TEST_IMG_ACCEPT.value,
			msg='Tag=%s does not return a coded concept from the `concept` attribute. tag.concept type=%s tag.concept-value=%s' % (
				tag1.pk, type(tag1.concept).__name__, tag1.concept.value
			))

		# Remove tag
		tag1.delete()
		self.assertTrue(not tag1.pk in set(_t.pk for _t in iserver.fetch_tags(testgroup)))

		# Delete ACL policy and ensure that attempting to retrieve the tags for the group results in a 404 error
		testacl.delete()
		self.assertRaises(ClientOperationError, lambda: iserver.fetch_tags(testgroup))
