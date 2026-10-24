"""`vizard-mcp install`: register the server in Claude Code and Claude Desktop config files."""

import argparse
import difflib
import json
import os
import shutil
import sys
import tempfile
import time

DEFAULT_NAME = 'vizard'


class ConfigError(RuntimeError):
	"""A config file cannot be read or understood; nothing is written."""


def claude_code_config_path():
	base = os.environ.get('CLAUDE_CONFIG_DIR') or os.path.expanduser('~')
	return os.path.join(base, '.claude.json')


def claude_desktop_config_path():
	appdata = os.environ.get('APPDATA')
	if appdata:
		return os.path.join(appdata, 'Claude', 'claude_desktop_config.json')
	if sys.platform == 'darwin':
		return os.path.join(os.path.expanduser('~'), 'Library', 'Application Support', 'Claude',
		                    'claude_desktop_config.json')
	return os.path.join(os.path.expanduser('~'), '.config', 'Claude', 'claude_desktop_config.json')


def _claude_code_marker():
	return os.path.join(os.environ.get('CLAUDE_CONFIG_DIR') or os.path.expanduser('~'),
	                    '' if os.environ.get('CLAUDE_CONFIG_DIR') else '.claude')


# label, config file, and a folder whose existence shows the client is installed
TARGETS = {
	'claude-code': ('Claude Code', claude_code_config_path, _claude_code_marker),
	'claude-desktop': ('Claude Desktop', claude_desktop_config_path, lambda: os.path.dirname(claude_desktop_config_path())),
}


def read_config(path):
	"""(data, text) of a JSON config file; ({}, '') if it is missing or empty. Raises ConfigError."""
	try:
		with open(path, 'r', encoding='utf-8') as f:
			text = f.read()
	except FileNotFoundError:
		return {}, ''
	except OSError as e:
		raise ConfigError('cannot read %s: %s' % (path, e))
	if not text.strip():
		return {}, text
	try:
		data = json.loads(text)
	except ValueError as e:
		raise ConfigError('%s is not valid JSON (%s); fix or remove it, then run again' % (path, e))
	if not isinstance(data, dict):
		raise ConfigError('%s does not hold a JSON object' % path)
	return data, text


def default_command():
	"""(command, args) that start this installation's server: the vizard-mcp script next to
	the running Python, else one on PATH, else `python -m vizard_mcp`."""
	script_dir = os.path.dirname(os.path.abspath(sys.executable))
	for cand in (os.path.join(script_dir, 'Scripts', 'vizard-mcp.exe'), os.path.join(script_dir, 'vizard-mcp.exe'),
	             os.path.join(script_dir, 'vizard-mcp')):
		if os.path.isfile(cand):
			return cand, []
	on_path = shutil.which('vizard-mcp')
	if on_path:
		return os.path.abspath(on_path), []
	return os.path.abspath(sys.executable), ['-m', 'vizard_mcp']


def wanted_entry(target, command, args, existing=None):
	"""The server entry to store. Keys the user added to an existing entry (env, ...) are kept."""
	entry = dict(existing) if isinstance(existing, dict) else {}
	if target == 'claude-code':
		entry['type'] = 'stdio'
	entry['command'] = command
	entry['args'] = list(args)
	if target == 'claude-code':
		entry.setdefault('env', {})
	return entry


def plan(path, target, name, command, args):
	"""What installing into `path` would do: (action, old_text, new_text); action is
	'added', 'updated' or 'unchanged'. Raises ConfigError."""
	data, old_text = read_config(path)
	servers = data.get('mcpServers')
	if servers is None:
		servers = data['mcpServers'] = {}
	elif not isinstance(servers, dict):
		raise ConfigError('"mcpServers" in %s is not an object' % path)
	have = servers.get(name)
	entry = wanted_entry(target, command, args, have)
	if have == entry:
		return 'unchanged', old_text, old_text
	servers[name] = entry
	new_text = json.dumps(data, indent=2, ensure_ascii=False) + ('\n' if old_text.endswith('\n') or not old_text else '')
	return ('updated' if have is not None else 'added'), old_text, new_text


