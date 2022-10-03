import os, posixpath, logging, glob, re, fnmatch, pydicom, zipfile, pathlib, shutil
from functools import reduce
from collections import OrderedDict, namedtuple
from io import BytesIO
from pydicom.dataset import FileDataset as DCMFileDataset

from concurrent.futures import ThreadPoolExecutor

from ..apisettings import DCM_EXTENSIONS_DEFAULT, IMAGING_SERVER_RESOURCE_STUDY, IMAGING_SERVER_RESOURCE_SERIES, \
	DCMHEADER_STUDY_INSTANCE_UID, DCMHEADER_STUDY_DESCRIPTION, DCMHEADER_SERIES_INSTANCE_UID, DCMHEADER_SERIES_DESCRIPTION, \
	DCM_CONTENT_TYPE, DicomMetaKey, DicomMeta
from ..remote import sonador_datacollection_list, sonador_dataobject_details, sonador_dataobject_schema_display, \
	fetch_sonador_dataobject
from ..servers import SonadorImagingServerCollection, DicomImagingModalityCollection
from ..remote import GuruObjectCollection

from .meta import dcmcache_imgmeta, dcmcache_scanfiles, dcm_findfiles

logger = logging.getLogger(__name__)


def build_dataset(iserver, query, tpool=None, threads=8, *args, **kwargs):
    ''' Create a dataset which includes the results from all components of the query iterable.
        Queries are executed in parallel.
        
        @input iserver (sonador.servers.SonadorImageServer): image server from which the results
            should be retrieved.
        @input query (iterable): query to be used for creating the dataset
        @input resource (str, default='Study'): type of resource to be queried
        
        @returns Sonador resource collection
    '''
    # Ensure all components of the query are dictionaries
    if not all([isinstance(p, dict) for p in query]):
        raise ValueError('All individual components of parallel imaging server queries must be dictionaries.')
        
    # Create thread pool
    tpool = tpool or ThreadPoolExecutor(max_workers=threads)
    
    # Execute query and aggregate results
    return reduce(lambda r0, r1: r0+r1, tpool.map(lambda q: iserver.query(q, *args, **kwargs), query))

