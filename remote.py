import six, posixpath, requests, json, csv, collections, logging
from collections import OrderedDict
from urllib.parse import urlencode

from tabulate import tabulate

from client import apisettings as gcapicodes
from client import auth as guru_auth
from client.utils.urls import validate_url, build_url, resource_fullurl
from client.utils.microservices import JsonBaseObject, JsonObjectCollection, GuruRemotePaginationMixin, RemotePage, \
	server_controloperation_json_response
from client.utils.object import pick

from .helpers import request_client_error
from .serialization import json_datetime_parser, \
	OUTPUT_TYPE_TABULATE, OUTPUT_TYPE_CSV, OUTPUT_TYPE_SUPPORTED

logger = logging.getLogger(__name__)


class SonadorBaseObject(JsonBaseObject):
	'''	Python representation of a Sonador object
	'''
	verify_ssl = False
	pk_attr = 'token'

	def __init__(self, server, *args, **kwargs):
		self.server = server
		self.collection = kwargs.pop('collection', None)
		super().__init__(*args, **kwargs)

	@property
	def url(self):
		return self._objectdata.get(gcapicodes.UPDATE_URL)

	@property
	def pk(self):
		if self._objectdata.get(self.pk_attr):
			return self._objectdata.get(self.pk_attr)

		return self._objectdata.get('uid')

	def delete(self, verify=None, **kwargs):
		if verify is None:
			verify = self.server.verify

		r = requests.delete(self.server.sonador_apiurl(self.url, method='DELETE'),
			verify=verify, headers=self.sonador.sonador_request_headers(), **kwargs)

		if not r.ok:
			request_client_error('Unable to delete Sonador object %s, a server error occurred'
				% self.url, r)

		return r


class SonadorObjectCollection(GuruRemotePaginationMixin, JsonObjectCollection):
	'''	Collection of Sonador objects
	'''
	model = SonadorBaseObject

	def __init__(self, server, *args, **kwargs):
		self.server = server
		self.remote_schema = kwargs.pop('remote_schema', None)
		super().__init__(*args, **kwargs)

	def _init_collection_models(self, **kwargs):
		# Add reference to the collection
		kwargs['collection'] = self

		return map(lambda ojson: self.model(self.server, ojson, **kwargs), self._objectdata)

	def append(self, value):
		'''	Append a model to the end of the collection
		'''
		if not isinstance(value, self.model):
			raise TypeError('Unable to add %s to collection, unsupported type %s' % (value, type(value)))

		return self.models.append(value)

	def extend(self, other):
		'''	Add the model instances from other to the existing instance

			@input other (iterable): Iterable of model instances to be added to the collection
		'''
		return self.models.extend(m for m in other)

	def __iadd__(self, other):
		'''	Add the members of the other collection to the the existing instance. Delegates to `extend`
		'''
		# Ensure that the two collections are of the same type
		if not isinstance(other, type(self)):
			raise TypeError('Unsupport += operation of type(s): %s and %s. Collections must be of the same type.'
				% (type(self), type(other)))

		# Extend the current collection
		self.extend(other)
		return self


def fetch_sonador_dataobject_schema(sonador_server, datamodel_class, verify=False,
		data_collection_endpoint=None):
	'''	Retrieve a Sonador data schema.

		@returns dict
	'''
	data_collection_endpoint = data_collection_endpoint or datamodel_class.fetch_endpoint

	rs = requests.options(sonador_server.sonador_apiurl(data_collection_endpoint, method='OPTIONS'),
			verify=verify, headers=sonador_server.sonador_request_headers())

	if not rs.ok:
		request_client_error('Unable to retrieve requested schema from Sonador due to a server error.', rs)

	return rs.json()



