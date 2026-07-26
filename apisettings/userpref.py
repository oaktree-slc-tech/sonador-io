'''	Sonador User Preferences API. Constants and enumerations used by the client methods
	which persist viewer and study-list display settings to Sonador (sonador#42).

	Preferences are stored on a single per-user record (`UserPref`) which carries two
	versioned JSON documents: `viewer` (the four preference categories exposed by the
	Sonador Viewer) and `studylist` (the display configuration of the study-list
	interfaces). Both documents are keyed by "<major>.<minor>" release so that older and
	newer viewer releases can store their keys without interfering with one another.
'''
from collections import OrderedDict


# Root endpoint of the user-preference API. The whole-document endpoint is served from the
# root; each preference section is registered as a child route.
SONADOR_USERPREF_ENDPOINT = '/visionaire/api/user-preferences'

# Release keys for the versioned preference documents. Sonador 0.4 is the current release;
# 0.3 is the key under which flat (pre-versioning) documents were nested by the data
# migration which introduced the versioned structure.
SONADOR_USERPREF_VERSION = '0.4'
SONADOR_USERPREF_LEGACY_VERSION = '0.3'

# JSON fields of the UserPref record which hold the versioned documents.
SONADOR_USERPREF_FIELD_VIEWER = 'viewer'
SONADOR_USERPREF_FIELD_STUDYLIST = 'studylist'
SONADOR_USERPREF_FIELDS = (SONADOR_USERPREF_FIELD_VIEWER, SONADOR_USERPREF_FIELD_STUDYLIST)

# Request/response keys used by the section endpoints. A section GET returns
# `{ version, values }`; a section POST accepts the same structure.
SONADOR_USERPREF_VERSION_KEY = 'version'
SONADOR_USERPREF_VALUES_KEY = 'values'


class _UserPrefUnset(object):
	'''	Sentinel distinguishing "not provided" from an explicit None in a whole-document
		write. A document left out of the request keeps its stored value; a document sent as
		None is cleared, so the two cannot share a default.
	'''
	def __repr__(self):
		return 'USERPREF_UNSET'

	def __bool__(self):
		return False


USERPREF_UNSET = _UserPrefUnset()


# Preference Sections

# Section keys as stored inside a version document. Note that the stored key and the URL
# slug of the corresponding endpoint are not always the same (see the path mapping below).
SONADOR_USERPREF_GENERAL = 'general'
SONADOR_USERPREF_HOTKEYS = 'hotkeys'
SONADOR_USERPREF_WINDOW_LEVEL = 'windowLevel'
SONADOR_USERPREF_VIEWER_META = 'viewerMetadata'
SONADOR_USERPREF_STUDYLIST = 'studylist'

# The four sections stored within the `viewer` document.
SONADOR_USERPREF_VIEWER_SECTIONS = (
	SONADOR_USERPREF_GENERAL,
	SONADOR_USERPREF_HOTKEYS,
	SONADOR_USERPREF_WINDOW_LEVEL,
	SONADOR_USERPREF_VIEWER_META,
)

# Every section endpoint, including the study-list section (which writes to its own field).
SONADOR_USERPREF_SECTIONS = SONADOR_USERPREF_VIEWER_SECTIONS + (SONADOR_USERPREF_STUDYLIST,)

# URL slug for each section. The slugs are hyphenated where the stored section keys are
# camel-cased (`windowLevel` -> `window-level`, `viewerMetadata` -> `viewer-meta`).
SONADOR_USERPREF_SECTION_PATHS = OrderedDict((
	(SONADOR_USERPREF_GENERAL, 'general'),
	(SONADOR_USERPREF_HOTKEYS, 'hotkeys'),
	(SONADOR_USERPREF_WINDOW_LEVEL, 'window-level'),
	(SONADOR_USERPREF_VIEWER_META, 'viewer-meta'),
	(SONADOR_USERPREF_STUDYLIST, 'studylist'),
))

# UserPref field written by each section.
SONADOR_USERPREF_SECTION_FIELDS = OrderedDict(
	[(section, SONADOR_USERPREF_FIELD_VIEWER) for section in SONADOR_USERPREF_VIEWER_SECTIONS]
	+ [(SONADOR_USERPREF_STUDYLIST, SONADOR_USERPREF_FIELD_STUDYLIST)])


# Study List Interfaces

# Interface keys stored within a version of the `studylist` document. Each interface is an
# independent slice: a write which carries one interface leaves the others untouched.
SONADOR_USERPREF_INTERFACE_WORKLIST = 'worklist'
SONADOR_USERPREF_INTERFACE_ALLSTUDIES = 'allStudies'
SONADOR_USERPREF_INTERFACE_SHARED = 'shared'
SONADOR_USERPREF_INTERFACE_UPLOAD = 'upload'

SONADOR_USERPREF_INTERFACES = (
	SONADOR_USERPREF_INTERFACE_WORKLIST,
	SONADOR_USERPREF_INTERFACE_ALLSTUDIES,
	SONADOR_USERPREF_INTERFACE_SHARED,
	SONADOR_USERPREF_INTERFACE_UPLOAD,
)

# Fields carried by a study-list interface slice. Each is an array of tag/column ID strings.
# `selectedFilters` describes which filter controls are visible and is not valid for the
# upload interface, which has no filter row.
SONADOR_USERPREF_SLICE_FILTERS = 'selectedFilters'
SONADOR_USERPREF_SLICE_COLUMNS = 'selectedColumns'
SONADOR_USERPREF_SLICE_COLUMN_ORDER = 'columnOrder'

SONADOR_USERPREF_SLICE_FIELDS = (
	SONADOR_USERPREF_SLICE_FILTERS,
	SONADOR_USERPREF_SLICE_COLUMNS,
	SONADOR_USERPREF_SLICE_COLUMN_ORDER,
)

# Fields valid for the upload interface.
SONADOR_USERPREF_UPLOAD_SLICE_FIELDS = (
	SONADOR_USERPREF_SLICE_COLUMNS,
	SONADOR_USERPREF_SLICE_COLUMN_ORDER,
)


def userpref_section_endpoint(section=None):
	'''	Resource endpoint for a user-preference section, or for the whole-document endpoint
		when no section is provided.

		@input section (str, default=None): section key (one of SONADOR_USERPREF_SECTIONS)

		@returns str: resource endpoint, with the trailing slash expected by the API routes
	'''
	if section is None:
		return '%s/' % SONADOR_USERPREF_ENDPOINT

	if section not in SONADOR_USERPREF_SECTION_PATHS:
		raise ValueError('Unsupported user-preference section: "%s". Supported sections: %s.'
			% (section, ', '.join(SONADOR_USERPREF_SECTIONS)))

	return '%s/%s/' % (SONADOR_USERPREF_ENDPOINT, SONADOR_USERPREF_SECTION_PATHS[section])
