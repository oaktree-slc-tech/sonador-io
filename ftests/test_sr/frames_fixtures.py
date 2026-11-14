'''	Generator of the reference-frame fixtures: synthetic DICOM-SR documents for the Sonador Viewer's frame reader
	(oak-tree/medical-imaging/ohif-viewers#174) and the shared reader case table (`sonador-io.frames-cases.<date>.json`),
	which records, per scenario and frame, what the Sonador IO reader finds and the note state the Viewer shows.

	Every document is built on the synthetic study of the other SR fixtures: series P (the primary) and S, with
	different Frames of Reference, are referenced; a third series S2 shares P's Frame of Reference and is in the
	study but referenced by no document. Clean documents are written by the client (`create_sr`); each damaged
	or legacy variant is derived from one of them by the dataset operations listed in its `changes`, each naming
	the UID, container, declaration or matrix it changes. A scenario named `...-save1` / `...-save2` is the client's
	re-save of its `derived_from` input after an unrelated edit (the description of L1, or of the first Length
	that is not a subject of the scenario). UIDs, dates and times are fixed, so the output is reproducible byte for
	byte.

	Regenerate (from the directory containing the `sonador` package):

		python -m sonador.ftests.test_sr.frames_fixtures [output directory]

	The default output directory is `ftests/test_sr/fixtures/frames`.
'''
import copy, io, json, logging, os, sys
from collections import OrderedDict

import numpy as np
import pydicom
from pydicom.dataset import Dataset
from pydicom.sequence import Sequence
from pydicom.uid import generate_uid
from pydicom.sr.codedict import codes

from ...apisettings.base import LengthMeasurement, ReferenceFrame, DCMSR_FRAME_EDGE_USABLE
from ...apisettings.sr import dcm_code_matches, srcode2dataset, srcontent_is_named, SONADOR_COORDINATE_TRANSFORM, \
	DCMSR_COORDINATE_TRANSFORM
from ...apisettings.sr.frames import DCMSR_FRAME_CONTAINER, DCMSR_FRAME_UID, DCMSR_FRAME_PARENT, DCMSR_FRAME_UNIT, \
	DCMSR_FRAME_EDGE, DCMSR_FRAME_PRIMARY, DCMSR_FRAME_LEGACY, DCMSR_FRAME_UNVERIFIED, DCMSR_FRAMES_MIN_VERSION
from ...imaging.helpers.sr import SonadorComprehensiveSR3D, SonadorImagingMeasurementReport

logger = logging.getLogger(__name__)

FIXTURE_DATE = '2026-1114'
FIXTURES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fixtures', 'frames')
CASES_FILE = 'sonador-io.frames-cases.%s.json' % FIXTURE_DATE


def fixture_name(scenario):
	return 'sonador-io.frames-%s.%s.dcm' % (scenario, FIXTURE_DATE)


# ---- synthetic study ----

STUDY_UID = '2.25.1001'
MR_IMAGE_STORAGE = '1.2.840.10008.5.1.4.1.1.4'
SERIES = OrderedDict((
	('P', dict(series_uid='2.25.1002', sop_uid='2.25.1003', frame='2.25.45100', number='1', label='P')),
	('S', dict(series_uid='2.25.45201', sop_uid='2.25.45202', frame='2.25.45200', number='2', label='S')),
	('S2', dict(series_uid='2.25.45301', sop_uid='2.25.45302', frame='2.25.45100', number='3', label='S2')),
))
P_FOR, S_FOR = SERIES['P']['frame'], SERIES['S']['frame']

# Frames, by alias
FRAMES = OrderedDict((
	('P', P_FOR),
	('S', S_FOR),
	('A2', '2.25.45410'),		# L2's application frame (trusted edge to P in the intact document)
	('SIB', '2.25.45415'),		# unaffected sibling with a trusted edge to P
	('R3', '2.25.45420'),		# L3's parentless application root
	('R3B', '2.25.45425'),		# clean parentless root (control)
	('T', '2.25.45430'),		# intermediate parent
	('X', '2.25.45431'),		# child of T
	('Y', '2.25.45432'),		# child of T
	('G6', '2.25.45460'),		# frame of the genuine legacy item
	('U5', '2.25.45490'),		# known to nothing
))
FRAME_NAMES = {'A2': 'Anatomic', 'SIB': 'Femur', 'R3': 'Implant', 'R3B': 'Spare', 'T': 'Pelvis', 'X': 'Cup', 'Y': 'Stem', 'G6': 'Plan'}
ALIASES = dict((uid, alias) for alias, uid in FRAMES.items() if alias != 'S2')

# Lengths, by alias: tracking UID and frame
LENGTHS = OrderedDict((
	('L1', ('2.25.45501', 'P')), ('L2', ('2.25.45502', 'A2')), ('L3', ('2.25.45503', 'R3')), ('L4', ('2.25.45504', 'S')),
	('L5', ('2.25.45505', 'U5')), ('LX', ('2.25.45506', 'X')), ('LY', ('2.25.45507', 'Y')), ('L6', ('2.25.45508', 'G6')),
	('LT', ('2.25.45509', 'T')),
))
LENGTH_POINTS = {'L1': ((0, 0, 0), (10, 0, 0)), 'L2': ((1, 2, 3), (4, 6, 3)), 'L3': ((0, 0, 0), (0, 5, 0)),
	'L4': ((5, 5, 5), (5, 5, 15)), 'L5': ((2, 2, 2), (2, 9, 2)), 'LX': ((0, 0, 0), (3, 0, 0)), 'LY': ((0, 0, 0), (0, 0, 4)),
	'L6': ((1, 1, 1), (1, 1, 7)), 'LT': ((0, 0, 0), (6, 0, 0))}

