"""MCP server that connects an AI assistant to a running WorldViz Vizard app.

The tools forward to the vizmcp module inside Vizard (see vizard_side/vizmcp.py)
over a localhost socket. Images are prepared here, outside Vizard.
"""

import json
import os
import subprocess
import tempfile
import time
from typing import Any

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import api_index, doctor as doctor_mod, imaging, winviz
from .client import NotConnected, VizardClient, VizardError

VIZARD_SIDE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'vizard_side')

INSTRUCTIONS = """\
Tools for inspecting and driving a running WorldViz Vizard (Python VR engine) app.

Start with vizard_status. If no app is running, launch_app starts a Vizard script with
the vizmcp module injected (the script itself needs no changes).

Nodes are referenced by numeric id or by a Python expression evaluated in the app's
__main__ namespace, e.g. "ground" or "LAB.site.crane.hook"; find_nodes searches those names.
run_code executes Python inside the app on Vizard's main thread, so `viz`, `vizact` and
every global of the script are available, and the value of the last expression is returned.
Before writing Vizard code, check exact names and signatures with vizard_api.

Seeing: screenshot (annotate=true labels named nodes), pick (what is at a point of a screenshot),
project (where world points appear on screen), camera. doctor checks the installation.
wait_until runs the app until a condition holds; scene_stats counts nodes, textures, triangles and
lights to explain a slow scene.
GUI: gui_list lists the app's 2D widgets (buttons, sliders, ...) with text and window
position; click_button clicks one by its label.
Screen coordinates are 0..1 from the top-left corner of the screenshot.

This is the basic edition. The full edition adds asset audits, GUI smoke tests, snapshots and undo,
HTML reports, videos and camera tours, watches and validation against theory, VR simulation and
remote (LAN) apps; contact zarrarabbas73@gmail.com.

Vizard is Y-up and left-handed; euler is [yaw, pitch, roll] in degrees.
Vizard's free licence allows only one Vizard process at a time.
"""

class VizardToolError(ToolError, RuntimeError):
	"""A tool failure. ToolError makes the SDK send the message to the client
	(any other exception is reduced to "Error executing tool X"); RuntimeError keeps
	the type callers of the tool functions have always seen."""


# Tools that return [Image, text] must opt out of structured output: the SDK cannot serialise Image.
mcp = MCPServer('vizard-mcp', instructions=INSTRUCTIONS)
client = VizardClient()
_launched = {'proc': None, 'log': None}


def _call(cmd, wait_s=None, **params):
	"""Run a command in the app. wait_s: how long a multi-frame command may take."""
	params = {k: v for k, v in params.items() if v is not None}
	timeout = None
	if wait_s is not None:
		params['timeout_s'] = wait_s
		timeout = wait_s + 30
	try:
		return client.call(cmd, timeout=timeout, **params)
	except NotConnected as e:
		raise VizardToolError(str(e))
	except VizardError as e:
		raise VizardToolError('Vizard reported an error:\n%s' % e)


def _dump(x):
	return json.dumps(x, indent=1, ensure_ascii=False)


def _jpeg(im):
	return Image(data=imaging.to_jpeg(im), format='jpeg')


# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------

def find_winviz():
	exe = winviz.locate()
	if not exe:
		raise VizardToolError('winviz.exe not found. Set the VIZARD_EXE environment variable to its full path.')
	return exe


def _tail(path, n=40):
	try:
		with open(path, 'r', encoding='utf-8', errors='replace') as f:
			return ''.join(f.readlines()[-n:])
	except (OSError, TypeError):
		return ''


@mcp.tool()
def vizard_status() -> str:
	"""Report whether a Vizard app with vizmcp is reachable, and if so its script,
	Vizard version, frame number, FPS, window size, camera, node count
	and the commands it supports."""
	if not client.ping():
		msg = {'connected': False, 'port': client.port}
		proc = _launched['proc']
		if proc is not None:
			msg['launched_process_exit_code'] = proc.poll()
			msg['launch_log_tail'] = _tail(_launched['log'])
		return _dump(msg)
	info = _call('info')
	info['connected'] = True
	return _dump(info)


