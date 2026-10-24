"""Server side of watch_export, screenshot_diff and scene_stats, against a fake vizmcp."""

import asyncio
import json

import pytest
from PIL import Image

from conftest import FakeVizmcp
from mcp.server.mcpserver.exceptions import ToolError
from vizard_mcp import imaging
from vizard_mcp import server as S
from vizard_mcp.client import VizardClient


def run(coro):
	return asyncio.run(coro)


@pytest.fixture
def app(monkeypatch):
	"""Install a fake vizmcp whose handler can be swapped per test."""
	state = {'handler': None}
	fake = FakeVizmcp(lambda req: state['handler'](req) if req['cmd'] != 'ping' else {'ok': True, 'result': 'pong'})
	monkeypatch.setattr(S, 'client', VizardClient(port=fake.port))
	state['fake'] = fake
	yield state
	S.client.close()
	fake.close()


def sent(app, cmd):
	return [r for r in app['fake'].requests if r['cmd'] == cmd]


# --- watch_export -----------------------------------------------------------

def test_watch_export_forwards_absolute_path_and_options(app, tmp_path):
	app['handler'] = lambda req: {'ok': True, 'result': {'path': req['params']['path'], 'rows': 3}}
	out = json.loads(run(S.mcp.call_tool('watch_export', {
		'name': 'bounce', 'path': str(tmp_path / 'r.json'), 'format': 'JSON', 'columns': ['t', 'h']})).content[0].text)
	assert out['rows'] == 3
	params = sent(app, 'watch_export')[0]['params']
	assert params == {'name': 'bounce', 'path': str(tmp_path / 'r.json'), 'format': 'json', 'columns': ['t', 'h']}


def test_watch_export_drops_unset_options(app, tmp_path):
	app['handler'] = lambda req: {'ok': True, 'result': {}}
	run(S.mcp.call_tool('watch_export', {'name': 'w', 'path': str(tmp_path / 'r.csv')}))
	assert set(sent(app, 'watch_export')[0]['params']) == {'name', 'path'}


def test_watch_export_rejects_unknown_format(app, tmp_path):
	with pytest.raises(ToolError, match='csv" or "json'):
		run(S.mcp.call_tool('watch_export', {'name': 'w', 'path': str(tmp_path / 'r.xlsx'), 'format': 'xlsx'}))
	assert not sent(app, 'watch_export')


def test_watch_export_vizard_error_reaches_client(app, tmp_path):
	app['handler'] = lambda req: {'ok': False, 'error': "ValueError: no watch named 'w'"}
	with pytest.raises(ToolError, match='no watch named'):
		run(S.mcp.call_tool('watch_export', {'name': 'w', 'path': str(tmp_path / 'r.csv')}))


# --- scene_stats ------------------------------------------------------------

def test_scene_stats_forwards_top_and_returns_json(app):
	stats = {'nodes': 5, 'visible_nodes': 4, 'top_nodes': []}
	app['handler'] = lambda req: {'ok': True, 'result': stats}
	assert json.loads(run(S.mcp.call_tool('scene_stats', {'top': 3})).content[0].text) == stats
	assert sent(app, 'scene_stats')[0]['params'] == {'top': 3}


def test_scene_stats_default_top_and_validation(app):
	app['handler'] = lambda req: {'ok': True, 'result': {}}
	run(S.mcp.call_tool('scene_stats', {}))
	assert sent(app, 'scene_stats')[0]['params'] == {'top': 10}
	with pytest.raises(ToolError, match='top must be'):
		run(S.mcp.call_tool('scene_stats', {'top': -1}))


# --- screenshot_diff --------------------------------------------------------

def _png(path, color=(40, 80, 120), size=(100, 64), box=None, box_color=(255, 255, 0)):
	im = Image.new('RGB', size, color)
	if box:
		im.paste(Image.new('RGB', (box[2] - box[0], box[3] - box[1]), box_color), box[:2])
	im.save(path)
	return str(path)


def _shot_handler(path):
	return lambda req: {'ok': True, 'result': {'path': path, 'frame': 1, 'window_size': [100, 64]}}


def _text(res):
	return json.loads(res[1])


def test_diff_identical_images_pass(app, tmp_path):
	cap = _png(tmp_path / 'cap.png')
	ref = _png(tmp_path / 'ref.png')
	app['handler'] = _shot_handler(cap)
	res = run(S.mcp.call_tool('screenshot_diff', {'reference': ref, 'threshold_pct': 0.0}))
	out = json.loads(res.content[1].text)
	assert out['diff_pct'] == 0 and out['changed_pixels'] == 0 and out['passed'] is True
	assert out['bbox'] is None
	assert res.content[0].type == 'image'


