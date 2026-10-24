"""MCP server that connects an AI assistant to a running WorldViz Vizard app.

The tools forward to the vizmcp module inside Vizard (see vizard_side/vizmcp.py)
over a localhost socket. Images are prepared here, outside Vizard.
"""

import base64
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from typing import Any

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import api_index, doctor as doctor_mod, imaging, report, video, winviz
from .client import NotConnected, VizardClient, VizardError, is_local_host

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

Seeing: screenshot (annotate=true labels named nodes), multi_view (orbit a node from
several angles without disturbing the user's view), filmstrip (frames over time), pick
(what is at a point of a screenshot). For people to watch: record_video (MP4) and record_tour
(MP4 while the camera flies along keyframes or orbits a node; dry_run previews the path;
mode="steps" renders it frame by frame for a smooth video even when the scene draws slowly).
doctor checks the installation.
Undo: snapshot_save records the transforms and visibility of chosen nodes (and optional app state)
under a name, snapshot_restore puts them back (reporting nodes that are gone), snapshot_list shows them.
Reports: make_report writes a self-contained HTML report (print it to PDF) from headings, text,
watches, validate results, screenshots, images and key-value tables.
Measuring: watch_start / watch_read / watch_plot record any expressions every frame
(e.g. a deflection or a velocity) so results can be checked against theory, and
watch_export writes them to CSV or JSON for reports;
wait_until runs the app until a condition holds; profile finds slow Python per frame;
scene_stats counts nodes, textures, triangles and lights to explain a slow scene.
validate compares a recorded watch with a formula (beam deflection, free fall, ...) and
reports error statistics, pass/fail and a measured-vs-theory chart.
Regression checks: screenshot_diff compares the window with a reference PNG.
GUI: gui_list lists the app's 2D widgets (buttons, sliders, ...) with text and window
position; click_button clicks one by its label. vr_simulate fakes a headset's tracker poses
and button presses through vizconnect, to test VR menus on a desktop.
Screen coordinates are 0..1 from the top-left corner of the screenshot.
Assets: asset_check ranks nodes by cost and flags scale bugs, empty or hidden-but-costly nodes,
duplicated heavy subtrees, texture hogs and missing files. crawl_gui clicks each safe 2D button in
turn and reports what changed, which exceptions it raised and which lines it logged, undoing each
click from a snapshot.
The app may run on another PC (an exhibition machine): connect switches the host, port and token;
launch_app and doctor only manage apps on this PC.

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
_last = {}       # the latest asset_check / crawl_gui result, for make_report sections


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
	Vizard version, frame number, FPS, window size, camera, node count, active watches
	and the commands it supports."""
	if not client.ping():
		msg = {'connected': False, 'host': client.host, 'port': client.port}
		proc = _launched['proc']
		if proc is not None:
			msg['launched_process_exit_code'] = proc.poll()
			msg['launch_log_tail'] = _tail(_launched['log'])
		return _dump(msg)
	info = _call('info')
	info['connected'] = True
	info['target'] = '%s:%d' % (client.host, client.port)
	return _dump(info)


@mcp.tool()
def connect(host: str | None = None, port: int | None = None, token: str | None = None) -> str:
	"""Choose which vizmcp app this server talks to, and report what it reaches. Without
	arguments it only reports the current target. Settings you leave out stay as they are
	(the starting values are VIZMCP_HOST, VIZMCP_PORT and VIZMCP_TOKEN); token="" clears the token.

	host: 127.0.0.1 for this PC, or the address of another PC whose app was started with
	      VIZMCP_BIND and VIZMCP_TOKEN (see the README: firewall, LAN only). launch_app works
	      only for this PC.
	The token is never echoed back; the result says whether one is set."""
	if host is not None and not host.strip():
		raise VizardToolError('host must not be empty (use 127.0.0.1 for this PC).')
	if port is not None and not 0 < port < 65536:
		raise VizardToolError('port must be 1..65535.')
	client.configure(host=host, port=port, token=token)
	return _dump(_target_report())


def _target_report():
	state, detail = client.probe()
	out = {'host': client.host, 'port': client.port, 'local': client.is_local,
	       'token_set': bool(client.token), 'reachable': state == 'ok'}
	if state == 'ok':
		try:
			info = _call('info')
			out['app'] = {k: info.get(k) for k in ('script', 'vizard_version', 'vizmcp_version', 'listening',
			                                       'window_size', 'node_count')}
		except VizardToolError as e:
			out['app_error'] = str(e)
	else:
		out['problem'] = {'no_listener': 'nothing is listening there', 'bad_token': 'the app rejected the token',
		                  'error': 'the target answered with an error'}[state]
		out['detail'] = detail
		if state == 'bad_token':
			out['hint'] = 'use the same VIZMCP_TOKEN on the app and here (connect token=...)'
	if not client.is_local and not client.token:
		out['warning'] = ('no token set: a vizmcp app only listens on the network with a token, so this will '
		                  'fail unless the app runs without one on a non-loopback address (it refuses to)')
	return out


@mcp.tool()
def launch_app(script: str, args: list[str] | None = None, env: dict[str, str] | None = None,
               restart: bool = False, timeout_s: float = 120.0) -> str:
	"""Start a Vizard script (absolute path to a .py file) in winviz.exe with vizmcp
	injected, then wait until it answers. The script runs unmodified as __main__ in its
	own folder.

	env: extra environment variables for the app (e.g. a switch that skips a startup dialog).
	restart: if an app is already connected, ask it to quit first.

	Works only for an app on this PC; with a remote target (see connect) it refuses.
	"""
	if not client.is_local:
		raise VizardToolError(
			'launch_app starts winviz.exe on this PC, but the connected target is %s:%d (another PC). '
			'Start the app there yourself with VIZMCP_BIND and VIZMCP_TOKEN set and keep using connect, '
			'or call connect(host="127.0.0.1") to work with an app on this PC.' % (client.host, client.port))
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
	else:
		child_env.pop('VIZMCP_BIND', None)       # without a token the app would refuse to listen beyond this PC
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

def _app_image(path):
	"""Load an image the app wrote. For an app on another PC the file is fetched over the socket
	(the server cannot read that PC's disk)."""
	if client.is_local:
		return imaging.load(path)
	res = _call('fetch_file', path=path)
	return imaging.load(io.BytesIO(base64.b64decode(res['data'])))


def _require_local_files(what):
	if not client.is_local:
		raise VizardToolError(
			'%s writes a video on the PC where the app runs (%s), and this server cannot fetch it from '
			'there. Run it with the app on this PC, or take the video on the app PC.' % (what, client.host))


def _take_screenshot(save_to=None, annotate=False, label_limit=25):
	"""Capture the window now; returns (full-size PIL image, description)."""
	remote = not client.is_local
	res = _call('screenshot', wait_s=30, path=os.path.abspath(save_to) if save_to and not remote else None)
	im = _app_image(res['path'])
	if remote and save_to:
		os.makedirs(os.path.dirname(os.path.abspath(save_to)), exist_ok=True)
		im.save(os.path.abspath(save_to), 'PNG')
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


@mcp.tool(structured_output=False)
def screenshot_diff(reference: str, threshold_pct: float | None = None, pixel_tolerance: int = 8,
                    save_diff_to: str | None = None, update_reference: bool = False,
                    max_size: int = 1024) -> list[Any]:
	"""Visual regression check: capture the Vizard window and compare it with a reference
	PNG. Returns a diff image (reference dimmed, changed pixels in red, changed areas boxed)
	and the percentage of pixels that differ.

	reference: path of the reference PNG (it must be the same size as the window).
	threshold_pct: if given, the result says pass/fail: pass when the differing pixels are
	               at most this percentage of the image.
	pixel_tolerance: a pixel counts as different when any colour channel differs by more
	                 than this (0-255); absorbs anti-aliasing noise.
	save_diff_to: optional .png path for the full-size diff image.
	update_reference: write the current capture to `reference` (creating or replacing the
	                  baseline) instead of comparing.
	"""
	if not 0 <= pixel_tolerance <= 255:
		raise VizardToolError('pixel_tolerance must be 0..255.')
	if threshold_pct is not None and not 0 <= threshold_pct <= 100:
		raise VizardToolError('threshold_pct must be 0..100.')
	reference = os.path.abspath(reference)
	res = _call('screenshot', wait_s=30)
	current = _app_image(res['path'])
	if update_reference:
		os.makedirs(os.path.dirname(reference), exist_ok=True)
		current.save(reference, 'PNG')
		return [_jpeg(imaging.fit(current, max_size)),
		        _dump({'reference_written': reference, 'size': list(current.size)})]
	if not os.path.isfile(reference):
		raise VizardToolError('No reference image at %s. Pass update_reference=true to create it '
		                      'from the current window.' % reference)
	try:
		ref = imaging.load(reference)
	except OSError as e:
		raise VizardToolError('Cannot read the reference image %s: %s' % (reference, e))
	result = imaging.diff(current, ref, pixel_tolerance)
	diff_im = result.pop('image')
	out = dict(result, reference=reference, capture=res['path'])
	if threshold_pct is not None:
		out['threshold_pct'] = threshold_pct
		out['passed'] = (not out['size_mismatch']) and out['diff_pct'] <= threshold_pct
	if diff_im is not None and save_diff_to:
		path = os.path.abspath(save_diff_to)
		os.makedirs(os.path.dirname(path), exist_ok=True)
		diff_im.save(path, 'PNG')
		out['diff_image'] = path
	if diff_im is None:
		diff_im = current
		out['note'] = 'sizes differ: the capture is returned instead of a diff image'
	return [_jpeg(imaging.fit(diff_im, max_size)), _dump(out)]


@mcp.tool(structured_output=False)
def multi_view(orbit: str | None = None, target: list[float] | None = None, radius: float = 5.0,
               count: int = 4, elevation_deg: float = 25.0, distance: float | None = None,
               start_yaw_deg: float = 45.0, views: list[dict[str, Any]] | None = None,
               max_size: int = 1400) -> list[Any]:
	"""See something from several angles in one image. The view is moved for a few frames
	per angle and then put back exactly as it was.

	orbit: node (id or expression) to circle around, framed by its bounding box.
	target + radius: circle around a world point instead.
	count / elevation_deg / distance / start_yaw_deg: the orbit (distance defaults to
	about 2.8 x the radius).
	views: explicit list of {"position": [x,y,z], "look_at": [x,y,z]} or
	       {"position", "euler"}, instead of an orbit."""
	if orbit is None and target is None and not views:
		raise VizardToolError('Give orbit, target or views.')
	n = len(views) if views else count
	res = _call('multi_view', wait_s=20 + 5 * n, orbit=orbit, target=target, radius=radius,
	            count=count, elevation_deg=elevation_deg, distance=distance,
	            start_yaw_deg=start_yaw_deg, views=views)
	shots = res['shots']
	images = [_app_image(s['path']) for s in shots]
	captions = [s['view'].get('label') or 'view %d' % (i + 1) for i, s in enumerate(shots)]
	sheet = imaging.grid(images, captions, max_size)
	text = '\n'.join('%s: camera %s -> %s' % (c, s['view'].get('position'), s['view'].get('look_at') or s['view'].get('euler'))
	                 for c, s in zip(captions, shots))
	return [_jpeg(sheet), text]


@mcp.tool(structured_output=False)
def filmstrip(count: int = 6, interval_s: float = 0.5, max_size: int = 1400) -> list[Any]:
	"""See motion: capture `count` frames `interval_s` apart (the app keeps running) and
	return them as one grid image, captioned with the time of each frame."""
	res = _call('filmstrip', wait_s=30 + count * (interval_s + 2), count=count, interval_s=interval_s)
	shots = res['shots']
	images = [_app_image(s['path']) for s in shots]
	captions = ['t = %.2f s' % s['t_rel_s'] for s in shots]
	return [_jpeg(imaging.grid(images, captions, max_size)),
	        'frames %s' % [s['frame'] for s in shots]]


def _video_paths(path, fmt):
	"""(avi_path, final_path) for a recording; None for what Vizard should name itself."""
	if not path:
		return None, None
	stem, ext = os.path.splitext(os.path.abspath(path))
	return stem + '.avi', stem + ('.mp4' if fmt == 'mp4' else '.avi')


def _check_video_options(fmt, crf, fps, max_size):
	if fmt not in ('mp4', 'avi'):
		raise VizardToolError('format must be "mp4" or "avi".')
	if not 0 <= crf <= 51:
		raise VizardToolError('crf must be 0..51 (lower is better quality; 23 is the default).')
	if fps is not None and not 0 < fps <= 240:
		raise VizardToolError('fps must be more than 0 and at most 240.')
	if max_size is not None and max_size < 16:
		raise VizardToolError('max_size must be at least 16 pixels.')


def _finish_video(res, fmt, crf, fps, max_size):
	"""Turn Vizard's AVI into an MP4 when asked. The AVI is deleted after a successful
	conversion and kept, with the reason, when the conversion fails."""
	if fmt == 'avi':
		res['format'] = 'avi'
		return res
	avi = res['path']
	mp4 = os.path.splitext(avi)[0] + '.mp4'
	try:
		out = video.convert_to_mp4(avi, mp4, crf=crf, fps=fps, max_size=max_size)
	except video.VideoError as e:
		return dict(res, format='avi', mp4_converted=False,
		            mp4_error='%s\nThe uncompressed AVI was kept at %s' % (e, avi))
	try:
		os.remove(avi)
	except OSError as e:
		out['note'] = 'could not delete the AVI %s: %s' % (avi, e)
	return dict(res, path=out['path'], bytes=out['bytes'], format='mp4', mp4_converted=True,
	            avi_bytes=res['bytes'], avi_deleted='note' not in out, ffmpeg=out['ffmpeg'],
	            ffmpeg_source=out['ffmpeg_source'], **({'note': out['note']} if 'note' in out else {}))


@mcp.tool()
def record_video(seconds: float = 5.0, path: str | None = None, format: str = 'mp4', crf: int = 23,
                 fps: float | None = None, max_size: int | None = None) -> str:
	"""Record the Vizard window to a video file, for people to watch (use filmstrip to look at
	motion yourself). Returns the file path.

	format: "mp4" (default) records with Vizard's built-in recorder, which writes an
	        uncompressed AVI (about 25 MB per second at 800x600), then converts it here to
	        H.264 MP4 (yuv420p, even dimensions, fast-start) and deletes the AVI. If no ffmpeg
	        can be found, or the conversion fails, the AVI is kept and the result says why.
	        ffmpeg is looked for in $VIZMCP_FFMPEG, on PATH, then in the imageio-ffmpeg package
	        (pip install "vizard-mcp[video]"). "avi" keeps the raw AVI.
	crf: H.264 quality, 0..51, lower is better and larger (default 23).
	fps: resample the output to this frame rate (default: keep the recording's).
	max_size: shrink so the longest side is at most this many pixels (never enlarges)."""
	_check_video_options(format, crf, fps, max_size)
	_require_local_files('record_video')
	avi, _ = _video_paths(path, format)
	res = _call('record_video', wait_s=seconds + 120, seconds=seconds, path=avi)
	return _dump(_finish_video(res, format, crf, fps, max_size))


STEP_FRAME_LIMIT = 20000
STEPS_NOTE = ('The simulation kept running in wall time (%.1f s) while the camera was stepped through %.1f s of '
              'path, so motion in the scene is not frame-exact; only the camera path is.')


def _tour_steps(path, crf, fps, max_size, total, settle, **tour):
	"""record_tour mode "steps": the app captures numbered PNGs, they are encoded to MP4 here."""
	frames = max(1, round(fps * total))
	if frames > STEP_FRAME_LIMIT:
		raise VizardToolError('%d frames (fps x duration) is more than the limit of %d; lower fps or duration.'
		                      % (frames, STEP_FRAME_LIMIT))
	res = _call('record_tour', wait_s=frames * 0.3 * (settle + 3) + 120, mode='steps', fps=fps,
	            settle_frames=settle, **tour)
	folder, pattern = res['frames_dir'], res['frame_pattern']
	mp4 = os.path.splitext(os.path.abspath(path))[0] + '.mp4' if path else folder + '.mp4'
	res['simulation_note'] = STEPS_NOTE % (res['wall_s'], res['duration'])
	try:
		out = video.encode_sequence(folder, pattern, mp4, res['fps'], crf=crf, max_size=max_size)
	except video.VideoError as e:
		return dict(res, format='png', mp4_converted=False,
		            mp4_error='%s\nThe %d PNG frames were kept in %s' % (e, res['frames'], folder))
	left = 0
	for k in range(res['frames']):
		try:
			os.remove(os.path.join(folder, pattern % k))
		except OSError:
			left += 1
	try:
		os.rmdir(folder)
	except OSError:
		pass
	res.pop('frames_dir'), res.pop('frame_pattern')
	res.update(path=out['path'], bytes=out['bytes'], format='mp4', mp4_converted=True, ffmpeg=out['ffmpeg'],
	           ffmpeg_source=out['ffmpeg_source'], frames_deleted=left == 0)
	if left:
		res['note'] = 'could not delete %d of the PNG frames in %s' % (left, folder)
	return res


@mcp.tool(structured_output=False)
def record_tour(keyframes: list[dict[str, Any]] | None = None, orbit: str | None = None,
                target: list[float] | None = None, radius: float | None = None,
                elevation_deg: float = 25.0, degrees: float = 360.0, start_yaw_deg: float = 45.0,
                duration: float | None = None, interp: str = 'catmull', path: str | None = None,
                format: str = 'mp4', crf: int = 23, fps: float | None = None, max_size: int | None = None,
                dry_run: bool = False, count: int = 6, preview_size: int = 1400,
                mode: str = 'realtime', settle_frames: int = 2) -> Any:
	"""Record a video while the camera flies along a path (a fly-through or an orbit). The view
	is held for the duration and then given back exactly as the user had it. The output is MP4
	like record_video (see there for format, crf, fps, max_size and ffmpeg).

	Give one of:
	keyframes: list of {"position": [x,y,z], "euler": [yaw,pitch,roll] OR "look_at": [x,y,z],
	           "t": seconds}. Use euler in all keyframes or look_at in all. Positions (and look_at
	           points) follow a Catmull-Rom spline through the keyframes by default; angles turn
	           the short way round. t is optional for all keyframes (they are then spread evenly
	           over `duration`); the path holds its last pose if duration is longer.
	orbit / target: circle around a node (id or expression; radius defaults to 2.8 x its
	           half-size) or a world point (radius defaults to 5), looking at it, from
	           elevation_deg above the horizontal, turning `degrees` (360 = one full circle,
	           negative = the other way) starting at yaw start_yaw_deg, over `duration` (default 8 s).
	interp: "catmull" (smooth through the keyframes), "smoothstep" (eases to a stop at each) or "linear".
	dry_run: record nothing; return a grid image of `count` (2..12) frames along the path
	         (preview_size = longest side of the grid) so the path can be judged first.
	mode: "realtime" (default) records with Vizard's recorder while the camera follows the clock,
	      so the video is as smooth as the app renders (a heavy scene on an integrated GPU may
	      draw only ~10 fps). "steps" makes a smooth video of any scene: for each of fps x duration
	      output frames the camera is put at t = k / fps, `settle_frames` rendered frames are
	      waited, and the window is captured to a numbered PNG; the PNGs are then encoded here to
	      H.264 MP4 at `fps` (default 30; yuv420p, even dimensions, fast-start) and deleted (kept if
	      encoding fails). It takes as long as rendering and capturing each frame, which the result
	      reports (frames, wall_s). The simulation keeps running in wall time while the camera is
	      stepped, so only the camera path is frame-exact, not motion in the scene. steps writes
	      MP4 only; fps is the output rate, max_size and crf work as in record_video.
	settle_frames: with mode "steps", rendered frames to wait after moving the camera and before
	      each capture (default 2).

	Vizard is Y-up, left-handed; euler is [yaw, pitch, roll] in degrees."""
	_check_video_options(format, crf, fps, max_size)
	if not dry_run:
		_require_local_files('record_tour')
	if (keyframes is None) == (orbit is None and target is None):
		raise VizardToolError('Give keyframes, or orbit / target to circle around (not both).')
	if interp not in ('catmull', 'smoothstep', 'linear'):
		raise VizardToolError('interp must be "catmull", "smoothstep" or "linear".')
	if duration is not None and duration <= 0:
		raise VizardToolError('duration must be more than 0.')
	if mode not in ('realtime', 'steps'):
		raise VizardToolError('mode must be "realtime" or "steps".')
	if mode == 'steps' and not dry_run:
		if format != 'mp4':
			raise VizardToolError('mode "steps" writes MP4 only; leave format as "mp4".')
		if not 0 <= settle_frames <= 60:
			raise VizardToolError('settle_frames must be 0..60.')
	if dry_run:
		n = max(2, min(12, count))
		res = _call('record_tour', wait_s=30 + 8 * n, keyframes=keyframes, orbit=orbit, target=target,
		            radius=radius, elevation_deg=elevation_deg, degrees=degrees, start_yaw_deg=start_yaw_deg,
		            duration=duration, interp=interp, dry_run=True, count=n)
		shots = res['shots']
		images = [_app_image(s['path']) for s in shots]
		captions = [s['view'].get('label') or 'frame %d' % (i + 1) for i, s in enumerate(shots)]
		text = '%s over %.2f s (dry run, nothing recorded)\n' % (res.get('path'), res['duration'])
		text += '\n'.join('%s: camera %s -> %s' % (c, s['view'].get('position'),
		                                           s['view'].get('look_at') or s['view'].get('euler'))
		                   for c, s in zip(captions, shots))
		return [_jpeg(imaging.grid(images, captions, preview_size)), text]
	last_t = max([float(k['t']) for k in keyframes or [] if isinstance(k, dict) and k.get('t') is not None] or [0])
	total = duration or last_t or 8.0
	if mode == 'steps':
		return _dump(_tour_steps(path, crf, fps or 30.0, max_size, total, settle_frames, keyframes=keyframes,
		                         orbit=orbit, target=target, radius=radius, elevation_deg=elevation_deg,
		                         degrees=degrees, start_yaw_deg=start_yaw_deg, duration=duration, interp=interp))
	avi, _ = _video_paths(path, format)
	res = _call('record_tour', wait_s=total + 120, keyframes=keyframes, orbit=orbit, target=target,
	            radius=radius, elevation_deg=elevation_deg, degrees=degrees, start_yaw_deg=start_yaw_deg,
	            duration=duration, interp=interp, out_path=avi)
	return _dump(_finish_video(res, format, crf, fps, max_size))


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


# ---------------------------------------------------------------------------
# GUI smoke test
# ---------------------------------------------------------------------------

CRAWL_SNAPSHOT = '__crawl_gui__'
DEFAULT_DENY = 'quit|exit|close|delete'
_TRACE_HEAD = re.compile(r'^Traceback \(most recent call last\)')
_EXC_LINE = re.compile(r'^[A-Za-z_][\w.]*(Error|Exception|Exit|Interrupt)\b')


def _clip_text(text, n=1500):
	return text if len(text) <= n else text[:n] + '... [%d more characters]' % (len(text) - n)


def _log_exceptions(lines):
	"""Python exceptions in get_log lines (dicts with stream and text): a Traceback plus its final
	"Type: message" line, or a bare exception line on stderr. Returns one string per exception."""
	found, trace = [], None
	for ln in lines:
		text = ln.get('text', '')
		if ln.get('stream') != 'err':
			if trace is not None:
				found.append(trace)
				trace = None
			continue
		if _TRACE_HEAD.match(text):
			if trace is not None:
				found.append(trace)
			trace = text
		elif trace is not None:
			trace += '\n' + text
			if text and not text[0].isspace():          # the exception line ends the traceback
				found.append(trace)
				trace = None
		elif _EXC_LINE.match(text):
			found.append(text)
	if trace is not None:
		found.append(trace)
	return [_clip_text(f) for f in found]


def _crawl_buttons(widgets, label, deny, max_buttons):
	"""(to_click, skipped): the visible, enabled on-screen buttons that match `label` and do not
	match `deny`. A button with no text is skipped since the deny list cannot be checked on it."""
	pick, skipped = [], []
	for w in widgets:
		text = w.get('text')
		if w.get('kind') != 'button' or not w.get('visible'):
			continue
		if w.get('enabled') is False or not w.get('on_screen'):
			skipped.append({'id': w['id'], 'label': text, 'reason': 'disabled or off screen'})
		elif not text or not text.strip():
			skipped.append({'id': w['id'], 'label': text, 'reason': 'no text, so the deny list cannot be checked'})
		elif label is not None and not label.search(text):
			continue
		elif deny is not None and deny.search(text):
			skipped.append({'id': w['id'], 'label': text, 'reason': 'matches the deny pattern'})
		else:
			pick.append(w)
	if len(pick) > max_buttons:
		skipped += [{'id': w['id'], 'label': w['text'], 'reason': 'over max_buttons'} for w in pick[max_buttons:]]
		pick = pick[:max_buttons]
	return pick, skipped


def _text_table(columns, rows):
	cells = [[str(c) for c in columns]] + [[str(c) for c in r] for r in rows]
	widths = [max(len(r[i]) for r in cells) for i in range(len(columns))]
	return '\n'.join('  '.join(c.ljust(w) for c, w in zip(r, widths)).rstrip() for r in cells)


@mcp.tool()
def crawl_gui(label: str | None = None, max_buttons: int = 10, deny: str = DEFAULT_DENY,
              settle_frames: int = 10, restore: bool = True, nodes: list[str] | None = None,
              named: bool = False, pattern: str = '', root: str | None = None, max_nodes: int = 2000,
              state: str | None = None, restore_code: str | None = None, pixel_tolerance: int = 8,
              change_threshold_pct: float = 0.05, save_dir: str | None = None, html: bool = False) -> str:
	"""A smoke test of the app's 2D GUI: click every visible, enabled button (from gui_list) one at a
	time and report what each did. Per button: capture the window, click it (click_button by id), let
	`settle_frames` frames pass, read the new console lines and Python exceptions (get_log), compare
	the window with the capture before the click (screenshot_diff), then put the scene back from a
	snapshot. Returns a table (button, changed %, errors, new log lines), the details per button, and
	the buttons that were skipped and why. The result can go into make_report (crawl_gui section).

	label: regex (case-insensitive search) that button texts must match; default all buttons.
	max_buttons: click at most this many, in gui_list order.
	deny: buttons whose text matches this regex are never clicked (default "quit|exit|close|delete";
	      pass "" to allow all, at your own risk). Buttons without text are not clicked either.
	restore: after each click, put back what the snapshot recorded. The snapshot is taken with the
	      same arguments as snapshot_save (nodes, named, pattern, root, max_nodes, state, restore_code),
	      so give at least one of nodes / named / root / state; with restore=false nothing is undone.
	pixel_tolerance: as in screenshot_diff. change_threshold_pct: a click counts as visibly changing
	      the window when more than this percentage of pixels differ.
	save_dir: folder for the before / diff images (default: the vizmcp temp folder).
	html: also return `html_section` for reports.
	Clicking is the real-mouse click of click_button (the window must be visible); buttons can have any
	effect in your app, so run this on a scene you can restore."""
	if max_buttons < 1:
		raise VizardToolError('max_buttons must be at least 1.')
	if settle_frames < 0:
		raise VizardToolError('settle_frames must be 0 or more.')
	if not 0 <= pixel_tolerance <= 255:
		raise VizardToolError('pixel_tolerance must be 0..255.')
	if change_threshold_pct < 0:
		raise VizardToolError('change_threshold_pct must be 0 or more.')
	try:
		label_re = re.compile(label, re.I) if label else None
		deny_re = re.compile(deny, re.I) if deny else None
	except re.error as e:
		raise VizardToolError('Bad regular expression: %s' % e)
	if restore and not (nodes or named or root is not None or state):
		raise VizardToolError('restore=true needs something to snapshot: give nodes, named, root or state '
		                      '(as in snapshot_save), or pass restore=false to click without undoing.')
	gui = json.loads(gui_list(visible_only=True, kinds=['button']))
	pick, skipped = _crawl_buttons(gui['widgets'], label_re, deny_re, max_buttons)
	found = len(pick) + sum(1 for x in skipped if x['reason'] == 'over max_buttons')
	out_dir = os.path.abspath(save_dir) if save_dir else os.path.join(
		tempfile.gettempdir(), 'vizmcp', 'crawl_%s' % time.strftime('%Y%m%d_%H%M%S'))
	os.makedirs(out_dir, exist_ok=True)
	results = []
	if pick and restore:
		snapshot_save(CRAWL_SNAPSHOT, nodes=nodes, named=named, pattern=pattern, root=root,
		              max_nodes=max_nodes, state=state, restore_code=restore_code)
	for n, w in enumerate(pick, 1):
		row = {'id': w['id'], 'label': w['text'], 'clicked': False, 'changed': None, 'changed_pct': None,
		       'errors': [], 'new_log_lines': 0, 'log': [], 'click_error': None, 'restore': None}
		results.append(row)
		before = os.path.join(out_dir, '%02d_before.png' % n)
		try:
			screenshot_diff(before, update_reference=True)
			seq0 = json.loads(get_log(since=0, limit=1))['last_seq']
			try:
				_call('click_button', wait_s=40, id=w['id'])
				row['clicked'] = True
			except VizardToolError as e:
				row['click_error'] = str(e)
			if row['clicked']:
				if settle_frames:
					wait(frames=settle_frames)
				lines = json.loads(get_log(since=seq0, limit=1000))['lines']
				row['new_log_lines'] = len(lines)
				row['log'] = [x['text'] for x in lines[:20]]
				row['errors'] = _log_exceptions(lines)
				diff = json.loads(screenshot_diff(before, pixel_tolerance=pixel_tolerance,
				                                  save_diff_to=os.path.join(out_dir, '%02d_diff.png' % n))[1])
				row['changed_pct'] = diff['diff_pct']
				row['size_mismatch'] = bool(diff.get('size_mismatch'))
				row['changed'] = row['size_mismatch'] or diff['diff_pct'] > change_threshold_pct
		except VizardToolError as e:
			row['tool_error'] = str(e)
		if restore:
			try:
				r = json.loads(snapshot_restore(CRAWL_SNAPSHOT))
				row['restore'] = {'restored': r['restored'], 'missing': len(r['missing']),
				                  'failed': len(r['failed']), 'state': (r.get('state') or {}).get('restored')}
				wait(frames=max(2, settle_frames // 2))
			except VizardToolError as e:
				row['restore'] = {'error': str(e)}
	columns = ['Button', 'Changed %', 'Changed', 'Errors', 'New log lines']
	rows = [[r['label'], '-' if r['changed_pct'] is None else r['changed_pct'],
	         'not clicked' if not r['clicked'] else ('yes' if r['changed'] else 'no'),
	         len(r['errors']) + (1 if r['click_error'] or r.get('tool_error') else 0), r['new_log_lines']]
	        for r in results]
	res = {
		'summary': {'buttons_found': found, 'clicked': sum(1 for r in results if r['clicked']),
		            'changed': sum(1 for r in results if r['changed']),
		            'with_errors': sum(1 for r in results if r['errors'] or r['click_error'] or r.get('tool_error')),
		            'skipped': len(skipped), 'restore': restore,
		            'restore_issues': sum(1 for r in results if r['restore'] and (
		                r['restore'].get('error') or r['restore'].get('missing') or r['restore'].get('failed')))},
		'table': {'columns': columns, 'rows': rows},
		'text_table': _text_table(columns, rows),
		'results': results, 'skipped': skipped, 'output_dir': out_dir,
		'snapshot': CRAWL_SNAPSHOT if restore and pick else None,
	}
	_last['crawl_gui'] = res
	if html:
		res = dict(res, html_section='\n'.join(report.crawl_gui_html(res)))
	return _dump(res)


@mcp.tool()
def vr_simulate(poses: dict[str, dict[str, list[float]]] | None = None, press: list[str] | None = None,
                release: list[str] | None = None, frames: int = 0, input: str | None = None,
                button_map: dict[str, int] | None = None, send_events: bool = True,
                apply_to_node: bool = True, reset: bool = False) -> str:
	"""Test a vizconnect VR app on a desktop, with no headset. Needs an app that uses vizconnect.

	poses: tracker poses held until reset, e.g. {"head": {"position": [0, 1.7, 0], "euler":
	       [30, 0, 0]}, "right_hand": {"position": [0.3, 1.2, 0.4]}}. Keys are vizconnect tracker
	       names, or the aliases head / left_hand / right_hand; a part left out keeps its value.
	press / release: input buttons by name or index, e.g. ["trigger", "grip", "menu"]; write
	       "right_hand:trigger" to pick the input device (needed when the app has several, or
	       set `input`). release=["all"] lets go of everything.
	frames: with press, hold the buttons for this many frames and then release them. 0 keeps
	       them down until a later release.
	button_map: names to button indexes, e.g. {"trigger": 0, "grip": 1} (the mapping depends on
	       the device in the vizconnect config).
	send_events: also send viz.SENSOR_DOWN_EVENT / SENSOR_UP_EVENT for presses and releases.
	apply_to_node: also move each tracker's node every frame.
	reset: undo everything. The originals are also restored when the app quits.
	With no arguments it reports what is currently simulated. Parts of this that were only
	checked offline are listed in the README."""
	if frames < 0:
		raise VizardToolError('frames must be 0 or more.')
	for who, pose in (poses or {}).items():
		if not isinstance(pose, dict) or not set(pose) <= {'position', 'euler'}:
			raise VizardToolError('poses[%r] must be {"position": [x, y, z], "euler": [yaw, pitch, roll]}.' % who)
		if any(len(v) != 3 for v in pose.values()):
			raise VizardToolError('poses[%r]: position and euler need 3 numbers each.' % who)
	return _dump(_call('vr_simulate', wait_s=frames / 5.0 + 30, poses=poses, press=press, release=release,
	                   frames=frames, input=input, button_map=button_map, send_events=send_events,
	                   apply_to_node=apply_to_node, reset=reset or None))


@mcp.tool()
def wait(frames: int = 0, seconds: float = 0.0) -> str:
	"""Let the app run until at least `frames` frames and `seconds` seconds have passed."""
	return _dump(_call('wait', wait_s=seconds + frames / 5.0 + 30, frames=frames, seconds=seconds))


@mcp.tool()
def wait_until(condition: str, limit_s: float = 60.0) -> str:
	"""Let the app run until a Python expression is true in it, checked every frame
	(e.g. "truck.x > 30" or "LAB.state == 'collapsed'"). Returns met=false after limit_s."""
	return _dump(_call('wait_until', wait_s=limit_s + 30, condition=condition, limit_s=limit_s))


# ---------------------------------------------------------------------------
# Measuring
# ---------------------------------------------------------------------------

@mcp.tool()
def watch_start(expressions: dict[str, str], name: str | None = None, every_frames: int = 1,
                max_samples: int = 20000, until: str | None = None) -> str:
	"""Start recording Python expressions from the app every `every_frames` frames, with
	the app time of each sample. Use it to measure what the simulation actually does
	(e.g. {"midspan_mm": "LAB.engine.deflection_mm()", "truck_x": "truck.getPosition()[0]"}).
	Vectors are split into .x/.y/.z columns. Recording stops at max_samples, when `until`
	becomes true, or on an error. Read it with watch_read or watch_plot."""
	return _dump(_call('watch_start', expressions=expressions, name=name, every_frames=every_frames,
	                   max_samples=max_samples, until=until))


@mcp.tool()
def watch_read(name: str, max_points: int = 100, stop: bool = False, delete: bool = False,
               csv_path: str | None = None) -> str:
	"""Read a watch: per column n, first, last, min (and its time), max (and its time),
	mean, plus up to max_points evenly spaced samples. The full data is written to a CSV
	file whose path is returned. stop: end recording. delete: forget the watch."""
	return _dump(_call('watch_read', name=name, max_points=max_points, stop=stop, delete=delete,
	                   csv_path=os.path.abspath(csv_path) if csv_path else None))


def _watch_chart(name, columns=None, title=None):
	"""(chart image, watch_read result) for a watch's columns against app time."""
	res = _call('watch_read', name=name, max_points=1500)
	cols = res['columns']
	if not res.get('samples'):
		raise VizardToolError('watch %r has no samples yet' % name)
	want = columns or cols[2:]
	missing = [c for c in want if c not in cols]
	if missing:
		raise VizardToolError('unknown columns %s; have %s' % (missing, cols[2:]))
	t = [r[0] for r in res['samples']]
	series = [(c, [r[cols.index(c)] for r in res['samples']]) for c in want]
	return imaging.line_chart(title or 'watch "%s"' % name, t, series), res


@mcp.tool(structured_output=False)
def watch_plot(name: str, columns: list[str] | None = None, title: str | None = None) -> list[Any]:
	"""Plot a watch's columns against app time as a line chart image (max marked).
	columns: which ones (default all). Columns with very different scales are clearer
	in separate plots."""
	im, res = _watch_chart(name, columns, title)
	return [_jpeg(im), 'csv: %s; status: %s' % (res.get('csv'), res['status'])]


@mcp.tool()
def watch_export(name: str, path: str, format: str | None = None,
                 columns: list[str] | None = None) -> str:
	"""Write a recorded watch (every sample, not a downsample) to a file for validation
	reports.

	path: output file; its extension (.csv or .json) picks the format unless `format` is given.
	columns: which columns to write, in that order (default all: "t" is app time in seconds,
	         "frame", then the watched values; vectors are split into .x/.y/.z columns).
	CSV has a header row. JSON is {"watch", "expressions", "stop_reason", "samples",
	"columns", "rows"} with rows as lists in column order."""
	if format is not None and format.lower() not in ('csv', 'json'):
		raise VizardToolError('format must be "csv" or "json".')
	return _dump(_call('watch_export', name=name, path=os.path.abspath(path),
	                   format=format.lower() if format else None, columns=columns))


def _validate(watch, measured, theory, constants=None, abs_tol=None, pct_tol=None, t_min=None,
              t_max=None, min_pass_pct=100.0, title=None, csv_path=None):
	"""Run the app's validate command; returns (measured-vs-theory chart, result without the series)."""
	if not measured or not theory:
		raise VizardToolError('measured and theory are required.')
	for label, v in (('abs_tol', abs_tol), ('pct_tol', pct_tol)):
		if v is not None and v < 0:
			raise VizardToolError('%s must be 0 or more.' % label)
	if not 0 <= min_pass_pct <= 100:
		raise VizardToolError('min_pass_pct must be 0..100.')
	res = _call('validate', watch=watch, measured=measured, theory=theory, constants=constants,
	            abs_tol=abs_tol, pct_tol=pct_tol, t_min=t_min, t_max=t_max, min_pass_pct=min_pass_pct,
	            csv_path=os.path.abspath(csv_path) if csv_path else None, max_points=1500)
	series = res.pop('series')
	t = [r[0] for r in series]
	im = imaging.line_chart(title or 'watch "%s": %s vs theory' % (watch, measured), t,
	                        [('measured', [r[1] for r in series]), ('theory', [r[2] for r in series])])
	return im, res


@mcp.tool(structured_output=False)
def validate(watch: str, measured: str, theory: str, constants: dict[str, float] | None = None,
             abs_tol: float | None = None, pct_tol: float | None = None, t_min: float | None = None,
             t_max: float | None = None, min_pass_pct: float = 100.0, title: str | None = None,
             csv_path: str | None = None) -> list[Any]:
	"""Compare a recorded watch with theory. For every sample the theory expression is
	evaluated and compared with the measured column; returns max / mean / RMS error, % error,
	how many samples are within tolerance, and a chart of measured vs theory.

	watch: name of the watch. measured: the column to check, e.g. "midspan_mm".
	theory: an arithmetic expression over the watch's columns (t, frame and the watched
	        labels; "pos.y" is written pos_y) and `constants`, e.g. "P*L**3/(48*E*I)" or
	        "0.5*9.81*t**2", or just a reference column name. Allowed: numbers, + - * / // % **,
	        pi e tau, and sin cos tan asin acos atan atan2 sinh cosh tanh exp log log10 log2 sqrt
	        pow fabs hypot degrees radians floor ceil copysign abs min max. Nothing else runs.
	constants: named numbers used in the expression, {"P": 1000, "L": 2.0}.
	abs_tol / pct_tol: a sample passes if |measured - theory| <= max(abs_tol, pct_tol % of
	        |theory|); give one or both. Without either there are statistics but no verdict.
	t_min / t_max: only samples in this window of app time (seconds).
	min_pass_pct: overall pass needs at least this % of samples within tolerance (default all).
	csv_path: where to write the per-sample comparison (default: the vizmcp temp folder)."""
	im, res = _validate(watch, measured, theory, constants, abs_tol, pct_tol, t_min, t_max,
	                    min_pass_pct, title, csv_path)
	return [_jpeg(im), _dump(res)]


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
def asset_check(top: int = 15, max_size_m: float = 1000.0, min_size_m: float = 0.001,
                sibling_ratio: float = 100.0, duplicate_min_primitives: int = 1000,
                texture_hog_mb: float = 32.0, max_nodes: int = 5000, check_files: bool = True,
                html: bool = False) -> str:
	"""Audit the scene's assets. Returns scene totals (primitives, vertices, drawables, textures,
	texture memory; from viz.MainScene.getStats()) and, from the per-node getStats:
	top_nodes: the `top` nodes by their own primitives (a group's figure excludes its children), with
	  size, script names and file if the node reports one.
	size_outliers: nodes whose bounding box is over max_size_m or under min_size_m on its longest side, or
	  sibling_ratio times larger (or smaller) than the median of their siblings; typically a model
	  exported in the wrong unit (a GLB in millimetres or centimetres).
	empty_nodes: nodes with nothing to draw (lights, sounds and views excepted); hidden_costly: hidden
	  nodes that still hold primitives; duplicates: subtrees of at least duplicate_min_primitives that
	  occur more than once (same file, else identical statistics) and the primitives they waste;
	  texture_hogs: nodes by their own texture memory, flagged from texture_hog_mb; files: file names
	  found (getFilename / getName; null where a node has none) and the ones missing on disk.
	html: also return `html_section`, a fragment for reports (make_report has an asset_check section).
	Only the figures this Vizard provides are filled in; notes say what was unavailable."""
	for name, value in (('top', top), ('duplicate_min_primitives', duplicate_min_primitives)):
		if value < 0:
			raise VizardToolError('%s must be 0 or more.' % name)
	for name, value in (('max_size_m', max_size_m), ('min_size_m', min_size_m), ('sibling_ratio', sibling_ratio),
	                    ('texture_hog_mb', texture_hog_mb)):
		if not value > 0:
			raise VizardToolError('%s must be above 0.' % name)
	if max_nodes < 1:
		raise VizardToolError('max_nodes must be at least 1.')
	res = _call('asset_check', wait_s=120, top=top, max_size_m=max_size_m, min_size_m=min_size_m,
	            sibling_ratio=sibling_ratio, duplicate_min_primitives=duplicate_min_primitives,
	            texture_hog_mb=texture_hog_mb, max_nodes=max_nodes, check_files=check_files)
	_last['asset_check'] = res
	if html:
		res = dict(res, html_section='\n'.join(report.asset_check_html(res, top or 15)))
	return _dump(res)


@mcp.tool()
def profile(frames: int = 120, top: int = 25, sort: str = 'self') -> str:
	"""Profile all Python code that runs on the app's main thread for `frames` frames.
	Returns frame time (mean, p95, max), FPS, Python milliseconds per frame, and the top
	functions by self time (sort="self") or including callees (sort="total")."""
	return _dump(_call('profile', wait_s=frames / 5.0 + 60, frames=frames, top=top, sort=sort))


# ---------------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------------

@mcp.tool()
def snapshot_save(name: str, nodes: list[str] | None = None, named: bool = False, pattern: str = '',
                  root: str | None = None, max_nodes: int = 2000, state: str | None = None,
                  restore_code: str | None = None, path: str | None = None) -> str:
	"""Remember how part of the scene looks now, to put it back later with snapshot_restore (try
	something, then undo it). Records each node's local position, orientation (quaternion) and scale,
	and whether it is visible, under `name` in the app (a second save under the same name replaces it).
	Choose the nodes with any of:
	nodes: ids or Python expressions, e.g. ["ball", "LAB.site.crane.hook", 123].
	named: every node the script has a name for (as find_nodes lists them); `pattern` keeps only
	       names containing that text (case-insensitive).
	root: this node and everything under it.
	max_nodes: stop at this many nodes (the result says if it was cut off).
	state: optionally also keep app state: a Python expression whose value is copied, e.g.
	       "LAB.sim.state()". Give restore_code, a statement that puts it back, with the copy
	       available as `_state`, e.g. "LAB.sim.set_state(_state)".
	path: also write the snapshot to this JSON file (snapshot_restore can read it in a later session
	      of the same script). Nodes given by name are found again by name; nodes given by id or
	      without a name only in the app that saved them. State is written only if it is plain
	      JSON data (numbers, strings, lists, dicts).
	Camera and material colour are not recorded; use camera for the view. Scripts that move a node
	every frame will overwrite a restored transform."""
	if not name.strip():
		raise VizardToolError('name is required.')
	if not (nodes or named or root is not None or state):
		raise VizardToolError('Give nodes, named, root or state: nothing to record.')
	if restore_code and not state:
		raise VizardToolError('restore_code needs state (an expression that returns the value to keep).')
	if max_nodes < 1:
		raise VizardToolError('max_nodes must be at least 1.')
	return _dump(_call('snapshot_save', name=name, nodes=nodes or None, named=named or None,
	                   pattern=pattern or None, root=root, max_nodes=max_nodes, state=state,
	                   restore_code=restore_code, path=os.path.abspath(path) if path else None))


@mcp.tool()
def snapshot_restore(name: str | None = None, path: str | None = None,
                     restore_code: str | None = None) -> str:
	"""Put a snapshot back: first the app state (if the snapshot has some), then every recorded
	node's position, orientation, scale and visibility. A node that no longer exists, or cannot
	be found again, is listed under `missing` with the reason; the rest are still restored.
	`failed` lists values a node refused. The snapshot stays and can be restored again.

	name: a snapshot saved with snapshot_save (see snapshot_list).
	path: a snapshot JSON file to read instead (it is also kept in the app under its name, or
	      under `name` if you give one).
	restore_code: use this statement instead of the one saved with the state; the saved copy of
	      the state is available as `_state`."""
	if not name and not path:
		raise VizardToolError('Give name, or path of a snapshot file.')
	return _dump(_call('snapshot_restore', name=name, path=os.path.abspath(path) if path else None,
	                   restore_code=restore_code))


@mcp.tool()
def snapshot_list(path: str | None = None) -> str:
	"""List the snapshots kept in the app: name, when it was taken, how many nodes, whether it has
	app state and restore code, and its file if it has one. With `path`, describe a snapshot JSON
	file instead (read here, the app is not needed)."""
	if path:
		path = os.path.abspath(path)
		try:
			with open(path, 'r', encoding='utf-8') as f:
				doc = json.load(f)
		except (OSError, ValueError) as e:
			raise VizardToolError('Cannot read the snapshot file %s: %s' % (path, e))
		if not isinstance(doc, dict) or doc.get('format') != 'vizmcp-snapshot':
			raise VizardToolError('%s is not a vizmcp snapshot file.' % path)
		st = doc.get('state') or {}
		return _dump({'file': path, 'name': doc.get('name'), 'created': doc.get('created'),
		              'version': doc.get('version'), 'script': doc.get('script'),
		              'nodes': len(doc.get('nodes') or []), 'truncated': doc.get('truncated'),
		              'state_expression': st.get('expression'), 'restore_code': bool(st.get('restore_code')),
		              'state_in_file': bool(st.get('value_exact'))})
	return _dump(_call('snapshot_list'))


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

_REPORT_KEYS = {
	'heading': ({'text'}, {'level'}),
	'text': ({'text'}, set()),
	'watch': ({'name'}, {'columns', 'title', 'caption'}),
	'validate': ({'watch', 'measured', 'theory'}, {'constants', 'abs_tol', 'pct_tol', 't_min', 't_max',
	                                              'min_pass_pct', 'title', 'caption'}),
	'screenshot': (set(), {'annotate', 'label_limit', 'caption'}),
	'image': ({'path'}, {'caption'}),
	'keyvalue': ({'items'}, {'caption'}),
	'asset_check': (set(), {'result', 'top', 'caption'}),
	'crawl_gui': (set(), {'result', 'caption'}),
}
_REPORT_ALIASES = {'table': 'keyvalue', 'kv': 'keyvalue', 'paragraph': 'text', 'markdown': 'text'}


def _check_report_sections(sections):
	"""Validate every section before the app is touched; returns them with canonical types."""
	if not sections:
		raise VizardToolError('sections is empty: give at least one section.')
	out = []
	for i, sec in enumerate(sections, 1):
		where = 'section %d' % i
		if not isinstance(sec, dict) or not isinstance(sec.get('type'), str):
			raise VizardToolError('%s must be an object with a "type" (%s).' % (where, ', '.join(_REPORT_KEYS)))
		kind = _REPORT_ALIASES.get(sec['type'], sec['type'])
		if kind not in _REPORT_KEYS:
			raise VizardToolError('%s: unknown type %r; use one of %s.' % (where, sec['type'], ', '.join(_REPORT_KEYS)))
		need, may = _REPORT_KEYS[kind]
		given = set(sec) - {'type'}
		missing = [k for k in sorted(need) if sec.get(k) in (None, '')]
		if missing:
			raise VizardToolError('%s (%s) needs %s.' % (where, kind, ', '.join(missing)))
		extra = sorted(given - need - may)
		if extra:
			raise VizardToolError('%s (%s): unknown %s; it takes %s.' % (
				where, kind, ', '.join(extra), ', '.join(sorted(need | may)) or 'nothing else'))
		if kind == 'image' and not os.path.isfile(sec['path']):
			raise VizardToolError('%s (image): no such file: %s' % (where, sec['path']))
		if 'result' in sec and not isinstance(sec['result'], dict):
			raise VizardToolError('%s (%s): result must be the object the %s tool returned.' % (where, kind, kind))
		if kind == 'crawl_gui' and 'result' not in sec and 'crawl_gui' not in _last:
			raise VizardToolError('%s (crawl_gui): run the crawl_gui tool first, or pass its result.' % where)
		if kind == 'keyvalue':
			items = sec['items']
			if not (isinstance(items, dict) or (isinstance(items, list) and all(
					isinstance(x, (list, tuple)) and len(x) == 2 for x in items))):
				raise VizardToolError('%s (keyvalue): items must be an object or a list of [key, value] pairs.' % where)
		out.append(dict(sec, type=kind))
	return out


def _report_info():
	"""Header lines about the app and Vizard, or a note that no app answered."""
	try:
		info = _call('info')
	except VizardToolError:
		return [('Vizard app', 'not connected when the report was made')]
	fps = info.get('fps')
	rows = [('Vizard', info.get('vizard_version')), ('Script', info.get('script')),
	        ('Window', 'x'.join(str(v) for v in info.get('window_size') or [])),
	        ('Frame rate', '%.1f fps' % fps if fps else None), ('Scene nodes', info.get('node_count')),
	        ('vizmcp', info.get('vizmcp_version')), ('Python (in Vizard)', info.get('python'))]
	return [(k, v) for k, v in rows if v not in (None, '')]


def _report_watch(sec, max_size):
	im, res = _watch_chart(sec['name'], sec.get('columns'), sec.get('title'))
	f = report.fmt
	cols = sec.get('columns') or res['columns'][2:]
	rows = []
	for c in cols:
		st = res['stats'].get(c)
		if st is None:          # t and frame have no statistics
			continue
		rows.append([c, st['n'], f(st['first']), f(st['last']), '%s (t=%s)' % (f(st['min']), f(st['t_min'])),
		             '%s (t=%s)' % (f(st['max']), f(st['t_max'])), f(st['mean'])])
	status = res['status']
	cap = sec.get('caption') or 'Watch "%s": %d samples, %s.' % (
		sec['name'], status['samples'], 'still recording' if status['active'] else 'stopped (%s)' % status['stop_reason'])
	return [report.table(['Column', 'n', 'first', 'last', 'min', 'max', 'mean'], rows, cap),
	        report.figure(imaging.to_png(im), 'image/png', alt='Chart of watch %s' % sec['name'])], None


def _report_validate(sec, max_size):
	im, res = _validate(sec['watch'], sec['measured'], sec['theory'], sec.get('constants'), sec.get('abs_tol'),
	                    sec.get('pct_tol'), sec.get('t_min'), sec.get('t_max'), sec.get('min_pass_pct', 100.0),
	                    sec.get('title'), None)
	passed = res.get('passed')
	if passed is None:
		line = 'No tolerance was given, so there is no pass/fail verdict.'
	else:
		line = '%d of %d samples within tolerance (%.1f %%, %.1f %% required).' % (
			res['passing_samples'], res['samples'], res['pass_pct'], res['min_pass_pct'])
	f = report.fmt
	rows = [('Measured column', '%s of watch "%s"' % (res['measured'], res['watch'])), ('Theory', res['theory'])]
	if res.get('constants'):
		rows.append(('Constants', ', '.join('%s = %s' % (k, f(v)) for k, v in res['constants'].items())))
	if res.get('abs_tol') is not None or res.get('pct_tol') is not None:
		rows.append(('Tolerance', ' and '.join(x for x in (
			'%s absolute' % f(res['abs_tol']) if res.get('abs_tol') is not None else '',
			'%s %% of theory' % f(res['pct_tol']) if res.get('pct_tol') is not None else '') if x)))
	rows += [('Samples compared', '%d (%d skipped)' % (res['samples'], res['skipped'])),
	         ('Max absolute error', '%s at t = %s s' % (f(res['max_abs_error']), f(res['t_of_max_error']))),
	         ('Mean absolute error', f(res['mean_abs_error'])), ('RMS error', f(res['rms_error'])), ('Bias', f(res['bias'])),
	         ('Max error, % of theory', f(res['max_pct_error'])), ('Mean error, % of theory', f(res['mean_pct_error']))]
	if res.get('first_failure'):
		ff = res['first_failure']
		rows.append(('First failure', 't = %s s: measured %s, theory %s' % (f(ff['t']), f(ff['measured']), f(ff['theory']))))
	html = ['<p class="verdict">%s &nbsp; %s</p>' % (report.badge(passed), report.esc(line)),
	        report.keyvalue(rows, sec.get('caption')),
	        report.figure(imaging.to_png(im), 'image/png', alt='Measured against theory')]
	return html, {'watch': res['watch'], 'measured': res['measured'], 'passed': passed,
	              'samples': res['samples'], 'max_abs_error': res['max_abs_error']}


def _report_screenshot(sec, max_size):
	im, _ = _take_screenshot(None, bool(sec.get('annotate')), int(sec.get('label_limit', 25)))
	return [report.figure(imaging.to_jpeg(imaging.fit(im, max_size)), 'image/jpeg',
	                      sec.get('caption') or 'Screenshot of the Vizard window', 'Screenshot')], None


def _report_asset_check(sec, max_size):
	res = sec.get('result') or json.loads(asset_check())
	title = sec.get('caption')
	return ([report.markdown(title)] if title else []) + report.asset_check_html(res, int(sec.get('top', 15))), None


def _report_crawl_gui(sec, max_size):
	res = sec.get('result') or _last['crawl_gui']
	title = sec.get('caption')
	return ([report.markdown(title)] if title else []) + report.crawl_gui_html(res), None


def _report_image(sec, max_size):
	try:
		im = imaging.load(sec['path'])
	except OSError as e:
		raise VizardToolError('Cannot read the image %s: %s' % (sec['path'], e))
	im = imaging.fit(im, max_size)
	if sec['path'].lower().endswith('.png'):
		data, mime = imaging.to_png(im), 'image/png'
	else:
		data, mime = imaging.to_jpeg(im), 'image/jpeg'
	return [report.figure(data, mime, sec.get('caption') or os.path.basename(sec['path']))], None


@mcp.tool()
def make_report(sections: list[dict[str, Any]], path: str | None = None, title: str = 'Vizard report',
                author: str = 'Zarrar Abbas', date: str | None = None, max_image_size: int = 1400) -> str:
	"""Write a self-contained HTML report (images embedded as base64, a print-friendly stylesheet,
	no other files): open it in a browser, or print it to save a PDF. Returns the file path and a
	summary. The header has the title, author, date and what is known of the running app and Vizard
	(version, script, window, frame rate, node count; "not connected" if no app answers).

	sections: a list of objects, each with a "type":
	  {"type": "heading", "text": "Results", "level": 2}    (level 2..5)
	  {"type": "text", "text": "..."}   blank-line separated paragraphs, "# " headings, "- " and
	        "1. " lists, **bold**, *italic*, `code`, [links](https://...). HTML is escaped.
	  {"type": "watch", "name": "bounce", "columns": ["h"], "title": "...", "caption": "..."}
	        table of first / last / min / max / mean per column and a line chart of the watch.
	  {"type": "validate", "watch": "bounce", "measured": "h", "theory": "h0 - v**2/(2*g)",
	        "constants": {...}, "abs_tol": 0.02, "pct_tol": null, "t_min": null, "t_max": null,
	        "min_pass_pct": 100}   the validate tool: a PASS / FAIL badge (NO VERDICT without a
	        tolerance), error statistics and the measured-vs-theory chart.
	  {"type": "screenshot", "annotate": false, "caption": "..."}   the Vizard window as it is now.
	  {"type": "image", "path": "C:/out/photo.png", "caption": "..."}   an existing PNG or JPEG.
	  {"type": "asset_check", "result": {...}, "top": 15, "caption": "..."}   the asset_check tool's tables
	        (runs it now unless `result`, an earlier asset_check result, is given).
	  {"type": "crawl_gui", "result": {...}, "caption": "..."}   the latest crawl_gui result of this server
	        (or the `result` you pass); a crawl clicks buttons, so a report never starts one.
	  {"type": "keyvalue", "items": {"Load": "1 kN", "Span": "2 m"}, "caption": "..."}   a two-column
	        table; items may also be a list of [key, value] pairs. ("table" is accepted too.)
	The arguments of every section are checked before anything is taken from the app; a section
	that fails (unknown watch, ...) stops the report with the section number, nothing is written.
	path: output .html file (default: the vizmcp temp folder). date: default today.
	max_image_size: longest side of embedded screenshots and images, in pixels."""
	sections = _check_report_sections(sections)
	if max_image_size < 16:
		raise VizardToolError('max_image_size must be at least 16 pixels.')
	builders = {'watch': _report_watch, 'validate': _report_validate, 'screenshot': _report_screenshot,
	            'image': _report_image, 'asset_check': _report_asset_check, 'crawl_gui': _report_crawl_gui}
	body, results, images = [], [], 0
	for i, sec in enumerate(sections, 1):
		kind = sec['type']
		if kind == 'heading':
			body.append(report.heading(sec['text'], sec.get('level', 2)))
		elif kind == 'text':
			body.append(report.markdown(sec['text']))
		elif kind == 'keyvalue':
			body.append(report.keyvalue(sec['items'], sec.get('caption')))
		else:
			try:
				html, result = builders[kind](sec, max_image_size)
			except VizardToolError as e:
				raise VizardToolError('Report not written. Section %d (%s): %s' % (i, kind, e))
			body.extend(html)
			images += sum(h.count('<img ') for h in html)
			if result:
				results.append(dict(result, section=i))
	when = date or time.strftime('%Y-%m-%d')
	doc = report.document(title, author, when, _report_info(), body)
	out = os.path.abspath(path) if path else os.path.join(tempfile.gettempdir(), 'vizmcp',
	                                                       'report_%s.html' % time.strftime('%Y%m%d_%H%M%S'))
	os.makedirs(os.path.dirname(out), exist_ok=True)
	with open(out, 'w', encoding='utf-8', newline='\n') as f:
		f.write(doc)
	summary = {'path': out, 'bytes': os.path.getsize(out), 'sections': len(sections), 'images': images,
	           'validations': results}
	if results:
		summary['all_validations_passed'] = (all(r['passed'] for r in results)
		                                     if all(r['passed'] is not None for r in results) else None)
	return _dump(summary)


@mcp.tool()
def doctor() -> str:
	"""Check the vizard-mcp installation and print PASS / WARN / FAIL lines with a fix for each:
	Python, mcp and Pillow versions, winviz.exe and the Vizard folder, the vizmcp port, ffmpeg
	for MP4 videos, and whether the server is registered in Claude Code and Claude Desktop with a
	command path that exists. The only network traffic is one ping to the configured target
	(VIZMCP_HOST:VIZMCP_PORT); for a remote host the report says whether it answers."""
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