@mcp.tool()
def launch_app(script: str, args: list[str] | None = None, env: dict[str, str] | None = None,
               restart: bool = False, timeout_s: float = 120.0) -> str:
	"""Start a Vizard script (absolute path to a .py file) in winviz.exe with vizmcp
	injected, then wait until it answers. The script runs unmodified as __main__ in its
	own folder.

	env: extra environment variables for the app (e.g. a switch that skips a startup dialog).
	restart: if an app is already connected, ask it to quit first.
	"""
	script = os.path.abspath(script)
	if not os.path.isfile(script):
		raise VizardToolError('No such script: %s' % script)
	if client.ping():
		if not restart:
			raise VizardToolError('A Vizard app is already connected. Pass restart=true to replace it, '
			                   'or call quit_app first.')
		quit_app()

	exe = find_winviz()
	log_path = os.path.join(tempfile.gettempdir(), 'vizmcp_launch_%d.log' % int(time.time()))
	child_env = dict(os.environ)
	child_env.update(env or {})
	child_env['VIZMCP_TARGET'] = script
	child_env['VIZMCP_PORT'] = str(client.port)
	if client.token:
		child_env['VIZMCP_TOKEN'] = client.token
	log = open(log_path, 'wb')
	# stdin must not be left attached: Vizard's Python can spin on a broken console stdin.
	proc = subprocess.Popen(
		[exe, os.path.join(VIZARD_SIDE, 'vizmcp_boot.py')] + list(args or []),
		cwd=os.path.dirname(script), env=child_env,
		stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
		creationflags=getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0))
	log.close()
	_launched.update(proc=proc, log=log_path)

	t0 = time.time()
	while time.time() - t0 < timeout_s:
		code = proc.poll()
		if code is not None:
			hint = ''
			if code == 1 and time.time() - t0 < 5:
				hint = ('\nVizard exited almost at once. With the free licence this happens when '
				        'another Vizard script or the Vizard IDE is already running a script.')
			raise VizardToolError('winviz.exe exited with code %s.%s\nLog (%s):\n%s'
			                   % (code, hint, log_path, _tail(log_path)))
		if client.ping():
			info = _call('info')
			info.pop('commands', None)
			info.update(pid=proc.pid, startup_s=round(time.time() - t0, 1), launch_log=log_path)
			return _dump(info)
		time.sleep(0.5)
	raise VizardToolError('The app did not answer within %ds (it may be waiting in a dialog, e.g. a '
	                   'hardware selection window). Process is still running (pid %d). Log:\n%s'
	                   % (timeout_s, proc.pid, _tail(log_path)))


@mcp.tool()
def quit_app(timeout_s: float = 20.0) -> str:
	"""Ask the connected Vizard app to close, and wait for it to go away."""
	try:
		_call('quit')
	except VizardToolError as e:
		if 'No Vizard app' in str(e):
			return 'No app was running.'
		raise
	client.close()
	t0 = time.time()
	while time.time() - t0 < timeout_s:
		time.sleep(0.5)
		if not client.ping():
			proc = _launched['proc']
			if proc is not None:
				try:
					proc.wait(timeout=max(0.1, timeout_s - (time.time() - t0)))
				except subprocess.TimeoutExpired:
					pass
			return 'App closed.'
	return 'Quit was sent, but the app still answers after %ds.' % timeout_s


@mcp.tool()
def reload_module(module: str) -> str:
	"""importlib.reload() one of the app's own modules (by import name) after editing it.
	Objects created before the reload keep their old methods, so this suits modules of
	functions and constants; restart the app for deeper changes."""
	return _dump(_call('reload_module', module=module))


# ---------------------------------------------------------------------------
# Inspection
# ---------------------------------------------------------------------------