# Edges: rigid matrices (DICOM layout, millimetres)
RZ = np.array([[0.0, -1.0, 0.0, 10.0], [1.0, 0.0, 0.0, 0.0], [0.0, 0.0, 1.0, 0.0], [0.0, 0.0, 0.0, 1.0]])
RX = np.array([[1.0, 0.0, 0.0, 0.0], [0.0, 0.0, -1.0, 20.0], [0.0, 1.0, 0.0, 0.0], [0.0, 0.0, 0.0, 1.0]])
SHIFT = np.array([[1.0, 0.0, 0.0, 5.0], [0.0, 1.0, 0.0, 0.0], [0.0, 0.0, 1.0, -5.0], [0.0, 0.0, 0.0, 1.0]])
NON_RIGID = np.array([[2.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0], [0.0, 0.0, 1.0, 0.0], [0.0, 0.0, 0.0, 1.0]])

# Equipment profiles
PRE_TREE = ('Sonador-IO', '0.5.0a2')
FOREIGN = ('Foreign-Planner', '2.1')

FIXED_DATE, FIXED_TIME = '20261114', '120000'
PERSON_OBSERVER = 'Observer^Test'

# Loaded-series sets of the Viewer expectations: label -> series aliases
LOADED_SETS = OrderedDict((('none', ()), ('P', ('P',)), ('S', ('S',)), ('S2', ('S2',)), ('P+S2', ('P', 'S2'))))


def _uid(name):
	return generate_uid(entropy_srcs=['sonador-client frames fixtures', name])


def evidence_dataset(alias):
	series = SERIES[alias]
	ds = Dataset()
	for name in ('PatientBirthDate', 'PatientSex', 'AccessionNumber', 'StudyDate', 'StudyTime'):
		setattr(ds, name, '')
	ds.PatientName, ds.PatientID, ds.StudyID, ds.StudyInstanceUID = 'Anonymous', 'ANONYMOUS', '1', STUDY_UID
	ds.SeriesInstanceUID, ds.SOPInstanceUID, ds.SOPClassUID = series['series_uid'], series['sop_uid'], MR_IMAGE_STORAGE
	return ds


def references_map(aliases):
	return OrderedDict((SERIES[a]['series_uid'], SERIES[a]['frame']) for a in aliases)


# ---- clean documents (client writer) ----


def build_document(name, root='exchange', referenced=('P', 'S'), frames=('A2', 'SIB', 'R3', 'R3B'), edges=None,
		lengths=('L1', 'L2', 'L3', 'L4', 'L5'), retarget=None):
	'''	A clean document written by the client.

		@input name (str): scenario name (seeds the document UIDs)
		@input root (str): `exchange` (TID 1500 root, concept 126000) or `application` (Sonador-SR root)
		@input referenced (tuple of series aliases)
		@input frames (tuple of frame aliases): application frames declared (named by FRAME_NAMES)
		@input edges (dict alias -> (matrix, parent alias), default: A2 -> P by RZ, SIB -> P by RX)
		@input lengths (tuple of length aliases)
		@input retarget (dict length alias -> frame alias, default L5 -> U5): Lengths written in P
			and moved to the named frame after the write (a frame unknown to the graph)
		@returns pydicom.Dataset
	'''
	edges = edges if edges is not None else {'A2': (RZ, 'P'), 'SIB': (RX, 'P')}
	retarget = retarget if retarget is not None else {'L5': 'U5'}
	cls = SonadorImagingMeasurementReport if root == 'exchange' else SonadorComprehensiveSR3D
	report = cls(report_procedure=codes.LN.CTUnspecifiedBodyRegion, dcm_evidence=[evidence_dataset(a) for a in referenced],
		person_observer=PERSON_OBSERVER, imaging_frames=references_map(referenced), primary_frame_uid=P_FOR)
	graph = report.frames
	for alias in frames:
		graph.add_frame(ReferenceFrame(FRAMES[alias], name=FRAME_NAMES[alias]))
	pending = OrderedDict((a, e) for a, e in edges.items())
	while pending:
		for alias, (matrix, parent) in list(pending.items()):
			if FRAMES[parent] in graph and (graph.frame(FRAMES[parent]).imaging_backed or parent not in pending):
				graph.add_edge(matrix, FRAMES[alias], FRAMES[parent])
				del pending[alias]
	measurements = []
	for alias in lengths:
		tracking_uid, frame = LENGTHS[alias]
		stored = 'P' if alias in retarget else frame
		measurements.append(LengthMeasurement([tuple(float(c) for c in p) for p in LENGTH_POINTS[alias]],
			frame_of_reference_uid=FRAMES[stored], tracking_uid=tracking_uid, description='%s in %s' % (alias, frame)))
	report.measurements = measurements
	ds = write(report, name)
	for alias, frame in retarget.items():
		if alias in lengths:
			set_geometry_frame(ds, LENGTHS[alias][0], FRAMES[frame])
	return ds


def write(report, name, **kwargs):
	'''	The report's document with fixed UIDs, dates and times, serialised and read back.
	'''
	doc = report.create_sr(record_instances=True, dcmsr_series_uid=_uid(name + '/series'), dcmsr_instance_uid=_uid(name + '/sop'),
		device_observer_uid=_uid(name + '/device'), **kwargs)
	return fix_dates(roundtrip(doc))


