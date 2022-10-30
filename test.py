import os, logging, unittest, pkgutil

from client.utils.conversion import str2bool

from .helpers import initenv_sonador_server
from .servers import sonador_apitoken_fetch
from .apisettings import SONADOR_IMAGING_SERVER, IMAGING_SERVER_RESOURCE_STUDY, IMAGING_SERVER_RESOURCE_SERIES, \
	SONADOR_ACCESS_ID, SONADOR_SECRET_KEY, SONADOR_URL, SONADOR_APITOKEN, SONADOR_INTERNAL_DNS

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
							   internal_dns=None, **kwargs):
		''' Initialize Sonador Server connection. The method reads the standard Sonador environment
			variables for default arguments. If the environment variable is not defined, the default
			for the argument will be None.
		'''
		sonador_url = sonador_url or os.environ.get(SONADOR_URL)
		access_id = access_id or os.environ.get(SONADOR_ACCESS_ID)
		secret_key = secret_key or os.environ.get(SONADOR_SECRET_KEY)
		apitoken = apitoken or os.environ.get(SONADOR_APITOKEN)
		internal_dns = internal_dns or str2bool(os.environ.get(SONADOR_INTERNAL_DNS))

		from .servers import SonadorServer
		return SonadorServer(sonador_url, access_id=access_id, secret_key=secret_key, apitoken=apitoken,
			internal_dns=internal_dns, **kwargs)

	def getSonadorConnection(self, *args, **kwargs):
		return self.initenv_sonador_server(*args, **kwargs)

	def getImageServer(self, *args, **kwargs):
		'''	Retrieve an image server using the provided arguments or values defined in the environment
		'''
		iserverid = kwargs.get('iserverid') or os.environ.get(SONADOR_IMAGING_SERVER)
		if iserverid is None:
			raise ValueError('Invalid imaging server ID: "%s". Imaging server environment variable ("%s"): "%s".'
				% (iserverid, SONADOR_IMAGING_SERVER, os.environ.get(SONADOR_IMAGING_SERVER)))

		sconn = initenv_sonador_server(*args, **kwargs)
		iserver = sconn.get_imageserver(
			kwargs.get('iserverid') or os.environ.get(SONADOR_IMAGING_SERVER))
		return iserver
	
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
								% (r.parent.pk, r.pk, err))

					# Remove the 
					try: r.delete()
					except Exception as err:
						logger.info('Unable to remove series "%s". Error:\n%s' % (r.pk, err))