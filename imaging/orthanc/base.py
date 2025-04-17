''' Model classes associated with Orthanc DICOM resources. Provides tools
	for representing, queryying, modifying, and removing core DICOM resource instances.
'''
import six, requests, json, csv, collections, logging, functools, posixpath, zipfile, pydicom, datetime, traceback
from io import BytesIO

from abc import ABCMeta, abstractmethod

from urllib.parse import urlencode

from collections import namedtuple
from collections.abc import Iterable
from collections import OrderedDict

from tabulate import tabulate

from client import apisettings as gcapicodes
from client import auth as guru_auth
from client.errors import ClientOperationError, ConfigurationError

from client.utils.urls import build_url
from client.utils.object import pick
from client.utils.microservices import server_controloperation_json_response, RemotePage
from client.utils.colors import RGB

from ...apisettings import ImageCoord, ImageSpacing, ImageOrientation, ImageStackShape, \
	RGBColor, LABColor, XYZColor, EUCLID_COORD_ORIGIN, DicomDatetimePairKey, DicomDatetimePair, DicomMetaKey, DicomMeta, \
	IMAGING_SERVER_RESOURCE_PATIENT, IMAGING_SERVER_RESOURCE_STUDY, IMAGING_SERVER_RESOURCE_SERIES, \
	IMAGING_SERVER_RESOURCE_IMAGE, IMAGING_SERVER_LAST_UPDATE, IMAGING_SERVER_DICOMTAGS_SIGNATURE, \
	DCMHEADER_PATIENT_ID, DCMHEADER_PATIENT_NAME, \
	DCMHEADER_PATIENT_SEX, DCMHEADER_PATIENT_BIRTHDATE, \
	DCMHEADER_IMAGE_POSITION_PATIENT, DCMHEADER_IMAGE_ORIENTATION_PATIENT, DCM_DATE_STRFORMAT, DCM_TIME_STRFORMAT, \
	DCMHEADER_MODALITY, DCMHEADER_STUDY_INSTANCE_UID, DCMHEADER_STUDY_ID, \
	DCMHEADER_STUDY_DATE, DCMHEADER_STUDY_TIME, \
	DCMHEADER_SERIES_INSTANCE_UID, DCMHEADER_SERIES_NUMBER, DCMHEADER_OPERATORS_NAME, \
	DCMHEADER_SERIES_DATE, DCMHEADER_SERIES_TIME, DCMHEADER_SERIES_DESCRIPTION, DCMHEADER_PATIENT_POSITION, \
	DCMHEADER_SLICE_THICKNESS, DCMHEADER_SLICE_LOCATION, DCMHEADER_PIXEL_SPACING, \
	DCMHEADER_BODY_PART_EXAMINED, DCM_VERSION_2021b, \
	DCMHEADER_MODALITIES_IN_STUDY, DCM_MODALITY_SR, DCM_MODALITY_SEG, DCM_MODALITY_DOC, \
	DCMHEADER_SOP_CLASS_UID, DCMHEADER_SOP_INSTANCE_UID, DCMHEADER_CONTENT_DATE, DCMHEADER_CONTENT_TIME, DCMHEADER_CONTENT_DESCRIPTION, DCMHEADER_INSTANCE_NUMBER, \
	DCMTS_STUDY, DCMTS_SERIES, DCMTS_CONTENT
from ...apisettings.media import DCMEDIA_M3D_MODALITY

from ...helpers import request_client_error, fetch_sonador_session_token, response2filearchive
from ...helpers.valuerep import str2name
from ...serialization import json_datetime_parser, json_str2datetime, dcm_str2date, dcm_str2time
from ...remote import SonadorBaseObject, SonadorObjectCollection, fetch_sonador_data_collection
from ...servers import ImagingServerChildCollection, ImagingServerChildBaseObject, SonadorImagingServer
from ...servers.auth import SonadorUser, SonadorGroup
from ...errors import only_duplicate_resource_error

logger = logging.getLogger(__name__)


FILEARCHIVE_TYPE_ZIPARCHIVE = 'zip'
FILEARCHIVE_TYPE_DICOMDIR = 'dicomdir'
FILEARCHIVE_TYPE_SUPPORTED = (FILEARCHIVE_TYPE_ZIPARCHIVE, FILEARCHIVE_TYPE_DICOMDIR)


def parse_image_orientation(coords):
	'''	Parse the provided coordinates to a row/column paris of X,Y,Z values

		@input coords (variable): If the input is a string, it will be parsed to a pair
			of image coordinates containing the x,y,z values for row/column image position
			values.

		@returns tuple (row,column) of ImageCoord
	'''
	# Split into a pair of x, y, z coordinates by delimeter. Try '\', ',', before 
	# falling back to ' '
	if isinstance(coords, six.string_types):

		# Split the string into x,y,z,x,y,z tuple
		coords = coords.split('\\' if '\\' in coords
			else ',' if ',' in coords
			else ' ')

		# Ensure that all expected values are present
		if not len(coords) == 6:
			raise ValueError('Invalid patient orientation, expected row/column x,y,z pairs')

		# Unpack components of the tuple into row and column ImageCoord
		row, col = ImageCoord(*tuple(float(v) for v in coords[:3])), ImageCoord(*tuple(float(v) for v in coords[3:]))
		coords = ImageOrientation(row, col)

	return coords


class ImagingResourceCoreMixin(object, metaclass=ABCMeta):
	'''	Mixin class with convenience properties for accessing common Orthanc data fields.
	'''
	main_dcmtags_attr = 'MainDicomTags'

	def fetch_meta(self, *args, headers=None, **kwargs):
		'''	Retrieved the Orthanc metadata properties for the resource
		'''
		return server_controloperation_json_response(
			self.pacs._request_get(
				self.pacs.orthanc_apiurl(
					posixpath.join(self.resource_url, 'metadata'),
					query_params=json.dumps({ 'expand': True, })
				),
				lambda r: request_client_error(
					'Unable to retrieve metadata for %s on server %s. Status code: %s.' % (self.pk, self.pacs.server_label, r.status_code),
					r
				),
				headers=self.pacs.orthanc_request_headers(headers=headers)
			)
		)

	@property
	def meta(self):
		if getattr(self, '_meta', None) is None:
			self._meta = self.fetch_meta()

		return self._meta

	@property
	def lastupdate(self):
		'''	Timestamp of last resource update
		'''
		if getattr(self, '_lastupdate', None) is None:
			r = self.pacs._request_get(
				self.pacs.orthanc_apiurl(
					posixpath.join(self.resource_url, 'metadata', IMAGING_SERVER_LAST_UPDATE),
					query_params = json.dumps({'expand': True, })
				),
				lambda r: request_client_error(
					'Unable to retrieve metadata for %s on server %s. Status code: %s.' % (self.pk, self.pacs.server_label, r.status_code),
					r
				),
				headers=self.pacs.orthanc_request_headers(headers=None)
			).text
			setattr(self, '_lastupdate', json_str2datetime(r))

		return getattr(self, '_lastupdate', None)

	@property
	def modified_from(self):
		if getattr(self, '_modified_from', None) is None:
			try:
				r =  self.pacs._request_get(
					self.pacs.orthanc_apiurl(
						posixpath.join(self.resource_url, 'metadata', 'ModifiedFrom'),
						query_params = json.dumps({'expand': True, })
					),
					lambda r: request_client_error(
						'Unable to retrieve metadata for %s on server %s. Status code: %s.' % (self.pk, self.pacs.server_label, r.status_code),
						r
					),
					headers=self.pacs.orthanc_request_headers(headers=None)
				).text
			except Exception as e:
				logger.warning('Imaging resource for %s is not modified from another resource' % self.pk)
				r = None
			setattr(self, '_modified_from', r)
		return getattr(self, '_modified_from', None)

	@property
	def tags_signature(self):
		if getattr(self, '_tags_signature', None) is None:
			r = self.pacs._request_get(
				self.pacs.orthanc_apiurl(
					posixpath.join(self.resource_url, 'metadata', IMAGING_SERVER_DICOMTAGS_SIGNATURE),
					query_params = json.dumps({'expand': True, })
				),
				lambda r: request_client_error(
					'Unable to retrieve metadata for %s on server %s. Status code: %s.' % (self.pk, self.pacs.server_label, r.status_code),
					r
				),
				headers=self.pacs.orthanc_request_headers(headers=None)
			).text
			setattr(self, '_tags_signature', json_str2datetime(r))

		return getattr(self, '_tags_signature', None)

	@property
	def stable(self):
		return self._objectdata.get('IsStable')

	@property
	def dicomdata(self):
		return self._objectdata.get(self.main_dcmtags_attr, {})

	@property
	@abstractmethod
	def resource_url(self):
		'''	URL for the imaging resource
		'''

	@property
	def url(self):
		return self.resource_url

	@property
	@abstractmethod
	def kafka_url(self):
		'''	URL which should be used to trigger export of resource data to the Orthanc Kafka topic
		'''

	def fetch_kafka_data(self, *args, **kwargs):
		'''	Retrieve Kafka data payload for the resource. (Used for validating data structure and testing.)

			@returns dict
		'''
		r = self.pacs._request_get(
			self.pacs.orthanc_apiurl(self.kafka_url),
			lambda r: request_client_error(
				'Unable to retrieve Kafka data for %s on server %s. Status code: %s.'
					% (self.kafka_url, self.pacs.server_label, r.status_code),
				r),
			headers=self.pacs.orthanc_request_headers(**kwargs), verify=self.pacs.server.verify_ssl(**kwargs))

		return server_controloperation_json_response(r)

	def kafka_export(self, data=None, **kwargs):
		'''	Trigger export of the resource data to Kafka

			@returns dict
		'''
		r = self.pacs._request_post(
			self.pacs.orthanc_apiurl(self.kafka_url),
			lambda r: request_client_error(
				'Unable to trigger export of Kafka data for %s on server %s. Status code: %s.'
				% (self.kafka_url, self.pacs.server_label, r.status_code),
				r),
			json=data or {}, headers=self.pacs.orthanc_request_headers(**kwargs), verify=self.pacs.server.verify_ssl(**kwargs))

		return server_controloperation_json_response(r)

	def modify(self, replace=None, remove=None, keep=None, keep_source=None,
			remove_private_tags=False, force=False, transcode=None, private_creator=None,
			modify=None, headers=None, verify=None, **kwargs):
		'''	Modify tags or metadata associated with a DICOM resource.  The modified DICOM instances will be stored into a brand 
			new resource, whose Orthanc identifiers will be returned by the job. 

			@input replace (dict): Dictionary of DICOM tags to be replaced for the resource
			@input remove (iterable of tags): Iterable of tag names to be removed for the resource
			@input keep (iterable of tags, default=None): Iterable of tag names for which the 
				original values should be kept. If None, new values will be generated
				for StudyInstanceUID, SeriesInstanceUID, and SOPInstanceUID.
			@input keep_source (bool, default=true): if set to False, instructs Orthanc to remove
				the original sources. By default, the original resources are kept.
			@input remove_private_tags (bool, default=False): Flag that, when true, will cause
				private tags (i.e., manufacturer-specific tags) to be removed
			@input force (bool, default=False): Flag that, when true, allows modification of DICOM identifiers
				such as PatientID, StudyInstanceUID, SeriesInstanceuid, and SOPInstanceUID.
			@input transcode (str, default=None): Allows for the definition of the TransferSyntax of the 
				modified resources.

			@returns requests.Response
		'''
		modify = modify or {}
		if replace and not isinstance(replace, dict):
			raise TypeError('Unable to modify DICOM resource, replace terms must be submitted as a dictonary')
		if remove and not isinstance(remove, Iterable):
			raise TypeError('Unable to remove requested DICOM tags, remove terms must be submitted as an interable')

		# Create request structure
		modify.update({ 'RemovePrivateTags': remove_private_tags, 'Force': force })
		if replace:
			modify['Replace'] = replace
		if remove:
			modify['Remove'] = remove
		if remove_private_tags:
			modify['RemovePrivateTags'] = remove_private_tags
		if transcode:
			modify['Transcode'] = transcode
		if private_creator:
			modify['PrivateCreator'] = private_creator
		if keep:
			modify['Keep'] = keep
		if keep_source is not None:
			modify['KeepSource'] = keep_source

		# Execute operation
		logger.debug('Structure of modification request:\n%s' % json.dumps(modify))
		r = self.pacs._request_post(
			self.pacs.orthanc_apiurl(posixpath.join(self.resource_url, 'modify')),
			lambda r: request_client_error(
				'Unable to modify DICOM resource tags/metadata for %s on server %s. Status code: %s.' % (
					self.resource_url, self.pacs.server_label, r.status_code),
				r),
			json=modify, headers=self.pacs.orthanc_request_headers(headers=headers), verify=verify)

		logger.debug('Response from PACS imaging server:\n%s' % r.content)
		return r

	def delete(self, verify=None, headers=None, **kwargs):
		'''	Remove the imaging resource from Orthanc
		'''
		return self.pacs._request_delete(
			self.pacs.orthanc_apiurl(self.resource_url),
			lambda r: request_client_error(
				'Unable to delete resource %s from imaging server %s, a server error occurred' % (self.url, self.pacs.server_label),
				r),
			headers=self.pacs.orthanc_request_headers(headers=headers), verify=verify, **kwargs)

		return r


