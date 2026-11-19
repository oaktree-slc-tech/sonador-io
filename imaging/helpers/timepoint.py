'''	Reader and writer of the series-level timepoint of a DICOM dataset.

	A timepoint is a bucket inside one study that groups series. It is carried by an item of Referenced Performed
	Procedure Step Sequence (0008,1111) whose Referenced SOP Class UID is the Sonador timepoint class UID
	(`SONADOR_TIMEPOINT_SOP_CLASS_UID`), with a copy of its descriptive values in the Performed Procedure Step
	attributes of the series. The item is the record: the series-level copy is never read as the source of a
	timepoint. The encoding and its rules are the contract of oak-tree/medical-imaging/sonador-client#50.

	The encoding departs from the DICOM standard in five ways, by design:

	1. The standard defines the sequence as a reference to the Performed Procedure Step during which the series was
	   created. A timepoint item references no stored object.
	2. The item's Referenced SOP Class UID is a Sonador constant, not the Modality Performed Procedure Step SOP Class.
	3. The item carries four attributes the SOP Instance Reference macro (PS3.3 Table 10-11) does not define:
	   Performed Procedure Step ID, Start Date, Start Time and Description.
	4. On a series that already holds an item, the timepoint item is a second item where the standard permits one.
	5. On an SR document the four series-level attributes are additional Type 3 attributes (PS3.2 7.3), and their
	   meaning is the timepoint's, not a procedure step's.

	This module is the only one that reads or writes the sequence. It sits below the report models and the imaging
	server models and imports neither.

	@fn dcm_timepoint_items: the timepoint items of a dataset, with their positions
	@fn dcm_timepoint: the timepoint of a dataset or of a mapping of tags
	@fn dcm_encode_timepoint_item: the sequence item of a timepoint
	@fn dcm_set_timepoint: write the timepoint item, and the series-level copy when the caller allows it
	@fn dcm_remove_timepoint: remove every timepoint item
	@fn dcm_copy_foreign_items: copy the items that are not timepoint items from one dataset to another
	@fn timepoint_diagnostic: record a timepoint condition once per path and value
'''
import copy
from collections.abc import Mapping

from pydicom import config as dcmconfig
from pydicom.dataelem import DataElement
from pydicom.datadict import tag_for_keyword, dictionary_VR
from pydicom.dataset import Dataset
from pydicom.sequence import Sequence

from ...apisettings.base import Timepoint, timepoint_date_valid, timepoint_time_valid, SONADOR_TIMEPOINT_SOP_CLASS_UID, \
	DCMHEADER_REFERENCED_PERFORMED_PROCEDURE_STEP_SEQUENCE, DCMHEADER_SR_SOP_CLASS_UID, DCMHEADER_SR_REF_INSTANCE_UID, \
	DCMHEADER_PERFORMED_PROCEDURE_STEP_ID, DCMHEADER_PERFORMED_PROCEDURE_STEP_START_DATE, \
	DCMHEADER_PERFORMED_PROCEDURE_STEP_START_TIME, DCMHEADER_PERFORMED_PROCEDURE_STEP_DESCRIPTION, \
	DCMHEADER_TIMEPOINT_MIRROR, DCMHEADER_SOP_INSTANCE_UID
from ...apisettings.sr import sr_diagnostic, SRDiagnostic, SRDiagnosticError, SR_DIAGNOSTIC_WARNING, \
	SR_CONDITION_TIMEPOINT_MULTIPLE_ITEMS, SR_CONDITION_TIMEPOINT_MISSING_UID, SR_CONDITION_TIMEPOINT_INVALID_DATE
from .headers import dcm_header_value, dcm_header_uid

# Timepoint field by the keyword that carries it, in the item and in the series-level copy
_TIMEPOINT_FIELDS = (
	(DCMHEADER_PERFORMED_PROCEDURE_STEP_ID, 'id'),
	(DCMHEADER_PERFORMED_PROCEDURE_STEP_START_DATE, 'date'),
	(DCMHEADER_PERFORMED_PROCEDURE_STEP_START_TIME, 'time'),
	(DCMHEADER_PERFORMED_PROCEDURE_STEP_DESCRIPTION, 'description'),
)


