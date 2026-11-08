'''	DICOM-SR constants, data structures, and encoded concepts for Sonador.
	Provides utilities and tools to simplify the process of encoding and parsing DICOM-SR data.

	`sonador.apisettings.sr` is a package. This module holds the grammar shared by every Sonador-SR
	document (codes, content items, templates, diagnostics); each measurement profile is a submodule
	with its own concept registry, encoder and parser: `seriestag` (Series Tags) and `measurements`
	(Length, Angle, Cobb angle).

	Besides the encoders of the report templates (`srencode_*`) and the coded concept conversions
	(`srcode2dataset`, `dataset2srcode`), the module holds:
	* the code value helpers that read a value from whichever of `CodeValue`, `LongCodeValue` and
		`URNCodeValue` carries it (`dcm_code_value`, `dcm_code_matches`, `srcontent_is_named`);
	* the by-reference checks (`srcontent_is_by_reference`, `srcontent_holds_by_reference`) and the
		re-emission of preserved content items (`srcontent_extra_items`);
	* the parse diagnostics: `sr_diagnostic` records an `SRDiagnostic` with level `info`, `warning`
		or `error` and a condition code (`SR_CONDITION_*`); `strict=True` raises `SRDiagnosticError`
		on `warning` and `error`.
'''
import re, numbers, copy, logging
from collections import namedtuple
from typing import Union, Sequence

import numpy as np

from pydicom import Dataset as DcmDataset, Sequence as DcmSequence
from pydicom.uid import generate_uid
from pydicom.sr.codedict import codes as dcmcodes

from highdicom.version import __version__ as HIGHDICOM_VERSION
from highdicom.sr.templates import Code as DcmCode, Template as DcmTemplate, CodedConcept as DcmCodedConcept, \
	CodeContentItem, ContentSequence, ObserverContext, ObservationContext, DeviceObserverIdentifyingAttributes, \
	LanguageOfContentItemAndDescendants, MeasurementsAndQualitativeEvaluations, TrackingIdentifier, \
	DEFAULT_LANGUAGE as DCMSR_DEFAULT_LANGUAGE
from highdicom.sr import Scoord3DContentItem, GraphicTypeValues3D, Measurement as DcmMeasurement, \
	QualitativeEvaluation as DcmQualitativeEvaluation
from highdicom.sr.enum import RelationshipTypeValues, ValueTypeValues
from highdicom.sr.value_types import ContentItem, PnameContentItem
from highdicom.uid import UID as DcmUid

from client.utils.object import pick


from ..base import ImageCoord, ImageCoordCollection, ImageTransformMatrix, \
	Measurement, MeasurementCollection, Finding, FindingCollection, \
	DCMHEADER_CODE_VALUE, DCMHEADER_CODING_SCHEME_DESIGNATOR, \
	DCMHEADER_CODING_SCHEME_VERSION, DCMHEADER_CODE_MEANING, \
	DCMHEADER_MAPPING_RESOURCE, DCMHEADER_MAPPING_RESOURCE_UID, \
	DCMHEADER_LONG_CODE_VALUE, DCMHEADER_URN_CODE_VALUE, DCMHEADER_MAPPING_RESOURCE_NAME, \
	DCMHEADER_CONTEXT_GROUP_VERSION, DCMHEADER_CONTEXT_UID, \
	DCMSR_SONADOR_SR, SONADOR_SCHEME_VERSION_02, DCMHEADER_REFERENCE_FRAME, DCMHEADER_MATRIX_REGISTRATION_SEQUENCE, \
	DCMHEADER_MATRIX_REGISTRATION, DCMHEADER_MATRIX_TRANSFORMATION_TYPE, DCMHEADER_REFERENCE_FRAME_MATRIX, \
	DCM_MATRIX_RIGID, DCM_MATRIX_RIGID_SCALE, DCM_MATRIX_AFFINE, DCM_MATRIX_TRANSFORM_SUPPORTED, \
	DCMHEADER_REFERENCE_FRAME_TRANFORM_COMMENT, DCMHEADER_MATRIX_USED_FIDUCIALS, DCMHEADER_SR_REF_IMAGES_SEQ, \
	SONADOR_SCHEME_VERSION_02, DCMHEADER_CONCEPT_CODE_SEQUENCE, DCMHEADER_SR_VALUE_TYPE

logger = logging.getLogger(__name__)

DCM_CODED_CONCEPT_HEADERS = (DCMHEADER_CODE_VALUE, DCMHEADER_CODING_SCHEME_DESIGNATOR,
	DCMHEADER_CODE_MEANING, DCMHEADER_CODING_SCHEME_VERSION)
DCM_CODED_CONCEPT_MAPPING = {
	'value': DCMHEADER_CODE_VALUE,
	'scheme_designator': DCMHEADER_CODING_SCHEME_DESIGNATOR,
	'meaning': DCMHEADER_CODE_MEANING,
	'scheme_version': DCMHEADER_CODING_SCHEME_VERSION,
}

# Code value attributes (PS3.3 Table 8.8-1a). Writers select one by convention, readers accept any:
# `URNCodeValue` for a URN or URL, `CodeValue` up to 16 characters, `LongCodeValue` beyond. Values and
# meanings are written verbatim at any length; the DICOM VR limits are followed by convention, not
# enforced (an overlength value or meaning is an `info` diagnostic on read).
DCM_CODE_VALUE_ATTRIBUTES = (DCMHEADER_CODE_VALUE, DCMHEADER_LONG_CODE_VALUE, DCMHEADER_URN_CODE_VALUE)
DCM_CODE_VALUE_MAX_LENGTH = 16
DCM_CODE_MEANING_MAX_LENGTH = 64
DCM_URN_CODE_VALUE_RE = re.compile(r'^(urn:|[a-z][a-z0-9+.\-]*://)', re.IGNORECASE)

