import six, os, logging, argparse

from client.utils.urls import validate_url
from client.utils.logs import LOGGING_LEVELS


def argparse_type_directory(dpath):
	'''	Ensure that the provided path is a directory and that it exists
	'''
	if os.path.isdir(dpath):
		return dpath

	raise argparse.ArgumentTypeError('Provide a valid directory, %s does not exist' % dpath)


def argparse_keyval(s):
	''' Convert a a string into a key/value pair
	'''
	if not '=' in s:
		raise argparse.ArgumentTypeError('Invalid value "%s", items must be a key=value string.' % s)

	items = s.split('=')
	key = items[0].strip()

	# re-join any text which might have included '='
	if len(items) > 1:
		value = '='.join(items[1:])
	else: value = ''

	return (key, value)


def validate_sonador_connection_args(args, exitcode):
	'''	Ensure user-provided Sonador arguments are sane
	'''
	if not args.apitoken:

		if args.accessid is None:
			logger.error(six.text_type('The import client requires a Sonador Access ID. See --help for details.'))
			exitcode = 1
		if args.secretkey is None:
			logger.error(six.text_type('The import client requires a Sonador Secret Key. See --help for details.'))
			exitcode = 1

		# Verify endpoint value and structure
		if not args.endpoint:
			logger.error(six.text_type('A Sonador endpoint is required.'))
			exitcode = 1

	# Verify Sonador URL endpoint
	if args.endpoint:

		try: validate_url(args.endpoint)
		except ValueError as err:
			logger.error(
				six.text_type('Malformed endpoint URL "%s", a valid http URL is required. Example: http://domain.com:port'
					% args.endpoint))
			exitcode = 1
	
	return exitcode