import six, re, datetime, logging
from collections import OrderedDict

from client.utils.serialization import GuruLabsBaseJsonEncoder
from client.utils.serialization import datetime2str
from client.remote.serialization import json_str2datetime, str2datetime, json_datetime_parser, \
	DATETIME_REGEX1, DATETIME_FORMAT1, DATETIME_REGEX2, \
	DATETIME_FORMAT2, DATETIME_REGEX3, DATETIME_FORMAT3, DATE1_REGEX, \
	DATE1_FORMAT, DCM_DATE_REGEX, ISO8601_DATETIME_REGEX, ISO8601_DATETIME_FORMAT, \
	DCM_DATETIME_REGEX

from .apisettings import DCM_DATETIME_STRFORMAT, DCM_DATETIME_STRFORMAT_ALT1, \
	DCM_DATE_STRFORMAT, DCM_DATE_STRFORMAT_ALT1, DCM_DATE_STRFORMAT_ALT2, \
	DCM_TIME_STRFORMAT, DCM_TIME_STRFORMAT_ALT1, DCM_TIME_REGEX, \
	DCM_UTC_OFFSET_REGEX, DCM_UTC_OFFSET_MINUTES_MIN, DCM_UTC_OFFSET_MINUTES_MAX

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

		Values in the TM value representation (`HHMMSS.FFFFFF` with the trailing components
		optional: `HH`, `HHMM`, `HHMMSS`; a fraction of one to six digits) are parsed by their
		fixed-width components, so that `0304` is 03:04:00. Other values are tried against the
		`strptime` patterns in `formats`.

		@returns datetime.time
	'''
	# Return date/time objects which are already parsed
	if isinstance(v, (datetime.datetime, datetime.date, datetime.time)) or v is None:
		return v

	# Reject any values which are not strings
	if v and not isinstance(v, six.string_types):
		raise TypeError('Unable to convert provided value "%s" to time. Invalid type: %s'
			% (v, type(v)))

	# Conformant TM values: fixed-width components
	match = DCM_TIME_REGEX.match(v.strip())
	if match:
		hours, minutes, seconds, fraction = match.groups()
		return datetime.time(int(hours), int(minutes or 0), int(seconds or 0),
			int((fraction or '').ljust(6, '0')) if fraction else 0)

	for fmt in formats:
		try: return datetime.datetime.strptime(v, fmt).time()
		except ValueError as err:
			logger.debug('Unable to convert value "%s" to time using pattern "%s"'
				% (v, fmt))

	raise ValueError('Unable to convert value "%s" to time using patterns: %s'
		% (v, ', '.join('"%s"' % f for f in formats)))


def dcm_str2utcoffset(v):
	'''	Parse a Timezone Offset From UTC (0008,0201) value, or the offset suffix of a DT value, to a
		fixed-offset timezone. DICOM (PS3.3 C.12.1.1.8, PS3.5 6.2) encodes the offset as a sign and four
		digits (`&ZZXX`, hours then minutes) from `-1200` to `+1400` inclusive, minutes below 60, with
		`+0000` for UTC; `-0000` is not a valid encoding. Surrounding whitespace (DICOM padding) is
		ignored. Values which are already a tzinfo, and None, are returned as they are.

		Raises ValueError for a malformed or out of range offset and TypeError for a value which is not
		a string.

		@returns datetime.tzinfo or None
	'''
	if v is None or isinstance(v, datetime.tzinfo):
		return v

	# Reject values which are not strings
	if not isinstance(v, six.string_types):
		raise TypeError('Unable to convert provided value "%s" to UTC offset. Invalid type: %s'
			% (v, type(v)))

	match = DCM_UTC_OFFSET_REGEX.match(v.strip())
	if not match:
		raise ValueError('Unable to convert value "%s" to UTC offset. Expected a sign and four digits (&ZZXX).' % v)

	sign = -1 if match.group(1) == '-' else 1
	hours, minutes = int(match.group(2)), int(match.group(3))
	signed_minutes = sign * (hours * 60 + minutes)
	if minutes > 59 or not DCM_UTC_OFFSET_MINUTES_MIN <= signed_minutes <= DCM_UTC_OFFSET_MINUTES_MAX \
			or (sign < 0 and signed_minutes == 0):
		raise ValueError('Unable to convert value "%s" to UTC offset. Offsets range from -1200 to +1400 '
			'with minutes below 60; -0000 is not a valid encoding.' % v)

	return datetime.timezone(datetime.timedelta(minutes=signed_minutes))


def dcm_str2datetime(v, formats=(DCM_DATETIME_STRFORMAT, DCM_DATETIME_STRFORMAT_ALT1, 
			DCM_DATE_STRFORMAT, DCM_DATE_STRFORMAT_ALT1, DCM_DATE_STRFORMAT_ALT2)):
	'''	Parse a string value to a date/time object. Used to parse DCM tags to datetime.datetime.

		@returns datetime.datetime
	'''
	if v and isinstance(v, datetime.datetime):
		return v

	# Reject values which are not strings
	if v and not isinstance(v, six.string_types):
		raise TypeError('Unable to convert provided value "%s" to datetime. Invalid type: %s'
			% (v, type(v)))

	for fmt in formats:
		try: return datetime.datetime.strptime(v, fmt)
		except ValueError as err:
			logger.debug('Unable to convert value "%s" to date using pattern "%s"'
				% (v, fmt))

	raise ValueError('Unable to convert value "%s" to datetime uing patterns: %s'
		% (v, ', '.join('"%s"' % f for f in formats)))


def sonador_encode2str(v, datetime_format=DATETIME_FORMAT1):
	'''	String encode values for listing output (used for tabulate and CSV)

		@returns str encoded version of output
	'''
	return datetime2str(v, datetime_format) if isinstance(v, datetime.datetime) else v
