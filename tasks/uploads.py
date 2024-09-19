import os, posixpath, logging, glob, re, fnmatch, pydicom, zipfile, pathlib, shutil
from collections import OrderedDict, namedtuple
from io import BytesIO
from pydicom.dataset import FileDataset as DCMFileDataset

from concurrent.futures import ThreadPoolExecutor
from client.utils.general import first

from ..apisettings import DCM_EXTENSIONS_DEFAULT, IMAGING_SERVER_RESOURCE_STUDY, IMAGING_SERVER_RESOURCE_SERIES, \
	DCMHEADER_STUDY_INSTANCE_UID, DCMHEADER_STUDY_DESCRIPTION, DCMHEADER_SERIES_INSTANCE_UID, DCMHEADER_SERIES_DESCRIPTION, \
	DCM_CONTENT_TYPE, DicomMetaKey, DicomMeta
from ..remote import sonador_datacollection_list, sonador_dataobject_details, sonador_dataobject_schema_display, \
	fetch_sonador_dataobject
from ..servers import SonadorImagingServerCollection, DicomImagingModalityCollection

from .meta import dcmcache_imgmeta, dcmcache_scanfiles, dcm_findfiles

logger = logging.getLogger(__name__)


def imageserver_upload_folder(iserver, folders, tpool=None, threads=4, 
		verify=False, fileupload_check=False, destfolder_complete=None,
		dcm_extensions=DCM_EXTENSIONS_DEFAULT, ignore_errors=False,
		callback_preupload=None, callback_postupload=None, 
		callback_onerror=None, callback_onduplicate=None, dry_run=False,
		rapid_lookup=True, hcache=None):
	'''	Scan folders and upload all DICOM images to the provided imaging servers

		@input iserver (SonadorImagingServer instance): Imaging server to which the
			images should be uploaded.
		@input folders (iterable of folder paths): Paths for which all matching files
			should be uploaded to the provided imaging server.
		@fileupload_check (bool, default=False): Toggles whether a check for the series UID
			should be performed prior to uploading the files.

		@input callback_preupload (callable): Function  that is invoked
			immediately prior to uploading a DICOM file to Orthanc. The callback 
			should accept the following signature:
			- iname (str): name of the file
			- ifile (file-like object): DICOM data
			- dcmfile (pydicom.dataset.Dataset): PyDicom dataset object, containing a 
				dictionary of the DICOM data elements.

			and returns a file-like binary object with the data to be sent to Sonador.

		@input callback_postupload (callable): Function that is invoked immediately following
			a DICOM file is sent to Orthanc. The callback should accept the following
			signature:
			- uresults (requests.Response): HTTP response which contains the results
				of the upload and the server response.
			- iname (str): name of the file
			- ifile (file-like object): DICOM data
			- dcmfile (pydicom.dataset.Dataset): PyDicom dataset object, containing a
				dictionary of the DICOM data elements.

		@input callback_onerror (callable): Function that is invoked when there is an error 
			uploading a DICOM file to Orthanc. The callback should accept the following signaure:
			- err (Exception instance): exception which caused the upload error
			- iname (str): name of the file
			- ifile (file-like object): DICOM data

		@input callback_onduplicate (callable): Function that is invoked when there is a duplicate
			file detected on Orthanc.
			- resource (sonador.imaging.orthanc.ImagingSeries): Sonador series reference 
				which the instance/file is part of.
			- iname (str): name of the file
			- ifile (file-like object): DICOM data
			- dcmfile (pydicom.dataset.Dataset): PyDicom dataset object, containing a dictioary
				of the DICOM data elements.

		@input dry_run (bool, default=False): when True, the file scanning process executes
			but no files are transferred to the server.

		@returns OrderedDict of all files uploaded to Sonador
	'''
	# Create thread pool
	tpool = tpool or ThreadPoolExecutor(max_workers=threads)

	# Cache of image metadata for files processed
	hcache = hcache if isinstance(hcache, (OrderedDict, dict)) else OrderedDict()
	fcount = 0
	
	if destfolder_complete and not os.path.exists(destfolder_complete):
		raise Exception('Invalid destination folder %s does not exist' % destfolder_complete)
		
	for froot in folders:
		froot_path = pathlib.Path(froot)
		logger.info('Scan %s for images to upload to server %s' % (froot_path, iserver.server_label))

		# Walk through folders and locate images for upload
		for croot, cfolders, cfiles in os.walk(froot_path):

			# Find all DICOM files and variations inside of the directory
			dcmfiles = dcm_findfiles(cfiles, dcm_extensions=dcm_extensions)

			# Check to see if files have previously been uploaded: create a cache of series UIDs of files
			# in the folder, then query the Orthanc instance to for which series already exist.
			if fileupload_check:
				fmeta = dcmcache_scanfiles([os.path.join(croot, iname) for iname in dcmfiles], study_meta=False)

				# Check Orthanc to determine if the data has already been uploaded.
				fseries_meta = set(mkey.uid for mkey, mdata in fmeta.items() 
					if len(iserver.query_series({ mkey.header: mkey.uid }, rapid_lookup=rapid_lookup)))

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
					ipath = pathlib.Path(os.path.join(croot, iname))

					# Operational results
					op_mvimg = False
					op_code = None

					with open(ipath, 'rb') as img:
						logger.debug('Upload image %s to server %s' % (iname, iserver.pk))

						# Check the file cache to determine if the upload has already been sent to Sonador
						if fileupload_check:
							dcmfile = pydicom.dcmread(img)
							img.seek(0)

							if getattr(dcmfile, DCMHEADER_SERIES_INSTANCE_UID, None) in fseries_meta:

								# Retrieve header and metadata from duplicates cache
								mdata = first(fmeta.values(), key=lambda v: v.meta.uid == getattr(dcmfile, DCMHEADER_SERIES_INSTANCE_UID, None))
								if not mdata:
									raise ValueError('Series %s indicated as a duplicate, but unable to retrieve metadata. Abort upload.'
										% getattr(dcmfile, DCMHEADER_SERIES_INSTANCE_UID, None))

								# Retrieve series reference from Sonador (first instance of duplicate series, cached for subsequent instances)
								r = getattr(mdata, 'resource', None)
								if r is None:
									r = iserver.query_series({ mdata.meta.header: mdata.meta.uid }, rapid_lookup=rapid_lookup)[0]
									setattr(mdata, 'resource', r)
								
								logging.info('Image %s (series %s) already available on server %s: series-uid=%s.'
									% (ipath, getattr(dcmfile, DCMHEADER_SERIES_INSTANCE_UID, None), iserver.server_label, r.pk))

								# Invoke on-duplicate callback
								if callable(callback_onduplicate):
									callback_onduplicate(r, iname, img, dcmfile)
								
								return None

						# Upload image to PACS imaging server
						try:
							# Parse image to ensure that it is well formed prior to upload
							ifile = BytesIO(img.read())
							dcmfile = dcmcache_imgmeta(ifile, hcache)

							# Invoke preupload hook
							if callable(callback_preupload):
								ifile = callback_preupload(iname, ifile, dcmfile)

							# Upload file to Orthanc
							if not dry_run:
								r = iserver.upload_image(ifile)
								op_code = r.ok
								logger.debug('File %s uploaded to server "%s" successfuly' % (ipath, iserver.pk))
							else:
								logger.debug('Dry Run: file %s processed successfully' % ipath)

							# Invoke postupload hook
							if callable(callback_postupload):
								callback_postupload(r, iname, ifile, dcmfile)

							# Upload successful: if indicated, move the image to the results tree
							op_mvimg = True if destfolder_complete and op_code else False

						except pydicom.errors.InvalidDicomError as err:

							# Invoke onerror callback
							if callable(callback_onerror):
								callback_onerror(err, iname, img)

							# Log and suppress the error
							if ignore_errors:
								logger.error('Unable to upload file %s, invalid DICOM file. Skipping.' % ipath)
								op_code = False
								op_mvimg = True if destfolder_complete and ignore_errors else False

							else: raise err

						except Exception as err:

							# Invoke onerror callback
							if callable(callback_onerror):
								callback_onerror(err, iname, img)

							# Log and Suppress the error
							if ignore_errors:
								logger.error(
									'Unable to upload file %s due to an error. Skipping file. Error:\n%s' % (iname, err))
								op_code = False
								op_mvimg = True if destfolder_complete and ignore_errors else False

							else: raise err

					# Move file to the destination folder
					if op_mvimg:
						ipath_dest = pathlib.Path(
							os.path.join(destfolder_complete, froot_path.name, ipath.relative_to(froot_path)))

						# Create parent folder
						if not ipath_dest.parent.exists():
							ipath_dest.parent.mkdir(parents=True, exist_ok=True)

						# Move the file to the destination folder
						shutil.move(ipath, ipath_dest)
						logger.debug(
							'Move file %s to destination folder %s' % (ipath, ipath_dest))
					
					return op_code

				# Upload images and add successful number of transfers to overall file count
				uresults = sum(filter(lambda v: v is not None, tpool.map(upload_dcmimages, dcmfiles)))
				fcount += uresults
				if uresults:
					logger.info('Transfer results (%s): %s images uploaded successfully' % (croot, uresults))

	return hcache, fcount		