# Value types whose Concept Name Code Sequence is optional (PS3.3 C.17.3, Type 1C)
DCMSR_VALUE_TYPES_OPTIONAL_NAME = frozenset(('IMAGE', 'COMPOSITE', 'SCOORD', 'SCOORD3D', 'TCOORD', 'WAVEFORM'))
DCMHEADER_SR_REFERENCED_CONTENT_ITEM_IDENTIFIER = 'ReferencedContentItemIdentifier'


def dcm_code_value_attribute(value):
	'''	Attribute a code value is written to by convention: `URNCodeValue` for a URN or URL,
		`CodeValue` up to 16 characters, `LongCodeValue` beyond.

		@input value (str): the code value; None counts as empty
		@returns str: the attribute keyword
	'''
	text = '' if value is None else str(value)
	if DCM_URN_CODE_VALUE_RE.match(text):
		return DCMHEADER_URN_CODE_VALUE
	return DCMHEADER_LONG_CODE_VALUE if len(text) > DCM_CODE_VALUE_MAX_LENGTH else DCMHEADER_CODE_VALUE


def dcm_code_value(code):
	'''	Code value of a coded concept dataset (or simplified-tags dictionary), from whichever of the
		three value attributes carries it.

		@returns str, or None when none of the attributes is present
	'''
	from ...imaging.helpers.headers import dcm_header_value
	for attr in DCM_CODE_VALUE_ATTRIBUTES:
		value = dcm_header_value(code, attr)
		if value is not None:
			return str(value)
	return None


def dcm_code_matches(code, expected):
	'''	Whether a coded concept dataset carries the value and scheme designator of `expected`,
		whatever the scheme version and the value attribute used.

		@input code (pydicom.Dataset or dict): the coded concept checked
		@input expected (Code, CodedConcept, pydicom.Dataset or dict): the concept matched; without a
			scheme designator only the value is compared

		@returns bool: False when either is None
	'''
	if code is None or expected is None:
		return False
	from ...imaging.helpers.headers import dcm_header_value
	expected_value = getattr(expected, 'value', None) if not isinstance(expected, (dict, DcmDataset)) else dcm_code_value(expected)
	expected_scheme = getattr(expected, 'scheme_designator', None) if not isinstance(expected, (dict, DcmDataset)) \
		else dcm_header_value(expected, DCMHEADER_CODING_SCHEME_DESIGNATOR)
	return dcm_code_value(code) == (None if expected_value is None else str(expected_value)) \
		and (expected_scheme is None or dcm_header_value(code, DCMHEADER_CODING_SCHEME_DESIGNATOR) == expected_scheme)


def srcontent_name(item):
	'''	Concept name of a content item: the first item of its Concept Name Code Sequence.

		@returns pydicom.Dataset, or None for an item without a concept name
	'''
	from ...imaging.helpers.headers import dcm_header_value
	names = dcm_header_value(item, DCMHEADER_CONCEPT_CODE_SEQUENCE)
	return names[0] if names else None


def srcontent_is_named(item, code):
	'''	Whether a content item's concept name matches `code` by value and scheme designator.

		@returns bool
	'''
	return dcm_code_matches(srcontent_name(item), code)


def srcontent_is_by_reference(item):
	'''	Whether a content item is a by-reference relationship: it carries a Referenced Content Item
		Identifier instead of a value. Such items are preserved by the reader and refused by the
		writers, since rewriting the content tree would invalidate their target.

		@returns bool
	'''
	from ...imaging.helpers.headers import dcm_header_value
	return dcm_header_value(item, DCMHEADER_SR_REFERENCED_CONTENT_ITEM_IDENTIFIER) is not None


def srcontent_holds_by_reference(item):
	'''	Whether a content item is a by-reference relationship or holds one anywhere in its subtree
		(a preserved unknown CONTAINER may carry one). Used wherever a preserved item decides whether
		the document can be rewritten.

		@returns bool
	'''
	from ...imaging.helpers.headers import dcm_header_value
	if srcontent_is_by_reference(item):
		return True
	return any(srcontent_holds_by_reference(child) for child in (dcm_header_value(item, 'ContentSequence') or []))


# Parse diagnostics: `info` never fails anything; `strict=True` raises on `warning` (legacy repairs,
# document class upgrades) and `error` (dropped items, refused rewrites).
SR_DIAGNOSTIC_INFO = 'info'
SR_DIAGNOSTIC_WARNING = 'warning'
SR_DIAGNOSTIC_ERROR = 'error'
SR_DIAGNOSTIC_SEVERITIES = (SR_DIAGNOSTIC_INFO, SR_DIAGNOSTIC_WARNING, SR_DIAGNOSTIC_ERROR)
SR_DIAGNOSTIC_STRICT_SEVERITIES = (SR_DIAGNOSTIC_WARNING, SR_DIAGNOSTIC_ERROR)
SR_DIAGNOSTIC_LOG_LEVELS = {
	SR_DIAGNOSTIC_INFO: logging.INFO, SR_DIAGNOSTIC_WARNING: logging.WARNING, SR_DIAGNOSTIC_ERROR: logging.ERROR }

