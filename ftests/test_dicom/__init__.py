'''	Offline tests of the DICOM encoding helpers (pydicom 3 / highdicom fork): the codec and part-10
	backfill helpers, the measurement-report and evidence readers (`dicom_encoding`), and the frame of
	reference, units and STL encoding of M3D models (`dicom_m3d`). No server connection is needed.

		python -m unittest sonador.ftests.test_dicom.dicom_encoding sonador.ftests.test_dicom.dicom_m3d
'''
from .dicom_encoding import *
from .dicom_m3d import *
