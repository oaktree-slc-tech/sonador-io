'''	Reference frames in a Sonador-SR 0.3 document: the wire format of the frame graph and its resolution.

	A patient-domain measurement or point set is stored in millimetres in the frame its SCOORD3D
	`ReferencedFrameOfReferenceUID` names: an **imaging-backed** frame (the Frame of Reference of a series
	the report references) or an **application** frame the document declares. Frames form a forest: a frame
	has at most one parent, related by a rigid 4x4 matrix (frame -> parent). Three records encode it, each
	keyed by the frame's UID:

	* **Node**: one `Sonador-ReferenceFrame` CONTAINER per application frame (and per imaging-backed source of
		an explicit edge), a direct `CONTAINS` child of the root: UID, Name, Unit, optional Description and
		identity Code, and Parent (UIDREF) when the frame has an edge. A parentless root has no Parent and no
		Registration item.
	* **Edge**: one Registration Sequence (0070,0308) item on the SR dataset: outer `FrameOfReferenceUID` = the
		source frame; `MatrixRegistrationSequence[0]` with the edge marker `Sonador-ReferenceFrame.Edge` in
		`ConceptNameCodeSequence` (read from `RegistrationTypeCodeSequence` as well) and the frame name as the
		Transformation Comment (3006,00C8); `MatrixSequence[0]` with `RIGID` and the 16 coefficients (3006,00C6),
		row-major, DICOM layout, millimetres. The parent is named by the container's Parent only.
	* **Declarations**: root-level UIDREF items, regenerated on every write from the classification of the
		preceding read: `.Primary` (the selected primary imaging frame), `.Legacy` (a retained container-less item
		accepted as a one-hop edge to that primary) and `.Unverified` (a retained UID the read quarantined).

	The Registration Sequence stays on the SR dataset, where the client has always written it: a documented
	extension of the SR IOD, kept for compatibility with the existing writers and documents. The Spatial
	Registration IOD relates every registered frame to one target (a star); the frame graph here is a tree,
	whose parent is recorded in the container, so a reader that ignores the containers must not apply an item's
	matrix as a transform to any frame: it cannot know the item's target.

	**Resolution** runs after every record has been read, in two phases, then paths are built:
	* Phase 1, node identity. A UID is **established** when it has a record of its own: a referenced series'
		Frame of Reference, or its own valid container. A UID known only from its own Registration item, a status
		declaration or another container's Parent field (a *mention*) is known, but no edge can target it. A
		geometry UID with no evidence at all is `unknown-frame`.
	* Phase 2, edge classification: the rows of `classify_frame`, first match wins, invalid and conflicting
		evidence before any positive row.
	* The **quarantine** set is every UID an input `.Unverified` names plus every UID classified in a row that
		writes `.Unverified` back. It is computed before any reachability query, so the first read of a damaged
		document and every read after a save give the same paths. A path uses trusted and accepted legacy edges,
		forwards or through the inverse, and never passes through a quarantined UID in any position. An exact
		frame match (`native`) needs no path.

	**Trust**. An edge is trusted only when its source is not quarantined, its item carries the marker, a
	container with the same UID names a Parent that is an established node, the matrix is rigid and the edge
	makes no cycle. A label or a rigid matrix alone never authorises a path. A quarantined parent leaves its
	children's edges trusted (they write no declaration) and blocks every path through it.

	**Legacy documents**. A Registration item without the marker and without a container of its own is a legacy
	candidate: a one-hop transform written by a Sonador writer before frames were declared. In an **unmarked**
	document (no container, marker or declaration) it is trusted only with a proven pre-tree writer
	(`ManufacturerModelName` `Sonador-IO` with a parseable `SoftwareVersions` below `DCMSR_FRAMES_MIN_VERSION`, or
	a `SCoord3DTx*` method code on the item when the document has no `ManufacturerModelName` at all or names
	`Sonador-IO` with a missing, `dev` or unparseable version), exactly one imaging-backed frame among the references
	and a rigid matrix. A missing, unparseable or `dev` version with no qualifying method code proves nothing, and a
	document naming any other equipment model never qualifies, method code or not. In a **marked** document it is trusted only when a `.Legacy` names it,
	no `.Unverified` names it, exactly one valid `.Primary` is present and the matrix is rigid; the equipment
	headers of a marked document are never consulted. An accepted legacy edge **ends at** its recorded primary and
	is never rebound: a save that would change the persisted primary while retaining it is refused
	(`legacy-target-change`).

	**Re-save**. Preservation never raises trust: every retained record the read did not trust is written back
	with one `.Unverified`, every accepted legacy item with `.Legacy` (and the `.Primary`), so any later reader
	reproduces the same state whatever the equipment headers then say. Lowering trust is permitted: a
	`.Legacy` with no valid primary, or both declarations on one UID, is written back as one `.Unverified`
	(`frame-status-demoted`). A quarantine is **sticky**: it is applied before every positive rule on every read
	and re-emitted on every save, until the caller re-declares the frame in a session (`FrameGraph.remove_frame`,
	then `add_frame` / `add_edge`), which writes it afresh with no declaration. Imported items, containers and
	opaque records are written back verbatim in their slots and multiplicity; nothing is synthesised for a
	quarantined UID.

	What the standard provides: the Registration Sequence and its Matrix Registration items, Frame of Reference
	UIDs, SCOORD3D. What this client extends: the Registration Sequence on an SR dataset, the frame containers,
	the edge marker and the root declarations, all Sonador-SR 0.3 concepts. The Sonador Viewer is to read the same
	records with the same rules, and its regenerating save is to carry them through, classifying a pre-tree exchange
	document under the input's equipment profile before stamping the Viewer's own. The fixtures and the case table
	in `ftests/test_sr/fixtures/frames` hold the expected outcomes for those readers: the Python suite verifies this
	reader against them; a consuming reader or writer verifies itself against the same table. Specification:
	oak-tree/medical-imaging/sonador-client#45.
'''
import copy, logging
from collections import OrderedDict

import numpy as np

from pydicom import Dataset as DcmDataset, Sequence as DcmSequence

