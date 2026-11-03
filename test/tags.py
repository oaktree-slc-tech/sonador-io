'''	Tests of the Series Tag model which need no server connection: the `Meta` document write and
	the readback that follows it.

	Run from the directory containing the `sonador` package: python -m unittest sonador.test.tags
'''
import unittest

from sonador.servers import SonadorServer
from sonador.servers.auth import SonadorGroup
from sonador.imaging.orthanc.ext import readback_request_options
from sonador.imaging.orthanc.group import OrthancGroup
from sonador.imaging.orthanc.tags import Tag, TagCollection

from .worklists import IMAGESERVER_JSON, FakeResponse

TAG_JSON = {'ID': 'tag-1', 'Value': 'T-1', 'Meaning': 'Tag one', 'SchemeDesignator': 'SONADOR', 'Meta': {'DisplayAttributes': {'Color': {'Value': '#112233'}}}}


class ReadbackRequestOptionsTestCase(unittest.TestCase):
	'''	Which options of a write are repeated on the readback
	'''
	def test_transport_options_are_kept(self):
		options = {'verify': 'test-ca.pem', 'headers': {'X': '1'}, 'rkwargs': {'timeout': 5, 'cert': 'client.pem'}, 'cache_response': True}
		self.assertEqual(readback_request_options(options),
			{'verify': 'test-ca.pem', 'headers': {'X': '1'}, 'rkwargs': {'timeout': 5, 'cert': 'client.pem'}})

	def test_payload_options_stay_with_the_write(self):
		self.assertEqual(readback_request_options({'rkwargs': {'timeout': 5, 'json': {'x': 1}, 'data': b'', 'files': {}}}),
			{'rkwargs': {'timeout': 5}})

	def test_rkwargs_entries_the_write_overrides_are_dropped(self):
		'''	`verify` / `headers` inside `rkwargs` never reach the server as given (the write sets both
			itself); the readback must not receive them a second time
		'''
		self.assertEqual(readback_request_options({'verify': False, 'rkwargs': {'verify': True, 'headers': {'X': '1'}, 'timeout': 5}}),
			{'verify': False, 'rkwargs': {'timeout': 5}})

	def test_defaults_are_left_to_the_fetch(self):
		self.assertEqual(readback_request_options({}), {})
		self.assertEqual(readback_request_options({'verify': None, 'rkwargs': None}), {})


