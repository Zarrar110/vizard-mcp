"""vizmcp -- lets an MCP client (Claude, or any other) look inside a running
WorldViz Vizard application and drive it.

Add two lines to any Vizard script:

    import vizmcp
    vizmcp.start()

or launch an unmodified script through the bootstrap:

    winviz.exe vizmcp_boot.py  (with VIZMCP_TARGET=path\\to\\script.py)

The module opens a TCP socket on 127.0.0.1 (port 9877 by default) and speaks
newline-delimited JSON:

    -> {"id": 1, "cmd": "scene_tree", "params": {"depth": 2}}
    <- {"id": 1, "ok": true, "result": {...}}

Vizard's scene graph is not thread-safe, so the socket threads only queue
requests. Every command runs on Vizard's main thread inside a
vizact.onupdate() callback. A command that needs frames to pass (screenshot,
wait, recording, profiling) is written as a generator: it yields once per
frame and returns its result, so the app keeps running meanwhile.

Only standard-library modules and Vizard's own modules are used, so the file
can be copied next to any Vizard script.

Unofficial community project. Not affiliated with or endorsed by WorldViz.
"""

import ast
import collections
import cProfile
import csv
import importlib
import inspect
import io
import json
import math
import os
import pstats
import queue
import socket
import sys
import tempfile
import threading
import time
import traceback

import viz
import vizact

__version__ = '1.0.0'

DEFAULT_PORT = 9877
_PUMP_PRIORITY = viz.PRIORITY_LAST_UPDATE + 1000   # after app logic and vizconnect
_HOLD_PRIORITY = _PUMP_PRIORITY - 1
_PUMP_BUDGET_S = 0.025                              # max main-thread time per frame for new commands
_REPLY_TIMEOUT_S = 600.0
_MAX_TEXT = 20000


# ---------------------------------------------------------------------------
# Console capture
# ---------------------------------------------------------------------------

class _LogBuffer(object):
	"""Ring buffer of console lines, each with a sequence number."""

	def __init__(self, maxlen=5000):
		self._lines = collections.deque(maxlen=maxlen)
		self._seq = 0
		self._partial = {'out': '', 'err': ''}
		self._lock = threading.Lock()

	def write(self, stream, text):
		with self._lock:
			buf = self._partial[stream] + text
			parts = buf.split('\n')
			self._partial[stream] = parts.pop()
			for line in parts:
				self._seq += 1
				self._lines.append((self._seq, stream, time.time(), line.rstrip('\r')))

	def read(self, since=0, limit=200, stream=None, contains=None):
		with self._lock:
			rows = [r for r in self._lines if r[0] > since]
		if stream:
			rows = [r for r in rows if r[1] == stream]
		if contains:
			rows = [r for r in rows if contains.lower() in r[3].lower()]
		rows = rows[-limit:]
		return {
			'last_seq': self._seq,
			'lines': [{'seq': s, 'stream': st, 'text': t} for s, st, _, t in rows],
		}


class _Tee(object):
	"""Forwards writes to the original stream and copies them to the log."""

	def __init__(self, orig, log, name):
		self._orig = orig
		self._log = log
		self._name = name

	def write(self, text):
		try:
			self._log.write(self._name, text)
		except Exception:
			pass
		if self._orig is not None:
			return self._orig.write(text)
		return len(text)

	def flush(self):
		if self._orig is not None:
			self._orig.flush()

	def __getattr__(self, name):
		return getattr(self._orig, name)


_log = _LogBuffer()


def _install_tee():
	if not isinstance(sys.stdout, _Tee):
		sys.stdout = _Tee(sys.stdout, _log, 'out')
	if not isinstance(sys.stderr, _Tee):
		sys.stderr = _Tee(sys.stderr, _log, 'err')


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _jsonable(x, depth=0):
	"""Convert Vizard return values into JSON-safe data."""
	if depth > 6:
		return repr(x)
	if x is None or isinstance(x, (bool, int, str)):
		return x
	if isinstance(x, float):
		if x != x or x in (float('inf'), float('-inf')):
			return repr(x)
		return round(x, 6)
	if isinstance(x, (list, tuple)):
		return [_jsonable(v, depth + 1) for v in x]
	if isinstance(x, dict):
		return {str(k): _jsonable(v, depth + 1) for k, v in x.items()}
	if isinstance(x, viz.VizNode):
		return {'node_id': x.id, 'type': type(x).__name__}
	return _clip(repr(x), 2000)


def _clip(text, n=_MAX_TEXT):
	if len(text) > n:
		return text[:n] + '\n... [%d more characters]' % (len(text) - n)
	return text


def _main_ns():
	return sys.modules['__main__'].__dict__


def _project_dir():
	main = sys.modules['__main__']
	path = getattr(main, '__file__', None) or (sys.argv[0] if sys.argv else '')
	return os.path.normcase(os.path.dirname(os.path.abspath(path))) if path else None


def _in_project(path, project):
	if not path or not project:
		return False
	return os.path.normcase(os.path.abspath(path)).startswith(project + os.sep)


def _is_project_object(value, project, main):
	"""True for instances of classes defined by the app itself (not viz or the stdlib)."""
	mod = sys.modules.get(getattr(type(value), '__module__', None))
	return mod is main or _in_project(getattr(mod, '__file__', None), project)


def _labels(max_depth=4, max_objects=6000):
	"""Map node id -> Python names that refer to the node.

	Walks breadth-first from __main__'s globals through attributes of the
	app's own objects (LAB.render.deck), lists/dicts (cars[2]) and the
	globals of the app's own modules (scene.ground). Shortest names come
	first, so these are what a person reading the script would call the node.
	"""
	out = collections.defaultdict(list)
	project = _project_dir()
	main = sys.modules['__main__']
	seen = set()
	frontier = [(k, v) for k, v in list(_main_ns().items()) if not k.startswith('__')]
	for mname, mod in list(sys.modules.items()):
		if mod is None or mod is main or not _in_project(getattr(mod, '__file__', None), project):
			continue
		short = mname.rsplit('.', 1)[-1]
		frontier += [('%s.%s' % (short, k), v) for k, v in list(vars(mod).items())
		             if not k.startswith('__')]

	for _ in range(max_depth):
		nxt = []
		for name, value in frontier:
			if isinstance(value, viz.VizNode):
				names = out[value.id]
				if len(names) < 6 and name not in names:
					names.append(name)
				continue
			if value is None or isinstance(value, (str, bytes, int, float, bool, type)):
				continue
			if id(value) in seen or len(seen) > max_objects:
				continue
			seen.add(id(value))
			if isinstance(value, (list, tuple)):
				nxt += [('%s[%d]' % (name, i), v) for i, v in enumerate(value[:128])]
			elif isinstance(value, dict):
				nxt += [('%s[%r]' % (name, k), v) for k, v in list(value.items())[:128]
				        if isinstance(k, (str, int))]
			elif not callable(value) and _is_project_object(value, project, main):
				attrs = getattr(value, '__dict__', None)
				if isinstance(attrs, dict):
					nxt += [('%s.%s' % (name, k), v) for k, v in list(attrs.items())[:400]]
		frontier = nxt
	for names in out.values():
		names.sort(key=len)
		del names[4:]
	return out