# Conditions
SR_CONDITION_MISSING_RELATIONSHIP = 'missing-relationship-type'
SR_CONDITION_SELECTED_FROM_IMAGE = 'selected-from-image-reference'
SR_CONDITION_VERSION_EQUALS_DESIGNATOR = 'coding-scheme-version-equals-designator'
SR_CONDITION_UNDEFINED_SCHEME_VERSION = 'undefined-scheme-version'
SR_CONDITION_GROUP_LEVEL_PROPERTIES = 'group-level-properties'
SR_CONDITION_OVERLENGTH_CODE_VALUE = 'overlength-code-value'
SR_CONDITION_OVERLENGTH_CODE_MEANING = 'overlength-code-meaning'
SR_CONDITION_PLACEHOLDER_PERSON_OBSERVER = 'placeholder-person-observer'
SR_CONDITION_INVALID_DISPLAY_COLOR = 'invalid-display-color'
SR_CONDITION_ITEM_DROPPED = 'content-item-dropped'
SR_CONDITION_GROUP_WITHOUT_FINDING = 'series-tag-group-without-finding'
SR_CONDITION_BY_REFERENCE_ITEM = 'by-reference-item'
SR_CONDITION_DOCUMENT_CLASS_UPGRADED = 'document-class-upgraded'
SR_CONDITION_PREDECESSOR_CHAIN_UNVERIFIED = 'predecessor-chain-unverified'
SR_CONDITION_UID_ORDER_TIE_BREAK = 'sop-instance-uid-order-tie-break'
SR_CONDITION_SHARED_TRACKING_UID = 'shared-tracking-uid'
SR_CONDITION_UNSUPPORTED_TEMPLATE = 'unsupported-template'
SR_CONDITION_UNKNOWN_TRACKING_ID = 'unknown-tracking-identifier'
SR_CONDITION_UNRESOLVED_REFERENCE = 'unresolved-image-reference'
SR_CONDITION_ROOT_VALUE_TYPE_MISSING = 'root-value-type-missing'
SR_CONDITION_EMPTY_REPORT_CONTENT = 'empty-report-content'
SR_CONDITION_MEASUREMENT_VALUE_MISMATCH = 'measurement-value-mismatch'
SR_CONDITION_MEASUREMENT_VALUE_UNVERIFIED = 'measurement-value-unverified'
SR_CONDITION_MEASUREMENT_UNCALIBRATED = 'measurement-uncalibrated'
SR_CONDITION_MEASUREMENT_NOT_COMPUTABLE = 'measurement-not-computable'
SR_CONDITION_MEASUREMENT_WITHOUT_VALUE = 'measurement-without-value'
SR_CONDITION_MEASUREMENT_READ_AS_GENERIC = 'measurement-read-as-generic'
SR_CONDITION_FOREIGN_MEASUREMENT_LABEL = 'foreign-measurement-label'
SR_CONDITION_DEGENERATE_GEOMETRY = 'degenerate-geometry'
SR_CONDITION_INVALID_GEOMETRY = 'invalid-measurement-geometry'
SR_CONDITION_SCOORD3D_WITHOUT_FRAME = 'scoord3d-without-frame-of-reference'
SR_CONDITION_ANGLE_MIDDLE_POINTS_DIFFER = 'angle-middle-points-differ'


class SRDiagnostic(namedtuple('SRDiagnostic', ('severity', 'condition', 'path', 'value', 'message'))):
	'''	One condition met while reading or writing a DICOM-SR document. `info` records a fact,
		`warning` a repair the reader made, `error` a content item that was dropped or a rewrite that
		was refused.

		@property severity (str): `info`, `warning` or `error`
		@property condition (str): condition code (`SR_CONDITION_*`)
		@property path (str): location in the content tree (tracking UID, concept, item index), or None
		@property value: offending value, if any
		@property message (str): description
	'''
	__slots__ = ()

	def __str__(self):
		return '[%s] %s%s: %s' % (self.severity, self.condition, (' at %s' % self.path) if self.path else '', self.message)


class SRDiagnosticError(ValueError):
	'''	Raised in strict mode for a `warning` or `error` diagnostic, and by the writers for a document
		that cannot be rewritten (a by-reference content item).

		@property diagnostic (SRDiagnostic): the diagnostic raised
	'''
	def __init__(self, diagnostic):
		self.diagnostic = diagnostic
		super().__init__(str(diagnostic))


def sr_diagnostic(diagnostics, severity, condition, message, path=None, value=None, strict=False):
	'''	Record a diagnostic: appended to `diagnostics` when a list is given, logged at the level of
		its severity and, in strict mode, raised for `warning` and `error`.

		@input diagnostics (list or None): list the diagnostic is appended to
		@input severity (str): `info`, `warning` or `error`
		@input condition (str): condition code (`SR_CONDITION_*`)
		@input message (str): description
		@input path (str, default=None): location in the content tree
		@input value (default=None): offending value
		@input strict (bool, default=False): raise SRDiagnosticError for `warning` and `error`

		@returns SRDiagnostic
		@raises ValueError for an unknown severity
	'''
	if severity not in SR_DIAGNOSTIC_SEVERITIES:
		raise ValueError('Invalid diagnostic severity: %s' % severity)
	diagnostic = SRDiagnostic(severity, condition, path, value, message)
	if diagnostics is not None:
		diagnostics.append(diagnostic)
	logger.log(SR_DIAGNOSTIC_LOG_LEVELS[severity], 'DICOM-SR %s', str(diagnostic))
	if strict and severity in SR_DIAGNOSTIC_STRICT_SEVERITIES:
		raise SRDiagnosticError(diagnostic)
	return diagnostic


def sr_diagnostics_with(diagnostics, severity=None, condition=None):
	'''	Diagnostics of a list filtered by severity and/or condition.

		@returns list of SRDiagnostic
	'''
	return [d for d in (diagnostics or [])
		if (severity is None or d.severity == severity) and (condition is None or d.condition == condition)]


# Coded Concept Orthanc API Headers
DCMSR_CODE_VALUE = 'Value'
DCMSR_CODE_MEANING = 'Meaning'
DCMSR_CODE_SCHEME = 'SchemeDesignator'
DCMSR_CDOE_SCHEME_VERSION = 'SchemeVersion'

# Document of properties attached to a tag, e.g. display attributes (see display_attrs)
DCMSR_TAG_META = 'Meta'


