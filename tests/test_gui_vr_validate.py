"""gui_list / click_button, vr_simulate and validate: vizmcp commands against a stand-in for
Vizard's `viz` and `vizconnect` modules, and the server tools against a fake vizmcp.

What this cannot show is how real Vizard 8 widgets and vizconnect objects behave; the
stand-ins follow the calls the code makes. See README, "What still needs a live Vizard run".
"""

import asyncio
import inspect
import json
import math
import sys
import types

import pytest
from PIL import Image

from mcp.server.mcpserver.exceptions import ToolError
from test_new_tools import app, sent          # noqa: F401  (fixture)
from test_vizard_side import FakeNode, make_watch, vm      # noqa: F401  (fixture)
from vizard_mcp import server as S


def run(coro):
	return asyncio.run(coro)


def drive(vm, result, frames=1000):
	"""Run a command the way the pump does: a generator advances once per frame."""
	if not inspect.isgenerator(result):
		return result
	for _ in range(frames):
		try:
			next(result)
		except StopIteration as e:
			return e.value
		vm.viz_stub.frame += 1
	raise AssertionError('command never finished')


# --- stand-in widgets ---------------------------------------------------------

class Box(object):
	def __init__(self, cx, cy, w, h):
		self.center = [cx, cy, 0]
		self.xmin, self.xmax, self.ymin, self.ymax = cx - w / 2, cx + w / 2, cy - h / 2, cy + h / 2

	def valid(self):
		return True


def widget(cls_name, text=None, center=(0.5, 0.5), size=(0.1, 0.05), parent=None, **extra):
	cls = type(cls_name, (FakeNode,), {})
	node = cls(getParents=lambda: [parent] if parent is not None else [],
	           getBoundingBox=lambda: Box(center[0], center[1], size[0], size[1]),
	           getPosition=lambda mode=None: [1.0, 2.0, 3.0], **extra)
	if text is not None:
		node.getMessage = lambda: text
	return node


@pytest.fixture
def gui(vm):
	viz = vm.viz_stub
	viz.ABS_GLOBAL = 1
	viz.MainScene = types.SimpleNamespace(getChildren=lambda: [])
	viz.VizNode._GetObject = staticmethod(lambda nid: next((n for n in viz.nodes if n.id == nid), None))
	clicks = []

	def fake_click(p):
		clicks.append(p)
		yield
		return {'clicked': [1, 2]}
	vm.cmd_click = fake_click
	vm.clicks = clicks
	return vm


def set_scene(vm, *nodes, world_roots=()):
	vm.viz_stub.nodes = list(nodes)
	vm.viz_stub.MainScene.getChildren = lambda: list(world_roots)


# --- gui_list -----------------------------------------------------------------

def test_gui_list_reports_screen_widgets_with_top_left_coordinates(gui):
	b = widget('VizButton', 'Reset ball', center=(0.15, 0.92), size=(0.2, 0.06), getEnabled=lambda: True)
	lab = widget('VizText', 'Score: 3', center=(0.5, 0.5))
	sl = widget('VizSlider', center=(0.5, 0.1), get=lambda: 0.25)
	ground = FakeNode()                              # not a widget
	set_scene(gui, b, lab, sl, ground)
	res = json.loads(json.dumps(gui._jsonable(gui.cmd_gui_list({}))))
	by_id = {w['id']: w for w in res['widgets']}
	assert set(by_id) == {b.id, lab.id, sl.id}
	w = by_id[b.id]
	assert w['kind'] == 'button' and w['type'] == 'VizButton' and w['text'] == 'Reset ball'
	assert w['center'] == [0.15, 0.08] and w['size'] == [0.2, 0.06]       # y flipped to top-left origin
	assert w['visible'] is True and w['enabled'] is True and w['on_screen'] is True
	assert by_id[lab.id]['kind'] == 'label' and by_id[lab.id]['enabled'] is None
	assert by_id[sl.id]['kind'] == 'slider' and by_id[sl.id]['value'] == 0.25 and by_id[sl.id]['text'] is None
	assert res['world_widgets'] == []