def _resolve_node(ref):
	"""A node reference is a node id (int) or a Python expression in __main__."""
	if ref is None:
		raise ValueError('node reference is required')
	if isinstance(ref, int) or (isinstance(ref, str) and ref.strip().isdigit()):
		node = viz.VizNode._GetObject(int(ref))
	else:
		node = eval(ref, _main_ns())
	if not isinstance(node, viz.VizNode):
		raise ValueError('%r is not a Vizard node (got %s)' % (ref, type(node).__name__))
	if not node.valid():
		raise ValueError('node %r has been removed' % (ref,))
	return node


def _vec(v, n=3):
	if v is None:
		return None
	v = list(v)
	if len(v) != n:
		raise ValueError('expected %d numbers, got %r' % (n, v))
	return [float(a) for a in v]


def _bbox(node):
	try:
		bb = node.getBoundingBox(viz.ABS_GLOBAL)
		if not bb.valid():
			return None
		return {
			'min': _jsonable([bb.xmin, bb.ymin, bb.zmin]),
			'max': _jsonable([bb.xmax, bb.ymax, bb.zmax]),
			'center': _jsonable(bb.center),
			'size': _jsonable(bb.size),
		}
	except Exception:
		return None


def _eval(code_obj):
	return eval(code_obj, _main_ns())


class _Pending(object):
	"""Older style multi-frame result: poll() returns (done, result) each frame."""

	def __init__(self, poll, timeout_s=30.0):
		self.poll = poll
		self.timeout_s = timeout_s


def _wait_frames(n):
	f0 = viz.getFrameNumber()
	while viz.getFrameNumber() - f0 < n:
		yield


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

class _State(object):
	frame_dt = collections.deque(maxlen=120)
	camera_hold = None
	hold_apply = None
	held_keys = {}
	orig_isdown = None
	shot_count = 0
	work_dir = os.path.join(tempfile.gettempdir(), 'vizmcp')
	quit_requested = False
	watches = collections.OrderedDict()
	recording = None


def _new_path(prefix, ext):
	os.makedirs(_State.work_dir, exist_ok=True)
	_State.shot_count += 1
	return os.path.join(_State.work_dir, '%s_%d_%04d.%s' % (prefix, os.getpid(), _State.shot_count, ext))


def _prune_files(keep=80):
	try:
		files = sorted((os.path.join(_State.work_dir, f) for f in os.listdir(_State.work_dir)
		                if f.endswith('.png')), key=os.path.getmtime)
		for f in files[:-keep]:
			os.remove(f)
	except OSError:
		pass


# ---------------------------------------------------------------------------
# Basic commands
# ---------------------------------------------------------------------------

def cmd_ping(p):
	return {'pong': True, 'vizmcp': __version__}


def _fps():
	dts = list(_State.frame_dt)
	return (len(dts) / sum(dts)) if dts and sum(dts) > 0 else None


def cmd_info(p):
	main = sys.modules['__main__']
	return {
		'vizmcp_version': __version__,
		'vizard_version': viz.version(),
		'python': sys.version.split()[0],
		'script': os.path.abspath(getattr(main, '__file__', '') or sys.argv[0] or ''),
		'cwd': os.getcwd(),
		'frame': viz.getFrameNumber(),
		'time_s': viz.getFrameTime(),
		'fps': _fps(),
		'window_size': _jsonable(viz.window.getSize()),
		'camera': cmd_camera_get({}),
		'camera_held': _State.camera_hold is not None,
		'node_count': len(viz.getNodeList()),
		'vizconnect': 'vizconnect' in sys.modules,
		'watches': {n: w.status() for n, w in _State.watches.items()},
		'recording': _State.recording['path'] if _State.recording else None,
		'commands': sorted(COMMANDS),
	}


def _node_summary(node, labels, with_bbox=False):
	d = {
		'id': node.id,
		'type': type(node).__name__,
		'names': labels.get(node.id, []),
		'visible': bool(node.getVisible()),
		'pos': _jsonable(node.getPosition()),
	}
	if with_bbox:
		d['bbox'] = _bbox(node)
	return d


def cmd_scene_tree(p):
	depth = int(p.get('depth', 3))
	max_nodes = int(p.get('max_nodes', 400))
	only_named = bool(p.get('only_named', False))
	labels = _labels()
	count = [0]
	truncated = [False]

	def walk(node, level):
		if count[0] >= max_nodes:
			truncated[0] = True
			return None
		count[0] += 1
		d = _node_summary(node, labels)
		kids = node.getChildren()
		d['child_count'] = len(kids)
		if level < depth and kids:
			out = []
			for k in kids:
				w = walk(k, level + 1)
				if w is not None and (not only_named or w['names'] or w.get('children')):
					out.append(w)
			d['children'] = out
		return d

	if p.get('root') is not None:
		roots = [_resolve_node(p['root'])]
	else:
		roots = viz.MainScene.getChildren()
	tree = [w for w in (walk(n, 1) for n in roots) if w is not None]
	if only_named:
		tree = [t for t in tree if t['names'] or t.get('children')]
	return {'nodes': tree, 'listed': count[0], 'truncated': truncated[0]}


def cmd_find_nodes(p):
	"""Nodes whose script name contains `pattern` (case-insensitive)."""
	pattern = (p.get('pattern') or '').lower()
	limit = int(p.get('limit', 100))
	found = []
	for nid, names in sorted(_labels().items(), key=lambda kv: kv[1][0]):
		if pattern and not any(pattern in n.lower() for n in names):
			continue
		node = viz.VizNode._GetObject(nid)
		if not node.valid():
			continue
		found.append(_node_summary(node, {nid: names}))
		if len(found) >= limit:
			break
	return {'nodes': found, 'truncated': len(found) >= limit}


def cmd_node_info(p):
	node = _resolve_node(p.get('node'))
	labels = _labels()
	d = _node_summary(node, labels, with_bbox=True)
	d.update({
		'pos_global': _jsonable(node.getPosition(viz.ABS_GLOBAL)),
		'euler': _jsonable(node.getEuler()),
		'euler_global': _jsonable(node.getEuler(viz.ABS_GLOBAL)),
		'scale': _jsonable(node.getScale()),
		'parents': [{'id': n.id, 'names': labels.get(n.id, [])} for n in node.getParents()],
		'children': [{'id': n.id, 'type': type(n).__name__, 'names': labels.get(n.id, [])}
		             for n in node.getChildren()[:200]],
	})
	try:
		d['subnode_count'] = node.getNodeCount()
	except Exception:
		pass
	if p.get('subnode_names'):
		limit = int(p.get('limit', 200))
		names = node.getNodeNames()
		d['subnode_names'] = names[:limit]
		d['subnode_names_truncated'] = len(names) > limit
	return d