from highdicom.sr.templates import Code
from highdicom.sr.enum import RelationshipTypeValues
from highdicom.sr.value_types import ContainerContentItem, ContentSequence, TextContentItem, UIDRefContentItem, CodeContentItem

from ..base import SONADOR_SR, SONADOR_IO, SONADOR_SCHEME_VERSION_03, DCM_MATRIX_RIGID, DCMHEADER_REFERENCE_FRAME, \
	DCMHEADER_MATRIX_REGISTRATION_SEQUENCE, DCMHEADER_MATRIX_REGISTRATION, DCMHEADER_MATRIX_TRANSFORMATION_TYPE, \
	DCMHEADER_REFERENCE_FRAME_MATRIX, DCMHEADER_REFERENCE_FRAME_TRANFORM_COMMENT, DCMHEADER_CONCEPT_CODE_SEQUENCE, \
	DCMHEADER_MATRIX_USED_FIDUCIALS, DCMHEADER_SR_VALUE_TYPE, DCMHEADER_REGISTRATION_SEQUENCE, \
	ReferenceFrame, FrameEdge, FrameGraph, ImageTransformMatrix, frame_unit, rigid_failure, matrix_4x4, \
	DCMSR_FRAME_EDGE_TRUSTED, DCMSR_FRAME_EDGE_UNVERIFIED, DCMSR_FRAME_EDGE_LEGACY, DCMSR_FRAME_EVIDENCE_REFERENCE, \
	DCMSR_FRAME_EVIDENCE_CONTAINER, DCMSR_FRAME_EVIDENCE_ITEM, DCMSR_FRAME_EVIDENCE_DECLARATION, DCMSR_FRAME_EVIDENCE_MENTION, \
	DCMSR_FRAME_NAME_CONTAINER, DCMSR_FRAME_NAME_COMMENT, DCMSR_TRANSFORM_TOLERANCE, DCMSR_FRAME_UNITS, DCMSR_UNIT_MM
from . import sr_diagnostic, srcontent_is_named, dcm_code_matches, dcm_code_value, srcode2dataset, \
	SR_DIAGNOSTIC_INFO, SR_DIAGNOSTIC_WARNING, SR_DIAGNOSTIC_ERROR, SRDiagnostic, SRDiagnosticError, \
	SR_CONDITION_FRAME_EDGE_UNVERIFIED, SR_CONDITION_FRAME_NODE_QUARANTINED, SR_CONDITION_FRAME_STATUS_CONFLICT, \
	SR_CONDITION_FRAME_STATUS_DEMOTED, SR_CONDITION_FRAME_EDGE_LEGACY, SR_CONDITION_FRAME_NODE_INVALID, \
	SR_CONDITION_FRAME_NODE_CONFLICT, SR_CONDITION_FRAME_NODE_MENTIONED_ONLY, SR_CONDITION_FRAME_PRIMARY_UNRESOLVED, \
	SR_CONDITION_FRAME_PRIMARY_INVALID, SR_CONDITION_FRAME_PRIMARY_CHANGED, SR_CONDITION_LEGACY_TARGET_CHANGE, \
	SONADOR_COORDINATE_TRANSFORM

logger = logging.getLogger(__name__)


# Version boundary of the legacy rule: the first Sonador IO release that declares frames (writes containers,
# markers and declarations). A `Sonador-IO` document of a parseable version below it is a pre-tree document.
DCMSR_FRAMES_MIN_VERSION = '0.5.0a3'


# Concepts (Sonador-SR 0.3; every code value is longer than 16 characters, so it is written as LongCodeValue)

def _frame_code(suffix, meaning):
	value = SONADOR_REFERENCE_FRAME + (('.%s' % suffix) if suffix else '')
	return Code(value, SONADOR_SR, meaning, scheme_version=SONADOR_SCHEME_VERSION_03)


SONADOR_REFERENCE_FRAME = 'Sonador-ReferenceFrame'
DCMSR_FRAME_CONTAINER = _frame_code(None, 'Reference frame')
DCMSR_FRAME_UID = _frame_code('UID', 'Frame UID')
DCMSR_FRAME_NAME = _frame_code('Name', 'Frame name')
DCMSR_FRAME_UNIT = _frame_code('Unit', 'Source point unit')
DCMSR_FRAME_PARENT = _frame_code('Parent', 'Parent frame UID')
DCMSR_FRAME_DESCRIPTION = _frame_code('Description', 'Frame description')
DCMSR_FRAME_CODE = _frame_code('Code', 'Frame identity code')
DCMSR_FRAME_PRIMARY = _frame_code('Primary', 'Primary imaging frame UID')
DCMSR_FRAME_LEGACY = _frame_code('Legacy', 'Legacy one-hop edge to the primary, accepted')
DCMSR_FRAME_UNVERIFIED = _frame_code('Unverified', 'Frame quarantined: no path through this UID')
DCMSR_FRAME_EDGE = _frame_code('Edge', 'Edge to declared parent frame')

DCMSR_FRAME_DECLARATIONS = (DCMSR_FRAME_PRIMARY, DCMSR_FRAME_LEGACY, DCMSR_FRAME_UNVERIFIED)

# Attribute the edge marker is also read from
DCMHEADER_REGISTRATION_TYPE_CODE_SEQUENCE = 'RegistrationTypeCodeSequence'

# Status a retained record is written back with
DCMSR_FRAME_WRITE_LEGACY = 'legacy'
DCMSR_FRAME_WRITE_UNVERIFIED = 'unverified'