def test_gui_list_separates_world_space_widgets(gui):
	tablet = FakeNode()                                    # a vizconnect wrist/tablet node in the 3D scene
	tablet.getParents = lambda: []
	panel_btn = widget('VizButton', 'Grab', parent=tablet)
	text3d = widget('VizText', 'sign', parent=tablet)      # 3D text is not a GUI widget
	screen_btn = widget('VizButton', 'OK')
	set_scene(gui, tablet, panel_btn, text3d, screen_btn, world_roots=[tablet])
	res = gui.cmd_gui_list({})
	assert [w['text'] for w in res['widgets']] == ['OK']
	assert [w['text'] for w in res['world_widgets']] == ['Grab']
	assert res['world_widgets'][0]['world_position'] == [1.0, 2.0, 3.0]
	assert 'center' not in res['world_widgets'][0]


def test_gui_list_visibility_filters_and_off_screen(gui):
	canvas = widget('VizGUICanvas', visible=False)
	canvas.getVisible = lambda: False
	kid = widget('VizButton', 'Inside hidden canvas', parent=canvas)
	hidden = widget('VizButton', 'Hidden')
	hidden.getVisible = lambda: False
	away = widget('VizButton', 'Away', center=(1.5, 0.5))
	shown = widget('VizButton', 'Shown')
	set_scene(gui, canvas, kid, hidden, away, shown)
	allw = {w['text']: w for w in gui.cmd_gui_list({})['widgets'] if w['text']}
	assert allw['Hidden']['visible'] is False and allw['Inside hidden canvas']['visible'] is False
	assert allw['Away']['on_screen'] is False
	assert [w['text'] for w in gui.cmd_gui_list({'visible_only': True})['widgets']] == ['Away', 'Shown']
	assert [w['text'] for w in gui.cmd_gui_list({'kinds': ['button'], 'visible_only': True})['widgets']] == ['Away', 'Shown']
	assert gui.cmd_gui_list({'kinds': ['slider']})['widgets'] == []


def test_gui_list_survives_missing_bounding_box(gui):
	b = widget('VizButton', 'X')
	b.getBoundingBox = lambda: (_ for _ in ()).throw(RuntimeError('no'))
	set_scene(gui, b)
	w = gui.cmd_gui_list({})['widgets'][0]
	assert w['center'] is None and w['on_screen'] is False


# --- click_button ---------------------------------------------------------------

def click(gui, **p):
	return drive(gui, gui.cmd_click_button(p))


def test_click_button_exact_then_case_insensitive_partial(gui):
	reset = widget('VizButton', 'Reset ball', center=(0.2, 0.9))
	quit_ = widget('VizButton', 'Quit', center=(0.8, 0.9))
	reset2 = widget('VizButton', 'reset', center=(0.4, 0.9))
	set_scene(gui, reset, quit_, reset2)
	res = click(gui, label='reset')                       # exact beats partial
	assert res['widget']['id'] == reset2.id
	assert gui.clicks[-1] == {'x': 0.4, 'y': pytest.approx(0.1), 'button': 'left', 'double': False}
	assert click(gui, label='QUIT')['widget']['id'] == quit_.id         # ignoring case
	assert click(gui, label='ball')['widget']['id'] == reset.id         # partial
	res = click(gui, label='Quit', button='right', double=True)
	assert gui.clicks[-1]['button'] == 'right' and gui.clicks[-1]['double'] is True
	assert res['click'] == {'clicked': [1, 2]}


def test_click_button_ambiguous_lists_candidates(gui):
	set_scene(gui, widget('VizButton', 'Start A'), widget('VizButton', 'Start B'))
	with pytest.raises(ValueError, match=r'ambiguous.*Start A.*Start B'):
		click(gui, label='start')
	assert gui.clicks == []


def test_click_button_not_found_lists_candidates(gui):
	set_scene(gui, widget('VizButton', 'Start'), widget('VizText', 'A label'))
	with pytest.raises(ValueError, match=r"no widget with text matching 'nope'.*Start") as e:
		click(gui, label='nope')
	assert 'A label' not in str(e.value)                  # labels are not click targets