class ImagingResourceParentMixin(object, metaclass=ABCMeta):
	'''	Mixin class which defines the resource/parent interface for Orthanc imaging resources
	'''
	@property
	@abstractmethod
	def parent(self):
		'''	Retrieve the parent of the current resource
		'''


class ImagingResourceMixin(ImagingResourceCoreMixin):
	'''	Mixin class with convenience properties for accessing data fields on higher-order resources 
		such as series, studies, and patients.
	'''
	patient_dcmtags_attr = 'PatientMainDicomTags'

	@property
	def patientdata(self):
		return self._objectdata.get(self.patient_dcmtags_attr, {})

	@property
	@abstractmethod
	def filearchive_url(self):
		''' File archive URL for the imaging resource
		'''

	@property
	@abstractmethod
	def dicomdir_url(self):
		''' DICOMDIR archive URL for the resource
		'''

	@property
	@abstractmethod
	def cache_indexurl(self):
		'''	Sonador cache URL used to index the resource
		'''

	@property
	def user_acl_url(self):
		'''	URL for user authorization policies associated with the resource
		'''
		return posixpath.join(self.resource_url, 'acl/user')

	@property
	def dicomweb_user_acl_url(self):
		'''	DICOMweb URL for user ACL policies
		'''
		return posixpath.join(self.dicomweb_resource_url, 'acl/user')

	@property
	def group_acl_url(self):
		'''	URL for group authorization policies associated with the resource
		'''
		return posixpath.join(self.resource_url, 'acl/group')

	@property
	def dicomweb_group_acl_url(self):
		'''	DICOMweb URL for group ACL policies
		'''
		return posixpath.join(self.dicomweb_resource_url, 'acl/group')

	@property
	def type(self):
		return self._objectdata.get('Type')

	def filearchive(self, cache=False, filearchive_type=FILEARCHIVE_TYPE_ZIPARCHIVE, verify=None):
		'''	Retrieve a ZIP archive of all data associated with the resource.

			@input cache (bool, default=False): Cache the data locally to speed up access.

			@returns zipfile.ZipFile
		'''
		# Retrieve cached copy of the file (if available)
		if getattr(self, '_filearchive', None):
			return self._filearchive

		# Determine URL from which to retrieve the data
		if FILEARCHIVE_TYPE_DICOMDIR == FILEARCHIVE_TYPE_ZIPARCHIVE:
			filearchive_url = self.filearchive_url
		elif FILEARCHIVE_TYPE_DICOMDIR == FILEARCHIVE_TYPE_DICOMDIR:
			filearchive_url = self.dicomdir_url
		else:
			raise TypeError('Unable to download archive of image data, invalid archive type: %s' % filearchive_type)

		# Retrieve file data from Orthanc
		r = self.pacs._request_get(
			self.pacs.orthanc_apiurl(filearchive_url), 
			lambda r: request_client_error(
				'Unable to retrieve DICOM resource file data for %s on server % s. Status code: %s.' % (self.filearchive_url, self.pacs.server_label, r.status_code),
				r),
			headers=self.pacs.orthanc_request_headers(), verify=verify)

		# Initialize file archive from request data, cache (if indicated)
		farchive = response2filearchive(r)
		if cache:
			setattr(self, '_filearchive', farchive)

		return farchive

	def index(self, link=True, headers=None, rdata=None, **kwargs):
		''' Add the resouce to the Sonador resource cache.

			@returns requests.Response
		'''
		rdata = rdata or {}

		# Request components
		rdata['link'] = link

		r = self.pacs._request_post(
			self.pacs.orthanc_apiurl(self.cache_indexurl),
			lambda r: request_client_error(
				'Unable to add DICOM resource %s on server %s to the Sonador resource cache. Status code: %s.'
					% (self.cache_indexurl, self.pacs.server_label, r.status_code),
				r),
			json=rdata, headers=self.pacs.orthanc_request_headers(headers=headers), **kwargs)

		logger.debug('Response from PACS imaging server:\n%s' % r.content)
		return r

	def _clear_index(self, headers=None, rdata=None, **kwargs):
		'''	Remove the resource entry from the Sonador resource cache

			@returns requests.Response
		'''
		# Request keyword arguments
		rkwargs = {}

		if rdata:
			rkwargs['json'] = rdata or {}

		r = self.pacs._request_delete(
			self.pacs.orthanc_apiurl(self.cache_indexurl),
			lambda r: request_client_error(
				'Unable to remove DICOM resource %s from server %s Sonador resoruce cache. Status code: %s.'
					% (self.cache_indexurl, self.pacs.server_label, r.status_code),
				r),
			headers=self.pacs.orthanc_request_headers(headers=headers), **kwargs)

		logger.warning('Response from PACS imaging server:\n%s' % r.content)
		return r

	def reconstruct(self, reconstruct_files=False, headers=None, verify=None, rdata=None):
		'''	Launch a job which to re-build the resource in the database. Refer to 
			https://api.orthanc-server.com/ for endpoint details.

			@input reconstruct_files (bool, default=False): When true, the reconstruction will
				also reconstruct the files of resources, which will re-code the instances to use 
				the server's "ingest transcoding" and "storage compression."

			@returns requests.Response
		'''
		rdata = rdata or {}

		# Request components
		if reconstruct_files:
			rdata['ReconstructFiles'] = reconstruct_files

		r = self.pacs._request_post(
			self.pacs.orthanc_apiurl(posixpath.join(self.resource_url, 'reconstruct')),
			lambda r: request_client_error(
				'Unable to reconstruct DICOM resource for %s on server %s. Status code: %s.'
					% (self.resource_url, self.pacs.server_label, r.status_code),
				r),
			json=rdata, headers=self.pacs.orthanc_request_headers(headers=headers), verify=verify)

		logger.debug('Response from PACS imaging server:\n%s' % r.content)
		return r

	def anonymize(self, replace=None, keep=None, remove=None, keep_private_tags=True, 
			dicom_version=DCM_VERSION_2021b, anonymize=None, headers=None, verify=None):
		'''	Anonymize the resource. Anonymization erases all tags specified in Table E.1-1 from PS 3.15
			 of the DICOM standard. (Refer to 
			 http://dicom.nema.org/medical/dicom/current/output/chtml/part15/chapter_E.html#table_E.1-1.)

			 @input replace (dict, default=None): dict of DICOM tags to replace on the resource.
			 	Example: { 'PatientName': 'Example Patient', '0010-1011': 'Example Tag Value'}.
			 	Replacements are applied after all the tags to anonymize have been removed. replace
			 	may be used to add new tags to the resource.
			 @input keep (iterable, default=None): iterable of tags to be preserved from full anonymization.
			 @input keep_private_tags (bool, default=True): preserves private (manufacturer-specific) tag
			 	values. The default behavior of the server is to remove the tags.
			 @input dicom_version (str, default='2021b'): version of the DICOM standard to be used
			 	for anonymization.
			 @input remove (iterable, default=None): iterable ot tags to be removed outside of those
			 	specified in the standard.

			 @returns request.Response
		'''
		anonymize = anonymize or {}
		if replace and not isinstance(replace, dict):
			raise TypeError('Unable to anonymize DICOM resource, replace terms must be submitted as a dictionary.')
		if remove and not isinstance(remove, Iterable):
			raise TypeError('Unable to remove DICOM tags, remove terms must be submitted as an iterable.')

		# Create request structure
		anonymize.update({ 'KeepPrivateTags': keep_private_tags, 'DicomVersion': dicom_version })
		if replace:
			anonymize['Replace'] = replace
		if remove:
			anonymize['Remove'] = remove
		if keep:
			anonymize['Keep'] = keep

		# Execute operation
		logger.debug('Structure of modification request:\n%s' % json.dumps(anonymize))
		r = self.pacs._request_post(
			self.pacs.orthanc_apiurl(posixpath.join(self.resource_url, 'anonymize')),
			lambda r: request_client_error(
				'Unable to anonymize DICOM resource tags for %s on server %s. Status code: %s.' % (
					self.resource_url, self.pacs.server_label, r.status_code),
				r),
			json=anonymize, headers=self.pacs.orthanc_request_headers(headers=headers), verify=verify)

		logger.debug('Response from PACS imaging server:\n%s' % r.content)
		return r

	@property
	def user_acl_modelcollection_class(self):
		'''	Model collection class that should be used to initialize user ACL collections
		'''
		from .auth import OrthancUserResourceAccessControlListCollection
		return OrthancUserResourceAccessControlListCollection

	def fetch_user_acl(self, **kwargs):
		'''	Retrieve user ACL policies associated with the resource

			@returns collection of ACL policies
		'''
		return self.user_acl_modelcollection_class.fetch(parent=self, **kwargs)

	def user_acl_from_json(self, jdata, **kwargs):
		'''	Initialize ACL collection from JSON

			@returns collection of user ACL policies
		'''
		return self.server._init_dataclass_from_json(
			self.user_acl_modelcollection_class, jdata, pacs=self.pacs, **kwargs)

	@property
	def user_acl(self):
		'''	User ACL policies associated with the resource
		'''
		if getattr(self, '_user_acl', None) is None:
			setattr(self, '_user_acl', self.fetch_user_acl())

		return self._user_acl

	@user_acl.setter
	def user_acl(self, acl_collection):
		'''	Set user ACL policy collection for the resource
		'''
		if not isinstance(acl_collection, self.user_acl_modelcollection_class):
			raise ValueError('Input must be an instance of a user ACL collection')

		setattr(self, '_user_acl', acl_collection)

	def create_user_acl(self, user, policy, fetch_existing=True, update_existing=True, **kwargs):
		'''	Create policy for the provided user
		'''
		if not isinstance(user, SonadorUser):
			raise ValueError('Input must be a Sonador user instance')
		if not isinstance(policy, dict):
			raise ValueError('Invalid user ACL policy')

		policy['User'] = user.pk
		try:
			return self.user_acl_modelcollection_class.create(self, policy, **kwargs)
		
		except ClientOperationError as err:
			_details = getattr(err, 'details', {})

			# Attempt to retrieve existing instance of the ACL
			if fetch_existing and only_duplicate_resource_error(err, field_check='User'):

				# Inspect server response for ID of existing policy
				if _details.get(gcapicodes.SERVER_RESPONSE):
					_rdata = json.loads(_details.get(gcapicodes.SERVER_RESPONSE))

					# Retrieve existing model instance
					if _rdata.get(gcapicodes.OBJECT_DATA) \
						and _rdata.get(gcapicodes.OBJECT_DATA, {}).get(self.user_acl_modelcollection_class.model.pk_attr):
						_acl = self.get_user_acl(
							_rdata.get(gcapicodes.OBJECT_DATA, {}).get(self.user_acl_modelcollection_class.model.pk_attr), **kwargs)

						# Update the existing policy to match the requested policy
						if update_existing:
							_acl.update(policy)
							_acl = self.get_user_acl(_acl.pk, **kwargs)

						return _acl

			raise err

	def get_user_acl(self, cid, *args, **kwargs):
		'''	Retrieve the specified user ACL policy

			@input cid (str): Orthanc resource ID (resource.pk) of the ACL to be retrieved.

			@returns user ACL instance
		'''
		return self.user_acl_modelcollection_class.fetch_modelinstance(self, cid, *args, **kwargs)

	@property
	def group_acl_modelcollection_class(self):
		'''	Model collection class that should be used to initialize group ACL collections
		'''
		from .auth import OrthancGroupResourceAccessControlListCollection
		return OrthancGroupResourceAccessControlListCollection

	def fetch_group_acl(self, **kwargs):
		'''	Retrieve group ACL policies associated with the resource

			@returns collection of ACL policies 
		'''
		return self.group_acl_modelcollection_class.fetch(parent=self, **kwargs)

	def group_acl_from_json(self, jdata, **kwargs):
		'''	Initialize ACL collection from JSOn

			@returns collection of group ACL policies
		'''
		return self.server._init_dataclass_from_json(
			self.group_acl_modelcollection_class, jdata, pacs=self.pacs, **kwargs)

	@property
	def group_acl(self):
		'''	Group ACL policies associated with the resource
		'''
		if getattr(self, '_group_acl', None) is None:
			setattr(self, '_group_acl', self.fetch_group_acl())

		return self._group_acl

	@group_acl.setter
	def group_acl(self, acl_collection):
		if not isinstance(acl_collection, self.group_acl_modelcollection_class):
			raise ValueError('Input must be an instance of a group ACL collection')

		setattr(self, '_group_acl', acl_collection)

	def create_group_acl(self, group, policy, fetch_existing=True, update_existing=True, **kwargs):
		'''	Create policy for the provided group
		'''
		if not isinstance(group, SonadorGroup):
			raise ValueError('Input must be a Sonador group instance')
		if not isinstance(policy, dict):
			raise ValueError('Invalid group ACL policy')

		policy['Group'] = group.pk
		try:
			return self.group_acl_modelcollection_class.create(self, policy, **kwargs)

		except ClientOperationError as err:
			_details = getattr(err, 'details', {})

			# Attempt to retrieve existing instance of the ACL
			if fetch_existing and only_duplicate_resource_error(err, field_check='Group'):

				# Inspect server response for ID of existing policy
				if _details.get(gcapicodes.SERVER_RESPONSE):
					_rdata = json.loads(_details.get(gcapicodes.SERVER_RESPONSE))

					# Retrieve existing model instance
					if _rdata.get(gcapicodes.OBJECT_DATA) \
						and _rdata.get(gcapicodes.OBJECT_DATA, {}).get(self.group_acl_modelcollection_class.model.pk_attr):
						_acl = self.get_group_acl(
							_rdata.get(gcapicodes.OBJECT_DATA, {}).get(self.group_acl_modelcollection_class.model.pk_attr), **kwargs)

						# Update the existing policy to match the requested policy
						if update_existing:
							_acl.update(policy)
							_acl = self.get_group_acl(_acl.pk, **kwargs)

						return _acl

			raise err

	def get_group_acl(self, cid, *args, **kwargs):
		'''	Retrieve the specified group ACL policy

			@input cid (str): Orthanc resource ID (resource.pk) of the ACL to be retrieved.

			@returns group ACL instance
		'''
		return self.group_acl_modelcollection_class.fetch_modelinstance(self, cid, *args, **kwargs)


