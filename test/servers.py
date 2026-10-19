'''	Tests of the Sonador server models which need no server connection.

	Run from the directory containing the `sonador` package: python -m unittest sonador.test.servers
'''
import unittest
from types import SimpleNamespace

from highdicom.sr import CodedConcept

from sonador.servers import SonadorImagingServer
from sonador.servers.auth import SonadorGroup, SonadorGroupCollection
from sonador.imaging.orthanc.group import OrthancGroup


class ImagingServerLabelTestCase(unittest.TestCase):
	'''	`server_label` is what applications show for an imaging server
	'''
	def label(self, **attrs):
		return SonadorImagingServer.server_label.fget(SimpleNamespace(**attrs))

	def test_label_uses_the_name(self):
		self.assertEqual(self.label(name='Playground PACS', pk='SU28jV4OoVuOAGkB'), 'Playground PACS (SU28jV4OoVuOAGkB)')

	def test_label_falls_back_to_the_key(self):
		self.assertEqual(self.label(name='', pk='SU28jV4OoVuOAGkB'), 'SU28jV4OoVuOAGkB')
		self.assertEqual(self.label(name=None, pk='k'), 'k')


class RecordingCollection:
	'''	Stand-in for the tag / display attribute collections: records the calls of the class methods
		the Orthanc group proxy makes instead of sending requests
	'''
	calls = []

	@classmethod
	def fetch(cls, parent, *args, **kwargs):
		cls.calls.append(('fetch', parent, args, kwargs))
		return 'fetched'

	@classmethod
	def create(cls, parent, data, **kwargs):
		cls.calls.append(('create', parent, data, kwargs))
		return 'created'

	@classmethod
	def fetch_modelinstance(cls, parent, uid, *args, **kwargs):
		cls.calls.append(('get', parent, (uid,) + args, kwargs))
		return 'instance'


class TagRecorder(RecordingCollection): calls = []
class DisplayAttributeRecorder(RecordingCollection): calls = []


class RecordingOrthancGroup(OrthancGroup):
	'''	Orthanc group proxy whose collections record instead of request
	'''
	@property
	def tags_modelcollection_class(self): return TagRecorder

	@property
	def display_attributes_modelcollection_class(self): return DisplayAttributeRecorder


class CustomGroup(SonadorGroup): pass
class CustomGroupCollection(SonadorGroupCollection): model = CustomGroup


