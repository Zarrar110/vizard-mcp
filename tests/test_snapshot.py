"""snapshot_save / snapshot_restore / snapshot_list: the commands in the app (against stand-in nodes
and a stand-in __main__ namespace) and the server tools (against a fake vizmcp)."""

import asyncio
import json
import os

import pytest

from mcp.server.mcpserver.exceptions import ToolError
from test_new_tools import app, sent          # noqa: F401  (fixture)
from test_vizard_side import FakeNode, vm     # noqa: F401  (fixture)
from vizard_mcp import server as S


def run(coro):
	return asyncio.run(coro)


def tool(name, args):
	return json.loads(run(S.mcp.call_tool(name, args)).content[0].text)


class Node(FakeNode):
	"""A node with a settable local transform, visibility and children."""

	def __init__(self, pos=(0, 0, 0), quat=(0, 0, 0, 1), scale=(1, 1, 1), visible=True, children=()):
		FakeNode.__init__(self, visible=visible)
		self.pos, self.quat, self.scale, self.kids, self.refuse = list(pos), list(quat), list(scale), list(children), set()

	def getPosition(self, mode=None):
		return list(self.pos)

	def getQuat(self, mode=None):
		return list(self.quat)

	def getScale(self, mode=None):
		return list(self.scale)

	def setPosition(self, v, mode=None):
		if 'position' in self.refuse:
			raise RuntimeError('position is driven by the app')
		self.pos = list(v)

	def setQuat(self, v, mode=None):
		self.quat = list(v)

	def setScale(self, v, mode=None):
		self.scale = list(v)

	def visible(self, flag):
		self._visible = flag

	def getChildren(self):
		return list(self.kids)


class Sim(object):
	def __init__(self):
		self.t, self.items = 1.5, [1, 2]

	def state(self):
		return {'t': self.t, 'items': self.items}

	def set_state(self, s):
		self.t, self.items = s['t'], s['items']


@pytest.fixture
def scene(vm, monkeypatch):
	"""vizmcp with: ball, crane (with hook and arm), a hidden lamp, a Sim, and the script names for them."""
	viz = vm.viz_stub
	viz.ABS_PARENT = 0
	viz.version = lambda: '8.3.1'
	arm = Node(pos=(0, 3, 0), quat=(0, 0.7071, 0, 0.7071))
	hook = Node(pos=(1, 2, 3), scale=(1, 2, 1))
	crane = Node(pos=(5, 0, 0), children=[arm, hook])
	ball = Node(pos=(2.5, 1, 0))
	lamp = Node(pos=(0, 9, 0), visible=False)
	viz.nodes = [ball, crane, arm, hook, lamp]
	viz.VizNode._GetObject = staticmethod(lambda nid: next((n for n in viz.nodes if n.id == nid), None))
	ns = {'ball': ball, 'crane': crane, 'LAB': type('Lab', (), {'sim': Sim(), 'hook': hook})(), 'lamp': lamp}
	monkeypatch.setattr(vm, '_main_ns', lambda: ns)
	names = {ball.id: ['ball'], crane.id: ['crane'], hook.id: ['LAB.hook'], lamp.id: ['lamp']}
	monkeypatch.setattr(vm, '_labels', lambda *a, **k: names)
	vm._State.snapshots.clear()
	return types_ns(vm=vm, ns=ns, ball=ball, crane=crane, arm=arm, hook=hook, lamp=lamp, viz=viz, names=names)


def types_ns(**kw):
	import types
	return types.SimpleNamespace(**kw)


def disturb(sc):
	sc.ball.pos, sc.crane.pos, sc.hook.scale = [9, 9, 9], [0, 0, 0], [3, 3, 3]
	sc.arm.quat, sc.lamp._visible, sc.ball._visible = [1, 0, 0, 0], True, False


# --- save -----------------------------------------------------------------------------

