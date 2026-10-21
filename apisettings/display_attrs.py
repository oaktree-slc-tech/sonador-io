'''	Display attributes: properties which control how a resource is rendered in the viewer.

	The shared element is the color-role contract (`Background`, `Value`, `Meaning`, each a
	hexadecimal sRGB color). Each consumer wraps it differently; a tag stores it in its `Meta`
	document (`DCMSR_TAG_META`) as `{ 'DisplayAttributes': { 'Color': {...} } }`.
'''
import re


DISPLAY_ATTRIBUTES = 'DisplayAttributes'
DISPLAY_ATTRIBUTES_COLOR = 'Color'
DISPLAY_COLOR_BACKGROUND = 'Background'
DISPLAY_COLOR_VALUE = 'Value'
DISPLAY_COLOR_MEANING = 'Meaning'
DISPLAY_COLOR_ROLES = (DISPLAY_COLOR_BACKGROUND, DISPLAY_COLOR_VALUE, DISPLAY_COLOR_MEANING)

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
