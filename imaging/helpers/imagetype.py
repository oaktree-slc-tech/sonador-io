'''	Image Type readers: tell a derived image or series (a display copy resampled to a coarser grid, a
	secondary capture, a computed image) from the acquired, primary imaging it was made from. The
	matching writers compose the attribute from the `DCM_IMAGE_TYPE_*` values of `sonador.apisettings`.

	Image Type (0008,0008; PS3.3 C.7.6.1.1.2) is multi-valued: value 1 is ORIGINAL or DERIVED (or MIXED
	for an Enhanced multi-frame image whose frames are of both kinds, PS3.3 C.8.16.1.1), value 2 PRIMARY
	or SECONDARY. An image is derived, in the sense of these readers "not primary imaging", when value 1
	is DERIVED or MIXED or value 2 is SECONDARY: a MIXED image holds computed frames and is never
	certified as a primary source. Without an Image Type the derivation attributes decide (Derivation
	Description, Source Image Sequence). An explicit Image Type is trusted over the derivation
	attributes.

	The series readers accept the imaging series models by duck typing: the series tags (`dicomdata`,
	the main tags the imaging server indexes at the series level, when the object has them) are
	consulted first and the tags of the first instance (`dcm0.tags`) second, since Orthanc does not
	index the Image Type at the series level by default. A series-like object may carry either or both;
	one that carries neither cannot be classified and is rejected by the guard. 
	For the Sonador models a cold series fetches its instance collection and then the
	instance tags from the server, and caches both on the resource, so later reads of the same model
	cost nothing; a duck-typed object caches as it chooses. The simplified tags of the server give a
	multi-valued attribute as one backslash-separated string; a pydicom dataset gives a MultiValue.
	Both forms are read.

	Pipelines that derive objects from imaging (models, secondary captures, reports, resampled
	volumes) use the list and guard helpers so that the objects are linked to and reference primary
	imaging only: a study that holds display copies of an acquisition lists them as series of the same
	modality, and a selection by modality or position alone would pick them up.

	@fn dcm_image_type: values of the Image Type of a tag dictionary
	@fn dcm_tags_are_derived: whether a tag dictionary describes a derived image
	@fn dcm_series_imagetype_tags: the tags a series is classified from
	@fn dcm_series_image_type: Image Type of a series resource
	@fn dcm_series_is_derived: three-valued classification of a series resource (True, False, None)
	@fn dcm_exclude_derived_series: a series list without its derived series
	@fn dcm_primary_series_list: the primary series of a list, of given modalities
	@fn dcm_check_primary_series: guard of a link, the linked and referenced series must be primary
'''
import logging

from ...apisettings import DicomMetaKey, DCMHEADER_IMAGE_TYPE, DCMHEADER_DERIVATION_DESCRIPTION, \
	DCMHEADER_SR_SOURCE_IMAGE_SEQ, DCM_IMAGE_TYPE_DERIVED, DCM_IMAGE_TYPE_MIXED, DCM_IMAGE_TYPE_SECONDARY, \
	DCM_MULTIVALUE_SEPARATOR, SERIES_UNKNOWN_ERROR, SERIES_UNKNOWN_SKIP, SERIES_UNKNOWN_KEEP, SERIES_UNKNOWN_OPTIONS

# Image Type value 1 terms of an image that is not primary imaging: computed from other images (DERIVED)
# or holding computed frames beside acquired ones (MIXED, Enhanced multi-frame images)
DCM_IMAGE_TYPE_NONPRIMARY_VALUE1 = (DCM_IMAGE_TYPE_DERIVED, DCM_IMAGE_TYPE_MIXED)

logger = logging.getLogger(__name__)


