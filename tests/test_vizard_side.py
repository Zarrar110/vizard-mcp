"""vizmcp's new commands, run against a stand-in for Vizard's `viz` module.

Only the logic that does not need a renderer is covered here: the file writers behind
watch_export, scene_stats on fake nodes, and click coordinate handling. Real-Vizard checks
for these are in live_test.py.
"""

import csv
import importlib.util
import json
import os
import sys
import types

import pytest

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'src', 'vizard_mcp', 'vizard_side', 'vizmcp.py')


class FakeNode(object):
	_next = [100]

	def __init__(self, visible=True, subnodes=1, **extra):
		FakeNode._next[0] += 1
		self.id = FakeNode._next[0]
		self._visible, self._subnodes = visible, subnodes
		for k, v in extra.items():
			setattr(self, k, v)

	def valid(self):
		return True

	def getVisible(self):
		return self._visible

	def getNodeCount(self):
		return self._subnodes


class FakeLight(FakeNode):
	pass


@pytest.fixture
def vm(monkeypatch):
	viz = types.ModuleType('viz')
	viz.PRIORITY_LAST_UPDATE = 10
	viz.VizNode = FakeNode
	viz.frame = 0

	def frame_number():
		return viz.frame
	viz.getFrameNumber = frame_number
	viz.getFrameTime = lambda: viz.frame / 60.0
	viz.nodes = []
	viz.getNodeList = lambda: list(viz.nodes)
	monkeypatch.setitem(sys.modules, 'viz', viz)
	monkeypatch.setitem(sys.modules, 'vizact', types.ModuleType('vizact'))
	spec = importlib.util.spec_from_file_location('vizmcp_under_test', SRC)
	mod = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(mod)
	mod._State.watches.clear()
	mod.viz_stub = viz
	return mod


def make_watch(vm, name='w', n=4):
	vm._State.watches.clear()
	w = vm._Watch(name, {'a': '1 + 1', 'pos': '(1.0, 2.0, 3.0)'}, 1, 1000, None)
	for _ in range(n):
		w.sample()
		vm.viz_stub.frame += 1
	vm._State.watches[name] = w
	return w


# --- watch_export -----------------------------------------------------------

def test_export_csv_all_columns(vm, tmp_path):
	make_watch(vm)
	res = vm.cmd_watch_export({'name': 'w', 'path': str(tmp_path / 'out' / 'w.csv')})
	assert res['format'] == 'csv' and res['rows'] == 4
	assert res['columns'] == ['t', 'frame', 'a', 'pos.x', 'pos.y', 'pos.z']
	with open(res['path']) as f:
		rows = list(csv.reader(f))
	assert rows[0] == res['columns'] and len(rows) == 5
	assert [float(v) for v in rows[1]][2:] == [2.0, 1.0, 2.0, 3.0]
	assert [int(r[1]) for r in rows[1:]] == [0, 1, 2, 3]


def test_export_column_subset_and_order(vm, tmp_path):
	make_watch(vm)
	res = vm.cmd_watch_export({'name': 'w', 'path': str(tmp_path / 'w.csv'), 'columns': ['pos.z', 't']})
	with open(res['path']) as f:
		rows = list(csv.reader(f))
	assert rows[0] == ['pos.z', 't'] and rows[1][0] == '3.0'


def test_export_json_with_metadata(vm, tmp_path):
	make_watch(vm)
	res = vm.cmd_watch_export({'name': 'w', 'path': str(tmp_path / 'w.json'), 'columns': ['t', 'a']})
	with open(res['path']) as f:
		doc = json.load(f)
	assert res['format'] == 'json'
	assert doc['watch'] == 'w' and doc['columns'] == ['t', 'a'] and doc['samples'] == 4
	assert doc['expressions']['a'] == '1 + 1' and doc['rows'][0] == [0.0, 2.0]


def test_export_format_argument_overrides_extension(vm, tmp_path):
	make_watch(vm)
	res = vm.cmd_watch_export({'name': 'w', 'path': str(tmp_path / 'w.dat'), 'format': 'json'})
	assert res['format'] == 'json'
	with open(res['path']) as f:
		json.load(f)


