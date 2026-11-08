'''	Lenient reader of DICOM-SR documents for the Sonador report models.

	A DICOM-SR document is a tree of coded content items; the Sonador report models
	(`sonador.imaging.helpers.sr`) read it into findings, measurements and report groups. The read
	path is: document (file, bytes, dataset or imaging server instance) -> pydicom (`srdoc_read`) ->
	repairs on a copy (`srdoc_repair`) -> highdicom (`srdoc_highdicom`) -> report model
	(`srdoc_parse`, the entry point `SonadorBaseSR.from_sr` calls).

	The reader never discards content it does not recognise: unmodelled items are preserved on the
	model objects as `extra_items`, by-reference items (Referenced Content Item Identifier) are
	preserved and prevent a rewrite, and the only items dropped are those without a Value Type or
	without a required concept name. Every repair is recorded as a diagnostic
	(`sonador.apisettings.sr.sr_diagnostic`) with level `info`, `warning` or `error`; `strict=True`
	raises SRDiagnosticError on `warning` and `error`.

	Accepted documents: Enhanced SR, Comprehensive SR and Comprehensive 3D SR with root template
	`1500` or `Sonador-SR.1500` (the mapping resource is ignored on read), in the nested, flat or
	legacy per-finding layout (`sonador.apisettings.sr.seriestag`).

	The module also holds the helpers of a re-save: the predecessor chain (`srdoc_predecessors`,
	`srdoc_predecessor_items`), the study a document is filed under (`srdoc_set_study`) and evidence
	synthesised without instance files (`srdoc_synthetic_evidence`).
'''
import copy, io, os, logging
from collections import OrderedDict, namedtuple

import pydicom
from pydicom.dataset import Dataset as DcmDataset
from pydicom.sequence import Sequence as DcmSequence
from pydicom.sr.codedict import codes as dcmcodes

from highdicom.sr import EnhancedSR, ComprehensiveSR, Comprehensive3DSR
from highdicom.sr.templates import CodedConcept
from highdicom.sr.enum import RelationshipTypeValues

from ....apisettings.base import ReportGroup, ReportGroupCollection, ImageReference, \
	DCMHEADER_SR_EVIDENCE_SEQUENCES, DCMHEADER_SR_REF_SERIES_SEQ, DCMHEADER_SR_REF_SOP_SEQ, DCMHEADER_SR_REF_INSTANCE_UID, \
	DCMHEADER_SR_SOP_CLASS_UID, DCMHEADER_SERIES_INSTANCE_UID, DCMHEADER_STUDY_INSTANCE_UID, DCMHEADER_SOP_CLASS_UID, \
	DCMHEADER_SOP_INSTANCE_UID, DCMHEADER_SR_VALUE_TYPE, DCMHEADER_PATIENT_ID, DCMHEADER_PATIENT_NAME, \
	DCMHEADER_PATIENT_BIRTHDATE, DCMHEADER_PATIENT_SEX, DCMHEADER_ACCESSION_NUMBER, DCMHEADER_STUDY_ID, \
	DCMHEADER_STUDY_DATE, DCMHEADER_STUDY_TIME, DCMHEADER_REFERRING_PHYSICIAN, DCMHEADER_SPECIFIC_CHARSET
from ....apisettings.sr import sr_diagnostic, SRDiagnosticError, srcontent_is_by_reference, srcontent_holds_by_reference, \
	srcontent_is_named, srcontent_name, dataset2srcode, DCMSR_PERSON_OBSERVER_PLACEHOLDER, \
	SR_DIAGNOSTIC_INFO, SR_DIAGNOSTIC_WARNING, SR_DIAGNOSTIC_ERROR, \
	SR_CONDITION_BY_REFERENCE_ITEM, SR_CONDITION_PLACEHOLDER_PERSON_OBSERVER, SR_CONDITION_UNSUPPORTED_TEMPLATE, \
	SR_CONDITION_UNRESOLVED_REFERENCE, SR_CONDITION_MISSING_RELATIONSHIP, SR_CONDITION_ROOT_VALUE_TYPE_MISSING