def test_save_by_expression_and_id_records_transform_quat_scale_and_visibility(scene):
	res = scene.vm.cmd_snapshot_save({'name': 'a', 'nodes': ['ball', scene.lamp.id]})
	assert res['name'] == 'a' and res['nodes'] == 2 and res['truncated'] is False and res['file'] is None
	snap = scene.vm._State.snapshots['a']
	ball, lamp = snap['nodes']
	assert ball['ref'] == 'ball' and ball['kind'] == 'expr' and ball['id'] == scene.ball.id
	assert ball['position'] == [2.5, 1, 0] and ball['quat'] == [0, 0, 0, 1] and ball['scale'] == [1, 1, 1] and ball['visible'] is True
	assert lamp['ref'] == scene.lamp.id and lamp['kind'] == 'id' and lamp['visible'] is False and lamp['names'] == ['lamp']
	json.dumps(res)


def test_save_named_uses_find_nodes_and_pattern(scene):
	res = scene.vm.cmd_snapshot_save({'name': 'n', 'named': True})
	assert res['nodes'] == 4                                    # ball, crane, LAB.hook, lamp
	refs = sorted(e['ref'] for e in scene.vm._State.snapshots['n']['nodes'])
	assert refs == ['LAB.hook', 'ball', 'crane', 'lamp']
	res = scene.vm.cmd_snapshot_save({'name': 'n2', 'named': True, 'pattern': 'LAB'})
	assert [e['ref'] for e in scene.vm._State.snapshots['n2']['nodes']] == ['LAB.hook']


def test_save_root_takes_the_node_and_every_descendant_once(scene):
	deep = Node()
	scene.arm.kids.append(deep)
	scene.viz.nodes.append(deep)
	scene.crane.kids.append(deep)                               # reachable twice
	res = scene.vm.cmd_snapshot_save({'name': 'r', 'root': 'crane'})
	snap = scene.vm._State.snapshots['r']
	assert res['nodes'] == 4 and len({e['id'] for e in snap['nodes']}) == 4
	assert snap['nodes'][0]['ref'] == 'crane'                   # a node with a name is found by name
	arm = next(e for e in snap['nodes'] if e['id'] == scene.arm.id)
	assert arm['kind'] == 'id' and arm['quat'] == [0, 0.7071, 0, 0.7071]


def test_save_combines_selectors_without_duplicates_and_respects_max_nodes(scene):
	res = scene.vm.cmd_snapshot_save({'name': 'c', 'nodes': ['ball'], 'named': True, 'root': 'crane'})
	assert res['nodes'] == 5 and len({e['id'] for e in scene.vm._State.snapshots['c']['nodes']}) == 5
	res = scene.vm.cmd_snapshot_save({'name': 'small', 'named': True, 'root': 'crane', 'max_nodes': 3})
	assert res['nodes'] == 3 and res['truncated'] is True


def test_save_replaces_a_snapshot_of_the_same_name(scene):
	scene.vm.cmd_snapshot_save({'name': 'a', 'nodes': ['ball']})
	scene.vm.cmd_snapshot_save({'name': 'a', 'nodes': ['ball', 'crane']})
	assert len(scene.vm._State.snapshots) == 1 and len(scene.vm._State.snapshots['a']['nodes']) == 2


def test_nodes_that_cannot_report_a_value_record_none_and_restore_skips_it(scene):
	scene.ball.getQuat = lambda mode=None: (_ for _ in ()).throw(RuntimeError('no quat'))
	scene.vm.cmd_snapshot_save({'name': 'a', 'nodes': ['ball']})
	assert scene.vm._State.snapshots['a']['nodes'][0]['quat'] is None
	disturb(scene)
	res = scene.vm.cmd_snapshot_restore({'name': 'a'})
	assert res['restored'] == 1 and not res['failed'] and scene.ball.pos == [2.5, 1, 0]


@pytest.mark.parametrize('params, msg', [
	({}, 'name is required'),
	({'name': 'x'}, 'nothing to record'),
	({'name': 'x', 'restore_code': 'pass', 'nodes': ['ball']}, 'restore_code needs state'),
	({'name': 'x', 'nodes': ['nope']}, "NameError|name 'nope'"),
	({'name': 'x', 'nodes': ['LAB']}, 'not a Vizard node'),
	({'name': 'x', 'root': 'nope'}, 'nope')])
def test_save_errors_leave_nothing_behind(scene, params, msg):
	with pytest.raises(Exception, match=msg):
		scene.vm.cmd_snapshot_save(params)
	assert not scene.vm._State.snapshots