# Reasons of an unverified or quarantined record
DCMSR_FRAME_REASON_QUARANTINED = 'quarantined'
DCMSR_FRAME_REASON_NODE_CONFLICT = 'frame-node-conflict'
DCMSR_FRAME_REASON_NODE_INVALID = 'frame-node-invalid'
DCMSR_FRAME_REASON_MISSING_ITEM = 'missing-item'
DCMSR_FRAME_REASON_NO_PARENT = 'no-parent'
DCMSR_FRAME_REASON_NO_MARKER = 'no-marker'
DCMSR_FRAME_REASON_PARENT_UNKNOWN = 'parent-unknown'
DCMSR_FRAME_REASON_NON_RIGID = 'non-rigid'
DCMSR_FRAME_REASON_CYCLE = 'cycle'
DCMSR_FRAME_REASON_SECOND_PARENT = 'second-parent'
DCMSR_FRAME_REASON_CONFLICTING_DUPLICATE = 'conflicting-duplicate'
DCMSR_FRAME_REASON_NO_CONTAINER = 'no-container'
DCMSR_FRAME_REASON_LEGACY_AMBIGUOUS = 'legacy-target-ambiguous'
DCMSR_FRAME_REASON_LEGACY_UNKNOWN = 'legacy-target-unknown'
DCMSR_FRAME_REASON_LEGACY_PROVENANCE = 'legacy-provenance'
DCMSR_FRAME_CHECK_MATRIX_TYPE = 'matrix type'
DCMSR_FRAME_CHECK_COEFFICIENTS = 'coefficients'


# ---- records ----


class FrameItemRecord:
	'''	Registration Sequence item as read.

		@property slot (int): position in the Registration Sequence
		@property dataset (pydicom.Dataset): the item, kept verbatim
		@property uid (str or None): outer `FrameOfReferenceUID` (the source frame)
		@property marked (bool): carries the edge marker
		@property method_codes (list of str): `SCoord3DTx*` code values
		@property comment (str or None): Transformation Comment
		@property matrix (numpy.ndarray or None): the 4x4, or None when the coefficients are unusable
		@property check (str or None): rigid check the matrix fails (`matrix type`, `coefficients` or a rigid check)
	'''
	def __init__(self, slot, dataset):
		self.slot = slot
		self.dataset = dataset
		uid = dataset.get(DCMHEADER_REFERENCE_FRAME)
		self.uid = str(uid) if uid else None
		self.marked, self.method_codes, self.comment, self.matrix, self.check = False, [], None, None, None
		self.matrix_type = None
		registrations = dataset.get(DCMHEADER_MATRIX_REGISTRATION_SEQUENCE) or []
		registration = registrations[0] if len(registrations) else None
		if registration is None:
			self.check = DCMSR_FRAME_CHECK_COEFFICIENTS
			return
		codes = list(registration.get(DCMHEADER_CONCEPT_CODE_SEQUENCE) or []) + list(registration.get(DCMHEADER_REGISTRATION_TYPE_CODE_SEQUENCE) or [])
		self.marked = any(dcm_code_matches(code, DCMSR_FRAME_EDGE) for code in codes)
		self.method_codes = [dcm_code_value(code) for code in codes
			if (dcm_code_value(code) or '').startswith(SONADOR_COORDINATE_TRANSFORM)]
		comment = registration.get(DCMHEADER_REFERENCE_FRAME_TRANFORM_COMMENT)
		self.comment = str(comment) if comment else None
		matrices = registration.get(DCMHEADER_MATRIX_REGISTRATION) or []
		matrix_item = matrices[0] if len(matrices) else None
		if matrix_item is None:
			self.check = DCMSR_FRAME_CHECK_COEFFICIENTS
			return
		self.matrix_type = str(matrix_item.get(DCMHEADER_MATRIX_TRANSFORMATION_TYPE) or '') or None
		values = matrix_item.get(DCMHEADER_REFERENCE_FRAME_MATRIX)
		try:
			values = [float(v) for v in (values if values is not None else [])]
		except (TypeError, ValueError):
			values = []
		if len(values) != 16:
			self.check = DCMSR_FRAME_CHECK_COEFFICIENTS
			return
		self.matrix = np.asarray(values, dtype=float).reshape((4, 4))
		self.check = rigid_failure(self.matrix)
		if self.check is None and self.matrix_type != DCM_MATRIX_RIGID:
			self.check = DCMSR_FRAME_CHECK_MATRIX_TYPE

	def same_edge(self, other):
		'''	Whether two items for one UID describe the same edge (identical duplicates coalesce).
		'''
		if self.marked != other.marked or (self.matrix is None) != (other.matrix is None):
			return False
		return self.matrix is None or bool(np.allclose(self.matrix, other.matrix, atol=DCMSR_TRANSFORM_TOLERANCE, rtol=0))


class FrameContainerRecord:
	'''	`Sonador-ReferenceFrame` container as read.

		@property slot (int): number of preserved root items before it (its position among them)
		@property dataset (pydicom.Dataset): the container, kept verbatim
		@property uid, name, unit, parent, description (str or None)
		@property code (pydicom.Dataset or None): the identity code
		@property problem (str or None): why the container is malformed (`no UID`, `no Name`, `no Unit`, `self-parent`)
	'''
	def __init__(self, slot, dataset):
		self.slot = slot
		self.dataset = dataset
		values = OrderedDict()
		for child in (dataset.get('ContentSequence') or []):
			for concept, attr in ((DCMSR_FRAME_UID, 'UID'), (DCMSR_FRAME_PARENT, 'UID'), (DCMSR_FRAME_NAME, 'TextValue'),
					(DCMSR_FRAME_UNIT, 'TextValue'), (DCMSR_FRAME_DESCRIPTION, 'TextValue')):
				if srcontent_is_named(child, concept):
					values.setdefault(concept.value, []).append(str(child.get(attr) or '') or None)
			if srcontent_is_named(child, DCMSR_FRAME_CODE):
				sequence = child.get('ConceptCodeSequence') or []
				values.setdefault(DCMSR_FRAME_CODE.value, []).append(sequence[0] if len(sequence) else None)

		def one(concept):
			found = values.get(concept.value) or []
			return found[0] if len(found) == 1 else None

		self.uid = one(DCMSR_FRAME_UID)
		self.name = one(DCMSR_FRAME_NAME)
		self.unit = one(DCMSR_FRAME_UNIT)
		self.parent = one(DCMSR_FRAME_PARENT)
		self.description = one(DCMSR_FRAME_DESCRIPTION)
		self.code = one(DCMSR_FRAME_CODE)
		self.repeated = [c for c, found in values.items() if len(found) > 1]
		self.problem = None

	def check(self, imaging_backed):
		'''	Validity of the container: a UID; for an application frame, a Name and a Unit of the registry; no Parent
			naming its own UID; no repeated child.

			@input imaging_backed (bool): the UID is a referenced imaging frame (UID and Parent only)
			@returns str or None: the problem, or None for a valid container
		'''
		if not self.uid:
			return 'no UID'
		if self.repeated:
			return 'repeated %s' % ', '.join(self.repeated)
		if not imaging_backed and not self.name:
			return 'no Name'
		if not imaging_backed and (not self.unit or self.unit not in DCMSR_FRAME_UNITS):
			return 'no Unit' if not self.unit else 'unit %s outside the registry' % self.unit
		if self.parent and self.parent == self.uid:
			return 'self-parent'
		return None

	def declares(self):
		'''	Declared content, for comparing duplicate containers of one UID.
		'''
		code = (dcm_code_value(self.code), str(self.code.get('CodingSchemeDesignator') or '')) if self.code is not None else None
		return (self.name, self.unit, self.parent, self.description, code)


