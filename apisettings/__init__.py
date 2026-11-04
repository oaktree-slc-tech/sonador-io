from .media import *
from .sr import *
from .display_attrs import *
from .base import *
from .sr.seriestag import *
# `from .base import *` re-exports the data-class module `base.sr` under the name `sr`, shadowing the
# `apisettings.sr` package on this namespace; rebind the name to the package (`from . import sr` would
# return the existing attribute instead of the package)
from importlib import import_module as _import_module
sr = _import_module('.sr', __name__)
from .fhir import SonadorFhirAddress
from .ext import *