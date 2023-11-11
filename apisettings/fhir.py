'''	FHIR constants, data structures, and encoding primitives for Sonador.
'''

import functools, usaddress
from collections import OrderedDict
from client.utils.object import pick, omit

from .base import DCMHEADER_PATIENT_SEX, \
	DCM_PATIENT_SEX_MALE, DCM_PATIENT_SEX_FEMALE, DCM_PATIENT_SEX_OTHER, DCM_PATIENT_SEX_UNKNOWN


FHIR_UNKNOWN = 'unknown'

# FHIR Locality Codes
LOCALITY_US = 'US'

# FHIR Use Codes
FHIR_USE_USUAL = 'usual'
FHIR_USE_OFFICIAL = 'official'
FHIR_USE_TEMP = 'temp'
FHIR_USE_SECONDARY = 'secondary'
FHIR_USE_BILLING = 'billing'

# Address Use Codes
FHIR_ADDR_USE_HOME = 'home'
FHIR_ADDR_USE_WORK = 'work'

# Address Type Codes
FHIR_ADDR_TYPE_POSTAL = 'postal'
FHIR_ADDR_TYPE_PHYSICAL = 'physical'
FHIR_ADDR_TYPE_BOTH = 'both'

# Support Codes
FHIR_ID_USE_CODES = (
	FHIR_USE_USUAL, FHIR_USE_OFFICIAL, FHIR_USE_TEMP, FHIR_USE_SECONDARY)
FHIR_NAME_USE_CODES = (
	FHIR_USE_USUAL, FHIR_USE_OFFICIAL, FHIR_USE_TEMP, 'nickname', 'anonymous', 'old', 'maiden')
FHIR_ADDR_USE_CODES = (FHIR_ADDR_USE_HOME, FHIR_ADDR_USE_WORK, FHIR_USE_TEMP, 'old', FHIR_USE_BILLING)
FHIR_ADDR_TYPE_CODES = (FHIR_ADDR_TYPE_POSTAL, FHIR_ADDR_TYPE_PHYSICAL, FHIR_ADDR_TYPE_BOTH, FHIR_UNKNOWN)

# Status Codes
FHIR_STATUS_DRAFT = 'draft'
FHIR_STATUS_ACTIVE = 'active'
FHIR_STATUS_REVOKED = 'revoked'
FHIR_STATUS_COMPLETE = 'complete'
FHIR_STATUS_ENTERED_IN_ERROR = 'entered-in-error'

FHIR_REQUEST_STATUS = (
	FHIR_STATUS_DRAFT, FHIR_STATUS_ACTIVE, FHIR_STATUS_REVOKED, FHIR_STATUS_COMPLETE, 
	FHIR_STATUS_ENTERED_IN_ERROR, FHIR_UNKNOWN)


# Supported sex/gender codes: DICOM <-> FHIR
FHIR_GENDER_DCM_MAP = {
	DCM_PATIENT_SEX_MALE: 'male',
	DCM_PATIENT_SEX_FEMALE: 'female',
	DCM_PATIENT_SEX_UNKNOWN: FHIR_UNKNOWN,
	DCM_PATIENT_SEX_OTHER: 'other',
}
DCM_GENDER_FHIR_MAP = dict((v,k) for k,v in FHIR_GENDER_DCM_MAP.items())



# Sonador / FHIR Address Representation

ADDR_PARSER_NUMBER = 'AddressNumber'
ADDR_PARSER_STREET_NAME_PRE = 'StreetNamePreDirectional'
ADDR_PARSER_STREET_NAME = 'StreetName'
ADDR_PARSER_STREET_POST_TYPE = 'StreetNamePostType'
ADDR_PARSER_STREET_POST_DIRECTIONAL = 'StreetNamePostDirectional'
ADDR_PARSER_OCCUPANCY_IDENTIFIER = 'OccupancyIdentifier'
ADDR_PARSER_OCCUPANCY_TYPE = 'OccupancyType'
ADDR_PARSER_RECIPIENT = 'Recipient'
ADDR_PARSER_PLACE_NAME = 'PlaceName'
ADDR_PARSER_STATE_NAME = 'StateName'
ADDR_PARSER_ZIPCODE = 'ZipCode'
ADDR_PARSER_COUNTRYNAME = 'CountryName'