def timepoint_diagnostic(diagnostics, condition, message, path=None, value=None, strict=False):
	'''	Record a timepoint condition as a `warning`. A condition already in the list with the same path and value is
		not recorded again, so that repeated reads of one document do not repeat it; under `strict` it is raised either way.

		@input diagnostics (list or None): list the diagnostic is appended to
		@input condition (str): condition code (`SR_CONDITION_TIMEPOINT_*`)
		@input message (str): description
		@input path (str, default=None): where the condition was met
		@input value (default=None): offending value
		@input strict (bool, default=False): raise SRDiagnosticError

		@returns SRDiagnostic
		@raises SRDiagnosticError under `strict`
	'''
	for known in (diagnostics or ()):
		if known.condition == condition and known.path == path and known.value == value:
			if strict:
				raise SRDiagnosticError(known)
			return known
	return sr_diagnostic(diagnostics, SR_DIAGNOSTIC_WARNING, condition, message, path=path, value=value, strict=strict)


def _sequence_items(headers):
	'''	Items of the Referenced Performed Procedure Step Sequence of a dataset or a mapping of tags, as a list. A
		sequence delivered as one item reads the same as one delivered as a list of items.

		@input headers (pydicom.Dataset or Mapping): headers read
		@returns list of pydicom.Dataset or Mapping
	'''
	value = dcm_header_value(headers, DCMHEADER_REFERENCED_PERFORMED_PROCEDURE_STEP_SEQUENCE)
	if value is None:
		return []
	if isinstance(value, (Mapping, Dataset)):
		return [value]
	return [item for item in value if isinstance(item, (Mapping, Dataset))]


def _is_timepoint_item(item):
	return dcm_header_uid(item, DCMHEADER_SR_SOP_CLASS_UID) == SONADOR_TIMEPOINT_SOP_CLASS_UID


def _text(item, name):
	'''	An optional value of a timepoint item as text, as the document holds it: None when the item does not hold the
		attribute, the empty string when it holds it without a value. Presence is kept, so that a value read is written
		back as it was and members that differ in presence are told apart.
	'''
	if name not in item:
		return None
	value = item.get(name)
	return '' if value is None else str(value)


def dcm_timepoint_items(headers):
	'''	Timepoint items of a dataset: the items of Referenced Performed Procedure Step Sequence whose Referenced SOP
		Class UID is the timepoint class UID, at any position and among any number of other items.

		@input headers (pydicom.Dataset or Mapping): a dataset, or a mapping of tags by keyword (the simplified tags of
			an imaging server instance)
		@returns list of (int, item): position in the sequence and the item, in sequence order
	'''
	return [(position, item) for position, item in enumerate(_sequence_items(headers)) if _is_timepoint_item(item)]


