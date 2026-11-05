'''	Writers of the Referenced Series Sequence: the items that link a derived DICOM object (a model, a
	secondary capture, a report, a resampled volume) to the series it was derived from. The matching
	readers (`dcm_referenced_series_uids`) live in `headers`; the writers sit at the same level, below
	the remote models, and accept the model objects by duck typing.

	A reference is anything that names a series: a series resource (any object with a `series_uid`,
	so imaging, SR, SEG and encapsulated-document series alike), a series `DicomMetaKey`, or a Series
	Instance UID string. An item is written in one of two forms:

	- with `reference_instances=True` (the default) the item carries the Series Instance UID and a
	  Referenced Instance Sequence of SOP Class and SOP Instance UIDs, the form of the Common Instance
	  Reference module (PS3.3 C.12.2). The module asks for one or more referenced instances, not for
	  the whole series; listing every instance of the series is this helper's default, read from the
	  resource (`instances_collection`, else `slices_collection`), and `instances` selects the subset
	  to list instead;
	- with `reference_instances=False` the item carries the Series Instance UID only and no Referenced
	  Instance Sequence (one already present in `dcm_mdata` is removed). The Sonador readers resolve
	  references by series UID, and the instance list repeats every slice of every source series on
	  every derived instance, so a pipeline that writes many derived instances may choose this form.
	  It departs from the module's requirements; the choice is the caller's.

	@fn dcm_reference_series_uid: Series Instance UID of a reference
	@fn dcm_encode_instance_reference: Referenced Instance Sequence item of an instance
	@fn dcm_encode_series_reference: Referenced Series Sequence item of a reference
	@fn dcm_reference_series_list: the references of a derived object, the linked series first, each once
	@fn dcm_encode_reference_series_seq: Referenced Series Sequence of a list of references
'''
from pydicom.dataset import Dataset
from pydicom.sequence import Sequence

from ...apisettings import DicomMetaKey, IMAGING_SERVER_RESOURCE_SERIES, \
	DCMHEADER_SERIES_INSTANCE_UID, DCMHEADER_SR_REF_INSTANCE_SEQ, DCMHEADER_SR_SOP_CLASS_UID, DCMHEADER_SR_REF_INSTANCE_UID, \
	DCMHEADER_SOP_CLASS_UID, DCMHEADER_SOP_INSTANCE_UID
from .headers import dcm_header_uid


# Default form of a series reference: the Common Instance Reference form, with the instances listed
DCM_REFERENCE_INSTANCES_DEFAULT = True


def dcm_reference_series_uid(reference):
	'''	Series Instance UID of a reference.

		@input reference: a series resource (any object with a `series_uid`), a `DicomMetaKey` of a
			series, or a Series Instance UID string
		@returns str
		@raises ValueError when no UID can be read from the reference
	'''
	if isinstance(reference, DicomMetaKey):
		if reference.resource == IMAGING_SERVER_RESOURCE_SERIES and reference.uid and str(reference.uid).strip():
			return str(reference.uid).strip()
		raise ValueError('Unable to determine the series UID of reference %r: a series DicomMetaKey with a UID '
			'is required' % (reference,))

	if isinstance(reference, str):
		if reference.strip():
			return reference.strip()
		raise ValueError('Unable to determine the series UID of an empty reference')

	series_uid = getattr(reference, 'series_uid', None)
	if series_uid is not None and str(series_uid).strip():
		return str(series_uid).strip()

	raise ValueError('Unable to determine the series UID of reference %r: a series resource, a series DicomMetaKey '
		'or a UID string is required' % (reference,))


def dcm_reference_instances(reference, instances=None):
	'''	Instances of a referenced series, for the Referenced Instance Sequence of its item: `instances`
		when given, else the instances of the resource (`instances_collection`, else `slices_collection`).

		@input reference: series resource
		@input instances (iterable, default=None): instances to list instead of the resource's
		@returns list
		@raises ValueError when the reference cannot supply instances (a UID or a DicomMetaKey)
	'''
	if instances is not None:
		return list(instances)

	for attr in ('instances_collection', 'slices_collection'):
		collection = getattr(reference, attr, None)
		if collection is not None:
			return list(collection)

	raise ValueError('Unable to reference the instances of series %s: a series resource with an instance '
		'collection (or an explicit `instances` list) is required when reference_instances=True'
		% dcm_reference_series_uid(reference))


