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
import copy
import cProfile
import csv
import importlib
import inspect
import io
import json
import math
import operator
import os
import pstats
import queue
import re
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
		return float('%.9g' % x)     # keeps small residuals (1e-7 m) that round(x, 6) would zero
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
	vr = None
	snapshots = collections.OrderedDict()


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
	_vr_restore()
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


def _orbit_center(p, default_radius=5.0):
	"""(center, radius) of what to orbit: a node's bounding box, or a world point + radius."""
	if p.get('orbit') is not None:
		node = _resolve_node(p['orbit'])
		bb = node.getBoundingBox(viz.ABS_GLOBAL)
		if not bb.valid():
			raise ValueError('node %r has no bounding box to orbit' % p['orbit'])
		return list(bb.center), 0.5 * max(bb.size)
	return _vec(p['target']), float(p.get('radius') or default_radius)


def _orbit_views(p):
	center, radius = _orbit_center(p)
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


def _record_window(path, seconds):
	"""Generator: record the window to an AVI for `seconds`, wait for the file to stop
	growing, and return what was written."""
	if _State.recording:
		raise ValueError('already recording to %s' % _State.recording['path'])
	viz.window.startRecording(path)
	t0, f0 = time.time(), viz.getFrameNumber()
	_State.recording = {'path': path, 't0': t0}
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


def cmd_record_video(p):
	"""Record the window to an AVI file for `seconds` (Vizard's built-in recorder)."""
	seconds = float(p.get('seconds', 5))
	path = os.path.abspath(p.get('path') or _new_path('video', 'avi'))
	return (yield from _record_window(path, seconds))


# ---------------------------------------------------------------------------
# Camera tours: a path through space and time, for record_tour
# ---------------------------------------------------------------------------

def _smoothstep(u):
	return u * u * (3.0 - 2.0 * u)


def _shortest_delta(a, b):
	"""Signed turn in degrees from angle a to angle b, the short way round."""
	return (b - a + 180.0) % 360.0 - 180.0


def _wrap_angle(a):
	return (a + 180.0) % 360.0 - 180.0


def _blend(vals, i, u, mode):
	"""Point at fraction u of segment i..i+1 of the control points `vals` (lists of floats)."""
	b, c = vals[i], vals[i + 1]
	if mode == 'linear':
		return [x + (y - x) * u for x, y in zip(b, c)]
	if mode == 'smoothstep':
		s = _smoothstep(u)
		return [x + (y - x) * s for x, y in zip(b, c)]
	a = vals[max(i - 1, 0)]
	d = vals[min(i + 2, len(vals) - 1)]
	u2, u3 = u * u, u * u * u
	return [0.5 * (2.0 * q + (-p + r) * u + (2.0 * p - 5.0 * q + 4.0 * r - s) * u2 + (-p + 3.0 * q - 3.0 * r + s) * u3)
	        for p, q, r, s in zip(a, b, c, d)]


def _unwrap(angles):
	"""Make a series of angles continuous by always turning the short way round."""
	out = [angles[0]]
	for a in angles[1:]:
		out.append(out[-1] + _shortest_delta(out[-1], a))
	return out


class _TourPath(object):
	"""A camera pose as a function of time: pose(t) -> {"position": [...], "euler" or "look_at": [...]}."""

	def __init__(self, duration, pose, kind, description):
		self.duration, self.pose, self.kind, self.description = duration, pose, kind, description

	def at(self, t):
		return self.pose(min(max(t, 0.0), self.duration))


def _keyframe_path(frames, duration, interp):
	if interp not in ('catmull', 'smoothstep', 'linear'):
		raise ValueError('interp must be "catmull", "smoothstep" or "linear"')
	if not isinstance(frames, (list, tuple)) or len(frames) < 2:
		raise ValueError('keyframes needs at least 2 entries of {"position", "euler" or "look_at", "t"}')
	modes = set()
	for k, f in enumerate(frames):
		if not isinstance(f, dict) or f.get('position') is None:
			raise ValueError('keyframes[%d] needs a position' % k)
		has_e, has_l = f.get('euler') is not None, f.get('look_at') is not None
		if has_e == has_l:
			raise ValueError('keyframes[%d] needs exactly one of euler or look_at' % k)
		modes.add('euler' if has_e else 'look_at')
	if len(modes) > 1:
		raise ValueError('use euler in every keyframe or look_at in every keyframe, not a mix')
	mode = modes.pop()
	have_t = [f.get('t') is not None for f in frames]
	if any(have_t) and not all(have_t):
		raise ValueError('give t on every keyframe or on none')
	if all(have_t):
		times = [float(f['t']) for f in frames]
		if times[0] < 0 or any(b <= a for a, b in zip(times, times[1:])):
			raise ValueError('keyframe times t must be 0 or more and strictly increasing')
		total = float(duration) if duration else times[-1]
	else:
		if not duration:
			raise ValueError('give duration, or t on every keyframe')
		total = float(duration)
		times = [total * k / (len(frames) - 1) for k in range(len(frames))]
	if total <= 0:
		raise ValueError('duration must be more than 0')
	pos = [_vec(f['position']) for f in frames]
	if mode == 'euler':
		cols = [_unwrap([_vec(f['euler'])[ax] for f in frames]) for ax in range(3)]
		rot = [[cols[0][k], cols[1][k], cols[2][k]] for k in range(len(frames))]
	else:
		rot = [_vec(f['look_at']) for f in frames]

	def pose(t):
		if t <= times[0]:
			i, u = 0, 0.0
		elif t >= times[-1]:
			i, u = len(times) - 2, 1.0
		else:
			i = max(k for k in range(len(times)) if times[k] <= t)
			u = (t - times[i]) / (times[i + 1] - times[i])
		out = {'position': _blend(pos, i, u, interp)}
		r = _blend(rot, i, u, interp)
		out[mode] = [_wrap_angle(a) for a in r] if mode == 'euler' else r
		return out
	return _TourPath(total, pose, 'keyframes', '%d keyframes (%s)' % (len(frames), interp))


def _orbit_path(center, dist, elevation_deg, degrees, start_yaw_deg, duration):
	if duration <= 0:
		raise ValueError('duration must be more than 0')
	if dist <= 0:
		raise ValueError('radius must be more than 0')
	elev = math.radians(elevation_deg)

	def pose(t):
		yaw = math.radians(start_yaw_deg + degrees * t / duration)
		return {'position': [center[0] + dist * math.cos(elev) * math.sin(yaw),
		                     center[1] + dist * math.sin(elev),
		                     center[2] + dist * math.cos(elev) * math.cos(yaw)],
		        'look_at': list(center)}
	return _TourPath(duration, pose, 'orbit', 'orbit of %g degrees at radius %g' % (degrees, dist))


def _tour_path(p):
	duration = p.get('duration')
	if p.get('keyframes') is not None:
		if p.get('orbit') is not None or p.get('target') is not None:
			raise ValueError('give keyframes or an orbit (orbit/target), not both')
		return _keyframe_path(p['keyframes'], duration, p.get('interp') or 'catmull')
	if p.get('orbit') is None and p.get('target') is None:
		raise ValueError('give keyframes, or orbit (a node) or target (a world point) to circle around')
	center, size = _orbit_center(p)
	if p.get('radius'):
		dist = float(p['radius'])
	else:
		dist = max(size * 2.8, 1.0) if p.get('orbit') is not None else size
	return _orbit_path(center, dist, float(p.get('elevation_deg', 25.0)), float(p.get('degrees', 360.0)),
	                   float(p.get('start_yaw_deg', 45.0)), float(duration or 8.0))


