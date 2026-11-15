'''	Reference frames of a report model: the frame graph of `SonadorComprehensiveSR3D`.

	The graph (`sonador.apisettings.base.FrameGraph`) is built on first use from what the report already holds,
	without a request: the Frame of Reference UIDs of the series it references (from their cached records, as
	Source-Series resolution reads them, or from `imaging_frames`), the frame records of the parsed document
	(`sonador.apisettings.sr.frames`), the caller's legacy `reference_frames` matrices and the conversion
	transforms of measurements built with `transform=`. The model then answers representability against a target,
	converts geometry explicitly through trusted edges, applies a writer policy (`create_sr(require=...)`) and
	writes the frame records back: generated records in canonical order, imported ones verbatim in their slots,
	declarations regenerated from the read.

	Writer policy (`require`, against `target`, default the primary):

	| `require` | native | registered | unregistered | image domain |
	|---|---|---|---|---|
	| None (default) | written | written as given | written as given | written |
	| `'registered'` | written | written as given (still unconverted) | refused | ignored |
	| `'native'` | written | refused (`conversion required`) | refused (`no trusted path`) | ignored |

	A writer that wants everything in the target frame converts each object (`convert`) and writes with
	`require='native'`.
'''
import copy
from collections import OrderedDict

from pydicom import Dataset as DcmDataset

from ....apisettings.base import LandmarkSet, Measurement, ArrowAnnotation, ImageCoord, ImageCoordCollection, ImageTransformMatrix, \
	ReferenceFrame, VolumePointCollectionGroup, FrameConversionError, frame_reference_uid, drawing_plane_vectors, \
	DCMSR_FRAME_NATIVE, DCMSR_FRAME_REGISTERED, DCMSR_FRAME_UNREGISTERED, DCMSR_FRAME_NOT_APPLICABLE
from ....apisettings.sr import sr_diagnostic, SRDiagnostic, SRDiagnosticError, srcontent_extra_items, \
	SR_DIAGNOSTIC_INFO, SR_DIAGNOSTIC_WARNING, SR_DIAGNOSTIC_ERROR, SR_CONDITION_UNKNOWN_FRAME, SR_CONDITION_FRAME_POLICY_REFUSED, \
	SR_CONDITION_FRAME_EDGE_INVALID
from ....apisettings.sr.frames import resolve_frame_graph, frame_write_plan
from .parse import SRPointCollection


# Writer policies
DCMSR_REQUIRE_REGISTERED = 'registered'
DCMSR_REQUIRE_NATIVE = 'native'
DCMSR_REQUIRE_POLICIES = (None, DCMSR_REQUIRE_REGISTERED, DCMSR_REQUIRE_NATIVE)


