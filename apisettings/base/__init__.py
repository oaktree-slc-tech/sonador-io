'''	Base DICOM constants, data structures and encoded concepts for Sonador.

	`sonador.apisettings.base` is a package: `constants` holds the DICOM headers, enumerated values,
	scheme versions and equipment profile; `sr` holds the SR data classes (`Finding`, `Measurement`,
	`ReportGroup`, ...). Both are re-exported here, so `from sonador.apisettings.base import Finding`
	and `from sonador.apisettings.base import DCMHEADER_MODALITY` keep working.
'''
from .constants import *
from .sr import *