def _apply_pose(pose):
	v = viz.MainView
	v.setPosition(pose['position'])
	if pose.get('look_at') is not None:
		v.lookAt(pose['look_at'])
	else:
		v.setEuler(pose['euler'])


_STEP_FRAME = 'frame_%05d.png'
_MAX_STEP_FRAMES = 100000


def _tour_steps(path, p):
	"""Generator: step the view along `path` one output frame at a time. For frame k the held
	pose is the one at t = k / fps; after `settle_frames` rendered frames the window is captured
	to a numbered PNG in a fresh folder. The server encodes the sequence. The app keeps running
	in wall time meanwhile, so its own motion is not stepped. The view is given back afterwards."""
	fps = float(p.get('fps') or 30.0)
	if not 0 < fps <= 240:
		raise ValueError('fps must be more than 0 and at most 240')
	settle = int(p.get('settle_frames', 2))
	if settle < 0:
		raise ValueError('settle_frames must be 0 or more')
	count = max(1, int(round(fps * path.duration)))
	if count > _MAX_STEP_FRAMES:
		raise ValueError('%d frames is too many (limit %d); lower fps or duration' % (count, _MAX_STEP_FRAMES))
	folder = _new_path('tour', 'frames')
	os.makedirs(folder)
	v = viz.MainView
	prev_apply, prev_pos, prev_eul = _State.hold_apply, v.getPosition(), v.getEuler()
	at = {'t': 0.0}

	def apply():
		_apply_pose(path.at(at['t']))
	sizes = []
	t0 = time.time()
	done = 0
	try:
		apply()
		_set_hold(apply)
		yield from _wait_frames(3)
		for k in range(count):
			at['t'] = k / fps
			apply()
			yield from _wait_frames(settle)
			info = yield from _capture(os.path.join(folder, _STEP_FRAME % k))
			if info['window_size'] not in sizes:
				sizes.append(info['window_size'])
			done += 1
	finally:
		_set_hold(prev_apply)
		v.setPosition(prev_pos)
		v.setEuler(prev_eul)
	wall = time.time() - t0
	res = {'mode': 'steps', 'frames_dir': folder, 'frame_pattern': _STEP_FRAME, 'frames': done, 'fps': fps,
	       'settle_frames': settle, 'duration': path.duration, 'wall_s': round(wall, 2),
	       'wall_s_per_frame': round(wall / max(done, 1), 3), 'window_size': sizes[0] if sizes else None,
	       'path_kind': path.kind, 'path_description': path.description}
	if len(sizes) > 1:
		res['warning'] = 'the window changed size while capturing (%s); the video is scaled to the first size' % sizes
	return res


def cmd_record_tour(p):
	"""Move the view along a path while recording, or (dry_run) capture a few frames along it.
	The view is held for the duration and then given back exactly as it was. With mode "steps"
	the path is stepped frame by frame and captured to PNGs instead (see _tour_steps)."""
	mode = p.get('mode') or 'realtime'
	if mode not in ('realtime', 'steps'):
		raise ValueError('mode must be "realtime" or "steps"')
	path = _tour_path(p)
	if mode == 'steps' and not p.get('dry_run'):
		return (yield from _tour_steps(path, p))
	if p.get('dry_run'):
		count = max(2, min(12, int(p.get('count', 6))))
		views = []
		for k in range(count):
			t = path.duration * k / (count - 1)
			views.append(dict(path.at(t), label='t = %.2f s' % t))
		res = yield from cmd_multi_view({'views': views, 'settle_frames': int(p.get('settle_frames', 3))})
		res.update(dry_run=True, duration=path.duration, path=path.description)
		return res
	if _State.recording:
		raise ValueError('already recording to %s' % _State.recording['path'])
	out = os.path.abspath(p.get('out_path') or _new_path('tour', 'avi'))
	v = viz.MainView
	prev_apply, prev_pos, prev_eul = _State.hold_apply, v.getPosition(), v.getEuler()

	def apply():
		rec = _State.recording
		_apply_pose(path.at(time.time() - rec['t0'] if rec else 0.0))
	try:
		apply()
		_set_hold(apply)
		yield from _wait_frames(3)
		res = yield from _record_window(out, path.duration)
	finally:
		_set_hold(prev_apply)
		v.setPosition(prev_pos)
		v.setEuler(prev_eul)
	res.update(duration=path.duration, path_kind=path.kind, path_description=path.description)
	return res


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


def _stats(obj):
	"""Vizard 8's NodeStats (vertexCount, primitiveCount, drawableCount, textureCount,
	textureBytes, ...) of a node or scene, or None where getStats is missing or fails."""
	fn = getattr(obj, 'getStats', None)
	if fn is None:
		return None
	try:
		return fn()
	except Exception:
		return None


def _hidden_roots(costs):
	"""Hidden nodes with no hidden ancestor, so subtracting their subtrees never double counts.
	None when parents cannot be read."""
	hidden = set(c['node'].id for c in costs if not c['visible'])
	roots = []
	for c in costs:
		if c['visible']:
			continue
		try:
			stack, seen, covered = list(c['node'].getParents()), set(), False
			while stack:
				par = stack.pop()
				if par.id in seen:
					continue
				seen.add(par.id)
				if par.id in hidden:
					covered = True
					break
				stack.extend(par.getParents())
		except Exception:
			return None
		if not covered:
			roots.append(c)
	return roots


def _node_cost(node):
	"""Per-node numbers for scene_stats: dict(triangles, subnodes, texture) with None where unknown.
	getStats counts the node's whole subtree; the older accessors are tried where it is missing."""
	st = _stats(node)
	if st is not None:
		tris, tri_src = st.primitiveCount, 'getStats'
	else:
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
	notes = []
	scene = _stats(getattr(viz, 'MainScene', None))
	extra = {}
	if scene is not None:
		# Exact totals from the scene graph; a node's own figure covers its whole subtree.
		tri_all = int(scene.primitiveCount)
		roots = _hidden_roots(costs)
		tri_visible = None if roots is None else tri_all - sum(c['triangles'] or 0 for c in roots)
		extra = {'vertices': int(scene.vertexCount), 'drawables': int(scene.drawableCount),
		         'texture_bytes': int(scene.textureBytes)}
		notes.append('triangles are primitives from viz.MainScene.getStats(); per-node figures include the subtree')
	else:
		tri_all = sum(c['triangles'] or 0 for c in costs) if have_tris else None
		tri_visible = sum(c['triangles'] or 0 for c in costs if c['visible']) if have_tris else None
		# A node's triangles may already include its children's; the totals then double count.
		if not have_tris:
			notes.append('no triangle accessor worked on this Vizard (tried getStats, %s); heavy nodes are '
			             'ranked by sub-node count' % ', '.join(_TRIANGLE_ACCESSORS))
		else:
			notes.append('triangles are approximate: group nodes may already include their children')
	tex_total, tex_src = _count_list(_TEXTURE_LIST), _TEXTURE_LIST
	if tex_total is None and scene is not None:
		tex_total, tex_src = int(scene.textureCount), 'getStats'
	if tex_total is None:
		tex_src = 'node textures' if textures else None
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
		'textures_source': tex_src,
		'triangles': tri_all,
		'triangles_visible': tri_visible,
		'lights': lights,
		'ranked_by': 'triangles' if have_tris else 'subnodes',
		'top_nodes': [{'id': c['node'].id, 'type': type(c['node']).__name__,
		               'names': labels.get(c['node'].id, []), 'visible': c['visible'],
		               'triangles': c['triangles'], 'subnodes': c['subnodes']} for c in heavy],
		'notes': notes,
		**extra,
	}


