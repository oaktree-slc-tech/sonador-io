import logging, posixpath, requests
from collections import OrderedDict

from ...helpers import request_client_error, fetch_sonador_session_token
from ...serialization import json_datetime_parser
from ...servers import ImagingServerBaseObject, ImagingServerChildCollection

logger = logging.getLogger(__name__)


class OrthancJobBaseObject(ImagingServerBaseObject):
	'''	Orthanc processing job base object. Used for reporting jobs and their results.
	'''
	def __init__(self, *args, **kwargs):
		self.dicomweb = kwargs.pop('dicomweb', None)
		super(OrthancJobBaseObject, self).__init__(*args, **kwargs)


JOBS_OUTPUT_COLUMNS = OrderedDict((
		('pk', 'ID'),
		('type', 'Job Type'),
		('status', 'Job Status'),
		('ctime', 'Created'),
		('ts', 'Retrieved'),
		('runtime', 'Execution Time'),
		('progress', 'Progress'),
		('priority', 'Priority'),
	))


class OrthancJob(OrthancJobBaseObject):
	'''	Orthanc processing job
	'''
	pk_attr = 'ID'
	fetch_endpoint = '/jobs'
	tabulate_output_columns = JOBS_OUTPUT_COLUMNS

	@property
	def resource_url(self):
		return posixpath.join(self.fetch_endpoint, self.pk)

	@property
	def type(self):
		return self._objectdata.get('Type')

	@property
	def content(self):
		return self._objectdata.get('Content', {})

	@property
	def network_usage_mb(self):
		return int(self.content.get('NetworkUsageMB', 0))

	@property
	def count_received_instances(self):
		return int(self.content.get('ReceivedInstancesCount', 0))

	@property
	def ctime(self):
		return self._objectdata.get('CreationTime')

	@property
	def ts(self):
		return self._objectdata.get('Timestamp')

	@property
	def runtime(self):
		return self._objectdata.get('EffectiveRuntime')

	@property
	def error(self):
		return self._objectdata.get('ErrorCode')

	@property
	def error_code(self):
		return self.content.get('FunctionErrorCode')

	@property
	def error_description(self):
		return self._objectdata.get('ErrorDescription')

	@property
	def status(self):
		return self._objectdata.get('State')

	@property
	def priority(self):
		return self._objectdata.get('Priority')

	@property
	def progress(self):
		return self._objectdata.get('Progress')

	def dispatch_operation(self, rstub, rdata=None, headers=None, **kwargs):
		'''	Dispatch an operation for the job: cancel, pause, resume, resubmit

			@input rstub (str): Job resource stub to which the ouperation request should be sent.
			@input rdata (dict, default=new dict): JSON payload to be sent in the body of the request.
				Default is an empty dict.
			@input headers (dict, default=new dict): Headers to be sent with the request.
		'''
		rdata = rdata or {}

		# Dispatch request and parse respone
		r = requests.post(self.pacs.orthanc_apiurl(posixpath.join(self.resource_url, rstub)), json=rdata,
			headers=self.pacs.orthanc_request_headers(headers=headers))
		if not r.ok:
			request_client_error('Unable to dispatch %s update to job %s on server %s. Status code: %s' 
					% (rstub, self.pk, self.pacs.server_label, r.status_code),
				r)

		return self.server._parse_apiresponse_json(r)

	def retry(self, rdata=None, headers=None, **kwargs):
		'''	Retry/resubmit the failed job
		'''
		return self.dispatch_operation('resubmit', rdata=rdata, headers=headers, **kwargs)

	def pause(self, rdata=None, headers=None, **kwargs):
		'''	Pause execution the job
		'''
		return self.dispatch_operation('pause', rdata=rdata, headers=headers, **kwargs)

	def resume(self, rdata=None, headers=None, **kwargs):
		'''	Resume exection of the job (if paused)
		'''
		return self.dispatch_operation('resume', rdata=rdata, headers=headers, **kwargs)

	def cancel(self, rdata=None, headers=None, **kwargs):
		'''	Cancel the job
		'''
		return self.dispatch_operation('cancel', rdata=rdata, headers=headers, **kwargs)


class OrthancJobCollection(ImagingServerChildCollection):
	'''	Collection of processing jobs
	'''
	model = OrthancJob


class OrthancJobResult(OrthancJobBaseObject):
	''' Results from an Orthanc processing job
	'''
	