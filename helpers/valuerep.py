import re, usaddress

from nameparser import HumanName
from pydicom.valuerep import PersonName as DcmPersonName

from ..apisettings.sr import points2array
from ..apisettings.fhir import SonadorFhirAddress


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
				first=_name.given_name, last=_name.family_name, middle=_name.middle_name, 
				title=_name.name_prefix, suffix=_name.name_suffix)

	# Name encoded as HumanName
	elif isinstance(val, HumanName):
		return val

	# Decode as HumanName
	return HumanName(str(val))


def str2address(val) -> SonadorFhirAddress:
	'''	Parse the provided string to FHIR address components
	'''
	if isinstance(val, SonadorFhirAddress):
		return val

	# Address from components
	elif isinstance(val, dict):
		return SonadorFhirAddress(**val)

	return SonadorFhirAddress(val)