class SRFrameMixin:
	'''	Frame graph of a report model.

		@property frames (FrameGraph): the graph, built on first access
		@property point_collections (list of SRPointCollection and ImageCoordCollection): point sets of the report:
			those read from the document, then those of its point collection groups
	'''

	def _init_frames(self, primary_frame_uid=None, imaging_frames=None, study_frames=None, frames_strict=False, **kwargs):
		'''	@input primary_frame_uid (str, default=None): the caller's primary imaging frame: a referenced
				imaging frame; it replaces a persisted primary for this session (`frame-primary-changed`)
			@input imaging_frames (mapping or iterable, default=None): Frame of Reference UIDs of the referenced
				imaging series, when the report's cached records do not carry them: `{series_uid: frame_uid}`, or
				frame UIDs
			@input study_frames (mapping, default=None): `{series_uid: frame_uid}` of the cached series of the
				document's study; only the series the document references are used
		'''
		self._primary_frame_uid = primary_frame_uid
		self._imaging_frames_arg = imaging_frames
		self._study_frames = study_frames
		self._frames_strict = frames_strict
		self._frame_graph = None
		self._integrated = set()
		self._declared_matrices = OrderedDict()
		self._pending_plan = None

	# ---- graph ----

	def _referenced_series_uids(self):
		'''	Series the report references: its reference series, then the series of the parsed document's evidence.

			@returns list of str
		'''
		found = []
		for sx in (self.ref_series_list or []):
			if sx.series_uid and sx.series_uid not in found:
				found.append(sx.series_uid)
		if self.parsed is not None:
			for reference in (self.parsed.evidence or {}).values():
				if reference.series_uid and reference.series_uid not in found:
					found.append(reference.series_uid)
		return found

	def _imaging_frame_map(self):
		'''	Imaging-backed frames of the report, from what is cached: no request.

			@returns OrderedDict: Frame of Reference UID -> list of Series Instance UID
		'''
		from ...orthanc.sr import dcmsr_series_frame_uid
		frames = OrderedDict()

		def put(frame_uid, series_uid=None):
			if not frame_uid:
				return
			series = frames.setdefault(str(frame_uid), [])
			if series_uid and series_uid not in series:
				series.append(series_uid)

		explicit = self._imaging_frames_arg
		if isinstance(explicit, dict):
			for series_uid, frame_uid in explicit.items():
				put(frame_uid, series_uid)
		elif explicit:
			for frame_uid in explicit:
				put(frame_uid)
		for sx in (self.ref_series_list or []):
			put(dcmsr_series_frame_uid(sx), sx.series_uid)
		referenced = self._referenced_series_uids()
		known = self._known_series() if referenced else {}
		for series_uid in referenced:
			if series_uid in known:
				put(dcmsr_series_frame_uid(known[series_uid]), series_uid)
		for series_uid, frame_uid in (self._study_frames or {}).items():
			if series_uid in referenced:
				put(frame_uid, series_uid)
		return frames

	@property
	def frames(self):
		'''	Frame graph of the report, built on first access (see the module notes).

			@returns FrameGraph
		'''
		if self._frame_graph is None:
			records = getattr(self.parsed, 'frame_records', None) if self.parsed is not None else None
			self._frame_graph = resolve_frame_graph(records, self._imaging_frame_map(), primary_frame_uid=self._primary_frame_uid,
				diagnostics=self.diagnostics, strict=self._frames_strict)
			if self.parsed is not None:
				for obj, frame_uid, _, _ in self._patient_items():
					if not isinstance(obj, SRPointCollection):
						obj._parsed_frame_uid = frame_uid
				self._report_unknown_frames()
		self._integrate_pending(raise_errors=False)
		return self._frame_graph

	@property
	def has_frame_knowledge(self):
		'''	Whether the report can tell known frames from unknown ones: it resolves at least one imaging frame, or
			its document carries frame records.
		'''
		graph = self.frames
		return bool(graph.imaging_frames) or bool(graph.document is not None and graph.document.has_records)

	def _report_unknown_frames(self):
		'''	`unknown-frame` once per parsed geometry whose frame has no evidence at all.
		'''
		graph = self._frame_graph
		if not (graph.imaging_frames or (graph.document is not None and graph.document.has_records)):
			return
		for obj, frame_uid, path, _ in self._patient_items():
			if frame_uid and frame_uid not in graph:
				sr_diagnostic(self.diagnostics, SR_DIAGNOSTIC_WARNING, SR_CONDITION_UNKNOWN_FRAME, 'the frame of this geometry is '
					'known to no reference, container, item, declaration or mention; it is held where it is', path=path, value=frame_uid,
					strict=self._frames_strict)

	def _integrate_pending(self, raise_errors=True):
		'''	Add to the graph the frames and edges the caller declared outside it: the `reference_frames` matrices
			(a frame named by the matrix's comment or source label, with an edge to its `target_frame` or the
			primary) and the transforms of measurements built with `transform=` (an edge from the source frame to
			the target the caller gave, when that target is an established frame).

			@input raise_errors (bool): raise for a matrix the graph refuses; otherwise leave it pending
		'''
		graph = self._frame_graph

		# `reference_frames` is reconciled with the frames it declared: a matrix removed from the collection, or
		# replaced or changed under its UID, removes the frame it declared; the current matrices are then declared.
		# Frames read from the document or declared on the graph directly are never touched.
		current = OrderedDict()
		for matrix in (self.reference_frames or []):
			if matrix.uid is None:
				matrix._uid = ReferenceFrame().uid
			current[matrix.uid] = matrix
		for uid, key in list(self._declared_matrices.items()):
			if uid not in current or _matrix_key(current[uid]) != key:
				graph.remove_frame(uid)
				del self._declared_matrices[uid]
		for uid, matrix in current.items():
			if uid in self._declared_matrices or uid in graph:
				continue
			target = matrix.target_frame or graph.primary_uid
			try:
				if not target:
					raise SRDiagnosticError(SRDiagnostic(SR_DIAGNOSTIC_ERROR, SR_CONDITION_FRAME_EDGE_INVALID, None, uid,
						'Reference frame %s names no target_frame and the report has no primary imaging frame' % uid))
				frame = ReferenceFrame(uid, name=matrix.comment or matrix.source_frame or uid, unit=matrix.source_unit)
				graph.add_frame(frame)
				try:
					graph.add_edge(matrix, frame, target)
				except Exception:
					graph.remove_frame(frame)
					raise
			except SRDiagnosticError:
				if raise_errors:
					raise
				continue
			self._declared_matrices[uid] = _matrix_key(matrix)

		for obj in self._transform_items():
			if id(obj) in self._integrated:
				continue
			transform = obj.source_transform
			target = transform.target_frame or obj.frame_of_reference_uid
			target_frame = graph.frame(target) if target else None
			self._integrated.add(id(obj))
			if target_frame is None or not target_frame.established or target_frame.quarantined:
				continue
			source_uid = transform.uid or getattr(obj, '_source_frame_uid', None) or ReferenceFrame().uid
			obj._source_frame_uid = source_uid
			if source_uid in graph:
				continue
			frame = ReferenceFrame(source_uid, name=transform.source_frame or transform.comment or source_uid, unit=transform.source_unit)
			graph.add_frame(frame)
			graph.add_edge(ImageTransformMatrix(transform.matrix, uid=source_uid, comment=transform.comment, codes=transform.codes,
				fiducials=transform.fiducials, ref_images=transform.ref_images), frame, target)

	def _transform_items(self):
		found = list(self.measurements or [])
		for group in (self.groups or []):
			found.extend(getattr(group, 'measurements', None) or [])
		found.extend(self.arrow_findings)
		found.extend(getattr(self, 'landmarks', None) or [])
		return [m for m in found if getattr(m, 'source_transform', None) is not None]

	# ---- geometry ----

	@property
	def point_collections(self):
		'''	Point collections of the report: the SCOORD3D items of the document's legacy point groups (a group migrated by
			an edit of its legacy landmark set is no longer listed), then those of the report's point collection groups.

			@returns list of SRPointCollection and ImageCoordCollection
		'''
		replaced = getattr(self, '_legacy_groups_replaced', None)
		migrating = set(lset.tracking_uid for lset, _, _ in (replaced(strict=False) if replaced is not None else []))
		found = [p for p in (list(getattr(self.parsed, 'point_collections', None) or ()) if self.parsed is not None else [])
			if not (p.tracking_uid and p.tracking_uid in migrating)]
		for group in (self.groups or []):
			if isinstance(group, VolumePointCollectionGroup):
				for points in (group.points or []):
					found.append(points)
		return found

	def _patient_items(self):
		'''	Patient-domain geometry the report writes: measurements, arrows and point collections.

			@returns list of (object, frame UID or None, diagnostic path, new) where `new` is True for geometry created
				or edited in this session
		'''
		found = []
		measurements = list(self.measurements or [])
		for group in (self.groups or []):
			measurements.extend(getattr(group, 'measurements', None) or [])
		for item in measurements + list(self.arrow_findings):
			geometry = item.geometry
			if geometry is None or not geometry.is_patient:
				continue
			# The frame an item was read with travels with the item (and with a converted copy, as its new frame), so a
			# frame-only change is an edit in whichever report the item is written
			read_frame = getattr(item, '_parsed_frame_uid', item.frame_of_reference_uid)
			new = getattr(item, '_parsed_points', None) is None or bool(getattr(item, 'points_changed', True)) \
				or read_frame != item.frame_of_reference_uid
			found.append((item, item.frame_of_reference_uid, 'group %s' % item.tracking_uid, new))
		for lset in (getattr(self, '_landmark_sets', None) or []):
			if lset.is_patient and (not lset.legacy or lset.edited):
				# New geometry is a set created here, or one whose points, membership, order or frame changed; a migrated
				# legacy group is written in a new form and checked once. An edit of names or descriptions is not
				new = lset.geometry_edited or lset.legacy
				found.append((lset, lset.frame_of_reference_uid, 'group %s' % lset.tracking_uid, new))
		for points in self.point_collections:
			if isinstance(points, SRPointCollection):
				found.append((points, points.frame_uid, 'group %s' % points.tracking_uid, False))
			else:
				found.append((points, points.frame_uid, 'points', True))
		return found

	@staticmethod
	def _frame_of(obj):
		'''	Frame of an object, or None for image-domain geometry.

			@returns tuple (str or None, bool patient)
		'''
		if isinstance(obj, str):
			return obj, True
		if isinstance(obj, ReferenceFrame):
			return obj.uid, True
		if isinstance(obj, SRPointCollection):
			return obj.frame_uid, True
		if isinstance(obj, (ImageCoordCollection, ImageCoord)):
			return obj.frame_uid, True
		geometry = getattr(obj, 'geometry', None)
		if geometry is None or not geometry.is_patient:
			return None, False
		return obj.frame_of_reference_uid, True

	def representability(self, obj, target=None):
		'''	State of an object's geometry against a target frame.

			@input obj: a measurement, an arrow, a point collection, a ReferenceFrame or a frame UID
			@input target (str or ReferenceFrame, default=the primary)
			@returns str: `native`, `registered`, `unregistered`, or `not-applicable` for image-domain geometry
			@raises FrameConversionError (`no target`) without a target or a primary
		'''
		frame_uid, patient = self._frame_of(obj)
		if not patient:
			return DCMSR_FRAME_NOT_APPLICABLE
		return self.frames.representability(frame_uid, target)

	def convert(self, obj, to_frame):
		'''	A new object with the geometry of `obj` expressed in `to_frame`, through trusted and accepted legacy
			edges and no quarantined frame. The original is unchanged. A drawing plane rotates with it (rotation
			only); the tracking UID, stored value and provenance carry over.

			@input obj: a measurement, an arrow, a point collection (`SRPointCollection`, `ImageCoordCollection`) or a
				single point (`ImageCoord`)
			@input to_frame (str or ReferenceFrame)
			@returns an object of the same kind (an `ImageCoordCollection` for a point collection read from a document, an
				`ImageCoord` for a single point), naming `to_frame`
			@raises FrameConversionError for image-domain geometry, an unknown frame, or no trusted path (with the reason)
		'''
		to_uid = getattr(to_frame, 'uid', to_frame)
		frame_uid, patient = self._frame_of(obj)
		if not patient:
			raise FrameConversionError('Image-domain geometry has no frame to convert from', reason=DCMSR_FRAME_NOT_APPLICABLE)
		if isinstance(obj, LandmarkSet):
			return _converted_set(obj, frame_uid, to_uid, self.frames)
		transform = self.frames.transform(frame_uid, to_uid)
		if isinstance(obj, ImageCoord):
			converted = transform.convert([tuple(obj)])[0]
			return ImageCoord(*converted, reference_frame=to_uid, name=obj.name, point_type=obj.type)
		if isinstance(obj, (SRPointCollection, ImageCoordCollection)):
			points = list(obj.points) if isinstance(obj, SRPointCollection) else [tuple(p) for p in obj]
			return ImageCoordCollection([ImageCoord(*p) for p in transform.convert(points)], reference_frame=to_uid,
				point_type=(obj.graphic_type if isinstance(obj, SRPointCollection) else obj.type), name=getattr(obj, 'name', None))
		return _converted_annotation(obj, transform, to_uid)

	# ---- write ----

	def _apply_write_policy(self, require=None, target=None):
		'''	Refuse a write that does not meet the writer policy, or that carries new or edited geometry in a frame the
			graph does not know (when the report resolves its references).

			@raises SRDiagnosticError `frame-policy-refused`, naming the object (tracking UID), its frame, the target and
				the reason
			@raises FrameConversionError (`no target`) for a policy without a target on a report with no primary
			@raises ValueError for an unknown policy
		'''
		if require not in DCMSR_REQUIRE_POLICIES:
			raise ValueError('Unknown writer policy require=%r (supported: None, %r, %r)' % (require, DCMSR_REQUIRE_REGISTERED, DCMSR_REQUIRE_NATIVE))
		graph = self.frames
		target_uid = graph.resolve_target(target) if require is not None else None
		knowledge = self.has_frame_knowledge

		def refuse(path, frame_uid, reason):
			raise SRDiagnosticError(SRDiagnostic(SR_DIAGNOSTIC_ERROR, SR_CONDITION_FRAME_POLICY_REFUSED, path, frame_uid,
				'Write refused for %s: frame %s, target %s: %s' % (path, frame_uid, target_uid, reason)))

		for obj, frame_uid, path, new in self._patient_items():
			frame_uid = frame_uid or graph.primary_uid
			if new and knowledge and frame_uid and frame_uid not in graph:
				refuse(path, frame_uid, 'new or edited geometry in a frame unknown to the graph')
			if require is None or not frame_uid:
				continue
			state = graph.representability(frame_uid, target_uid)
			if state == DCMSR_FRAME_NATIVE:
				continue
			if require == DCMSR_REQUIRE_NATIVE:
				refuse(path, frame_uid, 'conversion required' if state == DCMSR_FRAME_REGISTERED else 'no trusted path')
			if state == DCMSR_FRAME_UNREGISTERED:
				refuse(path, frame_uid, 'no trusted path')

	def frame_write_plan(self):
		'''	Frame records the next write emits (`sonador.apisettings.sr.frames.frame_write_plan`), after the pending
			frames and edges are added to the graph.

			@returns dict (`declarations`, `containers`, `items`, `declared`)
			@raises SRDiagnosticError for a refused frame or edge, or `legacy-target-change`
		'''
		graph = self.frames
		self._integrate_pending(raise_errors=True)
		return frame_write_plan(graph, diagnostics=self.diagnostics)

	def _root_tail(self, plan):
		'''	Root items written after the report template: the declarations, then the preserved root items with the
			frame containers in their slots (imported containers where they were read, generated ones after the last
			imported one).

			@returns list of highdicom content items
		'''
		extras = srcontent_extra_items(self._extra_items_for_write() if hasattr(self, '_extra_items_for_write') else self.extra_items)
		containers = sorted(enumerate(plan['containers']), key=lambda entry: (entry[1][0], entry[0]))
		tail = list(plan['declarations'])
		position = 0
		for index, extra in enumerate(extras):
			while position < len(containers) and containers[position][1][0] <= index:
				tail.append(_content_item(containers[position][1][1]))
				position += 1
			tail.append(extra)
		while position < len(containers):
			tail.append(_content_item(containers[position][1][1]))
			position += 1
		return tail

	def _unframed_target(self):
		return self.frames.primary_uid