def test_export_errors(vm, tmp_path):
	make_watch(vm)
	with pytest.raises(ValueError, match='no watch named'):
		vm.cmd_watch_export({'name': 'x', 'path': str(tmp_path / 'a.csv')})
	with pytest.raises(ValueError, match='unknown columns'):
		vm.cmd_watch_export({'name': 'w', 'path': str(tmp_path / 'a.csv'), 'columns': ['nope']})
	with pytest.raises(ValueError, match='format must be'):
		vm.cmd_watch_export({'name': 'w', 'path': str(tmp_path / 'a.xlsx')})
	with pytest.raises(ValueError, match='path is required'):
		vm.cmd_watch_export({'name': 'w'})
	vm._State.watches['empty'] = vm._Watch('empty', {'a': '1'}, 1, 10, None)
	with pytest.raises(ValueError, match='no samples'):
		vm.cmd_watch_export({'name': 'empty', 'path': str(tmp_path / 'a.csv')})


def test_new_commands_are_registered(vm):
	assert {'watch_export', 'scene_stats', 'click'} <= set(vm.COMMANDS)


# --- scene_stats ------------------------------------------------------------

def test_scene_stats_with_triangle_counts(vm):
	vm.viz_stub.nodes = [
		FakeNode(getTriangleCount=lambda: 10), FakeNode(getTriangleCount=lambda: 5000),
		FakeNode(visible=False, getTriangleCount=lambda: 300), FakeLight(),
	]
	res = vm.cmd_scene_stats({'top': 2})
	assert res['nodes'] == 4 and res['visible_nodes'] == 3
	assert res['triangles'] == 5310 and res['triangles_visible'] == 5010
	assert res['ranked_by'] == 'triangles'
	assert [n['triangles'] for n in res['top_nodes']] == [5000, 300]
	assert res['top_nodes'][1]['visible'] is False
	assert res['lights'] == 1                                  # by node type: no list accessor here
	assert res['by_type'] == {'FakeNode': 3, 'FakeLight': 1}
	json.dumps(res)


def test_scene_stats_reports_unavailable_figures(vm):
	vm.viz_stub.nodes = [FakeNode(subnodes=3), FakeNode(subnodes=40), FakeNode(subnodes=7)]
	res = vm.cmd_scene_stats({})
	assert res['triangles'] is None and res['triangles_visible'] is None
	assert res['textures'] is None and res['lights'] is None
	assert res['ranked_by'] == 'subnodes'
	assert [n['subnodes'] for n in res['top_nodes']] == [40, 7, 3]
	assert any('triangle' in n for n in res['notes'])


def test_scene_stats_textures_and_lights_from_lists(vm):
	vm.viz_stub.getTextureList = lambda: ['a', 'b', 'c']
	vm.viz_stub.getLightList = lambda: ['L0', 'L1']
	vm.viz_stub.nodes = [FakeNode()]
	res = vm.cmd_scene_stats({'top': 0})
	assert res['textures'] == 3 and res['lights'] == 2 and res['top_nodes'] == []


def test_scene_stats_counts_distinct_node_textures(vm):
	tex = types.SimpleNamespace(id=7)
	vm.viz_stub.nodes = [FakeNode(getTexture=lambda: tex), FakeNode(getTexture=lambda: tex),
	                     FakeNode(getTexture=lambda: types.SimpleNamespace(id=8))]
	assert vm.cmd_scene_stats({})['textures'] == 2


def test_scene_stats_survives_failing_accessors(vm):
	def boom():
		raise RuntimeError('no')
	vm.viz_stub.nodes = [FakeNode(getTriangleCount=boom, getTexture=boom)]
	res = vm.cmd_scene_stats({})
	assert res['nodes'] == 1 and res['triangles'] is None


# --- click coordinates ------------------------------------------------------

def test_client_pixel_maps_corners_inside_the_client_area(vm):
	assert vm._client_pixel(0, 0, 800, 600) == (0, 0)
	assert vm._client_pixel(1, 1, 800, 600) == (799, 599)
	assert vm._client_pixel(0.5, 0.5, 801, 601) == (400, 300)


def test_client_pixel_rejects_out_of_range_and_empty_window(vm):
	with pytest.raises(ValueError, match='0..1'):
		vm._client_pixel(1.2, 0.5, 800, 600)
	with pytest.raises(ValueError, match='0..1'):
		vm._client_pixel(0.5, -0.1, 800, 600)
	with pytest.raises(RuntimeError, match='no client area'):
		vm._client_pixel(0.5, 0.5, 0, 0)


def test_click_rejects_bad_input_before_touching_windows(vm):
	gen = vm.cmd_click({'x': 3, 'y': 0.5})
	if sys.platform == 'win32':
		with pytest.raises(ValueError, match='0..1'):
			next(gen)
	else:
		with pytest.raises(Exception):       # no ctypes.windll off Windows
			next(gen)
