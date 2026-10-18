'''	Tests of the Sonador server models which need no server connection.

	Run from the directory containing the `sonador` package: python -m unittest sonador.test.servers
'''
import unittest
from types import SimpleNamespace

from sonador.servers import SonadorImagingServer


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


if __name__ == '__main__':
	unittest.main()
