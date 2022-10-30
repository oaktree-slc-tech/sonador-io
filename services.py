import six, requests, json, csv, collections, logging, posixpath
from urllib.parse import urlencode

from tabulate import tabulate
from collections import OrderedDict

from .remote import SonadorBaseObject, SonadorObjectCollection, \
    fetch_sonador_data_collection, fetch_sonador_dataobject
from .helpers import request_client_error

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
    pk_attr = 'service_id'

    @property
    def url(self):
        return posixpath.join(self.fetch_endpoint, self.pk)
    
    @property
    def url_token_validate(self):
        return posixpath.join(self.url, 'introspect')

    def verify_api_credentials(self, token_key, token_value, verify=None):
        ''' Send the provided token key and token value to Sonador for verification.
        '''
        if verify is None:
            verify = self.server.verify

        r = requests.post(self.server.sonador_apiurl(self.url_token_validate), 
            json={ 'token_key': token_key, 'token_value': token_value },
            verify=verify, headers=self.server.sonador_request_headers())
        
        if not r.ok:
            request_client_error('Unable to retrieve API credentials from Sonador due to an error.', r)
        
        return r.json()