def fetch_sonador_data_collection(sonador_server, datacollection_class,
		datasources_collection=None, page=1, items=100, verify=False, filters=None,
		fetch_remote_schema=False, data_collection_endpoint=None, **kwargs):
	'''	Fetch property or community data sources from Sonador
	'''
	data_collection_endpoint = data_collection_endpoint or datacollection_class.model.fetch_endpoint

	# URL encode filter string if provided as dict
	if isinstance(filters, dict):
		filters = urlencode(filters)

	r = requests.get(
		sonador_server.sonador_apiurl('%s?page=%d&items=%d%s' % (data_collection_endpoint, page, items, '&%s' % filters if filters else '')),
		verify=verify, headers=sonador_server.sonador_request_headers())

	if not r.ok:
		request_client_error('Unable to retrieve data sources from Sonador due to a server error.', r)

	# Parse request data to object instances
	rsources = RemotePage(datacollection_class(sonador_server,
		server_controloperation_json_response(r,
			json_loads=lambda rd, mkwargs: json_datetime_parser(rd.json(**mkwargs)), object_pairs_hook=OrderedDict),
		**kwargs))

	# If existing collection provided, add recently retrieved data sources to collection
	if datasources_collection:
		datasources_collection += rsources.collection
	else: datasources_collection = rsources.collection

	# Ensure that all data sources have been retrieved
	if rsources.has_next():
		fetch_sonador_data_collection(sonador_server, datacollection_class, datasources_collection=datasources_collection,
			page=page + 1, items=items, verify=verify, filters=filters, **kwargs)

	# Fetch the remote schema for the data source
	if fetch_remote_schema:
		datasources_collection.remote_schema = fetch_sonador_dataobject_schema(
			sonador_server, datacollection_class.model, verify=verify, data_collection_endpoint=data_collection_endpoint)

	return datasources_collection


def fetch_sonador_dataobject(sonador_server, datamodel_class, objectid, verify=False, dataobject_endpoint=None, **kwargs):
	'''	Retrieve the details for a single data object from Sonador
	'''
	dataobject_endpoint = dataobject_endpoint or posixpath.join(datamodel_class.fetch_endpoint, objectid)

	r = requests.get(
		sonador_server.sonador_apiurl(dataobject_endpoint),
		verify=verify, headers=sonador_server.sonador_request_headers())

	if not r.ok:
		if r.status_code == 404:
			request_client_error('%s (%s) does not exist.' % (objectid, datamodel_class), r)
		else:
			request_client_error('Unable to retrieve %s from server due to a server error.' % datamodel_class, r)

	# Parse request data to object instance
	return datamodel_class(
		sonador_server, 
		server_controloperation_json_response(r,
			json_loads=lambda rd, mkwargs: json_datetime_parser(rd.json(**mkwargs)), object_pairs_hook=OrderedDict), **kwargs)


def object2tabulate(object_data, tabulate_output_columns):
	'''	Convert a Sonador data object to the tabulated output format

		@input object_data (dict): Dictionary representation of object data from Sonador
		@input tabulate_output_columns (OrderDict): Ordered dictionary of output columns

		@returns tuple: data source listing in tabulated format
	'''
	return tuple(getattr(object_data, k, None) for k in six.iterkeys(tabulate_output_columns))


def sonador_dataobject_schema_display(sonador_server, output_dest, datamodel_class, 
		verify=False, data_collection_endpoint=None):
	'''	Output data schema for the provided data model
	'''
	datamodel_class.schema = fetch_sonador_dataobject_schema(sonador_server, datamodel_class, verify=verify,
		data_collection_endpoint=data_collection_endpoint)

	# Output untabulated values
	if hasattr(datamodel_class, 'schema'):
		logger.info('"%s" Data Schema' % datamodel_class.schema.get('model', ''))
		output_dest.write(
			'\n%s' % json.dumps(getattr(datamodel_class, 'schema', {}), indent=2, separators=(',', ': ')))
		output_dest.write('\n\n')