class FrameClassification:
	'''	Result of phase 2 for one UID.

		@property row (int): row of the classification table that matched
		@property state (str): `trusted`, `unverified`, `legacy`, or `root` for a frame with no outgoing edge
		@property reason (str or None): `DCMSR_FRAME_REASON_*`, with the failed check for `non-rigid:<check>`
		@property target (str or None): the parent (trusted or unverified edge with a declared parent) or the
			recorded primary (legacy edge)
		@property written_back (str or None): `legacy`, `unverified` or None
	'''
	def __init__(self, row, state, reason=None, target=None, written_back=None):
		self.row = row
		self.state = state
		self.reason = reason
		self.target = target
		self.written_back = written_back

	def __repr__(self):
		return 'FrameClassification(row=%r, state=%r, reason=%r, target=%r, written_back=%r)' % (
			self.row, self.state, self.reason, self.target, self.written_back)


class SRFrameRecords:
	'''	Frame records of one document: the Registration items, the frame containers and the root declarations,
		with the equipment profile of the writer. Built by the reader (`srdoc_frame_records`) and handed to
		`resolve_frame_graph`; the report keeps it to write the retained records back.

		@property items (list of FrameItemRecord), containers (list of FrameContainerRecord)
		@property primary, legacy, unverified (list of str): UIDs the root declarations name, in document order
		@property model_name, software_version (str or None): equipment profile of the document
		@property classification (OrderedDict): UID -> FrameClassification (after resolution)
		@property dropped (set of str): UIDs whose records the caller removed (re-declared frames)
	'''
	def __init__(self, items=None, containers=None, primary=None, legacy=None, unverified=None, model_name=None, software_version=None):
		self.items = list(items or [])
		self.containers = list(containers or [])
		self.primary = list(primary or [])
		self.legacy = list(legacy or [])
		self.unverified = list(unverified or [])
		self.model_name = model_name
		self.software_version = software_version
		self.classification = OrderedDict()
		self.dropped = set()

	@property
	def marked(self):
		'''	Whether the document declares frames: a container, a marked item or a root declaration.
		'''
		return bool(self.containers or any(i.marked for i in self.items) or self.primary or self.legacy or self.unverified)

	@property
	def has_records(self):
		return bool(self.items or self.containers or self.primary or self.legacy or self.unverified)

	def drop(self, uid):
		self.dropped.add(uid)

	def retained_items(self):
		return [i for i in self.items if i.uid not in self.dropped]

	def retained_containers(self):
		return [c for c in self.containers if c.uid is None or c.uid not in self.dropped]

	def written_back(self, uid):
		'''	Status the retained records of `uid` are written back with.

			@returns str (`legacy`, `unverified`) or None
		'''
		if uid in self.dropped:
			return None
		found = self.classification.get(uid)
		return found.written_back if found is not None else None


def _declaration_uids(items, code):
	return [str(i.get('UID') or '') for i in items if srcontent_is_named(i, code) and str(i.get(DCMHEADER_SR_VALUE_TYPE) or '') == 'UIDREF']


def srdoc_frame_records(ds, root_items, extra_items):
	'''	Read the frame records of a document.

		@input ds (pydicom.Dataset): the document
		@input root_items (iterable of (pydicom.Dataset, int)): root content items the report does not model, each
			with the number of preserved root items before it
		@input extra_items: unused; kept for the reader's signature

		@returns tuple (SRFrameRecords, set of int): the records, and the `id()` of every root item they consumed
	'''
	consumed, containers, declarations = set(), [], []
	for item, slot in root_items:
		if srcontent_is_named(item, DCMSR_FRAME_CONTAINER) and str(item.get(DCMHEADER_SR_VALUE_TYPE) or '') == 'CONTAINER':
			containers.append(FrameContainerRecord(slot, copy.deepcopy(item)))
			consumed.add(id(item))
		elif any(srcontent_is_named(item, code) for code in DCMSR_FRAME_DECLARATIONS):
			declarations.append(item)
			consumed.add(id(item))
	items = [FrameItemRecord(slot, copy.deepcopy(item)) for slot, item in enumerate(ds.get(DCMHEADER_REGISTRATION_SEQUENCE) or [])]
	versions = ds.get('SoftwareVersions')
	if versions is not None and not isinstance(versions, str):
		versions = list(versions)
		versions = str(versions[0]) if versions else None
	records = SRFrameRecords(items=items, containers=containers,
		primary=[u for u in _declaration_uids(declarations, DCMSR_FRAME_PRIMARY)],
		legacy=[u for u in _declaration_uids(declarations, DCMSR_FRAME_LEGACY)],
		unverified=[u for u in _declaration_uids(declarations, DCMSR_FRAME_UNVERIFIED)],
		model_name=str(ds.get('ManufacturerModelName') or '') or None,
		software_version=(str(versions) if versions else None))
	return records, consumed


# ---- legacy provenance ----