from ....apisettings.sr.seriestag import srcontent_is_valid, DCMSR_SOP_CLASS_ENHANCED, DCMSR_SOP_CLASS_COMPREHENSIVE, \
	DCMSR_SOP_CLASS_COMPREHENSIVE_3D, DCMSR_SOP_CLASSES_SUPPORTED, DCMSR_SUPPORTED_TEMPLATE_IDS, DCMR_TEMPLATE_ID, \
	mapping_resource_for_template
from ..headers import dcm_header_value, dcm_header_uid

logger = logging.getLogger(__name__)


# Root content items TID 1500 owns; everything else at the root is preserved as `extra_items`
DCMSR_ROOT_LANGUAGE = dcmcodes.DCM.LanguageOfContentItemAndDescendants
DCMSR_ROOT_PROCEDURE_REPORTED = dcmcodes.DCM.ProcedureReported
DCMSR_ROOT_IMAGE_LIBRARY = dcmcodes.DCM.ImageLibrary
DCMSR_ROOT_IMAGING_MEASUREMENTS = dcmcodes.DCM.ImagingMeasurements
DCMSR_ROOT_OBSERVER_CODES = (
	dcmcodes.DCM.ObserverType, dcmcodes.DCM.PersonObserverName, dcmcodes.DCM.DeviceObserverUID,
	dcmcodes.DCM.DeviceObserverName, dcmcodes.DCM.DeviceObserverManufacturer, dcmcodes.DCM.DeviceObserverModelName,
	dcmcodes.DCM.DeviceObserverSerialNumber)
DCMSR_ROOT_REGENERATED = (DCMSR_ROOT_LANGUAGE, DCMSR_ROOT_PROCEDURE_REPORTED, DCMSR_ROOT_IMAGE_LIBRARY,
	DCMSR_ROOT_IMAGING_MEASUREMENTS) + DCMSR_ROOT_OBSERVER_CODES

# highdicom document class by SOP class: the class a document is read with (`DCMSR_DOCUMENT_CLASSES`)
# and the class its re-save is written with (`DCMSR_WRITER_CLASSES`). A Comprehensive SR is
# rewritten as Comprehensive 3D SR; an Enhanced SR stays Enhanced SR.
DCMSR_DOCUMENT_CLASSES = OrderedDict((
	(DCMSR_SOP_CLASS_ENHANCED, EnhancedSR),
	(DCMSR_SOP_CLASS_COMPREHENSIVE, ComprehensiveSR),
	(DCMSR_SOP_CLASS_COMPREHENSIVE_3D, Comprehensive3DSR),
))
DCMSR_WRITER_CLASSES = OrderedDict((
	(DCMSR_SOP_CLASS_ENHANCED, EnhancedSR),
	(DCMSR_SOP_CLASS_COMPREHENSIVE, Comprehensive3DSR),
	(DCMSR_SOP_CLASS_COMPREHENSIVE_3D, Comprehensive3DSR),
))

# Patient / study attributes highdicom's SR constructors read from the first evidence item
DCMSR_EVIDENCE_HEADERS = (DCMHEADER_PATIENT_ID, DCMHEADER_PATIENT_NAME, DCMHEADER_PATIENT_BIRTHDATE, DCMHEADER_PATIENT_SEX,
	DCMHEADER_ACCESSION_NUMBER, DCMHEADER_STUDY_ID, DCMHEADER_STUDY_DATE, DCMHEADER_STUDY_TIME, DCMHEADER_REFERRING_PHYSICIAN,
	DCMHEADER_SPECIFIC_CHARSET)

# A predecessor instance named by a Predecessor Documents Sequence
SRPredecessor = namedtuple('SRPredecessor', ('study_uid', 'series_uid', 'sop_class_uid', 'sop_instance_uid'))

# What the document-level reader found, handed to the report model constructor
SRParsedDocument = namedtuple('SRParsedDocument', (
	'dataset', 'highdicom', 'title', 'template_id', 'mapping_resource', 'procedures', 'person_observer',
	'device_observer_uid', 'groups', 'extra_items', 'evidence', 'predecessors', 'diagnostics'))