# --- restore --------------------------------------------------------------------------

def test_round_trip_restores_every_recorded_value(scene):
	scene.vm.cmd_snapshot_save({'name': 'a', 'named': True, 'root': 'crane'})
	disturb(scene)
	res = scene.vm.cmd_snapshot_restore({'name': 'a'})
	assert res['restored'] == res['nodes_saved'] == 5 and res['missing'] == [] and res['failed'] == []
	assert scene.ball.pos == [2.5, 1, 0] and scene.crane.pos == [5, 0, 0] and scene.hook.scale == [1, 2, 1]
	assert scene.arm.quat == [0, 0.7071, 0, 0.7071]
	assert scene.lamp.getVisible() is False and scene.ball.getVisible() is True
	json.dumps(res)


def test_restore_can_be_repeated(scene):
	scene.vm.cmd_snapshot_save({'name': 'a', 'nodes': ['ball']})
	for _ in range(2):
		scene.ball.pos = [0, 0, 0]
		assert scene.vm.cmd_snapshot_restore({'name': 'a'})['restored'] == 1
		assert scene.ball.pos == [2.5, 1, 0]


def test_restore_reports_missing_nodes_and_still_restores_the_rest(scene):
	scene.vm.cmd_snapshot_save({'name': 'a', 'nodes': ['ball', 'crane', scene.arm.id]})
	disturb(scene)
	scene.viz.nodes.remove(scene.arm)                           # the arm was deleted from the scene
	scene.viz.nodes.remove(scene.crane)                         # the crane too, and the script no longer has "crane"
	del scene.ns['crane']
	res = scene.vm.cmd_snapshot_restore({'name': 'a'})
	assert res['restored'] == 1 and scene.ball.pos == [2.5, 1, 0]
	by_ref = {m['ref']: m for m in res['missing']}
	assert set(by_ref) == {'crane', scene.arm.id}
	assert 'NameError' in by_ref['crane']['reason'] and 'not a Vizard node' in by_ref[scene.arm.id]['reason']


def test_an_expression_that_no_longer_resolves_falls_back_to_the_id_in_the_same_app(scene):
	scene.vm.cmd_snapshot_save({'name': 'a', 'nodes': ['ball']})
	scene.ns['ball'] = 'rebound to something else'
	scene.ball.pos = [0, 0, 0]
	res = scene.vm.cmd_snapshot_restore({'name': 'a'})
	assert res['restored'] == 1 and res['missing'] == [] and scene.ball.pos == [2.5, 1, 0]


def test_a_node_a_setter_refuses_is_listed_as_failed_but_the_others_still_apply(scene):
	scene.vm.cmd_snapshot_save({'name': 'a', 'nodes': ['ball']})
	scene.ball.pos, scene.ball.scale = [0, 0, 0], [4, 4, 4]
	scene.ball.refuse.add('position')
	res = scene.vm.cmd_snapshot_restore({'name': 'a'})
	assert res['restored'] == 0 and len(res['failed']) == 1
	assert res['failed'][0]['what'] == 'position' and 'driven by the app' in res['failed'][0]['error']
	assert scene.ball.scale == [1, 1, 1]


def test_restore_unknown_name_and_missing_arguments(scene):
	with pytest.raises(ValueError, match="no snapshot named 'zzz' \\(have: none\\)"):
		scene.vm.cmd_snapshot_restore({'name': 'zzz'})
	scene.vm.cmd_snapshot_save({'name': 'a', 'nodes': ['ball']})
	with pytest.raises(ValueError, match='have: a'):
		scene.vm.cmd_snapshot_restore({'name': 'zzz'})
	with pytest.raises(ValueError, match='give name, or path'):
		scene.vm.cmd_snapshot_restore({})


# --- app state ------------------------------------------------------------------------

def test_state_round_trip_through_getter_and_setter(scene):
	sim = scene.ns['LAB'].sim
	scene.vm.cmd_snapshot_save({'name': 's', 'nodes': ['ball'], 'state': 'LAB.sim.state()',
	                            'restore_code': 'LAB.sim.set_state(_state)'})
	sim.t, sim.items = 99, ['x']
	res = scene.vm.cmd_snapshot_restore({'name': 's'})
	assert res['state'] == {'restored': True} and sim.t == 1.5 and sim.items == [1, 2]
	assert '_state' not in scene.ns                              # no leftover name in the script's namespace


