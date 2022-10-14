import logging

from ..remote import SonadorBaseObject

logger = logging.getLogger(__name__)



class OrthancImagingMixin(object):
	'''	Mixin object which provides methods for working with Orthanc.
	'''
	