class TagMetaReadbackTestCase(unittest.TestCase):
	'''	`Tag.update_meta` writes the document and reads the stored (normalized) document back, with
		both requests sent the way the caller asked
	'''
	def setUp(self):
		# The connection verifies SSL by default, so a per-call setting that differs from it is visible
		self.server = SonadorServer('https://sonador.invalid', apitoken='token', verify=True)
		self.iserver = self.server.imageserver_modelinstance_from_json(dict(IMAGESERVER_JSON))
		self.group = OrthancGroup(self.iserver, SonadorGroup(self.server, {'id': 7, 'name': 'Group'}))
		self.tag = Tag(self.server, dict(TAG_JSON), parent=self.group)
		self.stored = dict(TAG_JSON)
		self.requests = []
		self.iserver._request_put = self._request_put
		self.iserver._request_get = self._request_get

	def _request_put(self, resource_endpoint, error_msg=None, headers=None, verify=None, json=None, **kwargs):
		'''	Simulated server write: stores the document with its colours in canonical upper case
		'''
		self.requests.append(('PUT', resource_endpoint, verify, dict(kwargs, headers=dict(headers or {}))))
		self.stored = dict(TAG_JSON)
		del self.stored['Meta']
		if json.get('Meta'):
			colors = json['Meta'].get('DisplayAttributes', {}).get('Color', {})
			self.stored['Meta'] = {'DisplayAttributes': {'Color': dict((k, v.upper()) for k, v in colors.items())}}
		return FakeResponse(self.stored, url=resource_endpoint)

	def _request_get(self, resource_endpoint, error_msg=None, headers=None, verify=None, **kwargs):
		self.requests.append(('GET', resource_endpoint, verify, dict(kwargs, headers=dict(headers or {}))))
		return FakeResponse(self.stored, url=resource_endpoint)

	def assert_headers(self, wkwargs, rkwargs, **expected):
		'''	Both requests carry the expected headers plus the same Orthanc authorization header
		'''
		for kwargs in (wkwargs, rkwargs):
			self.assertIn('Authorization', kwargs['headers'])
			for name, value in expected.items():
				self.assertEqual(kwargs['headers'].get(name), value, kwargs['headers'])
		self.assertEqual(wkwargs['headers']['Authorization'], rkwargs['headers']['Authorization'])

	def test_readback_uses_the_request_settings_of_the_write(self):
		self.tag.update_meta({'DisplayAttributes': {'Color': {'Value': '#445566'}}}, verify='test-ca.pem', rkwargs={'timeout': 5})

		(wmethod, wurl, wverify, wkwargs), (rmethod, rurl, rverify, rkwargs) = self.requests
		self.assertEqual((wmethod, rmethod), ('PUT', 'GET'))
		self.assertTrue(wurl.endswith('/groups/7/tags/tag-1'), wurl)
		self.assertEqual(rurl, wurl)
		self.assertEqual((wverify, rverify), ('test-ca.pem', 'test-ca.pem'))
		self.assertEqual((wkwargs.get('timeout'), rkwargs.get('timeout')), (5, 5))
		self.assertNotIn('json', rkwargs)

		# The instance holds what the server stored
		self.assertEqual(self.tag.display_color, {'Value': '#445566'})
		self.assertEqual(self.tag.meta, {'DisplayAttributes': {'Color': {'Value': '#445566'}}})

	def test_readback_carries_the_caller_headers(self):
		'''	A header the endpoint requires (e.g. a proxy route) is sent on the write and on the readback,
			beneath the Orthanc authorization header
		'''
		self.tag.update_meta({'DisplayAttributes': {'Color': {'Value': '#445566'}}},
			headers={'X-Test-Route': 'synthetic-route', 'Authorization': 'Bearer caller'})

		(_, _, wverify, wkwargs), (_, _, rverify, rkwargs) = self.requests
		self.assert_headers(wkwargs, rkwargs, **{'X-Test-Route': 'synthetic-route'})
		self.assertNotEqual(rkwargs['headers']['Authorization'], 'Bearer caller')
		self.assertEqual((wverify, rverify), (True, True))
		self.assertEqual(self.tag.display_color, {'Value': '#445566'})

	def test_rkwargs_with_verify_or_headers_do_not_break_the_readback(self):
		'''	A reusable options dictionary may repeat `verify` / `headers` inside `rkwargs`; the write
			accepts it (and applies its own values), so the readback must as well
		'''
		self.tag.update_meta({'DisplayAttributes': {'Color': {'Value': '#445566'}}},
			verify=False, headers={'X-Test-Route': 'synthetic-route'}, rkwargs={'verify': True, 'headers': {'X-Ignored': '1'}, 'timeout': 5})

		(_, _, wverify, wkwargs), (_, _, rverify, rkwargs) = self.requests
		self.assertEqual((wverify, rverify), (False, False))
		self.assertEqual((wkwargs.get('timeout'), rkwargs.get('timeout')), (5, 5))
		self.assert_headers(wkwargs, rkwargs, **{'X-Test-Route': 'synthetic-route'})
		self.assertNotIn('X-Ignored', rkwargs['headers'])
		self.assertEqual(self.tag.display_color, {'Value': '#445566'})

	def test_fetch_modelinstance_accepts_headers(self):
		'''	The collection fetch of a single tag merges caller headers into the request and does not pass
			them to the model constructor
		'''
		tag = TagCollection.fetch_modelinstance(self.group, 'tag-1', headers={'X-Test-Route': 'synthetic-route'}, verify='test-ca.pem')
		self.assertIsInstance(tag, Tag)
		self.assertEqual((tag.pk, tag.display_color), ('tag-1', {'Value': '#112233'}))
		(method, url, verify, kwargs), = self.requests
		self.assertEqual((method, verify), ('GET', 'test-ca.pem'))
		self.assertTrue(url.endswith('/groups/7/tags/tag-1'), url)
		self.assertEqual(kwargs['headers'].get('X-Test-Route'), 'synthetic-route')
		self.assertIn('Authorization', kwargs['headers'])

	def test_normalized_document_is_adopted(self):
		self.tag.update_meta({'DisplayAttributes': {'Color': {'Value': '#aabbcc', 'Meaning': '#c0c7f8'}}})
		self.assertEqual(self.tag.display_color, {'Value': '#AABBCC', 'Meaning': '#C0C7F8'})
		self.assertEqual((self.tag.value, self.tag.meaning, self.tag.scheme), ('T-1', 'Tag one', 'SONADOR'))

	def test_defaults_follow_the_connection(self):
		self.tag.update_meta(None)
		self.assertEqual([(method, verify) for method, _, verify, _ in self.requests], [('PUT', True), ('GET', True)])
		self.assertEqual((self.tag.meta, self.tag.display_color), ({}, {}))


if __name__ == '__main__':
	unittest.main()
