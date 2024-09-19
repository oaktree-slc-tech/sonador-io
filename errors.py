'''	Methods and classes to help with error parsing in Sonador
'''
import logging, json

from client.errors import ClientOperationError, clientexception_server_errors
from client import apisettings as gapi

logger = logging.getLogger(__name__)


def soandor_clientexception_server_errors(err):
	'''	Retrieve a server errorlist from the provided client exception instance

		@returns dict or None if no errors are included in the exception details
	'''
	# Attempt to retrieve a standard error JSON from the error instance
	server_errors = clientexception_server_errors(err) if isinstance(err, ClientOperationError) else None
	if not server_errors:		

		# Parse server response to dict
		_details = getattr(err, 'details', None) or {}
		logger.critical(_details)

		_server_response = json.loads(_details.get(gapi.SERVER_RESPONSE)) \
				if (_details.get(gapi.SERVER_RESPONSE) and isinstance(_details.get(gapi.SERVER_RESPONSE), (bytes, str))) \
			else _details.get(gapi.SERVER_RESPONSE) if isinstance(_details.get(gapi.SERVER_RESPONSE), dict) \
			else {}		

		# Retrieve errors from server response
		server_errors = _server_response.get(gapi.ERRORS)

	return server_errors


def duplicate_resource_error(err, field_check=gapi.ERRORS_ALL):
	'''	Check the provided error instance to determine if the provided error instance
		was triggered by a "unique" or duplicate error.

		@returns True if the errors hash associated with the error instance
			includes a unique/duplicate error, False otherwise
	'''
	server_errors = clientexception_server_errors(err)
	if server_errors:
		_field_errors = server_errors.get(field_check, [])

		return any(emsg.get(gapi.CODE) == gapi.VALIDATION_APICODE_UNIQUE for emsg in _field_errors)

	return False


def only_duplicate_resource_error(err, field_check=gapi.ERRORS_ALL):
	'''	Check the provided error instance to determine if the reason for the rejection was ONLY
		due to a unique/duplicate error.

		1. Checks for presence of a duplicate resource error
		2. Ensures that no other fields are present in the error list
		3. Ensures that no other errors are present in __all__ section of the error response.

		@returns True if all checks pass, False otherwise
	'''
	if duplicate_resource_error(err, field_check=field_check):
		server_errors = clientexception_server_errors(err)

		# Ensure that there is only a single field in the errors hash,
		# the field is __all__ and that there is only a single error message
		# in the field list.
		if server_errors and len(server_errors) == 1 and server_errors.get(field_check):
			if len(server_errors.get(field_check, [])) == 1:
				return True

	return False