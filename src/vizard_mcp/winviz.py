"""Finding the Vizard install. Kept free of the mcp package so `vizard-mcp doctor` works without it."""

import glob
import os
import re


def locate():
	"""Full path of winviz.exe: $VIZARD_EXE, else the newest under Program Files\\WorldViz\\Vizard*. None if absent."""
	exe = os.environ.get('VIZARD_EXE')
	if exe and os.path.isfile(exe):
		return exe
	roots = [os.environ.get('ProgramFiles', r'C:\Program Files'),
			 os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)')]
	found = []
	for root in roots:
		found += glob.glob(os.path.join(root, 'WorldViz', 'Vizard*', 'bin', 'winviz.exe'))
	return sorted(found)[-1] if found else None


def version_from_path(exe):
	"""Vizard's version as far as its install folder says: .../Vizard8/bin/winviz.exe -> '8'. None if unknown."""
	for part in reversed(os.path.normpath(exe).replace('\\', '/').split('/')):
		m = re.fullmatch(r'Vizard[\s_-]*v?(\d+(?:\.\d+)*)', part, re.IGNORECASE)
		if m:
			return m.group(1)
	return None
