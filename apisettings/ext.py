'''	Sonador Extension API. Constants and enumerations used by the Sonador client.
'''
from client import apisettings as gcapi

SONADOR_PERMISSIONS_HEADER = 'sonador-permissions'
SONADOR_OPCODE_HEADER = 'sonador-%s' % gcapi.OPCODE
SONADOR_STATUS_HEADER = 'sonador-%s' % gcapi.STATUS


# DICOMweb extension sub-endpoints. Path segments hung from a DICOMweb study or series
# resource URL by the Sonador Orthanc plugin, none of which are part of the DICOMweb
# standard: {dicomweb_root}/{studies|series}/{uid}/{endpoint}
DICOMWEB_ENDPOINT_ARCHIVE = 'archive'

# Resource management namespace. Deliberately generic: this segment delivers removal
# (DELETE), and later management operations (anonymize, modify, reindex) extend the same
# route rather than needing a second naming decision.
DICOMWEB_ENDPOINT_MANAGE = 'manage'
