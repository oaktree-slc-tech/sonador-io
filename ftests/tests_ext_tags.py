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

		# Remove tags created by test methods, then the policies. A group's tags are reachable
		# only while the group has a policy on the server, and a removal requires Series Tags to be
		# enabled on that policy, even for the administrator.
		testgroup01 = iserver.server.admin_create_group(self.testgroup01)
		testgroup02 = iserver.server.admin_create_group(self.testgroup02)
		_group_ids = set((testgroup01.pk, testgroup02.pk))

		for _acl_policy in iserver.fetch_acl():
			if _acl_policy.group not in _group_ids:
				continue

			try:
				_acl_policy.update({ 'tag': True })
				for _t in iserver.fetch_tags(testgroup01 if _acl_policy.group == testgroup01.pk else testgroup02):
					_t.delete()

			except Exception as err:
				logger.warning('Unable to remove tags for group=%s due to an error. Error:\n%s' % (_acl_policy.group, err))

			finally:
				_acl_policy.delete()

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

		# Create server policy to associate the group with the image server; Series Tags must be
		# enabled on it before anyone, the administrator included, may create or remove tags
		testacl = iserver.admin_create_acl(testgroup, { 'resource': '*', 'duration': 1, 'tag': True, 'tag_modify': True })

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

	def test_tag_meta(self, *args, **kwargs):
		'''	Ensure that a tag's `Meta` document (display attributes) can be created, read, replaced,
			and cleared, and that invalid display attributes are refused

			1.	Create a tag with display colors; colors are returned in canonical `#RRGGBB` form
			2.	Replace the document with `update_meta`; the code fields are unchanged
			3.	An invalid color is refused and the stored document is kept
			4.	`update_meta(None)` clears the document
		'''
		iserver, testgroup, testuser = self.setupTestAuth(
			testuser_config=TESTUSER01, testgroup_name=self.testgroup01, **kwargs)
		iserver.admin_create_acl(testgroup, { 'resource': '*', 'duration': 1, 'tag': True, 'tag_modify': True })

		tag0 = iserver.create_tag(testgroup, TEST_IMG_ACCEPT,
			meta={ 'DisplayAttributes': { 'Color': { 'Background': '#123', 'Border': '#fff', 'Value': '#8ad0a8' } } })

		tag1 = iserver.get_tag(testgroup, tag0.pk)
		self.assertEqual(tag1.display_color, { 'Background': '#112233', 'Border': '#FFFFFF', 'Value': '#8AD0A8' })

		# The instance holds the normalized document and does not alias the caller's dictionary
		meta = { 'DisplayAttributes': { 'Color': { 'Value': '#abc', 'Meaning': None } } }
		tag1.update_meta(meta)
		self.assertEqual(tag1.display_color, { 'Value': '#AABBCC' })
		meta['DisplayAttributes']['Color']['Value'] = '#000000'
		self.assertEqual(tag1.display_color, { 'Value': '#AABBCC' })

		tag1.update_meta({ 'DisplayAttributes': { 'Color': { 'Meaning': '#C0C7F8' } } })
		self.assertEqual(tag1.display_color, { 'Meaning': '#C0C7F8' })
		tag2 = iserver.get_tag(testgroup, tag0.pk)
		self.assertEqual(tag2.display_color, { 'Meaning': '#C0C7F8' })
		self.assertEqual((tag2.value, tag2.meaning, tag2.scheme, tag2.scheme_version),
			(TEST_IMG_ACCEPT.value, TEST_IMG_ACCEPT.meaning, TEST_IMG_ACCEPT.scheme_designator, TEST_IMG_ACCEPT.scheme_version))

		for invalid in ({ 'DisplayAttributes': { 'Color': { 'Value': 'green' } } }, { 'DisplayAttributes': { 'Font': 'Arial' } }, { 'Unknown': {} }):
			self.assertRaises(ClientOperationError, lambda: tag2.update_meta(invalid))
		self.assertEqual(iserver.get_tag(testgroup, tag0.pk).display_color, { 'Meaning': '#C0C7F8' })

		tag2.update_meta(None)
		self.assertEqual((tag2.meta, tag2.display_color), ({}, {}))
		tag3 = iserver.get_tag(testgroup, tag0.pk)
		self.assertEqual((tag3.meta, tag3.display_color), ({}, {}))