def cmd_set_node(p):
	node = _resolve_node(p.get('node'))
	mode = viz.ABS_GLOBAL if p.get('space') == 'global' else viz.ABS_PARENT
	if p.get('position') is not None:
		node.setPosition(_vec(p['position']), mode)
	if p.get('euler') is not None:
		node.setEuler(_vec(p['euler']), mode)
	if p.get('scale') is not None:
		node.setScale(_vec(p['scale']))
	if p.get('visible') is not None:
		node.visible(bool(p['visible']))
	if p.get('color') is not None:
		node.color(_vec(p['color']))
	if p.get('alpha') is not None:
		node.alpha(float(p['alpha']))
	return cmd_node_info({'node': node.id})


class _Capture(io.TextIOBase):
	def __init__(self, orig):
		self.buf = io.StringIO()
		self.orig = orig

	def write(self, text):
		self.buf.write(text)
		return self.orig.write(text)

	def flush(self):
		self.orig.flush()


def cmd_run_code(p):
	code = p.get('code', '')
	ns = _main_ns()
	tree = ast.parse(code, '<vizmcp>', 'exec')
	last_expr = None
	if tree.body and isinstance(tree.body[-1], ast.Expr):
		last_expr = ast.Expression(tree.body.pop().value)
	out, err = _Capture(sys.stdout), _Capture(sys.stderr)
	old = sys.stdout, sys.stderr
	sys.stdout, sys.stderr = out, err
	value = None
	error = None
	try:
		exec(compile(tree, '<vizmcp>', 'exec'), ns)
		if last_expr is not None:
			value = eval(compile(last_expr, '<vizmcp>', 'eval'), ns)
	except Exception:
		error = traceback.format_exc()
	finally:
		sys.stdout, sys.stderr = old
	res = {
		'stdout': _clip(out.buf.getvalue()),
		'stderr': _clip(err.buf.getvalue()),
		'value': _clip(repr(value)) if last_expr is not None and error is None else None,
	}
	if error:
		res['error'] = _clip(error)
	return res


def cmd_get_log(p):
	return _log.read(since=int(p.get('since', 0)), limit=int(p.get('limit', 200)),
	                 stream=p.get('stream'), contains=p.get('contains'))


def cmd_quit(p):
	_State.quit_requested = True
	return {'quitting': True}


def cmd_reload_module(p):
	"""importlib.reload() one of the app's own modules. Objects created before the
	reload keep their old classes and functions; only new lookups see new code."""
	name = p.get('module')
	mod = sys.modules.get(name)
	if mod is None:
		raise ValueError('module %r is not imported' % name)
	if not _in_project(getattr(mod, '__file__', None), _project_dir()):
		raise ValueError('module %r is not part of the app folder; refusing to reload it' % name)
	importlib.reload(mod)
	return {'reloaded': name, 'file': mod.__file__}


# ---------------------------------------------------------------------------
# Camera
# ---------------------------------------------------------------------------

def cmd_camera_get(p):
	v = viz.MainView
	return {'position': _jsonable(v.getPosition()), 'euler': _jsonable(v.getEuler())}


def _set_hold(apply):
	"""Re-apply `apply` every frame after the app and vizconnect moved the view."""
	if _State.camera_hold is not None:
		_State.camera_hold.remove()
		_State.camera_hold = None
	_State.hold_apply = apply
	if apply is not None:
		_State.camera_hold = vizact.onupdate(_HOLD_PRIORITY, apply)


def _view_apply(position=None, euler=None, look_at=None):
	pos, eul, look = _vec(position), _vec(euler), _vec(look_at)

	def apply():
		v = viz.MainView
		if pos is not None:
			v.setPosition(pos)
		if look is not None:
			v.lookAt(look)
		elif eul is not None:
			v.setEuler(eul)
	return apply


def cmd_camera_set(p):
	apply = _view_apply(p.get('position'), p.get('euler'), p.get('look_at'))
	apply()
	_set_hold(apply if p.get('hold') else None)
	return dict(cmd_camera_get({}), held=_State.camera_hold is not None)


def cmd_camera_release(p):
	_set_hold(None)
	return {'held': False}


# ---------------------------------------------------------------------------
# Seeing: screenshots, views over space and time, picking
# ---------------------------------------------------------------------------

def _capture(path=None):
	"""Generator: capture the next rendered frame to a PNG and return its info."""
	if not path:
		_prune_files()
		path = _new_path('shot', 'png')
	path = os.path.abspath(path)
	if os.path.exists(path):
		os.remove(path)
	viz.window.screenCapture(path)
	f0 = viz.getFrameNumber()
	last = -1
	while True:
		yield
		if os.path.exists(path) and viz.getFrameNumber() - f0 >= 2:
			size = os.path.getsize(path)
			if size > 0 and size == last:
				return {'path': path, 'bytes': size, 'frame': viz.getFrameNumber(),
				        'time_s': round(viz.getFrameTime(), 3),
				        'window_size': _jsonable(viz.window.getSize())}
			last = size


def cmd_screenshot(p):
	return _capture(p.get('path'))


def _orbit_views(p):
	if p.get('orbit') is not None:
		node = _resolve_node(p['orbit'])
		bb = node.getBoundingBox(viz.ABS_GLOBAL)
		if not bb.valid():
			raise ValueError('node %r has no bounding box to orbit' % p['orbit'])
		center, radius = list(bb.center), 0.5 * max(bb.size)
	else:
		center, radius = _vec(p['target']), float(p.get('radius', 5.0))
	count = max(1, min(12, int(p.get('count', 4))))
	elev = math.radians(float(p.get('elevation_deg', 25)))
	dist = float(p.get('distance') or max(radius * 2.8, 1.0))
	yaw0 = float(p.get('start_yaw_deg', 45))
	views = []
	for k in range(count):
		yaw_deg = (yaw0 + 360.0 * k / count) % 360.0
		yaw = math.radians(yaw_deg)
		pos = [center[0] + dist * math.cos(elev) * math.sin(yaw),
		       center[1] + dist * math.sin(elev),
		       center[2] + dist * math.cos(elev) * math.cos(yaw)]
		views.append({'position': pos, 'look_at': center, 'label': 'from yaw %d deg' % round(yaw_deg)})
	return views


def cmd_multi_view(p):
	"""Capture several viewpoints, then give the view back exactly as it was."""
	views = p.get('views') or _orbit_views(p)
	settle = int(p.get('settle_frames', 3))
	v = viz.MainView
	prev_apply, prev_pos, prev_eul = _State.hold_apply, v.getPosition(), v.getEuler()
	shots = []
	try:
		for view in views:
			apply = _view_apply(view.get('position'), view.get('euler'), view.get('look_at'))
			apply()
			_set_hold(apply)
			yield from _wait_frames(settle)
			info = yield from _capture()
			info['view'] = _jsonable(view)
			shots.append(info)
	finally:
		_set_hold(prev_apply)
		v.setPosition(prev_pos)
		v.setEuler(prev_eul)
	return {'shots': shots}