def dcm_encode_instance_reference(instance, dcm_mdata=None):
	'''	Create a Referenced Instance Sequence item (Referenced SOP Class UID, Referenced SOP Instance UID)
		for an instance.

		@input instance: an instance resource (`sop_class_uid`, `sop_instance_uid`), or a dataset or
			dictionary of tags carrying SOPClassUID and SOPInstanceUID
		@input dcm_mdata (pydicom.dataset.Dataset, default=blank dataset): dataset the item is written to
		@returns pydicom.dataset.Dataset
		@raises ValueError when the instance carries no SOP Class UID or no SOP Instance UID
	'''
	sop_class = getattr(instance, 'sop_class_uid', None)
	sop_instance = getattr(instance, 'sop_instance_uid', None)
	if sop_class is None and sop_instance is None:
		sop_class = dcm_header_uid(instance, DCMHEADER_SOP_CLASS_UID)
		sop_instance = dcm_header_uid(instance, DCMHEADER_SOP_INSTANCE_UID)

	# Normalize before validating, so that a blank resource value is rejected like a blank tag value
	sop_class = str(sop_class).strip() if sop_class is not None else ''
	sop_instance = str(sop_instance).strip() if sop_instance is not None else ''
	if not sop_class or not sop_instance:
		raise ValueError('Unable to reference instance %r: a SOP Class UID and a SOP Instance UID are required'
			% (instance,))

	dcm_mdata = dcm_mdata if dcm_mdata is not None else Dataset()
	setattr(dcm_mdata, DCMHEADER_SR_SOP_CLASS_UID, sop_class)
	setattr(dcm_mdata, DCMHEADER_SR_REF_INSTANCE_UID, sop_instance)

	return dcm_mdata


def dcm_encode_series_reference(reference, reference_instances=DCM_REFERENCE_INSTANCES_DEFAULT, dcm_mdata=None,
		instances=None):
	'''	Create a Referenced Series Sequence item for a reference.

		@input reference: the referenced series (see `dcm_reference_series_uid`)
		@input reference_instances (bool, default=True): when True the item also carries a Referenced
			Instance Sequence (the Common Instance Reference form), listing every instance of the series
			unless `instances` selects a subset; when False it carries the Series Instance UID only, and a
			Referenced Instance Sequence already present in `dcm_mdata` is removed so that the item cannot
			pair the series with the instances of another
		@input dcm_mdata (pydicom.dataset.Dataset, default=blank dataset): dataset the item is written to;
			other elements it carries are kept
		@input instances (iterable, default=the instances of the resource): instances to list when
			`reference_instances` is True (see `dcm_encode_instance_reference` for the accepted types)

		@returns pydicom.dataset.Dataset
		@raises ValueError when the reference names no series, or when its instances are requested and
			cannot be read
	'''
	dcm_mdata = dcm_mdata if dcm_mdata is not None else Dataset()
	setattr(dcm_mdata, DCMHEADER_SERIES_INSTANCE_UID, dcm_reference_series_uid(reference))

	if reference_instances:
		setattr(dcm_mdata, DCMHEADER_SR_REF_INSTANCE_SEQ, Sequence(
			[dcm_encode_instance_reference(dcm) for dcm in dcm_reference_instances(reference, instances=instances)]))
	elif DCMHEADER_SR_REF_INSTANCE_SEQ in dcm_mdata:
		# Series-only form: an instance list left over from a previous use of the dataset would pair this
		# series with the SOP instances of another
		delattr(dcm_mdata, DCMHEADER_SR_REF_INSTANCE_SEQ)

	return dcm_mdata


def dcm_reference_series_list(primary, references=None):
	'''	References of a derived object: the series it is linked to first, then each further reference
		once. References are compared by Series Instance UID; None entries are skipped.

		@input primary: the series the object is linked to (see `dcm_reference_series_uid`), or None
		@input references (iterable, default=None): further references, in the order they are to be written
		@returns list of the references, in writing order
	'''
	result, uids = [], set()
	for ref in [primary] + list(references or ()):
		if ref is None:
			continue
		uid = dcm_reference_series_uid(ref)
		if uid in uids:
			continue
		uids.add(uid)
		result.append(ref)

	return result


def dcm_encode_reference_series_seq(references, reference_instances=DCM_REFERENCE_INSTANCES_DEFAULT):
	'''	Referenced Series Sequence for a list of references (see `dcm_reference_series_list`), one
		item per reference in the order given.

		@input references (iterable): the references
		@input reference_instances (bool, default=True): see `dcm_encode_series_reference`
		@returns pydicom.sequence.Sequence
	'''
	return Sequence([dcm_encode_series_reference(ref, reference_instances=reference_instances) for ref in references])