# ---------------------------------------------------------------------------
# 2D GUI: listing widgets and clicking them by label
# ---------------------------------------------------------------------------

# Widget classes by lower-case name. Anything else that derives from Vizard's GUI base
# classes is still listed, under its own class name.
_GUI_CLASS_KINDS = {
	'vizbutton': 'button', 'vizcheckbox': 'checkbox', 'vizradiobutton': 'radio',
	'vizslider': 'slider', 'viztextbox': 'textbox', 'viztext': 'label',
	'vizprogressbar': 'progress', 'vizdroplist': 'dropdown', 'vizpanel': 'panel',
	'vizguicanvas': 'canvas',
}
_NOT_CLICKABLE = ('label', 'panel', 'canvas', 'progress')
_TEXT_GETTERS = ('getMessage', 'getText', 'getLabel')
_ENABLED_GETTERS = ('getEnabled', 'getEnable', 'isEnabled')
_VALUE_KINDS = ('slider', 'checkbox', 'radio', 'textbox', 'dropdown')
_VALUE_GETTERS = ('getState', 'get', 'getValue')


def _gui_kind(node):
	name = type(node).__name__.lower()
	if name in _GUI_CLASS_KINDS:
		return _GUI_CLASS_KINDS[name]
	if _SCREEN_TYPES and isinstance(node, _SCREEN_TYPES):
		for frag, kind in (('radio', 'radio'), ('check', 'checkbox'), ('button', 'button'),
		                   ('slider', 'slider'), ('textbox', 'textbox'), ('canvas', 'canvas')):
			if frag in name:
				return kind
		return name
	return None


def _gui_place(node, world_roots):
	"""('world' | 'screen', visible). A widget under a node of the 3D scene (a panel on a
	vizconnect wrist or tablet, a world-space canvas) is 'world'; visible means it and every
	ancestor are visible."""
	cur, visible = node, True
	for _ in range(64):
		if not cur.getVisible():
			visible = False
		if cur.id in world_roots:
			return 'world', visible
		parents = cur.getParents()
		if not parents:
			return 'screen', visible
		cur = parents[0]
	return 'screen', visible


def _gui_geometry(node):
	"""Center and size in window coordinates, 0..1 from the top-left. Vizard's own 2D
	coordinates (GUI bounding boxes) have their origin at the bottom-left."""
	try:
		bb = node.getBoundingBox()
		if not bb.valid():
			return None
		c = bb.center
		return {'center': [round(c[0], 4), round(1.0 - c[1], 4)],
		        'size': [round(bb.xmax - bb.xmin, 4), round(bb.ymax - bb.ymin, 4)]}
	except Exception:
		return None


def _gui_widgets():
	"""(screen_widgets, world_widgets): dicts describing every 2D GUI widget in the app."""
	labels = _labels()
	nodes = {}
	getlist = getattr(viz, 'getNodeList', None)
	for n in (getlist() if getlist else []):
		nodes[n.id] = n
	for nid in labels:
		if nid not in nodes:
			node = viz.VizNode._GetObject(nid)
			if node is not None and node.valid():
				nodes[nid] = node
	world_roots = set(n.id for n in viz.MainScene.getChildren())
	screen, world = [], []
	for nid in sorted(nodes):
		node = nodes[nid]
		kind = _gui_kind(node)
		if kind is None or not node.valid():
			continue
		place, visible = _gui_place(node, world_roots)
		is_gui = bool(_SCREEN_TYPES) and isinstance(node, _SCREEN_TYPES)
		text, _ = _try_accessors(node, _TEXT_GETTERS)
		text = text if isinstance(text, str) else None
		enabled, _ = _try_accessors(node, _ENABLED_GETTERS)
		w = {'id': nid, 'names': labels.get(nid, []), 'type': type(node).__name__, 'kind': kind,
		     'text': text, 'visible': visible, 'enabled': enabled if isinstance(enabled, bool) else None}
		if kind in _VALUE_KINDS:
			value, _ = _try_accessors(node, _VALUE_GETTERS)
			if value is not None:
				w['value'] = _jsonable(value)
		if place == 'screen':
			geo = _gui_geometry(node)
			w['center'], w['size'] = (geo['center'], geo['size']) if geo else (None, None)
			c = w['center']
			w['on_screen'] = bool(c) and 0.0 <= c[0] <= 1.0 and 0.0 <= c[1] <= 1.0
			screen.append(w)
		elif is_gui or kind != 'label':      # 3D text is not a GUI widget
			w['world_position'] = _jsonable(node.getPosition(viz.ABS_GLOBAL))
			world.append(w)
	return screen, world


def cmd_gui_list(p):
	"""The app's 2D GUI widgets with text, state and window position (0..1, top-left
	origin, same as screenshots). Widgets in the 3D scene are listed apart and cannot be clicked."""
	screen, world = _gui_widgets()
	kinds = p.get('kinds')
	if kinds:
		screen = [w for w in screen if w['kind'] in kinds]
		world = [w for w in world if w['kind'] in kinds]
	if p.get('visible_only'):
		screen = [w for w in screen if w['visible']]
		world = [w for w in world if w['visible']]
	return {'widgets': screen, 'world_widgets': world,
	        'notes': ['center and size are fractions of the window, origin top-left',
	                  'world_widgets are parented into the 3D scene (e.g. a vizconnect wrist/tablet panel): '
	                  'they are reported, not clickable']}


def _gui_match(candidates, label):
	"""Widgets whose text equals label; else equals it ignoring case; else contains it ignoring case."""
	low = label.lower()
	for test in (lambda t: t == label, lambda t: t.lower() == low, lambda t: low in t.lower()):
		hits = [w for w in candidates if w['text'] and test(w['text'])]
		if hits:
			return hits
	return []


def _gui_brief(ws):
	return '; '.join('id %d %s %r%s' % (w['id'], w['kind'], w['text'] or '',
	                                    '' if w['visible'] else ' (hidden)') for w in ws) or 'none'


