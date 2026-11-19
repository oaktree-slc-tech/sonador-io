'''	The timepoint value: a bucket inside one study that groups series.

	A timepoint has a UID, a short identifier, a date and time and a description. Its DICOM encoding and the rules
	for reading, comparing and ordering timepoints are the contract of oak-tree/medical-imaging/sonador-client#50;
	the reader and the writer are in `sonador.imaging.helpers.timepoint`.

	@class Timepoint: the immutable value
	@fn timepoint_date_valid, timepoint_time_valid: whether a text is a DICOM date (eight digits) or a TM value
'''
import datetime, re
from collections import namedtuple

from pydicom.uid import generate_uid

from .constants import DicomDatetimePair, DCMTS_PERFORMED_PROCEDURE_STEP, DCM_TIME_REGEX, \
	TIMEPOINT_ID_MAX_LENGTH, TIMEPOINT_DESCRIPTION_MAX_LENGTH

__all__ = ('Timepoint', 'timepoint_date_valid', 'timepoint_time_valid')

_DATE_REGEX = re.compile(r'^\d{8}$')


def timepoint_date_valid(value):
	'''	Whether a value is a timepoint date: a text of eight digits (`YYYYMMDD`).

		@input value (str or None): the date text
		@returns bool
	'''
	return isinstance(value, str) and bool(_DATE_REGEX.match(value))


def timepoint_time_valid(value):
	'''	Whether a value is a DICOM TM value: `HH`, `HHMM`, `HHMMSS` or `HHMMSS.F` with a fraction of one to six digits,
		each component in its range.

		@input value (str or None): the time text
		@returns bool
	'''
	if not isinstance(value, str):
		return False
	match = DCM_TIME_REGEX.match(value)
	if not match:
		return False
	hours, minutes, seconds, _ = match.groups()
	return int(hours) <= 23 and int(minutes or 0) <= 59 and int(seconds or 0) <= 60


class Timepoint(namedtuple('Timepoint', ('uid', 'id', 'date', 'time', 'description'))):
	'''	A timepoint: immutable, holding the values a document holds. Two values are the same timepoint when their
		`uid` is equal (`same`); equality (`==`) compares every field, presence included.

		A value built by the reader holds what the document holds and is not checked. `create` builds a new timepoint
		and checks it; `validate` checks any value.

		An optional field is None when the document does not hold the attribute and the empty string when it holds
		it without a value: presence is part of the value, so the two are not equal.

		@property uid (str): timepoint UID
		@property id (str or None): short identifier, 16 characters at most
		@property date (str or None): DICOM date text (`YYYYMMDD`)
		@property time (str or None): DICOM time text (TM)
		@property description (str or None): description, 64 characters at most
	'''
	__slots__ = ()

	def __new__(cls, uid, id=None, date=None, time=None, description=None):
		return super().__new__(cls, uid, id, date, time, description)

	@classmethod
	def create(cls, description=None, ts=None, id=None, uid=None):
		'''	Build a new timepoint and check it.

			@input description (str, default=None): description, 64 characters at most
			@input ts (datetime.datetime or datetime.date, default=None): date and time of the timepoint, written as
				given with no time zone conversion; a date gives a timepoint without a time
			@input id (str, default=None): short identifier, 16 characters at most
			@input uid (str, default=a new UUID-derived UID): timepoint UID

			@returns Timepoint
			@raises ValueError for a value `validate` refuses
		'''
		date = time = None
		if isinstance(ts, datetime.datetime):
			date = ts.strftime('%Y%m%d')
			time = ts.strftime('%H%M%S') + (('.%06d' % ts.microsecond) if ts.microsecond else '')
		elif isinstance(ts, datetime.date):
			date = ts.strftime('%Y%m%d')
		elif ts is not None:
			raise ValueError('Timepoint date and time must be a datetime or a date, not %s' % type(ts).__name__)
		return cls(str(uid) if uid else generate_uid(prefix=None), id, date, time, description).validate()

	def validate(self):
		'''	Check the value against what a writer accepts.

			@returns Timepoint: the value
			@raises ValueError for a missing UID, an identifier over 16 characters, a description over 64 characters, a
				time without a date, a date that is not eight digits or a time that is not a TM value
		'''
		if not self.uid or not str(self.uid).strip():
			raise ValueError('A timepoint needs a UID')
		if self.id is not None and len(str(self.id)) > TIMEPOINT_ID_MAX_LENGTH:
			raise ValueError('Timepoint identifier has %d characters; %d at most' % (len(str(self.id)), TIMEPOINT_ID_MAX_LENGTH))
		if self.description is not None and len(str(self.description)) > TIMEPOINT_DESCRIPTION_MAX_LENGTH:
			raise ValueError('Timepoint description has %d characters; %d at most'
				% (len(str(self.description)), TIMEPOINT_DESCRIPTION_MAX_LENGTH))
		if self.time is not None and self.date is None:
			raise ValueError('A timepoint time is written only with a date')
		if self.date is not None and not timepoint_date_valid(self.date):
			raise ValueError('Timepoint date "%s" is not eight digits (YYYYMMDD)' % (self.date,))
		if self.time is not None and not timepoint_time_valid(self.time):
			raise ValueError('Timepoint time "%s" is not a DICOM TM value' % (self.time,))
		return self

	def same(self, other):
		'''	Whether another value is the same timepoint: its UID is equal. No other field takes part in identity.

			@input other (Timepoint or None)
			@returns bool
		'''
		return other is not None and self.uid == getattr(other, 'uid', None)

	@property
	def ts(self):
		'''	Date and time of the timepoint, local to it: no time zone is converted.

			@returns datetime.datetime (midnight for a date without a valid time), or None without a valid date
		'''
		if not timepoint_date_valid(self.date):
			return None
		time = self.time if timepoint_time_valid(self.time) else None
		try:
			return DicomDatetimePair(self.date, time, meta=DCMTS_PERFORMED_PROCEDURE_STEP).ts
		except (ValueError, TypeError):
			return None

	@property
	def sort_key(self):
		'''	Order key of the timepoint: dated before undated; date as text; the digits of the time padded on the right
			with zeros to twelve; description by UTF-16 code unit order, with no locale; UID in string order. An absent
			or invalid date is undated, an absent or invalid time is twelve zeros and an absent description is the empty
			string.

			The description is compared as its UTF-16 code units (its big-endian UTF-16 encoding), which is the order a
			JavaScript string comparison gives: a character outside the Basic Multilingual Plane sorts by its
			surrogates, before U+E000, where an order by code point would put it after.

			@returns tuple
		'''
		dated = timepoint_date_valid(self.date)
		digits = ''.join(c for c in self.time if c.isdigit()) if timepoint_time_valid(self.time) else ''
		description = str(self.description or '').encode('utf-16-be', 'surrogatepass')
		return (0 if dated else 1, self.date if dated else '', digits.ljust(12, '0'), description, str(self.uid or ''))