def test_state_is_a_copy_not_a_reference(scene):
	sim = scene.ns['LAB'].sim
	scene.vm.cmd_snapshot_save({'name': 's', 'state': 'LAB.sim.state()', 'restore_code': 'LAB.sim.set_state(_state)'})
	sim.items.append(3)                                          # mutates the object the getter returned
	scene.vm.cmd_snapshot_restore({'name': 's'})
	assert sim.items == [1, 2]
	sim.items.append(4)                                          # the setter handed our copy to the sim ...
	scene.vm.cmd_snapshot_restore({'name': 's'})                 # ... but the stored one is untouched
	assert sim.items == [1, 2]


def test_state_is_restored_before_the_nodes(scene):
	order = []
	scene.ns['note'] = lambda what: order.append(what)
	scene.vm.cmd_snapshot_save({'name': 's', 'nodes': ['ball'], 'state': '1', 'restore_code': 'note("state")'})
	orig = scene.ball.setPosition
	scene.ball.setPosition = lambda v, mode=None: (order.append('node'), orig(v, mode))[1]
	scene.vm.cmd_snapshot_restore({'name': 's'})
	assert order == ['state', 'node']


def test_restore_code_given_at_restore_time_wins_and_state_without_code_is_reported(scene):
	sim = scene.ns['LAB'].sim
	scene.vm.cmd_snapshot_save({'name': 's', 'nodes': ['ball'], 'state': 'LAB.sim.state()'})
	sim.t = 7
	res = scene.vm.cmd_snapshot_restore({'name': 's'})
	assert res['state']['restored'] is False and 'no restore_code' in res['state']['reason'] and sim.t == 7
	res = scene.vm.cmd_snapshot_restore({'name': 's', 'restore_code': 'LAB.sim.t = _state["t"]'})
	assert res['state'] == {'restored': True} and sim.t == 1.5


def test_a_failing_restore_code_is_reported_and_the_nodes_are_still_restored(scene):
	scene.vm.cmd_snapshot_save({'name': 's', 'nodes': ['ball'], 'state': '5', 'restore_code': '1 / 0'})
	scene.ball.pos = [0, 0, 0]
	res = scene.vm.cmd_snapshot_restore({'name': 's'})
	assert res['state']['restored'] is False and 'ZeroDivisionError' in res['state']['error']
	assert res['restored'] == 1 and scene.ball.pos == [2.5, 1, 0] and '_state' not in scene.ns


def test_uncopyable_state_is_refused_when_saving(scene):
	import threading
	scene.ns['lock'] = threading.Lock()
	with pytest.raises(ValueError, match='cannot be copied'):
		scene.vm.cmd_snapshot_save({'name': 's', 'state': 'lock'})
	assert not scene.vm._State.snapshots


# --- files ----------------------------------------------------------------------------

def test_file_round_trip_in_the_same_app(scene, tmp_path):
	sim = scene.ns['LAB'].sim
	f = str(tmp_path / 'sub' / 'snap.json')
	res = scene.vm.cmd_snapshot_save({'name': 'f', 'named': True, 'state': 'LAB.sim.state()',
	                                  'restore_code': 'LAB.sim.set_state(_state)', 'path': f})
	assert res['file'] == f
	doc = json.load(open(f))
	assert doc['format'] == 'vizmcp-snapshot' and doc['version'] == 1 and doc['name'] == 'f'
	assert doc['state'] == {'expression': 'LAB.sim.state()', 'restore_code': 'LAB.sim.set_state(_state)',
	                        'value': {'t': 1.5, 'items': [1, 2]}, 'value_exact': True, 'repr': "{'t': 1.5, 'items': [1, 2]}"}
	assert [n['ref'] for n in doc['nodes']] == ['LAB.hook', 'ball', 'crane', 'lamp']
	scene.vm._State.snapshots.clear()                            # as in a fresh session of the app's memory
	disturb(scene)
	sim.t = 50
	res = scene.vm.cmd_snapshot_restore({'path': f})
	assert res['name'] == 'f' and res['restored'] == 4 and not res['missing'] and res['state'] == {'restored': True}
	assert scene.ball.pos == [2.5, 1, 0] and sim.t == 1.5 and 'f' in scene.vm._State.snapshots


