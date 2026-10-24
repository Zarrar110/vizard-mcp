"""asset_check: the vizmcp command against stand-in nodes (fake getStats / getBoundingBox / valid), the
server tool against a fake vizmcp, and the report section."""

import asyncio
import json
import os
import types

import pytest

from mcp.server.mcpserver.exceptions import ToolError
from test_new_tools import app, sent          # noqa: F401  (fixture)
from test_vizard_side import FakeLight, FakeNode, vm     # noqa: F401  (fixture)
from vizard_mcp import report, server as S

MIB = 1024 * 1024


def run(coro):
	return asyncio.run(coro)


def tool(name, args=None):
	return json.loads(run(S.mcp.call_tool(name, args or {})).content[0].text)


def stats(prims=0, verts=0, drawables=0, tex=0, tex_bytes=0, nodes=1):
	return lambda: types.SimpleNamespace(primitiveCount=prims, vertexCount=verts, drawableCount=drawables,
	                                     transformCount=0, nodeCount=nodes, textureCount=tex, textureBytes=tex_bytes)


def bbox(size):
	x, y, z = size
	return lambda mode=None: types.SimpleNamespace(
		valid=lambda: True, xmin=0, ymin=0, zmin=0, xmax=x, ymax=y, zmax=z, center=[x / 2, y / 2, z / 2], size=[x, y, z])


class N(FakeNode):
	"""A stand-in node: stats, size, parents and an optional file."""

	def __init__(self, prims=0, size=None, parent=None, file=None, name=None, visible=True, drawables=None,
	             tex_bytes=0, tex=0, verts=None, **kw):
		extra = dict(kw)
		extra['getStats'] = stats(prims, prims * 2 if verts is None else verts,
		                          (1 if prims else 0) if drawables is None else drawables, tex, tex_bytes)
		extra['getParents'] = lambda: [self.parent] if self.parent is not None else []
		extra['getBoundingBox'] = bbox(size) if size else (lambda mode=None: types.SimpleNamespace(valid=lambda: False))
		if file is not None:
			extra['getFilename'] = (lambda: file) if not isinstance(file, Exception) else self._raise
		if name is not None:
			extra['getName'] = lambda: name
		FakeNode.__init__(self, visible=visible, **extra)
		self.parent = parent

	@staticmethod
	def _raise():
		raise RuntimeError('no file')


class VizLight(FakeLight):
	pass


@pytest.fixture
def scene(vm, monkeypatch, tmp_path):
	viz = vm.viz_stub
	viz.ABS_GLOBAL = 1
	exists = tmp_path / 'car.osgb'
	exists.write_text('x')
	root = N(prims=0, size=(300, 10, 300), name='city')
	cars = [N(prims=5000, size=(4, 1.5, 2), parent=root, file=str(exists), tex_bytes=40 * MIB, tex=1) for _ in range(3)]
	big = N(prims=300, size=(5000, 20, 5000), parent=root, file='/nowhere/tower.obj')        # exported in mm
	speck = N(prims=20, size=(0.0004, 0.0004, 0.0004), parent=root)
	empty = N(prims=0, parent=root)
	empty_child = N(prims=0, parent=empty)
	hidden = N(prims=2000, size=(3, 3, 3), parent=root, visible=False)
	hidden_child = N(prims=500, size=(1, 1, 1), parent=hidden, visible=False)
	light = VizLight(getStats=stats(0), getParents=lambda: [root], getBoundingBox=lambda mode=None: types.SimpleNamespace(valid=lambda: False))
	broken = N(prims=100, size=(2, 2, 2), parent=root, file=RuntimeError('x'), name='ground')
	rel = N(prims=10, size=(2, 2, 2), parent=root, file='models/missing_relative.osgb')
	root.getStats = stats(prims=17430, verts=1, drawables=9)      # a group's figures include its children
	nodes = [root] + cars + [big, speck, empty, empty_child, hidden, hidden_child, light, broken, rel]
	viz.nodes = nodes
	viz.MainScene = types.SimpleNamespace(getStats=stats(prims=15000 + 300 + 20 + 2000 + 100 + 10, verts=99000,
	                                                     drawables=9, tex=3, tex_bytes=120 * MIB))
	names = {root.id: ['city'], big.id: ['tower'], cars[0].id: ['car0'], cars[1].id: ['car1'], cars[2].id: ['car2']}
	monkeypatch.setattr(vm, '_labels', lambda *a, **k: names)
	monkeypatch.chdir(tmp_path)
	return types.SimpleNamespace(vm=vm, root=root, cars=cars, big=big, speck=speck, empty=empty, empty_child=empty_child,
	                             hidden=hidden, hidden_child=hidden_child, light=light, broken=broken, rel=rel,
	                             nodes=nodes, viz=viz)