def test_click_button_skips_hidden_disabled_and_world(gui):
	hidden = widget('VizButton', 'Go')
	hidden.getVisible = lambda: False
	disabled = widget('VizButton', 'Go too', getEnabled=lambda: False)
	tablet = FakeNode()
	tablet.getParents = lambda: []
	world = widget('VizButton', 'Go 3D', parent=tablet)
	set_scene(gui, hidden, disabled, tablet, world, world_roots=[tablet])
	with pytest.raises(ValueError, match='hidden, disabled or off screen'):
		click(gui, label='Go')
	with pytest.raises(ValueError, match='3D scene'):
		click(gui, id=world.id)
	with pytest.raises(ValueError, match='no 2D widget with id 999'):
		click(gui, id=999)
	good = widget('VizButton', 'Go for it')
	set_scene(gui, hidden, disabled, good)
	assert click(gui, label='go')['widget']['id'] == good.id


def test_click_button_by_id_and_argument_check(gui):
	a, b = widget('VizButton', 'Same', center=(0.1, 0.5)), widget('VizButton', 'Same', center=(0.9, 0.5))
	set_scene(gui, a, b)
	assert click(gui, id=b.id)['widget']['id'] == b.id and gui.clicks[-1]['x'] == 0.9
	with pytest.raises(ValueError, match='label or id'):
		click(gui)


def test_gui_commands_registered(vm):
	assert {'gui_list', 'click_button', 'vr_simulate', 'validate'} <= set(vm.COMMANDS)


# --- vr_simulate ----------------------------------------------------------------

class FakeTracker(object):
	def __init__(self, pos, euler, with_node=True):
		self._p, self._e = pos, euler
		self.node = types.SimpleNamespace(pos=None, euler=None)
		self.node.setPosition = lambda v: setattr(self.node, 'pos', list(v))
		self.node.setEuler = lambda v: setattr(self.node, 'euler', list(v))
		self.with_node = with_node

	def getPosition(self, mode=None):
		return list(self._p)

	def getEuler(self, mode=None):
		return list(self._e)

	def getNode3d(self):
		return self.node


class FakeInput(object):
	def __init__(self, bits=0):
		self.bits = bits
		self.raw = object()

	def isButtonDown(self, b):
		return b == 7

	def getButtonState(self):
		return self.bits

	def getRaw(self):
		return self.raw


@pytest.fixture
def vr(vm, monkeypatch):
	viz = vm.viz_stub
	viz.SENSOR_DOWN_EVENT, viz.SENSOR_UP_EVENT = 'down', 'up'
	viz.events = []
	viz.sendEvent = lambda *a: viz.events.append(a)
	holds = []

	class Handle(object):
		def __init__(self, fn):
			self.fn, self.removed = fn, False

		def remove(self):
			self.removed = True
	vm.vizact.onupdate = lambda prio, fn: holds.append(Handle(fn)) or holds[-1]
	vm.holds = holds
	head = FakeTracker([0, 1.6, 0], [0, 0, 0])
	lhand = FakeTracker([-0.2, 1, 0.3], [0, 0, 0])
	rhand = FakeTracker([0.2, 1, 0.3], [0, 0, 0])
	vc = types.ModuleType('vizconnect')
	vc.getTrackerDict = lambda: {'head': head, 'left_hand': lhand, 'right_touch_hand': rhand}
	right_in, left_in = FakeInput(), FakeInput()
	vc.getInputDict = lambda: {'left_controller': left_in, 'right_controller': right_in}
	monkeypatch.setitem(sys.modules, 'vizconnect', vc)
	vm.trk = types.SimpleNamespace(head=head, left=lhand, right=rhand)
	vm.inp = types.SimpleNamespace(left=left_in, right=right_in)
	vm.vc = vc
	yield vm
	vm._vr_restore()


def sim(vm, **p):
	return drive(vm, vm.cmd_vr_simulate(p))


def test_vr_simulate_without_vizconnect_is_a_clear_error(vm, monkeypatch):
	monkeypatch.delitem(sys.modules, 'vizconnect', raising=False)
	with pytest.raises(ValueError, match='vizconnect is not in use'):
		sim(vm, poses={'head': {'position': [0, 1, 0]}})


