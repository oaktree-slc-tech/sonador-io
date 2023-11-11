import logging

from ..test import SonadorBaseTestCase
from ..apisettings.fhir import SonadorFhirAddress, FHIR_ADDR_TYPE_PHYSICAL

logger = logging.getLogger(__name__)


class SonadorFhirAddressTests(SonadorBaseTestCase):
	'''	Ensure that the SonadorFhirAddress is able to accurately parse addresses from 
		text strings and create string representations of addresses in a reliable manner.
	'''
	# Test Address 0
	addr0_str = '123 Main Street, Suite 100 Chicago, IL 60007 USA'
	addr0_line1 = '123 Main Street'
	addr0_line2 = 'Suite 100'
	addr0_city = 'Chicago'
	addr0_state = 'IL'
	addr0_postalCode = '60007'
	addr0_country = 'USA'
	addr0_region = 'Chicago, IL 60007 USA'

	def test_fhir_addr0(self, *args, **kwargs):
		'''	usaddress control case: "123 Main Street, Suite 100 Chicago, IL 60007 USA"
		'''
		# Parse address from address string
		addr0p = SonadorFhirAddress(self.addr0_str)

		# Create address instance from components
		addr0c = SonadorFhirAddress(line=[self.addr0_line1, self.addr0_line2], 
			city=self.addr0_city, state=self.addr0_state, postalCode=self.addr0_postalCode, country=self.addr0_country, 
			address_type=FHIR_ADDR_TYPE_PHYSICAL)

		# Ensure that address components match expected values
		self.assertEqual(self.addr0_str, addr0p.text, 
			msg='SonadorFhirAddress: modified text representation of address. Expected: "%s". Test: "%s"' 
				% (self.addr0_str, addr0p.text))
		self.assertEqual(addr0p.line[0], self.addr0_line1,
			msg='SonadorFhirAddress: incorrectly parsed line 1 of address. Expected: "%s". Test: "%s"'
				% (addr0p.line[0], self.addr0_line1))
		self.assertEqual(addr0p.line[1], self.addr0_line2,
			msg='SonadorFhirAddress: incorrectly parsed line 2 of address. Expected: "%s". Test: "%s"'
				% (self.addr0_line2, addr0p.line[1]))
		self.assertEqual(addr0p.region_international, self.addr0_region,
			msg='SonadorFhirAddress: incorrect region. Expected: "%s". Test: "%s"' % (self.addr0_region, addr0p.region_international))
		self.assertEqual(addr0p.city, 'Chicago', 
			msg='SonadorFhirAddress: incorrectly parsed city. Expected: "%s". Test: "%s' % (self.addr0_city, addr0p.city))
		self.assertEqual(addr0p.state, self.addr0_state,
			msg='SonadorFhiAddress: incorrectly parsed state. Expected: "%s". Test: "%s"' % (self.addr0_postalCode, addr0p.state))
		self.assertEqual(addr0p.postalCode, self.addr0_postalCode, 
			msg='SonadorFhirAddress: incorrectly parsed postal code. Expected: "%s". Test: "%s"' 
				% (self.addr0_postalCode, self.addr0_postalCode))
		self.assertEqual(addr0p.country, self.addr0_country,
			msg='SonadorFhirAddress: incorrectly parsed country. Expected: "%s". Test: "%s"' 
				% (addr0p.country, self.addr0_country))

		# Ensure that parsed address matches address created from components
		self.assertEqual(addr0p.text, addr0c.text,
			msg='SonadorFhirAddress: text string created from components does not match expected output. Expected: "%s". Test: "%s"'
				% (addr0p.text, addr0c.text))
		self.assertEqual(addr0p.street, addr0c.street,
			msg='SonadorFhirAddress: street string created from components does not match parsed output. Expected: %s. Test: %s'
				% (addr0c.street, addr0p.street))
		self.assertEqual(addr0c.region_international, self.addr0_region,
			msg='SonadorFhiAddress: incorrect region str. Expected: "%s". Test: "%s"' % (self.addr0_region, addr0c.region_international))

	# Test Address 1
	addr1_str = '1858 North 200 East Centerville, UT 84014 USA'
	addr1_line1 = '1858 North 200 East'
	addr1_city = 'Centerville'
	addr1_state = 'UT'
	addr1_postalCode = '84014'
	addr1_country = 'USA'
	addr1_region = 'Centerville, UT 84014 USA'

	def test_fhir_addr1(self, *args, **kwargs):
		'''	SonadorFhirAddress test case 1: address with directional components:
			"1858 North 200 East Centerville, UT 84014 USA"
		'''

		# Parse address from address string
		addr1p = SonadorFhirAddress(self.addr1_str)

		# Create address instance from components
		addr1c = SonadorFhirAddress(line=[self.addr1_line1], 
			city=self.addr1_city, state=self.addr1_state, postalCode=self.addr1_postalCode, country=self.addr1_country, 
			address_type=FHIR_ADDR_TYPE_PHYSICAL)

		# Ensure that address components match expected values
		self.assertEqual(self.addr1_str, addr1p.text, 
			msg='SonadorFhirAddress: modified text representation of address. Expected: "%s". Test: "%s"' 
				% (self.addr1_str, addr1p.text))
		self.assertEqual(addr1p.line[0], self.addr1_line1,
			msg='SonadorFhirAddress: incorrectly parsed line 1 of address. Expected: "%s". Test: "%s"'
				% (addr1p.line[0], self.addr1_line1))
		self.assertTrue(len(addr1p.line) == 1,
			msg='SonadorFhirAddress: incorrectly parsed two lines from single line address. Expected: "%s". Test: "%s"'
				% (addr1p.line_sep.join(addr1p.line), self.addr1_line1))
		self.assertEqual(addr1p.region_international, self.addr1_region,
			msg='SonadorFhirAddress: incorrect region. Expected: "%s". Test: "%s"' % (self.addr1_region, addr1p.region_international))
		self.assertEqual(addr1p.city, self.addr1_city, 
			msg='SonadorFhirAddress: incorrectly parsed city. Expected: "%s". Test: "%s' % (self.addr1_city, addr1p.city))
		self.assertEqual(addr1p.state, self.addr1_state,
			msg='SonadorFhiAddress: incorrectly parsed state. Expected: "%s". Test: "%s"' % (self.addr1_postalCode, addr1p.state))
		self.assertEqual(addr1p.postalCode, self.addr1_postalCode, 
			msg='SonadorFhirAddress: incorrectly parsed postal code. Expected: "%s". Test: "%s"' 
				% (self.addr1_postalCode, self.addr1_postalCode))
		self.assertEqual(addr1p.country, self.addr1_country,
			msg='SonadorFhirAddress: incorrectly parsed country. Expected: "%s". Test: "%s"' 
				% (addr1p.country, self.addr1_country))

		# Ensure that parsed address matches address created from components
		self.assertEqual(addr1p.text, addr1c.text,
			msg='SonadorFhirAddress: text string created from components does not match expected output. Expected: "%s". Test: "%s"'
				% (addr1p.text, addr1c.text))
		self.assertEqual(addr1p.street, addr1c.street,
			msg='SonadorFhirAddress: street string created from components does not match parsed output. Expected: %s. Test: %s'
				% (addr1c.street, addr1p.street))
		self.assertEqual(addr1c.region_international, self.addr1_region,
			msg='SonadorFhiAddress: incorrect region str. Expected: "%s". Test: "%s"' % (self.addr1_region, addr1c.region_international))

	# Test Address 2
	addr2_str = '4329 Elizabeth Circle West Olive Branch, MS 38654 USA'
	addr2_line1 = '4329 Elizabeth Circle West'
	addr2_city = 'Olive Branch'
	addr2_state = 'MS'
	addr2_postalCode = '38654'
	addr2_country = 'USA'
	addr2_region = 'Olive Branch, MS 38654 USA'

	def test_fhir_addr2(self, *args, **kwargs):
		'''	SonadorFhirAddress test case 2: address with directional components:
			"4329 Elizabeth Circle West Olive Branch, MS 38654 USA"
		'''
		# Parse address from address string
		addr2p = SonadorFhirAddress(self.addr2_str)

		# Create address instance from components
		addr2c = SonadorFhirAddress(line=[self.addr2_line1], 
			city=self.addr2_city, state=self.addr2_state, postalCode=self.addr2_postalCode, country=self.addr2_country, 
			address_type=FHIR_ADDR_TYPE_PHYSICAL)

		# Ensure that address components match expected values
		self.assertEqual(self.addr2_str, addr2p.text, 
			msg='SonadorFhirAddress: modified text representation of address. Expected: "%s". Test: "%s"' 
				% (self.addr2_str, addr2p.text))
		self.assertEqual(addr2p.line[0], self.addr2_line1,
			msg='SonadorFhirAddress: incorrectly parsed line 1 of address. Expected: "%s". Test: "%s"'
				% (addr2p.line[0], self.addr2_line1))
		self.assertTrue(len(addr2p.line) == 1,
			msg='SonadorFhirAddress: incorrectly parsed two lines from single line address. Expected: "%s". Test: "%s"'
				% (addr2p.line_sep.join(addr2p.line), self.addr2_line1))
		self.assertEqual(addr2p.region_international, self.addr2_region,
			msg='SonadorFhirAddress: incorrect region. Expected: "%s". Test: "%s"' % (self.addr2_region, addr2p.region_international))
		self.assertEqual(addr2p.city, self.addr2_city, 
			msg='SonadorFhirAddress: incorrectly parsed city. Expected: "%s". Test: "%s' % (self.addr2_city, addr2p.city))
		self.assertEqual(addr2p.state, self.addr2_state,
			msg='SonadorFhiAddress: incorrectly parsed state. Expected: "%s". Test: "%s"' % (self.addr2_postalCode, addr2p.state))
		self.assertEqual(addr2p.postalCode, self.addr2_postalCode, 
			msg='SonadorFhirAddress: incorrectly parsed postal code. Expected: "%s". Test: "%s"' 
				% (self.addr2_postalCode, self.addr2_postalCode))
		self.assertEqual(addr2p.country, self.addr2_country,
			msg='SonadorFhirAddress: incorrectly parsed country. Expected: "%s". Test: "%s"' 
				% (addr2p.country, self.addr2_country))

		# Ensure that parsed address matches address created from components
		self.assertEqual(addr2p.text, addr2c.text,
			msg='SonadorFhirAddress: text string created from components does not match expected output. Expected: "%s". Test: "%s"'
				% (addr2p.text, addr2c.text))
		self.assertEqual(addr2p.street, addr2c.street,
			msg='SonadorFhirAddress: street string created from components does not match parsed output. Expected: %s. Test: %s'
				% (addr2c.street, addr2p.street))
		self.assertEqual(addr2c.region_international, self.addr2_region,
			msg='SonadorFhiAddress: incorrect region str. Expected: "%s". Test: "%s"' % (self.addr2_region, addr2c.region_international))

	# Test Address 3
	addr3_str = '''
			7135 Goodlette Farms Pkway
			Suite 200 ATTN: Visionaire
			Cordova, TN 38016
			USA
		'''.replace('\t', '').strip()
	addr3_line1 = '7135 Goodlette Farms Pkway'
	addr3_line2 = 'Suite 200 ATTN: Visionaire'
	addr3_city = 'Cordova'
	addr3_state = 'TN'
	addr3_postalCode = '38016'
	addr3_country = 'USA'
	addr3_region = 'Cordova, TN 38016 USA'

	def test_fhir_addr3(self, *args, **kwargs):
		'''	SOnadorFhirAddress test case 3: address with line breaks and ATTN line

			"7135 Goodlette Farms Pkway
			Suite 200, ATTN: Visionaire
			Cordova, TN 38016
			USA"
		'''
		# Parse address from address string
		addr3p = SonadorFhirAddress(self.addr3_str)

		# Create address instance from components
		addr3c = SonadorFhirAddress(line=[self.addr3_line1, self.addr3_line2], 
			city=self.addr3_city, state=self.addr3_state, postalCode=self.addr3_postalCode, country=self.addr3_country, 
			address_type=FHIR_ADDR_TYPE_PHYSICAL, line_sep='\n', text_sep='\n')

		# Ensure that address components match expected values
		self.assertEqual(self.addr3_str, addr3p.text, 
			msg='SonadorFhirAddress: modified text representation of address. Expected: "%s". Test: "%s"' 
				% (self.addr3_str, addr3p.text))
		self.assertEqual(addr3p.line[0], self.addr3_line1,
			msg='SonadorFhirAddress: incorrectly parsed line 1 of address. Expected: "%s". Test: "%s"'
				% (addr3p.line[0], self.addr3_line1))
		self.assertEqual(addr3p.line[1], self.addr3_line2,
			msg='SonadorFhirAddress: incorrectly parsed line 2 of address. Expected: "%s". Test: "%s"'
				% (self.addr3_line2, addr3p.line[1]))
		self.assertEqual(addr3p.region_international, self.addr3_region,
			msg='SonadorFhirAddress: incorrect region. Expected: "%s". Test: "%s"' % (self.addr3_region, addr3p.region_international))
		self.assertEqual(addr3p.city, self.addr3_city, 
			msg='SonadorFhirAddress: incorrectly parsed city. Expected: "%s". Test: "%s' % (self.addr3_city, addr3p.city))
		self.assertEqual(addr3p.state, self.addr3_state,
			msg='SonadorFhiAddress: incorrectly parsed state. Expected: "%s". Test: "%s"' % (self.addr3_postalCode, addr3p.state))
		self.assertEqual(addr3p.postalCode, self.addr3_postalCode, 
			msg='SonadorFhirAddress: incorrectly parsed postal code. Expected: "%s". Test: "%s"' 
				% (self.addr3_postalCode, self.addr3_postalCode))
		self.assertEqual(addr3p.country, self.addr3_country,
			msg='SonadorFhirAddress: incorrectly parsed country. Expected: "%s". Test: "%s"' % (addr3p.country, self.addr3_country))

		# Ensure that parsed address matches address created from components
		self.assertEqual(addr3p.text, addr3c.text,
			msg='SonadorFhirAddress: text string created from components does not match expected output. Expected: "%s". Test: "%s"'
				% (addr3p.text, addr3c.text))
		self.assertEqual(addr3p.street, addr3c.street,
			msg='SonadorFhirAddress: street string created from components does not match parsed output. Expected: %s. Test: %s'
				% (addr3c.street, addr3p.street))
		self.assertEqual(addr3c.region_international, self.addr3_region,
			msg='SonadorFhiAddress: incorrect region str. Expected: "%s". Test: "%s"' % (self.addr3_region, addr3c.region_international))
