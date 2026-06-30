import os, logging, unittest, pkgutil, contextlib, traceback

from time import sleep

import client.apisettings as gcapi
from client.utils.conversion import str2bool

from ..helpers import initenv_sonador_server
from ..servers import sonador_apitoken_fetch
from ..servers.auth import AdminSonadorApiToken
from ..apisettings import SONADOR_IMAGING_SERVER, IMAGING_SERVER_RESOURCE_STUDY, IMAGING_SERVER_RESOURCE_SERIES, \
	SONADOR_ACCESS_ID, SONADOR_SECRET_KEY, SONADOR_URL, SONADOR_APITOKEN, SONADOR_INTERNAL_DNS, SONADOR_VERIFY_SSL
from ..tasks.uploads import imageserver_upload_archive
from ..tasks.maintenance import imageserver_clear_index, imageserver_index_seriesdata

logger = logging.getLogger(__name__)


def load_testmodule(loader, suite, module, pattern=None):
	'''	Retrieve all unit test cases from the provided module
	'''
	for tcase in loader.loadTestsFromModule(module, pattern=pattern):
		logger.debug('Load test cases:\n%s' % '\n'.join(['%s' % t for t in tcase._tests]))
		suite.addTests(tcase)


def load_testcases(mpath, loader, suite, pattern=None):
	'''	Walk the provided module path, find test cases that match the provided
		pattern and add them to the test suite.
	'''
	logger.debug('Root test folder: %s' % mpath)
	
	# Load from test runner modules
	for imp, modname, _ in pkgutil.walk_packages(mpath):
		logger.debug('Scan module "%s" for function test cases' % modname)
		load_testmodule(
			loader, suite, imp.find_module(modname).load_module(modname), pattern=pattern)


class SonadorBaseTestCase(unittest.TestCase):
	'''	Unit TestCase with helper methods for working Sonador/Orthanc instances
	'''

	def initenv_sonador_server(self, sonador_url=None, access_id=None, secret_key=None, apitoken=None,
							   internal_dns=None, verify_ssl=None, **kwargs):
		''' Initialize Sonador Server connection. The method reads the standard Sonador environment
			variables for default arguments. If the environment variable is not defined, the default
			for the argument will be None.
		'''
		sonador_url = sonador_url or os.environ.get(SONADOR_URL)
		access_id = access_id or os.environ.get(SONADOR_ACCESS_ID)
		secret_key = secret_key or os.environ.get(SONADOR_SECRET_KEY)
		apitoken = apitoken or os.environ.get(SONADOR_APITOKEN)
		internal_dns = internal_dns or str2bool(os.environ.get(SONADOR_INTERNAL_DNS))
		verify_ssl = verify_ssl or str2bool(os.environ.get(SONADOR_VERIFY_SSL))

		from ..servers import SonadorServer
		return SonadorServer(sonador_url, access_id=access_id, secret_key=secret_key, apitoken=apitoken,
			internal_dns=internal_dns, verify=verify_ssl, **kwargs)

	def getSonadorConnection(self, *args, **kwargs):
		return self.initenv_sonador_server(*args, **kwargs)

	def getImageServer(self, *args, **kwargs):
		'''	Retrieve an image server using the provided arguments or values defined in the environment
		'''
		iserverid = kwargs.get('iserverid') or os.environ.get(SONADOR_IMAGING_SERVER)
		if iserverid is None:
			raise ValueError('Invalid imaging server ID: "%s". Imaging server environment variable ("%s"): "%s".'
				% (iserverid, SONADOR_IMAGING_SERVER, os.environ.get(SONADOR_IMAGING_SERVER)))
		
		iserver = self.getSonadorConnection(*args, **kwargs).get_imageserver(iserverid)
		return iserver

	def logErrorDetails(self, msg, err):
		'''	Log the details and traceback for the provided error instance
		'''
		logger.error('%s Error: "%s"\n%s\n%s' % (msg, err, getattr(err, 'details', None), traceback.format_exc()))
		raise err

	def fetchTestResource(self, url, timeout=3):
		'''	Fetch a remote test resource with a hard timeout.

			On a network error or timeout, calls self.fail() with a clean one-line
			message (no stack trace) so that connectivity problems appear as tidy
			test FAILUREs rather than ERROR tracebacks that would send a developer
			on a debugging detour.
		'''
		import requests as _requests
		try:
			r = _requests.get(url, timeout=timeout)
		except _requests.exceptions.RequestException as err:
			self.fail(
				'Could not retrieve test resource (%s): %s. '
				'Check connectivity — the source may be throttling requests.'
				% (url, type(err).__name__))
		if not r.ok:
			self.fail(
				'Could not retrieve test resource (%s). HTTP %s.'
				% (url, r.status_code))
		return r

	def cleanupImageUpload(self, iserver, hcache, remove_study=False):
		'''	Iterate through the resources in the provided cache and remove them from the server
		'''
		# Remove imaging studies (lower-level objects are purged when clearing the study)
		if remove_study:

			for hkey, hmeta in hcache.items():
				if hkey.resource == IMAGING_SERVER_RESOURCE_STUDY:

					# Retrieve all resources that match the UID
					results = iserver.query_study({ hkey.header: hkey.uid })
					for r in results:
						r.delete()

		# Remove imaging series. (When remove_study is True, this is a backup check to ensure 
		# all resources have been cleaned up.)
		for hkey, hmeta in hcache.items():

			if hkey.resource == IMAGING_SERVER_RESOURCE_SERIES:

				# Retrieve all resources that match thge UID, purge from the system
				results = iserver.query({ hkey.header: hkey.uid }, resource=hkey.resource)
				for r in results:

					# Remove parent study (if indicated)
					if remove_study:
						try: r.parent.delete()
						except Exception as err:
							logger.info('Unable to remove parent study "%s" for series "%s". Error:\n%s' 
								% (getattr(getattr(r, 'parent', None), 'pk', None), r.pk, err))

					# Remove the 
					try: r.delete()
					except Exception as err:
						logger.info('Unable to remove series "%s". Error:\n%s' % (r.pk, err))

		# Clear any items which may be left in the cache as ghosts
		for hkey, hmeta in hcache.items():

			if hkey.resource == IMAGING_SERVER_RESOURCE_SERIES:
				imageserver_clear_index(iserver.query_series({ hkey.header: hkey.uid }, rapid_lookup=True))

	@contextlib.contextmanager
	def stageImageArchiveTestData(self, iserver, afile, *args, **kwargs):
		'''	Context manager: upload the provided archive file to Sonador. Removes the staged image data
			on exit. Yields the header cache.
		'''
		try:
			# Upload archive data to Sonador
			hcache, _ = imageserver_upload_archive(iserver, afile)

			# Pause for 150 ms to allow for final upload to clear, then index series/study/patient data
			sleep(0.15)
			imageserver_index_seriesdata(iserver, hcache)
			
			yield hcache

		# Remove all series added to the server
		finally: self.cleanupImageUpload(iserver, hcache)

	@contextlib.contextmanager
	def getUserToken(self, sconn, user, *args, **kwargs):
		'''	Context manager: create temporary set of credentials. The temporary credentials will be deleted
			on exit. Yields a token instance.
		'''
		# Create temporary credentials
		_auth = sconn.admin_create_user_apitoken(user, **kwargs)
		_token = AdminSonadorApiToken(sconn, _auth.get(gcapi.OBJECT_DATA), user=user)

		yield _token

		# Remove credential instance
		_token.delete()

	@contextlib.contextmanager
	def getLimitedImageServer(self, iserver, user, *args, **kwargs):
		'''	Context manager: create temporary set of credentials for a set of operations using the provided
			image server. The temporary credentials will be deleted on exit. Yields a new image server instance
			using the temporary credentials.
		'''	
		# Create temporary credentials
		with self.getUserToken(iserver.server, user) as _token:
			iserver_limited = iserver.with_credentials(apitoken=_token.token)
			yield iserver_limited