def cmd_filmstrip(p):
	"""Capture `count` frames `interval_s` apart, to see motion over time."""
	count = max(1, min(16, int(p.get('count', 6))))
	interval = max(0.0, float(p.get('interval_s', 0.5)))
	t0 = time.time()
	shots = []
	for i in range(count):
		while time.time() - t0 < i * interval:
			yield
		info = yield from _capture()
		info['t_rel_s'] = round(time.time() - t0, 3)
		shots.append(info)
	return {'shots': shots}


def cmd_record_video(p):
	"""Record the window to an AVI file for `seconds` (Vizard's built-in recorder)."""
	seconds = float(p.get('seconds', 5))
	path = os.path.abspath(p.get('path') or _new_path('video', 'avi'))
	if _State.recording:
		raise ValueError('already recording to %s' % _State.recording['path'])
	viz.window.startRecording(path)
	_State.recording = {'path': path}
	t0, f0 = time.time(), viz.getFrameNumber()
	try:
		while time.time() - t0 < seconds:
			yield
	finally:
		viz.window.stopRecording()
		_State.recording = None
	frames = viz.getFrameNumber() - f0
	last = -1
	for _ in range(600):
		yield
		size = os.path.getsize(path) if os.path.exists(path) else 0
		if size > 0 and size == last:
			break
		last = size
	return {'path': path, 'bytes': os.path.getsize(path) if os.path.exists(path) else 0,
	        'seconds': round(time.time() - t0, 2), 'frames': frames}


def _to_screen(point):
	"""World point -> [x, y] in screenshot coordinates (0..1, origin top-left), or None."""
	s = viz.MainWindow.worldToScreen(point)
	if s is None or len(s) < 3:
		return None
	x, y, depth = s[0], s[1], s[2]     # depth: metres along the view axis, negative behind the camera
	if depth <= 0.0 or not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
		return None
	return [round(x, 4), round(1.0 - y, 4)]


def cmd_project(p):
	"""Where world points / node centers appear on screen (0..1, origin top-left)."""
	out = []
	for pt in p.get('points') or []:
		out.append({'point': _jsonable(pt), 'screen': _to_screen(_vec(pt))})
	labels = _labels() if p.get('nodes') else {}
	for ref in p.get('nodes') or []:
		node = _resolve_node(ref)
		bb = node.getBoundingBox(viz.ABS_GLOBAL)
		c = list(bb.center) if bb.valid() else node.getPosition(viz.ABS_GLOBAL)
		out.append({'node': node.id, 'names': labels.get(node.id, []), 'point': _jsonable(c),
		            'screen': _to_screen(c)})
	return {'items': out}


# 2D GUI lives in screen space; its bounding box is not a world position.
_SCREEN_TYPES = tuple(t for t in (getattr(viz, 'VizGUIObject', None), getattr(viz, 'VizGUICanvas', None)) if t)


def cmd_screen_labels(p):
	"""Named nodes that are on screen, with their screen position, for annotation."""
	limit = int(p.get('limit', 30))
	max_size = float(p.get('max_size_m', 1e9))
	world_roots = set(n.id for n in viz.MainScene.getChildren())

	def in_world(node):
		"""Rendered in the 3D scene: under a MainScene root, with no hidden ancestor."""
		cur = node
		for _ in range(64):
			if not cur.getVisible():
				return False
			if cur.id in world_roots:
				return True
			parents = cur.getParents()
			if not parents:
				return False
			cur = parents[0]
		return False

	max_screen = float(p.get('max_screen_fraction', 0.85))
	near = float(p.get('ignore_within_m', 0.5))
	eye = viz.MainView.getPosition()
	win = viz.MainWindow
	items = []
	skipped = collections.Counter()
	for nid, names in _labels().items():
		node = viz.VizNode._GetObject(nid)
		if not node.valid() or not node.getVisible() or isinstance(node, _SCREEN_TYPES):
			continue
		if not in_world(node):
			skipped['hidden or screen-space'] += 1
			continue
		bb = node.getBoundingBox(viz.ABS_GLOBAL)
		if not bb.valid() or max(bb.size) > max_size:
			continue
		center = list(bb.center)
		sc = _to_screen(center)
		if sc is None:
			continue
		if _dist(center, eye) < near:
			skipped['on the camera'] += 1
			continue
		# Scenery-sized groups (terrain, water, "all trees") have their center in an
		# arbitrary spot; labelling it would point at nothing in particular.
		corners = [win.worldToScreen([x, y, z]) for x in (bb.xmin, bb.xmax)
		           for y in (bb.ymin, bb.ymax) for z in (bb.zmin, bb.zmax)]
		if any(c[2] <= 0 for c in corners):
			skipped['larger than the view'] += 1
			continue
		xs, ys = [c[0] for c in corners], [c[1] for c in corners]
		if max(xs) - min(xs) > max_screen or max(ys) - min(ys) > max_screen:
			skipped['larger than the view'] += 1
			continue
		items.append({'id': nid, 'name': names[0], 'screen': sc, 'size_m': round(max(bb.size), 3),
		              'distance_m': round(_dist(center, eye), 2)})
	items.sort(key=lambda it: (len(it['name']), it['name']))
	return {'items': items[:limit], 'total_on_screen': len(items), 'skipped': dict(skipped)}


def _dist(a, b):
	return math.sqrt(sum((a[i] - b[i]) ** 2 for i in range(3)))


def cmd_pick(p):
	"""What is under screen point x, y (0..1 from the top-left of the screenshot)?"""
	x, y = float(p['x']), float(p['y'])
	near = float(p.get('ignore_within_m', 0.5))
	line = viz.MainWindow.screenToWorld([x, 1.0 - y])
	eye = viz.MainView.getPosition()
	hits = [h for h in viz.MainScene.intersect(line.begin, line.end, all=True) or []
	        if getattr(h, 'valid', False)]
	hits.sort(key=lambda h: _dist(h.point, eye))
	# Skip what sits on the camera itself, e.g. a vizconnect avatar's head.
	skipped = [h for h in hits if _dist(h.point, eye) < near]
	hits = [h for h in hits if _dist(h.point, eye) >= near]
	if not hits:
		return {'hit': False, 'x': x, 'y': y, 'ignored_near_camera': len(skipped)}
	info = hits[0]
	node = info.object
	labels = _labels()
	res = {'hit': True, 'x': x, 'y': y, 'point': _jsonable(info.point),
	       'distance_m': round(_dist(info.point, eye), 3),
	       'normal': _jsonable(info.normal), 'subnode_name': _jsonable(info.name)}
	if skipped:
		res['ignored_near_camera'] = len(skipped)
	if isinstance(node, viz.VizNode) and node.valid():
		res['node'] = {'id': node.id, 'type': type(node).__name__, 'names': labels.get(node.id, [])}
		# The nearest named ancestor tells which part of the app this belongs to.
		cur, hops = node, 0
		while cur is not None and hops < 32 and not labels.get(cur.id):
			parents = cur.getParents()
			cur = parents[0] if parents else None
			hops += 1
		if cur is not None and labels.get(cur.id):
			res['named_ancestor'] = {'id': cur.id, 'names': labels[cur.id], 'hops': hops}
	return res