def _matrix_key(matrix):
	'''	Everything a `reference_frames` matrix declares, so that a replacement changing any of it is written: the
		coefficients, comment, source label, target, unit, method codes (value, scheme, version and meaning), Used
		Fiducials and image references.

		@returns tuple
	'''
	from ....apisettings.base import matrix_4x4
	from ....apisettings.sr import srcode2dataset
	codes = tuple(_encoded(srcode2dataset(c) if not isinstance(c, DcmDataset) else c) for c in (matrix.codes or []))
	return (matrix_4x4(matrix.matrix).tobytes(), matrix.comment, matrix.source_frame, matrix.target_frame, matrix.source_unit, codes,
		tuple(_encoded(d) for d in (matrix.fiducials or [])), tuple(_encoded(d) for d in (matrix.ref_images or [])))


def _encoded(ds):
	'''	Explicit VR little endian bytes of a dataset, for comparing declared content.
	'''
	from pydicom.filewriter import write_dataset
	from pydicom.filebase import DicomBytesIO
	out = DicomBytesIO()
	out.is_little_endian, out.is_implicit_VR = True, False
	write_dataset(out, ds)
	return out.getvalue()


def _content_item(item):
	'''	A content item for the root sequence: an imported container (a pydicom Dataset) through the preserved-item
		conversion, a generated one as it is.
	'''
	from pydicom import Dataset
	if isinstance(item, Dataset) and not hasattr(item, 'relationship_type'):
		return srcontent_extra_items([item])[0]
	return item