def dcm_image_type(tags):
	'''	Values of the Image Type attribute of a tag dictionary (keywords) or dataset, normalised to upper
		case: the backslash-separated string of the simplified tags of the imaging server, a list of values
		or a pydicom MultiValue. The positions of the values are preserved, including empty values (PS3.3
		C.7.6.1.1.2 allows a zero-length value 3 before a value 4), so that value N is at index N - 1. An
		empty list when the attribute is absent or holds no value.

		@input tags (dict or pydicom.dataset.Dataset): tags of an instance or series
		@returns list of str
	'''
	value = tags.get(DCMHEADER_IMAGE_TYPE) if tags is not None else None
	if value is None:
		return []
	if isinstance(value, str):
		values = value.split(DCM_MULTIVALUE_SEPARATOR)
	elif hasattr(value, '__iter__'):
		values = list(value)
	else:
		values = [value]

	values = [str(v if v is not None else '').strip().upper() for v in values]
	return values if any(values) else []


def dcm_tags_are_derived(tags):
	'''	Whether a tag dictionary describes an image that is not primary imaging: Image Type value 1
		DERIVED or MIXED (an Enhanced image with computed frames beside acquired ones), or value 2
		SECONDARY. Without an Image Type the derivation attributes decide: an image that names a
		Derivation Description or a Source Image Sequence is derived. An explicit Image Type is trusted
		over the derivation attributes.

		@input tags (dict or pydicom.dataset.Dataset): tags of an instance or series
		@returns bool
	'''
	image_type = dcm_image_type(tags)
	if image_type:
		return image_type[0] in DCM_IMAGE_TYPE_NONPRIMARY_VALUE1 \
			or (len(image_type) > 1 and image_type[1] == DCM_IMAGE_TYPE_SECONDARY)

	if tags is None:
		return False
	return bool(tags.get(DCMHEADER_DERIVATION_DESCRIPTION) or tags.get(DCMHEADER_SR_SOURCE_IMAGE_SEQ))


def dcm_series_imagetype_tags(sx):
	'''	Tags a series resource is classified from: its series tags (`dicomdata`) when they carry the Image
		Type, else the tags of its first instance (`dcm0.tags`; a cold Sonador model fetches the instance
		collection and the instance tags from the server and caches them, see the module notes). Errors
		reading the instance tags propagate.

		@input sx (sonador.imaging.orthanc.ImagingSeries or series-like object)
		@returns dict
	'''
	series_tags = getattr(sx, 'dicomdata', None) or {}
	if dcm_image_type(series_tags):
		return series_tags

	return sx.dcm0.tags or {}


def dcm_series_image_type(sx):
	'''	Values of the Image Type of a series resource (`dcm_series_imagetype_tags`); an empty list when the
		attribute is absent. Errors reading the instance tags propagate.

		@input sx (sonador.imaging.orthanc.ImagingSeries or series-like object)
		@returns list of str
	'''
	return dcm_image_type(dcm_series_imagetype_tags(sx))


def _classify_series(sx, retries=1):
	'''	Classification of a series resource with the tags it was read from: (derived, tags). `derived` is
		None and `tags` is None when the tags could not be read after `retries` further attempts; no read
		is attempted beyond those.
	'''
	attempts = max(int(retries or 0), 0) + 1
	for attempt in range(1, attempts + 1):
		try:
			tags = dcm_series_imagetype_tags(sx)
			return dcm_tags_are_derived(tags), tags
		except Exception as err:
			if attempt < attempts:
				logger.warning('series=%s: Unable to read the instance tags of the series (attempt %d of %d), retrying. '
					'Error: "%s"' % (getattr(sx, 'pk', None), attempt, attempts, err))
				continue
			logger.error('series=%s: Unable to read the instance tags of the series (attempt %d of %d), the series '
				'cannot be classified as primary or derived imaging. Error: "%s"'
				% (getattr(sx, 'pk', None), attempt, attempts, err))
			return None, None


