import os, posixpath, logging, glob, re, fnmatch, pydicom, zipfile
from collections import OrderedDict, namedtuple
from io import BytesIO
from pydicom.dataset import FileDataset as DCMFileDataset

from concurrent.futures import ThreadPoolExecutor

from ..apisettings import DCM_EXTENSIONS_DEFAULT, IMAGING_SERVER_RESOURCE_STUDY, IMAGING_SERVER_RESOURCE_SERIES, \
	DCMHEADER_STUDY_INSTANCE_UID, DCMHEADER_STUDY_DESCRIPTION, DCMHEADER_SERIES_INSTANCE_UID, DCMHEADER_SERIES_DESCRIPTION
from ..remote import sonador_datacollection_list, sonador_dataobject_details, sonador_dataobject_schema_display, \
	fetch_sonador_dataobject
from ..servers import SonadorImagingServerCollection, DicomImagingModalityCollection

logger = logging.getLogger(__name__)


DCM_CONTENT_TYPE = 'application/octet-stream'

DicomMetaKey = namedtuple('DicomMetaKey', ('resource', 'header', 'uid'))
DicomMeta = namedtuple('DicomMeta', ('description', 'modality'))


def dcmcache_imgmeta(ifile, hcache, study_meta=True, series_meta=True):
	'''	Load the provided image file, retrieve header data, ensure that the file is well formed.
		Checks to see if the file is tracked in the image cache 
		(provided as an argument). If the file is not present, the file will be added to the cache.

		@input ifile (File like object): DCM file
		@input hcache (OrderedDict): Dictionary of image metadata processed as part of the
			image upload
	'''
	# Load DICOM file, retrieve header data, ensure that the file is well formed
	dcmfile = pydicom.dcmread(ifile)
	ifile.seek(0)
	
	# Updates to study metadata
	if study_meta and getattr(dcmfile, DCMHEADER_STUDY_INSTANCE_UID, None) \
		and not hcache.get(
		DicomMetaKey(IMAGING_SERVER_RESOURCE_STUDY, dcmfile.StudyInstanceUID, dcmfile.StudyInstanceUID)):
		hcache[DicomMetaKey(IMAGING_SERVER_RESOURCE_STUDY, DCMHEADER_STUDY_INSTANCE_UID, dcmfile.StudyInstanceUID)] \
			= DicomMeta(getattr(dcmfile, 'StudyDescription', None), None)

	# Update to series metadataf
	if series_meta and getattr(dcmfile, DCMHEADER_SERIES_INSTANCE_UID, None) \
		and not hcache.get(
			DicomMetaKey(IMAGING_SERVER_RESOURCE_SERIES, DCMHEADER_SERIES_INSTANCE_UID, dcmfile.SeriesInstanceUID)):
			hcache[DicomMetaKey(IMAGING_SERVER_RESOURCE_SERIES, DCMHEADER_SERIES_INSTANCE_UID, dcmfile.SeriesInstanceUID)] \
				= DicomMeta(getattr(dcmfile, 'SeriesDescription', None), getattr(dcmfile, 'Modality', None))

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