# ---------------------------------------------------------------------------
# Driving: keys and mouse
# ---------------------------------------------------------------------------

def _norm_key(k):
	return k.lower() if isinstance(k, str) else k


def _ensure_isdown_patch():
	if _State.orig_isdown is not None:
		return
	_State.orig_isdown = viz.key.isDown

	def isDown(key, *a, **kw):
		if _norm_key(key) in _State.held_keys:
			return True
		return _State.orig_isdown(key, *a, **kw)

	viz.key.isDown = isDown


def _key_value(k):
	"""'w' stays 'w'; 'KEY_UP' or 'viz.KEY_UP' becomes viz.KEY_UP."""
	if isinstance(k, str) and len(k) > 1:
		name = k.split('.')[-1]
		if hasattr(viz, name):
			return getattr(viz, name)
	return k


def cmd_send_key(p):
	key = _key_value(p.get('key'))
	frames = max(1, int(p.get('hold_frames', 1)))
	_ensure_isdown_patch()
	_State.held_keys[_norm_key(key)] = True
	viz.sendEvent(viz.KEYDOWN_EVENT, key)
	f0 = viz.getFrameNumber()
	try:
		yield from _wait_frames(frames)
	finally:
		_State.held_keys.pop(_norm_key(key), None)
		viz.sendEvent(viz.KEYUP_EVENT, key)
	return {'key': _jsonable(key), 'frames': viz.getFrameNumber() - f0}


def _client_pixel(x, y, width, height):
	"""0..1 (origin top-left) -> pixel inside a client area of width x height."""
	x, y = float(x), float(y)
	if not (-1e-6 <= x <= 1 + 1e-6 and -1e-6 <= y <= 1 + 1e-6):
		raise ValueError('x and y must be 0..1 from the top-left of the window, got (%s, %s)' % (x, y))
	if width < 1 or height < 1:
		raise RuntimeError('the Vizard window has no client area (minimised?)')
	return (min(width - 1, max(0, int(round(x * (width - 1))))),
	        min(height - 1, max(0, int(round(y * (height - 1))))))


def cmd_click(p):
	"""Click with the real OS mouse at x, y (0..1 of the window's client area, from the
	top-left). Brings the window to the foreground if it is not there already, refuses if
	another window covers that point, and puts the cursor back after."""
	import ctypes
	from ctypes import wintypes
	user32 = ctypes.windll.user32
	user32.WindowFromPoint.argtypes = [wintypes.POINT]
	user32.WindowFromPoint.restype = wintypes.HWND
	user32.GetAncestor.argtypes = [wintypes.HWND, ctypes.c_uint]
	user32.GetAncestor.restype = wintypes.HWND
	user32.GetForegroundWindow.restype = wintypes.HWND
	user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
	user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
	user32.SetForegroundWindow.argtypes = [wintypes.HWND]
	user32.IsIconic.argtypes = [wintypes.HWND]
	user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]

	flags = {'left': (0x0002, 0x0004), 'right': (0x0008, 0x0010), 'middle': (0x0020, 0x0040)}
	button = p.get('button', 'left')
	if button not in flags:
		raise ValueError('button must be left, right or middle')
	down, up = flags[button]
	_client_pixel(p['x'], p['y'], 2, 2)     # reject bad coordinates before touching the window
	hwnd = wintypes.HWND(viz.window.getHandle())
	root = user32.GetAncestor(hwnd, 2)      # GA_ROOT

	was_foreground = user32.GetForegroundWindow() == root
	if not was_foreground:
		if user32.IsIconic(root):
			user32.ShowWindow(root, 9)      # SW_RESTORE
		user32.SetForegroundWindow(root)
		for attempt in range(2):
			for _ in range(30):
				if user32.GetForegroundWindow() == root:
					break
				yield
			if user32.GetForegroundWindow() == root or attempt:
				break
			# Windows can refuse a focus change from a background process; a tap on Alt lifts that.
			user32.keybd_event(0x12, 0, 0, 0)
			user32.keybd_event(0x12, 0, 2, 0)
			user32.SetForegroundWindow(root)
	yield from _wait_frames(2)              # let the window settle at its (possibly new) size

	rect = wintypes.RECT()
	user32.GetClientRect(hwnd, ctypes.byref(rect))
	px, py = _client_pixel(p['x'], p['y'], rect.right - rect.left, rect.bottom - rect.top)
	pt = wintypes.POINT(px, py)
	user32.ClientToScreen(hwnd, ctypes.byref(pt))
	old = wintypes.POINT()
	user32.GetCursorPos(ctypes.byref(old))

	under = user32.WindowFromPoint(pt)
	if not under or user32.GetAncestor(under, 2) != root:
		raise RuntimeError('another window covers the Vizard window at that point; not clicking'
		                   ' (foreground before the click: %s)' % was_foreground)
	user32.SetCursorPos(pt.x, pt.y)
	yield from _wait_frames(2)
	try:
		for _ in range(2 if p.get('double') else 1):
			user32.mouse_event(down, 0, 0, 0, 0)
			yield from _wait_frames(int(p.get('hold_frames', 2)))
			user32.mouse_event(up, 0, 0, 0, 0)
			yield from _wait_frames(2)
	finally:
		if p.get('restore_cursor', True):
			user32.SetCursorPos(old.x, old.y)
	return {'clicked': [pt.x, pt.y], 'client_pixel': [px, py], 'client_size': [rect.right - rect.left, rect.bottom - rect.top],
	        'button': button, 'was_foreground': was_foreground}


# ---------------------------------------------------------------------------
# Time: waiting, watching values, profiling
# ---------------------------------------------------------------------------

def cmd_wait(p):
	frames = int(p.get('frames', 0))
	seconds = float(p.get('seconds', 0))
	f0, t0 = viz.getFrameNumber(), time.time()
	while viz.getFrameNumber() - f0 < frames or time.time() - t0 < seconds:
		yield
	return {'frames': viz.getFrameNumber() - f0, 'seconds': round(time.time() - t0, 3)}


def cmd_wait_until(p):
	"""Run until the Python condition is true in the app (checked every frame)."""
	cond = compile(p['condition'], '<condition>', 'eval')
	limit = float(p.get('limit_s', 60))
	f0, t0 = viz.getFrameNumber(), time.time()
	while True:
		value = _eval(cond)
		if value:
			return {'met': True, 'value': _clip(repr(value), 500), 'frames': viz.getFrameNumber() - f0,
			        'seconds': round(time.time() - t0, 3), 'app_time_s': round(viz.getFrameTime(), 3)}
		if time.time() - t0 > limit:
			return {'met': False, 'value': _clip(repr(value), 500), 'frames': viz.getFrameNumber() - f0,
			        'seconds': round(time.time() - t0, 3)}
		yield