def dcm_timepoint(headers, diagnostics=None, strict=False):
	'''	Timepoint of a dataset, read from its timepoint item. The item is selected by its class UID, never by
		position or item count; series-level Performed Procedure Step values alone never yield a timepoint. The value
		returned holds what the document holds and is not checked: an optional attribute the item holds without a value
		is the empty string, one it does not hold is None.

		Conditions, each a `warning` (raised under `strict`): several timepoint items, of which the first is used
		(`timepoint-multiple-items`); a first timepoint item without a UID, for which None is returned and no later
		item is used (`timepoint-missing-uid`); a date that is not eight digits or a time that is not a TM value, whose
		text is kept (`timepoint-invalid-date`).

		@input headers (pydicom.Dataset or Mapping): a dataset, or a mapping of tags by keyword
		@input diagnostics (list, default=None): list the conditions are appended to
		@input strict (bool, default=False): raise SRDiagnosticError on a condition

		@returns Timepoint, or None for a dataset without a timepoint
	'''
	items = dcm_timepoint_items(headers)
	if not items:
		return None

	sop_uid = dcm_header_uid(headers, DCMHEADER_SOP_INSTANCE_UID)
	path = 'instance %s' % sop_uid if sop_uid else None
	if len(items) > 1:
		timepoint_diagnostic(diagnostics, SR_CONDITION_TIMEPOINT_MULTIPLE_ITEMS,
			'%d timepoint items; the first is used' % len(items), path=path, value=len(items), strict=strict)

	position, item = items[0]
	uid = dcm_header_uid(item, DCMHEADER_SR_REF_INSTANCE_UID)
	if not uid:
		timepoint_diagnostic(diagnostics, SR_CONDITION_TIMEPOINT_MISSING_UID,
			'timepoint item %d has no Referenced SOP Instance UID; the dataset has no timepoint' % position,
			path=path, strict=strict)
		return None

	timepoint = Timepoint(uid, **{field: _text(item, name) for name, field in _TIMEPOINT_FIELDS})
	# A date or time held without a value is absent for ordering and is not an invalid value
	if timepoint.date and not timepoint_date_valid(timepoint.date):
		timepoint_diagnostic(diagnostics, SR_CONDITION_TIMEPOINT_INVALID_DATE,
			'timepoint date "%s" is not eight digits; the text is kept' % timepoint.date, path=path, value=timepoint.date,
			strict=strict)
	if timepoint.time and not timepoint_time_valid(timepoint.time):
		timepoint_diagnostic(diagnostics, SR_CONDITION_TIMEPOINT_INVALID_DATE,
			'timepoint time "%s" is not a DICOM TM value; the text is kept' % timepoint.time, path=path, value=timepoint.time,
			strict=strict)
	return timepoint


def _set_value(dataset, name, value):
	'''	Set a text attribute as given. A value longer than its value representation allows is written with pydicom's
		warning and is never refused here: the caller decides what is checked.
	'''
	tag = tag_for_keyword(name)
	dataset[tag] = DataElement(tag, dictionary_VR(tag), str(value), validation_mode=dcmconfig.WARN)


def dcm_encode_timepoint_item(timepoint):
	'''	Sequence item of a timepoint: the timepoint class UID, the timepoint UID and the optional values it holds. An
		optional value that is the empty string is written as an attribute without a value; None writes no attribute.

		@input timepoint (Timepoint): the timepoint, written as given
		@returns pydicom.Dataset
		@raises ValueError for a timepoint without a UID
	'''
	if timepoint is None or not timepoint.uid:
		raise ValueError('A timepoint item needs a timepoint with a UID')
	item = Dataset()
	setattr(item, DCMHEADER_SR_SOP_CLASS_UID, SONADOR_TIMEPOINT_SOP_CLASS_UID)
	setattr(item, DCMHEADER_SR_REF_INSTANCE_UID, str(timepoint.uid))
	for name, field in _TIMEPOINT_FIELDS:
		value = getattr(timepoint, field)
		if value is not None:
			_set_value(item, name, value)
	return item


def _dataset_items(dataset):
	'''	Items of the sequence of a pydicom dataset, as a list (empty when the sequence is absent) '''
	return list(dataset.get(DCMHEADER_REFERENCED_PERFORMED_PROCEDURE_STEP_SEQUENCE) or [])


