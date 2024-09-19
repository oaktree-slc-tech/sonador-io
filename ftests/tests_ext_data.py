import os, logging, traceback, requests, uuid
from time import sleep

from client import apisettings as gcapicodes
from client.errors import ClientOperationError

from ..helpers import response2filearchive, OAUTH_TOKEN_TYPE_BEARER, API_ACCESS_TOKEN
from ..test import SonadorBaseTestCase, SonadorSeriesBaseTestCase

logger = logging.getLogger(__name__)


class SonadorKafkaDataApiTests(SonadorSeriesBaseTestCase):
	'''	Test cases for Kafka data integration
	'''
	nih_cxr_testdcm = 'https://www.oak-tree.tech/documents/331/nih-cxr.patient-30775.zip'

	def test_kafka_data_export(self):
		'''	Upload sample file to Orthanc imaging server and verify Kafka data structure.
			Execute synchronous push to Kafka topic for the server.
		'''
		iserver = self.getImageServer()

		# Check if Kafka export is enabled on the server
		if not iserver.system_info().get('SonadorKafka', {}).get('Enabled'):
			self.skipTest('Kafka not enabled on imaging server=%s' % iserver.server_label)

		# Download test series
		r_cx = requests.get(self.nih_cxr_testdcm)
		if not r_cx.ok:
			raise ValueError('Unable to retrieve test data due to an error. Status code: %s' % r_cx.status_code)

		# Stage test files to imaging server
		with self.stageImageArchiveSeries(iserver, response2filearchive(r_cx)) as (test_sx, test_hcache):

			# Ensure that the test series and parents have been indexed
			sleep(0.15)
			test_sx.model_patient.index()
			test_sx.parent.index()
			test_sx.index()

			# Retrieve Kafka data for series
			kafka_series = test_sx.fetch_kafka_data()
			kafka_study = test_sx.parent.fetch_kafka_data()
			kafka_patient = test_sx.model_patient.fetch_kafka_data()

			# Validate DCM content of Kafka messages
			for _r, _k in ((test_sx, kafka_series), (test_sx.parent, kafka_study), (test_sx.model_patient, kafka_patient)):

				# Ensure that the Kafka DICOM content matches the resource object data
				self.assertEqual(
					_r.pk, _k.get('DCM', {}).get('ID'), msg='Invalid Kafka DCM message ID')
				self.assertEqual(
					_r.type, _k.get('Resource'), msg='Resource type does not match Kafka resource tag')
			
			# Retrieve instance representation for DICOM instances
			for uid in test_sx.slices:
				dcm = iserver.get_dcm_instance(uid)
				kafka_dcm = dcm.fetch_kafka_data()

				self.assertEqual(dcm.pk, kafka_dcm.get('ID'),
					msg='Instance UID and DICOM REST instance ID do not match')
				self.assertEqual(dcm.tags, kafka_dcm.get('DCM'),
					msg='Instance DICOM tags do not match server representation')

			# Trigger DICOM push
			kakfa_export_series = test_sx.kafka_export({ 'test': 'series-export' })
			self.assertEqual(kakfa_export_series.get(gcapicodes.OPCODE), 'kafka-export.series',
				msg='Series Kafka export returned wrong operation code')
			self.assertEqual(
				kakfa_export_series.get('RequestData', {}).get('test'), 'series-export',
				msg='Series Kafka export export included the wrong test code')

			kafka_export_study = test_sx.parent.kafka_export({ 'test': 'study-export' })
			self.assertEqual(kafka_export_study.get(gcapicodes.OPCODE), 'kafka-export.study',
				msg='Study Kafka export returned wrong operation code')
			self.assertEqual(
				kafka_export_study.get('RequestData', {}).get('test'), 'study-export',
				msg='Series Kafka export export included the wrong test code')
			
			kafka_export_patient = test_sx.model_patient.kafka_export({ 'test': 'patient-export' })
			self.assertEqual(kafka_export_patient.get(gcapicodes.OPCODE), 'kafka-export.patient',
				msg='Patient Kafka export returned wrong operation code')
			self.assertEqual(
				kafka_export_patient.get('RequestData', {}).get('test'), 'patient-export',
				msg='Patient Kafka export export included the wrong test code')

	def test_kafka_404(self):
		'''	Attempt to push Kafka for a non-existant resource, ensure that Orthanc returns a 404
		'''
		iserver = self.getImageServer()

		# Check if Kafka export is enabled on the server
		if not iserver.system_info().get('SonadorKafka', {}).get('Enabled'):
			self.skipTest('Kafka not enabled on imaging server=%s' % iserver.server_label)

		# Ensure that invalid Orthanc UIDs throw a 404 error
		for _url in ('/instances/%s/kafka', '/series/%s/kafka', '/studies/%s/kafka', '/patients/%s/kafka'):

			def _invalid_url():
				return iserver._request_get(iserver._request_get(
					iserver.orthanc_apiurl(_url % str(uuid.uuid4())), headers=iserver.orthanc_request_headers()))

			self.assertRaises(ClientOperationError, _invalid_url)
	