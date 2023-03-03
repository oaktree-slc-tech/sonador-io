import six, posixpath, requests, json, csv, collections, logging
from collections import OrderedDict
from urllib.parse import urlencode

from abc import ABCMeta, abstractmethod

from tabulate import tabulate

from client import apisettings as gcapicodes
from client import auth as guru_auth
from client.utils.urls import validate_url, build_url, resource_fullurl
from client.utils.microservices import JsonBaseObject, JsonObjectCollection, GuruRemotePaginationMixin, RemotePage as OakTreeRemotePage
from client.utils.object import pick
from client.remote import GuruBaseObject, GuruObjectCollection, fetch_dataobject_schema, fetch_data_collection, fetch_dataobject

from .helpers import request_client_error
from .serialization import json_datetime_parser, \
	OUTPUT_TYPE_TABULATE, OUTPUT_TYPE_CSV, OUTPUT_TYPE_SUPPORTED

logger = logging.getLogger(__name__)


class SonadorBaseObject(GuruBaseObject):
	'''	Python representation of a Sonador object
	'''
	def delete(self, verify=None, **kwargs):
		if verify is None:
			verify = self.server.verify

		r = requests.delete(self.server.sonador_apiurl(self.url, method='DELETE'),
			verify=verify, headers=self.sonador.sonador_request_headers(), **kwargs)

		if not r.ok:
			request_client_error('Unable to delete Sonador object %s, a server error occurred'
				% self.url, r)

		return r


class SonadorObjectCollection(GuruObjectCollection):
	'''	Collection of Sonador objects
	'''
	model = SonadorBaseObject

	def __init__(self, *args, **kwargs):
		self._model_lookup = kwargs.pop('lookup', {})
		if kwargs:
			print('Args: %s. Keyword args: %s.' % (args, kwargs))
		super().__init__(*args, **kwargs)

	def _init_empty_collection(self, *args, **kwargs):
		'''	Initialize an empty collection of the same type. Used by collection methods
			for filtering, slicing, and other operations.
		'''
		return type(self)(self.server, *args, **kwargs)
	
	def _init_collection_modelinstance(self, *args, **kwargs):
		'''	Initialize collection model instances. As part of the init, models
			are indexed to an internal hashmap that enables rapid lookup using
			the collection `get_modelinstance` method.

			@returns initialize model instance
		'''
		model = super()._init_collection_modelinstance(*args, **kwargs)
		self._model_lookup[model.pk] = model
		return model

	def _check_modelinit(self):
		'''	Collection models are lazily initialized on first access. Check to see if 
			a persistent 'models" structure has been created and whether or not
			the lookup is available.
		'''
		# Attempting to retrieve the length of the collection will force it to initialize
		if not self._model_lookup and self._objectdata: len(self)
	
	def get_modelinstance(self, pk):
		'''	Retrieve model instance from the collection using the model's unique identifier (primary key).

			@input pk (str): primary key of the model.

			@returns model instance or None: returns the instance of the model which corresponds 
				to the provided primary key.
		'''
		self._check_modelinit()
		return self._model_lookup.get(pk)

	def extend(self, other):
		'''	Add model instances in other to the existing collection, indexes model instances to lookup.
		'''
		self._check_modelinit()

		# In-case other is an iterator, un-pack to a persistent structue
		for m in other:
			if not m.pk in self._model_lookup:
				self._model_lookup[m.pk] = m

		return super().extend(other)

	def append(self, value):
		'''	Add model instance to the collection, indexes model instances to lookup.
		'''
		self._check_modelinit()

		if not m.pk in self._model_lookup:
			self._model_lookup[m.pk] = m

		return super().append(value)

	def __add__(self, other):
		return self.__iadd__(other)

	def filter(self, fn):
		'''	Return a copy of the collection with models filtered by the provided function.
			(Collection includes new copies of the model instances initialized using model._objectdata.)

			@input fn (callable): function used to filter collection models

			@returns filtered copy of collection
		'''
		return self._init_empty_collection([m._objectdata for m in filter(fn, self)])


def fetch_sonador_dataobject_schema(*args, apiurl_callable='sonador_apiurl', headers_callable='sonador_request_headers', **kwargs):
	'''	Retrieve a Sonador data schema

		@returns dict
	'''
	return fetch_dataobject_schema(*args, apiurl_callable=apiurl_callable, headers_callable=headers_callable, **kwargs)


def fetch_sonador_data_collection(*args, apiurl_callable='sonador_apiurl', headers_callable='sonador_request_headers',
		fetch_schema_callable=fetch_sonador_dataobject_schema, **kwargs):
	'''	Fetch imaging object collection from Sonador

		@returns instance of data collection class
	'''
	return fetch_data_collection(*args, apiurl_callable=apiurl_callable, headers_callable=headers_callable,
		fetch_schema_callable=fetch_schema_callable, **kwargs)


