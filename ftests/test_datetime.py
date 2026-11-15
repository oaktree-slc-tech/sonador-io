'''	Offline tests of the DICOM date/time helpers: `dcm_str2time` on TM values, `dcm_str2utcoffset`,
	and the UTC conversion of `DicomDatetimePair` (`utcoffset`, `ts_utc`), alongside the existing `ts`.

	These tests need no server. Run from the directory containing the `sonador` package:

		python -m unittest sonador.ftests.test_datetime
'''
import datetime, unittest

from ..apisettings import DicomDatetimePair, DCMTS_CONTENT, \
	DCMHEADER_TIMEZONE_OFFSET_FROM_UTC, DCMCODE_TIMEZONE_OFFSET_FROM_UTC
from ..serialization import dcm_str2utcoffset, dcm_str2time


def tz(minutes):
	return datetime.timezone(datetime.timedelta(minutes=minutes))


class TimeParseTestCase(unittest.TestCase):
	'''	`dcm_str2time`: the TM value representation with its optional trailing components.
	'''
	def test_components(self):
		self.assertEqual(dcm_str2time('030405'), datetime.time(3, 4, 5))
		self.assertEqual(dcm_str2time('0304'), datetime.time(3, 4, 0))
		self.assertEqual(dcm_str2time('03'), datetime.time(3, 0, 0))
		self.assertEqual(dcm_str2time('235959'), datetime.time(23, 59, 59))

	def test_fraction(self):
		self.assertEqual(dcm_str2time('030405.123456'), datetime.time(3, 4, 5, 123456))
		self.assertEqual(dcm_str2time('030405.5'), datetime.time(3, 4, 5, 500000))
		self.assertEqual(dcm_str2time('030405.000001'), datetime.time(3, 4, 5, 1))

	def test_padding_is_ignored(self):
		self.assertEqual(dcm_str2time('0304 '), datetime.time(3, 4, 0))

	def test_passthrough(self):
		self.assertIsNone(dcm_str2time(None))
		t = datetime.time(1, 2, 3)
		self.assertIs(dcm_str2time(t), t)

	def test_malformed(self):
		# (Digit strings of another odd length, such as `123`, are not TM values and fall through to the lenient
		# `strptime` patterns, which is the behaviour the parser has always had for non-conformant values.)
		for value in ('', '1', '120000.', '120000.1234567', '12:00:00', '240000', '126000', '120060', 'noon'):
			with self.assertRaises(ValueError, msg=value):
				dcm_str2time(value)

	def test_invalid_type(self):
		with self.assertRaises(TypeError):
			dcm_str2time(120000)


class UtcOffsetParseTestCase(unittest.TestCase):
	'''	`dcm_str2utcoffset`: the `&ZZXX` encoding and its DICOM range.
	'''
	def test_header_constants(self):
		self.assertEqual(DCMHEADER_TIMEZONE_OFFSET_FROM_UTC, 'TimezoneOffsetFromUTC')
		self.assertEqual(DCMCODE_TIMEZONE_OFFSET_FROM_UTC, ('0008', '0201'))

	def test_offsets(self):
		self.assertEqual(dcm_str2utcoffset('+0100'), tz(60))
		self.assertEqual(dcm_str2utcoffset('-0530'), tz(-330))
		self.assertEqual(dcm_str2utcoffset('+0000'), datetime.timezone.utc)
		self.assertEqual(dcm_str2utcoffset('+0045'), tz(45))
		self.assertEqual(dcm_str2utcoffset('-0959'), tz(-599))

	def test_endpoints(self):
		self.assertEqual(dcm_str2utcoffset('+1400'), tz(14 * 60))
		self.assertEqual(dcm_str2utcoffset('-1200'), tz(-12 * 60))

	def test_padding_is_ignored(self):
		# SH values are padded to an even length with a trailing space
		self.assertEqual(dcm_str2utcoffset('+0100 '), tz(60))
		self.assertEqual(dcm_str2utcoffset(' -0130'), tz(-90))

	def test_passthrough(self):
		self.assertIsNone(dcm_str2utcoffset(None))
		self.assertIs(dcm_str2utcoffset(datetime.timezone.utc), datetime.timezone.utc)

	def test_malformed(self):
		for value in ('', '0130', '--0130', '+1', '+01:00', 'Z', '+01000', '+0a00', '+ 100'):
			with self.assertRaises(ValueError, msg=value):
				dcm_str2utcoffset(value)

	def test_out_of_range(self):
		for value in ('+0060', '+2400', '+2300', '+1401', '-1201', '-0000', '-1260'):
			with self.assertRaises(ValueError, msg=value):
				dcm_str2utcoffset(value)

	def test_invalid_type(self):
		with self.assertRaises(TypeError):
			dcm_str2utcoffset(100)