def fix_dates(ds):
	for keyword in ('ContentDate', 'InstanceCreationDate', 'SeriesDate', 'StudyDate'):
		if keyword in ds:
			setattr(ds, keyword, FIXED_DATE if keyword != 'StudyDate' else ds.StudyDate)
	for keyword in ('ContentTime', 'InstanceCreationTime', 'SeriesTime'):
		if keyword in ds:
			setattr(ds, keyword, FIXED_TIME)
	for item in (ds.get('ContributingEquipmentSequence') or []):
		if 'ContributionDateTime' in item:
			item.ContributionDateTime = FIXED_DATE + FIXED_TIME
	return ds


def roundtrip(doc):
	buf = io.BytesIO()
	doc.save_as(buf)
	buf.seek(0)
	return pydicom.dcmread(buf)


def tobytes(ds):
	buf = io.BytesIO()
	ds.save_as(buf, enforce_file_format=True)
	return buf.getvalue()


def read_report(ds, referenced, root, primary_frame_uid=None, **kwargs):
	cls = SonadorImagingMeasurementReport if root == 'exchange' else SonadorComprehensiveSR3D
	return cls.from_sr(copy.deepcopy(ds), imaging_frames=references_map(referenced), primary_frame_uid=primary_frame_uid, **kwargs)


def client_save(ds, name, referenced, root, subjects=()):
	'''	Client re-save of a document after an unrelated edit: the description of L1 (in P), else of the first Length
		that is not a subject of the scenario.
	'''
	report = read_report(ds, referenced, root)
	measurements = [m for g in (report.groups or []) for m in (g.measurements or [])]
	subject_uids = set(LENGTHS[a][0] for a in subjects if a in LENGTHS)
	edited = next((m for m in measurements if m.tracking_uid == LENGTHS['L1'][0]), None) \
		or next(m for m in measurements if m.tracking_uid not in subject_uids)
	edited.description = 'edited before save'
	return write(report, name)


# ---- dataset operations (each names what it changes) ----


def _root(ds):
	return ds.ContentSequence


def _named(item, code):
	return srcontent_is_named(item, code)


def _container_uid(item):
	for child in (item.get('ContentSequence') or []):
		if _named(child, DCMSR_FRAME_UID):
			return str(child.get('UID') or '') or None
	return None


def containers_of(ds, uid=None):
	return [item for item in _root(ds) if _named(item, DCMSR_FRAME_CONTAINER) and (uid is None or _container_uid(item) == uid)]


def items_of(ds, uid=None):
	return [item for item in (ds.get('RegistrationSequence') or []) if uid is None or str(item.FrameOfReferenceUID) == uid]


def remove_container(ds, uid):
	for item in containers_of(ds, uid):
		_root(ds).remove(item)
	return {'op': 'remove-container', 'uid': uid}


def remove_all_containers(ds):
	removed = [_container_uid(c) for c in containers_of(ds)]
	for item in containers_of(ds):
		_root(ds).remove(item)
	return {'op': 'remove-all-containers', 'uids': removed}


def set_container_parent(ds, uid, parent):
	container = containers_of(ds, uid)[0]
	children = container.ContentSequence
	for child in list(children):
		if _named(child, DCMSR_FRAME_PARENT):
			children.remove(child)
	if parent:
		item = Dataset()
		item.RelationshipType, item.ValueType = 'CONTAINS', 'UIDREF'
		item.ConceptNameCodeSequence = Sequence([srcode2dataset(DCMSR_FRAME_PARENT)])
		item.UID = parent
		position = next((i + 1 for i, c in enumerate(children) if _named(c, DCMSR_FRAME_UNIT) or _named(c, DCMSR_FRAME_UID)), 1)
		children.insert(position, item)
	return {'op': 'set-container-parent', 'uid': uid, 'parent': parent}


def remove_container_child(ds, uid, code):
	container = containers_of(ds, uid)[0]
	for child in list(container.ContentSequence):
		if _named(child, code):
			container.ContentSequence.remove(child)
	return {'op': 'remove-container-child', 'uid': uid, 'concept': code.value}


def duplicate_container(ds, uid, parent):
	container = containers_of(ds, uid)[0]
	position = list(_root(ds)).index(container)
	duplicate = copy.deepcopy(container)
	_root(ds).insert(position + 1, duplicate)
	for child in duplicate.ContentSequence:
		if _named(child, DCMSR_FRAME_PARENT):
			child.UID = parent
	return {'op': 'duplicate-container', 'uid': uid, 'parent': parent}


def remove_item(ds, uid):
	sequence = ds.RegistrationSequence
	for item in items_of(ds, uid):
		sequence.remove(item)
	if not len(sequence):
		del ds.RegistrationSequence
	return {'op': 'remove-item', 'uid': uid}


def set_matrix(ds, uid, matrix, check=None):
	item = items_of(ds, uid)[0]
	item.MatrixRegistrationSequence[0].MatrixSequence[0].FrameOfReferenceTransformationMatrix = [float(v) for v in np.asarray(matrix).flatten()]
	return {'op': 'set-matrix', 'uid': uid, 'matrix': [float(v) for v in np.asarray(matrix).flatten()], 'fails': check}


def strip_marker(ds, uid=None):
	stripped = []
	for item in items_of(ds, uid):
		registration = item.MatrixRegistrationSequence[0]
		codes_left = [c for c in (registration.get('ConceptNameCodeSequence') or []) if not dcm_code_matches(c, DCMSR_FRAME_EDGE)]
		if codes_left:
			registration.ConceptNameCodeSequence = Sequence(codes_left)
		elif 'ConceptNameCodeSequence' in registration:
			del registration.ConceptNameCodeSequence
		stripped.append(str(item.FrameOfReferenceUID))
	return {'op': 'strip-edge-marker', 'uids': stripped}