def dcm_set_timepoint(dataset, timepoint, mirror=None, replace=False, validate=True):
	'''	Write the timepoint item of a dataset. Items that are not timepoint items stay in place, in order, with every
		attribute unchanged; the timepoint item is appended after them. A timepoint item with the same UID is replaced
		by the new values, so an optional value the timepoint lacks is no longer in the item.

		@input dataset (pydicom.Dataset): dataset written, in place
		@input timepoint (Timepoint): the timepoint
		@input mirror (bool, default=None): the series-level copy (Performed Procedure Step ID, Start Date, Start Time
			and Description of the dataset). None writes the copy only when the dataset holds none of the four
			attributes, one present with an empty value counting as held. True states that the caller owns the copy:
			the four attributes are written from the timepoint and one the timepoint lacks is removed. False leaves
			the four attributes untouched.
		@input replace (bool, default=False): remove every timepoint item the dataset holds, whatever its UID, and
			write this one
		@input validate (bool, default=True): check the timepoint first (`Timepoint.validate`). False writes the values
			as they are, for a value read from a document and written back unchanged.

		@returns pydicom.Dataset: `dataset`
		@raises ValueError for a timepoint `validate` refuses, for one without a UID, and, unless `replace`, for a
			dataset that holds a timepoint item with another UID or more than one timepoint item
	'''
	if not isinstance(timepoint, Timepoint):
		raise ValueError('A Timepoint is required, not %s' % type(timepoint).__name__)
	if validate:
		timepoint.validate()
	item = dcm_encode_timepoint_item(timepoint)

	items = _dataset_items(dataset)
	held = [existing for existing in items if _is_timepoint_item(existing)]
	if not replace:
		if len(held) > 1:
			raise ValueError('The dataset holds %d timepoint items; pass replace=True to write one in their place' % len(held))
		if held and dcm_header_uid(held[0], DCMHEADER_SR_REF_INSTANCE_UID) != str(timepoint.uid):
			raise ValueError('The dataset holds timepoint %s; pass replace=True to write timepoint %s in its place'
				% (dcm_header_uid(held[0], DCMHEADER_SR_REF_INSTANCE_UID), timepoint.uid))

	setattr(dataset, DCMHEADER_REFERENCED_PERFORMED_PROCEDURE_STEP_SEQUENCE,
		Sequence([existing for existing in items if not _is_timepoint_item(existing)] + [item]))

	if mirror is None:
		mirror = not any(name in dataset for name in DCMHEADER_TIMEPOINT_MIRROR)
		if not mirror:
			return dataset
	if mirror:
		for name, field in _TIMEPOINT_FIELDS:
			value = getattr(timepoint, field)
			if value is not None:
				_set_value(dataset, name, value)
			elif name in dataset:
				delattr(dataset, name)
	return dataset


def dcm_remove_timepoint(dataset, mirror=False):
	'''	Remove every timepoint item of a dataset. The sequence stays in place, empty when no other item remains, and
		is created empty on a dataset without one.

		@input dataset (pydicom.Dataset): dataset written, in place
		@input mirror (bool, default=False): True states that the caller owns the series-level copy, and the four
			Performed Procedure Step attributes of the dataset are removed as well. Nothing is removed because it
			equals the item.

		@returns pydicom.Dataset: `dataset`
	'''
	setattr(dataset, DCMHEADER_REFERENCED_PERFORMED_PROCEDURE_STEP_SEQUENCE,
		Sequence([existing for existing in _dataset_items(dataset) if not _is_timepoint_item(existing)]))
	if mirror:
		for name in DCMHEADER_TIMEPOINT_MIRROR:
			if name in dataset:
				delattr(dataset, name)
	return dataset


def dcm_copy_foreign_items(source, dataset):
	'''	Copy the items of a dataset's sequence that are not timepoint items into the sequence of another, deep-copied
		and in order, after the other items the destination already holds and before any timepoint item. A source
		without such an item leaves the destination as it is.

		@input source (pydicom.Dataset): dataset the items are read from
		@input dataset (pydicom.Dataset): dataset written, in place

		@returns pydicom.Dataset: `dataset`
		@raises TypeError for a source or a destination that is not a pydicom dataset (a mapping of tags holds no
			items that can be written)
	'''
	for value in (source, dataset):
		if not isinstance(value, Dataset):
			raise TypeError('Sequence items are copied between pydicom datasets, not %s' % type(value).__name__)
	foreign = [copy.deepcopy(item) for item in _sequence_items(source) if not _is_timepoint_item(item)]
	if not foreign:
		return dataset
	items = _dataset_items(dataset)
	setattr(dataset, DCMHEADER_REFERENCED_PERFORMED_PROCEDURE_STEP_SEQUENCE, Sequence(
		[existing for existing in items if not _is_timepoint_item(existing)] + foreign
		+ [existing for existing in items if _is_timepoint_item(existing)]))
	return dataset
