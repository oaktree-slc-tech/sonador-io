import os, posixpath, logging, glob, re, fnmatch, pydicom, zipfile
from collections import OrderedDict, namedtuple
from io import BytesIO

from concurrent.futures import ThreadPoolExecutor

from ..apisettings import DCM_EXTENSIONS_DEFAULT, IMAGING_SERVER_RESOURCE_STUDY, IMAGING_SERVER_RESOURCE_SERIES, \
	DCMHEADER_STUDY_INSTANCE_UID, DCMHEADER_STUDY_DESCRIPTION, DCMHEADER_SERIES_INSTANCE_UID, DCMHEADER_SERIES_DESCRIPTION
from ..remote import sonador_datacollection_list, sonador_dataobject_details, sonador_dataobject_schema_display, \
	fetch_sonador_dataobject
from ..servers import SonadorImagingServerCollection, DicomImagingModalityCollection

logger = logging.getLogger(__name__)


DCM_CONTENT_TYPE = 'application/octet-stream'

DicomMetaKey = namedtuple('DicomMetaKey', ('resource', 'header', 'uid'))
DicomMeta = namedtuple('DicomMeta', ('description',))


def dcmcache_imgmeta(ifile, hcache):
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
	if getattr(dcmfile, DCMHEADER_STUDY_INSTANCE_UID, None) \
		and not hcache.get(
		DicomMetaKey(IMAGING_SERVER_RESOURCE_STUDY, dcmfile.StudyInstanceUID, dcmfile.StudyInstanceUID)):
		hcache[DicomMetaKey(IMAGING_SERVER_RESOURCE_STUDY, DCMHEADER_STUDY_INSTANCE_UID, dcmfile.StudyInstanceUID)] \
			= DicomMeta(dcmfile.StudyDescription)

	# Update to series metadata
	if getattr(dcmfile, DCMHEADER_SERIES_INSTANCE_UID, None) \
		and not hcache.get(
			DicomMetaKey(IMAGING_SERVER_RESOURCE_SERIES, dcmfile.SeriesInstanceUID, dcmfile.SeriesInstanceUID)):
			hcache[DicomMetaKey(IMAGING_SERVER_RESOURCE_SERIES, DCMHEADER_SERIES_INSTANCE_UID, dcmfile.SeriesInstanceUID)] \
				= DicomMeta(dcmfile.SeriesDescription)


def imageserver_upload_folder(iserver, folders, tpool=None, threads=4, verify=False, 
		dcm_extensions=DCM_EXTENSIONS_DEFAULT):
	'''	Scan folders and upload all DICOM images to the provided imaging servers

		@input iserver (SonadorImagingServer instance): Imaging server to which the
			images should be uploaded.
	'''
	# Create thread pool
	tpool = tpool or ThreadPoolExecutor(max_workers=threads)

	for froot in folders:
		logger.info('Scan %s for images to upload to server %s' % (froot, iserver.server_label))

		# Walk through folders and locate images for upload
		for croot, cfolders, cfiles in os.walk(froot):

			# Find all DICOM files and variations inside of the directory
			dcmfiles = []
			for ext in dcm_extensions:
				dcmfiles.extend(fnmatch.filter(cfiles, ext))

			# Upload files (in parallel) to the image server
			if len(dcmfiles):
				logger.debug('Found %s files in folder %s, begin upload' % (len(dcmfiles), croot))

				def upload_dcmimages(iname):
					'''	Upload the provided image to Orthanc
					'''
					ipath = os.path.join(croot, iname)
					with open(ipath, 'rb') as img:
						logger.debug('Upload image %s to server %s' % (iname, iserver.pk))

						# Upload image to PACS imaging server
						r = iserver.upload_image(img)
						return r.ok

				uresults = sum(tpool.map(upload_dcmimages, dcmfiles))
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
