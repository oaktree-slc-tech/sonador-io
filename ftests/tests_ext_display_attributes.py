'''	Functional coverage for Display Attributes: the per-group collection routes and the aggregate,
	exercised under limited ACL policies and by a staff user who is not a member.

	Requires an imaging server running orthanc-sonador with the display attributes table and a Sonador
	that knows the `display_attr` / `display_attr_modify` policy flags.
'''
import copy, logging

from client.utils.general import create_token, first

from ..test.acl import AclBaseTestCase, TESTUSER01, TESTUSER02, TESTUSER_STAFF

logger = logging.getLogger(__name__)


# Protocol Name (0018,1030) is indexed by every Sonador imaging server; Series Description (0008,103E)
# likewise. Which further tags a server indexes is configuration, so the unindexed code used by the
# duplicate / unindexed checks is chosen against the server's catalogue at run time.
INDEXED_CODE = '0018,1030'
INDEXED_CODE_ALT = '(0008,103E)'
UNINDEXED_CANDIDATES = ('0008,0081', '0010,21B0', '0008,1080', '0040,0241', '0018,1000', '0008,1070')


class SonadorDisplayAttributesApiTests(AclBaseTestCase):
	'''	Display Attributes API: read-only members, managing members, staff, and separation from Series Tags
	'''
	def tearDown(self):
		'''	Remove the test groups' display attributes and policies
		'''
		iserver = self.getImageServer()

		for name in (self.testgroup01, self.testgroup02):
			group = iserver.server.admin_create_group(name)
			policy = self._policy(iserver, group, display_attr=True, display_attr_modify=True)

			try:
				self._remove_policy(iserver, group, policy)
			except Exception as err:
				logger.warning('Unable to remove display attributes for group=%s. Error:\n%s' % (group.pk, err))

		self.tearDownAcl()

	def _policy(self, iserver, group, **flags):
		perms = { 'resource': '*', 'duration': 1 }
		perms.update(flags)
		return iserver.admin_create_acl(group, perms)

	def _remove_policy(self, iserver, group, policy):
		'''	A group's collection is reachable only while the group has a policy on the server, and a
			removal, even by the administrator, requires the feature to be enabled on that policy; so
			the policy is re-enabled, the attributes are removed, and the policy is deleted last.
		'''
		try:
			policy.update({ 'display_attr': True })
			for tag in iserver.fetch_display_attributes(group):
				tag.delete()
		finally:
			policy.delete()

	def _codes(self, tags):
		return set(t.code for t in tags)

	def _unindexed_code(self, iserver):
		'''	A standard tag code the server's /cache/dcm-tags catalogue does not hold
		'''
		catalogue = iserver.cache_dcm_tags()
		for code in UNINDEXED_CANDIDATES:
			if tuple(code.split(',')) not in catalogue:
				return code
		self.skipTest('Every candidate code is indexed by server %s' % iserver.server_label)

	def test_member_with_read_flag_cannot_manage(self, *args, **kwargs):
		'''	`display_attr` alone grants the collection and the aggregate, not the write methods
		'''
		iserver, testgroup, testuser = self.setupTestAuth(testuser_config=TESTUSER01, testgroup_name=self.testgroup01, **kwargs)
		testacl = self._policy(iserver, testgroup, display_attr=True, display_attr_modify=False)

		# Seed one entry as the administrator so the member has something to read
		seeded = iserver.create_display_attribute(testgroup, INDEXED_CODE, label='Protocol')

		try:
			with self.getLimitedImageServer(iserver, testuser) as iserver_test:
				tags = iserver_test.fetch_display_attributes(testgroup)
				self.assertIn(seeded.pk, set(t.pk for t in tags), msg='Member with display_attr cannot read the collection')

				aggregate = iserver_test.fetch_display_attributes_aggregate()
				self.assertEqual([(g['id'], g['manage']) for g in aggregate['groups']], [(testgroup.pk, False)])
				self.assertIn(INDEXED_CODE, set(t['Code'] for t in aggregate['tags']))

				# Each write is refused as unauthorized, and the collection is unchanged when read back as the administrator
				self.assertDenied(lambda: iserver_test.create_display_attribute(testgroup, INDEXED_CODE_ALT), msg='create without display_attr_modify')
				self.assertEqual(self._codes(iserver.fetch_display_attributes(testgroup)), { INDEXED_CODE })

				self.assertDenied(lambda: iserver_test.update_display_attribute(testgroup, seeded.pk, 'Renamed'), msg='relabel without display_attr_modify')
				self.assertEqual(iserver.get_display_attribute(testgroup, seeded.pk).label, 'Protocol')

				self.assertDenied(lambda: iserver_test.delete_display_attribute(testgroup, seeded.pk), msg='delete without display_attr_modify')
				self.assertIn(seeded.pk, set(t.pk for t in iserver.fetch_display_attributes(testgroup)))

		finally:
			self._remove_policy(iserver, testgroup, testacl)

	def test_member_with_manage_flag_round_trip(self, *args, **kwargs):
		'''	`display_attr_modify` grants create, relabel and remove, and the server fills the keyword
		'''
		iserver, testgroup, testuser = self.setupTestAuth(testuser_config=TESTUSER01, testgroup_name=self.testgroup01, **kwargs)
		testacl = self._policy(iserver, testgroup, display_attr=True, display_attr_modify=True)

		try:
			with self.getLimitedImageServer(iserver, testuser) as iserver_test:
				tag0 = iserver_test.create_display_attribute(testgroup, INDEXED_CODE_ALT, label='Series')
				self.assertEqual(tag0.code, '0008,103E')
				self.assertEqual(tag0.keyword, 'SeriesDescription')
				self.assertEqual(tag0.label, 'Series')
				self.assertFalse(tag0.private)
				self.assertEqual(tag0.group.get('id'), testgroup.pk)

				tag1 = iserver_test.get_display_attribute(testgroup, tag0.pk)
				self.assertEqual((tag1.pk, tag1.code), (tag0.pk, tag0.code))

				tag1.relabel('Series description')
				self.assertEqual(iserver_test.get_display_attribute(testgroup, tag0.pk).label, 'Series description')

				iserver_test.update_display_attribute(testgroup, tag0.pk, '')
				self.assertIsNone(iserver_test.get_display_attribute(testgroup, tag0.pk).label)

				aggregate = iserver_test.fetch_display_attributes_aggregate()
				self.assertEqual([(g['id'], g['manage']) for g in aggregate['groups']], [(testgroup.pk, True)])
				self.assertIn('0008,103E', set(t['Code'] for t in aggregate['tags']))

				# Duplicate code in the same group, and a code the server does not index: both are validation errors
				self.assertRejected(lambda: iserver_test.create_display_attribute(testgroup, '0008103e'), msg='duplicate code')
				unindexed = self._unindexed_code(iserver)
				self.assertRejected(lambda: iserver_test.create_display_attribute(testgroup, unindexed), msg='unindexed code')
				self.assertEqual(self._codes(iserver_test.fetch_display_attributes(testgroup)), { '0008,103E' })

				iserver_test.delete_display_attribute(testgroup, tag0.pk)
				self.assertNotIn(tag0.pk, set(t.pk for t in iserver_test.fetch_display_attributes(testgroup)))

		finally:
			self._remove_policy(iserver, testgroup, testacl)

	def test_member_without_flags_sees_nothing(self, *args, **kwargs):
		'''	A policy with neither flag still allows the aggregate (empty) but not the collection
		'''
		iserver, testgroup, testuser = self.setupTestAuth(testuser_config=TESTUSER01, testgroup_name=self.testgroup01, **kwargs)
		testacl = self._policy(iserver, testgroup)

		try:
			with self.getLimitedImageServer(iserver, testuser) as iserver_test:
				self.assertEqual(iserver_test.fetch_display_attributes_aggregate(), { 'groups': [], 'tags': [] })
				self.assertDenied(lambda: iserver_test.fetch_display_attributes(testgroup), msg='collection without display_attr')

		finally:
			self._remove_policy(iserver, testgroup, testacl)

	def test_staff_manage_every_policy_group(self, *args, **kwargs):
		'''	A staff user who belongs to no test group manages the collection of any group whose policy
			enables display attributes, and nothing else
		'''
		iserver = self.getImageServer(*args, **kwargs)

		group01 = iserver.server.admin_create_group(self.testgroup01)
		group02 = iserver.server.admin_create_group(self.testgroup02)
		acl01 = self._policy(iserver, group01, display_attr=True)
		acl02 = self._policy(iserver, group02, display_attr=True)

		# The flag is an argument of admin_create_user, not an attribute: the method overwrites attrs['is_staff']
		staff_attrs = copy.deepcopy(TESTUSER_STAFF.attrs)
		staff_attrs['groups'] = []
		staff = iserver.server.admin_create_user(TESTUSER_STAFF.username, create_token(), is_staff=True, attrs=staff_attrs)

		try:
			with self.getLimitedImageServer(iserver, staff) as iserver_staff:
				created = iserver_staff.create_display_attribute(group02, INDEXED_CODE, label='Staff curated')
				self.assertEqual(created.group.get('id'), group02.pk)

				aggregate = iserver_staff.fetch_display_attributes_aggregate()
				managed = dict((g['id'], g['manage']) for g in aggregate['groups'])
				self.assertTrue(managed.get(group01.pk) and managed.get(group02.pk),
					msg='Staff aggregate does not report every policy group as manageable: %s' % managed)
				self.assertIn(INDEXED_CODE, set(t['Code'] for t in aggregate['tags']))

				iserver_staff.delete_display_attribute(group02, created.pk)

				# A policy that does not enable display attributes puts the group out of scope for staff. The
				# refusals are checked on requests the staff user has not made before: a grant is cached for
				# the policy duration, so a repeat of the earlier create could still be answered from the cache.
				seeded = iserver.create_display_attribute(group02, INDEXED_CODE_ALT, label='Seeded')
				acl02.update({ 'display_attr': False })
				self.assertDenied(lambda: iserver_staff.fetch_display_attributes(group02), msg='staff read on a policy without display_attr')
				self.assertDenied(lambda: iserver_staff.update_display_attribute(group02, seeded.pk, 'Renamed'), msg='staff relabel on a policy without display_attr')
				self.assertDenied(lambda: iserver_staff.delete_display_attribute(group02, seeded.pk), msg='staff delete on a policy without display_attr')
				self.assertEqual(iserver.get_display_attribute(group02, seeded.pk).label, 'Seeded')
				self.assertNotIn(group02.pk, set(g['id'] for g in iserver_staff.fetch_display_attributes_aggregate()['groups']))

				# The plugin applies the rule itself: the administrator, whom Sonador authorizes for
				# everything, is refused a create on the disabled policy with a validation error
				self.assertRejected(lambda: iserver.create_display_attribute(group02, INDEXED_CODE), msg='administrator create on a policy without display_attr')
				self.assertEqual(self._codes(iserver.fetch_display_attributes(group02)), { '0008,103E' })

		finally:
			for group in (group01, group02):
				for policy in iserver.fetch_acl():
					if policy.group == group.pk:
						self._remove_policy(iserver, group, policy)

	def test_display_attr_flags_do_not_grant_series_tags(self, *args, **kwargs):
		'''	The Display Attributes flags and the Series Tags flags are independent
		'''
		iserver, testgroup, testuser = self.setupTestAuth(testuser_config=TESTUSER02, testgroup_name=self.testgroup02, **kwargs)
		testacl = self._policy(iserver, testgroup, display_attr=True, display_attr_modify=True)

		try:
			with self.getLimitedImageServer(iserver, testuser) as iserver_test:
				self.assertDenied(lambda: iserver_test.fetch_tags(testgroup), msg='series tags with display attribute flags only')

		finally:
			self._remove_policy(iserver, testgroup, testacl)