def add_method_code(ds, uid):
	registration = items_of(ds, uid)[0].MatrixRegistrationSequence[0]
	sequence = list(registration.get('ConceptNameCodeSequence') or [])
	sequence.append(srcode2dataset(DCMSR_COORDINATE_TRANSFORM))
	registration.ConceptNameCodeSequence = Sequence(sequence)
	return {'op': 'add-method-code', 'uid': uid, 'code': SONADOR_COORDINATE_TRANSFORM}


DECLARATION_CODES = {'primary': DCMSR_FRAME_PRIMARY, 'legacy': DCMSR_FRAME_LEGACY, 'unverified': DCMSR_FRAME_UNVERIFIED}


def add_declaration(ds, kind, uid):
	item = Dataset()
	item.RelationshipType, item.ValueType = 'CONTAINS', 'UIDREF'
	item.ConceptNameCodeSequence = Sequence([srcode2dataset(DECLARATION_CODES[kind])])
	item.UID = uid
	root = _root(ds)
	first = next((i for i, c in enumerate(root) if _named(c, DCMSR_FRAME_CONTAINER)), len(root))
	root.insert(first, item)
	return {'op': 'add-declaration', 'kind': kind, 'uid': uid}


def remove_declarations(ds, kind=None):
	root = _root(ds)
	kinds = [kind] if kind else list(DECLARATION_CODES)
	removed = []
	for item in list(root):
		for k in kinds:
			if _named(item, DECLARATION_CODES[k]):
				removed.append((k, str(item.UID)))
				root.remove(item)
	return {'op': 'remove-declarations', 'kind': kind or 'all', 'removed': [{'kind': k, 'uid': u} for k, u in removed]}


def set_equipment(ds, model, version):
	for keyword, value in (('ManufacturerModelName', model), ('SoftwareVersions', version)):
		if value is None:
			if keyword in ds:
				delattr(ds, keyword)
		else:
			setattr(ds, keyword, value)
	return {'op': 'set-equipment', 'model': model, 'version': version}


def set_geometry_frame(ds, tracking_uid, frame_uid):
	def walk(items):
		for item in items:
			if str(item.get('ValueType') or '') == 'SCOORD3D' and owner[0] == tracking_uid:
				item.ReferencedFrameOfReferenceUID = frame_uid
			walk(item.get('ContentSequence') or [])
	owner = [None]
	for container in _root(ds):
		if not _named(container, codes.DCM.ImagingMeasurements):
			continue
		for group in container.ContentSequence:
			owner[0] = next((str(c.UID) for c in group.ContentSequence if _named(c, codes.DCM.TrackingUniqueIdentifier)), None)
			walk(group.ContentSequence)
	return {'op': 'set-geometry-frame', 'tracking_uid': tracking_uid, 'frame': frame_uid}


def add_evidence_series(ds, alias):
	series = SERIES[alias]
	evidence = ds.CurrentRequestedProcedureEvidenceSequence[0]
	entry = Dataset()
	entry.SeriesInstanceUID = series['series_uid']
	instance = Dataset()
	instance.ReferencedSOPClassUID, instance.ReferencedSOPInstanceUID = MR_IMAGE_STORAGE, series['sop_uid']
	entry.ReferencedSOPSequence = Sequence([instance])
	evidence.ReferencedSeriesSequence.append(entry)
	return {'op': 'add-evidence-series', 'series': series['series_uid']}


def unmark(ds, equipment):
	'''	Turn a client-written document into an unmarked (pre-tree) one: frame containers, declarations and edge
		markers removed, the equipment profile replaced.
	'''
	return [remove_all_containers(ds), remove_declarations(ds), strip_marker(ds), set_equipment(ds, *equipment)]


# ---- scenarios ----


class Scenario:
	'''	One fixture: how it is built, what it references, and its subjects (frames and Lengths) with the results
		the case table records for them.
	'''
	def __init__(self, name, build, root='exchange', referenced=('P', 'S'), derived_from=None, subjects=(), lengths=(),
			panel=False, caller_primary=None, note=None):
		self.name = name
		self.build = build
		self.root = root
		self.referenced = referenced
		self.derived_from = derived_from
		self.subjects = subjects
		self.lengths = lengths
		self.panel = panel
		self.caller_primary = caller_primary
		self.note = note
		self.changes = []
		self.dataset = None


