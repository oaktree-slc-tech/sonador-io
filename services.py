import six, requests, json, csv, collections, logging, posixpath
from urllib.parse import urlencode

from tabulate import tabulate
from collections import OrderedDict

from .remote import SonadorBaseObject, SonadorObjectCollection, \
    fetch_sonador_data_collection, fetch_sonador_dataobject

logger = logging.getLogger(__name__)


# Sonador Data Services
DATA_SERVICE_OUTPUT_COLUMNS = OrderedDict((
        ('pk', 'Service ID'),
        ('description', 'Data Service Description'),
        ('active', 'Active'),
        ('acl_allow_staff', 'Allow Staff'),
    ))


class DataService(SonadorBaseObject):
    ''' Object representation of a Sonador managed data service
    '''
    fetch_endpoint = '/visionaire/api/data/service'
    tabulate_output_columns = DATA_SERVICE_OUTPUT_COLUMNS
    details_exclude = ('token',)


