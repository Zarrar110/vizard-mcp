"""gui_list / click_button: vizmcp commands against a stand-in for Vizard's `viz` module, and the
server tools against a fake vizmcp.

What this cannot show is how real Vizard 8 widgets behave; the
stand-ins follow the calls the code makes. See README, "What still needs a live Vizard run".
"""

import asyncio
import inspect
import json
import types

import pytest

from mcp.server.mcpserver.exceptions import ToolError
from test_new_tools import app, sent          # noqa: F401  (fixture)
from test_vizard_side import FakeNode, vm      # noqa: F401  (fixture)
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
	assert {'gui_list', 'click_button'} <= set(vm.COMMANDS)


# --- vr_simulate ----------------------------------------------------------------































# --- validate: safe evaluator -----------------------------------------------------









# --- validate: commands -------------------------------------------------------------



CONSTS = {'L': 2.0, 'E': 200e9, 'I': 8e-6}












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
