@mcp.tool()
def scene_tree(depth: int = 3, root: str | None = None, max_nodes: int = 400,
               only_named: bool = False) -> str:
	"""List the scene graph: for each node its id, Python class, the names the script
	uses for it (e.g. "LAB.site.crane.hook", "cars[2]"), visibility, local position and
	children.

	root: start from this node (id or Python expression) instead of the whole scene.
	only_named: keep only nodes that the script refers to by name (and their ancestors).
	"""
	return _dump(_call('scene_tree', depth=depth, root=root, max_nodes=max_nodes,
	                   only_named=only_named))


@mcp.tool()
def find_nodes(pattern: str = '', limit: int = 100) -> str:
	"""Find nodes by the names the script uses for them, searching the app's globals,
	the attributes of its own objects (e.g. "LAB.render.deck") and its own modules.
	pattern is a case-insensitive substring; empty lists every named node."""
	return _dump(_call('find_nodes', pattern=pattern, limit=limit))


@mcp.tool()
def node_info(node: str, subnode_names: bool = False, limit: int = 200) -> str:
	"""Full details of one node: local and global position/euler, scale, world-space
	bounding box (min/max/center/size), parents, children and its script names.

	node: numeric id or a Python expression in the app (e.g. "bridge" or "lab.deck").
	subnode_names: also list named sub-nodes inside a loaded model (getNodeNames()).
	"""
	return _dump(_call('node_info', node=node, subnode_names=subnode_names, limit=limit))


@mcp.tool()
def set_node(node: str, position: list[float] | None = None, euler: list[float] | None = None,
             scale: list[float] | None = None, visible: bool | None = None,
             color: list[float] | None = None, alpha: float | None = None,
             space: str = 'local') -> str:
	"""Change a node's transform or appearance. Euler is [yaw, pitch, roll] in degrees;
	Vizard is Y-up and left-handed. space: "local" (relative to parent) or "global".
	Note: if the app sets this node every frame, the app will overwrite the change."""
	return _dump(_call('set_node', node=node, position=position, euler=euler, scale=scale,
	                   visible=visible, color=color, alpha=alpha, space=space))


@mcp.tool()
def run_code(code: str) -> str:
	"""Execute Python inside the running Vizard app, on its main thread, in the script's
	__main__ namespace (so viz, vizact and the script's globals are available and new
	names persist between calls). Returns stdout, stderr, the repr of the final
	expression's value, and the traceback if it raised.

	Do not block (no time.sleep loops, no viz.go()); the app's frame waits for the code.
	Use wait / wait_until to let time pass."""
	return _dump(_call('run_code', code=code))


@mcp.tool()
def get_log(since: int = 0, limit: int = 200, stream: str | None = None,
            contains: str | None = None) -> str:
	"""Read the app's console output (print output and tracebacks) captured since vizmcp
	started. Each line has a sequence number; pass the last seen 'last_seq' as `since` to
	get only new lines. stream: "out" or "err". contains: case-insensitive filter."""
	return _dump(_call('get_log', since=since, limit=limit, stream=stream, contains=contains))


@mcp.tool()
def vizconnect_info() -> str:
	"""If the app uses vizconnect: its trackers, transports, displays, inputs, avatars,
	tools and events with their world positions."""
	return _dump(_call('vizconnect_info'))


@mcp.tool()
def vizard_api(query: str = '', name: str | None = None, limit: int = 30) -> str:
	"""Search Vizard's own Python API (parsed from the installed Vizard's python folder;
	works without a running app). Returns exact signatures and docstrings.

	query: substring of a function/class/method/constant name, or a word from a docstring
	       (e.g. "boundingbox", "screenCapture", "onkeydown", "KEY_UP").
	name: full or trailing dotted name for one entry with its whole docstring
	      (e.g. "viz.VizNode.getBoundingBox" or "vizact.onupdate")."""
	root = api_index.vizard_python_dir(find_winviz())
	if name:
		hits = api_index.lookup(root, name)
		if not hits:
			return 'Nothing named %r. Try query=%r.' % (name, name.rsplit('.', 1)[-1])
		return '\n\n'.join('%s%s  [%s]\n%s:%d\n%s' % (e['name'], e['signature'], e['kind'], e['file'],
		                                             e['line'], e['doc'] or '(no docstring)') for e in hits)
	if not query:
		raise VizardToolError('Give query or name.')
	hits = api_index.search(root, query, limit=limit)
	if not hits:
		return 'No matches for %r.' % query
	return '\n'.join('%s%s  [%s]  %s' % (e['name'], e['signature'], e['kind'],
	                                     (e['doc'].splitlines() or [''])[0][:120]) for e in hits)