def _parse_version(text):
	'''	@returns packaging.version.Version, or None for a missing, `dev` or unparseable version
	'''
	if not text:
		return None
	try:
		from packaging.version import Version, InvalidVersion
	except ImportError:			# pragma: no cover
		return None
	try:
		return Version(str(text).strip())
	except InvalidVersion:
		return None


def pre_tree_writer(records, item):
	'''	Whether the writer of an unmarked document is proven to predate frame declarations:
		`Sonador-IO` with a parseable version below `DCMSR_FRAMES_MIN_VERSION`, or a `SCoord3DTx*` code on the item
		from a writer with no equipment profile or a Sonador-IO profile with no parseable version.

		@input records (SRFrameRecords)
		@input item (FrameItemRecord)
		@returns bool
	'''
	version = _parse_version(records.software_version)
	if records.model_name == SONADOR_IO and version is not None:
		return version < _parse_version(DCMSR_FRAMES_MIN_VERSION)
	if item.method_codes and (records.model_name is None or records.model_name == SONADOR_IO):
		return True
	return False


# ---- resolution ----


def resolve_frame_graph(records, imaging_frames, primary_frame_uid=None, diagnostics=None, strict=False):
	'''	Build the frame graph of a document: imaging-backed frames from the references, then the two-phase
		resolution of its records, then the quarantine set.

		@input records (SRFrameRecords or None): the document's frame records; None for a model built in Python
		@input imaging_frames (OrderedDict): Frame of Reference UID -> list of Series Instance UID, for the series
			the report references
		@input primary_frame_uid (str, default=None): the caller's primary
		@input diagnostics (list, default=None): list the diagnostics are appended to
		@input strict (bool, default=False): raise SRDiagnosticError on a `warning`

		@returns FrameGraph
		@raises ValueError for a caller primary that is not a referenced imaging frame
	'''
	graph = FrameGraph()
	for uid, series in imaging_frames.items():
		for series_uid in (series or [None]):
			graph.add_imaging_frame(uid, series_uid)
	records = records if records is not None else SRFrameRecords()
	graph.document = records

	def note(severity, condition, message, uid):
		sr_diagnostic(diagnostics, severity, condition, message, path='frame %s' % uid, value=uid, strict=strict)

	# Primary: the caller's (`frame-primary-changed` when it differs from a valid persisted one), else the valid
	# persisted one, else the only imaging frame when no `.Primary` is declared at all; an invalid or repeated
	# `.Primary` suppresses that fallback. A retained legacy edge still ends at the primary persisted with it
	if primary_frame_uid is not None and primary_frame_uid not in imaging_frames:
		raise ValueError('primary_frame_uid %s is not the Frame of Reference of a series the report references' % primary_frame_uid)
	persisted = None
	declared = records.primary
	if len(declared) == 1 and declared[0] in imaging_frames:
		persisted = declared[0]
	elif declared:
		note(SR_DIAGNOSTIC_WARNING, SR_CONDITION_FRAME_PRIMARY_INVALID, 'the document declares %d primary frames (%s); a valid primary '
			'is one Primary naming a referenced imaging frame' % (len(declared), ', '.join(declared)), ', '.join(declared))
	graph.persisted_primary_uid = persisted
	if persisted and primary_frame_uid and primary_frame_uid != persisted:
		note(SR_DIAGNOSTIC_INFO, SR_CONDITION_FRAME_PRIMARY_CHANGED, 'the caller selects primary %s; the document persists %s'
			% (primary_frame_uid, persisted), primary_frame_uid)
	if primary_frame_uid:
		graph.primary_uid = primary_frame_uid
	elif persisted:
		graph.primary_uid = persisted
	elif not declared and len(imaging_frames) == 1:
		graph.primary_uid = next(iter(imaging_frames))
	if graph.primary_uid is None and records.has_records:
		note(SR_DIAGNOSTIC_INFO, SR_CONDITION_FRAME_PRIMARY_UNRESOLVED, 'no primary imaging frame resolves; target-dependent calls '
			'need an explicit target', None)

	# Phase 1: evidence and node identity
	items_by_uid, containers_by_uid = OrderedDict(), OrderedDict()
	for item in records.items:
		if item.uid:
			items_by_uid.setdefault(item.uid, []).append(item)
	for container in records.containers:
		if container.uid:
			containers_by_uid.setdefault(container.uid, []).append(container)
		else:
			container.problem = 'no UID'
			note(SR_DIAGNOSTIC_WARNING, SR_CONDITION_FRAME_NODE_INVALID, 'frame container without a UID; retained verbatim', None)

	known = OrderedDict()
	for uid in list(imaging_frames) + list(containers_by_uid) + list(items_by_uid) + records.legacy + records.unverified:
		known.setdefault(uid, set())
	for uid in imaging_frames:
		known[uid].add(DCMSR_FRAME_EVIDENCE_REFERENCE)
	valid_container = OrderedDict()
	for uid, found in containers_by_uid.items():
		for container in found:
			container.problem = container.check(uid in imaging_frames)
		valid = [c for c in found if c.problem is None]
		if valid and len(valid) == len(found):
			known[uid].add(DCMSR_FRAME_EVIDENCE_CONTAINER)
			valid_container[uid] = valid
		for container in found:
			if container.parent:
				known.setdefault(container.parent, set()).add(DCMSR_FRAME_EVIDENCE_MENTION)
	for uid in items_by_uid:
		known[uid].add(DCMSR_FRAME_EVIDENCE_ITEM)
	for uid in records.legacy + records.unverified:
		known[uid].add(DCMSR_FRAME_EVIDENCE_DECLARATION)

	conflicting = set(uid for uid, found in valid_container.items() if len(set(c.declares() for c in found)) > 1)

	def established(uid):
		return uid in imaging_frames or (uid in valid_container and uid not in conflicting)

	for uid, evidence in known.items():
		if uid in imaging_frames:
			frame = graph.frame(uid)
		else:
			frame = ReferenceFrame(uid)
			graph._put(frame)
			frame.established = established(uid)
			frame.imported = True
		frame.evidence = set(evidence)
		if uid in valid_container and uid not in conflicting:
			container = valid_container[uid][0]
			if container.name:
				frame.name, frame.name_source = container.name, DCMSR_FRAME_NAME_CONTAINER
			if container.unit in DCMSR_FRAME_UNITS:
				frame.unit = frame_unit(container.unit)
			frame.description = container.description
			frame.code = container.code
		else:
			frame.name, frame.name_source = None, None
			comments = [i.comment for i in items_by_uid.get(uid, []) if i.comment]
			if comments and uid not in containers_by_uid:
				frame.name, frame.name_source = comments[0], DCMSR_FRAME_NAME_COMMENT
		if evidence == set((DCMSR_FRAME_EVIDENCE_MENTION,)):
			note(SR_DIAGNOSTIC_INFO, SR_CONDITION_FRAME_NODE_MENTIONED_ONLY, 'frame known only from another frame\'s Parent field; '
				'it can be the target of no edge', uid)

	# Phase 2: edge classification
	legacy_named, unverified_named = set(records.legacy), set(records.unverified)
	single_imaging = next(iter(imaging_frames)) if len(imaging_frames) == 1 else None
	classification = records.classification
	classification.clear()
	for uid in known:
		found = classify_frame(uid, records, items_by_uid.get(uid, []), containers_by_uid.get(uid, []), uid in imaging_frames,
			uid in legacy_named, uid in unverified_named, established, persisted, single_imaging)
		if found is not None:
			classification[uid] = found

	# Cycles among the otherwise trusted edges
	trusted = OrderedDict((uid, c.target) for uid, c in classification.items() if c.state == DCMSR_FRAME_EDGE_TRUSTED)
	for uid in list(trusted):
		node, seen = trusted.get(uid), [uid]
		while node in trusted and node not in seen:
			seen.append(node)
			node = trusted[node]
		if node == uid:
			for member in seen:
				classification[member] = FrameClassification(10, DCMSR_FRAME_EDGE_UNVERIFIED, DCMSR_FRAME_REASON_CYCLE,
					target=trusted[member], written_back=DCMSR_FRAME_WRITE_UNVERIFIED)

	# Quarantine: input declarations and this read's classification, before any path is built
	quarantine = set(unverified_named) | set(uid for uid, c in classification.items() if c.written_back == DCMSR_FRAME_WRITE_UNVERIFIED)
	for uid in quarantine:
		frame = graph.frame(uid)
		if frame is not None:
			frame.quarantined = True

	for uid, found in classification.items():
		frame = graph.frame(uid)
		frame.reason = found.reason
		if found.state in (DCMSR_FRAME_EDGE_TRUSTED, DCMSR_FRAME_EDGE_LEGACY, DCMSR_FRAME_EDGE_UNVERIFIED) and items_by_uid.get(uid):
			item = items_by_uid[uid][0]
			matrix = ImageTransformMatrix(item.matrix, uid=uid, comment=item.comment, source_frame=uid, target_frame=found.target) \
				if item.matrix is not None and found.state != DCMSR_FRAME_EDGE_UNVERIFIED else None
			edge = FrameEdge(uid, found.target, matrix, state=found.state, reason=found.reason,
				fiducials=item.dataset.get(DCMHEADER_MATRIX_USED_FIDUCIALS), imported=True)
			graph.edges[uid] = edge
		if found.state in (DCMSR_FRAME_EDGE_TRUSTED, DCMSR_FRAME_EDGE_LEGACY) or (found.target and uid in containers_by_uid):
			frame.parent_uid = found.target
		# Diagnostics, once per UID
		if found.row == 1:
			note(SR_DIAGNOSTIC_WARNING, SR_CONDITION_FRAME_NODE_QUARANTINED, 'a .Unverified declaration names this frame; it is '
				'excluded from every path until re-declared', uid)
			if uid in legacy_named:
				note(SR_DIAGNOSTIC_WARNING, SR_CONDITION_FRAME_STATUS_CONFLICT, '.Legacy and .Unverified both name this frame; '
					'it is quarantined and written back as .Unverified', uid)
		elif found.row == 2:
			note(SR_DIAGNOSTIC_WARNING, SR_CONDITION_FRAME_NODE_CONFLICT, 'two containers for this frame declare different values; '
				'both retained, the frame is quarantined', uid)
		elif found.row == 3:
			problems = sorted(set(c.problem for c in containers_by_uid.get(uid, []) if c.problem))
			note(SR_DIAGNOSTIC_WARNING, SR_CONDITION_FRAME_NODE_INVALID, 'malformed frame container (%s); retained verbatim, the '
				'frame is quarantined' % ', '.join(problems), uid)
		elif found.state == DCMSR_FRAME_EDGE_UNVERIFIED:
			note(SR_DIAGNOSTIC_WARNING, SR_CONDITION_FRAME_EDGE_UNVERIFIED, 'edge unverified (%s); its records are retained and '
				'written back unchanged, and no path passes through it' % found.reason, uid)
		elif found.state == DCMSR_FRAME_EDGE_LEGACY:
			note(SR_DIAGNOSTIC_INFO, SR_CONDITION_FRAME_EDGE_LEGACY, 'legacy one-hop edge to %s accepted (%s)'
				% (found.target, DCMSR_FRAME_REASON_LEGACY_PROVENANCE), uid)
	return graph