def test_a_snapshot_file_from_another_session_finds_nodes_by_name_but_not_by_id(scene, tmp_path):
	f = str(tmp_path / 'snap.json')
	scene.vm.cmd_snapshot_save({'name': 'f', 'nodes': ['ball', scene.arm.id], 'path': f})
	doc = json.load(open(f))
	doc['pid'] = doc['pid'] + 1                                  # saved by another process
	json.dump(doc, open(f, 'w'))
	scene.vm._State.snapshots.clear()
	scene.ball.pos = [0, 0, 0]
	scene.arm.quat = [1, 0, 0, 0]
	res = scene.vm.cmd_snapshot_restore({'path': f, 'name': 'renamed'})
	assert res['name'] == 'renamed' and res['restored'] == 1 and scene.ball.pos == [2.5, 1, 0]
	assert len(res['missing']) == 1 and 'another Vizard session' in res['missing'][0]['reason'] and scene.arm.quat == [1, 0, 0, 0]
	assert 'renamed' in scene.vm._State.snapshots


def test_state_that_is_not_json_stays_in_memory_and_the_file_says_so(scene, tmp_path):
	scene.ns['odd'] = {1, 2, 3}                                  # a set: copyable, not JSON
	f = str(tmp_path / 'odd.json')
	res = scene.vm.cmd_snapshot_save({'name': 'o', 'state': 'odd', 'restore_code': 'got.append(_state)', 'path': f})
	assert 'not JSON data' in res['note']
	doc = json.load(open(f))
	assert doc['state']['value'] is None and doc['state']['value_exact'] is False and '3' in doc['state']['repr']
	scene.ns['got'] = []
	assert scene.vm.cmd_snapshot_restore({'name': 'o'})['state'] == {'restored': True} and scene.ns['got'] == [{1, 2, 3}]
	scene.vm._State.snapshots.clear()
	res = scene.vm.cmd_snapshot_restore({'path': f})
	assert res['state']['restored'] is False and 'not JSON' in res['state']['reason']


def test_bad_snapshot_files_are_clear_errors(scene, tmp_path):
	bad = tmp_path / 'bad.json'
	bad.write_text(json.dumps({'hello': 1}))
	with pytest.raises(ValueError, match='not a vizmcp snapshot file'):
		scene.vm.cmd_snapshot_restore({'path': str(bad)})
	bad.write_text(json.dumps({'format': 'vizmcp-snapshot', 'version': 99}))
	with pytest.raises(ValueError, match='version 99'):
		scene.vm.cmd_snapshot_restore({'path': str(bad)})
	with pytest.raises(OSError):
		scene.vm.cmd_snapshot_restore({'path': str(tmp_path / 'absent.json')})


# --- list -----------------------------------------------------------------------------

def test_list_summarises_every_snapshot(scene, tmp_path):
	assert scene.vm.cmd_snapshot_list({}) == {'snapshots': []}
	scene.vm.cmd_snapshot_save({'name': 'a', 'nodes': ['ball']})
	scene.vm.cmd_snapshot_save({'name': 'b', 'nodes': ['ball', 'crane'], 'state': '1', 'restore_code': 'pass',
	                            'path': str(tmp_path / 'b.json')})
	rows = {r['name']: r for r in scene.vm.cmd_snapshot_list({})['snapshots']}
	assert rows['a']['nodes'] == 1 and rows['a']['state_expression'] is None and rows['a']['file'] is None
	assert rows['b']['nodes'] == 2 and rows['b']['state_expression'] == '1' and rows['b']['restore_code'] is True
	assert rows['b']['file'] == str(tmp_path / 'b.json') and rows['b']['same_app'] is True
	json.dumps(rows)


def test_snapshot_commands_are_registered(vm):
	assert {'snapshot_save', 'snapshot_restore', 'snapshot_list'} <= set(vm.COMMANDS)


