'''	Compatibility helpers for the DICOM stack (pydicom 3 / highdicom) used by the Sonador client.

	highdicom: the client requires the Oak-Tree fork of highdicom
	(https://code.oak-tree.tech/oak-tree/medical-imaging/highdicom). The fork tracks upstream
	releases and adds the `record_instances` option to the SR document constructors and to
	`highdicom.sr.utils.collect_evidence`, which the Sonador SR models rely on. It is published
	to the Oak-Tree package registry as `highdicom==<upstream version>.postN`; `highdicom.__version__`
	reports the upstream base version, so the distribution version (importlib.metadata) and the
	`record_instances` signature are used to tell the fork apart from a stock install.

	Transfer syntax: pydicom 3 deprecates the `is_little_endian` / `is_implicit_VR` dataset flags
	(removal is scheduled for pydicom 4) and derives the encoding used by `save_as` from the Transfer
	Syntax UID in the file meta. `save_as` refuses to convert between byte orders, so the dataset's
	`original_encoding` must agree with the transfer syntax. The helpers below map the encoding
	options previously expressed with the flags onto a Transfer Syntax UID and back.
'''
import inspect
from importlib import metadata

from pydicom.uid import UID, ImplicitVRLittleEndian, ExplicitVRLittleEndian, ExplicitVRBigEndian


HIGHDICOM_DISTRIBUTION = 'highdicom'
HIGHDICOM_FORK_URL = 'https://code.oak-tree.tech/oak-tree/medical-imaging/highdicom'
HIGHDICOM_FORK_INDEX_URL = 'https://code.oak-tree.tech/api/v4/projects/335/packages/pypi/simple'

# Exact pin of the published fork artifact. A compatible-release specifier (`~=`) ignores the
# post-release segment when deriving its prefix and therefore also admits stock 0.28.x releases
# (0.28.3, ...), which install fine but lack `record_instances`. Bump the version here, in
# requirements.txt and in packages/sonador together when a new fork build is published.
HIGHDICOM_FORK_VERSION = '0.28.2.post1'
HIGHDICOM_FORK_REQUIREMENT = '%s==%s' % (HIGHDICOM_DISTRIBUTION, HIGHDICOM_FORK_VERSION)


def highdicom_dist_version():
	'''	Installed highdicom distribution version (e.g. "0.28.2.post1"), or None if the
		package metadata is not available.
	'''
	try:
		return metadata.version(HIGHDICOM_DISTRIBUTION)
	except metadata.PackageNotFoundError:
		return None


def highdicom_dist_is_fork_release():
	'''	Check whether the installed highdicom distribution is the pinned fork release
		(`HIGHDICOM_FORK_VERSION`). This checks provenance via the package metadata only; use
		`highdicom_is_oaktree_fork` to check the imported module for the fork extension.

		@returns bool
	'''
	return highdicom_dist_version() == HIGHDICOM_FORK_VERSION


def highdicom_is_oaktree_fork():
	'''	Check whether the imported highdicom provides the Oak-Tree `record_instances` extension.

		@returns bool
	'''
	try:
		from highdicom.sr.utils import collect_evidence
		from highdicom.sr.sop import _SR
	except ImportError:
		return False

	return 'record_instances' in inspect.signature(collect_evidence).parameters \
		and 'record_instances' in inspect.signature(_SR.__init__).parameters


def require_oaktree_highdicom():
	'''	Raise ImportError with an actionable message if the imported highdicom is not the Oak-Tree fork.
	'''
	if highdicom_is_oaktree_fork():
		return

	import highdicom
	raise ImportError(
		('The Sonador client requires the Oak-Tree highdicom fork (%s), which adds the `record_instances` '
			+ 'option to the DICOM-SR document constructors. The imported highdicom (%s, distribution version %s, '
			+ 'module version %s) does not provide it. Install the fork with: '
			+ 'pip install --index-url %s "%s"')
		% (HIGHDICOM_FORK_URL, getattr(highdicom, '__file__', '?'), highdicom_dist_version(),
			getattr(highdicom, '__version__', '?'), HIGHDICOM_FORK_INDEX_URL, HIGHDICOM_FORK_REQUIREMENT))


def dcm_transfer_syntax(little_endian=True, implicit_vr=True):
	'''	Transfer Syntax UID for an uncompressed (native) encoding.

		@input little_endian (bool, default=True): byte order of the encoded dataset
		@input implicit_vr (bool, default=True): whether value representations are implicit
		@returns pydicom.uid.UID
	'''
	if little_endian and implicit_vr:
		return ImplicitVRLittleEndian
	if little_endian:
		return ExplicitVRLittleEndian
	if not implicit_vr:
		return ExplicitVRBigEndian

	raise ValueError('There is no Implicit VR Big Endian transfer syntax')


def dcm_transfer_syntax_from_dataset(dcm, default=ExplicitVRLittleEndian):
	'''	Infer the native Transfer Syntax UID from the encoding a dataset was read with.

		pydicom 3 exposes the encoding of datasets read from a file as `original_encoding`
		(implicit VR flag, little endian flag). Datasets created in memory have no original
		encoding and resolve to `default`.

		@input dcm (pydicom.Dataset): dataset to inspect
		@input default (pydicom.uid.UID, default=ExplicitVRLittleEndian): transfer syntax to use
			when the encoding is unknown
		@returns pydicom.uid.UID
	'''
	implicit_vr, little_endian = (None, None)

	encoding = getattr(dcm, 'original_encoding', None)
	if encoding:
		implicit_vr, little_endian = encoding

	if implicit_vr is None or little_endian is None:
		return default

	return dcm_transfer_syntax(little_endian=little_endian, implicit_vr=implicit_vr)


def dcm_encoding_from_transfer_syntax(transfer_syntax, default=(False, True)):
	'''	Dataset encoding implied by a Transfer Syntax UID.

		@input transfer_syntax (str or pydicom.uid.UID): transfer syntax UID
		@input default (tuple, default=(False, True)): encoding to report for transfer syntaxes
			pydicom does not know (private transfer syntaxes), as (implicit VR, little endian).
			Explicit VR Little Endian is the DICOM default for such cases.
		@returns tuple (implicit_vr: bool, little_endian: bool)
	'''
	transfer_syntax = UID(transfer_syntax)
	try:
		return (transfer_syntax.is_implicit_VR, transfer_syntax.is_little_endian)
	except ValueError:
		return default


def dcm_set_original_encoding(dcm, transfer_syntax=None):
	'''	Align a dataset's `original_encoding` with its Transfer Syntax UID so that `save_as` writes
		it in that encoding instead of raising a byte order conversion error.

		@input dcm (pydicom.Dataset): dataset to update
		@input transfer_syntax (UID, default=None): transfer syntax to apply; when None, the Transfer
			Syntax UID of the dataset's file meta is used
		@returns tuple (implicit_vr, little_endian) applied to the dataset
	'''
	if transfer_syntax is None:
		transfer_syntax = getattr(getattr(dcm, 'file_meta', None), 'TransferSyntaxUID', None)
	if not transfer_syntax:
		raise ValueError('Dataset has no Transfer Syntax UID; unable to determine its encoding')

	implicit_vr, little_endian = dcm_encoding_from_transfer_syntax(transfer_syntax)
	dcm.set_original_encoding(implicit_vr, little_endian)
	return (implicit_vr, little_endian)