def test_diff_measures_percentage_and_bbox(app, tmp_path):
	cap = _png(tmp_path / 'cap.png', box=(10, 10, 30, 20))      # 20 x 10 = 200 of 6400 px
	ref = _png(tmp_path / 'ref.png')
	app['handler'] = _shot_handler(cap)
	diff_png = tmp_path / 'sub' / 'diff.png'
	res = run(S.mcp.call_tool('screenshot_diff', {'reference': ref, 'threshold_pct': 5,
	                                              'save_diff_to': str(diff_png)}))
	out = json.loads(res.content[1].text)
	assert out['changed_pixels'] == 200
	assert out['diff_pct'] == pytest.approx(3.125)
	assert out['bbox'] == [10, 10, 30, 20]
	assert out['passed'] is True                                # 3.125 <= 5
	with Image.open(diff_png) as d:
		assert d.size == (100, 64)
		assert d.convert('RGB').getpixel((15, 15)) == (255, 0, 0)    # changed pixel is red


def test_diff_fails_over_threshold(app, tmp_path):
	cap = _png(tmp_path / 'cap.png', box=(0, 0, 50, 64))
	ref = _png(tmp_path / 'ref.png')
	app['handler'] = _shot_handler(cap)
	out = json.loads(run(S.mcp.call_tool('screenshot_diff', {'reference': ref, 'threshold_pct': 10})).content[1].text)
	assert out['diff_pct'] == 50 and out['passed'] is False


def test_diff_without_threshold_has_no_verdict(app, tmp_path):
	cap = _png(tmp_path / 'cap.png', box=(0, 0, 5, 5))
	ref = _png(tmp_path / 'ref.png')
	app['handler'] = _shot_handler(cap)
	out = json.loads(run(S.mcp.call_tool('screenshot_diff', {'reference': ref})).content[1].text)
	assert 'passed' not in out and out['changed_pixels'] == 25


def test_diff_pixel_tolerance(app, tmp_path):
	cap = _png(tmp_path / 'cap.png', color=(45, 80, 120))        # 5 levels off in one channel
	ref = _png(tmp_path / 'ref.png')
	app['handler'] = _shot_handler(cap)
	loose = json.loads(run(S.mcp.call_tool('screenshot_diff', {'reference': ref})).content[1].text)
	strict = json.loads(run(S.mcp.call_tool('screenshot_diff', {'reference': ref, 'pixel_tolerance': 4})).content[1].text)
	assert loose['diff_pct'] == 0 and strict['diff_pct'] == 100


def test_diff_size_mismatch_fails_with_threshold(app, tmp_path):
	cap = _png(tmp_path / 'cap.png', size=(100, 64))
	ref = _png(tmp_path / 'ref.png', size=(80, 64))
	app['handler'] = _shot_handler(cap)
	out = json.loads(run(S.mcp.call_tool('screenshot_diff', {'reference': ref, 'threshold_pct': 100})).content[1].text)
	assert out['size_mismatch'] is True and out['passed'] is False
	assert out['capture_size'] == [100, 64] and out['reference_size'] == [80, 64]


def test_diff_missing_reference_is_clear_error(app, tmp_path):
	app['handler'] = _shot_handler(_png(tmp_path / 'cap.png'))
	with pytest.raises(ToolError, match='No reference image'):
		run(S.mcp.call_tool('screenshot_diff', {'reference': str(tmp_path / 'nope.png')}))


def test_diff_update_reference_writes_baseline(app, tmp_path):
	cap = _png(tmp_path / 'cap.png', box=(1, 1, 9, 9))
	app['handler'] = _shot_handler(cap)
	ref = tmp_path / 'baselines' / 'ref.png'
	res = run(S.mcp.call_tool('screenshot_diff', {'reference': str(ref), 'update_reference': True}))
	assert json.loads(res.content[1].text)['reference_written'] == str(ref)
	with Image.open(ref) as a, Image.open(cap) as b:
		assert a.convert('RGB').tobytes() == b.convert('RGB').tobytes()
	# and comparing against it now matches
	out = json.loads(run(S.mcp.call_tool('screenshot_diff', {'reference': str(ref), 'threshold_pct': 0})).content[1].text)
	assert out['passed'] is True


def test_diff_validates_arguments(app, tmp_path):
	with pytest.raises(ToolError, match='threshold_pct'):
		run(S.mcp.call_tool('screenshot_diff', {'reference': 'x.png', 'threshold_pct': 101}))
	with pytest.raises(ToolError, match='pixel_tolerance'):
		run(S.mcp.call_tool('screenshot_diff', {'reference': 'x.png', 'pixel_tolerance': 300}))


def test_imaging_diff_tiles_and_identical():
	a = Image.new('RGB', (70, 40), (0, 0, 0))
	b = a.copy()
	b.putpixel((40, 5), (255, 255, 255))
	d = imaging.diff(b, a)
	assert d['changed_pixels'] == 1 and d['changed_tiles'] == 1 and d['bbox'] == [40, 5, 41, 6]
	assert imaging.diff(a, a)['changed_tiles'] == 0


def test_image_tools_work_through_the_sdk(app, tmp_path):
	"""Regression: [Image, text] results used to fail SDK output conversion."""
	cap = _png(tmp_path / 'cap.png')
	app['handler'] = _shot_handler(cap)
	res = run(S.mcp.call_tool('screenshot', {}))
	assert [c.type for c in res.content] == ['image', 'text']