def scenarios():
	'''	Every scenario, in build order (a derived scenario follows its input).

		@returns OrderedDict name -> Scenario
	'''
	found = OrderedDict()

	def add(scenario):
		found[scenario.name] = scenario
		return scenario

	def derive(name, base, ops, **kwargs):
		def build(scenario, done):
			ds = copy.deepcopy(done[base].dataset)
			scenario.changes = [op(ds) for op in ops]
			return ds
		kwargs.setdefault('root', found[base].root)
		kwargs.setdefault('referenced', found[base].referenced)
		kwargs.setdefault('lengths', found[base].lengths)
		kwargs.setdefault('subjects', found[base].subjects)
		return add(Scenario(name, build, derived_from=base, **kwargs))

	def saved(name, base, **kwargs):
		def build(scenario, done):
			source = done[base]
			scenario.changes = [{'op': 'client-save', 'edit': 'description of an unrelated Length'}]
			return client_save(source.dataset, name, source.referenced, source.root, subjects=source.lengths_subject)
		kwargs.setdefault('root', found[base].root)
		kwargs.setdefault('referenced', found[base].referenced)
		kwargs.setdefault('lengths', found[base].lengths)
		kwargs.setdefault('subjects', found[base].subjects)
		return add(Scenario(name, build, derived_from=base, **kwargs))

	def clean(name, root='exchange', **kwargs):
		build_kwargs = dict((k, kwargs.pop(k)) for k in ('frames', 'edges', 'lengths_built', 'retarget') if k in kwargs)
		if 'lengths_built' in build_kwargs:
			build_kwargs['lengths'] = build_kwargs.pop('lengths_built')
		referenced = kwargs.get('referenced', ('P', 'S'))

		def build(scenario, done):
			scenario.changes = [{'op': 'client-write'}]
			return build_document(name, root=root, referenced=referenced, **build_kwargs)
		return add(Scenario(name, build, root=root, **kwargs))

	MAIN = ('L1', 'L2', 'L3', 'L4', 'L5')
	NO_L3 = ('L1', 'L2', 'L4', 'L5')
	A2, T, R3 = FRAMES['A2'], FRAMES['T'], FRAMES['R3']

	# Item 14: panel fixtures, both roots
	for root in ('application', 'exchange'):
		r = root[:3]
		clean('%s-intact' % r, root=root, lengths=MAIN, subjects=('P', 'S', 'A2', 'SIB', 'R3', 'R3B', 'U5'), panel=True)
		derive('%s-all-containers-removed' % r, '%s-intact' % r, [remove_all_containers], subjects=('A2', 'SIB', 'R3', 'U5'), panel=True)
		derive('%s-l2-non-rigid' % r, '%s-intact' % r, [lambda ds: set_matrix(ds, A2, NON_RIGID, 'orthonormal rotation')],
			subjects=('A2', 'SIB'), panel=True)
		derive('%s-l2-container-removed' % r, '%s-intact' % r, [lambda ds: remove_container(ds, A2)], subjects=('A2',), panel=True)
		derive('%s-l3-container-removed' % r, '%s-intact' % r, [lambda ds: remove_container(ds, R3)], subjects=('R3',), panel=True)
		clean('%s-topology-a' % r, root=root, frames=('A2', 'R3'), edges={'A2': (RZ, 'R3')}, lengths_built=NO_L3,
			lengths=NO_L3, subjects=('A2', 'R3'), panel=True)
		clean('%s-topology-b-clean' % r, root=root, frames=('A2', 'T'), edges={'T': (RX, 'P'), 'A2': (RZ, 'T')}, lengths_built=NO_L3,
			lengths=NO_L3, subjects=('A2', 'T'))
		derive('%s-topology-b' % r, '%s-topology-b-clean' % r, [lambda ds: set_matrix(ds, T, NON_RIGID, 'orthonormal rotation')],
			subjects=('A2', 'T'), panel=True)
		clean('%s-dangling-edge-control' % r, root=root, frames=('A2', 'T', 'Y'),
			edges={'T': (RX, 'P'), 'A2': (RZ, 'T'), 'Y': (SHIFT, 'T')}, lengths_built=NO_L3, lengths=NO_L3, subjects=('A2', 'T', 'Y'), panel=True)
		clean('%s-dangling-root-control' % r, root=root, frames=('A2', 'T', 'Y'), edges={'A2': (RZ, 'T'), 'Y': (SHIFT, 'T')},
			lengths_built=NO_L3, lengths=NO_L3, subjects=('A2', 'T', 'Y'), panel=True)
		derive('%s-dangling-parent' % r, '%s-dangling-root-control' % r, [lambda ds: remove_container(ds, T)],
			subjects=('A2', 'T', 'Y'), panel=True)
		derive('%s-container-unmarked' % r, '%s-intact' % r, [lambda ds: set_container_parent(ds, A2, S_FOR),
			lambda ds: strip_marker(ds, A2), lambda ds: add_declaration(ds, 'legacy', A2)], subjects=('A2',), panel=True)
		derive('%s-quarantined-edge' % r, '%s-intact' % r, [lambda ds: add_declaration(ds, 'legacy', A2),
			lambda ds: add_declaration(ds, 'unverified', A2)], subjects=('A2', 'SIB'), panel=True)
		saved('%s-quarantined-edge-save1' % r, '%s-quarantined-edge' % r, panel=True)
		saved('%s-quarantined-edge-save2' % r, '%s-quarantined-edge-save1' % r, panel=True)
		derive('%s-quarantined-root' % r, '%s-intact' % r, [lambda ds: add_declaration(ds, 'unverified', R3)],
			subjects=('R3', 'R3B'), panel=True)
		saved('%s-quarantined-root-save1' % r, '%s-quarantined-root' % r, panel=True)
		saved('%s-quarantined-root-save2' % r, '%s-quarantined-root-save1' % r, panel=True)
		clean('%s-intermediate-clean' % r, root=root, frames=('T', 'X', 'Y', 'SIB'),
			edges={'T': (RX, 'P'), 'X': (RZ, 'T'), 'Y': (SHIFT, 'T'), 'SIB': (RX, 'P')},
			lengths_built=('L1', 'LX', 'LY', 'L4', 'L5'), lengths=('L1', 'LX', 'LY', 'L4', 'L5'), subjects=('T', 'X', 'Y', 'SIB'), panel=True)
		derive('%s-intermediate-declared' % r, '%s-intermediate-clean' % r, [lambda ds: add_declaration(ds, 'unverified', T)], panel=True)
		saved('%s-intermediate-declared-save1' % r, '%s-intermediate-declared' % r, panel=True)
		derive('%s-intermediate-non-rigid' % r, '%s-intermediate-clean' % r, [lambda ds: set_matrix(ds, T, NON_RIGID, 'orthonormal rotation')],
			panel=True)
		saved('%s-intermediate-non-rigid-save1' % r, '%s-intermediate-non-rigid' % r, panel=True)
		clean('%s-foreign-clean' % r, root=root, frames=('A2',), edges={'A2': (RZ, 'P')}, lengths_built=('L1', 'L2', 'L5'),
			retarget={'L5': 'U5'}, lengths=('L1', 'L2', 'L5'), subjects=('A2',))
		derive('%s-foreign' % r, '%s-foreign-clean' % r, [lambda ds: unmark(ds, FOREIGN)], subjects=('A2',), panel=True)
		saved('%s-foreign-save1' % r, '%s-foreign' % r, panel=True)
		clean('%s-two-reference-legacy-clean' % r, root=root, frames=('A2',), edges={'A2': (RZ, 'P')}, lengths_built=NO_L3,
			lengths=NO_L3, subjects=('A2',))
		derive('%s-two-reference-legacy' % r, '%s-two-reference-legacy-clean' % r, [lambda ds: unmark(ds, PRE_TREE)],
			subjects=('A2',), panel=True)

	# Genuine legacy document: one referenced imaging frame P
	clean('legacy-clean', root='exchange', referenced=('P',), frames=('G6',), edges={'G6': (RZ, 'P')}, lengths_built=('L1', 'L6'),
		retarget={}, lengths=('L1', 'L6'), subjects=('G6',))
	derive('legacy', 'legacy-clean', [lambda ds: unmark(ds, PRE_TREE)], subjects=('P', 'G6'), panel=True)
	saved('legacy-save1', 'legacy', panel=True)
	saved('legacy-save2', 'legacy-save1')

	# Legacy provenance: equipment with and without a pre-profile method code (unmarked, one imaging frame)
	G6 = FRAMES['G6']
	for label, equipment in (('dev', ('Sonador-IO', 'dev')), ('missing', (None, None)), ('foreign', FOREIGN),
			('post-boundary', ('Sonador-IO', DCMSR_FRAMES_MIN_VERSION))):
		derive('legacy-equipment-%s' % label, 'legacy', [lambda ds, e=equipment: set_equipment(ds, *e)], subjects=('G6',))
		derive('legacy-equipment-%s-code' % label, 'legacy', [lambda ds, e=equipment: set_equipment(ds, *e),
			lambda ds: add_method_code(ds, G6)], subjects=('G6',))
	derive('legacy-non-rigid', 'legacy', [lambda ds: set_matrix(ds, G6, NON_RIGID, 'orthonormal rotation')], subjects=('G6',))

	# Saved legacy: declarations and primary
	derive('legacy-saved-no-primary', 'legacy-save1', [lambda ds: remove_declarations(ds, 'primary')], subjects=('G6',))
	saved('legacy-saved-no-primary-save1', 'legacy-saved-no-primary')
	derive('legacy-saved-two-primaries', 'legacy-save1', [lambda ds: add_declaration(ds, 'primary', P_FOR)], subjects=('G6',))
	derive('legacy-saved-non-rigid', 'legacy-save1', [lambda ds: set_matrix(ds, G6, NON_RIGID, 'orthonormal rotation')], subjects=('G6',))
	derive('legacy-saved-conflict', 'legacy-save1', [lambda ds: add_declaration(ds, 'unverified', G6)], subjects=('G6',))
	saved('legacy-saved-conflict-save1', 'legacy-saved-conflict')
	derive('legacy-saved-second-series', 'legacy-save1', [lambda ds: add_evidence_series(ds, 'S')], referenced=('P', 'S'),
		subjects=('P', 'S', 'G6'))
	derive('legacy-saved-caller-primary', 'legacy-saved-second-series', [lambda ds: {'op': 'read-with-primary', 'primary': S_FOR}],
		referenced=('P', 'S'), subjects=('P', 'S', 'G6'),
		caller_primary=S_FOR, note='read with primary_frame_uid = S; a save is refused (legacy-target-change)')

	def legacy_with_edge(scenario, done):
		source = done['legacy-saved-second-series']
		report = read_report(source.dataset, source.referenced, source.root)
		report.frames.add_edge(SHIFT, P_FOR, S_FOR)
		scenario.changes = [{'op': 'client-add-edge', 'source': P_FOR, 'target': S_FOR}]
		return write(report, scenario.name)
	add(Scenario('legacy-saved-p-to-q', legacy_with_edge, referenced=('P', 'S'), derived_from='legacy-saved-second-series',
		subjects=('P', 'S', 'G6'), lengths=('L1', 'L6'), panel=True))

	# Rows of the classification table not covered above
	derive('row2-conflicting-containers', 'exc-intact', [lambda ds: duplicate_container(ds, A2, S_FOR)], subjects=('A2',))
	derive('row3-container-without-unit', 'exc-intact', [lambda ds: remove_container_child(ds, A2, DCMSR_FRAME_UNIT)], subjects=('A2',))
	derive('row4-item-removed', 'exc-intact', [lambda ds: remove_item(ds, A2)], subjects=('A2',))
	derive('row5-orphan-legacy', 'exc-intact', [lambda ds: remove_container(ds, A2), lambda ds: remove_item(ds, A2),
		lambda ds: add_declaration(ds, 'legacy', A2)], subjects=('A2',))
	saved('row5-orphan-legacy-save1', 'row5-orphan-legacy')
	saved('row5-orphan-legacy-save2', 'row5-orphan-legacy-save1')
	derive('row5-orphan-unverified', 'exc-intact', [lambda ds: remove_container(ds, A2), lambda ds: remove_item(ds, A2),
		lambda ds: add_declaration(ds, 'unverified', A2)], subjects=('A2',))
	saved('row5-orphan-unverified-save1', 'row5-orphan-unverified')
	derive('row6-container-without-parent', 'exc-intact', [lambda ds: set_container_parent(ds, A2, None)], subjects=('A2',))
	clean('row10-cycle-clean', frames=('A2', 'X'), edges={'A2': (RZ, 'P'), 'X': (SHIFT, 'A2')}, lengths_built=('L1', 'L2', 'LX'),
		retarget={}, lengths=('L1', 'L2', 'LX'), subjects=('A2', 'X'))
	derive('row10-cycle', 'row10-cycle-clean', [lambda ds: set_container_parent(ds, A2, FRAMES['X'])], subjects=('A2', 'X'))
	derive('row13-all-containers-removed', 'exc-intact', [remove_all_containers], subjects=('A2', 'SIB'))
	derive('row16-marked-unmarked-item', 'exc-intact', [lambda ds: remove_container(ds, A2), lambda ds: strip_marker(ds, A2)],
		subjects=('A2',))
	derive('primary-invalid', 'exc-intact', [lambda ds: remove_declarations(ds, 'primary'), lambda ds: add_declaration(ds, 'primary', A2)],
		subjects=('P', 'A2'))
	derive('primary-duplicated', 'exc-intact', [lambda ds: add_declaration(ds, 'primary', S_FOR)], subjects=('P', 'A2'))

	for scenario in found.values():
		scenario.lengths_subject = tuple(a for a in scenario.lengths if LENGTHS[a][1] in scenario.subjects)
	return found