# ---------------------------------------------------------------------------
# Seeing
# ---------------------------------------------------------------------------

def _take_screenshot(save_to=None, annotate=False, label_limit=25):
	"""Capture the window now; returns (full-size PIL image, description)."""
	res = _call('screenshot', wait_s=30, path=os.path.abspath(save_to) if save_to else None)
	im = imaging.load(res['path'])
	text = 'frame %s, window %s, saved at %s' % (res.get('frame'), res.get('window_size'), res['path'])
	if annotate:
		labels = _call('screen_labels', limit=label_limit)
		im = imaging.annotate(im, labels['items'])
		text += '\nlabelled %d of %d named nodes on screen' % (len(labels['items']), labels['total_on_screen'])
		if labels.get('skipped'):
			text += '; not labelled: %s' % ', '.join('%d %s' % (n, why) for why, n in labels['skipped'].items())
	return im, text


@mcp.tool(structured_output=False)
def screenshot(max_size: int = 1024, annotate: bool = False, label_limit: int = 25,
               save_to: str | None = None) -> list[Any]:
	"""Capture the Vizard window as it is rendered now and return the image.

	annotate: draw a tag on each named node that is on screen (its script name at its
	          bounding-box center), to connect what you see with the code.
	max_size: longest side of the returned image in pixels (0 = full size).
	save_to: optional .png path to also keep the full-size capture."""
	im, text = _take_screenshot(save_to, annotate, label_limit)
	return [_jpeg(imaging.fit(im, max_size)), text]


@mcp.tool()
def pick(x: float, y: float, ignore_within_m: float = 0.5) -> str:
	"""What is at screen point (x, y), given as 0..1 from the top-left of a screenshot?
	Returns the nearest node hit, its script names (or its nearest named ancestor), the
	world point, distance and surface normal, and the sub-node name inside a model.
	Hits closer than ignore_within_m to the camera (e.g. the user's own avatar head)
	are skipped."""
	return _dump(_call('pick', x=x, y=y, ignore_within_m=ignore_within_m))


@mcp.tool()
def project(points: list[list[float]] | None = None, nodes: list[str] | None = None) -> str:
	"""Where world points, or the centers of nodes, appear on screen (0..1 from the
	top-left of a screenshot). null means behind the camera or outside the window."""
	return _dump(_call('project', points=points, nodes=nodes))


@mcp.tool()
def camera(position: list[float] | None = None, euler: list[float] | None = None,
           look_at: list[float] | None = None, hold: bool = False, release: bool = False) -> str:
	"""Get or set the main view (viz.MainView). With no arguments, returns the current
	position and euler. look_at overrides euler.

	hold: keep re-applying this view every frame (needed when vizconnect or the app moves
	the view each frame). release: stop holding and give the view back to the app."""
	if release:
		_call('camera_release')
		return _dump(_call('camera_get'))
	if position is None and euler is None and look_at is None:
		return _dump(_call('camera_get'))
	return _dump(_call('camera_set', position=position, euler=euler, look_at=look_at, hold=hold))


# ---------------------------------------------------------------------------
# Driving
# ---------------------------------------------------------------------------

@mcp.tool()
def send_key(key: str, hold_frames: int = 1) -> str:
	"""Simulate a key press in the app: fires KEYDOWN, keeps viz.key.isDown(key) true for
	hold_frames frames, then fires KEYUP. key is a character ("w", " ") or a Vizard key
	constant name ("KEY_UP", "KEY_F1", "KEY_ESCAPE")."""
	return _dump(_call('send_key', wait_s=hold_frames / 10.0 + 30, key=key, hold_frames=hold_frames))