def srcode2dataset(dcmcode: Union[DcmCode,DcmCodedConcept], dcm_mdata=None, codedconcept_mapping=DCM_CODED_CONCEPT_MAPPING,
		omit_empty=False):
	'''	Convert the provided DICOM-SR code or coded concept to a pydicom.dataset.Dataset instance.

		The code value is written to `CodeValue`, `LongCodeValue` or `URNCodeValue` by convention
		(`dcm_code_value_attribute`), verbatim at any length. The Coding Scheme Version is Type 1C and
		is omitted when the code has none (never written empty, never derived).

		@input dcmcode (highdicom.sr.template.Code): code instance to convert to a Dataset.
		@input dcm_mdata (pydicom.dataset.Dataset, default=blank dataset instance): 
			dataset to which the code data should be added.
		@input omit_empty (bool, default=False): leave out the other attributes the code does not provide.
			By default they are written as empty elements.

		@returns pydicom.dataset.Dataset
	'''
	# Initialize dataset instance
	dcm_mdata = dcm_mdata or DcmDataset()

	# Map values from Code/CodedConcept to Dataset
	for k,v in codedconcept_mapping.items():
		value = getattr(dcmcode, k, None)
		if v == DCMHEADER_CODE_VALUE:
			for attr in DCM_CODE_VALUE_ATTRIBUTES:
				if attr in dcm_mdata:
					delattr(dcm_mdata, attr)
			setattr(dcm_mdata, dcm_code_value_attribute(value), None if value is None else str(value))
			continue
		if v == DCMHEADER_CODING_SCHEME_VERSION and (value is None or value == ''):
			continue
		if value is None and omit_empty:
			continue
		setattr(dcm_mdata, v, value)

	return dcm_mdata


def dataset2srcode(dcm:DcmDataset, codedconcept_mapping=DCM_CODED_CONCEPT_MAPPING, code_class=DcmCode):
	'''	Convert the provided DICOM dataset (or simplified-tags dictionary) to a code or coded concept.
		The value is read from whichever of `CodeValue`, `LongCodeValue` and `URNCodeValue` carries it;
		an empty Coding Scheme Version counts as absent.

		@input dcm (pydicom.Dataset): dataset to convert to the code/coded concept

		@returns highdicom.Code or highdicom.CodedConcept
	'''
	from ...imaging.helpers.headers import dcm_header_value

	# Map values from Dataset to Code/CodedConcept
	_kwargs = {}
	for k, v in codedconcept_mapping.items():
		if v == DCMHEADER_CODE_VALUE:
			_kwargs[k] = dcm_code_value(dcm)
		else:
			value = dcm_header_value(dcm, v)
			_kwargs[k] = None if value is None else str(value)

	return code_class(**pick(_kwargs, _kwargs.keys()))


def _placeholder_num_values(item, found):
	'''	Give every NUM item of a subtree without a value a placeholder one, which highdicom requires
		to read the item. Modifies the subtree in place.

		@input item (pydicom.Dataset): root of the subtree
		@input found (list): list the items changed are appended to
	'''
	if item.get('ValueType') == 'NUM' and not item.get('MeasuredValueSequence'):
		unit = DcmDataset()
		unit.CodeValue, unit.CodingSchemeDesignator, unit.CodeMeaning = '1', 'UCUM', 'no units'
		value = DcmDataset()
		value.NumericValue = 0
		value.MeasurementUnitsCodeSequence = DcmSequence([unit])
		item.MeasuredValueSequence = DcmSequence([value])
		found.append(item)
	for child in item.get('ContentSequence') or []:
		_placeholder_num_values(child, found)


def srcontent_from_dataset(item):
	'''	highdicom content item of a raw content item dataset, as a deep copy. Used to re-emit
		preserved (unmodelled) items; a by-reference item cannot be represented and is refused,
		wherever it sits in the item's subtree. A NUM item without a value is re-emitted with an empty
		Measured Value Sequence.

		@input item (pydicom.Dataset or highdicom ContentItem): the content item

		@returns highdicom.sr.value_types.ContentItem
		@raises SRDiagnosticError for a by-reference item (at any depth)
	'''
	if srcontent_holds_by_reference(item):
		raise SRDiagnosticError(SRDiagnostic(SR_DIAGNOSTIC_ERROR, SR_CONDITION_BY_REFERENCE_ITEM, None, item,
			'The document holds a by-reference content item (Referenced Content Item Identifier); rewriting '
			+ 'the content tree would invalidate its target, so the document is not rewritten.'))
	if isinstance(item, ContentItem):
		return copy.deepcopy(item)
	copied = copy.deepcopy(item)
	without_value = []
	_placeholder_num_values(copied, without_value)
	converted = ContentItem._from_dataset_derived(copied)
	for num in without_value:
		num.MeasuredValueSequence = DcmSequence()
	return converted


def srcontent_extra_items(items):
	'''	highdicom content items for the preserved `extra_items` of a model object, in document order.

		@input items (iterable of pydicom.Dataset or None)
		@returns list of highdicom.sr.value_types.ContentItem
		@raises SRDiagnosticError for a by-reference item (see `srcontent_from_dataset`)
	'''
	return [srcontent_from_dataset(item) for item in (items or ())]


# Transform Coordinate Systems (Reference Frames)


SONADOR_COORDINATE_TRANSFORM = 'SCoord3DTx'
SONADOR_COORDINATE_TRANSFORM_DESCRIPTION = '3D spatial coordinate transformation'
DCMSR_COORDINATE_TRANSFORM = DcmCode(SONADOR_COORDINATE_TRANSFORM, DCMSR_SONADOR_SR.value, 
	SONADOR_COORDINATE_TRANSFORM_DESCRIPTION, scheme_version=SONADOR_SCHEME_VERSION_02)

