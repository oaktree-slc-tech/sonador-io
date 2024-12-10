'''	Sonador data models and collections for working with local objects.
'''
from client.local import GuruCoreObject, GuruCoreCollection
from client.utils.object import omit


class SonadorCachedObjectCollectionMixin:
	'''	Mixin class which provides methods for initializing model instances and adding
		them to an internal cached indexed to the model's primary key.
	'''
	def _init_lookup(self, *args, **kwargs):
		'''	Initialize properties needed for cached lookup
		'''
		self._model_lookup = kwargs.get('lookup', {})

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

		# In-case other is an iterator, un-pack to a persistent structure
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


class SonadorLocalObject(GuruCoreObject):
	'''	Model instance for working with local Sonador data.
	'''


class SonadorLocalCollection(SonadorCachedObjectCollectionMixin, GuruCoreCollection):
	'''	Collection of local objects.
	'''
	model = GuruCoreObject

	def __init__(self, *args, **kwargs):
		self._init_lookup(*args, **kwargs)
		super().__init__(*args, **omit(kwargs, ('lookup',)))

	def _init_collection_models(self, **kwargs):
		'''	Initialize collection models, add "collection" reference to model instances
		'''
		kwargs['collection'] = self
		return map(lambda ojson: self._init_collection_modelinstance(ojson, **kwargs), self._objectdata)