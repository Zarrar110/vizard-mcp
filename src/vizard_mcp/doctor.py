"""`vizard-mcp doctor`: check the install and say how to fix what is wrong.

Everything here is local: files, environment, importlib metadata and one connection to
127.0.0.1. It never touches the network. Kept free of the mcp package so it still runs when
mcp is the thing that is broken.
"""

import importlib.metadata
import os
import re
import shutil
import socket
import sys
from collections import namedtuple

from . import __version__, install, video, winviz
from .client import DEFAULT_PORT, VizardClient

PASS, WARN, FAIL = 'PASS', 'WARN', 'FAIL'
Check = namedtuple('Check', 'status name detail hint')

MIN_PYTHON = (3, 10)
MIN_MCP, MAX_MCP = (2, 3), (3, 0)
MIN_PILLOW = (10, 0)


def _ver_tuple(text):
	nums = re.findall(r'\d+', text or '')[:2]
	return tuple(int(n) for n in nums) + (0,) * (2 - len(nums))


def check_python():
	v = sys.version_info
	text = 'Python %d.%d.%d' % v[:3]
	if v[:2] >= MIN_PYTHON:
		return Check(PASS, 'Python', text, '')
	return Check(FAIL, 'Python', text, 'vizard-mcp needs Python %d.%d or newer outside Vizard.' % MIN_PYTHON)


def _package_check(dist, label, low, high=None):
	try:
		ver = importlib.metadata.version(dist)
	except importlib.metadata.PackageNotFoundError:
		return Check(FAIL, label, 'not installed', 'pip install --upgrade vizard-mcp')
	t = _ver_tuple(ver)
	if t < low or (high is not None and t >= high):
		want = '>=%d.%d' % low + (',<%d.%d' % high if high else '')
		return Check(WARN, label, '%s %s' % (dist, ver), 'vizard-mcp is tested with %s %s; pip install "%s%s"'
		             % (dist, want, dist, want))
	return Check(PASS, label, '%s %s' % (dist, ver), '')


def check_winviz():
	exe = winviz.locate()
	if not exe:
		return Check(FAIL, 'Vizard (winviz.exe)', 'not found',
		             'Install WorldViz Vizard 8, or set VIZARD_EXE to the full path of winviz.exe '
		             '(launch_app and vizard_api need it).')
	ver = winviz.version_from_path(exe)
	return Check(PASS, 'Vizard (winviz.exe)', '%s%s' % (exe, ' (Vizard %s folder)' % ver if ver else ''), '')


def check_port():
	try:
		port = int(os.environ.get('VIZMCP_PORT', DEFAULT_PORT))
	except ValueError:
		return Check(FAIL, 'vizmcp port', 'VIZMCP_PORT=%r is not a number' % os.environ.get('VIZMCP_PORT'),
		             'Set VIZMCP_PORT to a port number, or remove it to use %d.' % DEFAULT_PORT)
	client = VizardClient(port=port)
	try:
		if client.ping():
			return Check(PASS, 'vizmcp port', 'port %d: a vizmcp app is answering' % port, '')
	finally:
		client.close()
	try:
		socket.create_connection((client.host, port), timeout=1.0).close()
	except OSError:
		return Check(PASS, 'vizmcp port', 'port %d is free (no app running; launch_app will start one)' % port, '')
	return Check(WARN, 'vizmcp port', 'port %d is in use, but not by a vizmcp app that answers' % port,
	             'If it is an app of yours, check that VIZMCP_TOKEN matches on both sides. Otherwise set '
	             'VIZMCP_PORT to another port (for the server and the app).')


def check_ffmpeg():
	found = video.find_ffmpeg()
	if found:
		return Check(PASS, 'ffmpeg (MP4 videos)', '%s (from %s)' % found, '')
	return Check(WARN, 'ffmpeg (MP4 videos)', 'not found; record_video would keep the large AVI',
	             'pip install "vizard-mcp[video]" (bundles ffmpeg), or put ffmpeg on PATH, or set VIZMCP_FFMPEG.')


def _is_ours(name, entry):
	if not isinstance(entry, dict):
		return False
	blob = ' '.join([str(entry.get('command', ''))] + [str(a) for a in entry.get('args') or []]).lower()
	return name.lower() in ('vizard', 'vizard-mcp') or 'vizard-mcp' in blob or 'vizard_mcp' in blob


def _registered(path, include_projects):
	"""[(where, name, entry)] of our server in one config file. Raises install.ConfigError."""
	data, _ = install.read_config(path)
	groups = [('', data.get('mcpServers'))]
	if include_projects:
		for proj, body in (data.get('projects') or {}).items():
			if isinstance(body, dict):
				groups.append((' (project %s)' % proj, body.get('mcpServers')))
	return [(where, name, entry) for where, servers in groups if isinstance(servers, dict)
	        for name, entry in servers.items() if _is_ours(name, entry)]


def _command_exists(command):
	return bool(command) and (os.path.isfile(command) or shutil.which(command) is not None)


def check_registration(label, path, include_projects=False):
	hint_install = 'Run: vizard-mcp install'
	try:
		found = _registered(path, include_projects)
	except install.ConfigError as e:
		return [Check(FAIL, '%s registration' % label, str(e), 'Fix the JSON syntax (or move the file away), then run: vizard-mcp install')]
	if not found:
		what = 'no vizard server in %s' % path if os.path.isfile(path) else 'no config file at %s' % path
		return [Check(WARN, '%s registration' % label, what, hint_install + ' (skip if you do not use %s)' % label)]
	checks = []
	for where, name, entry in found:
		command = entry.get('command')
		shown = '"%s"%s -> %s' % (name, where, command)
		if _command_exists(command):
			checks.append(Check(PASS, '%s registration' % label, shown, ''))
		else:
			checks.append(Check(FAIL, '%s registration' % label, shown + ' (this file does not exist)',
			                    'The registered command is gone (moved or uninstalled venv?). Run: vizard-mcp install'))
	return checks


def run_checks():
	checks = [check_python(),
	          _package_check('mcp', 'mcp package', MIN_MCP, MAX_MCP),
	          _package_check('pillow', 'Pillow', MIN_PILLOW),
	          check_winviz(), check_port(), check_ffmpeg()]
	checks += check_registration('Claude Code', install.claude_code_config_path(), include_projects=True)
	checks += check_registration('Claude Desktop', install.claude_desktop_config_path())
	return checks


def render(checks):
	lines = ['vizard-mcp %s doctor' % __version__, '']
	for c in checks:
		lines.append('[%s] %s: %s' % (c.status, c.name, c.detail))
		if c.hint and c.status != PASS:
			lines.append('       fix: %s' % c.hint)
	n = {s: sum(1 for c in checks if c.status == s) for s in (PASS, WARN, FAIL)}
	lines += ['', '%d passed, %d warnings, %d failed' % (n[PASS], n[WARN], n[FAIL])]
	return '\n'.join(lines)


def exit_code(checks):
	return 1 if any(c.status == FAIL for c in checks) else 0


def main(argv=None, out=print):
	if argv:
		out('usage: vizard-mcp doctor')
		return 2
	checks = run_checks()
	out(render(checks))
	return exit_code(checks)