def classify_frame(uid, records, items, containers, imaging_backed, legacy, unverified, established, persisted, single_imaging):
	'''	Phase 2 classification of one UID: rows matched top to bottom, the first match decides.

		| # | Evidence | Result | Written back |
		|---|---|---|---|
		| 1 | `.Unverified` and any record of its own (item, or container with or without Parent) | quarantined | `.Unverified` |
		| 2 | two containers declaring different values | `frame-node-conflict` | `.Unverified` |
		| 3 | malformed container | `frame-node-invalid` | `.Unverified` |
		| - | two items with different coefficients | `conflicting-duplicate` | `.Unverified` |
		| 4 | container with Parent, no item | `missing-item` | `.Unverified` |
		| 5 | status declaration only | `missing-item` | `.Unverified` |
		| 6 | container without Parent, item present | `no-parent` | `.Unverified` |
		| 7 | container with Parent, item without marker | `no-marker` | `.Unverified` |
		| 8 | container with Parent, marked item, Parent not established | `parent-unknown` | `.Unverified` |
		| 9 | ... matrix fails a rigid check | `non-rigid:<check>` | `.Unverified` |
		| 10 | ... cycle (resolved after this function) | `cycle` | `.Unverified` |
		| 11 | container with Parent, marked item, Parent established, rigid | trusted | none |
		| 12 | container without Parent, no item, no `.Unverified` | root | none |
		| 13 | no container, marked item | `no-container` | `.Unverified` |
		| 14 | no container, unmarked item, `.Legacy`, one valid `.Primary`, rigid | legacy to the Primary | `.Legacy` |
		| 15 | no container, unmarked item, `.Legacy`, no valid Primary or non-rigid | `legacy-target-unknown` / `non-rigid` | `.Unverified` |
		| 16 | no container, unmarked item, marked document, no declaration | `no-marker` | `.Unverified` |
		| 17 | no container, unmarked item, unmarked document, pre-tree writer, one imaging frame, rigid | legacy to that frame | `.Legacy` + `.Primary` |
		| 18 | no container, unmarked item, unmarked document, otherwise | `no-marker` / `legacy-target-ambiguous` / `non-rigid` | `.Unverified` |

		@returns FrameClassification, or None for a UID with no outgoing candidate and no record of its own
	'''
	U = DCMSR_FRAME_WRITE_UNVERIFIED
	unverified_state = DCMSR_FRAME_EDGE_UNVERIFIED

	def bad(row, reason, target=None):
		return FrameClassification(row, unverified_state, reason, target=target, written_back=U)

	parent = None
	declared = [c for c in containers if c.problem is None]
	if declared:
		parent = declared[0].parent

	if unverified and (items or containers):
		return FrameClassification(1, unverified_state, DCMSR_FRAME_REASON_QUARANTINED, target=parent, written_back=U)
	if len(declared) > 1 and len(set(c.declares() for c in declared)) > 1:
		return bad(2, DCMSR_FRAME_REASON_NODE_CONFLICT)
	if any(c.problem is not None for c in containers):
		return bad(3, DCMSR_FRAME_REASON_NODE_INVALID)
	if len(items) > 1 and not all(items[0].same_edge(other) for other in items[1:]):
		return bad(9, DCMSR_FRAME_REASON_CONFLICTING_DUPLICATE, target=parent)
	item = items[0] if items else None
	container = declared[0] if declared else None

	if container is not None and parent and item is None:
		return bad(4, DCMSR_FRAME_REASON_MISSING_ITEM, target=parent)
	if container is None and item is None:
		if legacy or unverified:
			return bad(5, DCMSR_FRAME_REASON_MISSING_ITEM)
		return None
	if container is not None and not parent and item is not None:
		return bad(6, DCMSR_FRAME_REASON_NO_PARENT)
	if container is not None and parent:
		if not item.marked:
			return bad(7, DCMSR_FRAME_REASON_NO_MARKER, target=parent)
		if not established(parent):
			return bad(8, DCMSR_FRAME_REASON_PARENT_UNKNOWN, target=parent)
		if item.check is not None:
			return bad(9, '%s:%s' % (DCMSR_FRAME_REASON_NON_RIGID, item.check), target=parent)
		return FrameClassification(11, DCMSR_FRAME_EDGE_TRUSTED, None, target=parent)
	if container is not None:
		return FrameClassification(12, 'root')

	# No container: an item only
	if item.marked:
		return bad(13, DCMSR_FRAME_REASON_NO_CONTAINER)
	if records.marked:
		if legacy:
			if persisted is None:
				return bad(15, DCMSR_FRAME_REASON_LEGACY_UNKNOWN)
			if item.check is not None:
				return bad(15, '%s:%s' % (DCMSR_FRAME_REASON_NON_RIGID, item.check))
			return FrameClassification(14, DCMSR_FRAME_EDGE_LEGACY, DCMSR_FRAME_REASON_LEGACY_PROVENANCE, target=persisted,
				written_back=DCMSR_FRAME_WRITE_LEGACY)
		return bad(16, DCMSR_FRAME_REASON_NO_MARKER)
	if not pre_tree_writer(records, item):
		return bad(18, DCMSR_FRAME_REASON_NO_MARKER)
	if single_imaging is None:
		return bad(18, DCMSR_FRAME_REASON_LEGACY_AMBIGUOUS)
	if item.check is not None:
		return bad(18, '%s:%s' % (DCMSR_FRAME_REASON_NON_RIGID, item.check))
	return FrameClassification(17, DCMSR_FRAME_EDGE_LEGACY, DCMSR_FRAME_REASON_LEGACY_PROVENANCE, target=single_imaging,
		written_back=DCMSR_FRAME_WRITE_LEGACY)


