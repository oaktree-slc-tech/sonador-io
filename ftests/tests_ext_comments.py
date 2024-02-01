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


class SonadorResourceCommentTests(SonadorSeriesBaseTestCase):
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

		# Temporarily stage data to Sonador to run the test
		with self.stageImageArchiveSeries(iserver, zipfile.ZipFile(BytesIO(ctr.content))) as (series, hcache):

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
	
	def test_resource_comment_dicomweb(self, *args, **kwargs):
		'''	Ensure that the test runner is able to connect to Sonador, upload an imaging series, create
			comments via the DICOMweb interface, update a comment via the DICOmweb interface, and remove
			the comment via the DICOmweb interface.
		'''
		# Retrieve the imaging server to be used by the test
		iserver = self.getImageServer(*args, **kwargs)

		# Retrieve and upload MRI data
		ctr = requests.get('https://www.oak-tree.tech/documents/331/nih-cxr.patient-30775.zip')
		if not ctr.ok:
			raise ValueError('Unable to retrieve test data due to an error. Status code: %s.' % r.status_code)

		# Temporarily stage data to Sonador to run the test
		with self.stageImageArchiveSeries(iserver, zipfile.ZipFile(BytesIO(ctr.content)), rapid_lookup=False) as (series, hcache):

			# Allow time for CXR instance to index
			sleep(0.25)

			# Clear any existing comments before running tests
			each(lambda c: c.delete(), series.fetch_comments())

			# Create comment instance
			ctxt = 'Test comment: chest x-ray series uploaded by functional test runner. Series-UID="%s"' % series.series_uid
			utxt = '%s (r1)' % ctxt
			c0 = series.create_comment(ctxt, dicomweb_api=True, cache_response=True)
			_comments0 = series.fetch_comments(dicomweb_api=True, cache_response=True)

			# Ensure that the API request was sent via the DICOMweb API
			self.assertTrue(
				getattr(c0, 'http_response', None) is not None and iserver.dicomweb_root in c0.http_response.url,
				msg='Creation request not sent to DICOMweb API endpoint')
			self.assertTrue(
				getattr(_comments0, 'http_response', None) is not None and iserver.dicomweb_root in _comments0.http_response.url,
				msg='Fetch comments request not sent to DICOMweb API endpoint')

			# Ensure that the comment series was created correctly
			self.assertTrue(
				any(ctxt == _c.text and _c._objectdata.get(DCMHEADER_SERIES_INSTANCE_UID) == series.series_uid for _c in _comments0), 
				msg='Series comment did not persist to Orthanc')

			# Retrieve comment via API call
			c1 = series.get_comment(c0.pk, dicomweb_api=True, cache_response=True)
			self.assertTrue(getattr(c1, 'http_response', None) is not None and iserver.dicomweb_root in c1.http_response.url,
				msg='Fetch model instance not sent to DICOMweb endpoint')
			self.assertTrue(ctxt == c1.text, msg='Commment instance retrieved via DICOMweb API does not match text')

			# Update comment, ensure that the request was sent to the DICOMweb API, and was committed successfully.
			r_update = c1.update({ 'Text': utxt }, cache_response=True)
			self.assertTrue(r_update.response.status_code == 200 and iserver.dicomweb_root in r_update.response.url,
				msg='Update request did not complete successfully or was sent to the wrong endpoint')

			# Fetch updated comment and ensure it contains the revised text
			c2 = series.get_comment(c0.pk, dicomweb_api=True, cache_response=True)
			self.assertTrue(c2.text == utxt and iserver.dicomweb_root in c2.http_response.url,
				msg='Updated text not persisted to Orthanc.')

			# Remove comment from server
			r_del = c2.delete()
			self.assertTrue(len(series.fetch_comments(dicomweb_api=True)) == 0 and iserver.dicomweb_root in r_del.url,
				msg='Comment should have been removed from the series, but was not.')