# Imaging Resource Base Collection
class ImagingResourceBaseCollection(ImagingServerChildCollection):
	'''	Collection of imaging resources stored in a Sonador managed PACS imaging server.
		The base collection defines an interface that can be used to bulk populate
		models related to the instances within the collection.
	'''
	@abstractmethod
	def bulkpopulate_related(self, *args, **kwargs):
		'''	Populate models related to collection instances in the most efficient manner possible
			via the Orthanc /tools/bulk-content endpoint.
		'''


# PACS Imaging


IMAGING_PATIENT_OUTPUT_COLUMNS = OrderedDict((
		('pk', 'Patient ID'),
		('patient_name', 'Patient Name'),
		('patientid', 'MRN'),
		('patient_sex', 'Sex'),
		('birth_date', 'Birth Date'),
	))


class ImagingPatient(ImagingResourceMixin, ImagingServerChildBaseObject):
	'''	Patient 
	'''
	pk_attr = 'ID'
	tabulate_output_columns = IMAGING_PATIENT_OUTPUT_COLUMNS
	fetch_endpoint = 'patients'
	cache_queryurl = '/cache/patients'

	resource_level = IMAGING_SERVER_RESOURCE_PATIENT

	@property
	def resource_url(self):
		return posixpath.join(self.fetch_endpoint, self.pk)

	@property
	def filearchive_url(self):
		return posixpath.join(self.fetch_endpoint, self.pk, 'archive')

	@property
	def dicomdir_url(self):
		return posixpath.join(self.fetch_endpoint, self.pk, 'media')

	@property
	def cache_indexurl(self):
		return posixpath.join(self.cache_queryurl, self.pk, 'index')

	@property
	def kafka_url(self):
		return posixpath.join(self.resource_url, 'kafka')

	@property
	def patient_name_vr(self):
		if self.patient_name:
			return str2name(self.patient_name)

	@property
	def name(self):
		return self.patient_name_vr

	@property
	def patient_name(self):
		return self.dicomdata.get(DCMHEADER_PATIENT_NAME)

	@property
	def patientid(self):
		return self.dicomdata.get(DCMHEADER_PATIENT_ID)

	@property
	def patient_sex(self):
		return self.dicomdata.get(DCMHEADER_PATIENT_SEX)

	@property
	def birth_datestr(self):
		return self.dicomdata.get(DCMHEADER_PATIENT_BIRTHDATE)

	@property
	def birth_date(self):
		'''	Patient birth date

			@returns datetime.date
		'''
		if getattr(self, '_birthdate', None) is None and self.birth_datestr:
			setattr(self, '_birthdate', dcm_str2date(self.birth_datestr))

		return getattr(self, '_birthdate', None)

	@property
	def studies(self):
		return self._objectdata.get('Studies')

	def fetch_studies(self, **kwargs):
		'''	Retrieve details of the studies associated with the patient

			@returns collection of DICOM study instances associated with the current patient
		'''
		verify = kwargs.get('verify', None)

		# Retrieve study details
		r = self.pacs._request_get(
			self.pacs.orthanc_apiurl(posixpath.join(self.resource_url, 'studies')),
			lambda r: request_client_error(
				'Unable to retrieve details for patient %s studies on server %s. Status code: %s.' % (
					elf.pk, self.pacs.server_label, r.status_code),
				r),
			headers=self.pacs.orthanc_request_headers(headers=kwargs.get('headers')), verify=verify)

		# Parse response and return collection
		return self.server._init_dataclass(ImagingStudyCollection, r, pacs=self.pacs, patient=self, **kwargs)

	def studies_from_json(self, jdata, **kwargs):
		'''	Initialize studies collection from JSON structure.
		'''
		return self.server._init_dataclass_from_json(
			ImagingStudyCollection, jdata, pacs=self.pacs, patient=self, **kwargs)

	@property
	def studies_collection(self):
		'''	Cached property for retrieving the study instances associated with the patient
		'''
		if getattr(self, '_studies', None) is None:
			setattr(self, '_studies', self.fetch_studies())

		return self._studies

	def fetch_series(self, **kwargs):
		'''	Retrieve details of the series associated with the patient

			@returns collection of DICOM series instances that associated with the current patient
		'''

		verify = kwargs.get('verify', None)

		# Retrieve series details
		r = self.pacs._request_get(
			self.pacs.orthanc_apiurl(posixpath.join(self.resource_url, 'series')),
			lambda r: request_client_error(
				'Unable to retrieve details for patient %s series on server %s. Status code: %s.' % (
					self.pk, self.pacs.server_label, r.status_code),
				r),
			headers=self.pacs.orthanc_request_headers(headers=kwargs.get('headers')), verify=verify)

		# Parse response and return collection
		return self.server._init_dataclass(ImagingSeriesCollection, r, pacs=self.pacs, patient=self, **kwargs)

	def series_from_json(self, jdata, **kwargs):
		'''	Initialize series collection from JSON structure
		'''
		return self.server._init_dataclass_from_json(
			ImagingSeriesCollection, jdata, pacs=self.pacs, patient=self, **kwargs)

	@property
	def series_collection(self):
		'''	Cached property for retrieving the series instances associated with the patient
		'''
		if getattr(self, '_series', None) is None:
			setattr(self, '_series', self.fetch_series())

		return self._series