def build_all():
	'''	Build every scenario's dataset.

		@returns OrderedDict name -> Scenario (with `dataset` and `changes`)
	'''
	done = OrderedDict()
	for name, scenario in scenarios().items():
		scenario.dataset = scenario.build(scenario, done)
		done[name] = scenario
	return done


# ---- the case table ----


def viewer_state(graph, frame_uid, loaded):
	'''	Note state of a measurement in the Viewer, from the frame graph the client reads: `drawn` for a stored frame
		in the loaded set, else A (registered to a loaded series), C (relationship damaged on the frame or its path),
		B (known, disconnected), D (unknown), decided in that order.

		@input graph (FrameGraph)
		@input frame_uid (str)
		@input loaded (set of str): Frame of Reference UIDs of the loaded series
		@returns tuple (str state, list of str: the loaded frames a state-A measurement reaches)
	'''
	if frame_uid in loaded:
		return 'drawn', []
	if frame_uid not in graph:
		return 'D', []
	reached = [uid for uid in sorted(loaded) if graph.path(frame_uid, uid) is not None]
	if reached:
		return 'A', reached
	node, seen = frame_uid, set()
	while node and node not in seen:
		seen.add(node)
		frame = graph.frame(node)
		if frame is None or frame.quarantined:
			return 'C', []
		edge = graph.edges.get(node)
		if edge is not None and not edge.usable:
			return 'C', []
		if edge is None and not (frame.established or frame.imaging_backed):
			return 'C', []
		node = edge.target_uid if edge is not None else None
	return 'B', []


