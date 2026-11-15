'''	Generator of the landmark-set fixtures: synthetic DICOM-SR documents holding landmark sets in the 0.3 form and legacy
	`Sonador-SR.1002` point groups, for the Sonador IO tests (`sr_landmarks`) and the Viewer's landmark work
	(oak-tree/medical-imaging/ohif-viewers#175).

	Every document is built on the synthetic study of the other SR fixtures (series P and S of `frames_fixtures`), by
	the client's writers, with fixed UIDs, dates and times, so the output is reproducible byte for byte. `FIXTURES`
	names each fixture with its root title and root template identifier, recorded independently: the root title
	(Concept Name) tells an exchange document (DCM 126000, Imaging Measurement Report) from a Sonador application
	document (the Sonador-SR `Clinical-Report` title); the template identifier is written beside it. A legacy fixture
	also records the dataset change made after writing, if any.

	Regenerate (from the directory containing the `sonador` package):

		python -m sonador.ftests.test_sr.landmarks_fixtures [output directory]

	The default output directory is `ftests/test_sr/fixtures`.
'''
import copy, logging, os, sys
from collections import OrderedDict

import numpy as np
from pydicom.sr.codedict import codes

from highdicom.sr.templates import Code, CodedConcept

from ...apisettings.base import LandmarkSet, Landmark, ReferenceFrame, LengthMeasurement, ArrowAnnotation, Finding, Measurement, \
	ImageCoord, ImageCoordCollection, VolumePointCollectionGroup, ReportMetaGroup, ImageReference, MeasurementCollection, \
	DCMSR_LANDMARK_ANATOMIC, DCMSR_LANDMARK_REGISTRATION, DCMSR_UNITS_MM
from ...apisettings.display_attrs import DisplayAttributes
from ...apisettings.sr import DCMSR_SCOORD3D_ANATOMIC, DCMSR_SCOORD3D_LANDMARK
from ...imaging.helpers.sr import SonadorComprehensiveSR3D, SonadorImagingMeasurementReport
from . import frames_fixtures as F

FIXTURE_DATE = '2026-1114'
FIXTURES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fixtures')

P_FOR, S_FOR = F.P_FOR, F.S_FOR
APP_FRAME, ROOT_FRAME = '2.25.47110', '2.25.47120'
P_SOP, S_SOP = F.SERIES['P']['sop_uid'], F.SERIES['S']['sop_uid']
P_SERIES, S_SERIES = F.SERIES['P']['series_uid'], F.SERIES['S']['series_uid']

# Concepts of the fixtures: a consumer vocabulary in a `99` private scheme
ASIS = Code('ASIS-L', '99EXAMPLE', 'Left anterior superior iliac spine')
PUBIC = Code('PUB-SYM', '99EXAMPLE', 'Pubic symphysis')
PELVIS = Code('PELVIS-SET', '99EXAMPLE', 'Pelvic landmarks')
TEARDROP = 'Tear drop'

# Tracking UIDs of the sets
SET_UIDS = OrderedDict((('patient', '2.25.47201'), ('image', '2.25.47202'), ('image-second', '2.25.47203'), ('application', '2.25.47204'),
	('root', '2.25.47205'), ('legacy', '2.25.47206'), ('legacy-polyline', '2.25.47207'), ('legacy-mixed', '2.25.47208')))


def fixture_name(topic):
	return 'sonador-io.%s.%s.dcm' % (topic, FIXTURE_DATE)


def _fid(name):
	return F._uid('landmark ' + name)


def reference_set(tracking_uid=SET_UIDS['patient'], frame=P_FOR):
	'''	The landmark set of the patient and exchange fixtures: a concept, a label, a description and Line / Text colours,
		with landmarks covering every attribute state.
	'''
	landmarks = [
		Landmark((10.0, 20.0, 30.0), concept=ASIS, label='ASIS (left)', landmark_type=DCMSR_LANDMARK_ANATOMIC,
			description='Every attribute set', display={'Line': '#FF0000', 'Text': '#00FF00'}, fiducial_uid=_fid('all')),
		Landmark((11.0, 21.0, 31.0), label='Label only', fiducial_uid=_fid('label')),
		Landmark((12.0, 22.0, 32.0), concept=PUBIC, fiducial_uid=_fid('concept')),
		Landmark((13.0, 23.0, 33.0), concept=ASIS, label='Concept and label', fiducial_uid=_fid('both')),
		Landmark((14.0, 24.0, 34.0), fiducial_uid=_fid('none')),
		Landmark((15.0, 25.0, 35.0), label='Line override', display={'Line': '#FF8000'}, fiducial_uid=_fid('override')),
		Landmark((16.0, 26.0, 36.0), label='Épine iliaque antérieure', fiducial_uid=_fid('utf8')),
		Landmark((17.0, 27.0, 37.0), label=TEARDROP, fiducial_uid=_fid('same-1')),
		Landmark((18.0, 28.0, 38.0), label=TEARDROP, fiducial_uid=_fid('same-2')),
		Landmark((19.0, 29.0, 39.0), landmark_type=DCMSR_LANDMARK_REGISTRATION, fiducial_uid=_fid('registration')),
	]
	return LandmarkSet(landmarks, concept=PELVIS, label='Pelvis (planning)', description='Points picked on the planning model',
		display={'Line': '#00C0FF', 'Text': '#FFFFFF'}, tracking_uid=tracking_uid, frame_of_reference_uid=frame, series_uid=P_SERIES)