def cmd_click_button(p):
	"""Click the visible, enabled 2D widget whose text matches `label`, or has id `id`."""
	label, wid = p.get('label'), p.get('id')
	if label is None and wid is None:
		raise ValueError('give label or id')
	screen, world = _gui_widgets()
	if wid is not None:
		wid = int(wid)
		pool = [w for w in screen if w['id'] == wid]
		if not pool:
			if any(w['id'] == wid for w in world):
				raise ValueError('widget %d is in the 3D scene (world-space panel); only 2D screen '
				                 'widgets can be clicked' % wid)
			raise ValueError('no 2D widget with id %d; widgets: %s' % (wid, _gui_brief(screen)))
	else:
		pool = [w for w in screen if w['kind'] not in _NOT_CLICKABLE]
	# Hidden or disabled widgets never shadow a usable match.
	usable = [w for w in pool if w['visible'] and w['enabled'] is not False and w['on_screen']]
	if wid is None:
		usable = _gui_match(usable, str(label))
		if not usable:
			unusable = _gui_match([w for w in pool if w not in usable], str(label))
			if unusable:
				raise ValueError('widget(s) %s match but are hidden, disabled or off screen' % _gui_brief(unusable))
			raise ValueError('no widget with text matching %r; candidates: %s' % (label, _gui_brief(pool)))
	elif not usable:
		raise ValueError('widget(s) %s match but are hidden, disabled or off screen' % _gui_brief(pool))
	if len(usable) > 1:
		raise ValueError('%r is ambiguous, pass id. Matches: %s' % (label, _gui_brief(usable)))
	w = usable[0]
	x, y = w['center']
	res = yield from cmd_click({'x': min(1.0, max(0.0, x)), 'y': min(1.0, max(0.0, y)),
	                            'button': p.get('button', 'left'), 'double': p.get('double', False)})
	return {'widget': w, 'click': res}


# ---------------------------------------------------------------------------
# vizconnect without a headset
# ---------------------------------------------------------------------------

_MISSING = object()
_POSE_METHODS = ('getPosition', 'getEuler', 'getQuat', 'getMatrix')
_BUTTON_METHODS = ('isButtonDown', 'isButtonUp', 'getButtonState')


def _is_hand(n, side):
	"""left/right hand names in the usual spellings: left_hand, r_hand_tracker, rhand, R_Controller."""
	if n in (side, side[0]):
		return True
	parts = [t for t in re.split(r'[^a-z0-9]+', n) if t]
	handish = ('hand', 'wrist', 'controller', 'touch', 'wand')
	if any(t.startswith(side[0]) and t[1:] in handish for t in parts):    # rhand, lcontroller
		return True
	return (side in n or side[0] in parts) and any(k in n for k in handish)


_VR_ALIASES = {
	'head': lambda n: 'head' in n or 'hmd' in n,
	'left_hand': lambda n: _is_hand(n, 'left'),
	'right_hand': lambda n: _is_hand(n, 'right'),
	'left': lambda n: 'left' in n,
	'right': lambda n: 'right' in n,
}


def _vr_resolve(table, wanted, what):
	"""Key of `table` (vizconnect name -> object) for an exact name, a case-insensitive
	name, or the alias head / left_hand / right_hand."""
	names = [str(k) for k in table]
	wanted = str(wanted)
	hits = [n for n in names if n == wanted] or [n for n in names if n.lower() == wanted.lower()]
	if not hits:
		pred = _VR_ALIASES.get(wanted.lower().replace('-', '_').replace(' ', '_'))
		hits = [n for n in names if pred(n.lower())] if pred else []
	if len(hits) != 1:
		raise ValueError('%s %r %s; vizconnect has: %s' % (
			what, wanted, 'is ambiguous' if hits else 'not found', ', '.join(names) or 'none'))
	return hits[0]


def _vr_token(b):
	if isinstance(b, bool):
		raise ValueError('button must be a name or an index, got %r' % (b,))
	if isinstance(b, int) or (isinstance(b, str) and b.strip().isdigit()):
		return int(b)
	return str(b).strip().lower()


def _vr_matrix(pose):
	m = viz.Matrix()
	m.setEuler(pose['euler'])
	m.setPosition(pose['position'])
	return m


class _VrObject(object):
	"""A vizconnect tracker or input whose methods are replaced on the instance."""

	def __init__(self, key, obj):
		self.key, self.obj, self.saved = key, obj, {}
		self.pose = None
		self.down = {}      # token -> set of equivalent tokens (its name and mapped index)
		self.wrapper = None     # the vizconnect wrapper whose raw object was swapped
		self.orig_raw = None    # what it had before
		self.fake = None        # the stand-in raw: a group node (tracker) or a proxy (input)

	def override(self, name, func):
		d = getattr(self.obj, '__dict__', None)
		if d is None:
			raise ValueError('cannot override %s on %s: no instance dict' % (name, type(self.obj).__name__))
		if name not in self.saved:
			self.saved[name] = d[name] if name in d else _MISSING
		setattr(self.obj, name, func)

	def restore(self):
		for name, old in list(self.saved.items()):
			try:
				if old is _MISSING:
					delattr(self.obj, name)
				else:
					setattr(self.obj, name, old)
			except Exception:
				pass
		self.saved = {}

	def call_original(self, name, *a, **k):
		"""The method as it was before the override: an instance attribute the object had,
		else the class's."""
		old = self.saved.get(name, _MISSING)
		if old is not _MISSING and callable(old):
			return old(*a, **k)
		return getattr(type(self.obj), name)(self.obj, *a, **k)

	def equivalents(self):
		eq = set()
		for toks in self.down.values():
			eq |= toks
		return eq