SONADOR_COORDINATE_TRANSFORM_MORPHOLOGY = '%s-Morph' % SONADOR_COORDINATE_TRANSFORM
SONADOR_COORDINATE_TRANSFORM_MORPHOLOGY_DESCRIPTION = '3D coordinate transform to facilitate morphology analysis'
DCMSR_COORDINATE_TRANSFORM_MORPHOLOGY = DcmCode(SONADOR_COORDINATE_TRANSFORM_MORPHOLOGY, DCMSR_SONADOR_SR.value, 
	SONADOR_COORDINATE_TRANSFORM_MORPHOLOGY_DESCRIPTION, scheme_version=SONADOR_SCHEME_VERSION_02)


def mtx4x4(mtx: np.ndarray) -> np.ndarray:
	'''	Transform the matrix to a 4x4 representation

		@input mtx (np.ndarray): matrix to transform the 4x4 representation

		@returns np.ndarray
	'''
	Mtx = np.identity(4)

	# Rotational matrix: 3x3 (9 elements)
	if mtx.shape == (3,3):
		Mtx[:3,:3] = mtx

	# Single row rotational matrix
	elif mtx.shape == (9,):
		Mtx[:3,:3] = mtx.reshape((3,3))

	# Rational matrix and x,y,z translational offset (12 elements)
	elif mtx.shape == (3,4):
		Mtx[:3,:4] = mtx

	# Single row rotational matrix and x,y,z translational offet
	elif mtx.shape == (12,):
		Mtx[:3,:4] = mtx.reshape((3,4))

	# Single row 4x4 matrix
	elif mtx.shape == (16,):
		Mtx[:4,:4] = mtx.reshape((4,4))

	# 4x4 matrix
	elif mtx.shape == (4,4):
		Mtx[:4,:4] = mtx

	else:
		raise ValueError('Invalid transform matrix dimensions: %s' % str(mtx.shape))

	return Mtx


def dcm_encode_reference_frame_transform_matrix(txmatrix: np.array, reference_uid:str=None, 
		transform_type:str=None, transform_comment:str=None, fiducials=None, ref_images=None, 
		codes:Sequence[Union[DcmCode,DcmCodedConcept,DcmDataset]]=None,
		dcm_mdata:DcmDataset=None, dcm_matrix_mdata:DcmDataset=None):
	'''	Create a Frame Of Reference DICOM structure for the provided transformation matrix.
		Refer to https://dicom.nema.org/medical/Dicom/2016c/output/chtml/part03/sect_C.20.2.html.
	
		@input txmatrix (np.array): transform matrix to be encoded
		@input dcm_mdata (pydicom.Dataset, default=new dataset): Dataset to w`hich 
			the frame of reference headers should be added to. If no dataset is provided
			a new one will be created.
		@input reference_uid (str, default=new UID): DICOM UID to be used for the
			reference frame. If an existing DICOM meta dataset is provided that already 
			has a frame of reference UID, the previously existing UID is retained.
		@input dcm_matrix_mdata (pydicom.Dataset, default=new dataset): dataset to which matrix headers
			should be added. If not dataset is provided, a new one will be created.

		@returns pydicom.Dataset
	'''
	# Ensure that the provided transform matrix is 4x4
	txmatrix = mtx4x4(txmatrix)

	# Transform dataset and reference UID
	dcm_mdata = dcm_mdata or DcmDataset()
	if not getattr(dcm_mdata, DCMHEADER_REFERENCE_FRAME, None):
		setattr(dcm_mdata, DCMHEADER_REFERENCE_FRAME, reference_uid or generate_uid())

	# Matrix Registration Sequence (single member sequence which contains the matrix sequence)
	dcm_matrix_registration_seq = getattr(dcm_mdata, DCMHEADER_MATRIX_REGISTRATION_SEQUENCE, None) or [DcmDataset()]
	dcm_matrix_registration_mdata = dcm_matrix_registration_seq[0]

	# MatrixSequence dataset: Transform type and 4x4 matrix (added to transform dataset)
	dcm_matrix_mdata = dcm_matrix_mdata or DcmDataset()
	transform_type = transform_type or DCM_MATRIX_RIGID	
	if not transform_type in DCM_MATRIX_TRANSFORM_SUPPORTED:
		raise ValueError('Unsupported transform type: %s' % transform_type)

	setattr(dcm_matrix_mdata, DCMHEADER_MATRIX_TRANSFORMATION_TYPE, transform_type)
	setattr(dcm_matrix_mdata, DCMHEADER_REFERENCE_FRAME_MATRIX, [v for v in txmatrix.flatten()])

	# Add to transform dataset
	matrix_sequence = getattr(dcm_mdata, DCMHEADER_MATRIX_REGISTRATION, None) or []
	matrix_sequence.append(dcm_matrix_mdata)
	setattr(dcm_matrix_registration_mdata, DCMHEADER_MATRIX_REGISTRATION, DcmSequence(m for m in matrix_sequence))

	# Add comment
	if transform_comment:
		setattr(dcm_matrix_registration_mdata, DCMHEADER_REFERENCE_FRAME_TRANFORM_COMMENT, transform_comment)

	# Add fiducial references
	if fiducials:
		setattr(dcm_mdata, DCMHEADER_MATRIX_USED_FIDUCIALS, DcmSequence(f for f in fiducials))

	# Add image references
	if ref_images:
		setattr(dcm_mdata, DCMHEADER_SR_REF_IMAGES_SEQ, DcmSequence(i for i in ref_images))

	# Add coded concepts
	if codes:
		setattr(dcm_matrix_registration_mdata, DCMHEADER_CONCEPT_CODE_SEQUENCE, DcmSequence(
			c for c in map(lambda d: srcode2dataset(d) if isinstance(d, (DcmCodedConcept, DcmCode)) else d, codes)))

	# Add Matrix Registration Sequence to transform dataset
	setattr(dcm_mdata, DCMHEADER_MATRIX_REGISTRATION_SEQUENCE, DcmSequence(dcm_matrix_registration_seq))

	return dcm_mdata



