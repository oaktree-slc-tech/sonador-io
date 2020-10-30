import os, logging

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