# Reading

def srdoc_read(dcmsr):
	'''	Read a DICOM-SR document into a pydicom dataset.

		@input dcmsr: a dataset (a highdicom SR object is one, returned as is), a path, bytes, a
			file-like object, or an imaging server instance (`DcmSRInstance`), read through its cached
			file (one `file` request when the file is not cached yet)

		@returns pydicom.Dataset
		@raises TypeError for any other input
	'''
	if isinstance(dcmsr, DcmDataset):
		return dcmsr
	if hasattr(dcmsr, 'dcmfile'):
		return dcmsr.dcmfile(cache=True)
	if isinstance(dcmsr, (bytes, bytearray)):
		return pydicom.dcmread(io.BytesIO(dcmsr), force=True)
	if isinstance(dcmsr, (str, os.PathLike)):
		return pydicom.dcmread(dcmsr, force=True)
	if hasattr(dcmsr, 'read'):
		return pydicom.dcmread(dcmsr, force=True)
	raise TypeError('Unsupported DICOM-SR input: %r' % type(dcmsr))


def srdoc_sop_class(ds):
	'''	SOP Class UID of a document; the Media Storage SOP Class UID of the file meta when the dataset
		carries none itself.

		@returns str, or None when neither is present
	'''
	return dcm_header_uid(ds, DCMHEADER_SOP_CLASS_UID) \
		or dcm_header_uid(getattr(ds, 'file_meta', None), 'MediaStorageSOPClassUID')


def srdoc_root_template(ds):
	'''	Root template of a document, from its Content Template Sequence.

		@returns tuple (str, str): (template identifier, mapping resource); (None, None) when the
			document declares no template
	'''
	seq = dcm_header_value(ds, 'ContentTemplateSequence')
	if not seq:
		return (None, None)
	item = seq[0]
	tid = dcm_header_value(item, 'TemplateIdentifier')
	return (None if tid is None else str(tid), dcm_header_value(item, 'MappingResource'))


def srdoc_document_class(ds):
	'''	highdicom document class a re-save of the document is written with: Enhanced SR stays
		Enhanced SR, Comprehensive SR and Comprehensive 3D SR are written as Comprehensive 3D SR.

		@returns highdicom SR class: EnhancedSR or Comprehensive3DSR (the latter for an unknown SOP class)
	'''
	return DCMSR_WRITER_CLASSES.get(srdoc_sop_class(ds), Comprehensive3DSR)


def srdoc_children(item):
	'''	Content items directly under a content item or under the document root.

		@returns list of pydicom.Dataset: empty when there are none
	'''
	return list(dcm_header_value(item, 'ContentSequence') or [])


def srdoc_root_items(ds, code):
	'''	Root content items whose concept name is `code`.

		@input ds (pydicom.Dataset): the document
		@input code (Code or CodedConcept): concept name matched by value and scheme designator

		@returns list of pydicom.Dataset
	'''
	return [item for item in srdoc_children(ds) if srcontent_is_named(item, code)]


def srdoc_measurement_groups(ds):
	'''	Measurement Group items of the Imaging Measurements container, in document order.

		@returns list of pydicom.Dataset
	'''
	groups = []
	for container in srdoc_root_items(ds, DCMSR_ROOT_IMAGING_MEASUREMENTS):
		for item in srdoc_children(container):
			if srcontent_is_named(item, dcmcodes.DCM.MeasurementGroup):
				groups.append(item)
	return groups