def test_vr_poses_override_and_restore_trackers(vr):
	head = vr.trk.head
	res = sim(vr, poses={'head': {'position': [1, 2, 3], 'euler': [30, 0, 0]},
	                     'right_hand': {'position': [0.5, 1.1, 0.4]}})
	assert head.getPosition() == [1, 2, 3] and head.getEuler() == [30, 0, 0]
	assert vr.trk.right.getPosition() == [0.5, 1.1, 0.4]
	assert vr.trk.right.getEuler() == [0, 0, 0]                      # a part left out keeps its value
	assert vr.trk.left.getPosition() == [-0.2, 1, 0.3]               # untouched tracker not overridden
	assert 'getPosition' not in vars(vr.trk.left)
	assert res['active'] is True and set(res['trackers']) == {'head', 'right_touch_hand'}
	assert head.node.pos == [1, 2, 3] and head.node.euler == [30, 0, 0]   # held onto the node
	assert len(vr.holds) == 1
	sim(vr, poses={'head': {'position': [4, 5, 6]}})                   # update in place
	assert head.getPosition() == [4, 5, 6] and head.getEuler() == [30, 0, 0] and len(vr.holds) == 1
	vr.holds[0].fn()
	assert head.node.pos == [4, 5, 6]
	assert sim(vr, reset=True)['reset'] is True
	assert vars(head) .keys() >= {'_p'} and 'getPosition' not in vars(head) and 'getEuler' not in vars(head)
	assert head.getPosition() == [0, 1.6, 0] and vr.holds[0].removed and vr._State.vr is None


def test_vr_pose_names_resolution_errors(vr):
	with pytest.raises(ValueError, match="tracker 'nose' not found.*head, left_hand, right_touch_hand"):
		sim(vr, poses={'nose': {'position': [0, 0, 0]}})
	with pytest.raises(ValueError, match='unknown keys'):
		sim(vr, poses={'head': {'pos': [0, 0, 0]}})
	assert vr._State.vr is None                                       # nothing half applied
	vr.vc.getTrackerDict = lambda: {'left_hand': vr.trk.left, 'left_wrist': vr.trk.right}
	with pytest.raises(ValueError, match='ambiguous'):
		sim(vr, poses={'left': {'position': [0, 0, 0]}})


def test_vr_buttons_hold_for_n_frames_then_release(vr):
	inp = vr.inp.right
	states = []
	gen = vr.cmd_vr_simulate({'press': ['right_controller:trigger'], 'frames': 3, 'button_map': {'trigger': 1}})
	next(gen)
	states.append((inp.isButtonDown('trigger'), inp.isButtonDown(1), inp.getButtonState(), inp.isButtonDown(7)))
	assert vr.viz_stub.events == [('down', inp.raw, 1)]
	res = drive(vr, gen)
	assert states == [(True, True, 0b10, True)]
	assert (inp.isButtonDown('trigger'), inp.getButtonState()) == (False, 0)
	assert vr.viz_stub.events[-1] == ('up', inp.raw, 1)
	assert res['events'] == ['down right_controller:trigger', 'up right_controller:trigger']
	assert vr.inp.left.isButtonDown('trigger') is False


def test_vr_press_stays_down_until_release(vr):
	inp = vr.inp.left
	res = sim(vr, press=['grip', 3], input='left')
	assert inp.isButtonDown('GRIP') and inp.isButtonDown(3) and inp.getButtonState() == 0b1000
	assert res['inputs']['left_controller']['down'] == ['3', 'grip']
	sim(vr, release=['grip'], input='left')
	assert not inp.isButtonDown('grip') and inp.isButtonDown(3)
	res = sim(vr, release=['all'])
	assert not inp.isButtonDown(3) and res['active'] is False
	assert 'isButtonDown' not in vars(inp)


def test_vr_needs_to_know_which_input(vr):
	with pytest.raises(ValueError, match='which input'):
		sim(vr, press=['trigger'])
	vr.vc.getInputDict = lambda: {'only': vr.inp.left}
	sim(vr, press=['trigger'])
	assert vr.inp.left.isButtonDown('trigger')