class OrthancGroupProxyTestCase(unittest.TestCase):
	'''	The tag and display attribute methods of the imaging server share the Orthanc group proxy
		setup (`_orthanc_group_for`): group validation, model overrides and argument forwarding
	'''
	def setUp(self):
		self.sonador = SimpleNamespace(group_datacollection_class=SonadorGroupCollection)
		self.pacs = SonadorImagingServer(self.sonador, {'id': 'pacs-1', 'name': 'PACS'})
		self.group = SonadorGroup(self.sonador, {'id': 7, 'name': 'Group'})
		self.concept = CodedConcept('T-1', 'SONADOR', 'Tag one')
		del TagRecorder.calls[:]
		del DisplayAttributeRecorder.calls[:]
		# Proxy class override for every call (the default proxy would send requests)
		self.options = {'orthanc_group_datamodel_class': RecordingOrthancGroup, 'verify': False, 'headers': {'X': '1'}}

	def assert_proxy(self, parent):
		self.assertIsInstance(parent, RecordingOrthancGroup)
		self.assertIs(parent.pacs, self.pacs)
		self.assertIs(parent.group, self.group)
		self.assertEqual((parent.tags_url, parent.display_attributes_url), ('/groups/7/tags', '/groups/7/display-attributes'))

	def test_tags(self):
		self.assertEqual(self.pacs.create_tag(self.group, self.concept, **self.options), 'created')
		self.assertEqual(self.pacs.fetch_tags(self.group, page=2, **self.options), 'fetched')
		self.assertEqual(self.pacs.get_tag(self.group, 'tag-1', 'extra', **self.options), 'instance')

		(op1, parent1, data, kwargs1), (op2, parent2, args2, kwargs2), (op3, parent3, args3, kwargs3) = TagRecorder.calls
		self.assertEqual((op1, op2, op3), ('create', 'fetch', 'get'))
		for parent in (parent1, parent2, parent3):
			self.assert_proxy(parent)

		# Payload: the coded concept, scheme version only when the concept has one
		self.assertEqual(data, {'Value': 'T-1', 'Meaning': 'Tag one', 'SchemeDesignator': 'SONADOR'})
		self.pacs.create_tag(self.group, CodedConcept('T-2', 'SONADOR', 'Tag two', scheme_version='2'), **self.options)
		self.assertEqual(TagRecorder.calls[-1][2], {'Value': 'T-2', 'Meaning': 'Tag two', 'SchemeDesignator': 'SONADOR', 'SchemeVersion': '2'})

		# Forwarded arguments: request options kept, proxy setup options and `verify` stripped
		self.assertEqual(kwargs1, {'headers': {'X': '1'}})
		self.assertEqual((args2, kwargs2), ((), {'headers': {'X': '1'}, 'page': 2}))
		self.assertEqual((args3, kwargs3), (('tag-1', 'extra'), {'headers': {'X': '1'}}))
		self.assertEqual(DisplayAttributeRecorder.calls, [])

	def test_display_attributes(self):
		self.assertEqual(self.pacs.create_display_attribute(self.group, '0018,1030', label='Protocol', **self.options), 'created')
		self.assertEqual(self.pacs.create_display_attribute(self.group, '0008,1030', **self.options), 'created')
		self.assertEqual(self.pacs.fetch_display_attributes(self.group, **self.options), 'fetched')
		self.assertEqual(self.pacs.get_display_attribute(self.group, 'attr-1', **self.options), 'instance')

		calls = DisplayAttributeRecorder.calls
		self.assertEqual([c[0] for c in calls], ['create', 'create', 'fetch', 'get'])
		for call in calls:
			self.assert_proxy(call[1])
		self.assertEqual(calls[0][2], {'Code': '0018,1030', 'Label': 'Protocol'})
		self.assertEqual(calls[1][2], {'Code': '0008,1030'})
		self.assertEqual(calls[3][2], ('attr-1',))
		for call in calls:
			self.assertEqual(call[3], {'headers': {'X': '1'}})
		self.assertEqual(TagRecorder.calls, [])

	def test_invalid_group(self):
		stranger = SimpleNamespace(pk=7)
		for call in (lambda: self.pacs.create_tag(stranger, self.concept, **self.options),
				lambda: self.pacs.fetch_tags(stranger, **self.options),
				lambda: self.pacs.get_tag(stranger, 'tag-1', **self.options),
				lambda: self.pacs.fetch_display_attributes(stranger, **self.options),
				lambda: self.pacs.create_display_attribute(stranger, '0018,1030', **self.options),
				lambda: self.pacs.get_display_attribute(stranger, 'attr-1', **self.options)):
			with self.assertRaises(TypeError):
				call()
		self.assertEqual((TagRecorder.calls, DisplayAttributeRecorder.calls), ([], []))

	def test_invalid_concept(self):
		with self.assertRaises(TypeError):
			self.pacs.create_tag(self.group, {'Value': 'T-1'}, **self.options)
		self.assertEqual(TagRecorder.calls, [])

	def test_group_model_override(self):
		'''	A custom group collection class changes which group instances are accepted
		'''
		custom = CustomGroup(self.sonador, {'id': 8, 'name': 'Custom'})
		self.assertEqual(self.pacs.fetch_tags(custom, group_datacollection_class=CustomGroupCollection, **self.options), 'fetched')
		self.assertIs(TagRecorder.calls[-1][1].group, custom)
		self.assertEqual(TagRecorder.calls[-1][3], {'headers': {'X': '1'}})
		with self.assertRaises(TypeError):
			self.pacs.fetch_tags(self.group, group_datacollection_class=CustomGroupCollection, **self.options)

		# Without an override, the default Orthanc group model is used
		self.assertIsInstance(self.pacs._orthanc_group_for(self.group), OrthancGroup)
		self.assertNotIsInstance(self.pacs._orthanc_group_for(self.group), RecordingOrthancGroup)


if __name__ == '__main__':
	unittest.main()
