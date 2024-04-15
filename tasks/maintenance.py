''' Tasks for running maintenance tasks and operations
'''
import json, logging, traceback

import client.apisettings as gcapi
from client.remote import request_client_error

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
