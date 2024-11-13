import os, logging
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)


def download_imaging_filearchive(r, download_folder, extract=False):
	'''	Download the file archive for the provided resource and write the contents to disk.
		If extract is True, the contents of the archive rather than the archive itself
		will be written to disk.

		@input r (imaging resource model instance): Imaging resource model instance
			for which the data should be downloaded.
		@input download_folder (str): Folder to which the imaging data should be downloaded.
		@input extract (bool, default=False): Toggles whether the file or the file
			contents should be written to disk. When True, the contents of the
			archive will be written to the provided destination.
	'''
	if not os.path.exists(download_folder):
		raise ValueError('Download folder "%s" does not exist' % download_folder)

	with r.filearchive() as a:

		# Extract archive contents
		if extract:
			a.extractall(os.path.join(download_folder, r.pk))
			logger.info('File archive for %s extacted to %s successfully' 
				% (r.pk, os.path.join(download_folder, r.pk)))

		# Write data to file
		else:
			
			with open(os.path.join(download_folder, '%s.zip' % r.pk), 'wb') as f:
				a.raw.seek(0)
				f.write(a.raw.read())
				logger.info('File archive for %s downloaded successfully to %s'
					% (r.pk, os.path.join(download_folder, '%s.zip' % r.pk)))


def stream_imaging_series(sx, download_folder, tpool=None, threads=4):
	'''	Download the imaging data for the provided series and write contents to disk.
		Streaming downloads make one request for each file in the series.

		@input sx (sonador.imaging.orthanc.ImagingSeries): imaging series 
			to be downloaded
		@input download_folder (str): path to the download folder where the data should
			be stored
	'''
	if not os.path.exists(download_folder):
		raise ValueError('Download folder "%s" does not exist' % download_folder)

	# Create thread pool
	tpool = tpool or ThreadPoolExecutor(max_workers=threads)

	def _download_dcm(dcm):
		'''	Download the DCM instance and write to dest folder

			@returns True if download succesful
		'''
		dcm_fpath = os.path.join(download_folder, '%s.dcm' % dcm.pk)
		with open(dcm_fpath, 'wb') as f:
			dcm.dcmfile().save_as(f)

			logger.debug('DICOM file for series="%s" instance="%s" downloaded successfully: %s'
				% (sx.pk, dcm.pk, dcm_fpath))

			return True

	fcount = sum(tpool.map(_download_dcm, sx.instances_collection))
	logger.info('DICOM series="%s" downloaded successfully' % sx.pk)


def stream_imaging_study(s, download_folder, tpool=None, threads=4, **kwargs):
	'''	Download the imaging data for the provided study and write contents to disk.
		Streaming downloads make one request for each file in the study's DICOM's series.
		Series downloads delegate to `stream_imaging_series`.

		@input s (sonador.imaging.orthanc.ImagingStudy): imaging study to be downloaded
		@input download_folder (str): path to the download folder where the data should
			be stored
	'''
	if not os.path.exists(download_folder):
		raise ValueError('Download folder "%s" does not exist')

	# Create thread pool
	tpool = tpool or ThreadPoolExecutor(max_workers=threads)

	logger.info('<-- Begin streaming download of study="%s" -->')

	for sx in s.series_collection:

		# Create subfolder for imaging series (if it does not exist)
		sx_download_folder = os.path.join(download_folder, sx.pk)
		if not os.path.exists(sx_download_folder):
			os.mkdir(sx_download_folder)

		stream_imaging_series(sx, sx_download_folder, tpool=tpool, **kwargs)

	logger.info('<-- Streaming download of study="%s" finished successfully -->' % s.pk)


def stream_imaging_patient(p, download_folder, tpool=None, threads=4, **kwargs):
	'''	Download the imaging data for the provided patient and write contents to disk.
		Streaming downloads make one request per each file in each series associated with
		the patient. Study downloads delegate to `stream_imaging_study`.

		@input p (sonador.imiaging.orthanc.ImagingPatient): imaging patient to be downloaded
		@input download_folder (str): path to the download folder where the data
			should be stored
	'''
	if not os.path.exists(download_folder):
		raise ValueError('Download folder "%s" does not exist')

	# Create thread pool
	tpool = tpool or ThreadPoolExecutor(max_workers=threads)

	logger.info('<--## Begin streaming download of patient="%s" ##-->')

	for s in p.studies_collection:

		# Create subfolder for imaging study (if it does not exist)
		s_download_folder = os.path.join(download_folder, s.pk)
		if not os.path.exists(s_download_folder):
			os.mkdir(s_download_folder)

		stream_imaging_study(s, s_download_folder, tpool=tpool, **kwargs)

	logger.info('<--## Streaming download of patient="%s" finished successfully ##-->' % p.pk)