class ImagingPatientCollection(ImagingResourceBaseCollection):
	'''	Collection of patients
	'''
	model = ImagingPatient

	def bulkpopulate_related(self, *args, child_studies=True, child_series=True, **kwargs):
		'''	Populate models related to collection instances in the most efficient manner possible.

			@input child_studies (bool, default=True): bulk populate "studies_collection" of collection instances.
			@input child_series (bool, default=True): bulk populate "series_collection" of child studies.
		'''
		if child_series and not child_studies:
			raise ValueError('Unable to populate series data, option rquires that study be retrieved.')

		if child_studies:
			studies_uids = []

			# Aggregate patient UIDs
			for p in self:
				studies_uids.extend(p.studies)

			# Retrieve child studies and unpack
			bdata = self.pacs.fetch_bulk_content(studies_uids, *args, **kwargs)
			bdata_study = bdata.get(IMAGING_SERVER_RESOURCE_STUDY)

			# Unpack data
			for p in self:

				# Study
				if bdata_study:
					p.studies_from_json([bdata_study.get_modelinstance(sid)._objectdata for sid in p.studies if bdata_study.get_modelinstance(sid)])

		if child_series:
			series_uids = []

			# Aggregate series UIDs
			for p in self:
				for s in p.studies_collection:
					series_uids.extend(s.series)

			# Retrieve child series and unpack
			bdata = self.pacs.fetch_bulk_content(series_uids, *args, **kwargs)
			bdata_series = bdata.get(IMAGING_SERVER_RESOURCE_SERIES)

			# Unpack data
			for p in self:
				for s in p.studies_collection:

					# Child series
					if bdata_series:
						s.series_collection = s.series_from_json(
							[bdata_series.get_modelinstance(sid)._objectdata for sid in s.series if bdata_series.get_modelinstance(sid)])
						s._populate_subcollections()


IMAGING_STUDY_OUTPUT_COLUMNS = OrderedDict((
		('patient', 'Parent Patient'),
		('pk', 'Study ID'),
		('patient_name', 'Patient Name'),
		('patientid', 'MRN'),
		('accession_number', 'Accession#'),
		('study_date', 'Study Date'),
		('physician', 'Requesting Physician'),
		('description', 'Description'),
	))