@mcp.tool()
def click(x: float, y: float, button: str = 'left', double: bool = False) -> str:
	"""Click in the Vizard window with the real mouse at (x, y), 0..1 of the window's client
	area from the top-left (the same coordinates as a screenshot); for on-screen GUI
	(buttons, sliders, menus) and mouse handlers. The window is brought to the front if it
	is not there already (and restored if minimised), the click is refused if another
	window covers that point, and the cursor is put back afterwards. Note that a mouse-look navigator may
	also react to the click."""
	return _dump(_call('click', wait_s=20, x=x, y=y, button=button, double=double))


@mcp.tool()
def gui_list(visible_only: bool = False, kinds: list[str] | None = None) -> str:
	"""List the app's 2D GUI widgets (buttons, labels, checkboxes, sliders, textboxes, ...):
	id, script names, class, kind, text/label, visible, enabled, and center/size as 0..1
	fractions of the window from the top-left (the same coordinates as a screenshot and click).
	Widgets parented into the 3D scene (e.g. a vizconnect wrist or tablet panel) are listed
	separately under world_widgets with their world position; they cannot be clicked.
	visible_only: leave out hidden widgets. kinds: only these kinds, e.g. ["button"]."""
	return _dump(_call('gui_list', visible_only=visible_only or None, kinds=kinds))


@mcp.tool()
def click_button(label: str | None = None, id: int | None = None, button: str = 'left',
                 double: bool = False) -> str:
	"""Click a 2D GUI widget by its text instead of by coordinates (see gui_list). The label
	is matched against visible, enabled widgets: exact text first, then ignoring case, then as
	a case-insensitive part of the text ("reset" finds "Reset ball"). If nothing matches, or
	several widgets do, it fails and lists the candidates; pass `id` from gui_list to choose
	one. The click itself is the real-mouse click of the click tool, with the same caveats."""
	if label is None and id is None:
		raise VizardToolError('Give label or id.')
	return _dump(_call('click_button', wait_s=40, label=label, id=id, button=button, double=double))


@mcp.tool()
def wait(frames: int = 0, seconds: float = 0.0) -> str:
	"""Let the app run until at least `frames` frames and `seconds` seconds have passed."""
	return _dump(_call('wait', wait_s=seconds + frames / 5.0 + 30, frames=frames, seconds=seconds))


@mcp.tool()
def wait_until(condition: str, limit_s: float = 60.0) -> str:
	"""Let the app run until a Python expression is true in it, checked every frame
	(e.g. "truck.x > 30" or "LAB.state == 'collapsed'"). Returns met=false after limit_s."""
	return _dump(_call('wait_until', wait_s=limit_s + 30, condition=condition, limit_s=limit_s))


@mcp.tool()
def scene_stats(top: int = 10) -> str:
	"""Counts that explain why a scene is slow, e.g. on an integrated GPU: nodes, visible
	nodes, node types, textures, approximate triangles (all and visible), lights and the
	`top` heaviest nodes with their script names. A figure the running Vizard cannot
	provide is null, with a note saying so; heavy nodes are then ranked by sub-node count."""
	if top < 0:
		raise VizardToolError('top must be 0 or more.')
	return _dump(_call('scene_stats', top=top))


@mcp.tool()
def doctor() -> str:
	"""Check the vizard-mcp installation and print PASS / WARN / FAIL lines with a fix for each:
	Python, mcp and Pillow versions, winviz.exe and the Vizard folder, the vizmcp port, and whether the server
	is registered in Claude Code and Claude Desktop with a command path that exists. Local checks
	only; nothing is sent over the network."""
	return doctor_mod.render(doctor_mod.run_checks())


@mcp.tool()
def call_command(command: str, params: dict[str, Any] | None = None) -> str:
	"""Call an app-specific command that the script registered with
	vizmcp.register(name, func)."""
	return _dump(_call(command, **(params or {})))


def main():
	from .cli import main as cli_main
	cli_main()


if __name__ == '__main__':
	main()