def ids(rows):
	return [r['id'] for r in rows]


def test_totals_come_from_the_scene_stats_and_are_json(scene):
	res = scene.vm.cmd_asset_check({})
	t = res['totals']
	assert t['primitives'] == 17430 and t['vertices'] == 99000 and t['textures'] == 3 and t['texture_bytes'] == 120 * MIB
	assert t['texture_mb'] == 120.0 and t['source'] == 'viz.MainScene.getStats()' and t['nodes'] == len(scene.nodes)
	json.dumps(res)


def test_top_nodes_rank_by_own_cost_and_carry_names_and_sizes(scene):
	res = scene.vm.cmd_asset_check({'top': 3})
	rows = res['top_nodes']
	assert len(rows) == 3
	assert all(r['own_primitives'] == 5000 for r in rows) and sorted(r['names'][0] for r in rows) == ['car0', 'car1', 'car2']
	assert rows[0]['size'] == [4, 1.5, 2] and rows[0]['file'].endswith('car.osgb') and rows[0]['texture_bytes'] == 40 * MIB
	assert scene.vm.cmd_asset_check({'top': 0})['top_nodes'] == []


def test_a_group_costs_nothing_of_its_own(scene):
	rows = {r['id']: r for r in scene.vm.cmd_asset_check({'top': 50})['top_nodes']}
	root = rows[scene.root.id]
	assert root['primitives'] == 17430 and root['own_primitives'] == 0
	scene.root.getStats = stats(prims=18000, verts=1, drawables=9)       # 570 of its own on top of the children
	root = next(r for r in scene.vm.cmd_asset_check({'top': 50})['top_nodes'] if r['id'] == scene.root.id)
	assert root['own_primitives'] == 570


def test_size_outliers_huge_tiny_and_sibling_scale(scene):
	res = scene.vm.cmd_asset_check({})
	flags = {r['id']: r['flags'] for r in res['size_outliers']}
	assert flags[scene.big.id] == ['huge', 'larger_than_siblings']
	assert flags[scene.speck.id] == ['tiny', 'smaller_than_siblings']
	assert not set(flags) & {c.id for c in scene.cars}
	# thresholds are parameters
	res = scene.vm.cmd_asset_check({'max_size_m': 10000, 'min_size_m': 1e-6, 'sibling_ratio': 1e9})
	assert res['size_outliers'] == []


def test_a_small_family_is_not_judged_against_siblings(scene):
	a = N(prims=10, size=(1, 1, 1), parent=scene.empty_child)
	b = N(prims=10, size=(900, 1, 1), parent=scene.empty_child)
	scene.viz.nodes += [a, b]
	flagged = ids(scene.vm.cmd_asset_check({})['size_outliers'])
	assert a.id not in flagged and b.id not in flagged         # two siblings are not enough for a median


def test_empty_nodes_listed_once_and_lights_excluded(scene):
	res = scene.vm.cmd_asset_check({})
	empty = ids(res['empty_nodes']['nodes'])
	assert scene.empty.id in empty and scene.empty_child.id not in empty
	assert scene.light.id not in empty and res['empty_nodes']['count'] == len(empty)


def test_hidden_nodes_that_cost_primitives_roll_up(scene):
	res = scene.vm.cmd_asset_check({})
	hc = res['hidden_costly']
	assert ids(hc['nodes']) == [scene.hidden.id] and hc['count'] == 1
	assert hc['primitives'] == 2000                              # the hidden child's subtree is inside its parent's
	scene.hidden.visible = lambda *a: None
	scene.hidden._visible = True
	scene.hidden_child._visible = False
	res = scene.vm.cmd_asset_check({})
	assert ids(res['hidden_costly']['nodes']) == [scene.hidden_child.id]