def srdoc_evidence(ds):
	'''	Evidence of a document: the instances its Current Requested Procedure Evidence Sequence and
		Pertinent Other Evidence Sequence record. Each reference keeps the study it was recorded under,
		so that a re-save files it under the same evidence sequence again. A series recorded without
		instances contributes one entry keyed by its Series Instance UID with no SOP instance.

		@returns OrderedDict: SOP Instance UID (or Series Instance UID) -> ImageReference (SOP class,
			SOP instance, no frame number, series UID, study UID), in document order
	'''
	references = OrderedDict()
	for seq in DCMHEADER_SR_EVIDENCE_SEQUENCES:
		for study_item in dcm_header_value(ds, seq) or []:
			study_uid = dcm_header_uid(study_item, DCMHEADER_STUDY_INSTANCE_UID)
			for series_item in dcm_header_value(study_item, DCMHEADER_SR_REF_SERIES_SEQ) or []:
				series_uid = dcm_header_uid(series_item, DCMHEADER_SERIES_INSTANCE_UID)
				sops = dcm_header_value(series_item, DCMHEADER_SR_REF_SOP_SEQ) or []
				if not sops and series_uid and series_uid not in references:
					references[series_uid] = ImageReference(None, None, None, series_uid, study_uid)
				for sop in sops:
					sop_uid = dcm_header_uid(sop, DCMHEADER_SR_REF_INSTANCE_UID)
					if sop_uid and sop_uid not in references:
						references[sop_uid] = ImageReference(dcm_header_uid(sop, DCMHEADER_SR_SOP_CLASS_UID), sop_uid, None, series_uid, study_uid)
	return references


def srdoc_set_study(ds, study_uid):
	'''	File a written document under `study_uid` and partition its evidence accordingly: the study
		items of that study go to the Current Requested Procedure Evidence Sequence, every other study
		to the Pertinent Other Evidence Sequence, and an empty sequence is removed. highdicom takes the
		document's study from the first evidence dataset, so without this step a re-save of a document
		whose evidence is all from other studies would move the document to one of them.

		@input ds (highdicom SR document): the written document, modified in place
		@input study_uid (str): Study Instance UID the document belongs to; an empty value is a no-op

		@returns highdicom SR document: `ds`
	'''
	if not study_uid:
		return ds
	ds.StudyInstanceUID = study_uid
	items = []
	for seq in DCMHEADER_SR_EVIDENCE_SEQUENCES:
		items.extend(dcm_header_value(ds, seq) or [])
	current = [item for item in items if dcm_header_uid(item, DCMHEADER_STUDY_INSTANCE_UID) == study_uid]
	other = [item for item in items if dcm_header_uid(item, DCMHEADER_STUDY_INSTANCE_UID) != study_uid]
	for seq, selected in ((DCMHEADER_SR_EVIDENCE_SEQUENCES[0], current), (DCMHEADER_SR_EVIDENCE_SEQUENCES[1], other)):
		if selected:
			setattr(ds, seq, DcmSequence(selected))
		elif seq in ds:
			delattr(ds, seq)
	return ds


def srdoc_study_uids(ds):
	'''	Study Instance UIDs a document refers to: its own study first, then every study recorded in
		its evidence sequences.

		@returns list of str: distinct, in document order
	'''
	uids = []
	own = dcm_header_uid(ds, DCMHEADER_STUDY_INSTANCE_UID)
	if own:
		uids.append(own)
	for seq in DCMHEADER_SR_EVIDENCE_SEQUENCES:
		for study_item in dcm_header_value(ds, seq) or []:
			uid = dcm_header_uid(study_item, DCMHEADER_STUDY_INSTANCE_UID)
			if uid and uid not in uids:
				uids.append(uid)
	return uids


def srdoc_predecessors(ds):
	'''	Instances the document's Predecessor Documents Sequence names: the documents this one
		supersedes.

		@returns list of SRPredecessor: empty when the document names none
	'''
	predecessors = []
	for study_item in dcm_header_value(ds, 'PredecessorDocumentsSequence') or []:
		study_uid = dcm_header_uid(study_item, DCMHEADER_STUDY_INSTANCE_UID)
		for series_item in dcm_header_value(study_item, DCMHEADER_SR_REF_SERIES_SEQ) or []:
			series_uid = dcm_header_uid(series_item, DCMHEADER_SERIES_INSTANCE_UID)
			for sop in dcm_header_value(series_item, DCMHEADER_SR_REF_SOP_SEQ) or []:
				predecessors.append(SRPredecessor(study_uid, series_uid,
					dcm_header_uid(sop, DCMHEADER_SR_SOP_CLASS_UID), dcm_header_uid(sop, DCMHEADER_SR_REF_INSTANCE_UID)))
	return predecessors


