'''	Display attributes: properties which control how a resource is rendered in the viewer.

	The shared element is the color-role contract: six roles, each a hexadecimal sRGB color. The
	Series Tag roles are `Background`, `Border`, `Value` and `Meaning`; a measurement's own roles are
	`Line` (the drawn geometry) and `Text` (its label). Any role may occur on any item: a role read is
	a role written back, and no role set is enforced per tool. Each consumer wraps it differently; a tag stores it in its `Meta`
	document (`DCMSR_TAG_META`) as `{ 'DisplayAttributes': { 'Color': {...} } }`, a Series Tag finding
	carries them as `DisplayAttributes` (RGB tuples in the model, CIELab text only in the DICOM-SR codec,
	see `apisettings.sr.seriestag`).
'''
import re
from collections import OrderedDict

from client.utils.colors import RGB, hex2rgb, rgb2hex


DISPLAY_ATTRIBUTES = 'DisplayAttributes'
DISPLAY_ATTRIBUTES_COLOR = 'Color'
DISPLAY_COLOR_BACKGROUND = 'Background'
DISPLAY_COLOR_BORDER = 'Border'
DISPLAY_COLOR_VALUE = 'Value'
DISPLAY_COLOR_MEANING = 'Meaning'
DISPLAY_COLOR_LINE = 'Line'
DISPLAY_COLOR_TEXT = 'Text'
DISPLAY_COLOR_ROLES = (DISPLAY_COLOR_BACKGROUND, DISPLAY_COLOR_BORDER, DISPLAY_COLOR_VALUE, DISPLAY_COLOR_MEANING,
	DISPLAY_COLOR_LINE, DISPLAY_COLOR_TEXT)

HEX_COLOR_RE = re.compile(r'^#(?:[0-9A-Fa-f]{3}|[0-9A-Fa-f]{6})$')


def normalize_hex_color(value):
	'''	Canonical upper-case `#RRGGBB` form of a hexadecimal sRGB color. Accepts `#RGB` and
		`#RRGGBB` in any case, with surrounding whitespace. `None` is returned unchanged; how a
		blank value is treated is left to the caller.

		@raises ValueError when the value is not a hexadecimal color
	'''
	if value is None:
		return None
	if not isinstance(value, str) or not HEX_COLOR_RE.match(value.strip()):
		raise ValueError('Invalid color %r, colors must be hexadecimal RGB (#RRGGBB)' % (value,))

	hexvalue = value.strip()[1:]
	if len(hexvalue) == 3:
		hexvalue = ''.join(c*2 for c in hexvalue)
	return '#' + hexvalue.upper()


def tag_display_color(meta):
	'''	Color roles of a tag `Meta` document (empty when none are stored)
	'''
	display_attributes = (meta or {}).get(DISPLAY_ATTRIBUTES) or {}
	return display_attributes.get(DISPLAY_ATTRIBUTES_COLOR) or {}


class DisplayAttributes:
	'''	Display attributes of a finding or measurement: the colour roles as RGB tuples, plus any
		unmodelled content items read from the two display containers of a document, each re-emitted
		under the container it was read from on write. A role that is absent is not written.

		@property color (OrderedDict): role -> `client.utils.colors.RGB`, roles in `DISPLAY_COLOR_ROLES`
			order, only the roles that carry a colour
		@property extra_items (tuple of pydicom.Dataset): preserved unmodelled children of the
			`Sonador-DisplayAttributes` container (siblings of the colour container)
		@property color_extra_items (tuple of pydicom.Dataset): preserved unmodelled children of the
			`Sonador-DisplayAttributes.Color` container (siblings of the role items)
	'''
	def __init__(self, color=None, extra_items=None, color_extra_items=None):
		self.color = OrderedDict()
		for role in DISPLAY_COLOR_ROLES:
			value = (color or {}).get(role)
			if value is None:
				continue
			self.color[role] = self._rgb(value)
		self.extra_items = tuple(extra_items or ())
		self.color_extra_items = tuple(color_extra_items or ())

	@staticmethod
	def _rgb(value):
		'''	RGB tuple of a colour given as `#RRGGBB`, an RGB triplet or an `RGB` instance
		'''
		if isinstance(value, str):
			value = hex2rgb(normalize_hex_color(value))
		if not isinstance(value, RGB):
			value = RGB(*tuple(int(round(v)) for v in value))
		return value

	@classmethod
	def from_color(cls, value):
		'''	Display attributes from a colour-role dictionary, a tag `Meta` document, a `DisplayAttributes`
			payload or an existing instance.
		'''
		if isinstance(value, cls):
			return value
		if not isinstance(value, dict):
			raise TypeError('Display attributes must be a dictionary of colour roles, got %r' % type(value))

		color = value
		if DISPLAY_ATTRIBUTES in value:
			color = tag_display_color(value)
		elif DISPLAY_ATTRIBUTES_COLOR in value and not any(role in value for role in DISPLAY_COLOR_ROLES):
			color = value.get(DISPLAY_ATTRIBUTES_COLOR) or {}
		return cls(color=color)

	@property
	def hex(self):
		'''	Colour roles as canonical `#RRGGBB` strings (OrderedDict)
		'''
		return OrderedDict((role, rgb2hex(tuple(rgb)).upper()) for role, rgb in self.color.items())

	def to_meta(self):
		'''	Tag `Meta` document form: `{ 'DisplayAttributes': { 'Color': {...} } }`
		'''
		return { DISPLAY_ATTRIBUTES: { DISPLAY_ATTRIBUTES_COLOR: dict(self.hex) } }

	def __bool__(self):
		return bool(self.color) or bool(self.extra_items) or bool(self.color_extra_items)

	def __eq__(self, other):
		if not isinstance(other, DisplayAttributes):
			return NotImplemented
		return dict(self.color) == dict(other.color)

	def __ne__(self, other):
		return not self == other

	def __repr__(self):
		return 'DisplayAttributes(%s)' % ', '.join('%s=%s' % (role, hexval) for role, hexval in self.hex.items())
