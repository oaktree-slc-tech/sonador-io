'''	Access to DICOM headers held in pydicom datasets or in dictionaries of tags (the simplified
	tags of an Orthanc instance, the main DICOM tags of a series), so that the same reader serves
	retrieved files and the tags index of the imaging server.

	The helpers sit below the remote model layer (they import only the API settings) and are shared
	by the Orthanc models and the imaging helpers.

	@fn dcm_header_value: read a header, empty values as the default
	@fn dcm_header_uid: read a UID header as a stripped string
	@fn dcm_referenced_series_uids: Series Instance UIDs of the Referenced Series Sequence, in order
'''
from client.utils.general import unique_ordered_sequence

from ...apisettings import DCMHEADER_SERIES_INSTANCE_UID, DCMHEADER_SR_REF_SERIES_SEQ


def dcm_header_value(headers, name, default=None):
	'''	Retrieve a header value from a pydicom dataset or from a dictionary of tags. Both support
		`get(keyword, default)`; objects that do not are read by attribute. Absent headers, None,
		empty and whitespace-only strings are returned as the default; other values (including
		falsey values such as 0 or an empty sequence) are returned as they are.

		@input headers (pydicom.dataset.Dataset or Mapping): headers
		@input name (str): DICOM keyword of the header
		@input default: value returned when the header is absent or empty

		@returns header value or default
	'''
	if headers is None:
		return default

	getter = getattr(headers, 'get', None)
	value = getter(name, None) if callable(getter) else getattr(headers, name, None)
	if value is None or (isinstance(value, str) and not value.strip()):
		return default

	return value


def dcm_header_uid(headers, name):
	'''	Retrieve a UID header as a stripped string, or None if absent or empty. The value is
		normalized (str, surrounding whitespace removed), not validated as a UID.
	'''
	value = dcm_header_value(headers, name)
	return str(value).strip() or None if value is not None else None


def dcm_referenced_series_uids(headers):
	'''	Series Instance UIDs of the series referenced by the headers (Referenced Series Sequence),
		in the order of the sequence, without repeats; items without a UID are skipped.

		@input headers (pydicom.dataset.Dataset or Mapping): headers of the instance
		@returns list of str
	'''
	uids = (dcm_header_uid(item, DCMHEADER_SERIES_INSTANCE_UID)
		for item in dcm_header_value(headers, DCMHEADER_SR_REF_SERIES_SEQ) or [])

	return list(unique_ordered_sequence(uid for uid in uids if uid))