def srdoc_predecessor_items(predecessors):
	'''	Items of a Predecessor Documents Sequence for `SRPredecessor` records, grouped study -> series
		-> SOP instance in the order given.

		@input predecessors (iterable of SRPredecessor)
		@returns list of pydicom.Dataset: one study item per study
	'''
	studies = OrderedDict()
	for pre in predecessors:
		studies.setdefault(pre.study_uid, OrderedDict()).setdefault(pre.series_uid, []).append(pre)
	items = []
	for study_uid, series in studies.items():
		study_item = DcmDataset()
		study_item.StudyInstanceUID = study_uid
		study_item.ReferencedSeriesSequence = DcmSequence()
		for series_uid, sops in series.items():
			series_item = DcmDataset()
			series_item.SeriesInstanceUID = series_uid
			series_item.ReferencedSOPSequence = DcmSequence()
			for pre in sops:
				sop_item = DcmDataset()
				sop_item.ReferencedSOPClassUID = pre.sop_class_uid
				sop_item.ReferencedSOPInstanceUID = pre.sop_instance_uid
				series_item.ReferencedSOPSequence.append(sop_item)
			study_item.ReferencedSeriesSequence.append(series_item)
		items.append(study_item)
	return items


# Repairs for highdicom

def _fill_relationship(child, parent_value_type):
	'''	Relationship type assumed for a content item without one: CONTAINS under a CONTAINER and at
		the root, INFERRED FROM for an IMAGE item under any other parent, HAS PROPERTIES otherwise.

		@input child (pydicom.Dataset): the content item
		@input parent_value_type (str): Value Type of its parent, None at the root

		@returns str: the Relationship Type value
	'''
	value_type = dcm_header_value(child, DCMHEADER_SR_VALUE_TYPE)
	if parent_value_type in (None, 'CONTAINER'):
		return RelationshipTypeValues.CONTAINS.value
	if value_type == 'IMAGE':
		return RelationshipTypeValues.INFERRED_FROM.value
	return RelationshipTypeValues.HAS_PROPERTIES.value


def _repair_sequence(parent, parent_value_type, diagnostics, strict, path):
	'''	Repair the content sequence of `parent` in place, recursively: fill missing Relationship
		Types, remove by-reference items, NUM items without a value (which highdicom cannot read) and
		items that cannot be modelled. `parent` is the copy
		`srdoc_repair` made. Every filled Relationship Type is recorded here as a `warning`
		(`missing-relationship-type`, one per item at any depth, so `strict=True` rejects it); the
		model parsers (`sonador.apisettings.sr.seriestag`) read the original items and do not record it
		again. Dropped items are recorded by the model parsers, which know the item's role.

		@input parent (pydicom.Dataset): content item or document root whose children are repaired
		@input parent_value_type (str): Value Type of `parent`, None at the root
		@input diagnostics (list): list the diagnostics are appended to
		@input strict (bool): raise SRDiagnosticError on a `warning`
		@input path (str): content tree location of `parent`, for the diagnostics
	'''
	children = srdoc_children(parent)
	if not children:
		return
	kept = []
	for idx, child in enumerate(children):
		child_path = '%s[%d]' % (path, idx)
		if srcontent_is_by_reference(child):
			continue
		if not srcontent_is_valid(child, diagnostics=None, strict=False):
			continue
		if dcm_header_value(child, DCMHEADER_SR_VALUE_TYPE) == 'NUM' and not dcm_header_value(child, 'MeasuredValueSequence'):
			continue
		if dcm_header_value(child, 'RelationshipType') is None:
			filled = _fill_relationship(child, parent_value_type)
			sr_diagnostic(diagnostics, SR_DIAGNOSTIC_WARNING, SR_CONDITION_MISSING_RELATIONSHIP,
				'content item without a Relationship Type; read as %s' % filled, path=child_path,
				value=dcm_header_value(child, DCMHEADER_SR_VALUE_TYPE), strict=strict)
			child.RelationshipType = filled
		_repair_sequence(child, dcm_header_value(child, DCMHEADER_SR_VALUE_TYPE), diagnostics, strict, child_path)
		kept.append(child)
	parent.ContentSequence = DcmSequence(kept)