# --- the server tools -----------------------------------------------------------------

def test_save_tool_forwards_everything_and_drops_unset_options(app, tmp_path):
	app['handler'] = lambda req: {'ok': True, 'result': {'name': 'a', 'nodes': 3}}
	tool('snapshot_save', {'name': 'a', 'nodes': ['ball', '12'], 'state': 'LAB.s()', 'restore_code': 'LAB.set(_state)',
	                       'path': str(tmp_path / 's.json'), 'max_nodes': 50})
	assert sent(app, 'snapshot_save')[0]['params'] == {
		'name': 'a', 'nodes': ['ball', '12'], 'max_nodes': 50, 'state': 'LAB.s()', 'restore_code': 'LAB.set(_state)',
		'path': str(tmp_path / 's.json')}
	tool('snapshot_save', {'name': 'b', 'named': True, 'pattern': 'crane', 'root': 'site'})
	assert sent(app, 'snapshot_save')[1]['params'] == {'name': 'b', 'named': True, 'pattern': 'crane', 'root': 'site',
	                                                   'max_nodes': 2000}


@pytest.mark.parametrize('args, msg', [
	({'name': ' '}, 'name is required'),
	({'name': 'a'}, 'nothing to record'),
	({'name': 'a', 'nodes': ['x'], 'restore_code': 'pass'}, 'needs state'),
	({'name': 'a', 'nodes': ['x'], 'max_nodes': 0}, 'max_nodes')])
def test_save_tool_argument_errors_do_not_touch_the_app(app, args, msg):
	with pytest.raises(ToolError, match=msg):
		run(S.mcp.call_tool('snapshot_save', args))
	assert not sent(app, 'snapshot_save')


def test_restore_tool_forwards_and_requires_name_or_path(app, tmp_path):
	app['handler'] = lambda req: {'ok': True, 'result': {'restored': 2, 'missing': [{'ref': 'x', 'reason': 'gone'}]}}
	out = tool('snapshot_restore', {'name': 'a', 'restore_code': 'go(_state)'})
	assert out['missing'][0]['ref'] == 'x'
	assert sent(app, 'snapshot_restore')[0]['params'] == {'name': 'a', 'restore_code': 'go(_state)'}
	tool('snapshot_restore', {'path': str(tmp_path / 's.json')})
	assert sent(app, 'snapshot_restore')[1]['params'] == {'path': str(tmp_path / 's.json')}
	with pytest.raises(ToolError, match='name, or path'):
		run(S.mcp.call_tool('snapshot_restore', {}))
	assert len(sent(app, 'snapshot_restore')) == 2


def test_vizard_errors_reach_the_client(app):
	app['handler'] = lambda req: {'ok': False, 'error': "ValueError: no snapshot named 'q' (have: none)"}
	with pytest.raises(ToolError, match='no snapshot named'):
		run(S.mcp.call_tool('snapshot_restore', {'name': 'q'}))


def test_list_tool_asks_the_app_or_reads_a_file_without_it(app, tmp_path):
	app['handler'] = lambda req: {'ok': True, 'result': {'snapshots': [{'name': 'a'}]}}
	assert tool('snapshot_list', {}) == {'snapshots': [{'name': 'a'}]}
	f = tmp_path / 's.json'
	f.write_text(json.dumps({'format': 'vizmcp-snapshot', 'version': 1, 'name': 'file one', 'created': 'now',
	                         'nodes': [{}, {}], 'state': {'expression': 'x', 'restore_code': 'y', 'value_exact': True}}))
	n = len(app['fake'].requests)
	out = tool('snapshot_list', {'path': str(f)})
	assert out['name'] == 'file one' and out['nodes'] == 2 and out['restore_code'] is True and out['state_in_file'] is True
	assert len(app['fake'].requests) == n                       # the app was not asked
	f.write_text('{"x": 1}')
	with pytest.raises(ToolError, match='not a vizmcp snapshot'):
		run(S.mcp.call_tool('snapshot_list', {'path': str(f)}))
	with pytest.raises(ToolError, match='Cannot read'):
		run(S.mcp.call_tool('snapshot_list', {'path': str(tmp_path / 'absent.json')}))