class ImagingStudy(ImagingResourceMixin, ImagingResourceParentMixin, ImagingServerChildBaseObject):
	'''	Imaging study: set of sequences/series/scans
	'''
	pk_attr = 'ID'
	tabulate_output_columns = IMAGING_STUDY_OUTPUT_COLUMNS
	fetch_endpoint = 'studies'
	cache_queryurl = '/cache/studies'

	parent_class = ImagingPatient
	resource_level = IMAGING_SERVER_RESOURCE_STUDY

	def __init__(self, *args, **kwargs):
		self._parent = kwargs.pop('patient', None)
		super().__init__(*args, **kwargs)

		# Study timestamp (parsed from StudyDate and StudyTime)
		self._sts = DicomDatetimePair(self.study_datestr, self.study_timestr, meta=DCMTS_STUDY)

	@property
	@functools.lru_cache()
	def hmeta_key(self):
		'''	Study metadata key: resource, UID header, and DICOM UID
		'''
		return DicomMetaKey(IMAGING_SERVER_RESOURCE_STUDY, DCMHEADER_STUDY_INSTANCE_UID, self.study_uid)

	@property
	@functools.lru_cache()
	def hmeta(self):
		'''	Study metadata: description
		'''
		return DicomMeta(self.description, None, meta=self.hmeta_key)

	@property
	def resource_url(self):
		return posixpath.join(self.fetch_endpoint, self.pk)

	@property
	def dicomweb_resource_url(self):
		return posixpath.join(self.pacs.dicomweb_root, self.fetch_endpoint, self.study_uid)

	@property
	def filearchive_url(self):
		return posixpath.join(self.resource_url, 'archive')

	@property
	def dicomdir_url(self):
		return posixpath.join(self.resource_url, 'media')

	@property
	def cache_indexurl(self):
		return posixpath.join(self.cache_queryurl, self.pk, 'index')

	@property
	def worklist_reviewer_url(self):
		'''	URL for reviewer worklist items associated with the study
		'''
		return posixpath.join(self.resource_url, 'worklists')

	@property
	def dicomweb_worklist_reviewer_url(self):
		'''	DICOMweb reviewer worklist item URL for the study
		'''
		return posixpath.join(self.dicomweb_resource_url, 'worklists')

	@property
	def kafka_url(self):
		return posixpath.join(self.resource_url, 'kafka')

	@property
	def patient(self):
		return self._objectdata.get('ParentPatient')

	@property
	def parent(self):
		'''	Retrieve the parent patient for the study
		'''
		if getattr(self, '_parent', None) is None:
			self._parent = self.pacs.get_patient(self.patient)

			# Propagate cache lookup settings of current instance
			if getattr(self, 'resource_cache_lookup', None) is not None:
				setattr(self._parent, 'resource_cache_lookup', self.resource_cache_lookup)

		return self._parent
		
	@parent.setter
	def parent(self, patient_model):
		'''	Sets the parent patient for the study
		'''
		if not isinstance(patient_model, ImagingPatient):
			raise ValueError("Input must be a instance of a patient")

		setattr(self, '_parent', patient_model)

	def parent_from_json(self, jdata, **kwargs):
		''' Initialize patient model from the provided JSON data.
		'''
		return self.server._init_dataclass_from_json(ImagingPatient, jdata, pacs=self.pacs, **kwargs)

	@property
	def model_patient(self):
		return self.parent

	@property
	def study_uid(self):
		return self.dicomdata.get(DCMHEADER_STUDY_INSTANCE_UID)

	@property
	def study_id(self):
		return self.dicomdata.get(DCMHEADER_STUDY_ID)

	@property
	def patient_name(self):
		return self.patientdata.get('PatientName')

	@property
	def patientid(self):
		return self.patientdata.get('PatientID')

	@property
	def description(self):
		return self.dicomdata.get('StudyDescription')

	@property
	def accession_number(self):
		return self.dicomdata.get('AccessionNumber')

	@property
	def study_datestr(self):
		return self.dicomdata.get(DCMHEADER_STUDY_DATE)

	@property
	def study_date(self):
		'''	Date that the study was acquired. (Parsed from study_datestr.)
		'''
		return self._sts.date_value

	@property
	def study_timestr(self):
		return self.dicomdata.get(DCMHEADER_STUDY_TIME)

	@property
	def study_time(self):
		return self._sts.time_value

	@property
	def ts(self):
		'''	Date/time that the study was acquired (created from study_date and study_time properties).
			Returns None is there is no study date value. Study time is used if available, with midnight
			being used if it is not.
		'''
		try:
			if getattr(self, '_ts', None) is None and self.study_date:
				self._ts = self._sts.ts
		
		except Exception as err:
			logger.error('Invalid study timestamp. Error: "%s"' % err)
			self._ts = None

		return getattr(self, '_ts', None)

	@property
	def modalities(self):
		'''	List of modalities in the study
		'''
		return self.dicomdata.get(DCMHEADER_MODALITIES_IN_STUDY)

	@property
	def physician(self):
		return self.dicomdata.get('RequestingPhysician')

	@property
	def series(self):
		return self._objectdata.get('Series')

	def fetch_series(self, **kwargs):
		'''	Retrieve details of the series associated with the study

			@returns collection of DICOM series instances that associated with the current study
		'''
		verify = kwargs.get('verify', None)
		if verify is None:
			verify = self.server.verify

		# Retrieve series details
		r = self.pacs._request_get(
			self.pacs.orthanc_apiurl(posixpath.join(self.resource_url, 'series')),
			lambda r: request_client_error(
				'Unable to retrieve details for study %s series on server %s. Status code: %s.' % (
					self.pk, self.pacs.server_label, r.status_code),
				r),
			headers=self.pacs.orthanc_request_headers(headers=kwargs.get('headers')), verify=verify)

		# Parse response and return collection
		return self.server._init_dataclass(ImagingSeriesCollection, r, pacs=self.pacs, study=self, **kwargs)

	def series_from_json(self, jdata, **kwargs):
		'''	Initialize series collection from JSON structure
		'''
		return self.server._init_dataclass_from_json(
			ImagingSeriesCollection, jdata, pacs=self.pacs, study=self, **kwargs)

	@property
	def series_collection(self):
		'''	Series instances associated with the study
		'''
		
		if getattr(self, '_series', None) is None:
			setattr(self, '_series', self.fetch_series())

		return self._series
	
	@series_collection.setter
	def series_collection(self, series_collection):
		'''	Series instances associated with the study
		'''
		if not isinstance(series_collection, ImagingSeriesCollection):
			raise ValueError("Series must be a instance of the Series Collection")

		setattr(self, '_series', series_collection)

	def fetch_sr(self, **kwargs):
		'''	Fetch the DICOM-SR instances that are associated with the study
		'''
		return self.pacs.query_sr({ DCMHEADER_STUDY_INSTANCE_UID: self.study_uid, }, 
			rapid_lookup=getattr(self, 'resource_cache_lookup', None), pacs=self.pacs, study=self, **kwargs)

	def sr_from_json(self, jdata, **kwargs):
		''' Initialize segmentation collection from JSON structure
		'''
		from .sr import DcmSRSeriesCollection
		return self.server._init_dataclass_from_json(
			DcmSRSeriesCollection, jdata, pacs=self.pacs, study=self, **kwargs)

	@property
	def sr_collection(self):
		'''	DICOM-SR instances associated with the study
		'''
		if getattr(self, '_sr', None) is None:
			setattr(self, '_sr', self.fetch_sr())

		return self._sr

	@sr_collection.setter
	def sr_collection(self, sr_collection):
		'''	Set SR collection instances which belong to the study
		'''
		from .sr import DcmSRSeriesCollection
		if not isinstance(sr_collection, DcmSRSeriesCollection):
			raise ValueError('Input must be an instance of a DcmSRSeriesCollection')

		setattr(self, '_sr', sr_collection)

	def fetch_seg(self, **kwargs):
		'''	Fetch the DICOM-SEG instances that are associated with the study
		'''
		return self.pacs.query_seg({ DCMHEADER_STUDY_INSTANCE_UID: self.study_uid },
			rapid_lookup=getattr(self, 'resource_cache_lookup', None), pacs=self.pacs, study=self, **kwargs)

	def seg_from_json(self, jdata, **kwargs):
		'''	Initialize segmentation collection from JSON structure
		'''
		from .seg import DcmSegmentationSeriesCollection
		return self.server._init_dataclass_from_json(
			DcmSegmentationSeriesCollection, jdata, pacs=self.pacs, study=self, **kwargs)

	@property
	def seg_collection(self):
		'''	DICOM-SEG instances associated with the study
		'''
		if getattr(self, '_seg', None) is None:
			setattr(self, '_seg', self.fetch_seg())

		return self._seg

	@seg_collection.setter
	def seg_collection(self, seg_collection):
		'''	Set segmentation instances which belong to the study
		'''
		from .seg import DcmSegmentationSeriesCollection
		if not isinstance(seg_collection, DcmSegmentationSeriesCollection):
			raise ValueError('Input must be an instance of a DcmSegmentationSeriesCollection')

		setattr(self, '_seg', seg_collection)

	def fetch_m3d(self, **kwargs):
		'''	Fetch the M3D instances associated with the study	
		'''
		return self.pacs.query_m3d({ DCMHEADER_STUDY_INSTANCE_UID: self.study_uid },
			rapid_lookup=getattr(self, 'resource_cache_lookup', None), pacs=self.pacs, study=self, **kwargs)

	def m3d_from_json(self, jdata, **kwargs):
		'''	Initialize M3D collection from JSON structure
		'''
		from .m3d import DcmM3DSeriesCollection
		return self.server._init_dataclass_from_json(
			DcmM3DSeriesCollection, jdata, pacs=self.pacs, study=self, **kwargs)

	@property
	def m3d_collection(self):
		'''	M3D series associated with the study	
		'''
		if getattr(self, '_m3d', None) is None:
			setattr(self, '_m3d', self.fetch_m3d())

		return self._m3d

	@m3d_collection.setter
	def m3d_collection(self, m3d_collection):
		'''	Set M3D series which belong to the study
		'''
		from .m3d import DcmM3DSeriesCollection
		if not isinstance(m3d_collection, DcmM3DSeriesCollection):
			raise ValueError('Input must be an instance of a DcmM3DSeriesCollection')

		setattr(self, '_m3d', m3d_collection)

	def fetch_doc(self, **kwargs):
		return self.pacs.query_doc({ DCMHEADER_STUDY_INSTANCE_UID: self.study_uid },
			rapid_lookup=getattr(self, 'resource_cache_lookup', None), pacs=self.pacs, study=self, **kwargs)

	def doc_from_json(self, jdata, **kwargs):
		'''	Initialize DOC collection from JSON structure
		'''
		from .media import DcmEncapsulatedDocumentSeriesCollection
		return self.server._init_dataclass_from_json(
			DcmEncapsulatedDocumentSeriesCollection, jdata, pacs=self.pacs, study=self, **kwargs)

	@property
	def doc_collection(self):
		'''	Encapsulated document series associated with the study
		'''
		if getattr(self, '_doc', None) is None:
			setattr(self, '_doc', self.fetch_doc())

		return self._doc

	@doc_collection.setter
	def doc_collection(self, doc_collection):
		'''	Set DOC series which belong to the study
		'''
		from .media import DcmEncapsulatedDocumentSeriesCollection
		if not isinstance(doc_collection, DcmEncapsulatedDocumentSeriesCollection):
			raise ValueError('Input must be an instance of DcmEncapsulatedDocumentSeriesCollection')

		setattr(self, '_doc', DcmEncapsulatedDocumentSeriesCollection)

	@property
	def reviewer_worklist_item_class(self):
		'''	Model collection class for worklist items
		'''
		from .worklists import ReviewerStudyWorklistItemCollection
		return ReviewerStudyWorklistItemCollection

	def fetch_reviewer_worklist(self, **kwargs):
		'''	Retrieve worklist items for the study

			@returns collection of worklist items
		'''
		return self.reviewer_worklist_item_class.fetch(parent=self, **kwargs)

	def reviewer_worklist_from_json(self, jdata, **kwargs):
		'''	Initialize reviewer worklist from JSON
		'''
		self.server._init_dataclass_from_json(
			self.reviewer_worklist_item_class, jdata, pacs=self.pacs, parent=self, study=self, **kwargs)

	@property
	def reviewer_worklist_collection(self):
		'''	Reviewer work list items associated with the study
		'''
		if getattr(self, '_reviewer_worklist', None) is None:
			setattr(self, '_reviewer_worklist', self.fetch_reviewer_worklist())

		return self._reviewer_worklist

	@reviewer_worklist_collection.setter
	def reviewer_worklist_collection(self, worklist_items_collection):
		'''	Set reviewer worklist for the study
		'''
		if not isinstance(worklist_items_collection, self.reviewer_worklist_item_class):
			raise ValueError('Input must be an instance of a reviewer worklist collection')

		setattr(self, '_reviewer_worklist', worklist_items_collection)

	def create_reviewer_worklist_item(self, group: SonadorGroup, user: SonadorUser, state, complete=None, meta=None, 
			worklist=None, **kwargs):
		'''	Create a reviewer worklist item for the provided group and user
		'''
		worklist = worklist or {}

		if not isinstance(group, SonadorGroup):
			raise ValueError('Input must be a Sonador group instance')
		if not isinstance(user, SonadorUser):
			raise ValueError('Input must be a Sonador user instance')
		if not isinstance(worklist, dict):
			raise ValueError('Invalid worklist item data')

		# Worklist request structure		
		worklist.update({
			'Group': group.pk, 'User': user.pk, 'State': state,
		})
		if meta:
			worklist['Meta'] =  meta

		if complete:
			worklist['Complete'] = datetime.datetime.now().isoformat()

		return self.reviewer_worklist_item_class.create(self, worklist, **kwargs)

	def get_reviewer_worklist_item(self, cid, *args, **kwargs):
		'''	Retrieve a worklist item by UID

			@input cid (str): Worklist resource ID (worklist.pk) of the worklist item to be retrieved.

			@returns worklist item instance
		'''
		return self.reviewer_worklist_item_class.fetch_modelinstance(self, cid, *args, **kwargs)
	
	@property
	def comments_url(self):
		'''	URL for comments associated with the imaging study
		'''
		return posixpath.join(self.resource_url, 'comments')

	@property
	def dicomweb_comments_url(self):
		'''	DICOMweb comments URL for the study
		'''
		return posixpath.join(self.dicomweb_resource_url, 'comments')
	
	@property
	def comments_modelcollection_class(self):
		'''	Model collection class that should be used to initialize comments
		'''
		from .ext import ResourceCommentCollection
		return ResourceCommentCollection

	def fetch_comments(self, **kwargs):
		'''	Retrieve comments associated with the study

			@returns collection of comments
		'''
		return self.comments_modelcollection_class.fetch(parent=self, **kwargs)

	def comments_from_json(self, jdata, **kwargs):
		'''	Initialize comments from JSON

			@returns collection of comments
		'''
		return self.server._init_dataclass_from_json(
			self.comments_modelcollection_class, jdata, pacs=self.pacs, parent=self, study=self, **kwargs)

	@property
	def comments_collection(self):
		'''	Comments associated with the study
		'''		
		if getattr(self, '_comments', None) is None:
			setattr(self, '_comments', self.fetch_comments())

		return self._comments

	@comments_collection.setter
	def comments_collection(self, comments_collection):
		'''	Set comments collection property for the study
		'''
		if not isinstance(comments_collection, self.comments_modelcollection_class):
			raise ValueError('Input must be an instance of a comments collection')

		setattr(self, '_comments', comments_collection)

	def create_comment(self, text, data=None, **kwargs):
		'''	Create a comment for the study

			@input text (str): Text for the comment
		'''
		data = data or {}
		data.update({ 'Text': text })

		return self.comments_modelcollection_class.create(self, data, **kwargs)

	def get_comment(self, cid, *args, **kwargs):
		'''	Retrieve a comment instance

			@input cid (str): Orthanc resource ID (resource.pk) of the comment to be retrieved.

			@returns comment instance
		'''
		return self.comments_modelcollection_class.fetch_modelinstance(self, cid, *args, **kwargs)

	def _populate_subcollections(self, 
			populate_sr=True, populate_seg=True, populate_m3d=True, populate_doc=True):
		'''	Populate study SR and SEG collections from the series collection
		'''
		if populate_sr:
			self.sr_collection = self.sr_from_json(
				[sx._objectdata for sx in self.series_collection if sx.modality == DCM_MODALITY_SR])

		if populate_seg:
			self.seg_collection = self.seg_from_json(
				[sx._objectdata for sx in self.series_collection if sx.modality == DCM_MODALITY_SEG])

		if populate_m3d:
			self.m3d_collection = self.m3d_from_json(
				[sx._objectdata for sx in self.series_collection if sx.modality == DCMEDIA_M3D_MODALITY])

		if populate_doc:
			self.doc_collection = self.doc_from_json(
				[sx._objectdata for sx in self.series_collection if sx.modality == DCM_MODALITY_DOC])
	
	def merge_resources(self, resources: list, asynchronous=False, keep_source=False, permissive=False, 
			priority=0, merge=None, verify=None, headers=None):
		'''	Merge the specified resources into a the current study. This is done by updating 
			the following DICOM tags of the provided resources: StudyInstanceUID (0x0020, 0x000d), 
			SeriesInstanceUID (0x0020, 0x000e), and SOPInstanceUID (0x0008, 0x0018). 
			Additionally, all the DICOM tags that are part of the “Patient Module Attributes” and 
			the “General Study Module Attributes” (as specified by the DICOM 2011 standard in 
			Tables C.7-1 and C.7-3), are modified to match the target study. 
			(Refer to https://book.orthanc-server.com/users/anonymization.html#split-merge-of-dicom-studies)

			 @input resources (list): The UIDS of DICOM resources (studies, series, and/or instances) 
			 	to be merged into the study of interest.
			 @input asynchronous (boolean, default=False): If true, run the job in asynchronous mode.
			 	When run asynchronously, the REST API call will immediately return, reporting the identifier 
			 	of a job. The job instance can be used to retrieve the status of the job.
			 @input keep_source (bool, default=False): If set to true, instructs Orthanc to keep 
			 	a copy of the original series in the source study. By default, the original 
			 	resources are deleted from Orthanc.
			 @input permissive (permissive, default=False): If true, ignore errors during the individual 
			 	steps of the job.
			 @input priority (int, default=0): The priority of the job. The lower the number, 
			 	the higher the priority.
			 @input merge (iterable, default=None): iterable ot tags to be removed outside of those
			 	specified in the standard.

			 @returns requests.Response
		'''
		merge = merge or {}

		# Create request structure
		merge.update({ 
			'Asynchronous': asynchronous, 
			'Permissive': permissive,
			'KeepSource': keep_source,
			'Priority': priority,
			'Resources': resources,
			'Synchronous': False
		})

		return self._merge_split_request(merge, 'merge', asynchronous, headers, verify)
	
	def split_study(self, resources: list, asynchronous: bool=False, keep_source: bool=False, permissive: bool=True, priority: int=0, 
			remove: list=None, replace: dict=None, split: dict=None, verify=None, headers=None, **kwargs):
		'''	Remove the DICOM series specified in resources from the current study and placing them in a new study.
			The new study is created by setting the StudyInstanceUID of the specified series to a new value.
			(Refer to https://book.orthanc-server.com/users/anonymization.html#split-merge-of-dicom-studies.)

			 @input resources (list): the list of series UIDs to be split from the current study. (Must
			 	be part of the current study.)
			 @input asynchronous (boolean, default=False): If true, run the job in asynchronous mode. When asynchronously,
			 	REST API call will immediately return, reporting the identifier of a job. The job instance can be used
			 	to retrieve the status of the job.
			 @input keep_source (bool, default=False): If set to true, instructs Orthanc to keep a copy of 
			 	the original resources in their source study. By default, the original resources are deleted from Orthanc.
			 @input permissive (permissive, default=False): If true, ignore errors during the individual steps of the job.
			 @input priority (iterable, default=None): In asynchronous mode, the priority of the job. 
			 	The lower the value, the higher the priority.
			 @input remove (list, default=None): List of tags that must be removed in the new study 
			 	(from the same modules as in the Replace option).
			 @input replace (dict, default=None): Associative array to change the value of some 
			 	DICOM tags in the new study. The tags must be part of the "Patient Module Attributes" 
			 	or the "General Study Module Attributes",  as specified by the DICOM 2011 standard in Tables 
			 	C.7-1 and C.7-3.

			 @returns requests.Response
		'''
		
		# Check resources are available
		for series in resources:
			if series not in self.series:
				raise ValueError('Invalid series UID. %s not in study.' % (series))

		# Request options
		split = split or {}

		# Create request structure adding the series retrieved from the search
		split.update({ 
			'Asynchronous': asynchronous, 
			'Permissive': permissive,
			'KeepSource': keep_source,
			'Priority': priority,
			'Series': resources,
		})

		# DICOM tags to be replaced
		if replace:
			split.update({'Replace': replace})

		# DICOM tags to be removed 
		if remove:
			split.update({'Remove': remove})

		return self._merge_split_request(split, 'split', asynchronous, headers, verify)
		
	def _merge_split_request(self, data_send: dict, endpoint: str, asynchronous=False, headers=None, verify=None, **kwargs):
		'''	Function that is responsible for communication with the server and executing the request
		'''
		# Execute operation
		logger.debug('Structure of merge/split request:\n%s' % json.dumps(data_send))
		r = self.pacs._request_post(
			self.pacs.orthanc_apiurl(posixpath.join(self.resource_url, endpoint)),
			lambda r: request_client_error(
				'Unable to merge DICOM resource tags for %s on server %s. Status code: %s.'
					% (self.resource_url, self.pacs.server_label, r.status_code),
				r),
			json=data_send, headers=self.pacs.orthanc_request_headers(headers=headers), verify=verify)

		logger.debug('Response from PACS imaging server:\n%s' % r.content)

		# Retrieve job instance 
		if asynchronous:
			response_json = server_controloperation_json_response(r)
			from .jobs import OrthancJob
			return self.pacs.get_imaging_resource(response_json['ID'], OrthancJob, headers=headers, **kwargs)
		
		# Retrieve new imaging study
		else:
			response_json = server_controloperation_json_response(r)
			return self.pacs.get_imaging_resource(response_json['TargetStudy'], ImagingStudy, headers=headers, **kwargs)