ADDR_ARG_ADDRID = 'address_identifier'
ADDR_ARG_STREET_PRE_DIRECTIONAL = 'street_pre_directional'
ADDR_ARG_STREET_NAME = 'street_name'
ADDR_ARG_STREET_POST_TYPE = 'street_type'
ADDR_ARG_STREET_POST_DIRECTIONAL = 'street_post_directional'
ADDR_ARG_OCCUPANCY_IDENTIFIER = 'occupancy_identifier'
ADDR_ARG_OCCUPANCY_TYPE = 'occupancy_type'
ADDR_ARG_RECIPIENT = 'recipient'

ADDR_ARG_CITY = 'city'
ADDR_ARG_STATE = 'state'
ADDR_ARG_POSTALCODE = 'postalCode'
ADDR_ARG_COUNTRY = 'country'

ADDR_PARSER_ARGS = {
	ADDR_PARSER_NUMBER: ADDR_ARG_ADDRID,
	ADDR_PARSER_STREET_NAME_PRE: ADDR_ARG_STREET_PRE_DIRECTIONAL,
	ADDR_PARSER_STREET_NAME: ADDR_ARG_STREET_NAME,
	ADDR_PARSER_STREET_POST_TYPE: ADDR_ARG_STREET_POST_TYPE,
	ADDR_PARSER_STREET_POST_DIRECTIONAL: ADDR_ARG_STREET_POST_DIRECTIONAL,
	ADDR_PARSER_OCCUPANCY_IDENTIFIER: ADDR_ARG_OCCUPANCY_IDENTIFIER,
	ADDR_PARSER_OCCUPANCY_TYPE: ADDR_ARG_OCCUPANCY_TYPE,
	ADDR_PARSER_RECIPIENT: ADDR_ARG_RECIPIENT,
	ADDR_PARSER_PLACE_NAME: ADDR_ARG_CITY,
	ADDR_PARSER_STATE_NAME: ADDR_ARG_STATE,
	ADDR_PARSER_ZIPCODE: ADDR_ARG_POSTALCODE,
	ADDR_PARSER_COUNTRYNAME: ADDR_ARG_COUNTRY,
}
ADDR_PARSER_LINE = set((
	ADDR_ARG_ADDRID, ADDR_ARG_STREET_PRE_DIRECTIONAL, ADDR_ARG_STREET_NAME, 
		ADDR_ARG_STREET_POST_TYPE, ADDR_ARG_STREET_POST_DIRECTIONAL, 
	ADDR_ARG_OCCUPANCY_IDENTIFIER, ADDR_ARG_OCCUPANCY_TYPE, ADDR_ARG_RECIPIENT
))
ADDR_PARSER_GEOGRAPHY = set((
	ADDR_ARG_CITY, ADDR_ARG_STATE, ADDR_ARG_COUNTRY,
))
ADDR_PARSER_FHIR_TYPE = {
	'Street Address': FHIR_ADDR_TYPE_PHYSICAL,
	'Ambiguous': FHIR_UNKNOWN,
}