def srdoc_repair(ds, diagnostics=None, strict=False):
	'''	Deep copy of a document that highdicom can represent: Relationship Types filled, by-reference,
		valueless NUM and unmodellable items removed, a missing root Value Type set to CONTAINER (`info`
		diagnostic), Continuity of Content defaulted to SEPARATE, and root template `Sonador-SR.1500`
		written as `1500` so that highdicom reads the content as a `MeasurementReport`. The original
		dataset is not modified.

		@input ds (pydicom.Dataset): the document
		@input diagnostics (list, default=None): list the diagnostics are appended to
		@input strict (bool, default=False): raise SRDiagnosticError on a `warning`

		@returns pydicom.Dataset: the repaired copy
	'''
	repaired = copy.deepcopy(ds)
	if dcm_header_value(repaired, 'ContinuityOfContent') is None:
		repaired.ContinuityOfContent = 'SEPARATE'
	if dcm_header_value(repaired, DCMHEADER_SR_VALUE_TYPE) is None:
		# dcmjs's Comprehensive 3D SR documents omit the root Value Type; the root is a CONTAINER
		sr_diagnostic(diagnostics, SR_DIAGNOSTIC_INFO, SR_CONDITION_ROOT_VALUE_TYPE_MISSING,
			'root content item without a Value Type; read as CONTAINER', path='root')
		repaired.ValueType = 'CONTAINER'
	template_id, _ = srdoc_root_template(repaired)
	if template_id in DCMSR_SUPPORTED_TEMPLATE_IDS and template_id != DCMR_TEMPLATE_ID:
		repaired.ContentTemplateSequence[0].TemplateIdentifier = DCMR_TEMPLATE_ID
	_repair_sequence(repaired, None, diagnostics, strict, 'root')
	return repaired


def srdoc_highdicom(ds, diagnostics=None, strict=False):
	'''	highdicom SR object of a document, built from a repaired copy (`srdoc_repair`). Its `content`
		is a `MeasurementReport` for the supported root templates.

		@input ds (pydicom.Dataset): the document
		@input diagnostics (list, default=None): list the diagnostics are appended to
		@input strict (bool, default=False): raise SRDiagnosticError on a `warning`

		@returns
			- highdicom.sr.EnhancedSR for an Enhanced SR document
			- highdicom.sr.ComprehensiveSR for a Comprehensive SR document
			- highdicom.sr.Comprehensive3DSR for a Comprehensive 3D SR document
		@raises ValueError for any other SOP class
	'''
	sop_class = srdoc_sop_class(ds)
	document_class = DCMSR_DOCUMENT_CLASSES.get(sop_class)
	if document_class is None:
		raise ValueError('Unsupported DICOM-SR SOP class: %s (supported: %s)' % (sop_class, ', '.join(DCMSR_SOP_CLASSES_SUPPORTED)))
	return document_class.from_dataset(srdoc_repair(ds, diagnostics=diagnostics, strict=strict), copy=False)


# Evidence synthesis (no file downloads)

def srdoc_evidence_headers(source):
	'''	Patient and study attributes an evidence dataset needs (`DCMSR_EVIDENCE_HEADERS`), read from a
		document, a dataset or a dictionary of tags.

		@returns OrderedDict: keyword -> value, None for an attribute the source lacks
	'''
	return OrderedDict((name, dcm_header_value(source, name)) for name in DCMSR_EVIDENCE_HEADERS)


