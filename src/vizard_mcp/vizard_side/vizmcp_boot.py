"""Run an unmodified Vizard script with vizmcp enabled.

    set VIZMCP_TARGET=C:\\path\\to\\app.py
    winviz.exe vizmcp_boot.py [script args...]

The target runs as __main__ in this module's namespace, with __file__,
sys.argv, sys.path[0] and the working directory set as if it had been
launched directly, so vizmcp's run_code and node names see the app's own
globals.
"""

import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
if _here not in sys.path:
	sys.path.append(_here)

import vizmcp  # noqa: E402

_target = os.environ.get('VIZMCP_TARGET')
if not _target:
	raise SystemExit('[vizmcp] set VIZMCP_TARGET to the Vizard script to run')
_target = os.path.abspath(_target)
_target_dir = os.path.dirname(_target)

vizmcp.start()

os.chdir(_target_dir)
sys.path.insert(0, _target_dir)
sys.argv = [_target] + sys.argv[1:]
__file__ = _target

with open(_target, 'rb') as _f:
	_code = compile(_f.read(), _target, 'exec')
del _f, _here
exec(_code, globals())