def dcm_series_is_derived(sx, retries=1):
	'''	Whether a series resource is derived from other images (`dcm_tags_are_derived` over
		`dcm_series_imagetype_tags`).

		The outcome is three-valued: a series whose tags cannot be read (after `retries` further
		attempts) is unknown, so that a read failure is never reported as primary imaging. No read is
		attempted beyond the configured attempts. The callers decide what an unknown series means for
		them (`dcm_exclude_derived_series`, `dcm_check_primary_series`).

		@input sx (sonador.imaging.orthanc.ImagingSeries or series-like object)
		@input retries (int, default=1): further attempts to read the instance tags after a failure
		@returns True (derived), False (primary) or None (unknown: the tags could not be read)
	'''
	return _classify_series(sx, retries=retries)[0]


def _scope(modalities):
	'''	Modality scope of an operation, materialised once: None (every series) or a tuple. `modalities`
		may be any iterable, including a one-use iterator, and is read here only.
	'''
	return None if modalities is None else tuple(modalities)


def _series_in_scope(sx, scope):
	'''	Whether a series is subject to the classification: every series when `scope` is None, else the
		series whose modality is listed
	'''
	return scope is None or getattr(sx, 'modality', None) in scope


def _image_type_label(tags):
	'''	Image Type of tags already read, for the messages; "(unknown)" when there are none
	'''
	return DCM_MULTIVALUE_SEPARATOR.join(dcm_image_type(tags)) or '(unknown)'


def _context(context):
	return ('%s: ' % context) if context else ''


def dcm_exclude_derived_series(series_list, modalities=None, unknown=SERIES_UNKNOWN_ERROR, retries=1, context=None):
	'''	Series of a list without its derived series: a series in scope (every series when `modalities` is
		None, else the series of the listed modalities) is kept when `dcm_series_is_derived` is False and
		left out when it is True; series out of scope are kept as they are. The order of the list is
		preserved. `modalities` may be any iterable; it is read once.

		A series whose tags cannot be read is of unknown kind. By default the listing stops with an error,
		because a derived object linked to a display copy or another derived series is wrong and cannot
		be corrected once uploaded; `unknown` may instead leave such series out (`skip`) or keep them
		(`keep`). A kept unknown series is still unclassified: `dcm_check_primary_series` rejects it.

		@input series_list (iterable of sonador.imaging.orthanc.ImagingSeries or series-like objects)
		@input modalities (iterable of str, default=None): modalities subject to the check; None checks every series
		@input unknown (str, default=SERIES_UNKNOWN_ERROR): what to do with a series that cannot be
			classified: `error`, `skip` or `keep`
		@input retries (int, default=1): see `dcm_series_is_derived`
		@input context (str, default=None): prefix of the log and error messages (a case or job identifier)
		@returns list
		@raises ValueError when a series cannot be classified and `unknown` is `error`, or `unknown` is invalid
	'''
	if unknown not in SERIES_UNKNOWN_OPTIONS:
		raise ValueError('%sInvalid `unknown` option %r for the series classification, expected one of %s'
			% (_context(context), unknown, ', '.join(SERIES_UNKNOWN_OPTIONS)))
	scope = _scope(modalities)

	kept = []
	for sx in series_list or ():
		if sx is None:
			continue
		if not _series_in_scope(sx, scope):
			kept.append(sx)
			continue

		derived, tags = _classify_series(sx, retries=retries)
		if derived is None:
			if unknown == SERIES_UNKNOWN_ERROR:
				raise ValueError('%sUnable to tell whether %s series %s is primary or derived imaging, its tags could '
					'not be read. The primary imaging check cannot be completed.'
					% (_context(context), getattr(sx, 'modality', None), getattr(sx, 'pk', None)))
			logger.warning('%s%s series %s cannot be classified as primary or derived imaging, the series is %s'
				% (_context(context), getattr(sx, 'modality', None), getattr(sx, 'pk', None),
					'left out' if unknown == SERIES_UNKNOWN_SKIP else 'kept unclassified'))
			if unknown == SERIES_UNKNOWN_KEEP:
				kept.append(sx)
			continue

		if derived:
			logger.info('%sDerived %s series %s is left out (ImageType=%s description="%s"), only primary imaging is kept'
				% (_context(context), getattr(sx, 'modality', None), getattr(sx, 'pk', None),
					_image_type_label(tags), getattr(sx, 'description', None)))
			continue

		kept.append(sx)

	return kept