def _report(root, referenced=('P', 'S'), **kwargs):
	cls = SonadorImagingMeasurementReport if root == 'exchange' else SonadorComprehensiveSR3D
	return cls(report_procedure=codes.LN.CTUnspecifiedBodyRegion, dcm_evidence=[F.evidence_dataset(a) for a in referenced],
		person_observer=F.PERSON_OBSERVER, imaging_frames=F.references_map(referenced), primary_frame_uid=P_FOR, **kwargs)


def build_patient(root):
	report = _report(root)
	report.landmarks = [reference_set()]
	return report


def build_mixed():
	report = _report('exchange')
	reference = ImageReference(F.MR_IMAGE_STORAGE, P_SOP, None, P_SERIES)
	tag = Finding.series_tag(CodedConcept('img-qc.accept', 'Sonador-SEG', 'Image Accepted', scheme_version='0.2'), reference=reference,
		tracking_uid='2.25.47301')
	length = LengthMeasurement([(0.0, 0.0, 0.0), (10.0, 0.0, 0.0)], frame_of_reference_uid=P_FOR, tracking_uid='2.25.47302')
	arrow = ArrowAnnotation.from_points((1.0, 1.0, 1.0), (5.0, 1.0, 1.0), 'Arrow beside the set', frame_of_reference_uid=P_FOR,
		tracking_uid='2.25.47303')
	report.findings = [tag, arrow]
	report.measurements = [length]
	report.landmarks = [reference_set()]
	return report


def build_image():
	report = _report('exchange')
	first = LandmarkSet.from_points([(10.5, 20.5), (30.25, 40.0), (5.0, 6.0)], name='Image landmarks', names=[ASIS, None, 'Corner'],
		reference=ImageReference(F.MR_IMAGE_STORAGE, P_SOP, None, P_SERIES), fiducial_uids=[_fid('img-1'), _fid('img-2'), _fid('img-3')])
	first.tracking_uid = SET_UIDS['image']
	second = LandmarkSet.from_points([(1.0, 2.0)], name='Second image set', reference=ImageReference(F.MR_IMAGE_STORAGE, S_SOP, None,
		S_SERIES), fiducial_uids=[_fid('img-4')])
	second.tracking_uid = SET_UIDS['image-second']
	report.landmarks = [first, second]
	return report


def build_application_frame():
	report = _report('application')
	graph = report.frames
	graph.add_frame(ReferenceFrame(APP_FRAME, name='Anatomic'))
	graph.add_edge(F.RZ, APP_FRAME, P_FOR)
	graph.add_frame(ReferenceFrame(ROOT_FRAME, name='Implant'))
	trusted = LandmarkSet.from_points([(1.0, 2.0, 3.0), (4.0, 5.0, 6.0)], name='Anatomic frame set', frame_of_reference_uid=APP_FRAME,
		fiducial_uids=[_fid('app-1'), _fid('app-2')])
	trusted.tracking_uid = SET_UIDS['application']
	root = LandmarkSet.from_points([(0.0, 0.0, 0.0)], name='Implant frame set', frame_of_reference_uid=ROOT_FRAME, fiducial_uids=[_fid('root-1')])
	root.tracking_uid = SET_UIDS['root']
	report.landmarks = [trusted, root]
	return report


