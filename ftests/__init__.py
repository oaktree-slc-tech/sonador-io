import os, imp, glob


mpath, mfilename = os.path.split(__file__)
MODULE_FILES = [os.path.split(p)[1] for p in glob.glob(os.path.join(mpath, '*.py'))]

# Remove current file if located in list
if mfilename in MODULE_FILES:
	MODULE_FILES.remove(mfilename)

from .tests_sonadorauth import *
from .tests_sonadorauth_credapi import *
from .tests_sonadorenv import *
from .tests_fhir_addr import *
from .tests_ext_comments import *
