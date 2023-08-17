import six, re, datetime, logging
from collections import OrderedDict

from client.utils.serialization import GuruLabsBaseJsonEncoder
from client.utils.serialization import datetime2str
from client.remote.serialization import json_str2datetime, str2datetime, json_datetime_parser, \
	DATETIME_REGEX1, DATETIME_FORMAT1, DATETIME_REGEX2, \
	DATETIME_FORMAT2, DATETIME_REGEX3, DATETIME_FORMAT3, DATE1_REGEX, \
	DATE1_FORMAT, DCM_DATE_REGEX, ISO8601_DATETIME_REGEX, ISO8601_DATETIME_FORMAT, \
	DCM_DATETIME_REGEX

from .apisettings import DCM_DATETIME_STRFORMAT, \
	DCM_DATE_STRFORMAT, DCM_DATE_STRFORMAT_ALT1, DCM_DATE_STRFORMAT_ALT2, \
	DCM_TIME_STRFORMAT, DCM_TIME_STRFORMAT_ALT1

logger = logging.getLogger(__name__)


OUTPUT_TYPE_TABULATE = 'tabulate'
OUTPUT_TYPE_CSV = 'csv'


OUTPUT_TYPE_SUPPORTED = OrderedDict((
	(OUTPUT_TYPE_TABULATE, 'tabulated output'),
	(OUTPUT_TYPE_CSV, 'comma separated values'),
))


class SonadorJsonEncoder(GuruLabsBaseJsonEncoder):
	'''	JSON encoder instance to be used with Sonador
	'''
	datetime_format = DATETIME_FORMAT1


def dcm_str2date(v, 
		formats=(DCM_DATE_STRFORMAT, DCM_DATE_STRFORMAT_ALT1, DCM_DATE_STRFORMAT_ALT2)):
	'''	Parse a string value to a date object. Used to parse DCM tags to datetime.date objects.

		@returns datetime.date
	'''
	# Return date/time objects which are already parsed
	if isinstance(v, (datetime.datetime, datetime.date, datetime.time)) or v is None:
		return v

	# Reject values which are not strings
	if v and not isinstance(v, six.string_types):
		raise TypeError('Unable to convert provided value "%s" to date. Invalid type: %s'
			% (v, type(v)))

	for fmt in formats:
		try: return datetime.datetime.strptime(v, fmt).date()
		except ValueError as err:
			logger.debug('Unable to convert value "%s" to time using pattern "%s'
				% (v, fmt))

	raise ValueError('Unabe to convert value "%s" to date using patterns: %s'
		% (v, ', '.join('"%s"' % f for f in formats)))


def dcm_str2time(v, formats=(DCM_TIME_STRFORMAT, DCM_TIME_STRFORMAT_ALT1)):
	''' Parse a string value to a time object. Used to parse DCM tags to datetime.time objects.

		@returns datetime.time
	'''
	# Return date/time objects which are already parsed
	if isinstance(v, (datetime.datetime, datetime.date, datetime.time)) or v is None:
		return v

	# Reject any values which are not strings
	if v and not isinstance(v, six.string_types):
		raise TypeError('Unable to convert provided value "%s" to time. Invalid type: %s'
			% (v, type(v)))

	for fmt in formats:
		try: return datetime.datetime.strptime(v, fmt).time()
		except ValueError as err:
			logger.debug('Unable to convert value "%s" to time using pattern "%s"'
				% (v, fmt))

	raise ValueError('Unable to convert value "%s" to time using patterns: %s'
		% (v, ', '.join('"%s"' % f for f in formats)))


def sonador_encode2str(v, datetime_format=DATETIME_FORMAT1):
	'''	String encode values for listing output (used for tabulate and CSV)

		@returns str encoded version of output
	'''
	return datetime2str(v, datetime_format) if isinstance(v, datetime.datetime) else v
