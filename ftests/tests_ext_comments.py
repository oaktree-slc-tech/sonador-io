import os, posixpath, unittest, requests, logging, json, tempfile, zipfile
from io import BytesIO

from client.utils.general import first
from client.utils.object import each

from ..helpers import initenv_sonador_server
from ..servers import sonador_apitoken_fetch
from ..apisettings import SONADOR_IMAGING_SERVER, IMAGING_SERVER_RESOURCE_STUDY, IMAGING_SERVER_RESOURCE_SERIES

from ..tasks.uploads import imageserver_upload_archive
from ..test import SonadorBaseTestCase

logger = logging.getLogger(__name__)


class SonadorResourceCommentTests(SonadorBaseTestCase):
	'''	Ensure that resource comments function as expected
	'''
	def test_resource_comment_valid(self, *args, **kwargs):
		'''	Ensure that the test runner is able to connect to Sonador, upload an imaging
			series, create comments, update a comment, and remove the comment.
		'''
		# Retrieve imaging server to be used by the test
		iserver = self.getImageServer(*args, **kwargs)

		# Retrieve and upload CT data
		ctr = requests.get(
    		'https://oak-tree.tech/documents/156/example.lung-ct.volume-3d.zip')
		if not ctr.ok:
			raise ValueError('Unable to retrieve test data due to an error. Status code: %s.' % r.status_code)

		# Load file data to an archive and upload to Sonador
		hcache, _ = imageserver_upload_archive(iserver, zipfile.ZipFile(BytesIO(ctr.content)))

		# Create a comment for the series, fetch the resulting collection, update the comment, and then delete.
		for hkey, hmeta in hcache.items():

			if hkey.resource == IMAGING_SERVER_RESOURCE_SERIES:

				# Retrieve series from the server
				results = iserver.query({ hkey.header: hkey.uid }, resource=hkey.resource, rapid_lookup=True)
				self.assertEqual(len(results), 1, msg=('Unable to retrieve match for resource (%s) %s=%s' if len(results) == 0
					else 'Retrieved more than a single match for resource (%s) %s=%s') % (hkey.resource, hkey.header, hkey.uid))
				series = results[0]

				# Clear any existing comments before running tests
				each(lambda c: c.delete(), series.fetch_comments())

				# Create comment instance
				ctxt = 'Test comment: CT lung scan series downloaded by functional test runner'
				utxt = '%s (r1)' % ctxt
				series.create_comment(ctxt)
				self.assertTrue(any(ctxt == c.text for c in series.fetch_comments()), msg='Series comment did not persist to Orthanc')

				# Retrieve comment instance from server, update text, and ensure that the update was persisted
				c = first(series.fetch_comments(), key=lambda c: c.text == ctxt)
				self.assertTrue(c is not None, msg='Unable to retrieve comment instance from Sonador')
				c.update({ 'Text': utxt })
				self.assertTrue(any(utxt == c.text for c in series.fetch_comments()), msg='Comment update failed to persist to Orthanc')

				# Retrieve comment using series.get_comment server, delete the comment, and verify that it was deleted
				c = series.get_comment(c.pk)				
				c.delete()
				self.assertTrue(len(series.fetch_comments()) == 0, msg='Comment should have been removed from the series, but was not.')

		# Remove all series added to the server as part of the test
		self.cleanupImageUpload(iserver, hcache)
		