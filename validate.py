import six, os, logging, argparse


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
