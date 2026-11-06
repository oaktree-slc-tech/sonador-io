'''	Coded series attributes: read and write the coded concepts of the Series Description Code Sequence
	(0008,103F; General Series module, PS3.3 C.7.3, Type 3), the series level carrier of codes that
	describe what a series represents.

	The sequence may hold several items here, which goes beyond the standard: PS3.3 C.7.3 permits a single
	Item. This is an explicit exception authorized by the maintainer on 2026-11-06: Sonador uses the
	sequence as a list of the coded attributes of a series, deliberately. The items
	already present are kept first and new codes are added after them, so a reader that tolerates
	additional items and uses only the first sees the value it saw before. That order is all this
	guarantees: other implementations may reject the object or discard the additional items, and strict
	validators may report them. A reader that needs a particular code looks through every item
	(`dcm_series_description_codes`, `dcm_code_in`) and never relies on the position of a code.

	A coded sequence has two forms: a pydicom Sequence of items in a local dataset, and a list of
	dictionaries in the simplified tags of the imaging server. The readers accept both.

	The imaging server does not index the sequence at the series level unless it is configured to, and
	only for data ingested after that. `dcm_series_tags` reads the series tags (`dicomdata`) when they
	carry the attribute and the tags of the first instance (`dcm0.tags`) otherwise. For the Sonador models
	a cold series fetches its instance collection and then the instance tags from the server, and caches
	both on the resource.

	The sequence is never copied between objects through the simplified tags: a list of dictionaries
	assigned to a dataset is not a valid element value. Writers start from `Code` objects, or from the
	dataset that holds the sequence.

	@fn dcm_coded_concepts: coded concepts of a sequence, in either form, or of codes
	@fn dcm_code_in: whether a code is among a list of coded concepts
	@fn dcm_series_tags: the tags an attribute of a series is read from
	@fn dcm_series_description_codes: coded concepts of the Series Description Code Sequence
	@fn dcm_merge_series_description_codes: a new sequence with codes added after the items of another
	@fn dcm_add_series_description_codes: add codes after the items of the sequence of a dataset
'''
import copy

from collections.abc import Mapping

from pydicom.dataset import Dataset
from pydicom.sequence import Sequence

from ...apisettings import DCMHEADER_SERIES_DESCRIPTION_CODE_SEQUENCE, DCMHEADER_CODING_SCHEME_DESIGNATOR, \
	DCMHEADER_CODING_SCHEME_VERSION, DCMHEADER_CODE_MEANING, Code, CodedConcept, dcm_code_value, \
	dcm_code_matches, srcode2dataset
from .headers import dcm_header_value


def _coded_concept(item):
	'''	Coded concept of a `Code`, an item dataset or a simplified-tags dictionary.

		@returns CodedConcept, None for an item without a code value or a scheme designator
	'''
	if isinstance(item, Code):
		value, designator, meaning, version = item.value, item.scheme_designator, item.meaning, item.scheme_version
	elif isinstance(item, (Dataset, Mapping)):
		value = dcm_code_value(item)
		designator = dcm_header_value(item, DCMHEADER_CODING_SCHEME_DESIGNATOR)
		meaning = dcm_header_value(item, DCMHEADER_CODE_MEANING)
		version = dcm_header_value(item, DCMHEADER_CODING_SCHEME_VERSION)
	else:
		return None

	if not str(value or '').strip() or not str(designator or '').strip():
		return None

	return CodedConcept(str(value), str(designator), str(meaning or ''), scheme_version=str(version) if version else None)


def dcm_coded_concepts(items):
	'''	Coded concepts of a coded sequence or of a collection of codes: a pydicom Sequence, the
		simplified-tags form of the imaging server (a list of dictionaries with `CodeValue`,
		`CodingSchemeDesignator`, `CodingSchemeVersion` and `CodeMeaning`), any iterable of `Dataset`,
		`CodedConcept`, `Code` or dictionaries, or one such item. `LongCodeValue` and `URNCodeValue` are
		read as well. The meaning and the version are optional (a missing meaning is read as an empty
		string). Items without a code value or a scheme designator are skipped, as is anything that is not
		a coded item. An iterable is read once.

		@input items: see above; None gives an empty list
		@returns list of CodedConcept
	'''
	if items is None or isinstance(items, (str, bytes)):
		return []
	if isinstance(items, (Code, Dataset, Mapping)):
		items = [items]

	concepts = (_coded_concept(item) for item in items)
	return [concept for concept in concepts if concept is not None]