def fetch_sonador_dataobject(*args, apiurl_callable='sonador_apiurl', headers_callable='sonador_request_headers', **kwargs):
	'''	Retrieve the details for a single data object from Sonador
	'''
	return fetch_dataobject(*args, apiurl_callable=apiurl_callable, headers_callable=headers_callable, **kwargs)


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


def sonador_datacollection_serialize(datacollection, output_dest, output_type=OUTPUT_TYPE_TABULATE, **kwargs):
	'''	Write collection data to the provided output in the desired output type

		@input datacollection (collection of data objects): Collection to be serialized to the provided output.
		@input output_dest: Output destination to which the data should be written
		@input output_type (str, default='tabulate'): Format which should be used for the output
	'''

	# Apply sort/ordering
	if getattr(datacollection, 'model', None) and getattr(datacollection.model, 'order_by', None):
		dcollection = sorted(datacollection, 
			key=lambda m: getattr(m, datacollection.model.order_by, None),
			reverse=kwargs.get('reverse', False))
	else: dcollection = datacollection

	# Convert data source (JSON) to desired output format and write to the specified destination
	if output_type == OUTPUT_TYPE_TABULATE:
		tabulate_output_columns = datacollection.model.tabulate_output_columns

		output_dest.write(
			tabulate((object2tabulate(s, tabulate_output_columns) for s in dcollection),
				headers=tuple(six.itervalues(tabulate_output_columns))))
	
	elif output_type == OUTPUT_TYPE_CSV:

		# Ensure schema for the object is present
		if not datacollection.remote_schema:
			raise ValueError('Unable to create CSV file, collection did not include a remote schema')

		w = csv.DictWriter(output_dest, tuple(datacollection.remote_schema.get('fields', [])))
		w.writeheader()

		# Output data
		for d in dcollection:
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
		dataobject_endpoint=None, included_extended_attrs=True, dobject=None, **kwargs):
	'''	Retrieve details for Sonador cata object and output to provided destination
	'''
	# Retrieve data object (if not already provided). dobject is included in signature to allow
	# for output of details of already existing objects.
	dobject = dobject or fetch_sonador_dataobject(
		sonador_server, datamodel_class, objectid, verify=verify, dataobject_endpoint=dataobject_endpoint, **kwargs)

	sonador_dataobject_serialize(dobject, output_dest, include_extended_attrs=included_extended_attrs)
	logger.debug('Object Resource Data:\n%s' % json.dumps(dobject._objectdata))
	return dobject


def sonador_dataobject_create(sonador_server, datamodel_class, object_data, verify=False, dataobject_endpoint=None, 
		apiurl_callable='sonador_apiurl', headers_callable='sonador_request_headers', rkwargs=None, **kwargs):
	'''	Create an instance of the data object using the provided object data.
		Throws an operation error if the model cannot be created.

		@input sonador_server (sonador.servers.SonadorServer): Sonador server instance
		@input datamodel_class (subclass of remote.SonadorBaseObject): data model class
			which will be used to create the object instance.
		@input object_data (dict): data to be used for creating the model instance
		@input verify (bool, default=False): when True SSL connections will be verified

		@returns server response
	'''
	dataobject_endpoint = dataobject_endpoint or datamodel_class.fetch_endpoint

	# Create request components: URL, headers, keyword arguments
	rurl = getattr(sonador_server, apiurl_callable)(dataobject_endpoint)
	rheaders = getattr(sonador_server, headers_callable)()
	rkwargs = rkwargs or {}

	# Create object instance on the server
	r = requests.post(rurl, json=object_data, verify=verify, headers=rheaders, **kwargs)

	if not r.ok:
		request_client_error('Unable to create instance of model type %s due to a server error' % datamodel_class.__name__, r)

	return sonador_server._parse_apiresponse_json(r)


def sonador_dataobject_update(datamodel_instance, object_data, dataobject_endpoint=None, verify=False,
		apiurl_callable='sonador_apiurl', headers_callable='sonador_request_headers', rkwargs=None, **kwargs):
	'''	Update the data model instance with the parameters container in object data.
	'''
	dataobject_endpoint = dataobject_endpoint or posixpath.join(datamodel_instance.fetch_endpoint, datamodel_instance.pk)

	# Create request components: URL, headers, keyword arguments
	rurl = getattr(datamodel_instance.server, apiurl_callable)(dataobject_endpoint)
	rheaders = getattr(datamodel_instance.server, headers_callable)()
	rkwargs = rkwargs or {}

	# Update object instance on the server
	r = requests.put(rurl, json=object_data, headers=rheaders, verify=verify, **rkwargs)

	if not r.ok:
		request_client_error('Unable to update instance of model type %s (pk=%s) due to a server error' 
			% (type(datamodel_instance).__name__, datamodel_instance.pk))

	return datamodel_instance.server._parse_apiresponse_json(r)
