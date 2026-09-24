"""Former name of jev_browser_bridge, kept so existing imports keep working.

`import jev_nolayout` and `from jev_nolayout.browser import Browser` both
resolve to the same modules as their jev_browser_bridge counterparts. The
warning is a FutureWarning rather than a DeprecationWarning because it is
meant for the people using the package, and Python only shows the latter to
developers by default.
"""
import importlib
import sys
import warnings

import jev_browser_bridge as _package
from jev_browser_bridge import *  # noqa: F401,F403
from jev_browser_bridge import __all__  # noqa: F401

warnings.warn("jev_nolayout is now jev_browser_bridge; update the import.",
              FutureWarning, stacklevel=2)

for _name in ("agent", "browser", "cli", "evidence", "model", "session"):
    sys.modules[f"{__name__}.{_name}"] = importlib.import_module(f"{_package.__name__}.{_name}")