class ImagingStudyCollection(ImagingResourceBaseCollection):
	'''	Collection of imaging studies
	'''
	model = ImagingStudy

	def __init__(self, *args, **kwargs):
		self.parent = kwargs.pop('patient', None)
		super().__init__(*args, **kwargs)

	def _init_empty_collection(self, *args, **kwargs):
		'''	Initialize empty collection: propagates patient to new collection instance.
		'''
		if kwargs.get('patient') is None and self.parent:
			kwargs['patient'] = self.parent

		return super()._init_empty_collection(*args, **kwargs)

	def _init_collection_models(self, **kwargs):
		if self.parent:
			kwargs['patient'] = self.parent

		return super()._init_collection_models(**kwargs)

	def bulkpopulate_related(self, *args, parent_patient=True, child_series=True, **kwargs):
		'''	Populate models related to collection instances in the most efficient manner possible.

			@input parent_patient (bool, default=True): bulk populate "parent" attribute of collection instances.
			@input child_series (bool, default=True): bulk populate the "series_collection" attribute
				of model instances.
		'''
		# Retrieve patient and sibling series data. Both types of resources can be retrieved in a single request.
		if parent_patient or child_series:

			# Aggregate resouce UIDs
			patient_uids = []
			child_uids = []

			for s in self:
				if parent_patient: patient_uids.append(s.patient)
				if child_series: child_uids.extend(s.series)

			# Retrieve bulk resources and unpack
			bdata = self.pacs.fetch_bulk_content(patient_uids+child_uids, *args, **kwargs)
			bdata_patient = bdata.get(IMAGING_SERVER_RESOURCE_PATIENT)
			bdata_series = bdata.get(IMAGING_SERVER_RESOURCE_SERIES)

			# Unpack data
			for s in self:

				# Patient
				if bdata_patient and bdata_patient.get_modelinstance(s.patient):
					s.parent = s.parent_from_json(
						bdata_patient.get_modelinstance(s.patient)._objectdata)

				# Sibling series, DICOM-SR, and DICOM-SEG attributes
				if bdata_series:
					s.series_collection = s.series_from_json(
						[bdata_series.get_modelinstance(sid)._objectdata for sid in s.series if bdata_series.get_modelinstance(sid)])
					s._populate_subcollections()


IMAGING_SERIES_OUTPUT_COLUMNS = OrderedDict((
		('study', 'Parent Study'),
		('pk', 'Series ID'),
		('modality', 'Modality'),
		('sequence_name', 'Name'),
		('series_number', 'Number'),
		('series_datestr', 'Date'),
		('series_timestr', 'Time'),
		('series_uid', 'UID'),
		('body_part', 'Body Part Examined'),
		('description', 'Description'),
	))


FileDataResponse = namedtuple('FileDataRequest', ('buffer', 'response'))


class ImagingSeriesCoreResource(ImagingResourceMixin, ImagingResourceParentMixin, ImagingServerChildBaseObject):
	'''	Imaging series: set of grouped images
	'''
	pk_attr = 'ID'
	tabulate_output_columns = IMAGING_SERIES_OUTPUT_COLUMNS
	fetch_endpoint = 'series'
	cache_queryurl = '/cache/series'

	def __init__(self, *args, **kwargs):
		self._parent = kwargs.pop('study', None) or kwargs.pop('patient', None)
		super().__init__(*args, **kwargs)

		# Series timestamp (parsed from SeriesDate and SeriesTime)
		self._sts = DicomDatetimePair(self.series_datestr, self.series_timestr, meta=DCMTS_SERIES)

	@property
	@functools.lru_cache()
	def hmeta_key(self):
		'''	Series metadata key: resource, UID header, and DICOM UID

			@returns DicomMeta
		'''
		return DicomMetaKey(IMAGING_SERVER_RESOURCE_SERIES, DCMHEADER_SERIES_INSTANCE_UID, self.series_uid)

	@property
	@functools.lru_cache()
	def hmeta(self):
		'''	Series metadata: description, modality
		'''
		return DicomMeta(self.description, self.modality, meta=self.hmeta_key)

	@property
	def resource_url(self):
		return posixpath.join(self.fetch_endpoint, self.pk)

	@property
	def dicomweb_resource_url(self):
		'''	DICOMweb resource URL for the series
		'''
		return posixpath.join(self.pacs.dicomweb_root, self.fetch_endpoint, self.series_uid)

	@property
	def filearchive_url(self):
		return posixpath.join(self.fetch_endpoint, self.pk, 'archive')

	@property
	def dicomdir_url(self):
		return posixpath.join(self.fetch_endpoint, self.pk, 'media')

	@property
	def cache_indexurl(self):
		return posixpath.join(self.cache_queryurl, self.pk, 'index')

	@property
	def comments_url(self):
		'''	URL for comments associated with the imaging series
		'''
		return posixpath.join(self.resource_url, 'comments')

	@property
	def dicomweb_comments_url(self):
		'''	DICOMweb comments URL for the series
		'''
		return posixpath.join(self.dicomweb_resource_url, 'comments')
	
	@property
	def study(self):
		return self._objectdata.get('ParentStudy')

	@property
	def parent(self):
		'''	Retrieve parent study for the series
		'''
		if getattr(self, '_parent', None) is None:
			self._parent = self.pacs.get_study(self.study)

			# Propagate cache lookup settings of current instance
			if getattr(self, 'resource_cache_lookup', None) is not None:
				setattr(self._parent, 'resource_cache_lookup', self.resource_cache_lookup)

		return self._parent

	@parent.setter
	def parent(self, study_model):
		'''	Sets the parent study for the series
		'''
		if not isinstance(study_model, ImagingStudy):
			raise ValueError('Input must be an instance of a study')

		setattr(self, '_parent', study_model)

	def parent_from_json(self, jdata, **kwargs):
		'''	Initialize study model from the provided JSON data.
		'''
		return self.server._init_dataclass_from_json(ImagingStudy, jdata, pacs=self.pacs, **kwargs)

	@property
	def model_study(self):
		return self.parent

	@property
	def model_patient(self):
		return self.model_study.parent

	@property
	def sequence_name(self):
		return self.dicomdata.get('SequenceName')

	@property
	def modality(self):
		return self.dicomdata.get(DCMHEADER_MODALITY)

	@property
	def description(self):
		return self.dicomdata.get(DCMHEADER_SERIES_DESCRIPTION)

	@property
	def series_number(self):
		return self.dicomdata.get(DCMHEADER_SERIES_NUMBER)

	@property
	def number(self):
		return int(self.series_number) if self.series_number else None

	@property
	def series_datestr(self):
		'''	DICOM string representation of when the series was acquired. (Created from the SeriesDate header.)
		'''
		return self.dicomdata.get(DCMHEADER_SERIES_DATE)

	@property
	def series_date(self):
		'''	Date that the series was acquired. (Parsed from series_datestr.)
		'''
		return self._sts.date_value

	@property
	def series_timestr(self):
		'''	DICOM string representation of when the series was acquired. (Created from the SeriesTime header.)
		'''
		return self.dicomdata.get(DCMHEADER_SERIES_TIME)

	@property
	def series_time(self):
		'''	Time that the series was acquired. (Parsed from series_timestr.)

			@returns datetime.time
		'''
		return self._sts.time_value

	@property
	def ts(self):
		'''	Date/time that the series was acquired. (Created from series_date and series_time properties.)
			Returns None is there is no series date value. Series time is used if available, with midnight
			being used if it is not.
		'''
		try: 
			if getattr(self, '_ts', None) is None and self.series_date:
				self._ts = self._sts.ts

		except Exception as err:
			logger.error('Invalid series timestamp. Error: "%s"' % err)
			self._ts = None

		return getattr(self, '_ts', None)

	@property
	def series_uid(self):
		return self.dicomdata.get(DCMHEADER_SERIES_INSTANCE_UID)

	@property
	def body_part(self):
		return self.dicomdata.get(DCMHEADER_BODY_PART_EXAMINED)

	@property
	def operator_name(self):
		'''	Name of 
		'''
		return self.dicomdata.get(DCMHEADER_OPERATORS_NAME)

	@property
	def operator_name_vr(self):
		if self.operator_name:
			return str2name(self.operator_name)

	@property
	@abstractmethod
	def dcminstance_modelcollection_class(self):
		'''	Model collection class that should be used to initialize instances associated with the series
		'''

	def fetch_dcminstances(self, **kwargs):
		'''	Retrieve details for slices in the series

			@returns collection of DICOM instances
		'''
		return self.dcminstance_modelcollection_class.fetch(
			self.pacs, data_collection_endpoint=posixpath.join(self.resource_url, 'instances'), 
			series=self, **kwargs)

	def dcminstances_from_json(self, jdata, **kwargs):
		'''	Initialize DCM instances from JSON

			@returns collection of DICOM instances
		'''
		return self.server._init_dataclass_from_json(
			self.dcminstance_modelcollection_class, jdata, pacs=self.pacs, series=self, **kwargs)
	
	@property
	def comments_modelcollection_class(self):
		'''	Model collection class that should be used to initialize comments
		'''
		from .ext import ResourceCommentCollection
		return ResourceCommentCollection

	def fetch_comments(self, **kwargs):
		'''	Retrieve comments associated with the series

			@returns collection of comments
		'''
		return self.comments_modelcollection_class.fetch(parent=self, **kwargs)

	def comments_from_json(self, jdata, **kwargs):
		'''	Initialize comments from JSON

			@returns collection of comments
		'''
		return self.server._init_dataclass_from_json(
			self.comments_modelcollection_class, jdata, pacs=self.pacs, parent=self, series=self, **kwargs)

	@property
	def comments_collection(self):
		'''	Comments associated with the series
		'''		
		if getattr(self, '_comments', None) is None:
			setattr(self, '_comments', self.fetch_comments())

		return self._comments

	@comments_collection.setter
	def comments_collection(self, comments_collection):
		'''	Set comments collection property for the series
		'''
		if not isinstance(comments_collection, self.comments_modelcollection_class):
			raise ValueError('Input must be an instance of a comments collection')

		setattr(self, '_comments', comments_collection)

	def create_comment(self, text, data=None, **kwargs):
		'''	Create a comment for the series

			@input text (str): Text for the comment
		'''
		data = data or {}
		data.update({ 'Text': text })

		return self.comments_modelcollection_class.create(self, data, **kwargs)

	def get_comment(self, cid, *args, **kwargs):
		'''	Retrieve a comment instance

			@input cid (str): Orthanc resource ID (resource.pk) of the comment to be retrieved.

			@returns comment instance
		'''
		return self.comments_modelcollection_class.fetch_modelinstance(self, cid, *args, **kwargs)