def backup(path):
	stamp = time.strftime('%Y%m%d-%H%M%S')
	dest, n = '%s.bak-%s' % (path, stamp), 1
	while os.path.exists(dest):
		n += 1
		dest = '%s.bak-%s-%d' % (path, stamp, n)
	shutil.copy2(path, dest)
	return dest


def write_atomic(path, text):
	folder = os.path.dirname(path) or '.'
	os.makedirs(folder, exist_ok=True)
	fd, tmp = tempfile.mkstemp(prefix='.vizard-mcp-', suffix='.tmp', dir=folder)
	try:
		with os.fdopen(fd, 'w', encoding='utf-8', newline='') as f:
			f.write(text)
		os.replace(tmp, path)
	except BaseException:
		try:
			os.remove(tmp)
		except OSError:
			pass
		raise


def install_into(target, name, command, args, dry_run, out):
	"""Install into one client's config. Returns True on success."""
	label, path_fn, _ = TARGETS[target]
	path = path_fn()
	try:
		action, old, new = plan(path, target, name, command, args)
	except ConfigError as e:
		out('%s: %s' % (label, e))
		return False
	if action == 'unchanged':
		out('%s: %s is already registered as "%s" (%s). Nothing to do.' % (label, 'vizard-mcp', name, path))
		return True
	verb = 'Would %s' % {'added': 'add', 'updated': 'update'}[action] if dry_run else action.capitalize()
	out('%s: %s "%s" in %s' % (label, verb, name, path))
	if dry_run:
		diff = difflib.unified_diff(old.splitlines(), new.splitlines(), 'current' if old else 'new file',
									'after install', lineterm='', n=2)
		for line in diff:
			out('    ' + line)
		return True
	if old:
		out('  backup: %s' % backup(path))
	try:
		write_atomic(path, new)
	except OSError as e:
		out('%s: could not write %s: %s' % (label, path, e))
		return False
	return True


def main(argv=None, out=print):
	ap = argparse.ArgumentParser(prog='vizard-mcp install',
	                             description='Register the vizard-mcp server in Claude Code and/or Claude Desktop. '
	                                         'The config file is backed up first; running it again changes nothing.')
	ap.add_argument('--claude-code', action='store_true', help='user-level config of Claude Code (~/.claude.json)')
	ap.add_argument('--claude-desktop', action='store_true', help='Claude Desktop (claude_desktop_config.json)')
	ap.add_argument('--name', default=DEFAULT_NAME, help='server name in the config (default: %(default)s)')
	ap.add_argument('--command', help='path of the vizard-mcp program (default: this installation)')
	ap.add_argument('--dry-run', action='store_true', help='show the change, write nothing')
	ns = ap.parse_args(argv)

	targets = [t for t, flag in (('claude-code', ns.claude_code), ('claude-desktop', ns.claude_desktop)) if flag]
	if not targets:
		# no choice made: every client that looks installed (its config file or folder exists)
		targets = [t for t, (_, config, marker) in TARGETS.items()
		           if os.path.exists(config()) or os.path.isdir(marker())]
		if not targets:
			out('Neither Claude Code nor Claude Desktop looks installed (no config file found).\n'
			    'Pass --claude-code and/or --claude-desktop to create the config file anyway.')
			return 1
	if ns.command:
		command, args = os.path.abspath(ns.command), []
	else:
		command, args = default_command()
	out('Server command: %s' % ' '.join([command] + args))
	ok = [install_into(t, ns.name, command, args, ns.dry_run, out) for t in targets]
	if ns.dry_run:
		out('Dry run: nothing was written.')
	elif all(ok):
		out('Done. Restart Claude Desktop, or start a new Claude Code session, then run: vizard-mcp doctor')
	return 0 if all(ok) else 1