class _VrSim(object):
	def __init__(self):
		self.trackers = collections.OrderedDict()
		self.inputs = collections.OrderedDict()
		self.hold = None
		self.apply_to_node = True
		self.send_events = True
		self.notes = []

	def tracker(self, key, obj):
		rec = self.trackers.get(key)
		if rec is not None:
			return rec
		rec = _VrObject(key, obj)
		pos, eul = [0.0, 0.0, 0.0], [0.0, 0.0, 0.0]
		for name, dest in (('getPosition', pos), ('getEuler', eul)):
			try:
				dest[:] = [float(v) for v in getattr(obj, name)()]
			except Exception:
				pass
		rec.pose = {'position': pos, 'euler': eul}
		# vizconnect links the raw tracker to the tracker's node every frame, so the pose only
		# reaches the view and the hands if the raw object itself is replaced. Tracker.setRaw
		# re-links it; a plain group then holds the simulated pose.
		if all(hasattr(obj, n) for n in ('setRaw', 'getRaw')) and hasattr(viz, 'addGroup'):
			try:
				fake = viz.addGroup()
				fake.setPosition(pos)
				fake.setEuler(eul)
				rec.orig_raw = obj.getRaw()
				obj.setRaw(fake)
				rec.wrapper, rec.fake = obj, fake
				self.trackers[key] = rec
				return rec
			except Exception as e:
				self.notes.append('%s: setRaw failed (%s: %s); overriding its methods instead'
				                  % (key, type(e).__name__, e))
		for name in _POSE_METHODS:
			orig = getattr(obj, name, None)
			if orig is None:
				continue
			if name == 'getPosition':
				rec.override(name, lambda *a, **k: list(rec.pose['position']))
			elif name == 'getEuler':
				rec.override(name, lambda *a, **k: list(rec.pose['euler']))
			elif hasattr(viz, 'Matrix'):
				rec.override(name, _vr_derived(rec, name, orig))
			else:
				self.notes.append('%s: %s left unchanged (no viz.Matrix)' % (key, name))
		self.trackers[key] = rec
		return rec

	def input(self, key, obj):
		rec = self.inputs.get(key)
		if rec is not None:
			return rec
		# Button queries go to the raw device (wrapper.getRaw()), which is what apps poll.
		raw = None
		try:
			raw = obj.getRaw()
		except Exception:
			pass
		rec = _VrObject(key, raw if raw is not None else obj)
		obj = rec.obj

		def is_down(button, *a, **k):
			if _vr_token(button) in rec.equivalents():
				return True
			try:
				return bool(rec.call_original('isButtonDown', button, *a, **k))
			except Exception:
				return False

		def state(*a, **k):
			try:
				val = rec.call_original('getButtonState', *a, **k)
			except Exception:
				val = None
			ints = [t for t in rec.equivalents() if isinstance(t, int)]
			if isinstance(val, (list, tuple)):
				val = list(val)
				for i in ints:
					if 0 <= i < len(val):
						val[i] = True
				return val
			mask = 0
			for i in ints:
				mask |= 1 << i
			return (val or 0) | mask if isinstance(val, int) or val is None else val

		def mouse_state(*a, **k):
			try:
				val = int(rec.call_original('getState', *a, **k) or 0)
			except Exception:
				val = 0
			for i in rec.equivalents():
				if isinstance(i, int):
					val |= 1 << i
			return val

		funcs = {'isButtonDown': is_down, 'isButtonUp': lambda b, *a, **k: not is_down(b, *a, **k),
		         'getButtonState': state, 'getState': mouse_state}
		present = [n for n in funcs if hasattr(obj, n)]
		try:
			for n in present:
				rec.override(n, funcs[n])
		except (ValueError, AttributeError, TypeError):
			# A built-in device object takes no new attributes: give the wrapper a proxy instead.
			rec.restore()
			wrapper = self._wrapper_of(key)
			if wrapper is not None and hasattr(wrapper, 'setRaw') and raw is not None:
				rec.orig_raw, rec.wrapper = raw, wrapper
				rec.fake = _VrRawProxy(raw, dict((n, funcs[n]) for n in present))
				wrapper.setRaw(rec.fake)
				self.notes.append('%s: device object is read-only, so vizconnect now reads a proxy; code '
				                  'that kept a reference to the original device does not see presses'
				                  % key)
			else:
				self.notes.append('%s: device object is read-only; only events are sent' % key)
		if not present:
			self.notes.append('%s: has none of %s; only events are sent' % (key, ', '.join(_BUTTON_METHODS)))
		self.inputs[key] = rec
		return rec

	def _wrapper_of(self, key):
		vc = sys.modules.get('vizconnect')
		try:
			return {str(k): v for k, v in vc.getInputDict().items()}.get(key)
		except Exception:
			return None

	def apply_nodes(self):
		for rec in self.trackers.values():
			try:
				if rec.fake is not None:
					rec.fake.setPosition(rec.pose['position'])
					rec.fake.setEuler(rec.pose['euler'])
					continue
				node = rec.obj.getNode3d()
				if node is not None:
					node.setPosition(rec.pose['position'])
					node.setEuler(rec.pose['euler'])
			except Exception:
				pass

	def start_hold(self):
		if self.hold is None and self.apply_to_node:
			self.hold = vizact.onupdate(_HOLD_PRIORITY, self.apply_nodes)

	def stop_hold(self):
		if self.hold is not None:
			try:
				self.hold.remove()
			except Exception:
				pass
			self.hold = None

	def event(self, rec, kind, token):
		"""Tell the app a sensor button changed, as a real device would."""
		if not self.send_events:
			return None
		const = getattr(viz, 'SENSOR_DOWN_EVENT' if kind == 'down' else 'SENSOR_UP_EVENT', None)
		if const is None:
			return 'viz has no SENSOR_%s_EVENT' % kind.upper()
		# Handlers (vizact.onsensordown, viztask.waitSensorDown) read e.object and e.button,
		# and match e.object against the device they registered, i.e. the original raw device.
		raw = rec.orig_raw if rec.orig_raw is not None else rec.obj
		try:
			viz.sendEvent(const, viz.Event(object=raw, button=token))
		except Exception as e:
			return '%s: %s' % (type(e).__name__, e)
		return None

	def restore(self):
		errors = []
		for rec in self.inputs.values():
			for tok in list(rec.down):
				err = self.event(rec, 'up', tok)
				if err:
					errors.append(err)
		self.stop_hold()
		for rec in list(self.trackers.values()) + list(self.inputs.values()):
			rec.restore()
			_vr_unswap(rec, errors)
		self.trackers.clear()
		self.inputs.clear()
		return errors

	def status(self):
		return {
			'trackers': {k: {'position': _jsonable(r.pose['position']), 'euler': _jsonable(r.pose['euler']),
			                 'overridden': sorted(r.saved), 'raw_swapped': r.fake is not None}
			             for k, r in self.trackers.items()},
			'inputs': {k: {'down': sorted(map(str, r.down)), 'overridden': sorted(r.saved),
			               'raw_swapped': r.fake is not None}
			           for k, r in self.inputs.items()},
		}


class _VrRawProxy(object):
	"""Stands in for a read-only raw input device: the simulated button methods, and everything
	else passed through to the device."""

	def __init__(self, raw, funcs):
		self.__dict__['_raw'] = raw
		self.__dict__.update(funcs)

	def __getattr__(self, name):
		return getattr(self.__dict__['_raw'], name)

	def __setattr__(self, name, value):
		if name in self.__dict__ and name != '_raw':
			self.__dict__[name] = value
		else:
			setattr(self.__dict__['_raw'], name, value)


def _vr_unswap(rec, errors):
	"""Give a vizconnect wrapper its own raw object back and drop the stand-in."""
	if rec.wrapper is None:
		return
	try:
		rec.wrapper.setRaw(rec.orig_raw)
	except Exception as e:
		errors.append('%s: setRaw back failed (%s: %s)' % (rec.key, type(e).__name__, e))
	if rec.fake is not None and hasattr(rec.fake, 'remove') and not isinstance(rec.fake, _VrRawProxy):
		try:
			rec.fake.remove()
		except Exception:
			pass
	rec.wrapper = rec.fake = rec.orig_raw = None


def _vr_derived(rec, name, orig):
	def get(*a, **k):
		try:
			m = _vr_matrix(rec.pose)
			return m if name == 'getMatrix' else m.getQuat()
		except Exception:
			return orig(*a, **k)
	return get


def _vr_restore():
	sim = _State.vr
	_State.vr = None
	return sim.restore() if sim else []