def srdoc_synthetic_evidence(headers, study_uid, references):
	'''	Evidence datasets for highdicom built without instance files: one minimal dataset per
		reference, holding the patient and study attributes of `headers` and the study, series, SOP
		instance and SOP class UIDs of the reference. A reference keeps the study it carries
		(`ImageReference.study_uid`, as read from a document's evidence sequences), so that highdicom
		files it under the same evidence sequence again; `study_uid` is used for a reference without
		one. A reference without a SOP instance (series-level evidence) is skipped. No request is made.

		@input headers (dict): patient and study attributes (`srdoc_evidence_headers`); a Specific
			Character Set of None is left out
		@input study_uid (str): study of the document, assumed for references that carry none
		@input references (iterable of ImageReference or (series_uid, sop_class_uid, sop_instance_uid))

		@returns list of pydicom.Dataset
	'''
	evidence = []
	for ref in references:
		ref_study = None
		if isinstance(ref, ImageReference):
			series_uid, sop_class, sop_instance, ref_study = ref.series_uid, ref.sop_class_uid, ref.sop_instance_uid, ref.study_uid
		else:
			series_uid, sop_class, sop_instance = ref
		if not sop_instance:
			continue
		item = DcmDataset()
		for name, value in headers.items():
			if name == DCMHEADER_SPECIFIC_CHARSET and value is None:
				continue
			setattr(item, name, value)
		item.StudyInstanceUID = ref_study or study_uid
		item.SeriesInstanceUID = series_uid
		item.SOPInstanceUID = sop_instance
		item.SOPClassUID = sop_class or ''
		evidence.append(item)
	return evidence


# Document-level parse