def _loaded_frames(label):
	return set(SERIES[a]['frame'] for a in LOADED_SETS[label])


def _loaded_series(label, frames):
	return [SERIES[a]['label'] for a in LOADED_SETS[label] if SERIES[a]['frame'] in frames]


def case_entry(scenario, done):
	'''	Case-table entry of one scenario: what the client reads for each subject frame and Length, and what its
		re-save writes.
	'''
	diagnostics = []
	report = read_report(scenario.dataset, scenario.referenced, scenario.root, primary_frame_uid=scenario.caller_primary,
		diagnostics=diagnostics)
	graph = report.frames
	records = graph.document
	try:
		declared = report.frame_write_plan()['declared']
		save = 'written'
	except Exception as err:
		declared = None
		save = 'refused: %s' % getattr(getattr(err, 'diagnostic', None), 'condition', type(err).__name__)

	def alias(uid):
		return ALIASES.get(uid, uid)

	frames = OrderedDict()
	for subject in scenario.subjects:
		uid = FRAMES[subject]
		frame = graph.frame(uid)
		found = records.classification.get(uid) if records is not None else None
		edge = graph.edges.get(uid)
		entry = OrderedDict()
		entry['uid'] = uid
		entry['known'] = frame is not None
		entry['evidence'] = sorted(frame.evidence) if frame is not None else []
		entry['established'] = bool(frame is not None and frame.established)
		entry['quarantined'] = bool(frame is not None and frame.quarantined)
		entry['imaging_backed'] = bool(frame is not None and frame.imaging_backed)
		entry['name'] = frame.name if frame is not None else None
		entry['name_source'] = frame.name_source if frame is not None else None
		entry['displayed_name'] = frame.name if (frame is not None and frame.name_source == 'container') else None
		entry['row'] = found.row if found is not None else None
		entry['trust'] = found.state if found is not None else ('root' if frame is not None else None)
		entry['reason'] = found.reason if found is not None else None
		entry['target'] = alias(found.target) if (found is not None and found.target) else None
		entry['edge_usable'] = bool(edge is not None and edge.usable and not graph.is_quarantined(uid))
		entry['bucket'] = _bucket(graph, uid)
		entry['reachable'] = [alias(u) for u in graph.reachable(uid)] if frame is not None else []
		entry['written_back'] = records.written_back(uid) if records is not None else None
		viewer = OrderedDict()
		for label in LOADED_SETS:
			state, reached = viewer_state(graph, uid, _loaded_frames(label))
			viewer[label] = state if state != 'A' else 'A (%s)' % ', '.join(_loaded_series(label, set(reached)))
		entry['viewer'] = viewer
		frames[subject] = entry

	measurements = OrderedDict()
	for alias_ in scenario.lengths:
		tracking_uid, frame_alias = LENGTHS[alias_]
		uid = FRAMES[frame_alias]
		viewer = OrderedDict()
		for label in LOADED_SETS:
			state, reached = viewer_state(graph, uid, _loaded_frames(label))
			viewer[label] = state if state != 'A' else 'A (%s)' % ', '.join(_loaded_series(label, set(reached)))
		measurements[alias_] = OrderedDict((('tracking_uid', tracking_uid), ('frame', frame_alias), ('frame_uid', uid),
			('bucket', _bucket(graph, uid)), ('viewer', viewer)))

	entry = OrderedDict()
	entry['fixture'] = fixture_name(scenario.name)
	entry['root'] = scenario.root
	entry['referenced'] = [SERIES[a]['label'] for a in scenario.referenced]
	entry['derived_from'] = scenario.derived_from
	entry['changes'] = scenario.changes
	if scenario.note:
		entry['note'] = scenario.note
	entry['caller_primary'] = alias(scenario.caller_primary) if scenario.caller_primary else None
	entry['marked'] = bool(records is not None and records.marked)
	entry['primary'] = alias(graph.primary_uid) if graph.primary_uid else None
	entry['persisted_primary'] = alias(graph.persisted_primary_uid) if graph.persisted_primary_uid else None
	entry['frames'] = frames
	entry['lengths'] = measurements
	entry['save'] = save
	entry['declarations_written'] = OrderedDict((k, [alias(u) for u in v]) for k, v in declared.items()) if declared is not None else None
	entry['diagnostics'] = sorted(set(('%s %s %s' % (d.severity, d.condition, alias(d.value) if isinstance(d.value, str) else '')).strip()
		for d in report.diagnostics if d.condition.startswith(('frame', 'legacy', 'unknown-frame'))))
	return entry


