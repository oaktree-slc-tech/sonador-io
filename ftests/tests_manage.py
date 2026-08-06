import posixpath, logging, requests
from time import sleep, time

from client import apisettings as gapi

from ..apisettings import IMAGING_SERVER_RESOURCE_STUDY, IMAGING_SERVER_RESOURCE_SERIES, \
	DCMHEADER_STUDY_INSTANCE_UID, DCMHEADER_SERIES_INSTANCE_UID, \
	DICOMWEB_ENDPOINT_ARCHIVE, DICOMWEB_ENDPOINT_MANAGE
from ..helpers import response2filearchive

from ..imaging.orthanc import ImagingStudy, ImagingSeries, DcmInstance

from ..test.acl import AclBaseTestCase, TESTGROUP01, TESTUSER01

logger = logging.getLogger(__name__)


# DICOM UID which is guaranteed to have no DicomIdentifiers mapping
DCMWEB_UNKNOWN_UID = '9.9.9.9.9.9.9.9'


class SonadorDcmManageEndpointTestCase(AclBaseTestCase):
	'''	Test removal of studies and series via the DICOMweb resource management endpoints
		(orthanc-sonador#57 5.7):

			DELETE {dicomweb_root}/studies/{StudyInstanceUID}/manage
			DELETE {dicomweb_root}/series/{SeriesInstanceUID}/manage

		The endpoints resolve a DICOM UID to a Sonador cache resource, confirm that it exists,
		and answer with a 307 redirect to that resource's own Orthanc API URL, where the
		removal is actually performed. Authorization is enforced outside the views, by the
		orthanc-authorization plugin consulting the Sonador ACL system, so these tests drive
		the endpoints with raw requests calls and assert on the returned status code directly
		(assertDenied/assertRejected wrap ClientOperationError and are only useful for
		sonador-client calls).

		These flows are destructive. Every test stages its own data through
		stageImageArchiveSeries and the staged data is removed on exit; cleanupImageUpload
		already tolerates a resource which the test itself has removed.
	'''

	def tearDown(self):
		'''	Remove server policies associated with test data
		'''
		self.tearDownAcl()

	def public_apiurl(self, iserver, resource_endpoint):
		'''	Build a fully qualified Orthanc API URL on the PUBLIC origin.

			The public origin is used deliberately. orthanc_apiurl honours the imaging server's
			internal_dns setting (SONADOR_INTERNAL_DNS), while the Location emitted by the
			management view is always the registered public origin
			(orthanc_apiurl_fqdn(..., internal_dns=False) is unconditional in the plugin). If
			the first hop is addressed by the internal origin the redirect becomes
			cross-origin, requests strips the Authorization header at the origin boundary, and
			the second hop answers 403 -- a failure which reads as an authorization defect and
			is not one. See orthanc-sonador#57 V-3.

			@input iserver (sonador.PacsImagingServer): imaging server instance to address
			@input resource_endpoint (str): resource path, e.g. an imaging resource's
				dicomweb_manage_url or resource_url

			@returns str: fully qualified URL on the public origin
		'''
		return iserver.orthanc_apiurl(resource_endpoint, internal_dns=False)

	def dcmweb_unknown_uid_manage_url(self, iserver, resource_class):
		'''	Build a DICOMweb management URL for a DICOM UID which maps to no resource. Composed
			here rather than read from a model property, since by construction there is no model
			instance to read it from.

			@input resource_class: imaging resource class supplying the DICOMweb collection
				segment, ImagingStudy or ImagingSeries
		'''
		return self.public_apiurl(iserver, posixpath.join(
			iserver.dicomweb_root, resource_class.fetch_endpoint, DCMWEB_UNKNOWN_UID, DICOMWEB_ENDPOINT_MANAGE))

	def assertResourceRemoved(self, iserver, resource_url, msg=None):
		'''	Assert that the provided Orthanc resource endpoint no longer resolves.

			The check is made with the administrative server credentials rather than the
			limited ones used to issue the removal, so that a 403 from the authorization
			plugin cannot be mistaken for a removed resource.

			@input iserver (sonador.PacsImagingServer): administrative imaging server instance
			@input resource_url (str): Orthanc resource endpoint, e.g. "series/{orthanc-id}"
		'''
		r = requests.get(self.public_apiurl(iserver, resource_url),
			headers=iserver.orthanc_request_headers(), verify=iserver.verify_ssl(), timeout=30)
		self.assertEqual(r.status_code, 404,
			msg=msg or 'Expected 404 for removed resource "%s". Got: %s. Body: %s' % (
				resource_url, r.status_code, r.text[:200]))

	def assertResourceUncached(self, iserver, sfilter, resource, timeout=30, interval=0.5, msg=None):
		'''	Assert that the Sonador resource cache no longer indexes a resource matching the
			provided query filter.

			Orthanc removes the resource synchronously, but the Sonador cache row is pruned
			asynchronously by the change-callback pipeline (lua/SonadorEvents.lua ->
			sonador_orthanc/web/events.py -> tasks/maintenance/cache.py::remove_cache_resource).
			The delay scales with the number of rows to prune, so a study removal takes
			measurably longer than a series removal. Poll to a deadline rather than sleeping a
			fixed interval: a fixed sleep either flakes or is padded well past what it needs.

			@input iserver (sonador.PacsImagingServer): administrative imaging server instance
			@input sfilter (dict): query terms identifying the removed resource
			@input resource (str): resource level for the query, e.g. "Study" or "Series"
			@input timeout (int, default=30): seconds to wait for the cache row to be pruned
			@input interval (float, default=0.5): seconds between polls
		'''
		_deadline = time() + timeout

		while True:

			_results = iserver.query(sfilter, resource=resource)
			if len(_results) == 0:
				return

			if time() >= _deadline:
				return self.fail(msg or
					'%s resource matching %s is still present in the Sonador resource cache %ds after removal. Matches: %d'
						% (resource, sfilter, timeout, len(_results)))

			sleep(interval)

	def test_dcmweb_manage_delete_series_acl_ltd(self, *args, **kwargs):
		'''	Verify that a limited user holding a local "Remove" grant is able to remove a series
			via the DICOMweb management endpoint.
		'''
		# Setup test authentication: create user, group, and generate a blank ACL policy to associate the group with the server
		iserver, testgroup01, testuser01 = self.setupTestAuth(
			testuser_config=TESTUSER01, testgroup_name=TESTGROUP01, **kwargs)
		server_acl = iserver.admin_create_acl(testgroup01, { 'resource': '*', 'duration': 1 })

		# Download test series
		r_cx = self.fetchTestResource(self.nih_cxr_testdcm)

		with self.getLimitedImageServer(iserver, testuser01, object_data={'description': 'ACL manage testing'}) as iserver_test:

			# Stage test files to imaging server
			with self.stageImageArchiveSeries(iserver, response2filearchive(r_cx)) as (test_sx, test_hache):

				# Create group ACL authorizing removal of the test series
				testacl01_sx_local = test_sx.create_group_acl(testgroup01, {
					'View': True, 'Modify': False, 'Remove': True, 'CommentEdit': True, 'CommentView': True, 'ACL': False,
				})

				# Create DICOMweb management URL for series: /dicom-web/series/{ SeriesInstanceUID }/manage
				url = self.public_apiurl(iserver_test, test_sx.dicomweb_manage_url)

				# Assert against the UN-followed response. This is the point of the test: a
				# regression from 307 to 302 permits a user agent to rewrite the DELETE to a
				# GET, which lands on the resource view's get handler and answers 200 with
				# resource JSON having removed nothing (orthanc-sonador#57 AR-1).
				r = requests.delete(url, allow_redirects=False,
					headers=iserver_test.orthanc_request_headers(), verify=iserver_test.verify_ssl(), timeout=30)
				self.assertEqual(r.status_code, 307,
					msg='Expected 307 from DICOMweb series management endpoint. Got: %s. Body: %s' % (
						r.status_code, r.text[:200]))
				self.assertTrue(r.headers.get('Location', '').endswith('/%s' % test_sx.resource_url),
					msg='Redirect Location does not address the series resource endpoint. Expected suffix: /%s. Got: %s' % (
						test_sx.resource_url, r.headers.get('Location')))

				# The view itself performs no removal; the series is still present until the
				# redirect is followed.
				_results = iserver.query({ DCMHEADER_SERIES_INSTANCE_UID: test_sx.series_uid },
					resource=IMAGING_SERVER_RESOURCE_SERIES)
				self.assertEqual(len(_results), 1,
					msg='Test series was removed by the management view itself, before the redirect was followed.')

				# Follow the redirect and remove the series
				r = requests.delete(url, allow_redirects=True,
					headers=iserver_test.orthanc_request_headers(), verify=iserver_test.verify_ssl(), timeout=30)
				self.assertTrue(r.ok,
					msg='Removal failed after following the management redirect. Status-code: %s. Body: %s' % (
						r.status_code, r.text[:200]))
				self.assertEqual([_h.status_code for _h in r.history], [307],
					msg='Expected the removal to have been reached through a single 307 redirect. Got: %s' % (
						[_h.status_code for _h in r.history],))

				# Confirm the series is gone, both from Orthanc and from the Sonador resource cache
				self.assertResourceRemoved(iserver, test_sx.resource_url)
				self.assertResourceUncached(iserver, { DCMHEADER_SERIES_INSTANCE_UID: test_sx.series_uid },
					IMAGING_SERVER_RESOURCE_SERIES)

	def test_dcmweb_manage_delete_study_acl_ltd(self, *args, **kwargs):
		'''	Verify that a limited user holding a local "Remove" grant is able to remove a study
			via the DICOMweb management endpoint, and that the removal cascades to the study's
			child series and instances.
		'''
		# Setup test authentication: create user, group, and generate a blank ACL policy to associate the group with the server
		iserver, testgroup01, testuser01 = self.setupTestAuth(
			testuser_config=TESTUSER01, testgroup_name=TESTGROUP01, **kwargs)
		server_acl = iserver.admin_create_acl(testgroup01, { 'resource': '*', 'duration': 1 })

		# Download test series
		r_cx = self.fetchTestResource(self.nih_cxr_testdcm)

		with self.getLimitedImageServer(iserver, testuser01, object_data={'description': 'ACL manage testing'}) as iserver_test:

			# Stage test files to imaging server
			with self.stageImageArchiveSeries(iserver, response2filearchive(r_cx)) as (test_sx, test_hache):

				test_s = iserver.get_study(test_sx.parent.pk)

				# Capture the child resources so the cascade can be verified after removal
				test_instances = list(test_sx.instances or [])
				self.assertTrue(len(test_instances) > 0,
					msg='Test series carries no instances; unable to verify that removal cascades to instances.')

				# Create group ACL authorizing removal of the test study
				testacl01_s_local = test_s.create_group_acl(testgroup01, {
					'View': True, 'Modify': False, 'Remove': True, 'CommentEdit': True, 'CommentView': True, 'ACL': False,
				})

				# Create DICOMweb management URL for study: /dicom-web/studies/{ StudyInstanceUID }/manage
				url = self.public_apiurl(iserver_test, test_s.dicomweb_manage_url)

				# Assert against the UN-followed response (see AR-1, above)
				r = requests.delete(url, allow_redirects=False,
					headers=iserver_test.orthanc_request_headers(), verify=iserver_test.verify_ssl(), timeout=30)
				self.assertEqual(r.status_code, 307,
					msg='Expected 307 from DICOMweb study management endpoint. Got: %s. Body: %s' % (
						r.status_code, r.text[:200]))
				self.assertTrue(r.headers.get('Location', '').endswith('/%s' % test_s.resource_url),
					msg='Redirect Location does not address the study resource endpoint. Expected suffix: /%s. Got: %s' % (
						test_s.resource_url, r.headers.get('Location')))

				# Follow the redirect and remove the study
				r = requests.delete(url, allow_redirects=True,
					headers=iserver_test.orthanc_request_headers(), verify=iserver_test.verify_ssl(), timeout=30)
				self.assertTrue(r.ok,
					msg='Removal failed after following the management redirect. Status-code: %s. Body: %s' % (
						r.status_code, r.text[:200]))

				# Confirm the study and its children are gone
				self.assertResourceRemoved(iserver, test_s.resource_url)
				self.assertResourceRemoved(iserver, test_sx.resource_url,
					msg='Child series "%s" survived removal of its parent study "%s".' % (test_sx.pk, test_s.pk))

				for _instance_id in test_instances:
					self.assertResourceRemoved(iserver, posixpath.join(DcmInstance.fetch_endpoint, _instance_id),
						msg='Child instance "%s" survived removal of the study "%s".' % (_instance_id, test_s.pk))

				# Confirm the Sonador cache rows for the study and its child series are pruned
				self.assertResourceUncached(iserver, { DCMHEADER_STUDY_INSTANCE_UID: test_s.study_uid },
					IMAGING_SERVER_RESOURCE_STUDY)
				self.assertResourceUncached(iserver, { DCMHEADER_SERIES_INSTANCE_UID: test_sx.series_uid },
					IMAGING_SERVER_RESOURCE_SERIES)

	def test_dcmweb_manage_delete_series_acl_denied(self, *args, **kwargs):
		'''	Verify that a limited user holding "View" but not "Remove" is denied by the
			authorization plugin when calling the DICOMweb series management endpoint.
		'''
		# Setup test authentication: create user, group, and generate a blank ACL policy to associate the group with the server
		iserver, testgroup01, testuser01 = self.setupTestAuth(
			testuser_config=TESTUSER01, testgroup_name=TESTGROUP01, **kwargs)
		server_acl = iserver.admin_create_acl(testgroup01, { 'resource': '*', 'duration': 1 })

		# Download test series
		r_cx = self.fetchTestResource(self.nih_cxr_testdcm)

		with self.getLimitedImageServer(iserver, testuser01, object_data={'description': 'ACL manage testing'}) as iserver_test:

			# Stage test files to imaging server
			with self.stageImageArchiveSeries(iserver, response2filearchive(r_cx)) as (test_sx, test_hache):

				# Create group ACL which grants visibility of the series but withholds removal
				testacl01_sx_local = test_sx.create_group_acl(testgroup01, {
					'View': True, 'Modify': False, 'Remove': False, 'CommentEdit': True, 'CommentView': True, 'ACL': False,
				})

				# Create DICOMweb management URL for series: /dicom-web/series/{ SeriesInstanceUID }/manage
				url = self.public_apiurl(iserver_test, test_sx.dicomweb_manage_url)

				r = requests.delete(url, allow_redirects=False,
					headers=iserver_test.orthanc_request_headers(), verify=iserver_test.verify_ssl(), timeout=30)
				self.assertEqual(r.status_code, 403,
					msg='Expected 403 for DICOMweb series removal without a "Remove" grant. Got: %s. Body: %s' % (
						r.status_code, r.text[:200]))

				# Confirm the denial left the series in place
				_results = iserver.query({ DCMHEADER_SERIES_INSTANCE_UID: test_sx.series_uid },
					resource=IMAGING_SERVER_RESOURCE_SERIES)
				self.assertEqual(len(_results), 1,
					msg='Test series is no longer present after a denied removal. Matches: %d' % len(_results))

				# Clear the local grant so it cannot leak into a subsequent test case
				self.clearSeriesTestAcl(test_sx)

	def test_dcmweb_manage_delete_study_acl_revoked(self, *args, **kwargs):
		'''	Verify that the DICOMweb study management endpoint denies removal once the local ACL
			grant enabling it has been revoked.
		'''
		# Setup test authentication: create user, group, and generate a blank ACL policy to associate the group with the server
		iserver, testgroup01, testuser01 = self.setupTestAuth(
			testuser_config=TESTUSER01, testgroup_name=TESTGROUP01, **kwargs)
		server_acl = iserver.admin_create_acl(testgroup01, { 'resource': '*', 'duration': 1 })

		# Download test series
		r_cx = self.fetchTestResource(self.nih_cxr_testdcm)

		with self.stageImageArchiveSeries(iserver, response2filearchive(r_cx)) as (test_sx, test_hache):

			test_s = iserver.get_study(test_sx.parent.pk)

			# Create group ACL authorizing removal of the test study
			testacl01_s_local = test_s.create_group_acl(testgroup01, {
				'View': True, 'Modify': False, 'Remove': True, 'CommentEdit': True, 'CommentView': True, 'ACL': False,
			})

			with self.getLimitedImageServer(iserver, testuser01, object_data={'description': 'ACL manage revoke testing'}) as iserver_test:

				url = self.public_apiurl(iserver_test, test_s.dicomweb_manage_url)

				# Confirm the grant authorizes the removal before revoking it. The redirect is
				# deliberately NOT followed: the management view performs no removal of its own,
				# so a 307 proves the authorization plugin granted the request while leaving the
				# study in place for the post-revocation probe.
				r = requests.delete(url, allow_redirects=False,
					headers=iserver_test.orthanc_request_headers(), verify=iserver_test.verify_ssl(), timeout=30)
				self.assertEqual(r.status_code, 307,
					msg='Expected 307 from DICOMweb study management endpoint prior to revocation. Got: %s. Body: %s' % (
						r.status_code, r.text[:200]))

				# Revoke the local grant
				testacl01_s_local.delete()
				sleep(0.5)

			# The post-revocation probe is issued with a FRESH API token, deliberately.
			#
			# OrthancServiceAuthorizationView caches GRANTED authorization decisions (denials are
			# never cached) for up to AUTH_CREDENTIALS_CACHE_MAX_AGE, and orthanc-sonador#57 6.4
			# records the decision NOT to invalidate that cache when a grant is revoked. Where the
			# cache is active a revocation therefore takes effect only once the window expires,
			# and a same-credential re-probe would be a race against it.
			#
			# The window is real and observable on the development stack, not a theoretical
			# concern: the equivalent same-credential assertions in tests_download.py
			# (test_dcmweb_download_study_acl_revoked and its series counterpart) intermittently
			# observe 200 where they expect 403, on runs where the revocation has not yet taken
			# effect for credentials which were already authorized.
			#
			# A fresh token varies the token component of the cache key, so the probe forces a
			# live authorization decision rather than racing whatever was cached for the
			# credentials used above. It does not weaken the assertion: what is under test is the
			# ACL evaluator's answer after revocation, not the lifetime of the cached decision.
			with self.getLimitedImageServer(iserver, testuser01, object_data={'description': 'ACL manage revoke testing'}) as iserver_revoked:

				url = self.public_apiurl(iserver_revoked, test_s.dicomweb_manage_url)

				r = requests.delete(url, allow_redirects=False,
					headers=iserver_revoked.orthanc_request_headers(), verify=iserver_revoked.verify_ssl(), timeout=30)
				self.assertEqual(r.status_code, 403,
					msg='Expected 403 for DICOMweb study removal after local ACL revocation. Got: %s. Body: %s' % (
						r.status_code, r.text[:200]))

			# Confirm the study survived both probes
			_results = iserver.query_study({ DCMHEADER_STUDY_INSTANCE_UID: test_s.study_uid })
			self.assertEqual(len(_results), 1,
				msg='Test study is no longer present after a denied removal. Matches: %d' % len(_results))

			# Clear any remaining local grants so they cannot leak into a subsequent test case
			self.clearSeriesTestAcl(test_sx)

	def test_dcmweb_manage_unknown_uid(self, *args, **kwargs):
		'''	Verify that a DICOM UID with no DicomIdentifiers mapping answers 404 with a JSON
			error body, and does not raise a TypeError from inside http404_resource_not_found
			(orthanc-sonador#57 AR-5).
		'''
		iserver = self.getImageServer(*args, **kwargs)

		for _rclass in (ImagingStudy, ImagingSeries):

			url = self.dcmweb_unknown_uid_manage_url(iserver, _rclass)

			r = requests.delete(url, allow_redirects=False,
				headers=iserver.orthanc_request_headers(), verify=iserver.verify_ssl(), timeout=30)
			self.assertEqual(r.status_code, 404,
				msg='Expected 404 from DICOMweb %s management endpoint for an unknown UID. Got: %s. Body: %s' % (
					_rclass.fetch_endpoint, r.status_code, r.text[:200]))

			# The plugin emits client.apisettings.ERROR, which is lower case "error"
			try: _rdata = r.json()
			except ValueError:
				return self.fail('DICOMweb %s management endpoint returned a non-JSON 404 body: %s' % (
					_rclass.fetch_endpoint, r.text[:200]))

			self.assertTrue(_rdata.get(gapi.ERROR),
				msg='404 response for an unknown UID carries no "%s" member. Body: %s' % (gapi.ERROR, _rdata))
			self.assertIn(DCMWEB_UNKNOWN_UID, _rdata.get(gapi.ERROR),
				msg='404 error message does not name the requested UID. Body: %s' % _rdata)

	def test_dcmweb_manage_method_not_allowed(self, *args, **kwargs):
		'''	Verify that the DICOMweb management endpoints accept only DELETE, and specifically
			that a GET does not fall through to an inherited archive download handler
			(orthanc-sonador#57 AR-3).

			Exercised against a resource which really exists, so that a fall-through to the
			archive handler would produce an observable redirect rather than a 404 which would
			let the assertion pass vacuously.

			OPTIONS and PATCH are deliberately not asserted here. Orthanc does not dispatch
			OPTIONS to Python REST callbacks, so OrthancBaseView.options is unreachable over
			HTTP and Orthanc answers 404; PATCH is rejected by Orthanc core with a 400 before
			dispatch. Both behaviours are properties of Orthanc rather than of these views --
			/archive and /comments behave identically -- and the CORS preflight is answered by
			the nginx ingress before proxy_pass. See orthanc-sonador#57 V-2.
		'''
		iserver = self.getImageServer(*args, **kwargs)

		# Download test series
		r_cx = self.fetchTestResource(self.nih_cxr_testdcm)

		# Stage test files to imaging server
		with self.stageImageArchiveSeries(iserver, response2filearchive(r_cx)) as (test_sx, test_hache):

			for _resource in (test_sx.parent, test_sx):

				url = self.public_apiurl(iserver, _resource.dicomweb_manage_url)

				for _method in ('get', 'post', 'put'):

					r = getattr(requests, _method)(url, allow_redirects=False,
						headers=iserver.orthanc_request_headers(), verify=iserver.verify_ssl(), timeout=30)
					self.assertEqual(r.status_code, 405,
						msg='Expected 405 for %s on the DICOMweb %s management endpoint. Got: %s. Body: %s' % (
							_method.upper(), _resource.fetch_endpoint, r.status_code, r.text[:200]))

					# A GET which fell through to the inherited archive handler would answer with
					# a redirect to the resource's archive endpoint
					self.assertNotIn(DICOMWEB_ENDPOINT_ARCHIVE, r.headers.get('Location', ''),
						msg='%s on the DICOMweb %s management endpoint redirected to the archive endpoint: %s' % (
							_method.upper(), _resource.fetch_endpoint, r.headers.get('Location')))

				# Confirm the archive endpoint itself is unaffected by the management registration
				r = requests.get(self.public_apiurl(iserver, _resource.dicomweb_filearchive_url), allow_redirects=False,
					headers=iserver.orthanc_request_headers(), verify=iserver.verify_ssl(), timeout=30)
				self.assertEqual(r.status_code, 302,
					msg='DICOMweb %s archive endpoint no longer returns its redirect. Got: %s' % (
						_resource.fetch_endpoint, r.status_code))