class DicomDatetimePairUtcTestCase(unittest.TestCase):
	'''	`DicomDatetimePair.ts_utc` beside the unchanged `ts`.
	'''
	def pair(self, date=None, time=None, offset=None):
		return DicomDatetimePair(date, time, offset_value=offset, meta=DCMTS_CONTENT)

	def test_ts_is_unchanged(self):
		pair = self.pair('20260102', '120000', '+0100')
		self.assertEqual(pair.ts, datetime.datetime(2026, 1, 2, 12, 0, 0))
		self.assertIsNone(pair.ts.tzinfo)
		self.assertEqual(pair.utcoffset, tz(60))

	def test_without_offset_is_taken_as_utc(self):
		pair = self.pair('20260102', '030405.123456')
		self.assertIsNone(pair.utcoffset)
		self.assertEqual(pair.ts_utc, datetime.datetime(2026, 1, 2, 3, 4, 5, 123456))
		self.assertEqual(self.pair('20260102', '030405.5').ts_utc, datetime.datetime(2026, 1, 2, 3, 4, 5, 500000))

	def test_offset_applied(self):
		self.assertEqual(self.pair('20260102', '120000', '+0100').ts_utc, datetime.datetime(2026, 1, 2, 11, 0, 0))
		self.assertEqual(self.pair('20260102', '003000', '-0530').ts_utc, datetime.datetime(2026, 1, 2, 6, 0, 0))
		self.assertEqual(self.pair('20260102', '120000', '+0000').ts_utc, datetime.datetime(2026, 1, 2, 12, 0, 0))
		# The endpoints cross the date line in either direction
		self.assertEqual(self.pair('20260102', '120000', '+1400').ts_utc, datetime.datetime(2026, 1, 1, 22, 0, 0))
		self.assertEqual(self.pair('20260102', '120000', '-1200').ts_utc, datetime.datetime(2026, 1, 3, 0, 0, 0))

	def test_result_is_naive(self):
		self.assertIsNone(self.pair('20260102', '120000', '+0100').ts_utc.tzinfo)

	def test_time_defaults_to_midnight(self):
		# As `ts` does: a date without a time is midnight in the offset's zone
		self.assertEqual(self.pair('20260102', None, '+0100').ts_utc, datetime.datetime(2026, 1, 1, 23, 0, 0))
		self.assertEqual(self.pair('20260102').ts_utc, datetime.datetime(2026, 1, 2, 0, 0, 0))
		self.assertIsNone(self.pair('20260102', None, '+0100').time_value)

	def test_without_date(self):
		self.assertIsNone(self.pair(None, '120000', '+0100').ts_utc)
		self.assertIsNone(self.pair().ts_utc)

	def test_malformed_values_raise(self):
		with self.assertRaises(ValueError):
			self.pair('20260102', '120000', '+2300').ts_utc
		with self.assertRaises(ValueError):
			self.pair('20260102', '120000', '-0000').utcoffset
		with self.assertRaises(ValueError):
			self.pair('garbage', '120000').ts_utc
		with self.assertRaises(ValueError):
			self.pair('20260102', '120000.1234567').ts_utc
		with self.assertRaises(ValueError):
			self.pair('20260102', '120000.').ts_utc
		# Trailing TM components may be omitted
		self.assertEqual(self.pair('20260102', '0304', '+0100').ts_utc, datetime.datetime(2026, 1, 2, 2, 4, 0))

	def test_unrepresentable_utc_instant(self):
		with self.assertRaises(OverflowError):
			self.pair('00010101', '000000', '+0100').ts_utc
		with self.assertRaises(OverflowError):
			self.pair('99991231', '235959', '-0100').ts_utc


if __name__ == '__main__':
	unittest.main()
