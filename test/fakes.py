'''	Stand-ins used by the offline tests under `ftests`: a minimal requests.Response and the JSON of an
	imaging server record as the Sonador API returns it. No server connection is made.
'''
import json
from types import SimpleNamespace


IMAGESERVER_JSON = {
	'pk': 'srv-1', 'name': 'Test PACS', 'hostname': 'pacs.invalid', 'scheme': 'https', 'port': 443,
	'internal_hostname': 'pacs.internal.invalid', 'internal_scheme': 'http', 'internal_port': 8042,
	'dicomweb_root': '/dicom-web', 'wado_root': '/wado', 'token': 'abc', 'active': True, 'default': True,
	'description': '', 'image_rendering_method': '', 'thumbnail_rendering_method': '', 'qido_supports_include': False,
}


class FakeResponse:
	'''	Minimal stand-in for requests.Response
	'''
	def __init__(self, data, url='https://pacs.invalid/x'):
		self._data = data
		self.status_code = 200
		self.ok = True
		self.headers = {'Content-Type': 'application/json'}
		self.text = json.dumps(data)
		self.content = self.text.encode()
		self.request = SimpleNamespace(url=url)

	def json(self, **kwargs):
		return json.loads(self.text, **kwargs)
