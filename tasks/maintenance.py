''' Tasks for running maintenance tasks and operations
'''
import json, logging, traceback

import client.apisettings as gcapi
from client.remote import request_client_error

from ..apisettings import IMAGING_SERVER_RESOURCE_SERIES

logger = logging.getLogger(__name__)


def imageserver_clear_index(imaging_resources, ignore_errors=False,
        callback_onclear=None, callback_onerror=None):
    ''' Clear the provided resources from the server's resource cache
        by calling resource._clear_index.

        @input imaging_resources (iterable): iterable of resources to be
            cleared from the imaging server.
        @input ignore_errors (bool, default=False): when True, errors are 
            logged but do not stop processing. When False, errors will
            be logged and any further data processing will be halted.

        @input callback_onclear (callable): Function that is invoked once 
            the resource has been removed from the index. The callback should accept
            the following signature:
            - resource: resource instance that was removed
        @input callback_onerror (callable): Function that is invoked when 
            there is an error removing a resource from the image server index.
            - resource: resource instance that triggered the error
            - err (Exception): exception instance
    '''
    for _r in imaging_resources:

        try:

            # Remove resource from Sonador resource index
            _sresponse = _r._clear_index()
            _srjson = _sresponse.json()

            if callable(callback_onclear):
                callback_onclear(_r)

        except Exception as err:

            # Retrieve details and server response 
            _details = getattr(err, 'details', None) or {}
            _sresponse = json.loads(_details.get(gcapi.SERVER_RESPONSE)) if _details.get(gcapi.SERVER_RESPONSE) else {}

            # Clear index may return with either a 200 or 404 error code after a successful operation.
            # 404 errors occur when there is a "ghost" resource that is defined in the cache, but not 
            # in the database. 404 errors trigger an operation exception within Sonador, even though
            # within the context of the _clear_index() method constitute a successful operation.
            if _details.get(gcapi.STATUS_CODE) == gcapi.STATUS_404 and _sresponse.get(gcapi.STATUS) == gcapi.SUCCESS:
                if callable(callback_onclear):
                    callback_onclear(_r)
            
            # Log exception and trigger onerror callback
            else:
                logger.error('Unable to clear resource=%s resource-uid=%s from index due to an error. Error: "%s".\nDetails: %s\n%s' % (
                    type(_r).__name__, _r.pk, err, _details, traceback.format_exc(),
                ))

                # Trigger callback
                if callable(callback_onerror):
                    callback_onerror(_r, err)

                # Raise error
                if not ignore_errors: raise err


def imageserver_index_studydata(imageserver, hcache, rapid_lookup=False):
    ''' Retrieve

        @input imageserver (sonador.servers.SonadorImagingServer): image server instance to use for the indexing
        @input hcache (OrderedDict): header cache to use for retrieving data from SOnador
    '''
    for hmeta in hcache:
        if hmeta.resource == IMAGING_SERVER_RESOURCE_STUDY:

            _results = imageserver.query_study({ hmeta.header: hmeta.uid }, rapid_lookup=rapid_lookup)

            # Check results and index matching series
            if len(_results):
                _s = _results[0]
                
                # Sonador indexes "top to bottom". This means that patient records must be indexed
                # first, study records second, and series records last.
                _s.parent.index()
                _s.index()

                for _sx in _s.series_collection:
                    _sx.index()

            else:
                logger.warning('Unable to retrieve series for dcm-uid="%s"' % hmeta.uid)


def imageserver_index_seriesdata(imageserver, hcache, rapid_lookup=False):
    ''' Retrieve and index the imaging series, study, and patient data for the provided header hcache

        @input imageserver (sonador.servers.SonadorImagingServer): image server instance to use for the indexing
        @input hcache (OrdereDict): header cache to use for retrieving data from Sonador        
    '''
    for hmeta in hcache:
        if hmeta.resource == IMAGING_SERVER_RESOURCE_SERIES:

            _results = imageserver.query_series({ hmeta.header: hmeta.uid }, rapid_lookup=rapid_lookup)

            # Check results and index matching series
            if len(_results):
                _sx = _results[0]
                
                # Sonador indexes "top to bottom". This means that patient records must be indexed
                # first, study records second, and series records last.
                _sx.model_patient.index()
                _sx.parent.index()
                _sx.index()

            else:
                logger.warning('Unable to retrieve series for dcm-uid="%s"' % hmeta.uid)