class SonadorSeriesBaseTestCase(SonadorBaseTestCase):
	'''	Unit Test case with helper methods for working with Sonador series data
	'''
	@contextlib.contextmanager
	def stageImageArchiveSeries(self, iserver, afile, rapid_lookup=False, *args, **kwargs):
		'''	Stage a single series from an archive file for a test case. Removes the staged image data
			on exit. If multiple series are found in the archive file, only the first instance is provided.
			Yields the first series instance and the header cache.
		'''
		with self.stageImageArchiveTestData(iserver, afile, *args, **kwargs) as hcache:

			if len(hcache) == 0:
				raise ValueError('Unable to locate imaging series in zipfile.')

			# Iterate through items n
			sx = None
			for hkey, hmeta in hcache.items():

				# Retrieve first series from the  instance from the server
				if hkey.resource == IMAGING_SERVER_RESOURCE_SERIES:

					# Retrieve series from the server
					results = iserver.query({ hkey.header: hkey.uid }, resource=hkey.resource, rapid_lookup=rapid_lookup)
					self.assertEqual(len(results), 1, msg=('Unable to retrieve match for resource (%s) %s=%s' if len(results) == 0
						else 'Retrieved more than a single match for resource (%s) %s=%s') % (hkey.resource, hkey.header, hkey.uid))
					sx = results[0]

					break

			if sx is None:
				raise ValueError('Unable to retrieve an imaging series from Sonador for the test.')

			yield (sx, hcache)

class SonadorStudyBaseTestCase(SonadorBaseTestCase):
	'''	Unit Test case with helper methods for working with Sonador series data
	'''
	@contextlib.contextmanager
	def stageImageArchiveStudy(self, iserver, afile, rapid_lookup=False, *args, **kwargs):
		'''	Stage a single study from an archive file for a test case. Removes the staged image data
			on exit. If multiple studies are found in the archive file, only the first instance is provided.
			Yields the first study instance and the header cache.
		'''
		with self.stageImageArchiveTestData(iserver, afile, *args, **kwargs) as hcache:

			if len(hcache) == 0:
				raise ValueError('Unable to locate imaging study in zipfile.')

			# Iterate through items n
			s = None
			for hkey, hmeta in hcache.items():

				if hkey.resource == IMAGING_SERVER_RESOURCE_STUDY:

					# Retrieve sereis from the server
					results = iserver.query_study({ hkey.header: hkey.uid })
					self.assertEqual(len(results), 1, msg=('Unable to retrieve match for resource (%s) %s=%s' if len(results) == 0
						else 'Retrieved more than a single match for resource (%s) %s=%s') % (hkey.resource, hkey.header, hkey.uid))
					s = results[0]

					break

			if s is None:
				raise ValueError('Unable to retrieve an imaging study from Sonador for the test.')

			yield (s, hcache)