def test_vr_send_events_off_and_event_failure_is_reported(vr):
	sim(vr, press=['menu'], input='right', send_events=False)
	assert vr.viz_stub.events == []
	sim(vr, reset=True)
	vr.viz_stub.sendEvent = lambda *a: (_ for _ in ()).throw(RuntimeError('nope'))
	res = sim(vr, press=['menu'], input='right')
	assert res['event_errors'] == ['RuntimeError: nope'] and vr.inp.right.isButtonDown('menu')


def test_vr_quit_and_stop_restore_originals(vr):
	sim(vr, poses={'head': {'position': [9, 9, 9]}}, press=['trigger'], input='left')
	vr.cmd_quit({})
	assert vr._State.vr is None and vr.trk.head.getPosition() == [0, 1.6, 0]
	assert not vr.inp.left.isButtonDown('trigger')
	sim(vr, poses={'head': {'position': [9, 9, 9]}})
	vr.stop()
	assert vr.trk.head.getPosition() == [0, 1.6, 0]
	vr._State.quit_requested = False


def test_vr_restores_pre_existing_instance_attributes(vr):
	original = lambda *a, **k: [7, 7, 7]
	vr.trk.head.getPosition = original
	sim(vr, poses={'head': {'position': [1, 1, 1]}})
	assert vr.trk.head.getPosition() == [1, 1, 1]
	sim(vr, reset=True)
	assert vr.trk.head.getPosition is original


def test_vr_quat_and_matrix_use_viz_matrix_when_available(vr):
	class Matrix(object):
		def __init__(self):
			self.e = self.p = None

		def setEuler(self, e):
			self.e = list(e)

		def setPosition(self, p):
			self.p = list(p)

		def getQuat(self):
			return ('quat', self.e)
	vr.viz_stub.Matrix = Matrix
	vr.trk.head.getMatrix = lambda: 'orig'
	vr.trk.head.getQuat = lambda: 'orig'
	sim(vr, poses={'head': {'position': [1, 2, 3], 'euler': [10, 0, 0]}})
	m = vr.trk.head.getMatrix()
	assert (m.p, m.e) == ([1, 2, 3], [10, 0, 0]) and vr.trk.head.getQuat() == ('quat', [10, 0, 0])
	sim(vr, reset=True)
	assert vr.trk.head.getMatrix() == 'orig'


# --- validate: safe evaluator -----------------------------------------------------

@pytest.mark.parametrize('src,env,want', [
	('P*L**3/(48*E*I)', {'P': 1000, 'L': 2.0, 'E': 200e9, 'I': 8e-6}, 1000 * 8 / (48 * 200e9 * 8e-6)),
	('0.5*9.81*t**2', {'t': 2.0}, 19.62),
	('-x + 2*sin(pi/2) - abs(-3) + max(1, 4) % 3', {'x': 1.0}, -1 + 2 - 3 + 1),
	('sqrt(x*x + y*y)', {'x': 3.0, 'y': 4.0}, 5.0),
	('atan2(1, 1) * 4', {}, math.pi),
	('x // 2 + e - e', {'x': 7.0}, 3.0),
])
def test_safe_compile_evaluates(vm, src, env, want):
	fn = vm._safe_compile(src, set(env))
	assert fn(env) == pytest.approx(want)


@pytest.mark.parametrize('src', [
	"__import__('os').system('echo hi')", 'x.__class__', "open('f')", '(lambda: 1)()', '[1, 2]', 'x[0]',
	"'abc'", 'True', 'x if x else 1', 'x < 2', '[i for i in x]', 'print(x)', 'sin(x=1)', 'sin(*x)',
	'sin', 'f(x)', 'x; y', 'x = 1', 'eval("1")', 'None', '1j', 'abs.__call__(1)',
])
def test_safe_compile_rejects(vm, src):
	with pytest.raises(ValueError):
		vm._safe_compile(src, {'x', 'y'})


def test_safe_compile_unknown_name_lists_available(vm):
	with pytest.raises(ValueError, match=r"unknown name 'z'.*x"):
		vm._safe_compile('z + 1', {'x'})


