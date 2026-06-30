import os, requests, logging, json, tempfile, zipfile
from io import BytesIO
from unittest.mock import patch

from client.utils.general import create_token

from sonador.imaging.orthanc.base import ImagingSeriesCollection

from ..helpers import OAUTH_ACCESS_TOKEN, OAUTH_TOKEN_TYPE
from ..servers import SonadorImagingServerCollection

from ..apisettings import (
    SONADOR_IMAGING_SERVER,
    SONADOR_ACCESS_ID,
    SONADOR_SECRET_KEY,
    SONADOR_URL,
    SONADOR_APITOKEN,
    SONADOR_INTERNAL_DNS,
)

from ..tasks.uploads import imageserver_upload_archive
from ..test import SonadorBaseTestCase

logger = logging.getLogger(__name__)


# Create copies of environment variables in order to contorl
# the environment the test executes in.
ENV_VARS = {
    SONADOR_URL: os.environ.get(SONADOR_URL),
    SONADOR_INTERNAL_DNS: os.environ.get(SONADOR_INTERNAL_DNS),
    SONADOR_IMAGING_SERVER: os.environ.get(SONADOR_IMAGING_SERVER),
}

API_TOKEN_ENV_VARS = ENV_VARS.copy()
API_TOKEN_ENV_VARS.update({SONADOR_APITOKEN: os.environ.get(SONADOR_APITOKEN)})

HMAC_ENV_VARS = ENV_VARS.copy()
HMAC_ENV_VARS.update(
    {
        SONADOR_ACCESS_ID: os.environ.get(SONADOR_ACCESS_ID),
        SONADOR_SECRET_KEY: os.environ.get(SONADOR_SECRET_KEY),
    }
)


