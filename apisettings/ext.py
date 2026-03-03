'''	Sonador Extension API. Constants and enumerations used by the Sonador client.
'''
from client import apisettings as gcapi

SONADOR_PERMISSIONS_HEADER = 'sonador-permissions'
SONADOR_OPCODE_HEADER = 'sonador-%s' % gcapi.OPCODE
SONADOR_STATUS_HEADER = 'sonador-%s' % gcapi.STATUS