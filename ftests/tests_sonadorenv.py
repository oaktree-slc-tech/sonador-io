import os, posixpath, unittest, requests, logging, json, tempfile, zipfile
from io import BytesIO
from time import sleep

from ..helpers import initenv_sonador_server, response2filearchive
from ..servers import sonador_apitoken_fetch
from ..apisettings import SONADOR_IMAGING_SERVER, IMAGING_SERVER_RESOURCE_STUDY, IMAGING_SERVER_RESOURCE_SERIES

from ..tasks.uploads import imageserver_upload_archive
from ..test import SonadorBaseTestCase

logger = logging.getLogger(__name__)


class SonadorEnvironmentTests(SonadorBaseTestCase):
	'''	Ensure that the Sonador environment is configured correctly.

		1.	Ensure that the test runner is able to resolve the hostname for the 
			Sonador web application and that the provided credentials are valid.
		2.	Ensure that the imaging server reference provided exists.
	'''
	
	def testenv_sonador_connection(self, *args, **kwargs):
		'''	Ensure that the test runner is able to connect to Sonador and retrieve
			resource lists with the provided credentials.
		'''
		sconn = initenv_sonador_server(*args, **kwargs)
		with tempfile.TemporaryFile(mode='w') as tfile:
			logger.info('\nSonador connection test output')
			sonador_apitoken_fetch(sconn, tfile, verify=False)

	def testenv_sonador_iserver(self, *args, **kwargs):
		'''	Check that imaging server exists
		'''	
		iserver = self.getImageServer(*args, **kwargs)

	def testenv_sonador_fileupload(self, *args, **kwargs):
		'''	Ensure that the imaging server can process a file upload and includes
			entries for all studies and series that were included in the original archive.
		'''
		# Retrieve imaging server to be used by the test
		iserver = self.getImageServer(*args, **kwargs)

		# Retrieve CT data
		ctr = requests.get(
    		'https://oak-tree.tech/documents/156/example.lung-ct.volume-3d.zip')
		if not ctr.ok:
			raise ValueError('Unable to retrieve test data due to an error. Status code: %s.' % r.status_code)

		# Load file data to an archive and upload to Sonador
		hcache, _ = imageserver_upload_archive(iserver, response2filearchive(ctr))

		# Check the Orthanc instance to ensure that the image was indexed correctly
		for hkey, hmeta in hcache.items():			
			
			# Query Orthanc DB
			results = iserver.query({ hkey.header: hkey.uid }, resource=hkey.resource)
			self.assertEqual(len(results), 1, msg=('Unable to retrieve match for resource (%s) %s=%s' if len(results) == 0
				else 'Retrieved more than a single match for resource (%s) %s=%s') % (hkey.resource, hkey.header, hkey.uid))

			# Query Sonador Resource Cache
			results = iserver.query({ hkey.header: hkey.uid }, resource=hkey.resource, rapid_lookup=True)
			self.assertEqual(len(results), 1, msg=('Unable to retrieve match for resource (%s) %s=%s' if len(results) == 0
				else 'Retrieved more than a single match for resource (%s) %s=%s') % (hkey.resource, hkey.header, hkey.uid))
		
		# Remove all series added to the server as part of the test, add pause to allow for series to clear from the cache
		self.cleanupImageUpload(iserver, hcache)
		sleep(0.25)

		# Query the cache to ensure that the resources were removed
		for hkey, hmeta in hcache.items():

			results = iserver.query({ hkey.header: hkey.uid }, resource=hkey.resource, rapid_lookup=True)
			self.assertEqual(len(results), 0, msg='Resource (%s) %s=%s remains in cache after being removed from DB'
				% (hkey.resource, hkey.header, hkey.uid))