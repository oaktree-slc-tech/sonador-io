'''	Tests of the Series Tag DICOM-SR exchange profile (Sonador-SR 0.3, specified in
	oak-tree/medical-imaging/sonador-client#36): the Sonador-SR 0.3
	registry and codecs, the readers and writers with their fidelity oracle (`sr_seriestag`), and
	the retrieval path and request budget against a mocked imaging server (`sr_retrieval`).

	These tests need no server. Run from the directory containing the `sonador` package:

		python -m unittest sonador.ftests.test_sr.sr_seriestag sonador.ftests.test_sr.sr_retrieval
'''
from .sr_seriestag import *
from .sr_retrieval import *