def build_legacy():
	'''	Two volume groups (one migratable: a POINT with a Fiducial UID and a MULTIPOINT in one frame; one with a POINT, a
		POLYLINE and a group measurement, which reads as a set and cannot be migrated) and a metadata group with the same
		template identifier.
	'''
	report = _report('application')
	migratable = VolumePointCollectionGroup(points=[
		ImageCoord(1.0, 2.0, 3.0, reference_frame=P_FOR, name=DCMSR_SCOORD3D_ANATOMIC, point_type='POINT'),
		ImageCoordCollection([ImageCoord(4.0, 5.0, 6.0), ImageCoord(7.0, 8.0, 9.0)], reference_frame=P_FOR, name=DCMSR_SCOORD3D_LANDMARK,
			point_type='MULTIPOINT')], tracking_id='Femoral head points', tracking_uid=SET_UIDS['legacy'])
	polyline = VolumePointCollectionGroup(points=[
		ImageCoord(5.0, 5.0, 5.0, reference_frame=P_FOR, point_type='POINT'),
		ImageCoordCollection([ImageCoord(0.0, 0.0, 0.0), ImageCoord(10.0, 0.0, 0.0)], reference_frame=P_FOR, point_type='POLYLINE')],
		measurements=MeasurementCollection([Measurement(10.0, unit=DCMSR_UNITS_MM, name=codes.SCT.Length)]), tracking_id='Axis line',
		tracking_uid=SET_UIDS['legacy-polyline'])
	meta = ReportMetaGroup(meta=[], tracking_id='Plan metadata', tracking_uid='2.25.47209')
	report.groups = [migratable, polyline, meta]
	return report


def build_legacy_mixed():
	report = _report('application')
	group = VolumePointCollectionGroup(points=[
		ImageCoord(1.0, 2.0, 3.0, reference_frame=P_FOR, point_type='POINT'),
		ImageCoord(4.0, 5.0, 6.0, reference_frame=S_FOR, point_type='POINT'),
		ImageCoord(7.0, 8.0, 9.0, reference_frame=P_FOR, point_type='POINT')], tracking_id='Mixed frame points',
		tracking_uid=SET_UIDS['legacy-mixed'])
	report.groups = [group]
	return report


def _legacy_fiducial(ds):
	'''	Give the first POINT item of the migratable legacy group a Fiducial UID (the legacy writer has no parameter for it).
	'''
	for container in ds.ContentSequence:
		for group in container.get('ContentSequence') or []:
			for item in group.get('ContentSequence') or []:
				if item.get('ValueType') == 'SCOORD3D' and item.get('GraphicType') == 'POINT':
					item.FiducialUID = _fid('legacy-point')
					return {'op': 'set-fiducial-uid', 'item': 'first POINT of the migratable group', 'uid': item.FiducialUID}
	raise AssertionError('no POINT item')


# name -> (topic, builder, root title, root template identifier, change after writing)
FIXTURES = OrderedDict((
	('patient', ('landmarks-patient', lambda: build_patient('application'), 'Clinical-Report (Sonador-SR)', 'Sonador-SR.1500', None)),
	('exchange', ('landmarks-exchange', lambda: build_patient('exchange'), '126000 (DCM, Imaging Measurement Report)', 'Sonador-SR.1500', None)),
	('mixed', ('landmarks-mixed', build_mixed, '126000 (DCM, Imaging Measurement Report)', 'Sonador-SR.1500', None)),
	('image', ('landmarks-image', build_image, '126000 (DCM, Imaging Measurement Report)', 'Sonador-SR.1500', None)),
	('application-frame', ('landmarks-application-frame', build_application_frame, 'Clinical-Report (Sonador-SR)', 'Sonador-SR.1500', None)),
	('legacy', ('point-collection-legacy', build_legacy, 'Clinical-Report (Sonador-SR)', 'Sonador-SR.1500', _legacy_fiducial)),
	('legacy-mixed', ('point-collection-legacy-mixed-frames', build_legacy_mixed, 'Clinical-Report (Sonador-SR)', 'Sonador-SR.1500', None)),
))

REFERENCED = {'patient': ('P', 'S'), 'exchange': ('P', 'S'), 'mixed': ('P', 'S'), 'image': ('P', 'S'), 'application-frame': ('P', 'S'),
	'legacy': ('P', 'S'), 'legacy-mixed': ('P', 'S')}
ROOTS = {'patient': 'application', 'exchange': 'exchange', 'mixed': 'exchange', 'image': 'exchange', 'application-frame': 'application',
	'legacy': 'application', 'legacy-mixed': 'application'}


def build_all():
	'''	@returns OrderedDict name -> pydicom.Dataset
	'''
	found = OrderedDict()
	for name, (topic, build, _, _, change) in FIXTURES.items():
		ds = F.write(build(), 'landmarks ' + name)
		if change is not None:
			change(ds)
		found[name] = ds
	return found


def write_fixtures(directory=FIXTURES_DIR):
	done = build_all()
	os.makedirs(directory, exist_ok=True)
	for name, ds in done.items():
		with open(os.path.join(directory, fixture_name(FIXTURES[name][0])), 'wb') as handle:
			handle.write(F.tobytes(ds))
	return done


if __name__ == '__main__':
	logging.getLogger('sonador').setLevel(logging.CRITICAL)
	write_fixtures(sys.argv[1] if len(sys.argv) > 1 else FIXTURES_DIR)
