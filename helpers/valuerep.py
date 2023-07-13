import re

from nameparser import HumanName
from pydicom.valuerep import PersonName as DcmPersonName


def str2name(val) -> HumanName:
	'''	Parse the provided name to components
	'''
	_name = None

	# Check for DICOM encoded name string
	if isinstance(val, (DcmPersonName, str, bytes)):

		# Check to see if name is DICOM encoded
		if isinstance(val, (str, bytes)):
			_name = DcmPersonName(val)

			# Name only found a single component, re-parse with HumanName
			if _name.family_name == val:
				_name = None

		# Unpack to HumanName instance
		if _name:
			return HumanName(
				first=_name.given_name, last=_name.family_name, middle=_name.middle_name, title=_name.name_prefix, suffix=_name.name_suffix)

	# Name encoded as HumanName
	elif isinstance(val, HumanName):
		return val

	# Decode as HumanName
	return HumanName(str(val))