class SonadorAuthenticationTests(SonadorBaseTestCase):
    """ Sonador authentication tests:

        1. Sonador API access ID and secret key (HMAC-SHA1 signature based access)
        2. Sonador API token (permanent)
        3. Sonador session token: retrieved via API call to token grant endpoint and time limited
    """
    def test_sonador_valid_api_token_servers_list(self, *args, **kwargs):
        """ Ensure that the test runner is able to retrieve a list of servers with valid API Token
        """
        self.assertTrue(API_TOKEN_ENV_VARS[SONADOR_APITOKEN] is not None,
            msg='No token provided as part of the environment.')

        # Retrieve sonador connection from env: provide random values for access ID and secret to prevent
        # those credentials from being used in the test.
        sconn = self.getSonadorConnection(*args, **kwargs)
        iservers = sconn.fetch_imageservers()

        self.assertTrue(len(iservers) > 0, msg='Server list from Sonador is empty.')
        self.assertEqual(type(iservers), SonadorImagingServerCollection, 
            msg='Unexpected server collection type')

    def test_sonador_invalid_api_token_servers_list(self, *args, **kwargs):
        """ Ensure that with bad API token test runner is receiving 401/403 when retrieving a list of servers
        """
        # Attempt to retrieve Sonador connection with bad credentials. create_token generates a random
        # value for use as the API token, access ID, and secret key.
        sconn = self.getSonadorConnection(
            *args, apitoken=create_token(), access_id=create_token(), secret_key=create_token(), **kwargs)
        try: iservers = sconn.fetch_imageservers()
        except Exception as err:
            self.assertTrue(err.http_code == 401 or err.http_code == 403)
        else:
            self.fail('Test runner able to retrieve list of servers with bad API token.')

    @patch.dict(os.environ, { SONADOR_APITOKEN: '' })
    def test_sonador_valid_hmac_sha1_servers_list(self, *args, **kwargs):
        """ Ensure that the test runner is able to retrieve a list of servers with valid HMAC-SHA1
        """
        # Check that an access ID and secret key were provided via environment variables
        self.assertTrue(os.environ.get(SONADOR_ACCESS_ID) is not None,
            msg='No acess ID provided as part of test environment.')
        self.assertTrue(os.environ.get(SONADOR_SECRET_KEY) is not None,
            msg='No secrey key provided as part test environment.')

        # Ensure that the API token variable is None for the test (as API token takes precendence if present)
        self.assertTrue(os.environ.get(SONADOR_APITOKEN) in (None, ''),
            msg='API token value found in test environment (value="%s", expected None.' % os.environ.get(SONADOR_APITOKEN))

        # Retrieve sonador connection from env: set the token environment variable to be None for the test
        sconn = self.getSonadorConnection(*args, **kwargs)
        iservers = sconn.fetch_imageservers()

        self.assertTrue(len(iservers) > 0, msg='Server list from Sonador is empty.')
        self.assertEqual(type(iservers), SonadorImagingServerCollection, msg='Unexpected server collection type')

    @patch.dict(os.environ, { SONADOR_APITOKEN: '' })
    def test_sonador_invalid_hmac_sha1_servers_list(self, *args, **kwargs):
        """ Ensure that with bad HMAC-SHA1 test runner is receiving 401/403 when retrieving a list of servers
        """
         # Ensure that the API token variable is None for the test (as API token takes precendence if present)
        self.assertTrue(os.environ.get(SONADOR_APITOKEN) in (None, ''),
            msg='API token value found in test environment (value="%s", expected None.' % os.environ.get(SONADOR_APITOKEN))

        # Retrieve sonador connectiong from env with bad credentials
        sconn = self.getSonadorConnection(access_id=create_token(), secret_key=create_token(), *args, **kwargs)

        try: iservers = sconn.fetch_imageservers()
        except Exception as err:
            if not hasattr(err, "http_code"):
                raise err
            self.assertTrue(err.http_code == 401 or err.http_code == 403)
        else:
            self.fail('Test runner able to retrieve list of servers with bad access ID and secret')

    @patch.dict(os.environ, { SONADOR_APITOKEN: '' })
    def test_valid_session_token_query(self, *args, **kwargs):
        """ Ensure that the test runner is able  to retrieve a session token and use session
            token to retrieve data from API using a valid HMAC-SHA1. Session tokens are issued 
            when trying to access Orthanc resources if a permanent API token is not provided.
        """
        # Ensure that the API token variable is None (as it will take precendence if present)
        # and prevent the issue of a session token.
        self.assertTrue(os.environ.get(SONADOR_APITOKEN) in (None, ''),
            msg='API token value found in test environment (value="%s", expected None.' % os.environ.get(SONADOR_APITOKEN))
        # Check that an access ID and secret key were provided via environment variables
        self.assertTrue(os.environ.get(SONADOR_ACCESS_ID) is not None,
            msg='No acess ID provided as part of test environment.')
        self.assertTrue(os.environ.get(SONADOR_SECRET_KEY) is not None,
            msg='No secret key provided as part test environment.')

        # Retrieve Sonador connection from env and retrieve imaging server
        sconn = self.getSonadorConnection(*args, **kwargs)
        iserver = sconn.get_imageserver(os.environ.get(SONADOR_IMAGING_SERVER))

        # Retrieve sesison token
        session_token = sconn.get_session_token()

        # Patch environment variables and clear all credentials (except the session token)
        with patch.dict(os.environ, dict((k,'') for k in ENV_VARS)):
            
            # Clear access ID and secret so it is not possible for the server
            # to issue a second token.
            iserver.server.access_id = None
            iserver.server.secret_key = None
            iserver.server._apitoken = session_token[OAUTH_ACCESS_TOKEN]
            iserver.server.apitoken_type = session_token[OAUTH_TOKEN_TYPE]

            # Fetching images from the server
            try: image_collection = iserver.query({ "PatientName": "*" }, limit=10)
            except Exception as err:
                self.fail('Unable to retrieve imaging resources from server due to an error. Error:\n%s' % err)

            # Ensure that the results are an imaging series collection
            self.assertEqual(type(image_collection), ImagingSeriesCollection)            

    @patch.dict(os.environ, { SONADOR_APITOKEN: '' })
    def test_invalid_session_token_query(self, *args, **kwargs):
        """ Ensure that with bad Session token test runner is receiving 401/403 when querying images
        """
        # Ensure that the API token variable is None (as it will take precendence if present)
        # and prevent the issue of a session token.
        self.assertTrue(os.environ.get(SONADOR_APITOKEN) in (None, ''),
            msg='API token value found in test environment (value="%s", expected None.' % os.environ.get(SONADOR_APITOKEN))
        
        # Check that an access ID and secret key were provided via environment variables
        # in order to fetch the session token.
        self.assertTrue(os.environ.get(SONADOR_ACCESS_ID) is not None,
            msg='No acess ID provided as part of test environment.')
        self.assertTrue(os.environ.get(SONADOR_SECRET_KEY) is not None,
            msg='No secrey key provided as part test environment.')

        # Retrieving Sonador connection from env and image server
        sconn = self.getSonadorConnection(*args, **kwargs)
        iserver = sconn.get_imageserver(os.environ.get(SONADOR_IMAGING_SERVER))

        # Retrieve session token and then set the value to an invalid value
        session_token = sconn.get_session_token()
        session_token[OAUTH_ACCESS_TOKEN] = "badtoken"

        # Isolate environment without including access_id and secret_key to prevent
        # issue of a second token.
        with patch.dict(os.environ, dict((k,'') for k in ENV_VARS)):
            
            # Setting access_id and secket_key as None, for testing connection only with session token
            iserver.server.access_id = None
            iserver.server.secret_key = None
            iserver.server._apitoken = session_token[OAUTH_ACCESS_TOKEN]
            iserver.server.apitoken_type = session_token[OAUTH_TOKEN_TYPE]
            
            # Fetching images from the server
            try:
                iserver.query({"PatientName": "*"}, limit=10)
                self.fail('Query to image server %s successful with bad token. Expected error/403 response.' % iserver.pk)
            except Exception as err:

                # Ensure that the error instance includes an HTTP code property
                if not hasattr(err, "http_code"):
                    self.fail('Unexpected error, expecting an error which includes an http_code property.')

                # Ensure that the repsonse code is 401 or 043
                self.assertTrue(err.http_code == 401 or err.http_code == 403)
            else:
                self.fail('Test runner able to query images with invalid session token.')      

    @patch.dict(os.environ, { SONADOR_APITOKEN: '' })
    def test_valid_session_token_upload(self, *args, **kwargs):
        """ Ensure that the test runner is able to upload images to Orthanc and then query
            for the corresponding studies/series with a session token
        """
        # Ensure that the API token variable is None (as it will take precendence if present)
        # and prevent the issue of a session token.
        self.assertTrue(os.environ.get(SONADOR_APITOKEN) in (None, ''),
            msg='API token value found in test environment (value="%s", expected None.' % os.environ.get(SONADOR_APITOKEN))
        
        # Check that an access ID and secret key were provided via environment variables
        # in order to fetch the session token.
        self.assertTrue(os.environ.get(SONADOR_ACCESS_ID) is not None,
            msg='No acess ID provided as part of test environment.')
        self.assertTrue(os.environ.get(SONADOR_SECRET_KEY) is not None,
            msg='No secrey key provided as part test environment.')

        # Retrieve Sonador connection from env and fetch image server reference
        sconn = self.getSonadorConnection(*args, **kwargs)
        iserver = sconn.get_imageserver(os.environ.get(SONADOR_IMAGING_SERVER))

        # Fetch session token for use in requests
        session_token = sconn.get_session_token()

        # Isolate environment without including access_id and secret_key to prevent
        # issue of a second token.
        with patch.dict(os.environ, dict((k,'') for k in ENV_VARS)):

            # Set access_id and secket_key as None, for testing connection only with session token
            iserver.server.access_id = None
            iserver.server.secret_key = None
            iserver.server._apitoken = session_token[OAUTH_ACCESS_TOKEN]
            iserver.server.apitoken_type = session_token[OAUTH_TOKEN_TYPE]

            # Retrieve CT data
            ctr = self.fetchTestResource("https://oak-tree.tech/documents/156/example.lung-ct.volume-3d.zip")
            
            # Load file data to an archive and upload to Sonador
            hcache, _ = imageserver_upload_archive(iserver, zipfile.ZipFile(BytesIO(ctr.content)))

            # Check the Orthanc instance to ensure that the image was indexed correctly
            for hkey, hmeta in hcache.items():
                results = iserver.query({ hkey.header: hkey.uid }, resource=hkey.resource, rapid_lookup=False)
                self.assertEqual(len(results), 1,
                    msg="Retrieved more than a single match for resource (%s) %s=%s." % (hkey.resource, hkey.header, hkey.uid))

            # Remove all series added to the server as part of the test
            self.cleanupImageUpload(iserver, hcache)

    @patch.dict(os.environ, { SONADOR_APITOKEN: '' })
    def test_invalid_session_token_upload(self, *args, **kwargs):
        """ Ensure that with bad Session token test runner is receiving 401/403 when uploading images
        """
        # Ensure that the API token variable is None (as it will take precendence if present)
        # and prevent the issue of a session token.
        self.assertTrue(os.environ.get(SONADOR_APITOKEN) in (None, ''),
            msg='API token value found in test environment (value="%s", expected None.' % os.environ.get(SONADOR_APITOKEN))
        
        # Check that an access ID and secret key were provided via environment variables
        # in order to fetch the session token.
        self.assertTrue(os.environ.get(SONADOR_ACCESS_ID) is not None,
            msg='No acess ID provided as part of test environment.')
        self.assertTrue(os.environ.get(SONADOR_SECRET_KEY) is not None,
            msg='No secrey key provided as part test environment.')

        # Retrieving Sonador connection from env
        sconn = self.getSonadorConnection(*args, **kwargs)
        iserver = sconn.get_imageserver(os.environ.get(SONADOR_IMAGING_SERVER))

        # Retrieve session token and then set the value to an invalid value
        session_token = sconn.get_session_token()
        session_token[OAUTH_ACCESS_TOKEN] = "badtoken"

        # Isolating environment without including access_id and secret_key
        with patch.dict(os.environ, dict((k,'') for k in ENV_VARS)):
            
            # Set access_id and secket_key as None, for testing connection only with session token
            iserver.server.access_id = None
            iserver.server.secret_key = None
            iserver.server._apitoken = session_token[OAUTH_ACCESS_TOKEN]
            iserver.server.apitoken_type = session_token[OAUTH_TOKEN_TYPE]

            # Retrieve CT data
            ctr = self.fetchTestResource("https://oak-tree.tech/documents/156/example.lung-ct.volume-3d.zip")
            
            # Load file data to an archive and upload to Sonador
            try:
                hcache, _ = imageserver_upload_archive(iserver, zipfile.ZipFile(BytesIO(ctr.content)))
                self.fail('Upload of image archive successful with invalid token.')
            
            except Exception as err:

                # Ensure that the error instance includes an HTTP code property
                if not hasattr(err, "http_code"):
                    raise err

                # Ensure that the repsonse code is 401 or 043
                self.assertTrue(err.http_code == 401 or err.http_code == 403)
            else:
                raise Exception("Test runner was able to upload images with bad session token.")