class _Watch(object):
	"""Samples Python expressions in the app every N frames."""

	def __init__(self, name, exprs, every, max_samples, until):
		self.name = name
		self.exprs = [(label, compile(src, '<watch %s>' % label, 'eval')) for label, src in exprs.items()]
		self.sources = dict(exprs)
		self.every = max(1, every)
		self.max_samples = max_samples
		self.until = compile(until, '<until>', 'eval') if until else None
		self.until_src = until
		self.frame0 = viz.getFrameNumber()
		self.t0 = viz.getFrameTime()
		self.columns = None
		self.rows = []
		self.active = True
		self.stop_reason = None

	def _flatten(self, values):
		cols, vals = [], []
		for (label, _), v in zip(self.exprs, values):
			if isinstance(v, (list, tuple)):
				axes = 'xyzw' if len(v) <= 4 else None
				for i, c in enumerate(v):
					cols.append('%s.%s' % (label, axes[i] if axes else i))
					vals.append(float(c))
			else:
				cols.append(label)
				vals.append(float(v))
		return cols, vals

	def sample(self):
		if (viz.getFrameNumber() - self.frame0) % self.every:
			return
		try:
			cols, vals = self._flatten([_eval(c) for _, c in self.exprs])
		except Exception as e:
			self.stop('error: %s: %s' % (type(e).__name__, e))
			return
		if self.columns is None:
			self.columns = cols
		elif cols != self.columns:
			self.stop('error: the shape of a watched value changed (%s -> %s)' % (self.columns, cols))
			return
		self.rows.append([round(viz.getFrameTime() - self.t0, 5), viz.getFrameNumber()] + vals)
		if len(self.rows) >= self.max_samples:
			self.stop('max_samples reached')
		elif self.until is not None:
			try:
				if _eval(self.until):
					self.stop('until condition met: %s' % self.until_src)
			except Exception as e:
				self.stop('error in until: %s' % e)

	def stop(self, reason):
		if self.active:
			self.active = False
			self.stop_reason = reason

	def status(self):
		return {'active': self.active, 'samples': len(self.rows), 'stop_reason': self.stop_reason}

	def stats(self):
		out = {}
		for j, col in enumerate(self.columns or []):
			vals = [r[2 + j] for r in self.rows]
			times = [r[0] for r in self.rows]
			i_min = min(range(len(vals)), key=vals.__getitem__)
			i_max = max(range(len(vals)), key=vals.__getitem__)
			out[col] = {'n': len(vals), 'first': vals[0], 'last': vals[-1],
			            'min': vals[i_min], 't_min': times[i_min],
			            'max': vals[i_max], 't_max': times[i_max],
			            'mean': sum(vals) / len(vals)}
		return out


def cmd_watch_start(p):
	name = p.get('name') or 'watch%d' % (len(_State.watches) + 1)
	exprs = p.get('expressions')
	if not exprs:
		raise ValueError('expressions must map labels to Python expressions')
	if isinstance(exprs, list):
		exprs = {e: e for e in exprs}
	w = _Watch(name, exprs, int(p.get('every_frames', 1)), int(p.get('max_samples', 20000)),
	           p.get('until'))
	_State.watches[name] = w
	w.sample()
	if w.rows == [] and not w.active:
		raise ValueError('first sample failed: %s' % w.stop_reason)
	return {'name': name, 'columns': w.columns, 'status': w.status()}


def _downsample(rows, n):
	if len(rows) <= n:
		return rows
	step = (len(rows) - 1) / float(n - 1)
	return [rows[int(round(i * step))] for i in range(n)]


def cmd_watch_read(p):
	name = p.get('name')
	if name not in _State.watches:
		raise ValueError('no watch named %r (have: %s)' % (name, ', '.join(_State.watches) or 'none'))
	w = _State.watches[name]
	if p.get('stop'):
		w.stop('stopped by request')
	res = {'name': name, 'status': w.status(), 'expressions': w.sources,
	       'columns': ['t', 'frame'] + (w.columns or []), 'stats': _jsonable(w.stats()) if w.rows else {}}
	max_points = int(p.get('max_points', 200))
	if max_points > 0 and w.rows:
		res['samples'] = _jsonable(_downsample(w.rows, max_points))
	if w.rows:
		path = os.path.abspath(p.get('csv_path') or os.path.join(_State.work_dir, 'watch_%s.csv' % name))
		os.makedirs(os.path.dirname(path), exist_ok=True)
		with open(path, 'w', newline='') as f:
			wr = csv.writer(f)
			wr.writerow(res['columns'])
			wr.writerows(w.rows)
		res['csv'] = path
	if p.get('delete'):
		del _State.watches[name]
	return res


def _write_rows(path, fmt, columns, rows, meta):
	"""Write rows (lists matching columns) as CSV, or JSON {meta..., columns, rows}."""
	path = os.path.abspath(path)
	os.makedirs(os.path.dirname(path), exist_ok=True)
	if fmt == 'csv':
		with open(path, 'w', newline='') as f:
			wr = csv.writer(f)
			wr.writerow(columns)
			wr.writerows(rows)
	else:
		doc = dict(meta, columns=columns, rows=rows)
		with open(path, 'w') as f:
			json.dump(doc, f, indent=1)
	return path


def cmd_watch_export(p):
	"""Write a watch's full data to a CSV or JSON file, optionally only some columns."""
	name = p.get('name')
	if name not in _State.watches:
		raise ValueError('no watch named %r (have: %s)' % (name, ', '.join(_State.watches) or 'none'))
	w = _State.watches[name]
	if not w.rows:
		raise ValueError('watch %r has no samples yet' % name)
	path = p.get('path')
	if not path:
		raise ValueError('path is required')
	fmt = (p.get('format') or os.path.splitext(path)[1].lstrip('.') or 'csv').lower()
	if fmt not in ('csv', 'json'):
		raise ValueError('format must be csv or json (from the extension or the format argument), got %r' % fmt)
	all_cols = ['t', 'frame'] + list(w.columns)
	cols = list(p.get('columns') or all_cols)
	missing = [c for c in cols if c not in all_cols]
	if missing:
		raise ValueError('unknown columns %s; have %s' % (missing, all_cols))
	idx = [all_cols.index(c) for c in cols]
	rows = [[r[i] for i in idx] for r in w.rows]
	meta = {'watch': name, 'expressions': w.sources, 'stop_reason': w.stop_reason, 'samples': len(rows)}
	path = _write_rows(path, fmt, cols, rows, meta)
	return {'path': path, 'format': fmt, 'columns': cols, 'rows': len(rows), 'bytes': os.path.getsize(path)}


def cmd_watch_list(p):
	return {n: dict(w.status(), expressions=w.sources) for n, w in _State.watches.items()}