def cmd_vr_simulate(p):
	"""Fake a headset: pose vizconnect trackers and hold input buttons, without hardware.
	Overrides are made on the vizconnect objects themselves and undone by reset, quit or exit."""
	if p.get('reset'):
		errors = _vr_restore()
		return {'reset': True, 'event_errors': errors}
	vc = sys.modules.get('vizconnect')
	if vc is None:
		raise ValueError('vizconnect is not in use by this app (no vizconnect module is imported), '
		                 'so there are no trackers or inputs to simulate')
	trackers = {str(k): v for k, v in vc.getTrackerDict().items()}
	inputs = {str(k): v for k, v in vc.getInputDict().items()}
	sim = _State.vr or _VrSim()
	if p.get('send_events') is not None:
		sim.send_events = bool(p['send_events'])
	if p.get('apply_to_node') is not None:
		sim.apply_to_node = bool(p['apply_to_node'])

	def as_list(v):
		return [] if v is None else ([v] if isinstance(v, (str, int)) else list(v))

	def split(item):
		"""'right_hand:trigger' -> (input key, button); 'trigger' -> (default input, button)."""
		name = p.get('input')
		if isinstance(item, str) and ':' in item:
			name, item = item.split(':', 1)
		if name is None:
			if len(inputs) != 1:
				raise ValueError('which input? pass input=... or "name:button"; vizconnect has: %s'
				                 % (', '.join(inputs) or 'none'))
			name = next(iter(inputs))
		return _vr_resolve(inputs, name, 'input'), item

	bmap = {str(k).lower(): int(v) for k, v in (p.get('button_map') or {}).items()}

	def tokens(button):
		tok = _vr_token(button)
		eq = set([tok])
		if isinstance(tok, str) and tok in bmap:
			eq.add(bmap[tok])
		return tok, eq

	# Validate everything before changing anything.
	poses = {}
	for who, pose in (p.get('poses') or {}).items():
		key = _vr_resolve(trackers, who, 'tracker')
		bad = set(pose) - set(('position', 'euler'))
		if bad:
			raise ValueError('pose for %s: unknown keys %s (use position and euler)' % (who, sorted(bad)))
		poses[key] = {k: _vec(v) for k, v in pose.items()}
	presses = [split(i) for i in as_list(p.get('press'))]
	rel_items = as_list(p.get('release'))
	release_all = 'all' in rel_items
	releases = [] if release_all else [split(i) for i in rel_items]
	frames = int(p.get('frames', 0))
	if frames < 0:
		raise ValueError('frames must be 0 or more')
	_State.vr = sim

	for key, pose in poses.items():
		rec = sim.tracker(key, trackers[key])
		rec.pose.update(pose)
	if poses:
		sim.start_hold()
		sim.apply_nodes()

	events, event_errors = [], []

	def press(key, button):
		rec = sim.input(key, inputs[key])
		tok, eq = tokens(button)
		rec.down[tok] = eq
		events.append('down %s:%s' % (key, tok))
		err = sim.event(rec, 'down', min([e for e in eq if isinstance(e, int)] or [tok]))
		if err:
			event_errors.append(err)

	def let_go(key, button):
		rec = sim.inputs.get(key)
		tok, _ = tokens(button)
		if rec is None or tok not in rec.down:
			return
		eq = rec.down.pop(tok)
		events.append('up %s:%s' % (key, tok))
		err = sim.event(rec, 'up', min([e for e in eq if isinstance(e, int)] or [tok]))
		if err:
			event_errors.append(err)

	if release_all:
		for key, rec in list(sim.inputs.items()):
			for tok in list(rec.down):
				let_go(key, tok)
	for key, button in releases:
		let_go(key, button)
	try:
		for key, button in presses:
			press(key, button)
		if presses and frames:
			yield from _wait_frames(frames)
	finally:
		if presses and frames:
			for key, button in presses:
				let_go(key, button)
	for key, rec in list(sim.inputs.items()):       # nothing held: give the device its own methods back
		if not rec.down:
			rec.restore()
			_vr_unswap(rec, event_errors)
			del sim.inputs[key]
	res = sim.status()
	res.update({'events': events, 'event_errors': event_errors, 'notes': sim.notes, 'frames': frames})
	if not (sim.trackers or sim.inputs):
		_vr_restore()
		res['active'] = False
	else:
		res['active'] = True
	return res


# ---------------------------------------------------------------------------
# Validation of a recorded watch against theory
# ---------------------------------------------------------------------------

_MATH_FUNCS = {n: getattr(math, n) for n in (
	'sin', 'cos', 'tan', 'asin', 'acos', 'atan', 'atan2', 'sinh', 'cosh', 'tanh', 'exp', 'log',
	'log10', 'log2', 'sqrt', 'pow', 'fabs', 'hypot', 'degrees', 'radians', 'floor', 'ceil', 'copysign')
	if hasattr(math, n)}
_MATH_FUNCS.update({'abs': abs, 'min': min, 'max': max})
_MATH_CONSTS = {'pi': math.pi, 'e': math.e, 'tau': 2 * math.pi}
_BIN_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
            ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod,
            ast.Pow: operator.pow}
_UNARY_OPS = {ast.USub: operator.neg, ast.UAdd: operator.pos}


def _safe_compile(src, names):
	"""Compile an arithmetic expression to f(values_dict) -> float.

	Allowed: numbers, `names` (columns and constants), the math constants pi/e/tau,
	+ - * / // % ** and unary +/-, and calls of math functions (sin, sqrt, atan2, ...,
	abs, min, max). Everything else -- attributes, subscripts, strings, lambdas,
	comprehensions, any other call -- is rejected before anything runs. Numbers are
	floats, so an expression cannot build a huge integer."""
	try:
		tree = ast.parse(str(src).strip(), mode='eval')
	except SyntaxError as e:
		raise ValueError('cannot parse expression %r: %s' % (src, e))

	def build(node):
		if isinstance(node, ast.Expression):
			return build(node.body)
		if isinstance(node, ast.Constant):
			v = node.value
			if isinstance(v, bool) or not isinstance(v, (int, float)):
				raise ValueError('only numbers are allowed in an expression, got %r' % (v,))
			v = float(v)
			return lambda env: v
		if isinstance(node, ast.Name):
			name = node.id
			if name in names:
				return lambda env: env[name]
			if name in _MATH_CONSTS:
				v = _MATH_CONSTS[name]
				return lambda env: v
			raise ValueError('unknown name %r; available: %s' % (
				name, ', '.join(sorted(set(names) | set(_MATH_CONSTS) | set(_MATH_FUNCS)))))
		if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPS:
			op, a = _UNARY_OPS[type(node.op)], build(node.operand)
			return lambda env: op(a(env))
		if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
			op, a, b = _BIN_OPS[type(node.op)], build(node.left), build(node.right)

			def binop(env):
				r = op(a(env), b(env))
				if isinstance(r, complex):
					raise ValueError('result is not a real number')
				return r
			return binop
		if isinstance(node, ast.Call):
			if not isinstance(node.func, ast.Name) or node.func.id not in _MATH_FUNCS:
				raise ValueError('only these functions may be called: %s' % ', '.join(sorted(_MATH_FUNCS)))
			if node.keywords or any(isinstance(a, ast.Starred) for a in node.args):
				raise ValueError('function calls take plain positional arguments')
			fn, args = _MATH_FUNCS[node.func.id], [build(a) for a in node.args]
			return lambda env: fn(*[a(env) for a in args])
		raise ValueError('%s is not allowed in an expression' % type(node).__name__)

	return build(tree)


def _column_names(columns):
	"""Identifier usable in an expression -> column index. 'pos.y' is available as pos_y."""
	out = {}
	for i, c in enumerate(columns):
		for alias in (c, c.replace('.', '_')):
			if alias.isidentifier() and alias not in out:
				out[alias] = i
	return out