def test_safe_compile_numbers_are_floats_so_no_big_ints(vm):
	fn = vm._safe_compile('9**9**9', set())
	with pytest.raises(OverflowError):
		fn({})
	with pytest.raises(ValueError, match='not a real number'):
		vm._safe_compile('(-8) ** 0.5', set())({})
	with pytest.raises(ZeroDivisionError):
		vm._safe_compile('1/x', {'x'})({'x': 0.0})


# --- validate: commands -------------------------------------------------------------

def deflection_watch(vm, name='beam', noise=0.0, n=11):
	"""t = 0..1 s, load P = 1000 * t N, midspan deflection with an optional constant offset."""
	vm._State.watches.clear()
	w = vm._Watch(name, {'P': 'p', 'defl': 'd', 'pos': 'q'}, 1, 1000, None)
	w.columns = ['P', 'defl', 'pos.x', 'pos.y', 'pos.z']
	k = 2.0 ** 3 / (48 * 200e9 * 8e-6)                       # L**3/(48 E I)
	for i in range(n):
		t = i / (n - 1)
		P = 1000.0 * t
		w.rows.append([t, i, P, P * k + noise, 0.0, 2.0 * t, 0.0])
	vm._State.watches[name] = w
	return w


CONSTS = {'L': 2.0, 'E': 200e9, 'I': 8e-6}


def test_validate_perfect_match(vm, tmp_path):
	deflection_watch(vm)
	res = vm.cmd_validate({'watch': 'beam', 'measured': 'defl', 'theory': 'P*L**3/(48*E*I)',
	                       'constants': CONSTS, 'abs_tol': 1e-9, 'csv_path': str(tmp_path / 'v.csv')})
	assert res['samples'] == 11 and res['skipped'] == 0
	assert res['max_abs_error'] == pytest.approx(0, abs=1e-15) and res['rms_error'] == pytest.approx(0, abs=1e-15)
	assert res['passed'] is True and res['passing_samples'] == 11 and res['failing_samples'] == 0
	assert res['pct_undefined_samples'] == 1                  # theory is 0 at t = 0
	assert res['max_pct_error'] == pytest.approx(0, abs=1e-9)
	assert len(res['series']) == 11 and set(map(len, res['series'])) == {3}
	with open(res['csv']) as f:
		lines = f.read().splitlines()
	assert lines[0] == 't,measured,theory,error,pass' and len(lines) == 12


def test_validate_error_statistics_and_tolerances(vm):
	deflection_watch(vm, noise=1e-5)                          # constant 0.01 mm too big
	base = {'watch': 'beam', 'measured': 'defl', 'theory': 'P*L**3/(48*E*I)', 'constants': CONSTS}
	res = vm.cmd_validate(dict(base))
	assert 'passed' not in res                                # no tolerance, no verdict
	assert res['max_abs_error'] == pytest.approx(1e-5) and res['mean_abs_error'] == pytest.approx(1e-5)
	assert res['rms_error'] == pytest.approx(1e-5) and res['bias'] == pytest.approx(1e-5)
	peak = 1000.0 * 8 / (48 * 200e9 * 8e-6)
	assert res['max_abs_error_pct_of_peak_theory'] == pytest.approx(100 * 1e-5 / peak)
	assert res['max_pct_error'] > res['mean_pct_error'] > 0

	tight = vm.cmd_validate(dict(base, abs_tol=1e-6))
	assert tight['passed'] is False and tight['passing_samples'] == 0 and tight['first_failure']['t'] == 0.0
	loose = vm.cmd_validate(dict(base, abs_tol=2e-5))
	assert loose['passed'] is True and loose['pass_pct'] == 100
	# percent band: error is 1e-5 / (1.04e-4 * t) = 9.6 % / t, so 20 % holds from t = 0.5 on
	pct = vm.cmd_validate(dict(base, pct_tol=20))
	assert pct['passing_samples'] == 6 and pct['failing_samples'] == 5 and pct['passed'] is False
	assert vm.cmd_validate(dict(base, pct_tol=20, min_pass_pct=50))['passed'] is True
	# both: the wider band per sample wins (20 % early, 2e-5 absolute where the theory is tiny)
	assert vm.cmd_validate(dict(base, pct_tol=20, abs_tol=2e-5))['passed'] is True