class SonadorFhirAddress:
	'''	Helper data class for working with address objects

		@property text (str): text representation of the address. If no value was provided
			during init, it will be composed from the address components.
	'''
	line_sep = ', '
	text_sep = ' '
	region_sep = ' '
	locality = LOCALITY_US

	def __init__(self, text:str=None, line=None, use:str=None, address_type:str=None, period=None,
			parse_components=True, **kwargs):
		''' Initialize FHIR address representation
			
			# FHIR Address Components
			@input text (str, default=None): text representation of the address.
				If not provided, it will be composed from the components.
			@input use (str, supported_values=('home', 'work', 'temp', 'old', 'billing'), default=None):
				purpose of the address
			@input type (str, supported_values=('postal', 'physical', 'both'), default=None): type of address
			@input line (iterator of strings, default=None): Street name, number, direction and PO Box.
			@input city (str, default=None): name of city, town, or region
			@input district (str, default=None): district name (eg, county)
			@input state (str, default=None): sub-unit of country
			@input postalCode (str, default=None): postal code for area
			@input country (str, default=None): ISO3166 2 or 3 letter country code

			# Address Sub-components
			@input address_identifier (str, default=None): address number (or similar identifier), example: "100".
				Sub component of `line`.
			@input street_name (str, default=None): street name, example: "Main". Sub component of line.
			@input street_type (str, default=None): type of street, examples: "Street", "Avenue", "Boulevard".
				Sub component of `line`.
			@input occupancy_identifier (str, default=None): identifier for the occupancy
			@input occupancy_type (str, default=None): type of occupancy (common component of business addresses),
				example: "100". Sub component of `line`.
				of business addresses), example: "Suite". Sub component of `line`.
		'''
		self.line_sep = kwargs.get('line_sep', self.line_sep)
		self.text_sep = kwargs.get('text_sep', self.text_sep)
		self.region_sep = kwargs.get('region_sep', self.region_sep)
		self.locality = kwargs.get('locality', self.locality)

		# Text and line components
		self._text = text
		self._line = line if isinstance(line, (tuple, list)) \
			else [line] if isinstance(line, str) \
			else line

		# Address type
		self._use = use
		self._type = address_type

		# Time-period in which the address was in-use
		self.period = period

		# Initialize sub-components from arguments
		if kwargs:
			self._init_line_components(**kwargs)
			self._init_region_components(**kwargs)

		# Parse line to sub-components
		elif line and parse_components == True and not pick(kwargs, ADDR_PARSER_LINE):
			components, atype = self.parse(_line)
			self._init_line_components(**components)

			# If address type not defined, use parser detected type
			if not self._type:
				self._type = atype

		# Parse full address to components
		elif text and isinstance(text, str) and parse_components == True and not pick(kwargs, ADDR_PARSER_ARGS.keys()):
			components, atype = self.parse(text)
			self._init_line_components(**components)
			self._init_region_components(**components)
			
			# If addres type not defined, use parser detected type
			if not self._type:
				self._type = atype

		# Verify components of address
		if self._use and not self._use in FHIR_ADDR_USE_CODES:
			raise ValueError('Invalid `use` value for address "%s". Supported: %s'
				% (self._use, ', '.join(FHIR_ADDR_USE_CODES)))
		if self._type:
			assert self.type in FHIR_ADDR_TYPE_CODES

	def _clean(self, val):
		'''	Clean the provided value
		'''
		if isinstance(val, str):
			return val.strip()

		return val

	def _init_line_components(self, address_identifier:str=None, street_pre_directional:str=None, street_name:str=None, 
			street_type:str=None, street_post_directional:str=None,
			occupancy_identifier=None, occupancy_type=None, recipient=None, **kwargs):
		'''	Initialize internal properties which correspond to the `line` components of the address.
		'''
		# Line sub-components
		self._addr_id = self._clean(address_identifier)
		self._street_pre_directional = self._clean(street_pre_directional)
		self._street = self._clean(street_name)
		self._street_type = self._clean(street_type)
		self._street_post_directional = self._clean(street_post_directional)
		self._occupancy_id = self._clean(occupancy_identifier)
		self._occupancy_type = self._clean(occupancy_type)
		self._recipient = self._clean(recipient)

	def _init_region_components(self, city:str=None, district:str=None, state:str=None, 
			postalCode:str=None, period:str=None, country:str=None, **kwargs):
		''' Initialize internal `regional` properties of the address
		'''
		# Regional components
		self._city = self._clean(city)
		self._district = self._clean(district)
		self._state = self._clean(state)
		self._postalCode = self._clean(postalCode)
		self._country = self._clean(country)

	def parse(self, text:str=None, components:OrderedDict=None):
		'''	Parse the provided text string to components

			@input text (str, default=instance text property)
			@input components (dict, default=New OrderedDict): ordered dictionary to which 
				the address components will be copied.

			@returns OrderedDict
		'''		
		if not text:
			raise ValueError('Invalid address text: "%s"' % text)

		# Parse to address components		
		components = components or OrderedDict()
		addr_tags, addr_type = usaddress.tag(text)
		
		for t,v in addr_tags.items():
			if ADDR_PARSER_ARGS.get(t): components[ADDR_PARSER_ARGS[t]] = v
			else: raise ValueError('Unknown address tag "%s": value="%s"' % (t,v))

		return components, addr_type

	@property
	def use(self):
		return self._use

	@property
	def type(self):
		'''	Address type: postal, physical, both
		'''
		if not self._type:
			return self._type
		elif self._type in FHIR_ADDR_TYPE_CODES:
			return self._type
		elif self._type in ADDR_PARSER_FHIR_TYPE:
			return ADDR_PARSER_FHIR_TYPE[self._type]

		raise ValueError('Invalid `type` code for address: "%s". Supported: %s'
			% (self._type, ', '.join(FHIR_ADDR_TYPE_CODES)))

	@property
	def text(self):
		if self._text:
			return self._text

		return self.text_sep.join(str(s) for s in (
			self.line_sep.join(self.line) if self.line else None, self.region, self.country,
		) if s)

	@property
	@functools.lru_cache()
	def street(self):
		'''	Return street components. If street components were not parsed
			as part of the initialization of the class they will be parsed from the line values.
		'''
		if self._street is None:
			line = self._line

			# Convert line to string before parsing to components
			if not line: _line = ''
			elif isinstance(line, str): _line = line
			elif isinstance(line, (tuple, list)): _line = self.line_sep.join(line)
			else:
				raise ValueError('Invalid `line` value: %s (type=%s)' % (line, type(line).__name__))

			# Parse line to sub components and initialize address properties
			if _line:
				components, atype = self.parse(_line)
				self._init_line_components(**components)
			
		return ' '.join(str(s) for s in (
			self._street_pre_directional, self._street, self._street_type, self._street_post_directional, 
		) if s)

	@property
	@functools.lru_cache()
	def line(self):
		if self._line:
			return self._line

		# Create line from sub-components		

		# Line 1: address ID, street, and street type/label
		self._line = [' '.join(str(self._clean(s)) for s in (self._addr_id, self.street) if s)]

		# Line 2: Occupancy ID and type/label
		if self._occupancy_id or self._occupancy_type:
			self._line.append(' '.join(str(self._clean(s)) for s in (self._occupancy_type, self._occupancy_id, self.recipient) if s))
		
		return self._line

	@property
	def city(self):
		return self._city

	@property
	def district(self):
		return self._district

	@property
	def state(self):
		return self._state

	@property
	def postalCode(self):
		return self._postalCode

	@property
	def country(self):
		return self._country

	@property
	def region(self):
		'''	Formatted representation of region: city, district, state, postalCode
		'''
		if self.locality == LOCALITY_US:
			citystate = ', '.join(str(s) for s in (self.city, self.state) if s)
			return self.region_sep.join(str(s) for s in (citystate, self.district, self.postalCode) if s)

		raise NotImplementedError('Unable to create region string. Unsupported locality: %s' % self.locality)

	@property
	def recipient(self):
		'''	Formatted representation of recipeint (ATTN)
		'''
		# Check for line breaks in recipient line, if present split. The parser sometimes
		# mistakenly places region and country components under the recipient line.
		if self._recipient and '\n' in self._recipient:

			# Take first component of recipient and re-parse remainder
			_rc = self._recipient.split('\n')
			self._recipient = _rc[0]

		return self._recipient

	@property
	def region_international(self):
		'''	Formatted representation of the region including the country
		'''
		if self.locality == LOCALITY_US:
			return self.region_sep.join(str(s) for s in (self.region, self.country) if s)

		raise NotImplementedError('Unable to create international region tring. Unsupported locality: %s' % self.locality)
	
	def json(self, *args, **kwargs):
		return pick(self, (
			'use', 'type', 'text', 'line', 'city', 'district', 'state', 'postalCode', 'country', 'period'))