def cmd_validate(p):
	"""Compare a watch column with a theoretical value computed from the other columns."""
	name = p.get('watch')
	if name not in _State.watches:
		raise ValueError('no watch named %r (have: %s)' % (name, ', '.join(_State.watches) or 'none'))
	w = _State.watches[name]
	if not w.rows:
		raise ValueError('watch %r has no samples yet' % name)
	columns = ['t', 'frame'] + list(w.columns)
	cols = _column_names(columns)
	measured = p.get('measured')
	if measured not in cols and measured not in columns:
		raise ValueError('unknown measured column %r; have %s' % (measured, columns))
	m_idx = columns.index(measured) if measured in columns else cols[measured]
	consts = {}
	for k, v in (p.get('constants') or {}).items():
		if not str(k).isidentifier() or k in _MATH_FUNCS or k in _MATH_CONSTS or k in cols:
			raise ValueError('constant name %r must be an identifier that is not a column, '
			                 'a math function or pi/e/tau' % k)
		consts[str(k)] = float(v)
	theory = p.get('theory')
	if not theory:
		raise ValueError('theory is required: an expression over the columns, e.g. "P*L**3/(48*E*I)"')
	fn = _safe_compile(theory, set(cols) | set(consts))
	abs_tol, pct_tol = p.get('abs_tol'), p.get('pct_tol')
	for label, v in (('abs_tol', abs_tol), ('pct_tol', pct_tol)):
		if v is not None and float(v) < 0:
			raise ValueError('%s must be 0 or more' % label)
	t_min, t_max = p.get('t_min'), p.get('t_max')

	picked, skipped, first_error = [], 0, None
	for row in w.rows:
		t = row[0]
		if (t_min is not None and t < float(t_min)) or (t_max is not None and t > float(t_max)):
			continue
		env = dict(consts)
		env.update((n, row[i]) for n, i in cols.items())
		try:
			th = float(fn(env))
		except (ArithmeticError, ValueError) as e:
			skipped += 1
			first_error = first_error or '%s: %s' % (type(e).__name__, e)
			continue
		if th != th or th in (float('inf'), float('-inf')):
			skipped += 1
			continue
		picked.append((t, row[m_idx], th))
	if not picked:
		raise ValueError('no usable samples (%d skipped%s, window t=%s..%s)' % (
			skipped, ': ' + first_error if first_error else '', t_min, t_max))

	n = len(picked)
	errs = [m - th for _, m, th in picked]
	abs_errs = [abs(e) for e in errs]
	i_max = max(range(n), key=abs_errs.__getitem__)
	pcts = [100.0 * abs(e) / abs(th) for e, (_, _, th) in zip(errs, picked) if th != 0]
	peak = max(abs(th) for _, _, th in picked)
	res = {
		'watch': name, 'measured': measured, 'theory': theory, 'constants': consts,
		'samples': n, 'skipped': skipped,
		'max_abs_error': abs_errs[i_max], 't_of_max_error': picked[i_max][0],
		'mean_abs_error': sum(abs_errs) / n, 'rms_error': math.sqrt(sum(e * e for e in errs) / n),
		'bias': sum(errs) / n,
		'max_pct_error': max(pcts) if pcts else None,
		'mean_pct_error': sum(pcts) / len(pcts) if pcts else None,
		'pct_undefined_samples': n - len(pcts),
		'max_abs_error_pct_of_peak_theory': 100.0 * abs_errs[i_max] / peak if peak else None,
	}
	allowed = None
	if abs_tol is not None or pct_tol is not None:
		allowed = [max(float(abs_tol or 0.0), float(pct_tol or 0.0) / 100.0 * abs(th)) for _, _, th in picked]
		ok = [a <= al for a, al in zip(abs_errs, allowed)]
		n_pass = sum(ok)
		need = float(p.get('min_pass_pct', 100.0))
		res.update({'abs_tol': abs_tol, 'pct_tol': pct_tol, 'passing_samples': n_pass,
		            'failing_samples': n - n_pass, 'pass_pct': 100.0 * n_pass / n,
		            'min_pass_pct': need, 'passed': 100.0 * n_pass / n >= need,
		            'tolerance_rule': 'a sample passes if |measured - theory| <= max(abs_tol, pct_tol% of |theory|)'})
		bad = [i for i, o in enumerate(ok) if not o]
		if bad:
			t, m, th = picked[bad[0]]
			res['first_failure'] = {'t': t, 'measured': m, 'theory': th, 'error': m - th}
	rows = [[t, m, th, m - th] + ([int(ok[i])] if allowed else []) for i, (t, m, th) in enumerate(picked)]
	head = ['t', 'measured', 'theory', 'error'] + (['pass'] if allowed else [])
	path = p.get('csv_path') or os.path.join(_State.work_dir, 'validate_%s.csv' % name)
	res['csv'] = _write_rows(path, 'csv', head, rows, {})
	res['series'] = [r[:3] for r in _downsample(rows, int(p.get('max_points', 1500)))]
	return res


# ---------------------------------------------------------------------------
# Snapshots: transforms and visibility of chosen nodes, plus optional app state
# ---------------------------------------------------------------------------

_SNAPSHOT_FORMAT = 'vizmcp-snapshot'
_SNAPSHOT_VERSION = 1


def _node_state(node):
	"""Local position, quaternion, scale and visibility of a node; None for what it cannot give."""
	d = {}
	for key, getter in (('position', lambda: node.getPosition(viz.ABS_PARENT)),
	                    ('quat', lambda: node.getQuat(viz.ABS_PARENT)),
	                    ('scale', lambda: node.getScale())):
		try:
			d[key] = [float(a) for a in getter()]
		except Exception:
			d[key] = None
	try:
		d['visible'] = bool(node.getVisible())
	except Exception:
		d['visible'] = None
	return d


def _descendants(root, limit):
	"""`root` and everything under it, breadth first, each node once; (nodes, truncated)."""
	out, seen, queue_ = [], set(), collections.deque([root])
	while queue_:
		n = queue_.popleft()
		if n.id in seen:
			continue
		if len(out) >= limit:
			return out, True
		seen.add(n.id)
		out.append(n)
		queue_.extend(n.getChildren())
	return out, False


def _is_id_ref(ref):
	return isinstance(ref, int) or (isinstance(ref, str) and ref.strip().isdigit())


def _snapshot_entries(p):
	"""Nodes to record: the given refs, then named nodes, then everything under `root`."""
	limit = max(1, int(p.get('max_nodes', 2000)))
	labels = _labels()
	entries = collections.OrderedDict()
	truncated = False

	def add(node, ref):
		if node.id in entries:
			return
		if len(entries) >= limit:
			raise _Full()
		names = labels.get(node.id, [])
		if ref is None:
			ref = names[0] if names else node.id
		kind = 'id' if _is_id_ref(ref) else 'expr'
		entries[node.id] = dict({'ref': int(ref) if kind == 'id' else ref, 'kind': kind, 'id': node.id,
		                         'names': list(names), 'type': type(node).__name__}, **_node_state(node))

	try:
		for ref in p.get('nodes') or []:
			add(_resolve_node(ref), ref)
		if p.get('named'):
			found = cmd_find_nodes({'pattern': p.get('pattern') or '', 'limit': limit})
			for item in found['nodes']:
				add(viz.VizNode._GetObject(item['id']), None)
			truncated = truncated or found['truncated']
		if p.get('root') is not None:
			nodes, cut = _descendants(_resolve_node(p['root']), limit)
			for n in nodes:
				add(n, None)
			truncated = truncated or cut
	except _Full:
		truncated = True
	return list(entries.values()), truncated


class _Full(Exception):
	pass


def _snapshot_write(snap, path):
	state = snap.get('state')
	doc = {'format': _SNAPSHOT_FORMAT, 'version': _SNAPSHOT_VERSION, 'name': snap['name'],
	       'created': snap['created'], 'pid': snap['pid'], 'vizard_version': snap['vizard_version'],
	       'script': snap['script'], 'truncated': snap['truncated'], 'nodes': snap['nodes'], 'state': None}
	if state:
		doc['state'] = {'expression': state['expression'], 'restore_code': state['restore_code'],
		                'value': state['json'], 'value_exact': state['json_exact'], 'repr': state['repr']}
	os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
	with open(path, 'w', encoding='utf-8') as f:
		json.dump(doc, f, indent=1)
	return path