def test_validate_window_reference_column_and_derived_names(vm):
	deflection_watch(vm)
	res = vm.cmd_validate({'watch': 'beam', 'measured': 'pos.y', 'theory': '2*t', 't_min': 0.25, 't_max': 0.75,
	                       'abs_tol': 1e-12})
	assert res['samples'] == 5 and res['passed'] is True       # t = 0.3 .. 0.7
	res = vm.cmd_validate({'watch': 'beam', 'measured': 'pos_y', 'theory': 'pos_y * 1.1', 'pct_tol': 10})
	assert res['passed'] is True and res['max_pct_error'] == pytest.approx(100 / 11.0)
	res = vm.cmd_validate({'watch': 'beam', 'measured': 'defl', 'theory': 'defl'})
	assert res['max_abs_error'] == 0


def test_validate_skips_undefined_theory_samples(vm):
	deflection_watch(vm)
	res = vm.cmd_validate({'watch': 'beam', 'measured': 'defl', 'theory': 'defl * t / t'})    # 0/0 at t = 0
	assert res['samples'] == 10 and res['skipped'] == 1
	with pytest.raises(ValueError, match='no usable samples.*ZeroDivisionError'):
		vm.cmd_validate({'watch': 'beam', 'measured': 'defl', 'theory': '1/0'})
	with pytest.raises(ValueError, match='no usable samples'):
		vm.cmd_validate({'watch': 'beam', 'measured': 'defl', 'theory': 'P', 't_min': 5})


def test_validate_argument_errors(vm):
	deflection_watch(vm)
	ok = {'watch': 'beam', 'measured': 'defl', 'theory': 'P'}
	with pytest.raises(ValueError, match='no watch named'):
		vm.cmd_validate(dict(ok, watch='x'))
	with pytest.raises(ValueError, match='unknown measured column'):
		vm.cmd_validate(dict(ok, measured='nope'))
	with pytest.raises(ValueError, match='theory is required'):
		vm.cmd_validate(dict(ok, theory=''))
	with pytest.raises(ValueError, match='unknown name'):
		vm.cmd_validate(dict(ok, theory='P * K'))
	with pytest.raises(ValueError, match='constant name'):
		vm.cmd_validate(dict(ok, constants={'P': 1}))           # shadows a column
	with pytest.raises(ValueError, match='constant name'):
		vm.cmd_validate(dict(ok, constants={'sin': 1}))
	with pytest.raises(ValueError, match='abs_tol must be'):
		vm.cmd_validate(dict(ok, abs_tol=-1))
	with pytest.raises(ValueError):
		vm.cmd_validate(dict(ok, theory="__import__('os')"))
	vm._State.watches['empty'] = vm._Watch('empty', {'a': '1'}, 1, 10, None)
	with pytest.raises(ValueError, match='no samples'):
		vm.cmd_validate(dict(ok, watch='empty'))


def test_jsonable_keeps_small_values(vm):
	assert vm._jsonable(1.2345678e-7) == pytest.approx(1.2345678e-7, rel=1e-8)
	assert vm._jsonable(0.1 + 0.2) == 0.3


# --- server tools -----------------------------------------------------------------

def call(name, args):
	return run(S.mcp.call_tool(name, args))


def test_server_gui_tools_forward_params(app):
	app['handler'] = lambda req: {'ok': True, 'result': {'widgets': [], 'cmd': req['cmd']}}
	call('gui_list', {'visible_only': True, 'kinds': ['button']})
	assert sent(app, 'gui_list')[0]['params'] == {'visible_only': True, 'kinds': ['button']}
	call('gui_list', {})
	assert sent(app, 'gui_list')[1]['params'] == {}
	call('click_button', {'label': 'Reset'})
	p = sent(app, 'click_button')[0]['params']
	assert p['label'] == 'Reset' and p['button'] == 'left' and p['double'] is False and 'id' not in p
	call('click_button', {'id': 12})
	assert sent(app, 'click_button')[1]['params']['id'] == 12
	with pytest.raises(ToolError, match='label or id'):
		call('click_button', {})


