'''	Helper utilities used for working with local DICOM instances (pydicom.Dataset or pydicom.FileDataset).
'''

import datetime
from pydicom.dataset import Dataset

from ...apisettings import SONADOR_CLIENT, DCM_ORIGINAL_ATTRIBUTES_SEQUENCE, \
	DCM_MODIFIED_ATTRIBUTES_SEQUENCE, DCM_ATTRIBUTE_MOD_DATETIME, \
	DCM_MODIFYING_SYSTEM, DCM_SOURCE_PREVIOUS_VALUES, DCM_MODIFICATION_REASON, \
	DCM_MODIFY_CODE_COERCE, DCM_MODIFY_CODE_CORRECT


def dcmheader_modify(dcm, headers, modified_attributes_sequence=True, 
		mod_ts=None, modifying_system=SONADOR_CLIENT, dcm_modify_code=DCM_MODIFY_CODE_COERCE,
		dcm_previous_values_source=None):
	'''	Modify the DICOM instance to use the provided header values. This method
		works on local instances of DICOM data. For modifying remote data stored in
		Sonador utilize imaging.orthanc.base.ImagingResource.modify method.

		@input dcm (pydicom.Dataset): DICOM instance to be modified
		@input headers (dict): Dictionary of new headers to be applied
		@input modified_attributes_sequence (bool, default=True): when True,
			create (or extend) a modified attributes sequence containing the
			previous values.
		@input mod_ts (datetime, default=datetime.utcnow): date/time to be used
			for the modified timestamp.
		@input modifying_system (str, default='Sonador-Client'): string to be used
			for the value of "modifying system".
		@input dcm_modify_code (str, default='COERCE'): string code to be used
			for indicating the reason why the value was changed.
		@input dcm_previous_values_source (str, default=None): add explanation as
			to why the value was modified.

		@returns modified version of the DICOM instance with the headers applied
	'''
	# Track original header values as part of original attributes sequence
	if modified_attributes_sequence:

		# Retrieve original attributes sequence, create a new "modattr sequence"
		originalattr_sequence = getattr(dcm, DCM_ORIGINAL_ATTRIBUTES_SEQUENCE, None) or []
		
		# Create modified attributes entry for the original sequence
		modified_attributes = Dataset()
		setattr(modified_attributes, DCM_ATTRIBUTE_MOD_DATETIME, mod_ts or datetime.datetime.utcnow())
		setattr(modified_attributes, DCM_MODIFYING_SYSTEM, modifying_system)
		setattr(modified_attributes, DCM_MODIFICATION_REASON, dcm_modify_code)
		
		if dcm_previous_values_source:
			setattr(modified_attributes, DCM_SOURCE_PREVIOUS_VALUES, dcm_previous_values_source)
		
		# Container for all modified attributes
		modattr_sequence = []
		
	else: modattr_sequence is None

	# Iterate through headers and apply the new values
	for h,v in headers.items():

		# Record previous values
		if modattr_sequence is not None and getattr(dcm, h, None) and getattr(dcm, h) != v:

			# Create dataset instance for previous value
			oattr = Dataset()
			setattr(oattr, h, getattr(dcm, h))

			# Append to sequence chain
			modattr_sequence.append(oattr)

		# Modify header value
		setattr(dcm, h, v)

	# Add modified attributes to the original attributes sequence and original to instance
	if modattr_sequence:
		setattr(modified_attributes, DCM_MODIFIED_ATTRIBUTES_SEQUENCE, modattr_sequence)
		originalattr_sequence.append(modified_attributes)
		setattr(dcm, DCM_ORIGINAL_ATTRIBUTES_SEQUENCE, originalattr_sequence)

	return dcm