def test_duplicates_by_file_and_by_statistics(scene):
	res = scene.vm.cmd_asset_check({})
	assert len(res['duplicates']) == 1
	d = res['duplicates'][0]
	assert d['copies'] == 3 and d['primitives_each'] == 5000 and d['wasted_primitives'] == 10000
	assert d['basis'] == 'same file' and d['wasted_texture_bytes'] == 80 * MIB
	assert sorted(n['names'][0] for n in d['nodes']) == ['car0', 'car1', 'car2']
	for c in scene.cars:
		del c.getFilename
	assert scene.vm.cmd_asset_check({})['duplicates'][0]['basis'] == 'identical statistics'
	assert scene.vm.cmd_asset_check({'duplicate_min_primitives': 6000})['duplicates'] == []


def test_a_wrapper_around_its_twin_is_not_a_duplicate(scene):
	wrapper = N(prims=5000, size=(4, 1.5, 2), parent=scene.root, tex_bytes=40 * MIB, tex=1)
	scene.cars[0].parent = wrapper
	scene.viz.nodes.append(wrapper)
	d = scene.vm.cmd_asset_check({})['duplicates'][0]
	assert d['copies'] == 3 and wrapper.id in [n['id'] for n in d['nodes']] and scene.cars[0].id not in [n['id'] for n in d['nodes']]


def test_texture_hogs(scene):
	res = scene.vm.cmd_asset_check({})
	hogs = res['texture_hogs']
	assert len(hogs) == 3 and all(h['texture_mb'] == 40.0 and h['flagged'] for h in hogs)
	assert hogs[0]['share_of_texture_memory_pct'] == pytest.approx(33.3, abs=0.1)
	res = scene.vm.cmd_asset_check({'texture_hog_mb': 64})
	assert not any(h['flagged'] for h in res['texture_hogs'])


def test_files_probed_defensively_and_missing_ones_flagged(scene):
	res = scene.vm.cmd_asset_check({})
	files = res['files']
	missing = {m['file']: m for m in files['missing']}
	assert missing['/nowhere/tower.obj']['certain'] is True and missing['/nowhere/tower.obj']['names'] == ['tower']
	assert missing['models/missing_relative.osgb']['certain'] is False
	assert not any(f.endswith('car.osgb') for f in missing)
	rows = {r['id']: r for r in scene.vm.cmd_asset_check({'top': 100})['top_nodes']}
	assert rows[scene.broken.id]['file'] is None and rows[scene.broken.id]['name'] == 'ground'
	assert any('relative paths' in n for n in res['notes'])
	assert scene.vm.cmd_asset_check({'check_files': False})['files'] == {'checked': 0, 'missing': []}


def test_a_getname_that_looks_like_a_file_counts_as_one(scene):
	node = N(prims=10, size=(1, 1, 1), parent=scene.root, name='/nowhere/by_name.fbx')
	other = N(prims=10, size=(1, 1, 1), parent=scene.root, name='ground')
	scene.viz.nodes += [node, other]
	res = scene.vm.cmd_asset_check({'top': 100})
	rows = {r['id']: r for r in res['top_nodes']}
	assert rows[node.id]['file'] == '/nowhere/by_name.fbx' and rows[other.id]['file'] is None
	assert '/nowhere/by_name.fbx' in [m['file'] for m in res['files']['missing']]


def test_nodes_without_getstats_are_skipped_with_a_note(scene):
	plain = FakeNode()
	scene.viz.nodes.append(plain)
	res = scene.vm.cmd_asset_check({'top': 100})
	assert plain.id not in ids(res['top_nodes']) and any('no getStats' in n for n in res['notes'])


def test_without_scene_stats_totals_are_summed_over_roots(scene):
	del scene.viz.MainScene
	res = scene.vm.cmd_asset_check({})
	assert res['totals']['primitives'] == 17430 and 'top-level' in res['totals']['source']
	assert any('top-level' in n for n in res['notes'])


def test_no_file_names_anywhere_is_noted(vm, monkeypatch):
	viz = vm.viz_stub
	viz.ABS_GLOBAL = 1
	viz.nodes = [N(prims=5, size=(1, 1, 1))]
	monkeypatch.setattr(vm, '_labels', lambda *a, **k: {})
	res = vm.cmd_asset_check({})
	assert any('no node reported a file name' in n for n in res['notes'])


def test_findings_summarise_everything(scene):
	f = ' | '.join(scene.vm.cmd_asset_check({})['findings'])
	for word in ('suspicious size', 'nothing to draw', 'hidden node', 'duplicated heavy', 'textures', 'not found on disk'):
		assert word in f


def test_max_nodes_truncates(scene):
	res = scene.vm.cmd_asset_check({'max_nodes': 3})
	assert res['truncated'] is True and res['nodes_analysed'] <= 3 and any('max_nodes' in n for n in res['notes'])