def dcm_primary_series_list(series_list, modalities=None, unknown=SERIES_UNKNOWN_ERROR, retries=1, context=None):
	'''	Primary series of a list: the series in scope (`modalities`) that are not derived
		(`dcm_exclude_derived_series`). Series out of scope are left out as well, so the result holds the
		imaging a derived object may be linked to and nothing else, with one exception: with
		`unknown='keep'` the series of the scope whose tags could not be read are kept unclassified, and
		`dcm_check_primary_series` rejects them. The order of the list is preserved; `modalities` may be
		any iterable and is read once.

		@input series_list (iterable of sonador.imaging.orthanc.ImagingSeries or series-like objects)
		@input modalities (iterable of str, default=None): modalities of the result; None keeps every modality
		@input unknown (str, default=SERIES_UNKNOWN_ERROR): see `dcm_exclude_derived_series`
		@input retries (int, default=1): see `dcm_series_is_derived`
		@input context (str, default=None): prefix of the log and error messages
		@returns list
		@raises ValueError when a series cannot be classified and `unknown` is `error`
	'''
	scope = _scope(modalities)
	return [sx for sx in dcm_exclude_derived_series(
			series_list, modalities=scope, unknown=unknown, retries=retries, context=context)
		if _series_in_scope(sx, scope)]


def dcm_check_primary_series(sx, references=None, modalities=None, retries=1, context=None):
	'''	Guard of a link: verify that the series a derived object is linked to (`sx`) and every series
		resource it references (`references`) are primary imaging whenever they are in scope
		(`modalities`, any iterable, read once). A derived series (a display copy, a computed image) or a
		series that cannot be classified raises a ValueError, so that a writer can stop before anything
		is written: a wrong link cannot be corrected once uploaded.

		References given as Series Instance UIDs (str) or series keys (DicomMetaKey) carry no tags and are
		not checked (resolve them to series resources first when they must be verified). Every other
		object in scope is a series resource and is classified from its series tags or its first
		instance's tags; an object that offers neither cannot be classified and is rejected like an
		unreadable one, so a silent pass never certifies an unchecked resource. `references` is read
		once; pass a list when the same references are needed afterwards.

		@input sx (sonador.imaging.orthanc.ImagingSeries, series-like object or None): linked series
		@input references (iterable, default=None): referenced series resources, UIDs or keys
		@input modalities (iterable of str, default=None): modalities subject to the check; None checks every series resource
		@input retries (int, default=1): see `dcm_series_is_derived`
		@input context (str, default=None): prefix of the error message
		@returns None
		@raises ValueError
	'''
	scope = _scope(modalities)
	checked = [('linked', sx)] + [('referenced', _sx) for _sx in (references or ())]
	for role, series in checked:
		if series is None or isinstance(series, (str, DicomMetaKey)) or not _series_in_scope(series, scope):
			continue

		derived, tags = _classify_series(series, retries=retries)
		if derived is None:
			raise ValueError('%sUnable to tell whether the %s %s series %s is primary or derived imaging, its tags could '
				'not be read. Only primary imaging may be linked.'
				% (_context(context), role, getattr(series, 'modality', None), getattr(series, 'pk', None)))
		if derived:
			raise ValueError('%sThe %s %s series %s is derived imaging (ImageType=%s description="%s"), only primary '
				'imaging may be linked.'
				% (_context(context), role, getattr(series, 'modality', None), getattr(series, 'pk', None),
					_image_type_label(tags), getattr(series, 'description', None)))