class ImagingSeriesDcm0Mixin:
	'''	Mixin which provides methods and helpers for working with imaging series
		data associated with the first instance (DCM0).
	'''
	@property
	def dcm0(self):
		'''	Retrieve the first DCM instance associated with the series
		'''
		if getattr(self, '_dcm0', None) is None:
			self._dcm0 = self.instances_collection[0]

		return self._dcm0
		

class ImagingSeries(ImagingSeriesDcm0Mixin, ImagingSeriesCoreResource):
	'''	Imaging series: set of grouped images
	'''
	parent_class = ImagingStudy
	resource_level = IMAGING_SERVER_RESOURCE_SERIES

	@property
	def dcminstance_modelcollection_class(self): return DcmInstanceCollection

	@property
	def kafka_url(self):
		return posixpath.join(self.resource_url, 'kafka')

	@property
	@functools.lru_cache()
	def image_orientation_patient(self):
		'''	Retrieve the image orientation, which specifies the direction cosines of the first row
			and the first column with respect to the patient. Corresponds to the ImageOrientationPatient
			DCM header. If the header is not present, the returned value will be None.

			@returns  pair of tuples: row value for the x, y, z axis followed by column value for the x, y, z
				or None if the ImageOrientationPatient header is not present.
		'''
		return parse_image_orientation(self.dicomdata.get(DCMHEADER_IMAGE_ORIENTATION_PATIENT))

	@property
	def slices(self):
		'''	Retrieve instance UIDs for the series
		'''
		return self._objectdata.get('Instances')

	@property
	def instances(self):
		'''	Retrieve instance UIDs for the series. Added to provide API compatibility
			with SR and M3D series classes. Defers to ImagingSeries.slices
		'''
		return self.slices

	@property
	def instances_collection(self):
		return self.slices_collection

	@instances_collection.setter
	def instances_collection(self, val):
		self.slices_collection = val

	@property
	def slices_collection(self):
		'''	Cached property for retrieving the slice/image instances which belong to the series
		'''
		if getattr(self, '_slices', None) is None:
			setattr(self, '_slices', self.fetch_dcminstances())

		return self._slices

	@slices_collection.setter
	def slices_collection(self, instances_collection):
		'''	Set slice/image instances which belong to the series
		'''
		if not isinstance(instances_collection, self.dcminstance_modelcollection_class):
			raise ValueError("Input must be a instance of a DICOM instances collection")

		setattr(self, '_slices', instances_collection)

	@property
	@functools.lru_cache()
	def shape(self):
		'''	Cached property that retrieves the dimensions of the image volume
		'''
		dcm0 = self.slices_collection[0]
		return ImageStackShape(
			len(self.slices), dcm0.dcmfile(cache=True).Rows, dcm0.dcmfile(cache=True).Columns)

	@property
	@functools.lru_cache()
	def segmentations(self):
		'''	DICOM-SEG segmentations associated with the imaging series. Sorted with the most recent segmentations first.
		'''
		return sorted(
			[dcmseg for dcmseg in self.parent.seg_collection if self.series_uid in dcmseg.series_reference_uids],
			key=lambda dcmseg: dcmseg.ts if dcmseg.ts else datetime.datetime(year=1900, month=1, day=1),
			reverse=True)

	@property
	@functools.lru_cache()
	def annotations(self):
		'''	DICOM-SR annotations associated with the series with most recent reports first.
		'''
		return sorted(
			[dcmsr for dcmsr in self.parent.sr_collection if self.series_uid in dcmsr.series_reference_uids],
			key=lambda dcm: dcm.ts if dcm.ts else datetime.datetime(year=1900, month=1, day=1),
			reverse=True)

	@property
	def m3d_models(self):
		'''	M3D models associated with the series. Sorted with the most recent models first.	
		'''
		return sorted(
			[dcm3d for dcm3d in self.parent.m3d_collection if self.series_uid in dcm3d.series_reference_uids],
			key=lambda dcm: dcm.ts if dcm.ts else datetime.datetime(year=1900, month=1, day=1),
			reverse=True)

	@property
	def patient_position(self):
		return self.dicomdata.get(DCMHEADER_PATIENT_POSITION)


class ImagingSeriesBulkPopulateMixin:
	'''	Mixin class which adds methods to enable efficient population of related models 
		(parent study and patient) on instances within series collections.
	'''
	def bulkpopulate_related(self, *args, parent_study=True, parent_patient=True, sibling_series=True, **kwargs):
		'''	Populate models related to collection instances in the most efficient manner possible.

			@input parent_study (bool, default=True): bulk populate the "parent" attribute of 
				collection instances.
			@input parent_patient (bool, default=True): bulk populate the "model_patient" attribue
				of collection instances. Requires that parent_study option be True.
			@input sibling_series (bool, default=True): bulk populate the "series_collection" attribute
				of the parent study. Requires that parent_study option be True.
		'''
		# For populating patient or sibling data, ensure that the parent study will be available.
		if (parent_patient or sibling_series) and not parent_study:
			raise ValueError('Unable to populate patient or sibling data, options requires parent study.')

		# Retrieve paarent study data. Study instances are required to populate the patient and 
		# sibling series properties, and must be retreived first.
		if parent_study:
			bdata = self.pacs.fetch_bulk_content([sx.study for sx in self], *args, **kwargs)
			bdata_study = bdata.get(IMAGING_SERVER_RESOURCE_STUDY)
			
			# Unpack study data and add to series
			if bdata_study:
				for sx in self:
					s = bdata_study.get_modelinstance(sx.study)
					if s: sx.parent = sx.parent_from_json(s._objectdata)

		# Retrieve patient and sibling series data. Both types of resources can be retrieved
		# in a single request.
		if parent_patient or sibling_series:

			# Aggregate resource UIDs
			patient_uids = []
			sibling_uids = []

			for sx in self:
				if parent_patient: patient_uids.append(sx.parent.patient)
				if sibling_series: sibling_uids.extend(sx.parent.series)

			# Retrieve bulk resources and unpack
			bdata = self.pacs.fetch_bulk_content(patient_uids+sibling_uids, *args, **kwargs)
			bdata_patient = bdata.get(IMAGING_SERVER_RESOURCE_PATIENT)
			bdata_series = bdata.get(IMAGING_SERVER_RESOURCE_SERIES)

			# Unpack data
			for sx in self:

				# Patient
				if bdata_patient and bdata_patient.get_modelinstance(sx.parent.patient):
					sx.parent.parent = sx.parent.parent_from_json(
						bdata_patient.get_modelinstance(sx.parent.patient)._objectdata)

				# Sibling series, DICOM-SR, and DICOM-SEG attributes
				if bdata_series:
					sx.parent.series_collection = sx.parent.series_from_json(
						[bdata_series.get_modelinstance(sid)._objectdata for sid in sx.parent.series if bdata_series.get_modelinstance(sid)])
					sx.parent._populate_subcollections()


class ImagingSeriesCollection(ImagingSeriesBulkPopulateMixin, ImagingResourceBaseCollection):
	''' Collection of imaging series
	'''
	model = ImagingSeries

	def __init__(self, *args, **kwargs):

		# Retrieve parent of the collection, ensure that it is an imaging patient or study
		self.parent = kwargs.pop('study', None) or kwargs.pop('patient', None)
		if self.parent and not isinstance(self.parent, (ImagingPatient, ImagingStudy)):
			raise ValueError('Unable to initialize imaging series, invalid parent type: %s' % type(self.parent))
		
		super().__init__(*args, **kwargs)

	def __init_empty_collection(self, *args, **kwargs):
		'''	Initialize empty collection: propagates parent (either study or patient) to the
			new collection instance.
		'''
		if kwargs.get('patient') is None and isinstance(self.parent, ImagingPatient):
			kwargs['patient'] = self.parent
		elif kwargs.get('study') is None and isinstance(self.parent, ImagingStudy):
			kwargs['study'] = self.parent

		return super()._init_empty_collection(*args, **kwargs)

	def _init_collection_models(self, **kwargs):
		if self.parent:

			# Determine which keyword should be used for the parent. ImagingSeriesCollections
			# can be initialized either from a study or patient.
			if isinstance(self.parent, ImagingPatient):
				kwargs['patient'] = self.parent
			elif isinstance(self.parent, ImagingStudy):
				kwargs['study'] = self.parent

		return super()._init_collection_models(**kwargs)




IMAGING_INSTANCE_OUTPUT_COLUMNS = OrderedDict((
		('series', 'Series'),
		('pk', 'Instance UID'),
		('sop_instance_uid', 'SOP Instance UID'),
	))


