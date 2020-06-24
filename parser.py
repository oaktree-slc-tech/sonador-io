import os, sys, six, argparse
from collections import OrderedDict

from client.utils.logs import LOGGING_LEVELS
from client.utils.object import pick

from .serialization import OUTPUT_TYPE_TABULATE, OUTPUT_TYPE_CSV, OUTPUT_TYPE_SUPPORTED


# General Commands
SONADOR_COMMAND_LIST = 'list'
SONADOR_COMMAND_DETAILS = 'details'


# Sonador Interface Commands
def add_arguments_for_api_connection(parser):
	'''Add arguments for parser like access-id, secret-key, api-endpoint
	'''
	parser.add_argument('--access-id', dest='accessid', default=os.environ.get('SONADOR_ACCESS_ID'),
		help='''Access ID used for authentication to Sonador. May also be provided 
			as the SONADOR_ACCESS_ID shell environment variable.'''.replace('\t', '').replace('\n', ''))
	parser.add_argument('--secret-key', dest='secretkey', default=os.environ.get('SONADOR_SECRET_KEY'),
		help='''Secret key used for authentication to Sonador. May also be 
			provided as the SONADOR_SECRET_KEY shell environment variable.'''.replace('\t', '').replace('\n', ''))
	parser.add_argument('--api-endpoint', dest='endpoint', default=os.environ.get('SONADOR_URL'),
		help='''Sonador instance to which API requests should be sent (including scheme, port, path). 
			May also be provided as the SONADOR_URL shell environment variable.'''.replace('\t', '').replace('\n', ''))
	parser.add_argument('--api-token', dest='apitoken', default=os.environ.get('SONADOR_APITOKEN'),
		help='''Sonador access token which should be used to make authenticated requests. When present, 
				takes precedence over the access ID and secret key. May also be provided as the
				SONADOR_APITOKEN shell environment variable.'''.replace('\t', '').replace('\n', ''))


def add_arguments_for_verify_ssl(parser):
	'''Add argument for parser like verify-ssl
	'''
	parser.add_argument('--verify-ssl', dest='verifyssl', action='store_true', default=True,
		help='Verify SSL connections')


def add_arguments_for_logging_options(parser):
	'''Add arguments for parser like loglevel, logformat, logname, error-traceback
	'''
	parser.add_argument('--log-level', dest='loglevel', default='info', choices=LOGGING_LEVELS.keys(),
		help='level of detail to show in output log')
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
	subparser.add_argument('--server', '-s', dest='server', type=str, 
		help='Imaging server. May also be provided as the SONADOR_IMAGING_SERVER shell environment variable.',
		default=os.environ.get('SONADOR_IMAGING_SERVER'))


def datamodel_schema_options(subparser):
	'''	Add CLI options for data model operations
	'''
	subparser.add_argument('--include-schema', dest='include_schema', action='store_true', default=False,
		help='Include the schema for the model in the output')
	