def test_server_click_button_error_reaches_client(app):
	app['handler'] = lambda req: {'ok': False, 'error': "ValueError: 'x' is ambiguous, pass id"}
	with pytest.raises(ToolError, match='ambiguous'):
		call('click_button', {'label': 'x'})


def test_server_vr_simulate(app):
	app['handler'] = lambda req: {'ok': True, 'result': {'active': True}}
	call('vr_simulate', {'poses': {'head': {'position': [0, 1.7, 0], 'euler': [0, 0, 0]}},
	                     'press': ['trigger'], 'frames': 5, 'button_map': {'trigger': 0}})
	p = sent(app, 'vr_simulate')[0]['params']
	assert p['poses']['head']['position'] == [0, 1.7, 0] and p['press'] == ['trigger'] and p['frames'] == 5
	assert p['send_events'] is True and p['apply_to_node'] is True and 'reset' not in p and 'release' not in p
	call('vr_simulate', {'reset': True})
	assert sent(app, 'vr_simulate')[1]['params']['reset'] is True
	for bad in ({'frames': -1}, {'poses': {'head': {'pos': [0, 0, 0]}}}, {'poses': {'head': {'position': [0, 0]}}}):
		with pytest.raises(ToolError):
			call('vr_simulate', bad)


def test_server_vr_simulate_vizconnect_missing_error(app):
	app['handler'] = lambda req: {'ok': False, 'error': 'ValueError: vizconnect is not in use by this app'}
	with pytest.raises(ToolError, match='vizconnect is not in use'):
		call('vr_simulate', {'poses': {'head': {'position': [0, 1, 0]}}})


def validate_result(n=20, **extra):
	series = [[i / 10.0, 0.5 * (i / 10.0) ** 2 * 1.02, 0.5 * (i / 10.0) ** 2] for i in range(n)]
	return dict({'watch': 'w', 'samples': n, 'max_abs_error': 0.01, 'passed': True, 'series': series}, **extra)


def test_server_validate_returns_chart_and_stats_without_series(app):
	app['handler'] = lambda req: {'ok': True, 'result': validate_result()}
	res = call('validate', {'watch': 'w', 'measured': 'h', 'theory': '0.5*9.81*t**2',
	                        'constants': {'g': 9.81}, 'pct_tol': 3, 't_max': 1.9})
	assert [c.type for c in res.content] == ['image', 'text']
	out = json.loads(res.content[1].text)
	assert out['passed'] is True and 'series' not in out and out['samples'] == 20
	p = sent(app, 'validate')[0]['params']
	assert p['watch'] == 'w' and p['theory'] == '0.5*9.81*t**2' and p['constants'] == {'g': 9.81}
	assert p['pct_tol'] == 3 and p['t_max'] == 1.9 and p['min_pass_pct'] == 100.0
	assert 'abs_tol' not in p and 't_min' not in p
	import base64
	import io
	im = Image.open(io.BytesIO(base64.b64decode(res.content[0].data)))
	assert im.size[0] > 500


def test_server_validate_single_sample_still_charts(app):
	app['handler'] = lambda req: {'ok': True, 'result': validate_result(n=1)}
	res = call('validate', {'watch': 'w', 'measured': 'h', 'theory': 't'})
	assert res.content[0].type == 'image'


def test_server_validate_argument_checks_and_errors(app):
	for bad in ({'abs_tol': -1}, {'pct_tol': -1}, {'min_pass_pct': 101}, {'theory': ''}):
		with pytest.raises(ToolError):
			call('validate', dict({'watch': 'w', 'measured': 'h', 'theory': 't'}, **bad))
	assert not sent(app, 'validate')
	app['handler'] = lambda req: {'ok': False, 'error': "ValueError: unknown name 'K'"}
	with pytest.raises(ToolError, match='unknown name'):
		call('validate', {'watch': 'w', 'measured': 'h', 'theory': 'K'})