def _converted_set(lset, frame_uid, to_uid, graph):
	'''	Copy of a landmark set in another frame: every point converted, with order, identities, types, names,
		descriptions, display attributes, tracking UID and opaque items kept. The source set is unchanged.

		@raises FrameConversionError (`landmark-conversion-opaque-geometry`) for a set holding an opaque slot with a
			coordinate, which could not be converted; and as `FrameGraph.transform` across frames
	'''
	from ....apisettings.sr import SR_CONDITION_LANDMARK_CONVERSION_OPAQUE_GEOMETRY, SR_CONDITION_LANDMARK_LEGACY_NOT_MIGRATABLE
	if lset._frozen is not None:
		raise FrameConversionError('Landmark set %s is a legacy point group that cannot be migrated (%s); it cannot be converted'
			% (lset.tracking_uid, lset._frozen), reason=SR_CONDITION_LANDMARK_LEGACY_NOT_MIGRATABLE, source_uid=frame_uid, target_uid=to_uid)
	if frame_uid == to_uid:
		# The identity path: a copy that keeps what the set was read as, so an unchanged set stays unchanged
		return copy.deepcopy(lset)
	if lset.has_opaque_geometry:
		raise FrameConversionError('Landmark set %s holds geometry the client could not read; it cannot be converted, since that '
			'geometry would stay in the old frame' % lset.tracking_uid, reason=SR_CONDITION_LANDMARK_CONVERSION_OPAQUE_GEOMETRY,
			source_uid=frame_uid, target_uid=to_uid)
	transform = graph.transform(frame_uid, to_uid)
	converted = copy.deepcopy(lset)
	for landmark in converted.landmarks:
		landmark.point = transform.convert([tuple(landmark.point)])[0]
	converted.frame_of_reference_uid = to_uid
	converted.source_transform = None
	return converted