def _bucket(graph, uid):
	'''	Series a Length in the frame keys under without a Source-Series (R9): the series of an imaging frame one
		referenced series has, else the document's.
	'''
	series = graph.imaging_series.get(uid) or []
	frame = graph.frame(uid)
	if frame is not None and frame.imaging_backed and len(series) == 1:
		return next(SERIES[a]['label'] for a in SERIES if SERIES[a]['series_uid'] == series[0])
	return 'document'


def case_table(done):
	table = OrderedDict()
	table['format'] = 'sonador-io frame reader cases, version 1'
	table['specification'] = 'oak-tree/medical-imaging/sonador-client#45 revision 8 (sections 3.4, 3.5 items 14 and 15); ' \
		'oak-tree/medical-imaging/sonador-client#46; oak-tree/medical-imaging/ohif-viewers#174'
	table['generator'] = 'python -m sonador.ftests.test_sr.frames_fixtures'
	table['study'] = OrderedDict((('study_uid', STUDY_UID), ('series', OrderedDict((a, OrderedDict((
		('series_uid', s['series_uid']), ('sop_instance_uid', s['sop_uid']), ('frame_of_reference_uid', s['frame']),
		('series_number', s['number']), ('referenced', a != 'S2')))) for a, s in SERIES.items()))))
	table['frames'] = OrderedDict((a, OrderedDict((('uid', u), ('name', FRAME_NAMES.get(a))))) for a, u in FRAMES.items())
	table['lengths'] = OrderedDict((a, OrderedDict((('tracking_uid', t), ('frame', f)))) for a, (t, f) in LENGTHS.items())
	table['loaded_sets'] = OrderedDict((label, [SERIES[a]['label'] for a in aliases]) for label, aliases in LOADED_SETS.items())
	table['viewer_states'] = OrderedDict((('drawn', 'stored frame matches a loaded series'),
		('A', 'registered to a loaded series: a path of trusted or accepted legacy edges through no quarantined frame'),
		('C', 'relationship unknown or damaged on the frame or its path'), ('B', 'known, disconnected from every loaded series'),
		('D', 'unknown frame')))
	table['scenarios'] = OrderedDict((name, case_entry(scenario, done)) for name, scenario in done.items())
	return table


def write_fixtures(directory=FIXTURES_DIR):
	'''	Write every fixture and the case table.

		@returns OrderedDict name -> Scenario
	'''
	done = build_all()
	os.makedirs(directory, exist_ok=True)
	for name, scenario in done.items():
		with open(os.path.join(directory, fixture_name(name)), 'wb') as handle:
			handle.write(tobytes(scenario.dataset))
	with open(os.path.join(directory, CASES_FILE), 'w', newline='\n') as handle:
		json.dump(case_table(done), handle, indent=1)
		handle.write('\n')
	return done


if __name__ == '__main__':
	logging.getLogger('sonador').setLevel(logging.CRITICAL)
	write_fixtures(sys.argv[1] if len(sys.argv) > 1 else FIXTURES_DIR)
