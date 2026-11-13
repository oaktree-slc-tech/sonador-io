'''	Tests of the Series Tag DICOM-SR exchange profile (Sonador-SR 0.3, specified in
	oak-tree/medical-imaging/sonador-client#36): the Sonador-SR 0.3
	registry and codecs, the readers and writers with their fidelity oracle (`sr_seriestag`), and
	the retrieval path and request budget against a mocked imaging server (`sr_retrieval`), and the
	quantitative measurements (`sr_measurements`, circles and the display colour roles in `sr_circle`,
	bidirectional measurements in `sr_bidirectional`), and the point classes `PixelCoord` and `ImageCoord`
	(`sr_points`).

	These tests need no server. Run from the directory containing the `sonador` package:

		python -m unittest sonador.ftests.test_sr.sr_seriestag sonador.ftests.test_sr.sr_retrieval sonador.ftests.test_sr.sr_measurements
'''
from .sr_seriestag import *
from .sr_retrieval import *
from .sr_measurements import *
from .sr_circle import *
from .sr_bidirectional import *
from .sr_points import *