def srdoc_parse(dcmsr, report_class, strict=False, diagnostics=None, calibrations=None, **kwargs):
	'''	Parse one DICOM-SR document to a report model: read the root items (title, procedures
		reported, Person Observer, Device Observer UID; everything else at the root is preserved),
		the measurement groups (`ReportGroup.from_sr`), the evidence and the predecessors, and hand
		them to the model class as an `SRParsedDocument`. The series of each finding and measurement is resolved from
		the evidence (`_resolve_reference`).

		@input dcmsr: the document (see `srdoc_read`)
		@input report_class (subclass of SonadorBaseSR): model class instantiated
		@input strict (bool, default=False): raise SRDiagnosticError on `warning` and `error`
			diagnostics
		@input diagnostics (list, default=new list): list the diagnostics are appended to; also
			attached to the model as `diagnostics`
		@input calibrations (mapping, default=None): `ImageFrameGeometry` by SOP Instance UID or
			`(SOP Instance UID, frame number)`, used to verify the stored values of image-domain
			measurements; without one a stored value in millimetres is kept unverified, with an `info`
			diagnostic. Makes no request.
		@input kwargs: constructor arguments of `report_class`

		@returns instance of `report_class`
		@raises SRDiagnosticError for a root template the class does not support (`unsupported-template`,
			an `error` raised in every mode)
	'''
	diagnostics = diagnostics if diagnostics is not None else []
	ds = srdoc_read(dcmsr)

	template_id, mapping_resource = srdoc_root_template(ds)
	supported = getattr(report_class, 'supported_templates', DCMSR_SUPPORTED_TEMPLATE_IDS)
	if template_id not in supported:
		sr_diagnostic(diagnostics, SR_DIAGNOSTIC_ERROR, SR_CONDITION_UNSUPPORTED_TEMPLATE,
			'root template "%s" is not one of %s' % (template_id, ', '.join(supported)), value=template_id, strict=True)

	highdicom_doc = srdoc_highdicom(ds, diagnostics=diagnostics, strict=strict)

	# Root items: title, procedures, observers; everything TID 1500 does not own is preserved
	title = CodedConcept.from_dataset(ds.ConceptNameCodeSequence[0], copy=True) if dcm_header_value(ds, 'ConceptNameCodeSequence') else None
	procedures, person_observer, device_observer_uid, extra_items = [], None, None, []
	for item in srdoc_children(ds):
		if srcontent_is_named(item, DCMSR_ROOT_PROCEDURE_REPORTED) and dcm_header_value(item, DCMHEADER_SR_VALUE_TYPE) == 'CODE':
			procedures.append(CodedConcept.from_dataset(item.ConceptCodeSequence[0], copy=True))
		elif srcontent_is_named(item, dcmcodes.DCM.PersonObserverName) and dcm_header_value(item, DCMHEADER_SR_VALUE_TYPE) == 'PNAME':
			person_observer = str(dcm_header_value(item, 'PersonName') or '') or None
			if person_observer == DCMSR_PERSON_OBSERVER_PLACEHOLDER:
				sr_diagnostic(diagnostics, SR_DIAGNOSTIC_INFO, SR_CONDITION_PLACEHOLDER_PERSON_OBSERVER,
					'placeholder Person Observer Name kept verbatim', value=person_observer)
		elif srcontent_is_named(item, dcmcodes.DCM.DeviceObserverUID) and dcm_header_value(item, DCMHEADER_SR_VALUE_TYPE) == 'UIDREF':
			device_observer_uid = str(dcm_header_value(item, 'UID') or '') or None
		elif any(srcontent_is_named(item, code) for code in DCMSR_ROOT_REGENERATED):
			continue
		elif srcontent_holds_by_reference(item):
			sr_diagnostic(diagnostics, SR_DIAGNOSTIC_INFO, SR_CONDITION_BY_REFERENCE_ITEM,
				'by-reference content item preserved at the root; the document cannot be rewritten', path='root')
			extra_items.append(copy.deepcopy(item))
		elif srcontent_is_valid(item, diagnostics=diagnostics, strict=strict, path='root'):
			extra_items.append(copy.deepcopy(item))

	# Measurement groups, routed by tracking identifier; findings aligned with the evidence
	evidence = srdoc_evidence(ds)
	groups = []
	for group_item in srdoc_measurement_groups(ds):
		group = ReportGroup.from_sr(group_item, strict=strict, diagnostics=diagnostics, repaired=True, calibrations=calibrations)
		for finding in (group.findings or []):
			_resolve_reference(finding, evidence, diagnostics, strict)
		for measurement in (group.measurements or []):
			_resolve_reference(measurement, evidence, diagnostics, strict)
		groups.append(group)

	parsed = SRParsedDocument(
		dataset=ds, highdicom=highdicom_doc, title=title, template_id=template_id,
		mapping_resource=mapping_resource or mapping_resource_for_template(template_id),
		procedures=procedures, person_observer=person_observer, device_observer_uid=device_observer_uid,
		groups=groups, extra_items=extra_items, evidence=evidence, predecessors=srdoc_predecessors(ds),
		diagnostics=diagnostics)
	return report_class._from_parsed(parsed, **kwargs)


def _resolve_reference(finding, evidence, diagnostics, strict):
	'''	Resolve the series of a finding or measurement from the document evidence: the series its referenced
		instance is recorded in, with the study and SOP class the evidence records when the reference
		lacks them. The finding's `reference` is replaced in place. A reference to an instance the
		evidence does not record is kept as it is, with a `warning` (`unresolved-image-reference`).

		@input finding (Finding or Measurement): item whose reference is resolved
		@input evidence (OrderedDict): `srdoc_evidence` of the document
		@input diagnostics (list): list the diagnostics are appended to
		@input strict (bool): raise SRDiagnosticError on the `warning`
	'''
	reference = finding.reference
	if reference is None or not reference.sop_instance_uid:
		return
	recorded = evidence.get(reference.sop_instance_uid)
	if recorded is None:
		sr_diagnostic(diagnostics, SR_DIAGNOSTIC_WARNING, SR_CONDITION_UNRESOLVED_REFERENCE,
			'referenced SOP instance is not recorded in the document evidence; series unresolved',
			path='group %s' % finding.tracking_uid, value=reference.sop_instance_uid, strict=strict)
		return
	finding.reference = reference._replace(
		series_uid=recorded.series_uid, study_uid=reference.study_uid or recorded.study_uid,
		sop_class_uid=reference.sop_class_uid or recorded.sop_class_uid)