def test_registered(vm):
	assert 'asset_check' in vm.COMMANDS


# --- the tool and the report section -------------------------------------------------------

RESULT = {
	'totals': {'nodes': 4, 'primitives': 100, 'vertices': 200, 'drawables': 3, 'textures': 1, 'texture_mb': 1.0,
	           'source': 'viz.MainScene.getStats()'},
	'top_nodes': [{'id': 1, 'type': 'VizNode', 'names': ['ball'], 'own_primitives': 90, 'primitives': 90, 'vertices': 10,
	               'textures': 1, 'texture_bytes': MIB, 'size': [1, 1, 1], 'flags': ['huge'], 'file': None}],
	'size_outliers': [{'id': 1, 'type': 'VizNode', 'names': ['ball'], 'size': [1, 1, 1], 'flags': ['huge']}],
	'hidden_costly': {'count': 1, 'primitives': 5, 'nodes': [{'id': 2, 'type': 'VizNode', 'names': [], 'primitives': 5}]},
	'empty_nodes': {'count': 1, 'nodes': [{'id': 3, 'type': 'VizGroup', 'names': ['g<b>']}]},
	'duplicates': [{'copies': 2, 'primitives_each': 50, 'wasted_primitives': 50, 'basis': 'same file',
	                'nodes': [{'id': 1, 'names': ['a']}, {'id': 2, 'names': []}]}],
	'texture_hogs': [{'id': 1, 'type': 'VizNode', 'names': ['ball'], 'texture_mb': 1.0, 'share_of_texture_memory_pct': 100.0, 'flagged': False}],
	'files': {'checked': 1, 'missing': [{'file': 'C:/x.obj', 'id': 1, 'names': ['ball'], 'certain': True}]},
	'findings': ['1 node(s) with a suspicious size'], 'notes': ['a note'],
}


def test_tool_forwards_parameters_and_adds_html_on_request(app):
	app['handler'] = lambda req: {'ok': True, 'result': RESULT}
	out = tool('asset_check', {'top': 5, 'max_size_m': 500, 'check_files': False})
	assert out['totals']['primitives'] == 100 and 'html_section' not in out
	p = sent(app, 'asset_check')[0]['params']
	assert p['top'] == 5 and p['max_size_m'] == 500 and p['check_files'] is False and p['sibling_ratio'] == 100.0
	out = tool('asset_check', {'html': True})
	assert '<table' in out['html_section'] and 'ball' in out['html_section']
	assert 'g<b>' not in out['html_section'] and 'g&lt;b&gt;' in out['html_section']      # escaped


def test_tool_argument_checks(app):
	app['handler'] = lambda req: {'ok': True, 'result': RESULT}
	for bad in ({'top': -1}, {'max_size_m': 0}, {'min_size_m': -1}, {'sibling_ratio': 0}, {'texture_hog_mb': 0},
	            {'max_nodes': 0}, {'duplicate_min_primitives': -5}):
		with pytest.raises(ToolError):
			tool('asset_check', bad)
	assert not sent(app, 'asset_check')


def test_report_html_fragments():
	html = '\n'.join(report.asset_check_html(RESULT))
	for text in ('Scene totals', 'Heaviest nodes', 'Size outliers', 'Hidden nodes', 'Duplicated heavy', 'Texture memory', 'C:/x.obj'):
		assert text in html
	clean = '\n'.join(report.asset_check_html({'totals': {}, 'findings': []}))
	assert 'PASS' in clean


def test_make_report_asset_check_section(app, tmp_path):
	def handler(req):
		if req['cmd'] == 'asset_check':
			return {'ok': True, 'result': RESULT}
		return {'ok': True, 'result': {'vizard_version': '8.3.1'}}
	app['handler'] = handler
	out_path = tmp_path / 'r.html'
	run(S.mcp.call_tool('make_report', {'path': str(out_path), 'sections': [
		{'type': 'asset_check', 'caption': 'Assets'}, {'type': 'asset_check', 'result': RESULT, 'top': 1}]}))
	html = out_path.read_text(encoding='utf-8')
	assert html.count('Scene totals') == 2 and 'Assets' in html
	assert len(sent(app, 'asset_check')) == 1                      # the second section used the result it was given
	with pytest.raises(ToolError, match='result must be'):
		run(S.mcp.call_tool('make_report', {'sections': [{'type': 'asset_check', 'result': 'x'}]}))