def _snapshot_read(path):
	with open(path, 'r', encoding='utf-8') as f:
		doc = json.load(f)
	if not isinstance(doc, dict) or doc.get('format') != _SNAPSHOT_FORMAT:
		raise ValueError('%s is not a vizmcp snapshot file' % path)
	if doc.get('version') != _SNAPSHOT_VERSION:
		raise ValueError('%s is snapshot version %r; this vizmcp reads version %d' % (path, doc.get('version'), _SNAPSHOT_VERSION))
	snap = {k: doc.get(k) for k in ('name', 'created', 'pid', 'vizard_version', 'script', 'nodes')}
	snap['truncated'] = bool(doc.get('truncated'))
	st = doc.get('state')
	if st:
		snap['state'] = {'expression': st.get('expression'), 'restore_code': st.get('restore_code'),
		                 'json': st.get('value'), 'json_exact': bool(st.get('value_exact')), 'repr': st.get('repr')}
	return snap


def _snapshot_summary(snap):
	st = snap.get('state')
	return {'name': snap['name'], 'created': snap['created'], 'nodes': len(snap['nodes']),
	        'truncated': snap['truncated'], 'state_expression': st['expression'] if st else None,
	        'restore_code': bool(st and st['restore_code']), 'file': snap.get('file'),
	        'same_app': snap['pid'] == os.getpid()}


def cmd_snapshot_save(p):
	"""Record the transforms and visibility of chosen nodes (and optionally app state) under a name."""
	name = p.get('name')
	if not name or not isinstance(name, str):
		raise ValueError('name is required')
	state_expr = p.get('state')
	if not (p.get('nodes') or p.get('named') or p.get('root') is not None or state_expr):
		raise ValueError('give nodes, named, root or state: nothing to record')
	if p.get('restore_code') and not state_expr:
		raise ValueError('restore_code needs state (an expression that returns the value to keep)')
	nodes, truncated = _snapshot_entries(p)
	snap = {'name': name, 'created': time.strftime('%Y-%m-%dT%H:%M:%S'), 'pid': os.getpid(),
	        'vizard_version': viz.version(), 'script': getattr(sys.modules['__main__'], '__file__', None),
	        'truncated': truncated, 'nodes': nodes}
	if state_expr:
		raw = _eval(state_expr)
		try:
			kept = copy.deepcopy(raw)
		except Exception as e:
			raise ValueError('the value of state %r cannot be copied (%s: %s); return plain data such as '
			                 'numbers, lists and dicts' % (state_expr, type(e).__name__, e))
		try:
			dumped = json.loads(json.dumps(raw))
			exact = True
		except (TypeError, ValueError):
			dumped, exact = None, False
		snap['state'] = {'expression': state_expr, 'restore_code': p.get('restore_code'), 'value': kept,
		                 'json': dumped, 'json_exact': exact, 'repr': _clip(repr(raw), 2000)}
	if p.get('path'):
		snap['file'] = _snapshot_write(snap, os.path.abspath(p['path']))
	_State.snapshots[name] = snap
	res = _snapshot_summary(snap)
	if state_expr and not snap['state']['json_exact']:
		res['note'] = ('the state value is not JSON data: it is kept in the app for snapshot_restore, but a '
		               'file holds only its repr and cannot restore it')
	return res


def _snapshot_resolve(entry, same_app):
	"""(node, None) or (None, reason) for one recorded node."""
	reason = None
	if entry['kind'] == 'expr':
		try:
			return _resolve_node(entry['ref']), None
		except Exception as e:
			reason = '%s: %s' % (type(e).__name__, e)
	elif not same_app:
		return None, 'node id %s is from another Vizard session' % entry['ref']
	if same_app and entry.get('id') is not None:
		try:
			return _resolve_node(entry['id']), None
		except Exception as e:
			reason = reason or '%s: %s' % (type(e).__name__, e)
	return None, reason


def cmd_snapshot_restore(p):
	"""Put a snapshot back: app state first (restore_code), then each node's transform and
	visibility. Nodes that cannot be found are reported, not fatal."""
	name, path = p.get('name'), p.get('path')
	if not name and not path:
		raise ValueError('give name, or path of a snapshot file')
	if path:
		snap = _snapshot_read(os.path.abspath(path))
		snap['file'] = os.path.abspath(path)
		name = name or snap['name']
		snap['name'] = name
		_State.snapshots[name] = snap
	elif name not in _State.snapshots:
		raise ValueError('no snapshot named %r (have: %s)' % (name, ', '.join(_State.snapshots) or 'none'))
	else:
		snap = _State.snapshots[name]
	same_app = snap['pid'] == os.getpid()
	res = {'name': name, 'nodes_saved': len(snap['nodes']), 'restored': 0, 'missing': [], 'failed': []}

	st = snap.get('state')
	if st:
		code = p.get('restore_code') or st['restore_code']
		if not code:
			res['state'] = {'restored': False, 'reason': 'the snapshot has state but no restore_code was given'}
		elif 'value' in st:
			value = copy.deepcopy(st['value'])
		elif st['json_exact']:
			value = copy.deepcopy(st['json'])
		else:
			value = _MISSING
			res['state'] = {'restored': False, 'reason': 'the state value was not JSON data and is not in this app'}
		if code and value is not _MISSING:
			ns = _main_ns()
			had, old = '_state' in ns, ns.get('_state')
			ns['_state'] = value
			try:
				exec(compile(code, '<vizmcp snapshot>', 'exec'), ns)
				res['state'] = {'restored': True}
			except Exception:
				res['state'] = {'restored': False, 'error': _clip(traceback.format_exc(), 2000)}
			finally:
				if had:
					ns['_state'] = old
				else:
					ns.pop('_state', None)

	for e in snap['nodes']:
		node, reason = _snapshot_resolve(e, same_app)
		if node is None:
			res['missing'].append({'ref': e['ref'], 'names': e.get('names', []), 'reason': reason})
			continue
		ok = True
		for what, setter, value in (
				('position', lambda v: node.setPosition(v, viz.ABS_PARENT), e.get('position')),
				('quat', lambda v: node.setQuat(v, viz.ABS_PARENT), e.get('quat')),
				('scale', lambda v: node.setScale(v), e.get('scale')),
				('visible', lambda v: node.visible(bool(v)), e.get('visible'))):
			if value is None:
				continue
			try:
				setter(value)
			except Exception as ex:
				ok = False
				res['failed'].append({'ref': e['ref'], 'what': what, 'error': '%s: %s' % (type(ex).__name__, ex)})
		res['restored'] += 1 if ok else 0
	return res


def cmd_snapshot_list(p):
	return {'snapshots': [_snapshot_summary(s) for s in _State.snapshots.values()]}


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
	'record_tour': cmd_record_tour,
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
	'gui_list': cmd_gui_list,
	'click_button': cmd_click_button,
	'vr_simulate': cmd_vr_simulate,
	'validate': cmd_validate,
	'snapshot_save': cmd_snapshot_save,
	'snapshot_restore': cmd_snapshot_restore,
	'snapshot_list': cmd_snapshot_list,
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
	_vr_restore()
	if _server is not None:
		_server.stop()
		_server = None
	if _pump_event is not None:
		_pump_event.remove()
		_pump_event = None