# ---- writer ----


def srencode_frame_container(frame, parent_uid=None):
	'''	`Sonador-ReferenceFrame` container of a frame the report generates.

		@input frame (ReferenceFrame)
		@input parent_uid (str, default=frame.parent_uid): the declared parent
		@returns highdicom ContainerContentItem
	'''
	parent_uid = parent_uid if parent_uid is not None else frame.parent_uid
	container = ContainerContentItem(name=DCMSR_FRAME_CONTAINER, relationship_type=RelationshipTypeValues.CONTAINS,
		is_content_continuous=False)
	children = [UIDRefContentItem(name=DCMSR_FRAME_UID, value=frame.uid, relationship_type=RelationshipTypeValues.CONTAINS)]
	if not frame.imaging_backed:
		children.append(TextContentItem(name=DCMSR_FRAME_NAME, value=frame.name, relationship_type=RelationshipTypeValues.CONTAINS))
		children.append(TextContentItem(name=DCMSR_FRAME_UNIT, value=frame.unit.ucum, relationship_type=RelationshipTypeValues.CONTAINS))
	if parent_uid:
		children.append(UIDRefContentItem(name=DCMSR_FRAME_PARENT, value=parent_uid, relationship_type=RelationshipTypeValues.CONTAINS))
	if frame.description and not frame.imaging_backed:
		children.append(TextContentItem(name=DCMSR_FRAME_DESCRIPTION, value=frame.description, relationship_type=RelationshipTypeValues.CONTAINS))
	if frame.code is not None and not frame.imaging_backed:
		code = frame.code if isinstance(frame.code, Code) else _code_from_dataset(frame.code)
		children.append(CodeContentItem(name=DCMSR_FRAME_CODE, value=code, relationship_type=RelationshipTypeValues.CONTAINS))
	container.ContentSequence = ContentSequence(children)
	return container