# DICOM-SR Helpers

DCMSR_PROCEDURE_REPORTED = DcmCode(
	value='121058', meaning='Procedure Reported', scheme_designator='DCM')


def srencode_procedure_reported(procedure, name:DcmCode=DCMSR_PROCEDURE_REPORTED, 
		relationship_type=RelationshipTypeValues.HAS_CONCEPT_MOD, sr_template=CodeContentItem):
	'''	DICOM-SR encode procedure reported value

		@returns CodeContentItem
	'''
	return sr_template(name=name, value=procedure, relationship_type=relationship_type)


DCMSR_PERSON_OBSERVER_NAME = dcmcodes.DCM.PersonObserverName
DCMSR_PERSON_OBSERVER_PLACEHOLDER = 'unknown^unknown'		# written by dcmjs when no person is named


def srencode_person_observer(name, relationship_type=RelationshipTypeValues.HAS_OBS_CONTEXT, sr_template=PnameContentItem):
	'''	Person Observer Name item (TID 1003, `121008`) in the form dcmjs writes: a bare PNAME item
		under HAS OBS CONTEXT, without an Observer Type item.

		@input name (str or pydicom.valuerep.PersonName): the person who made the findings, in DICOM
			PN form (`Family^Given`)
		@input relationship_type (RelationshipTypeValues, default=HAS OBS CONTEXT)
		@input sr_template (class, default=PnameContentItem): item class instantiated

		@returns highdicom.sr.value_types.PnameContentItem
	'''
	return sr_template(name=DCMSR_PERSON_OBSERVER_NAME, value=name, relationship_type=relationship_type)


def srencode_observation_context(context_uuid=None, device_name=None, person_name=None, sr_template=ObservationContext,
		**kwargs):
	'''	Encode the observation context of a report: the Person Observer (when named) followed by the
		Device Observer of the writing program (TID 1002 / 1004).

		@input context_uuid (str, default=new UID): Device Observer UID, generated per document
		@input device_name (str): Device Observer Name
		@input person_name (str, default=None): Person Observer Name; omitted when None or empty
		@input sr_template (class, default=ObservationContext): template class instantiated
		@input kwargs: other device observer attributes (`manufacturer_name`, `model_name`,
			`serial_number`, `physical_location`, `role_in_procedure`)

		@returns highdicom.sr.templates.ObservationContext
	'''
	observer_device = ObserverContext(
		observer_type=dcmcodes.DCM.Device,
		observer_identifying_attributes=DeviceObserverIdentifyingAttributes(
			uid=context_uuid or generate_uid(), name=device_name, **kwargs))

	context = sr_template(observer_device_context=observer_device)
	if person_name:
		context.insert(0, srencode_person_observer(person_name))
	return context



# DICOM-SR Spatial Data Structures

# 3D Coordinates

SONADOR_SCOORD3D = 'Sonador-Coord3D'
SONADOR_SCOORD3D_DESCRIPTION = '3D spatial point (x,y,z)'
DCMSR_SCOORD3D = DcmCode(SONADOR_SCOORD3D, DCMSR_SONADOR_SR.value, SONADOR_SCOORD3D_DESCRIPTION,
	scheme_version=SONADOR_SCHEME_VERSION_02)

SONADOR_SCOORD3D_ANTOMIC = '%s-Anatomic' % SONADOR_SCOORD3D
SONADOR_SCOORD3D_ANTOMIC_DESCRIPTION = '3D coordinate (x,y,z) describing an antomic landmark (or landmarks).'
DCMSR_SCOORD3D_ANATOMIC = DcmCode(SONADOR_SCOORD3D_ANTOMIC, DCMSR_SONADOR_SR.value, SONADOR_SCOORD3D_ANTOMIC_DESCRIPTION,
	scheme_version=SONADOR_SCHEME_VERSION_02)

SONADOR_SCOORD3D_LANDMARK = '%s-Landmark' % SONADOR_SCOORD3D
SONAODR_SCOORD3D_LANDMARK_DESCRIPTION = '3D coordinate (x,y,z) describing a non-anatomic landmark (or landmarks)'
DCMSR_SCOORD3D_LANDMARK = DcmCode(SONADOR_SCOORD3D_LANDMARK, DCMSR_SONADOR_SR.value, SONAODR_SCOORD3D_LANDMARK_DESCRIPTION,
	scheme_version=SONADOR_SCHEME_VERSION_02)

SONADOR_SCOORD3D_REGISTRATION = '%s-Registration' % SONADOR_SCOORD3D
SONADOR_SCOORD3D_REGISTRATION_DESCRIPTION = '3D coordinate describing a registration point (or points).'
DCMSR_SCOORD3D_REGISTRATION = DcmCode(SONADOR_SCOORD3D_REGISTRATION, DCMSR_SONADOR_SR.value, SONADOR_SCOORD3D_REGISTRATION_DESCRIPTION,
	scheme_version=SONADOR_SCHEME_VERSION_02)

SONADOR_SCOORD3D_TYPE_POINT_ELLIPSE = 'ELLIPSE'
SONADOR_SCOORD3D_TYPE_POINT_ELLIPSOID = 'ELLIPSOID'
SONADOR_SCOORD3D_TYPE_POINT_MULTIPOINT = 'MULTIPOINT'
SONADOR_SCOORD3D_TYPE_POINT_POINT = 'POINT'
SONADOR_SCOORD3D_TYPE_POINT_POLYGON = 'POLYGON'
SONADOR_SCOORD3D_TYPE_POINT_POLYLINE = 'POLYLINE'

