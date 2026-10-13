"""Offline index of Vizard's Python API, built by parsing the .py files that ship
with Vizard (no Vizard process needed). Gives exact signatures and docstrings, so
the assistant does not have to guess Vizard function names."""

import ast
import glob
import os

_INDEX = None

# Modules most scripts use come first in search results.
_PRIORITY = ['viz', 'vizact', 'vizshape', 'viztask', 'vizfx', 'vizconnect', 'vizmat', 'vizinfo',
             'vizdlg', 'vizproximity', 'vizcam', 'vizinput', 'vizsonic', 'vizmenu', 'vizhtml']


def vizard_python_dir(winviz):
	return os.path.normpath(os.path.join(os.path.dirname(winviz), '..', 'python'))


def _sig(fn):
	try:
		args = ast.unparse(fn.args)
	except Exception:
		args = '...'
	return '(%s)' % args


def _module_files(root):
	files = glob.glob(os.path.join(root, '*.py'))
	files += glob.glob(os.path.join(root, '*', '__init__.py'))
	return files


def build(root):
	entries = []
	for path in _module_files(root):
		if os.path.basename(path) == '__init__.py':
			mod = os.path.basename(os.path.dirname(path))
		else:
			mod = os.path.splitext(os.path.basename(path))[0]
		try:
			with open(path, 'r', encoding='utf-8-sig', errors='replace') as f:
				tree = ast.parse(f.read())
		except (SyntaxError, ValueError):
			continue
		for node in tree.body:
			if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith('_'):
				entries.append({'name': '%s.%s' % (mod, node.name), 'kind': 'function',
				                'signature': _sig(node), 'doc': ast.get_docstring(node) or '',
				                'module': mod, 'file': path, 'line': node.lineno})
			elif isinstance(node, ast.ClassDef) and not node.name.startswith('_'):
				bases = [ast.unparse(b) for b in node.bases] if hasattr(ast, 'unparse') else []
				entries.append({'name': '%s.%s' % (mod, node.name), 'kind': 'class',
				                'signature': '(%s)' % ', '.join(bases), 'doc': ast.get_docstring(node) or '',
				                'module': mod, 'file': path, 'line': node.lineno})
				for item in node.body:
					if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
							not item.name.startswith('_') or item.name == '__init__'):
						entries.append({'name': '%s.%s.%s' % (mod, node.name, item.name), 'kind': 'method',
						                'signature': _sig(item), 'doc': ast.get_docstring(item) or '',
						                'module': mod, 'file': path, 'line': item.lineno})
			elif isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
				name = node.targets[0].id
				if name.isupper() and isinstance(node.value, ast.Constant):
					entries.append({'name': '%s.%s' % (mod, name), 'kind': 'constant',
					                'signature': ' = %r' % (node.value.value,), 'doc': '',
					                'module': mod, 'file': path, 'line': node.lineno})
	return entries


def get_index(root):
	global _INDEX
	if _INDEX is None:
		_INDEX = build(root)
	return _INDEX


def _rank(e, q):
	name = e['name'].lower()
	last = name.rsplit('.', 1)[-1]
	mod_rank = _PRIORITY.index(e['module']) if e['module'] in _PRIORITY else len(_PRIORITY)
	if last == q:
		score = 0
	elif last.startswith(q):
		score = 1
	elif q in last:
		score = 2
	elif q in name:
		score = 3
	else:
		score = 4      # docstring match
	return (score, mod_rank, len(name))


def search(root, query, limit=40, kinds=None):
	q = query.lower().strip()
	idx = get_index(root)
	hits = []
	for e in idx:
		if kinds and e['kind'] not in kinds:
			continue
		if q in e['name'].lower() or (len(q) > 3 and q in e['doc'].lower()):
			hits.append(e)
	hits.sort(key=lambda e: _rank(e, q))
	return hits[:limit]


def lookup(root, name):
	idx = get_index(root)
	exact = [e for e in idx if e['name'] == name]
	if exact:
		return exact
	return [e for e in idx if e['name'].endswith('.' + name)][:10]
