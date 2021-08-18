import six, re, datetime, logging
from collections import OrderedDict

from client.utils.serialization import GuruLabsBaseJsonEncoder
from client.utils.serialization import datetime2str

from .apisettings import DCM_DATETIME_STRFORMAT, DCM_DATE_STRFORMAT, \
	DCM_TIME_STRFORMAT, DCM_TIME_STRFORMAT_ALT1

logger = logging.getLogger(__name__)


OUTPUT_TYPE_TABULATE = 'tabulate'
OUTPUT_TYPE_CSV = 'csv'


OUTPUT_TYPE_SUPPORTED = OrderedDict((
	(OUTPUT_TYPE_TABULATE, 'tabulated output'),
	(OUTPUT_TYPE_CSV, 'comma separated values'),
))



DATETIME_REGEX1 = re.compile(r'^\d{2}/\d{2}/\d{4} \d{2}:\d{2}:\d{2}.\d+$')
DATETIME_FORMAT1 = '%m/%d/%Y %H:%M:%S.%f'
DATETIME_REGEX2 = re.compile(r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}.\d+$')
DATETIME_FORMAT2 = '%Y-%m-%d %H:%M:%S.%f'
DATETIME_REGEX3 = re.compile(r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$')
DATETIME_FORMAT3 = '%Y-%m-%d %H:%M:%S'
DATE1_REGEX = re.compile(r'^\d{4}-\d{2}-\d{2}$')
DATE1_FORMAT = '%Y-%m-%d'
DCM_DATE_REGEX = re.compile(r'^\d{4}[1-9]{2}\d{2}$')
ISO8601_DATETIME_REGEX = re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}.\d+Z$')
ISO8601_DATETIME_FORMAT = '%Y-%m-%dT%H:%M:%S.%fZ'
DCM_DATETIME_REGEX = re.compile(
	r'^(?P<year>\d{4})(?P<month>\d{2})(?P<day>\d{2})(?P<hour>\d{2})(?P<minute>\d{2})(?P<second>\d{2}).(?P<fractional>\d{6}).+')



class SonadorJsonEncoder(GuruLabsBaseJsonEncoder):
	'''	JSON encoder instance to be used with Sonador
	'''
	datetime_format = DATETIME_FORMAT1


def json_str2datetime(v):
	'''	Parse a string value to a date/time object
	'''
	# Return 
	if isinstance(v, (datetime.datetime, datetime.date)):
		return v

	# DICOM formatted date/time
	if DCM_DATETIME_REGEX.match(v):
		return datetime.datetime.strptime(v, DCM_DATETIME_STRFORMAT)

	# ISO8601 formatted date/time
	elif ISO8601_DATETIME_REGEX.match(v):
		return datetime.datetime.strptime(v, ISO8601_DATETIME_FORMAT)

	# Sonador formatted date/time
	elif DATETIME_REGEX1.match(v):
		return datetime.datetime.strptime(v, DATETIME_FORMAT1)

	# Sonador formatted date/time (alt 1)
	elif DATETIME_REGEX2.match(v):
		return datetime.datetime.strptime(v, DATETIME_FORMAT2)

	# Sonador format date/time (alt 2)
	elif DATETIME_REGEX3.match(v):
		return datetime.datetime.strptime(v, DATETIME_FORMAT3)

	# Sonador formatted date (type 1)
	elif DATE1_REGEX.match(v):
		return datetime.datetime.strptime(v, DATE1_FORMAT).date()


def dcm_str2time(v, formats=(DCM_TIME_STRFORMAT, DCM_TIME_STRFORMAT_ALT1)):
	''' Parse a string value to a time object. Used to parse DCM tags to datetime.time objects.

		@returns datetime.time
	'''
	if isinstance(v, (datetime.datetime, datetime.date)) or v is None:
		return v

	if v and not isinstance(v, six.string_types):
		raise TypeError('Unable to convert provided value "%s" to datetime. Invalid type: %s'
			% (v, type(v)))

	for fmt in formats:
		try: return datetime.datetime.strptime(v, fmt).time()
		except ValueError as err:
			logger.debug('Unable to convert value "%s" to time using pattern "%s"'
				% (v, fmt))

	raise ValueError('Unable to convert value "%s" to time using patterns: %s'
		% (v, ', '.join('"%s"' % f for f in formats)))


def json_datetime_parser(jdata):
	'''	Post-processing method for a JSON parser that converts datetime strings to datetime objects
	'''
	if isinstance(jdata, dict):

		for k, v in six.iteritems(jdata):

			# Parse nested object structures
			if isinstance(v, dict):
				json_datetime_parser(v)

			# Convert date strings to date/time objects
			if isinstance(v, six.string_types):
				dv = json_str2datetime(v)
				if dv: jdata[k] = dv

	# Parse members of arrays
	elif isinstance(jdata, (tuple, list)):
		for v in jdata:
			json_datetime_parser(v)

	return jdata


def sonador_encode2str(v, datetime_format=DATETIME_FORMAT1):
	'''	String encode values for listing output (used for tabulate and CSV)

		@returns str encoded version of output
	'''
	return datetime2str(v, datetime_format) if isinstance(v, datetime.datetime) else v