def imageserver_upload_archive(iserver, archive, tpool=None, threads=4, verify=False, 
		dcm_extensions=DCM_EXTENSIONS_DEFAULT, ignore_errors=False, 
		callback_preupload=None, callback_postupload=None, callback_onerror=None):
	'''	Scan the provide archive folder and upload DICOM images to the imaging server.

		@input iserver (SonadorImagingServer instance): Imaging server to which the
			images should be uploaded.
		
		@input callback_preupload (callable): Function  that is invoked
			immediately prior to uploading a DICOM file to Orthanc. The callback 
			should accept the following signature:
			- ifile (file-like object): DICOM data
			- dcmfile (pydicom.dataset.Dataset): PyDicom dataset object, containing a 
				dictionary of the DICOM data elements.

			and returns a file-like binary object with the data to be sent to Sonador.

		@input callback_postupload (callable): Function that is invoked immediately following
			a DICOM file is sent to Orthanc. The callback should accept the following
			signature:
			- uresults (requests.Response): HTTP response which contains the results
				of the upload and the server response.
			- ifile (file-like object): DICOM data
			- dcmfile (pydicom.dataset.Dataset): PyDicom dataset object, containing a
				dictionarry of the DICOM  data  elements.

		@returns tuple: int, OrderedDict. Returns the count of uploaded files and 
			an ordered dictionary of image metadata. (Series/study UIDs and descriptions.)
	'''
	# Create thread pool
	tpool = tpool or ThreadPoolExecutor(max_workers=threads)

	# Locate all DCM files included in the archive
	dcmfiles = dcm_findfiles(archive.namelist(), dcm_extensions=dcm_extensions)

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
				dcmfile = dcmcache_imgmeta(ifile, hcache)

				# Invoke preupload hook
				if callable(callback_preupload):
					ifile = callback_preupload(iname, ifile, dcmfile)
				
				# Uplooad file to Orthanc
				uresults = iserver.upload_image(ifile)

				# Invoke postupload hook
				if callable(callback_postupload):
					callback_postupload(uresults, iname, ifile, dcmfile)

			except pydicom.errors.InvalidDicomError as err:

				# Invoke onerror callback
				if callable(callback_onerror):
					callback_onerror(err, iname, afile)

				# Log and suppress the error
				if ignore_errors:
					logger.error('Unable to upload file %s, invalid DCM file. Skipping.' % iname)
					return False

				raise err

			except Exception as err:

				# Invoke onerror callback
				if callable(callback_onerror):
					callback_onerror(err, iname, afile)

				# Log and suppress the error
				if ignore_errors:
					logger.error(
						'Unable to upload file %s due to an error. Skipping file. Error:\n%s' % (iname, err))
					return False

				raise err

			return True

	fcount = sum(tpool.map(upload_archiveimage, dcmfiles))
	return hcache, fcount
