import os, sys, six, argparse
from collections import OrderedDict

from client.utils.logs import LOGGING_LEVELS
from client.utils.object import pick

from .apisettings import SONADOR_ACCESS_ID, SONADOR_SECRET_KEY, SONADOR_URL, SONADOR_APITOKEN, \
	SONADOR_IMAGING_SERVER

from .validate import argparse_type_directory, argparse_keyval
from .serialization import OUTPUT_TYPE_TABULATE, OUTPUT_TYPE_CSV, OUTPUT_TYPE_SUPPORTED
from .imaging.orthanc import FILEARCHIVE_TYPE_ZIPARCHIVE, FILEARCHIVE_TYPE_DICOMDIR, FILEARCHIVE_TYPE_SUPPORTED 


# General Commands
SONADOR_COMMAND_LIST = 'list'
SONADOR_COMMAND_DETAILS = 'details'
SONADOR_COMMAND_CREATE = 'create'
SONADOR_COMMAND_UPDATE = 'update'


PARSER_SONADOR_ACCESSID = 'accessid'
PARSER_SONADOR_SECRET = 'secretkey'
PARSER_SONADOR_ENDPOINT = 'endpoint'
PARSER_SONADOR_APITOKEN = 'apitoken'
PARSER_SONADOR_IMAGESERVER = 'server'


ENV_MAPPINGS = {
	PARSER_SONADOR_ACCESSID: PARSER_SONADOR_ACCESSID,
	PARSER_SONADOR_SECRET:  SONADOR_SECRET_KEY,
	PARSER_SONADOR_ENDPOINT: SONADOR_URL,
	PARSER_SONADOR_APITOKEN: SONADOR_APITOKEN,
	PARSER_SONADOR_IMAGESERVER: SONADOR_IMAGING_SERVER,
}


# Sonador Interface Options

def add_arguments_for_api_connection(parser):
	'''Add  options for Sonador API connection: access-id, secret-key, api-endpoint, and token
	'''
	parser.add_argument('--access-id', dest=PARSER_SONADOR_ACCESSID, default=os.environ.get(SONADOR_ACCESS_ID),
		help=('''Access ID used for authentication to Sonador. May also be provided 
			as the %s shell environment variable.''' % SONADOR_ACCESS_ID).replace('\t', '').replace('\n', ''))
	parser.add_argument('--secret-key', dest=PARSER_SONADOR_SECRET, default=os.environ.get(SONADOR_SECRET_KEY),
		help=('''Secret key used for authentication to Sonador. May also be 
			provided as the %s shell environment variable.''' % SONADOR_SECRET_KEY).replace('\t', '').replace('\n', ''))
	parser.add_argument('--api-endpoint', dest=PARSER_SONADOR_ENDPOINT, default=os.environ.get(SONADOR_URL),
		help=('''Sonador instance to which API requests should be sent (including scheme, port, path). 
			May also be provided as the %s shell environment variable.''' % SONADOR_URL).replace('\t', '').replace('\n', ''))
	parser.add_argument('--api-token', dest=PARSER_SONADOR_APITOKEN, default=os.environ.get(SONADOR_APITOKEN),
		help=('''Sonador access token which should be used to make authenticated requests. When present, 
				takes precedence over the access ID and secret key. May also be provided as the 
				%s shell environment variable.''' % SONADOR_APITOKEN).replace('\t', '').replace('\n', ''))


def add_arguments_for_verify_ssl(parser):
	'''Add argument for Sonador API SSL verification
	'''
	parser.add_argument('--verify-ssl', dest='verifyssl', action='store_true', default=True,
		help='Verify SSL connections')


def add_arguments_for_internal_dns(parser):
	'''	Add argument for Sonador API DNS management
	'''
	parser.add_argument('--internal-dns', dest='internal_dns', action='store_true', default=False,
		help='Use internal DNS (if present) for Sonador and Orthanc API server connections')


def add_arguments_for_logging_options(parser, loglevel_default='info'):
	'''Add arguments for parser like loglevel, logformat, logname, error-traceback
	'''
	parser.add_argument('--log-level', dest='loglevel', default=loglevel_default, 
		choices=LOGGING_LEVELS.keys(), help='level of detail to show in output log')
	parser.add_argument('--log-format', dest='logformat', default='%(name)s:%(levelname)s: %(message)s',
		help='format string to use for log messages')
	parser.add_argument('--log-name', dest='logname', default='sonador',
		help='prefix string to append to log messages')
	parser.add_argument('--error-traceback', dest='error_traceback', action='store_true', default=False,
		help='''Show tracebacks on debug''')


def output_destination_options(subparser):
	'''	Add CLI output options to the specified subparser
	'''
	subparser.add_argument('--output-dest', dest='output_dest', type=argparse.FileType('w'), default=sys.stdout,
		help='Data output destination. Default: sys.stdout')


def output_operation_options(subparser):
	'''	Add CLI list operation options to the specified subparser
	'''
	subparser.add_argument('--output-type', dest='output_type',
		choices=tuple(six.iterkeys(OUTPUT_TYPE_SUPPORTED)), default=OUTPUT_TYPE_TABULATE,
		help='Data output type. Types: %s' % ', '.join(
			'%s (%s)' % (k, v) for k, v in six.iteritems(OUTPUT_TYPE_SUPPORTED)))
	output_destination_options(subparser)


