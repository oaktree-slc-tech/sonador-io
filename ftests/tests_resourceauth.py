import os, posixpath, unittest, requests, logging, json, tempfile, zipfile, contextlib
from io import BytesIO
from time import sleep

from client.utils.general import first
from client.utils.object import each

from ..helpers import initenv_sonador_server
from ..servers import sonador_apitoken_fetch
from ..apisettings import SONADOR_IMAGING_SERVER, IMAGING_SERVER_RESOURCE_STUDY, IMAGING_SERVER_RESOURCE_SERIES, \
	DCMHEADER_SERIES_INSTANCE_UID

from ..tasks.uploads import imageserver_upload_archive
from ..test import SonadorBaseTestCase, SonadorSeriesBaseTestCase

logger = logging.getLogger(__name__)


class SonadorResourceAuthTests(SonadorBaseTestCase):
	'''	Ensure that resource comments function as expected
	'''

    @contextlib.contextmanager
	def stageImageArchiveResource(self, iserver, afile, rapid_lookup=False, *args, **kwargs):
		'''	Stage a resource so that we can change and test it's auth rules later
		'''
		with self.stageImageArchiveTestData(iserver, afile, *args, **kwargs) as hcache:

			if len(hcache) == 0:
				raise ValueError('Unable to locate imaging series in zipfile.')

			# Iterate through items n
			sx = None
			for hkey, hmeta in hcache.items():

				if hkey.resource:

					# Retrieve sereis from the server
					results = iserver.query({ hkey.header: hkey.uid }, resource=hkey.resource, rapid_lookup=rapid_lookup)
					self.assertEqual(len(results), 1, msg=('Unable to retrieve match for resource (%s) %s=%s' if len(results) == 0
						else 'Retrieved more than a single match for resource (%s) %s=%s') % (hkey.resource, hkey.header, hkey.uid))
					sx = results[0]

					break

			if sx is None:
				raise ValueError('Unable to retrieve an imaging series from Sonador for the test.')

			yield (sx, hcache)

	def test_resource_auth_user(self, *args, **kwargs):
		'''	Ensure that the test runner is able to connect to Sonador, upload an imaging
			series, create auth rules and update auth rules for a user
            -This test assumes that the test userUID is hardcoded with an env var or something similiar
		'''
        #Didn't know if the iserver object has the orthanc URL in it so I grab it from env vars
        orthanchost = os.environ.get('SONADOR_IMAGING_SERVER_HOSTNAME')
        orthancport = os.environ.get('SONADOR_IMAGING_SERVER_PORT')
        orthancscheme = os.environ.get('SONADOR_IMAGING_SERVER_SCHEME')
        orthancurl = orthancscheme  + orthanchost + ':' + orthancport


		# Retrieve imaging server to be used by the test
		iserver = self.getImageServer(*args, **kwargs)

		# Retrieve and upload CT data
		ctr = requests.get(
    		'https://oak-tree.tech/documents/156/example.lung-ct.volume-3d.zip')
		if not ctr.ok:
			raise ValueError('Unable to retrieve test data due to an error. Status code: %s.' % r.status_code)

        userUID = 1
        orthanchost
		# Temporarily stage data to Sonador to run the test
		with self.stageImageArchiveResource(iserver, zipfile.ZipFile(BytesIO(ctr.content))) as (resource, hcache):

            ruid = resource.uid

            #Send post request to Orthanc to add permissions to user
            authendpoint = "/auth/series/user/%s" % (ruid)
            orthanc_auth_url = orthancurl + authendpoint

            payload = {
        	'user': userUID,
            'query': 'query'
        	 }
            # payload = json.loads(payload)

            post = requests.post(orthanc_auth_url, payload)

            if post[status_code] = '404'
                error = json.dumps(post)
                raise ValueError('Post request to modify user auth permissions failed: ERROR = %s' % error)

            if post[status_code] = '201':

                get_request = requests.get(orthanc_auth_url)

                if get_request['query'] == userUID:
                    response_string = json.dumps(get_request)
                    print("Test passed and user now has permissions for uploaded resource: RESPONSE = %s" % response_string)
                else:
                    raise ValueError('GET request to validate user has permissions failed for user %s' % useruid)