def _converted_annotation(obj, transform, to_uid):
	'''	Copy of a measurement or arrow with its points converted (see `SRFrameMixin.convert`).
	'''
	links = OrderedDict((attr, obj.__dict__.get(attr)) for attr in ('series', 'instance', 'sr_instance', 'collection') if attr in obj.__dict__)
	for attr in links:
		obj.__dict__[attr] = None
	try:
		converted = copy.deepcopy(obj)
	finally:
		obj.__dict__.update(links)
	converted.__dict__.update(links)
	keeps = getattr(obj, 'keeps_stored_value', False)
	geometry = obj.geometry
	converted._geometry = geometry._replace(points=transform.convert(geometry.points), frame_uid=to_uid)
	converted.frame_of_reference_uid = to_uid
	if getattr(obj, '_drawing_plane', None) is not None:
		converted._drawing_plane = tuple(transform.rotate_directions(drawing_plane_vectors(obj._drawing_plane)))
		converted._drawing_plane_text = None
	for attr in ('_normal',):
		if getattr(obj, attr, None) is not None:
			setattr(converted, attr, transform.rotate_directions([getattr(obj, attr)])[0])
	if keeps:
		converted._parsed_points = converted._geometry.points
		if hasattr(converted, '_parsed_frame_of_reference_uid'):
			converted._parsed_frame_of_reference_uid = to_uid
	converted._parsed_frame_uid = to_uid
	converted.source_transform = None
	return converted