def cmd_profile(p):
	"""Profile all Python running on the main thread for `frames` frames."""
	frames = max(10, int(p.get('frames', 120)))
	top = int(p.get('top', 25))
	dts = []
	prof = cProfile.Profile()
	t0 = time.time()
	prof.enable()
	try:
		for _ in range(frames):
			yield
			dts.append(viz.getFrameElapsed())
	finally:
		prof.disable()
	wall = time.time() - t0
	stats = pstats.Stats(prof)
	here = os.path.normcase(os.path.abspath(__file__))
	rows = []
	py_total = 0.0
	for (fname, line, func), (cc, nc, tt, ct, _) in stats.stats.items():
		py_total += tt
		if not fname.startswith('<') and os.path.normcase(os.path.abspath(fname)) == here:
			continue    # vizmcp's own pump
		rows.append({'function': func, 'file': fname, 'line': line, 'calls_per_frame': round(nc / float(frames), 2),
		             'self_ms_per_frame': round(tt / frames * 1000, 3),
		             'total_ms_per_frame': round(ct / frames * 1000, 3)})
	key = 'total_ms_per_frame' if p.get('sort') == 'total' else 'self_ms_per_frame'
	rows.sort(key=lambda r: r[key], reverse=True)
	dts_sorted = sorted(dts)
	mean = sum(dts) / len(dts)
	return {
		'frames': frames,
		'wall_s': round(wall, 3),
		'frame_ms': {'mean': round(mean * 1000, 2),
		             'p95': round(dts_sorted[int(0.95 * (len(dts) - 1))] * 1000, 2),
		             'max': round(dts_sorted[-1] * 1000, 2)},
		'fps': round(1.0 / mean, 1) if mean > 0 else None,
		'python_ms_per_frame': round(py_total / frames * 1000, 2),
		'note': 'python_ms_per_frame far below frame_ms means the frame is bound by rendering/GPU '
		        'or vsync, not by Python code.',
		'top': rows[:top],
	}


# ---------------------------------------------------------------------------
# vizconnect
# ---------------------------------------------------------------------------

def _describe(obj):
	d = {'type': type(obj).__name__}
	for getter in ('getNode3d', 'getRaw'):
		try:
			inner = getattr(obj, getter)()
			if inner is not None:
				obj = inner
				break
		except Exception:
			pass
	try:
		d['position'] = _jsonable(obj.getPosition(viz.ABS_GLOBAL))
	except Exception:
		try:
			d['position'] = _jsonable(obj.getPosition())
		except Exception:
			pass
	try:
		d['euler'] = _jsonable(obj.getEuler(viz.ABS_GLOBAL))
	except Exception:
		pass
	return d


def cmd_vizconnect_info(p):
	vc = sys.modules.get('vizconnect')
	if vc is None:
		return {'loaded': False}
	out = {'loaded': True}
	for kind, fn in (('trackers', 'getTrackerDict'), ('transports', 'getTransportDict'),
	                 ('displays', 'getDisplayDict'), ('inputs', 'getInputDict'),
	                 ('avatars', 'getAvatarDict'), ('tools', 'getToolDict'),
	                 ('events', 'getEventDict')):
		try:
			out[kind] = {str(k): _describe(v) for k, v in getattr(vc, fn)().items()}
		except Exception as e:
			out[kind] = {'error': str(e)}
	return out


# ---------------------------------------------------------------------------
# Scene statistics
# ---------------------------------------------------------------------------

# Accessors tried in order; the first one that works on a node is used. Which of these
# a given Vizard version provides is checked with the vizard_api tool, and the ones that
# do not exist are reported as unavailable instead of being guessed at.
_TRIANGLE_ACCESSORS = ('getTriangleCount', 'getPolyCount')
_TEXTURE_ACCESSORS = ('getTexture',)
_TEXTURE_LIST = 'getTextureList'
_LIGHT_LIST = 'getLightList'


def _try_accessors(obj, names):
	"""(value, accessor name) of the first accessor in names that exists and works."""
	for name in names:
		fn = getattr(obj, name, None)
		if fn is None:
			continue
		try:
			return fn(), name
		except Exception:
			continue
	return None, None


def _node_cost(node):
	"""Per-node numbers for scene_stats: dict(triangles, subnodes, texture) with None where unknown."""
	tris, tri_src = _try_accessors(node, _TRIANGLE_ACCESSORS)
	try:
		subnodes = int(node.getNodeCount())
	except Exception:
		subnodes = None
	tex, tex_src = _try_accessors(node, _TEXTURE_ACCESSORS)
	return {'triangles': int(tris) if isinstance(tris, (int, float)) else None, 'subnodes': subnodes,
	        'texture': None if tex is None else getattr(tex, 'id', id(tex)),
	        'sources': {'triangles': tri_src, 'texture': tex_src}}


def _count_list(fn_name):
	fn = getattr(viz, fn_name, None)
	if fn is None:
		return None
	try:
		return len(fn())
	except Exception:
		return None


def cmd_scene_stats(p):
	"""Counts that explain a slow scene: nodes, visible nodes, textures, triangles, lights, heavy nodes."""
	top = max(0, int(p.get('top', 10)))
	nodes = [n for n in viz.getNodeList() if n.valid()]
	by_type = collections.Counter(type(n).__name__ for n in nodes)
	visible = 0
	costs = []
	textures = set()
	tri_sources = set()
	for n in nodes:
		vis = bool(n.getVisible())
		visible += vis
		c = _node_cost(n)
		c['node'], c['visible'] = n, vis
		costs.append(c)
		if c['texture'] is not None:
			textures.add(c['texture'])
		if c['sources']['triangles']:
			tri_sources.add(c['sources']['triangles'])

	have_tris = any(c['triangles'] is not None for c in costs)
	tri_all = sum(c['triangles'] or 0 for c in costs) if have_tris else None
	tri_visible = sum(c['triangles'] or 0 for c in costs if c['visible']) if have_tris else None
	# A node's triangles may already include its children's; the totals then double count.
	notes = []
	if not have_tris:
		notes.append('no triangle accessor worked on this Vizard (tried %s); heavy nodes are ranked by '
		             'sub-node count' % ', '.join(_TRIANGLE_ACCESSORS))
	else:
		notes.append('triangles are approximate: group nodes may already include their children')
	tex_total = _count_list(_TEXTURE_LIST)
	lights = _count_list(_LIGHT_LIST)
	lights_by_type = sum(v for k, v in by_type.items() if 'light' in k.lower())
	if lights is None:
		lights = lights_by_type if lights_by_type else None
		if lights is None:
			notes.append('light count unavailable (no %s and no light nodes in the node list)' % _LIGHT_LIST)

	key = (lambda c: c['triangles'] or 0) if have_tris else (lambda c: c['subnodes'] or 0)
	heavy = sorted(costs, key=key, reverse=True)[:top]
	labels = _labels() if heavy else {}
	return {
		'nodes': len(nodes),
		'visible_nodes': visible,
		'by_type': dict(by_type.most_common()),
		'textures': tex_total if tex_total is not None else (len(textures) if textures else None),
		'textures_source': _TEXTURE_LIST if tex_total is not None else ('node textures' if textures else None),
		'triangles': tri_all,
		'triangles_visible': tri_visible,
		'lights': lights,
		'ranked_by': 'triangles' if have_tris else 'subnodes',
		'top_nodes': [{'id': c['node'].id, 'type': type(c['node']).__name__,
		               'names': labels.get(c['node'].id, []), 'visible': c['visible'],
		               'triangles': c['triangles'], 'subnodes': c['subnodes']} for c in heavy],
		'notes': notes,
	}