def sonador_datacollection_serialize(datacollection, output_dest, output_type=OUTPUT_TYPE_TABULATE):
	'''	Write collection data to the provided output in the desired output type

		@input datacollection (collection of data objects): Collection to be serialized to the provided output.
		@input output_dest: Output destination to which the data should be written
		@input output_type (str, default='tabulate'): Format which should be used for the output
	'''
	# Convert data source (JSON) to desired output format and write to the specified destination
	if output_type == OUTPUT_TYPE_TABULATE:
		tabulate_output_columns = datacollection.model.tabulate_output_columns
		output_dest.write(
			tabulate((object2tabulate(s, tabulate_output_columns) for s in datacollection),
				headers=tuple(six.itervalues(tabulate_output_columns))))
	
	elif output_type == OUTPUT_TYPE_CSV:

		# Ensure schema for the object is present
		if not datacollection.remote_schema:
			raise ValueError('Unable to create CSV file, collection did not include a remote schema')

		w = csv.DictWriter(output_dest, tuple(datacollection.remote_schema.get('fields', [])))
		w.writeheader()

		# Output data
		for d in datacollection:
			w.writerow(pick(d._objectdata, tuple(datacollection.remote_schema.get('fields', []))))


def sonador_datacollection_list(sonador_server, output_dest, datamodel_collection_class,
		output_type=OUTPUT_TYPE_TABULATE, verify=False, filters=None, data_collection_endpoint=None, **kwargs):
	'''	Retrieve Sonador data collection list to the provided output dest
	'''
	if not output_type in six.iterkeys(OUTPUT_TYPE_SUPPORTED):
		raise ValueError('Unsupported output type: %s. Supported: %s' % (output_type, ', '.join(six.iterkeys(OUTPUT_TYPE_SUPPORTED))))

	# Retrieve data collection (if not provided)
	datacollection = fetch_sonador_data_collection(sonador_server, datamodel_collection_class,
		verify=verify, filters=filters, fetch_remote_schema=True if output_type==OUTPUT_TYPE_CSV else False, 
		data_collection_endpoint=data_collection_endpoint, **kwargs)

	# Write data results to provided output destination
	sonador_datacollection_serialize(datacollection, output_dest, output_type=output_type)

	return datacollection


def sonador_dataobject_serialize(dataobject, output_dest, include_extended_attrs=True):
	'''	Write data object data to the provided output destination

		@input dataobject (model instance): Object for which the data should be output
		@input output_dest: Output destination to whcih the data should be written
	'''
	# Output tabulated values
	for pname, plabel in six.iteritems(dataobject.tabulate_output_columns):
		output_dest.write('%s: %s\n' % (plabel, getattr(dataobject, pname, '')))

	# Output extended attributes, drop attributes in the blacklist
	if include_extended_attrs:
		sonador_dataobject_extendattrs(dataobject, output_dest, 
			tuple(filter(lambda k: not k in getattr(dataobject, 'details_exclude', []),
				set(dataobject._objectdata.keys()).difference(set(dataobject.tabulate_output_columns.keys())))))


def sonador_dataobject_extendattrs(dataobject, output_dest, extended_attrs):
	'''	Write data object extended attributes to the provided output destination.
	'''
	for pname in extended_attrs:

		# Retrieve verbose name from schema (if available)
		if hasattr(dataobject, 'schema') and dataobject.schema.get('schema', {}).get(pname):
			plabel = dataobject.schema.get('schema', {}).get(pname, {}).get('verbose_name')
		else: plabel = pname

		# Determine type of object and retrieve data
		if isinstance(dataobject, dict): pval = dataobject.get(pname, '')
		else: pval = getattr(dataobject, pname, '')
		
		# Write to output destination
		output_dest.write('%s: %s\n' % (plabel, pval))


def sonador_dataobject_details(sonador_server, output_dest, datamodel_class, objectid, verify=False,
		dataobject_endpoint=None, included_extended_attrs=True, **kwargs):
	'''	Retrieve details for Sonador cata object and output to provided destination
	'''
	dobject = fetch_sonador_dataobject(sonador_server, datamodel_class, objectid, 
		verify=verify, dataobject_endpoint=dataobject_endpoint, **kwargs)

	sonador_dataobject_serialize(dobject, output_dest, include_extended_attrs=included_extended_attrs)
	logger.debug('Object Resource Data:\n%s' % json.dumps(dobject._objectdata))
	return dobject