def imageserver_operation_options(subparser):
	'''	Add CLI options for imageserver operations
	'''
	subparser.add_argument('--server', '-s', dest=PARSER_SONADOR_IMAGESERVER, type=str, 
		help='Imaging server. May also be provided as the %s shell environment variable.' % SONADOR_IMAGING_SERVER,
		default=os.environ.get(SONADOR_IMAGING_SERVER))


def datamodel_schema_options(subparser):
	'''	Add CLI options for data model operations
	'''
	subparser.add_argument('--include-schema', dest='include_schema', action='store_true', default=False,
		help='Include the schema for the model in the output')


def datamodel_query_options(subparser):
	'''	Add CLI options for data model query operations: query structure, 
	'''
	subparser.add_argument('--query', '-Q', dest='query', nargs='+', type=argparse_keyval, required=True, metavar='KEY=VALUE',
		help='Search values that should be sent to the server as part of the request. Query values should be '
			+ 'structured as key=value pairs. Example: -Q PatientID="*" StudyDescription="*Chest*". Partial expressions '
			+ 'can be matched by including a wildcard in the value. PatientName="Jones*" will match any '
			+ 'patient name that includes "Jones".')


def datamodel_pagination_options(subparser, limit_default=1000):
	'''	Add CLI options for data model pagination
	'''
	subparser.add_argument('--limit', dest='limit', type=int, default=limit_default, 
		help='The number of results which should be included in the query.')
	subparser.add_argument('--offset', dest='offset', type=int, default=0,
		help='Offset that should be applied to the results set. Can be used to along with --limit to paginate through '
			+ 'large queries.')

def datamodel_query_output_options(subparser):
	'''	Add CLI options for data model query output: collapse
	'''
	subparser.add_argument('--collapse', dest='collapse', default=False, action='store_true',
		help='Only retrieve resource IDs, rather than the full details, of the query results.')


def datamodel_dicom_modify_options(subparser):
	'''	Add CLI options for the data model modify command
	'''
	subparser.add_argument('--modify', '-M', dest='modify', nargs='+', type=argparse_keyval, required=True, metavar='KEY=VALUE',
		help='DICOM tags which should be modified. Values should be structured as key=value pairs. '
			+ 'Example: -M SeriesDescription="FastMRI sequence 5".')
	subparser.add_argument('--remove-tags', dest='remove_tags', nargs='+', type=str, metavar='TagName',
		help='DICOM tags which should be removed from the study.')
	subparser.add_argument('--private-creator', dest='private_creator', type=str, default=None, 
		help='The organization (private creator) to associate with the request. If modifying private DICOM tags, ' 
			+'the private creator must be specified along with the tags that will be updated.')


def dicom_download_options(subparser):
	'''	CLI options for download of DICOM data
	'''
	subparser.add_argument('--download-folder', '-d', dest='download_folder', type=argparse_type_directory, required=True,
		help='Folder to which files should be downloaded.')
	subparser.add_argument('--archive-type', dest='archive_type', choices=FILEARCHIVE_TYPE_SUPPORTED, 
		default=FILEARCHIVE_TYPE_ZIPARCHIVE, help='Style of archive format to retrieve from the server. '
			+ 'If "dicomdir" is used, all image files will be in a single folder and a DICOMDIR meta file '
			+ 'will be included at the root of the archive. If "zip" is used, image data will be separated '
			+ 'into subfolders by patient, study, and series.')
	subparser.add_argument('--extract', '-x', dest='extract', default=False, action='store_true',
		help='Extract archive contents rather than saving data as a zip file.')


def dicomweb_remote_server_operation_options(subparser):
	'''	CLI options for DICOMWeb remote server operations
	'''
	subparser.add_argument('--remote-server', dest='remote_server', type=str, 
		help='Remote DICOMweb server. May also be provided as the SONADOR_DICOMWEB_SERVER shell environment variable.',
		default=os.environ.get('SONADOR_DICOMWEB_SERVER'))


def background_job_options(subparser):
	'''	CLI options for tasks which submit operations to the background queue
	'''
	subparser.add_argument('--wait', dest='synchronous', action='store_true', default=False,
		help='When true, causes the script to wait until the server returns the result of the job. '
			+ 'Note: There is a risk of connection timeout if the requested resources include a large number of images.')


def general_transfer_options(subparser):
	'''	CLI options for tasks associated with the transfer of resources
	'''
	subparser.add_argument('--sync', dest='dicomweb_sync',
		action='store_true', default=False, help='Before queing for transfer, check the destination to determine if '
			+ 'the resource is already present.')


def study_transfer_options(subparser):
	'''	CLI options for study focused tasks which transfer data
	'''
	subparser.add_argument('--transfer-series', dest='dicomweb_study_series_transfer', 
		action='store_true', default=False, help='Modifies how the transfer job is submitted to the server. '
			+ 'When enabled, Orthanc sends each series as its own job rather than a single job for the entire study. '
			+ 'This helps improve transfer reliability for connections susceptible to timeouts.')


def resource_cache_options(subparser):
	'''	CLI options for resource cache
	'''
	subparser.add_argument('--rapid-lookup', dest='rapid_lookup', action='store_true', default=False,
		help='When present, uses the Sonador resource cache for query operations. The resource cache provides '
			+ 'a set of tables for series, studyies, and patients that are more optimized than the traditional '
			+ '/tools/find endpoint of Orthanc. Requests using the resource cache may be as much as 100 times faster '
			+ 'than the equivalent query to the primary Orthanc database. Responses from the resource cache will only contain '
			+ 'headers which are part of Orthanc instance MainDicomTags or ExtraMainDicomTags.')
	