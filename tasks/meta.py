import os, posixpath, logging, glob, re, fnmatch, pydicom, zipfile, pathlib, shutil
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

logger = logging.getLogger(__name__)


def dcmcache_imgmeta(ifile, hcache, study_meta=True, series_meta=True, force_read=False):
	'''	Load the provided image file, retrieve header data, ensure that the file is well formed.
		Checks to see if the file is tracked in the image cache 
		(provided as an argument). If the file is not present, the file will be added to the cache.

		@input ifile (File like object): DCM file
		@input hcache (OrderedDict): Dictionary of image metadata to which the file meta should be added.
		@input study_meta (bool, default=True): Add study metadata as part of the image cache.
		@input series_meta (bool, default=True): Add series metadata as part of the image cache.
		@input force_read (bool, default=False): Force loading of the provided file. When False,
			the load method throws an error if it is unable to read required components of the DCM header.

		@returns pydicom.FileDataset
	'''
	# Load DICOM file, retrieve header data, ensure that the file is well formed
	dcmfile = pydicom.dcmread(ifile, force=force_read)
	ifile.seek(0)
	
	# Updates to study metadata
	if study_meta and getattr(dcmfile, DCMHEADER_STUDY_INSTANCE_UID, None) \
		and not hcache.get(
		DicomMetaKey(IMAGING_SERVER_RESOURCE_STUDY, dcmfile.StudyInstanceUID, dcmfile.StudyInstanceUID)):
		hmeta = DicomMetaKey(IMAGING_SERVER_RESOURCE_STUDY, DCMHEADER_STUDY_INSTANCE_UID, dcmfile.StudyInstanceUID)
		hcache[hmeta] = DicomMeta(getattr(dcmfile, 'StudyDescription', None), None, meta=hmeta)

	# Update to series metadataf
	if series_meta and getattr(dcmfile, DCMHEADER_SERIES_INSTANCE_UID, None) \
		and not hcache.get(
			DicomMetaKey(IMAGING_SERVER_RESOURCE_SERIES, DCMHEADER_SERIES_INSTANCE_UID, dcmfile.SeriesInstanceUID)):
			hmeta = DicomMetaKey(IMAGING_SERVER_RESOURCE_SERIES, DCMHEADER_SERIES_INSTANCE_UID, dcmfile.SeriesInstanceUID)
			hcache[hmeta] = DicomMeta(getattr(dcmfile, 'SeriesDescription', None), getattr(dcmfile, 'Modality', None), meta=hmeta)

	return dcmfile


def dcmcache_scanfiles(ifilelist, hcache=None, study_meta=True, series_meta=True):
	'''	Scans the provided image list, retrive header data, ensure that the file is well formed,
		and builds an image cache of the resulting metadata.

		@filelist (iterable of file paths): List of files to be scanned by the method.
		@hcache (default=new OrderedDict): Existing header cache to which the data should be added.
			If no header cache is provided, a new structure is created.

		@study_meta (bool, default=True): Include study metadata in the image cache.
		@series_meta (bool, default=True): Include series metadata in the image cache.

		@returns OrderedDict of study/series metadata 
	'''
	if hcache is None:
		hcache = OrderedDict()

	for ipath in ifilelist:
		with open(ipath, 'rb') as img:
			dcmcache_imgmeta(img, hcache, study_meta=study_meta, series_meta=series_meta)

	return hcache


def dcm_findfiles(filelist, dcmfiles=None, dcm_extensions=DCM_EXTENSIONS_DEFAULT):
	'''	Scan the provided file list and retrieve all patterns that match the DCM extions.

		@input filelist (iterable): Iterable of file paths
		@input dcmfiles (previously existing list of files, default=new list): List to 
			which the files should be added.
		@input dcm_extensions (iterable of file patterns): File patterns
			that should be used to find and match potential DICOMs
	'''
	if dcmfiles is None:
		dcmfiles = []

	for ext in dcm_extensions:
		dcmfiles.extend(fnmatch.filter(filelist, ext))

	return dcmfiles


def dcmcache_scan_archive(archive, hcache=None, tpool=None, threads=4, dcm_extensions=DCM_EXTENSIONS_DEFAULT,
		ignore_errors=False, callback_onerror=None):
	'''	Scan the provided archive folder, locate images and build a header cache of the metadata.

		@input archive (zipfile.ZipFile): Zip archive to scan

		@returns tuple: int, OrderedDict. Returns the count of uploaded files and the
			 cache of study and series metadata from DICOM files in the archive
	'''
	# Initialize header cache and thread pool
	hcache = hcache or OrderedDict()
	tpool = tpool or ThreadPoolExecutor(max_workers=threads)

	# Locate all files included in the archive
	dcmfiles = dcm_findfiles(archive.namelist(), dcm_extensions=dcm_extensions)

	def scan_archivefile(iname):
		'''	Scan the provided archive file and add its associated metadata to the header archive
		'''
		# Open the referenced file
		with archive.open(iname) as afile:

			try:
				# Parse image to ensure that it is well formed and to retrieve the metadata
				ifile = BytesIO(afile.read())
				dcmfile = dcmcache_imgmeta(ifile, hcache)

			except pydicom.errors.InvalidDicomError as err:

				# Invoke onerror callback
				if callable(callback_onerror):
					callback_onerror(err, iname, afile)

				# Log and suppress the error
				if ignore_errors:
					logger.error('Unable to scan DICOM file %s, invalid file. Skipping file.' % iname)
					return False

				raise err

			except Exception as err:

				# Invoke onerror callback
				if callable(callback_onerror):
					callback_onerror(err, iname, afile)

				# Log and suppress the error
				if ignore_errors:
					logger.error('Unable to scan file %s due to an error. Skipping file. Error:\n%s' % (iname, err))
					return False

				raise err

		return True

	fcount = sum(tpool.map(scan_archivefile, dcmfiles))
	return hcache, fcount