def imageserver_upload_folder(iserver, folders, tpool=None, threads=4, 
		verify=False, fileupload_check=False, dcm_extensions=DCM_EXTENSIONS_DEFAULT):
	'''	Scan folders and upload all DICOM images to the provided imaging servers

		@input iserver (SonadorImagingServer instance): Imaging server to which the
			images should be uploaded.
		@input folders (iterable of folder paths): Paths for which all matching files
			should be uploaded to the provided imaging server.
		@fileuploa_check (bool, default=False): Toggles whether a check for the series UID
			should be performed prior to uploading the files.

		@returns OrderedDict of all files uploaded to Sonador
	'''
	# Create thread pool
	tpool = tpool or ThreadPoolExecutor(max_workers=threads)

	# Create cache of previously uploaded files to check upload status
	# before re-transmitting
	previously_uploaded = OrderedDict()

	for froot in folders:
		logger.info('Scan %s for images to upload to server %s' % (froot, iserver.server_label))

		# Walk through folders and locate images for upload
		for croot, cfolders, cfiles in os.walk(froot):

			# Find all DICOM files and variations inside of the directory
			dcmfiles = []
			for ext in dcm_extensions:
				dcmfiles.extend(fnmatch.filter(cfiles, ext))

			# Check to see if files have previously been uploaded: create a cache of series UIDs of files
			# in the folder, then query the Orthanc instance to for which series already exist.
			if fileupload_check:
				fmeta = dcmcache_scanfiles([os.path.join(croot, iname) for iname in dcmfiles], study_meta=False)

				# Check Orthanc to determine if the data has already been.
				fseries_meta = set(mkey.uid for mkey, mdata in fmeta.items() 
					if len(iserver.query_series({ mkey.header: mkey.uid })))

			# Upload files (in parallel) to the image server
			if len(dcmfiles):
				logger.debug('Found %s files in folder %s, begin upload' % (len(dcmfiles), croot))

				def upload_dcmimages(iname):
					'''	Upload the provided image to Orthanc.

						@returns bool or None: Result of the upload. If the upload was sent to Orthanc, the result
							will be either True or False (depending on if the upload was successful for not). If
							the upload was skipped (fileupload_check is True and the UID was already in Orthanc)
							the method returns None.
					'''
					ipath = os.path.join(croot, iname)
					with open(ipath, 'rb') as img:
						logger.debug('Upload image %s to server %s' % (iname, iserver.pk))

						# Check the file cache to determine if the upload has already been sent to Sonador
						if fileupload_check:
							dcmfile = pydicom.dcmread(img)
							img.seek(0)

							if getattr(dcmfile, DCMHEADER_SERIES_INSTANCE_UID, None) in fseries_meta:
								logging.info('Image %s (series %s) already available on server %s.'
									% (ipath, getattr(dcmfile, DCMHEADER_SERIES_INSTANCE_UID, None), iserver.server_label))
								return None

						# Upload image to PACS imaging server
						r = iserver.upload_image(img)
						return r.ok

				uresults = sum(filter(lambda v: v is not None, tpool.map(upload_dcmimages, dcmfiles)))
				if uresults:
					logger.info('Transfer results (%s): %s images uploaded successfully' % (croot, uresults))


def imageserver_upload_archive(iserver, archive, tpool=None, threads=4, verify=False, 
		dcm_extensions=DCM_EXTENSIONS_DEFAULT, ignore_errors=False):
	'''	Scan the provide archive folder and upload DICOM images to the imaging server.

		@input iserver (SonadorImagingServer instance): Imaging server to which the
			images should be uploaded.

		@returns tuple: int, OrderedDict. Returns the count of uploaded files and 
			an ordered dictionary of image metadata. (Series/study UIDs and descriptions.)
	'''
	# Create thread pool
	tpool = tpool or ThreadPoolExecutor(max_workers=threads)

	# Create regular expressions from the DCM extensions patterns
	dcm_fpatterns = [re.compile(fnmatch.translate(p)) for p in DCM_EXTENSIONS_DEFAULT]

	# Locate all DCM files included in the archive
	dcmfiles = []

	# List files from the zip archive, check file pattern and add matching patterns
	fnames = archive.namelist()
	for p in dcm_fpatterns:
		dcmfiles.extend([f for f in fnames if p.search(f)])

	# Cache of image metadata
	hcache = OrderedDict()

	def upload_archiveimage(iname):
		'''	Upload the provided file from the archive to Orthanc
		'''
		# Open the file reference and upload
		with archive.open(iname) as afile:

			try: 
				ifile = BytesIO(afile.read())
			
				# Parse image to ensure that the it is well formed prior to upload, upload to server
				dcmcache_imgmeta(ifile, hcache)
				iserver.upload_image(ifile)

			except pydicom.errors.InvalidDicomError as err:

				# Log and suppress the error
				if ignore_errors:
					logger.warning('Unable to upload file %s, invalid DCM file. Skipping.' % iname)

				raise err

			except Exception as err:

				# Log and suppress the error
				if ignore_errors:
					logger.error('Unable to upload file %s due to an error. Skipping file. Error:\n%s'
						% (iname, err))

				raise err

			return True

	fcount = sum(tpool.map(upload_archiveimage, dcmfiles))
	return hcache, fcount
