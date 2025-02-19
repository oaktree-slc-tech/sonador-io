import os, uuid, tempfile, pickle
from collections import UserDict
from client.utils.microservices import MicroserviceOrderedJsonResponseObject, MicroserviceJsonResponseObject

class SonadorDict(UserDict):
    ''' Dictionary instance that can be used for caching data
        in a temp folder on disk instead of in memory. Data is saved
        to the temp folder as a pickled binary string using 
        pickle.dump. Data is pickled using the class variable
        pickle_protocol.

        Data is removed when the dict instance is garbage collected.
        Stored files can be manually removed by calling SonadorDict.cleanup().
    '''
    pickle_protocol = pickle.HIGHEST_PROTOCOL

    def __init__(self, **kwargs):
        super().__init__()
        
        # Create temporary directory for data
        self.tmp = tempfile.TemporaryDirectory()
        self.update(**kwargs)
        
    def __getitem__(self, key):
        uid = super().__getitem__(key)
      
        with open(os.path.join(self.tmp.name, uid), "rb") as f:
            return pickle.load(f)

    def __setitem__(self, key, val):
        uid = super().get(key) or str(uuid.uuid4()) 
        with open(os.path.join(self.tmp.name, uid), "wb") as f:
            pickle.dump(val, f, protocol=self.pickle_protocol)
            
        super().__setitem__(key, uid)
    
    def __delitem__(self, key):
        uid = super().get(key)
        if uid:
            _fpath = os.path.join(self.tmp.name, uid)
            if os.path.exists(_fpath):
                os.remove(_fpath)
        
        return super().__delitem__(key)
    
    def __del__(self):
        self.cleanup()
        super().__del__()
        
    def get(self, key, default=None):
        try: return self.__getitem__(key)
        except KeyError as err:
            return default
    
    def cleanup(self, *args, **kwargs):
        ''' Remove all temporary files and clear UIDs from dict.
        '''
        self.tmp.cleanup()
        for k in self.keys():
            super().__delitem__(k)