# ---------------------------------------------------------------------------
# Command table
# ---------------------------------------------------------------------------

COMMANDS = {
	'ping': cmd_ping,
	'info': cmd_info,
	'scene_tree': cmd_scene_tree,
	'find_nodes': cmd_find_nodes,
	'node_info': cmd_node_info,
	'set_node': cmd_set_node,
	'run_code': cmd_run_code,
	'get_log': cmd_get_log,
	'reload_module': cmd_reload_module,
	'screenshot': cmd_screenshot,
	'multi_view': cmd_multi_view,
	'filmstrip': cmd_filmstrip,
	'record_video': cmd_record_video,
	'project': cmd_project,
	'screen_labels': cmd_screen_labels,
	'pick': cmd_pick,
	'camera_get': cmd_camera_get,
	'camera_set': cmd_camera_set,
	'camera_release': cmd_camera_release,
	'send_key': cmd_send_key,
	'click': cmd_click,
	'wait': cmd_wait,
	'wait_until': cmd_wait_until,
	'watch_start': cmd_watch_start,
	'watch_read': cmd_watch_read,
	'watch_list': cmd_watch_list,
	'watch_export': cmd_watch_export,
	'scene_stats': cmd_scene_stats,
	'profile': cmd_profile,
	'vizconnect_info': cmd_vizconnect_info,
	'quit': cmd_quit,
}


def register(name, func):
	"""Add an app-specific command. func(params) runs on the main thread and
	returns JSON-safe data, or is a generator that yields once per frame and
	returns its result."""
	COMMANDS[name] = func


# ---------------------------------------------------------------------------
# Main-thread pump
# ---------------------------------------------------------------------------

class _Job(object):
	def __init__(self, cmd, params):
		self.cmd = cmd
		self.params = params or {}
		self.done = threading.Event()
		self.response = None

	def finish(self, ok, result=None, error=None):
		self.response = {'ok': ok}
		if ok:
			self.response['result'] = _jsonable(result)
		else:
			self.response['error'] = error
		self.done.set()


_jobs = queue.Queue()
_tasks = []     # [job, generator, deadline]


def _from_pending(pend):
	def gen():
		while True:
			done, result = pend.poll()
			if done:
				return result
			yield
	return gen()


def _step(task):
	"""Advance one multi-frame command. Returns True when it has finished."""
	job, gen, deadline = task
	try:
		next(gen)
	except StopIteration as e:
		job.finish(True, e.value)
		return True
	except Exception:
		job.finish(False, error=traceback.format_exc())
		return True
	if time.time() > deadline:
		gen.close()
		job.finish(False, error='%s timed out' % job.cmd)
		return True
	return False


def _pump():
	try:
		_State.frame_dt.append(viz.getFrameElapsed())
	except Exception:
		pass

	for w in list(_State.watches.values()):
		if w.active:
			w.sample()

	for task in list(_tasks):
		if _step(task):
			_tasks.remove(task)

	t0 = time.time()
	while time.time() - t0 < _PUMP_BUDGET_S:
		try:
			job = _jobs.get_nowait()
		except queue.Empty:
			break
		func = COMMANDS.get(job.cmd)
		if func is None:
			job.finish(False, error='unknown command %r (known: %s)' % (job.cmd, ', '.join(sorted(COMMANDS))))
			continue
		try:
			result = func(job.params)
		except Exception:
			job.finish(False, error=traceback.format_exc())
			continue
		if isinstance(result, _Pending):
			result = _from_pending(result)
		if inspect.isgenerator(result):
			task = [job, result, time.time() + float(job.params.get('timeout_s', 300))]
			if not _step(task):
				_tasks.append(task)
		else:
			job.finish(True, result)

	if _State.quit_requested:
		_State.quit_requested = False
		viz.quit()


# ---------------------------------------------------------------------------
# Socket server (background threads; never touches viz)
# ---------------------------------------------------------------------------

class _Server(object):
	def __init__(self, host, port, token):
		self.host, self.port, self.token = host, port, token
		self.sock = None
		self.running = False

	def start(self):
		self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
		self.sock.bind((self.host, self.port))
		self.sock.listen(4)
		self.running = True
		t = threading.Thread(target=self._accept_loop, name='vizmcp-accept', daemon=True)
		t.start()

	def stop(self):
		self.running = False
		try:
			self.sock.close()
		except Exception:
			pass

	def _accept_loop(self):
		while self.running:
			try:
				conn, _ = self.sock.accept()
			except OSError:
				break
			threading.Thread(target=self._client, args=(conn,), name='vizmcp-client', daemon=True).start()

	def _client(self, conn):
		f = conn.makefile('rb')
		try:
			for raw in f:
				if not raw.strip():
					continue
				reply = self._handle(raw)
				conn.sendall((json.dumps(reply) + '\n').encode('utf-8'))
		except (OSError, ValueError):
			pass
		finally:
			try:
				conn.close()
			except OSError:
				pass

	def _handle(self, raw):
		try:
			req = json.loads(raw.decode('utf-8'))
		except ValueError as e:
			return {'id': None, 'ok': False, 'error': 'bad JSON: %s' % e}
		rid = req.get('id')
		if self.token and req.get('token') != self.token:
			return {'id': rid, 'ok': False, 'error': 'bad or missing token'}
		job = _Job(req.get('cmd'), req.get('params'))
		_jobs.put(job)
		if not job.done.wait(_REPLY_TIMEOUT_S):
			return {'id': rid, 'ok': False,
			        'error': 'Vizard did not finish the command within %ds (is the app frozen or paused in a dialog?)' % _REPLY_TIMEOUT_S}
		return dict(job.response, id=rid)


_server = None
_pump_event = None


def start(port=None, host='127.0.0.1', token=None, quiet=False):
	"""Start the vizmcp server. Safe to call more than once."""
	global _server, _pump_event
	if _server is not None:
		return _server.port
	port = int(port or os.environ.get('VIZMCP_PORT', DEFAULT_PORT))
	token = token or os.environ.get('VIZMCP_TOKEN') or None
	_install_tee()
	_server = _Server(host, port, token)
	try:
		_server.start()
	except OSError as e:
		_server = None
		print('[vizmcp] could not listen on %s:%d (%s) -- is another Vizard app using it?' % (host, port, e))
		return None
	_pump_event = vizact.onupdate(_PUMP_PRIORITY, _pump)
	vizact.onexit(stop)
	if not quiet:
		print('[vizmcp] %s listening on %s:%d' % (__version__, host, port))
	return port


def stop():
	global _server, _pump_event
	if _server is not None:
		_server.stop()
		_server = None
	if _pump_event is not None:
		_pump_event.remove()
		_pump_event = None