def _code_from_dataset(ds):
	from . import dataset2srcode
	return dataset2srcode(ds)


def srencode_frame_declaration(code, uid):
	'''	Root-level declaration (`.Primary`, `.Legacy`, `.Unverified`).

		@returns highdicom UIDRefContentItem
	'''
	return UIDRefContentItem(name=code, value=uid, relationship_type=RelationshipTypeValues.CONTAINS)


def srencode_frame_edge(edge, name=None):
	'''	Registration item of an edge the report generates: the source UID, the edge marker followed by the
		matrix's own method codes, the frame name as the Transformation Comment, `RIGID` and the 16 coefficients,
		with the matrix's Used Fiducials and Referenced Image Sequence on the outer item.

		@input edge (FrameEdge)
		@input name (str, default=the matrix comment): Transformation Comment
		@returns pydicom.Dataset
	'''
	from . import dcm_encode_reference_frame_transform_matrix
	codes = [DCMSR_FRAME_EDGE] + [c for c in (edge.matrix.codes or []) if not dcm_code_matches(
		srcode2dataset(c) if isinstance(c, Code) else c, DCMSR_FRAME_EDGE)]
	return dcm_encode_reference_frame_transform_matrix(matrix_4x4(edge.matrix.matrix), reference_uid=edge.source_uid,
		transform_type=DCM_MATRIX_RIGID, transform_comment=name or edge.matrix.comment, fiducials=edge.fiducials,
		ref_images=edge.matrix.ref_images, codes=codes)


def frame_write_plan(graph, diagnostics=None):
	'''	What a write emits for the frame graph: the root declarations, the containers (imported ones in their
		slots, generated ones after them) and the Registration items (imported ones in their slots, generated ones
		after them). Declarations are regenerated from the classification of the preceding read.

		@input graph (FrameGraph)
		@input diagnostics (list, default=None): `frame-status-demoted` is recorded here

		@returns dict with keys
			- `declarations` (list of highdicom UIDRefContentItem), in the order Primary, Legacy, Unverified
			- `containers` (list of (int slot, content item or pydicom.Dataset))
			- `items` (list of pydicom.Dataset)
			- `declared` (dict): `primary`, `legacy`, `unverified` UID lists, for inspection
		@raises SRDiagnosticError `legacy-target-change` when a retained legacy edge would be written beside a
			primary other than the one recorded with it
	'''
	records = graph.document
	declared = OrderedDict((('primary', []), ('legacy', []), ('unverified', [])))
	if graph.primary_uid:
		declared['primary'].append(graph.primary_uid)
	containers, items = [], []
	if records is not None:
		for uid, found in records.classification.items():
			if uid in records.dropped:
				continue
			if found.written_back == DCMSR_FRAME_WRITE_LEGACY:
				if found.target != graph.primary_uid:
					raise SRDiagnosticError(SRDiagnostic(SR_DIAGNOSTIC_ERROR, SR_CONDITION_LEGACY_TARGET_CHANGE, 'frame %s' % uid, uid,
						'The accepted legacy edge %s ends at %s; writing it beside primary %s would rebind its matrix. Keep the '
						'recorded primary, or convert and re-register the geometry through trusted edges and remove the legacy '
						'frame before saving' % (uid, found.target, graph.primary_uid)))
				declared['legacy'].append(uid)
			elif found.written_back == DCMSR_FRAME_WRITE_UNVERIFIED:
				declared['unverified'].append(uid)
				if uid in records.legacy:
					sr_diagnostic(diagnostics, SR_DIAGNOSTIC_INFO, SR_CONDITION_FRAME_STATUS_DEMOTED, 'stale .Legacy written back '
						'as .Unverified', path='frame %s' % uid, value=uid)
		containers = [(c.slot, c.dataset) for c in records.retained_containers()]
		items = [i.dataset for i in records.retained_items()]
	for kind in ('legacy', 'unverified'):
		declared[kind] = sorted(set(declared[kind]))

	# Generated records: parents before children, then by name, then by UID
	generated = [f for f in graph if not f.imported and (not f.imaging_backed or (f.uid in graph.edges and not graph.edges[f.uid].imported))]
	order = _generation_order(graph, generated)
	slot = max([s for s, _ in containers] or [0])
	for frame in order:
		containers.append((slot, srencode_frame_container(frame)))
	for frame in order:
		edge = graph.edges.get(frame.uid)
		if edge is not None and not edge.imported:
			items.append(srencode_frame_edge(edge, name=frame.name))
	declarations = [srencode_frame_declaration(DCMSR_FRAME_PRIMARY, uid) for uid in declared['primary']] \
		+ [srencode_frame_declaration(DCMSR_FRAME_LEGACY, uid) for uid in declared['legacy']] \
		+ [srencode_frame_declaration(DCMSR_FRAME_UNVERIFIED, uid) for uid in declared['unverified']]
	return dict(declarations=declarations, containers=containers, items=items, declared=declared)


def _generation_order(graph, frames):
	'''	Frames in writing order: parents before children (topological), then by name, then by UID.
	'''
	pending = list(frames)
	done, order = set(), []
	uids = set(f.uid for f in pending)
	while pending:
		ready = [f for f in pending if not (f.parent_uid in uids and f.parent_uid not in done)]
		if not ready:
			ready = pending
		ready.sort(key=lambda f: ((f.name or ''), f.uid))
		first = ready[0]
		order.append(first)
		done.add(first.uid)
		pending.remove(first)
	return order