SONADOR_SCOORD3D_TYPE_SUPPORTED = {
	SONADOR_SCOORD3D_TYPE_POINT_ELLIPSE: GraphicTypeValues3D.ELLIPSE,
	SONADOR_SCOORD3D_TYPE_POINT_ELLIPSOID: GraphicTypeValues3D.ELLIPSOID,
	SONADOR_SCOORD3D_TYPE_POINT_MULTIPOINT: GraphicTypeValues3D.MULTIPOINT,
	SONADOR_SCOORD3D_TYPE_POINT_POINT: GraphicTypeValues3D.POINT,
	SONADOR_SCOORD3D_TYPE_POINT_POLYGON: GraphicTypeValues3D.POLYGON,
	SONADOR_SCOORD3D_TYPE_POINT_POLYLINE: GraphicTypeValues3D.POLYLINE,
}


def points2array(pts: Union[ImageCoord,Sequence[ImageCoord],ImageCoordCollection,np.ndarray]) -> np.ndarray:
	'''	Convert the provided sequence of points to numpy array.
	'''
	# Image coordinate instance (single point)
	if isinstance(pts, ImageCoord):
		_p = np.array([*pts])

	# Sequence of points
	elif isinstance(pts, ImageCoordCollection) \
		or (isinstance(pts, (tuple, list)) and len(pts) and isinstance(pts[0], ImageCoord)):
		_p = np.array([[*p] for p in pts])

	# List/tuple encoding x,y,z data
	elif isinstance(pts, (tuple, list)) and len(pts) == 3 and isinstance(pts[0], numbers.Number):
		_p = np.array(pts)

	# Sequence of list/tuple encoding x,y,z data
	elif isinstance(pts, (tuple, list)) and len(pts) and all(
		[(len(p) == 3 and isinstance(p[0], numbers.Number)) for p in pts]):
		_p = np.array([p for p in pts])
	
	# NumPy array
	elif isinstance(pts, np.ndarray):
		_p = pts

	else:
		raise ValueError(
			'Unable to encode points, unsupported data type: "%s"' % type(pts))

	return _p


def srencode_coord3d(
		reference_frame: Union[str,DcmUid], 
		coord3d: Union[ImageCoord,Sequence[ImageCoord],ImageCoordCollection,ImageCoord,np.ndarray], 
		name: Union[DcmCode]=None, point_type: Union[GraphicTypeValues3D,str]=None, 
		sr_template=Scoord3DContentItem, relationship_type=RelationshipTypeValues.CONTAINS, **kwargs) -> Scoord3DContentItem:
	'''	DICOM-SR encode the provided 3D spatial coordinates (SCOORD3D)
		
		@input reference_frame (str or highdicom.uid.UID): UID of the coordinate system 
			(frame of reference) to use for the spatial data.
		@input coord3d (ImageCoord, iterable of ImageCoord, np.ndarray): spatial data to be encoded
		@input name (highdicom.sr.CodedConcent, default=SONADOR_SCOORD_3D): encoded point type/concept
			to use in SR document.
		@input point_type (str or highdicom.sr.GraphicTypeValue3D, 
			default=inferred point type based on structure of spatial_data): type of 3D point which the spatial data
			should be encoded to. If a point type is not explicitly declared, the method will attempt to determine
			the type of data (POINT or MULTIPOINT). For more specialized types of point data

		@returns highdicom.sr.Scoord3DContentItem
	'''
	# Add default name
	if name is None:
		name = DCMSR_SCOORD3D

	# Coerce spatial data to numpy array
	_p = points2array(coord3d)

	# Ensure that the spatial data array includes x,y,z dimensions.
	if (len(_p.shape) == 1 and _p.shape[0] != 3) \
		or (len(_p.shape) == 2 and _p.shape[1] != 3):
		_cidx = 1 if len(_p.shape) == 2 else 0
		raise ValueError(('Unable to encode spatial coordinates, incorrect number of columns: "%s". '
			+ 'Point instances must possess 3 columns of data (x,y,z).') % len(_p.shape[_cidx]))

	# Determine the type of point from the size of the spatial_data: POINT or MULTIPOINT.
	# For more specialized forms of point data (ELLIPSE, ELLIPSOID, POLYGON, POLYLINE),
	# the point type must be explicitly declared.
	if not point_type:
		point_type = GraphicTypeValues3D.MULTIPOINT if len(_p.shape) == 2 \
			else GraphicTypeValues3D.POINT if len(_p.shape) == 1 \
			else None		

	if not point_type in SONADOR_SCOORD3D_TYPE_SUPPORTED.keys() \
		and not point_type in SONADOR_SCOORD3D_TYPE_SUPPORTED.values():
		raise ValueError('Unsupported point type: %s' % point_type)

	# highdicom requires that individual points be formatted as a single row
	# in a (1,3) structure.
	if len(_p.shape) == 1: _p = _p.reshape((1,3))

	return sr_template(name=name, graphic_type=point_type, graphic_data=_p,
		frame_of_reference_uid=reference_frame, relationship_type=relationship_type, **kwargs)



# Measurements

SONADOR_LENGTH = dcmcodes.SCT.Length


