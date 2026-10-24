"""Command line entry point: `vizard-mcp` (the MCP server on stdio) and its subcommands.

Imports of the server (and with it the mcp package) are deferred, so `doctor` and `install`
work even when mcp is missing or broken."""

import os
import sys

from . import __version__

VIZARD_SIDE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'vizard_side')

USAGE = """\
usage: vizard-mcp                      run the MCP server (stdio); this is what Claude starts
       vizard-mcp install [options]    register the server in Claude Code / Claude Desktop (--help)
       vizard-mcp doctor               check the setup and print how to fix problems
       vizard-mcp copy-module DEST     copy vizmcp.py next to a Vizard script
       vizard-mcp --version"""


def main(argv=None):
	argv = list(sys.argv[1:] if argv is None else argv)
	cmd = argv[0] if argv else None
	if cmd in ('--version', '-V'):
		print('vizard-mcp', __version__)
		return
	if cmd in ('-h', '--help'):
		print(USAGE)
		return
	if cmd == 'copy-module' and len(argv) > 1:
		import shutil
		dest = os.path.abspath(argv[1])
		shutil.copy(os.path.join(VIZARD_SIDE, 'vizmcp.py'), dest)
		print('copied vizmcp.py to', dest)
		return
	if cmd == 'doctor':
		from . import doctor
		sys.exit(doctor.main(argv[1:]))
	if cmd == 'install':
		from . import install
		sys.exit(install.main(argv[1:]))
	if cmd is not None:
		print(USAGE, file=sys.stderr)
		sys.exit(2)
	from .server import mcp
	mcp.run()