class DcmInstanceCoreResource(ImagingResourceCoreMixin, ImagingResourceParentMixin, ImagingServerChildBaseObject):
	'''	Model used for DCM instance data
	'''
	pk_attr = 'ID'
	fetch_endpoint = 'instances'
	tabulate_output_columns = IMAGING_INSTANCE_OUTPUT_COLUMNS

	def __init__(self, *args, **kwargs):
		self._parent = kwargs.pop('series', None)
		super().__init__(*args, **kwargs)

		# Content timestamp (parsed from ContentDate and ContentTime)
		self._sts = DicomDatetimePair(self.content_datestr, self.content_timestr, meta=DCMTS_CONTENT)

	@property
	def series(self):
		return self._objectdata.get('ParentSeries')

	@property
	def series_index(self):
		return self._objectdata.get('IndexInSeries')

	@property
	def sop_class_uid(self):
		return self.dicomdata.get(DCMHEADER_SOP_CLASS_UID)

	@property
	def sop_instance_uid(self):
		return self.dicomdata.get(DCMHEADER_SOP_INSTANCE_UID)

	@property
	def instance_number(self):
		return self.dicomdata.get(DCMHEADER_INSTANCE_NUMBER)

	@property
	def number(self):
		return int(self.instance_number) if self.instance_number is not None else None

	@property
	def description(self):
		return self.dicomdata.get(DCMHEADER_CONTENT_DESCRIPTION)

	@property
	def content_datestr(self):
		return self.dicomdata.get(DCMHEADER_CONTENT_DATE)

	@property
	def content_date(self):
		return self._sts.date_value

	@property
	def content_timestr(self):
		return self.dicomdata.get(DCMHEADER_CONTENT_TIME)

	@property
	def content_time(self):
		return self._sts.time_value

	@property
	def ts(self):
		'''	Date/time that the instance was created. (Created from the content_date and content_time properties.)
			Returns None is there is no study date value. Study time is used if available, with midnight being used
			if it is not.
		'''
		try:
			if getattr(self, '_ts', None) is None and self.content_date:
				self._ts = self._sts.ts

		except Exception as err:
			logger.error('Invalid content timestamp. Error: "%s"' % err)
			self._ts = None

		return getattr(self, '_ts', None)

	@property
	def parent(self):
		'''	Retrieve the parent series for the instance
		'''
		if self._parent is None:
			self._parent = self.pacs.get_series(self.series)

			# Propagate cache lookup settings of current instance
			if getattr(self, 'resource_cache_lookup', None) is not None:
				setattr(self._parent, 'resource_cache_lookup', self.resource_cache_lookup)

		return self._parent

	@property
	def model_series(self):
		return self.parent

	@property
	def model_study(self):
		return self.model_series.parent

	@property
	def model_patient(self):
		return self.model_study.parent

	@property
	def resource_url(self):
		return posixpath.join(self.fetch_endpoint, self.pk)

	@property
	def kafka_url(self):
		return posixpath.join(self.resource_url, 'kafka')

	def fetch_tags(self, *args, **kwargs):
		'''	Retrieve tags for the DICOM instance
		'''
		r = self.pacs._request_get(
			self.pacs.orthanc_apiurl(posixpath.join(self.resource_url, 'simplified-tags'), query_params={ 'expand': True, }),
			lambda r: request_client_error(
				'Unable to retrieve tags for DCM instance %s on server %s. Status code: %s.' % (
					self.pk, self.pacs.server_label, r.status_code), 
				r),
			headers=self.pacs.orthanc_request_headers(), verify=kwargs.get('verify'))

		return server_controloperation_json_response(r)

	@property
	def tags(self):
		'''	Dictionary/JSON of all tags associated with the image
		'''
		if getattr(self, '_tags', None) is None:
			self._tags = self.fetch_tags()
			
		return self._tags

	def fetch_dcmtags(self, *args, **kwargs):
		'''	Retrieve DICOM tags representation for the instance
		'''
		r = self.pacs._request_get(
			self.pacs.orthanc_apiurl(posixpath.join(self.resource_url, 'tags'), query_params={ 'expand': True, }),
			lambda r: request_client_error(
				'Unable to retrieve full DCM tags for DCM instance %s on server %s. Status code: %s.' % (
					self.pk, self.pacs.server_label, r.status_code
				), r),
			headers=self.pacs.orthanc_request_headers(), verify=kwargs.get('verify'))

		return server_controloperation_json_response(r)

	@property
	def dcmtags(self):
		'''	Dictionary/JSON of all DICOM tags including hexadecimal indexes and value type
		'''
		if getattr(self, '_dcmtags', None) is None:
			self._dcmtags = self.fetch_dcmtags()

		return self._dcmtags

	def _get_filedata(self, dcmresource_url, verify=None, headers=None):
		'''	Retrieve DICOM resource data

			@returns io.BytesIO stream
		'''
		# Retrieve file data from Orthanc
		r = self.pacs._request_get(
			self.pacs.orthanc_apiurl(dcmresource_url), 
			lambda r: request_client_error(
				'Unable to retrieve DICOM resource file data for %s (instance %s) on server %s. Status code: %s.'
					% (dcmresource_url, self.pk, self.pacs.server_label, r.status_code),
				r),
			headers=self.pacs.orthanc_request_headers(headers=headers), verify=verify)
			
		# Initialize DICOM instance from request data, attach the raw content of the request
		return FileDataResponse(BytesIO(r.content), r)

	def dcmfile(self, cache=False, **kwargs):
		'''	Retrieve the raw DICOM data for the instance.

			@input cache (bool, default=False): Cache the data locally to speed up access.

			@returns pydicom.dataset.FileDataset
		'''
		# Retrieve cached copy of the file (if available)
		if getattr(self, '_dcmfile', None):
			return self._dcmfile
		
		fbuffer, _ = self._get_filedata(posixpath.join(self.resource_url, 'file'), **kwargs)
		dfile = pydicom.dcmread(fbuffer)
		setattr(dfile, 'raw', fbuffer)

		# Cache (if indicated)
		if cache:
			setattr(self, '_dcmfile', dfile)

		return dfile


class DcmInstance(DcmInstanceCoreResource):
	'''	DCM instance model used for imaging data
	'''
	resource_level = IMAGING_SERVER_RESOURCE_IMAGE

	def imgfile(self, stretch_dynamicrange=True, bitdepth=8, **kwargs):
		'''	Retrieve image file data from Orthanc

			@input stretch_dynamicrange (bool, default=True): When True, signed intger
				data stretched to the full dynamic range of the encoding type will be retrieved.
			@input bitdepth (iint, default=8): Bitdepth of the image

			@returns io.BytesIO
		'''
		# 8 bit stretched image where pixel data is set to [0..255]
		if stretch_dynamicrange and bitdepth == 8:
			dcmresource_url = posixpath.join(self.resource_url, 'preview')

		# 8 bit unsigned image where pixel data is left unmodified.
		# Pixel intensities are cropped to the maximal value encoded by the target image format.
		elif not stretch_dynamicrange and bitdepth == 8:
			dcmresource_url = posixpath.join(self.resource_url, 'image-uint8')

		# 16 bit unsigned image: pixel intensities are coppred to the maximal value encoded by the target image format.
		elif not stretch_dynamicrange and bitdepth == 16:
			dcmresource_url = posixpath.join(self.resource_url, 'image-uint16')

		# 16 bit signed image
		elif stretch_dynamicrange and bitdepth == 16:
			dcmresource_url = posixpath.join(self.resource_url, 'image-int16')

		fbuffer, _ = self._get_filedata(dcmresource_url, **kwargs)

		return fbuffer

	def pngfile(self, cache=False, **kwargs):
		'''	Retrieve a full-resolution PNG grayscale preview of the DCM file. Wraps imgfile

			@returns io.BytesIO
		'''
		if getattr(self, '_pngfile', None):
			return self._pngfile

		pbuffer = self.imgfile(**kwargs)

		# Cache (if indicated)
		if cache:
			setattr(self, '_pngfile', pbuffer)

		return pbuffer

	def jpegfile(self, cache=False, **kwargs):
		'''	Retrieve a full-resolution JPEG grayscale preview of the DCM file. Wraps imgfile.

			@returns io.BytesIO
		'''
		if getattr(self, '_jpegfile', None):
			return self._jpegfile

		headers = kwargs.get('headers') or {}
		headers['Accept'] = 'image/jpeg'

		jbuffer = self.imgfile(headers=headers, **kwargs)

		# Cache (if indicated)
		if cache:
			setattr(self, '_jpegfile', jbuffer)

		return jbuffer

	@property
	@functools.lru_cache()
	def image_position_patient(self):
		'''	Retrieve the image position in the MRI/patient coordinate system.
			Corresponds to the ImagePositionPatient header. The provided
			coordinate is for the upper left hand corner of the image, the center of the first
			voxel transmitted.

			@returns tuple (x, y, z) with image coordinates or None if the 
				header is not present
		'''
		# Retrieve patient position from the DICOM tags
		coord = self.tags.get(DCMHEADER_IMAGE_POSITION_PATIENT)

		# Split into x, y, z coordinates by delimiter. Try '\', ',', before
		# falling back to ' '
		if isinstance(coord, six.string_types):
			coord = coord.split('\\' if '\\' in coord 
				else ',' if ',' in coord
				else ' ')

		# Return values as coordinate or None
		return ImageCoord(*tuple(float(v) for v in coord)) if (coord and len(coord) == 3) \
			else coord

	@property
	@functools.lru_cache()
	def image_orientation_patient(self):
		'''	Retrieve the image orientation, which specifies the direction cosines of the first row
			and the first column with respect to the patient. Corresponds to the ImageOrientationPatient
			DCM header.

			@returns  pair of tuples: row value for the x, y, z axis followed by column value for the x, y, z
		'''
		return parse_image_orientation(
			self.tags.get(DCMHEADER_IMAGE_ORIENTATION_PATIENT))

	@property
	@functools.lru_cache()
	def slice_location(self):
		'''	Retrieve the slice location within the image volume. The location is taken from the SliceLocation header
			and will return None if the header is not present.
		@returns float or None
		'''
		zval = self.tags.get(DCMHEADER_SLICE_LOCATION)		
		return float(zval) if isinstance(zval, six.string_types) else zval

	@property
	@functools.lru_cache()
	def slice_thickness(self):
		'''	Retrieve the slice thinkess. The thickness is taken from the SliceThickness header and will return
			None if the header is not present.

			@returns float or None
		'''
		thickness = self.tags.get(DCMHEADER_SLICE_THICKNESS)
		return float(thickness) if isinstance(thickness, six.string_types) else thickness

	@property
	@functools.lru_cache()
	def pixel_spacing(self):
		'''	Retrieve the pixel spacing for the slice. The spacing components are retrieved from the PixelSpacing
			header and will return None if the header is not present.

			@returns tuple or None
		'''
		spacing = self.tags.get(DCMHEADER_PIXEL_SPACING)

		# Split the spacing into x and y components
		if isinstance(spacing, six.string_types):
			spacing = spacing.split('\\' if '\\' in spacing \
				else ',' if ',' in spacing 
				else '')

			# Ensure that all expected values are present
			if not len(spacing) == 2:
				raise ValueError('Invalid pixel spacing, unable to find x/y spacing components')

			# Unpack components of the tuple in x, y, and thickness
			spacing = ImageSpacing(float(spacing[0]), float(spacing[1]), self.slice_thickness)

		return spacing

	@property
	@functools.lru_cache()
	def plane_type(self):
		return int(self.tags.get('PlaneType')) if self.tags.get('PlaneType') \
			else self.tags.get('PlaneType')


class DcmInstanceCoreCollection(ImagingServerChildCollection):
	'''	Mixin object that provides convenience methods for working with collections of
		DICOM instances (imaging and DICOM-SR objects)
	'''
	def __init__(self, *args, **kwargs):
		self.parent = kwargs.pop('series', None)
		super().__init__(*args, **kwargs)

	def _init_empty_collection(self, *args, **kwargs):
		'''	Initialize empty collection: propagates series to the new collection instance.
		'''
		if kwargs.get('series') is None and self.parent:
			kwargs['series'] = self.parent

		return super()._init_empty_collection(*args, **kwargs)

	def _init_collection_models(self, **kwargs):
		if self.parent:
			kwargs['series'] = self.parent

		# Return a sorted copy of the collection so that the instances are ordered
		# by their index
		return sorted(
			super()._init_collection_models(**kwargs),
			key=lambda i: i.series_index or 0)

	@property
	def dcmfiles(self):
		''' Iterable of all DICOM files instances in the collection. Implemented as 
			a generator which retrieves the DICOM file from the instances. A reference 
			to the DICOM file is stored on the individual slice.
		'''
		for dcm in self:
			yield dcm.dcmfile(cache=True)


class DcmInstanceCollection(DcmInstanceCoreCollection):
	'''	Collection of image instances
	'''
	model = DcmInstance