def dcm_code_in(codes, code):
	'''	Whether a code is among a list of coded concepts: same code value and scheme designator, whatever
		the scheme version and the meaning. Both sides need a complete identity: a code without a code
		value or a scheme designator matches nothing, and an item without one is never matched.

		@input codes (any form read by `dcm_coded_concepts`): the concepts searched; read once
		@input code (Code, CodedConcept, Dataset or dict): the code looked for
		@returns bool
	'''
	expected = _coded_concept(code)
	if expected is None:
		return False

	return any(dcm_code_matches(concept, expected) for concept in dcm_coded_concepts(codes))


def dcm_series_tags(sx, keyword):
	'''	Tags an attribute of a series is read from: the series tags (`dicomdata`) when they carry the
		attribute, else the tags of the first instance (`dcm0.tags`; a cold Sonador model fetches the
		instance collection and the instance tags from the server and caches them, see the module notes).
		Errors reading the instance tags propagate.

		@input sx (sonador.imaging.orthanc.ImagingSeries or series-like object): a series-like object
			may carry `dicomdata`, `dcm0` or both
		@input keyword (str): DICOM keyword of the attribute
		@returns dict, {} when the object offers neither source or the first instance has no tags
	'''
	series_tags = getattr(sx, 'dicomdata', None) or {}
	if keyword in series_tags:
		return series_tags

	dcm0 = getattr(sx, 'dcm0', None)
	return (dcm0.tags if dcm0 is not None else None) or {}


def dcm_series_description_codes(source):
	'''	Coded concepts of the Series Description Code Sequence, in the order of the items.

		@input source (pydicom.Dataset, dict of tags or series resource): a dataset, the tags of a series
			or instance (`dicomdata`, `tags`), or a series resource, read through `dcm_series_tags`
		@returns list of CodedConcept, empty when the attribute is absent
	'''
	if source is None:
		return []

	tags = source if isinstance(source, (Dataset, Mapping)) \
		else dcm_series_tags(source, DCMHEADER_SERIES_DESCRIPTION_CODE_SEQUENCE)
	return dcm_coded_concepts(tags.get(DCMHEADER_SERIES_DESCRIPTION_CODE_SEQUENCE))


def dcm_merge_series_description_codes(existing, codes):
	'''	New sequence holding copies of the `existing` items followed by each code that is not among them
		(the sequence may exceed the single item of the standard, see the module notes). A code already
		present, by code value and scheme designator, is not added again, so merging is idempotent.
		Neither `existing` nor its items are changed: the result can be handed to `dcmheader_modify`, which
		records the sequence the dataset held as the previous value.

		@input existing (pydicom Sequence or iterable of Dataset, default=None): the items kept. Items
			the readers skip are kept as well.
		@input codes (Code, CodedConcept, or an iterable of them, see `dcm_coded_concepts`): codes to add
		@returns pydicom.sequence.Sequence
	'''
	sequence = Sequence([copy.deepcopy(item) for item in (existing or ())])

	present = dcm_coded_concepts(sequence)
	for code in dcm_coded_concepts(codes):
		if not dcm_code_in(present, code):
			sequence.append(srcode2dataset(code))
			present.append(code)

	return sequence


def dcm_add_series_description_codes(ds, codes):
	'''	Add codes to the Series Description Code Sequence of a dataset, after the items it already holds
		(`dcm_merge_series_description_codes`). The new Sequence is assigned to `ds`: the Sequence the
		dataset held before, and its items, are left as they were. Nothing is assigned when the result has
		no items.

		@input ds (pydicom.Dataset): dataset to update
		@input codes (Code, CodedConcept, or an iterable of them, see `dcm_coded_concepts`): codes to add
		@returns pydicom.sequence.Sequence: the sequence of `ds`
	'''
	sequence = dcm_merge_series_description_codes(ds.get(DCMHEADER_SERIES_DESCRIPTION_CODE_SEQUENCE), codes)
	if len(sequence):
		setattr(ds, DCMHEADER_SERIES_DESCRIPTION_CODE_SEQUENCE, sequence)

	return sequence