def srencode_measurement(measurement:Measurement, name_default=SONADOR_LENGTH, sr_template=DcmMeasurement, layout=None, diagnostics=None, **kwargs):
	'''	DICOM-SR encode the provided measurement: a TID 300 NUM item with its coordinates and per-item
		properties (`sonador.apisettings.sr.measurements`), followed by the other NUM items of its group.
		A measurement holding only a value and a unit writes a bare NUM item. Other write options of the
		document are accepted and ignored.

		@input measurement (Measurement)
		@input name_default (Code, default=SCT Length): concept name when the measurement names none
		@input sr_template (class, default=highdicom Measurement): template class instantiated
		@input layout (str, default=nested): layout of the per-item properties (`DCMSR_LAYOUT_NESTED` or
			`DCMSR_LAYOUT_FLAT`)
		@input diagnostics (list, default=None): list the write diagnostics are appended to

		@returns highdicom.sr.Measurement: the NUM item, then any further NUM items of the group
		@raises ValueError for a missing or invalid concept name
		@raises SRDiagnosticError for degenerate geometry, or a value that cannot be computed
	'''
	from .measurements import srencode_measurement_coordinates, srencode_measurement_children, srencode_measurement_values
	mname = measurement.name or name_default

	if not mname or not isinstance(mname, (DcmCode, DcmCodedConcept)):
		raise ValueError(('Unable to DICOM-SR encode measurement instance. Invalid `name` value "%s". '
			'`name` is required property and must be a Code or `CodedConcept`.') % mname)

	value, unit = measurement.resolve_value(diagnostics=diagnostics)
	coordinates = srencode_measurement_coordinates(measurement)
	encoded = sr_template(name=mname, value=value, unit=unit, algorithm_id=measurement.algorithm,
		tracking_identifier=None if measurement.has_group_identity else measurement.uid,
		referenced_images=measurement.ref_images, referenced_real_world_value_map=measurement.ref_value,
		referenced_coordinates=[coordinates] if coordinates is not None else None,
		**pick(measurement, ('qualifier', 'derivation', 'finding_sites', 'method', 'properties')))

	item = encoded[0]
	if measurement.keeps_stored_value and measurement.stored_value_item is not None:
		item.MeasuredValueSequence = DcmSequence([copy.deepcopy(measurement.stored_value_item)])
	children = srencode_measurement_children(measurement, layout=layout)
	if children:
		if not hasattr(item, 'ContentSequence'):
			item.ContentSequence = ContentSequence()
		for child in children:
			item.ContentSequence.append(child)
	for value_item in srencode_measurement_values(measurement):
		encoded.append(value_item)
	return encoded


def srencode_finding(finding:Finding, sr_template=DcmQualitativeEvaluation, layout=None, **kwargs):
	'''	DICOM-SR encode the provided finding: a CODE content item (TID 1501 "Finding" for Series Tags)
		with the per-finding children of the Sonador-SR 0.3 profile (image reference, extended meta,
		display attributes, preserved extra items) in the layout of the document class being written.

		@input finding (Finding)
		@input sr_template (class, default=QualitativeEvaluation): template class instantiated
		@input layout (str, default=nested): `sonador.apisettings.sr.seriestag.DCMSR_LAYOUT_NESTED` or
			`DCMSR_LAYOUT_FLAT`

		@returns highdicom.sr.QualitativeEvaluation: a one-item content sequence
	'''
	from .seriestag import srencode_finding_children
	evaluation = sr_template(finding.name, finding.finding)
	children = srencode_finding_children(finding, layout=layout)
	if children:
		item = evaluation[0]
		item.ContentSequence = ContentSequence(children)
	return evaluation



# Reports

DCMSR_SONADOR_DATA_REPORT = DcmCode('Clinical-Report', DCMSR_SONADOR_SR.value,
	'Sonador Clinical Data Report', scheme_version=SONADOR_SCHEME_VERSION_02)



# Report Groups

DCMSR_REPORT_GROUP = DcmCode('125007', 'DCM', 'Report Group')
DCMSR_REPORT_METADATA = DcmCode('%s.Meta' % DCMSR_SONADOR_DATA_REPORT.value, DCMSR_SONADOR_SR.value,
	'Metadata associated with a clinical report', scheme_version=SONADOR_SCHEME_VERSION_02)
DCMSR_REPORT_VOLUME_MEAUREMENTS = DcmCode('%s.Volume-Measurements' % DCMSR_SONADOR_DATA_REPORT.value,
	DCMSR_SONADOR_SR.value, 'Imaging Volume Meaurements', scheme_version=SONADOR_SCHEME_VERSION_02)


def srencode_report_group(tracking_id:Union[TrackingIdentifier,str], 
		measurements:MeasurementCollection=None, findings:FindingCollection=None,
		sr_template=MeasurementsAndQualitativeEvaluations, tracking_uid=None, extra_items=None, layout=None, diagnostics=None, **kwargs):
	'''	DICOM-SR encode the collection of measurements and findings using the provided template
	
		@input tracking_id (str or highdicom.sr.TrackingIdentifier): tracking identifier
			associated with the measurement group
		@input measurements (sonador.apisettings.MeasurementCollection, default=None): measurements
			to be encoded to the provided template.
		@input findings (sonador.apisettings.FindingCollection): findings to be encoded to the
			provided template.
		@input tracking_uid (str, default=None): tracking unique identifier paired with a string
			`tracking_id` (generated when None)
		@input extra_items (iterable of pydicom.Dataset, default=None): preserved content items of the
			group, re-emitted after the template-owned items
		@input layout (str, default=None): layout of the per-finding properties (see `srencode_finding`)
		@input diagnostics (list, default=None): list the measurement write diagnostics are appended to

		@returns highdicom.sr.templates.MeasurementsAndQualitativeEvaluations (an instance of `sr_template`)
	'''
	# Convert string value to DICOM-SR Tracking Identifier instance
	if isinstance(tracking_id, str):
		tracking_id = TrackingIdentifier(uid=tracking_uid, identifier=tracking_id)

	group = sr_template(tracking_identifier=tracking_id, 
		measurements=[m.create_sr(layout=layout, diagnostics=diagnostics) for m in measurements] if measurements else measurements, 
		qualitative_evaluations=[f.create_sr(layout=layout) for f in findings] if findings else findings, **kwargs)

	# Preserved items are re-emitted under the group, after the template-owned items
	preserved = srcontent_extra_items(extra_items)
	if preserved:
		group_item = group[0]
		if not hasattr(group_item, 'ContentSequence'):
			group_item.ContentSequence = ContentSequence()
		for item in preserved:
			group_item.ContentSequence.append(item)

	return group



