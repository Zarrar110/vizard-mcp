"""record_tour: the path maths and the Vizard-side command (against a stand-in viz), and the
server tool (against a fake vizmcp)."""

import asyncio
import json
import math
import os
import types

import pytest
from PIL import Image

from fake_tools import make_fake_ffmpeg
from mcp.server.mcpserver.exceptions import ToolError
from test_gui_vr_validate import drive
from test_new_tools import app, sent          # noqa: F401  (fixture)
from test_vizard_side import vm      # noqa: F401  (fixture)
from vizard_mcp import server as S


def run(coro):
	return asyncio.run(coro)


def close(a, b, tol=1e-6):
	return all(abs(x - y) <= tol for x, y in zip(a, b))


KF = [{'position': [0, 1, 0], 'look_at': [0, 1, 10], 't': 0},
      {'position': [4, 2, 4], 'look_at': [0, 1, 10], 't': 2},
      {'position': [8, 1, 0], 'look_at': [0, 1, 10], 't': 4}]


# --- path maths -------------------------------------------------------------------

@pytest.mark.parametrize('interp', ['catmull', 'smoothstep', 'linear'])
def test_every_interpolation_passes_through_the_keyframes_and_holds_the_ends(vm, interp):
	path = vm._tour_path({'keyframes': KF, 'interp': interp})
	assert path.duration == 4
	for f in KF:
		assert close(path.at(f['t'])['position'], f['position'])
	assert close(path.at(-5)['position'], KF[0]['position']) and close(path.at(99)['position'], KF[-1]['position'])
	assert path.at(1)['look_at'] == [0, 1, 10] and 'euler' not in path.at(1)


def test_smoothstep_eases_and_linear_does_not(vm):
	a = [{'position': [0, 0, 0], 'euler': [0, 0, 0], 't': 0}, {'position': [10, 0, 0], 'euler': [0, 0, 0], 't': 1}]
	assert vm._tour_path({'keyframes': a, 'interp': 'linear'}).at(0.25)['position'][0] == pytest.approx(2.5)
	assert vm._tour_path({'keyframes': a, 'interp': 'smoothstep'}).at(0.25)['position'][0] == pytest.approx(1.5625)
	assert vm._tour_path({'keyframes': a, 'interp': 'smoothstep'}).at(0.5)['position'][0] == pytest.approx(5)


def test_catmull_rom_is_smooth_through_a_keyframe(vm):
	"""A spline keeps its velocity through the middle keyframe; per-segment easing would stop there."""
	path = vm._tour_path({'keyframes': KF})
	eps = 1e-4
	before = [(a - b) / eps for a, b in zip(path.at(2)['position'], path.at(2 - eps)['position'])]
	after = [(a - b) / eps for a, b in zip(path.at(2 + eps)['position'], path.at(2)['position'])]
	assert close(before, after, 1e-2) and abs(before[0]) > 1.0
	eased = vm._tour_path({'keyframes': KF, 'interp': 'smoothstep'})
	assert abs(eased.at(2 + eps)['position'][0] - eased.at(2)['position'][0]) < 1e-6        # stops at the keyframe


def test_euler_turns_the_short_way_round(vm):
	kf = [{'position': [0, 0, 0], 'euler': [350, 0, 0], 't': 0}, {'position': [1, 0, 0], 'euler': [10, 20, -30], 't': 2}]
	path = vm._tour_path({'keyframes': kf, 'interp': 'linear'})
	mid = path.at(1)['euler']
	assert mid[0] == pytest.approx(0) and mid[1] == pytest.approx(10) and mid[2] == pytest.approx(-15)
	assert path.at(0)['euler'][0] == pytest.approx(-10)          # 350 is -10, normalised to (-180, 180]
	assert path.at(2)['euler'][0] == pytest.approx(10)
	back = vm._tour_path({'keyframes': [{'position': [0, 0, 0], 'euler': [10, 0, 0], 't': 0},
	                                    {'position': [0, 0, 0], 'euler': [350, 0, 0], 't': 2}], 'interp': 'linear'})
	assert back.at(1)['euler'][0] == pytest.approx(0)            # not through 180


def test_euler_unwraps_over_several_keyframes(vm):
	kf = [{'position': [0, 0, 0], 'euler': [a, 0, 0]} for a in (170, -170, 170)]
	path = vm._tour_path({'keyframes': kf, 'duration': 4, 'interp': 'linear'})
	assert abs(path.at(1)['euler'][0]) == pytest.approx(180)     # 170 -> 190 by the short way
	assert abs(path.at(3)['euler'][0]) == pytest.approx(180)


def test_times_are_spread_evenly_without_t(vm):
	kf = [{'position': [x, 0, 0], 'euler': [0, 0, 0]} for x in (0, 3, 6)]
	path = vm._tour_path({'keyframes': kf, 'duration': 10, 'interp': 'linear'})
	assert path.duration == 10 and path.at(5)['position'][0] == pytest.approx(3)


def test_duration_longer_than_last_t_holds_the_final_pose(vm):
	path = vm._tour_path({'keyframes': KF, 'duration': 6})
	assert path.duration == 6 and close(path.at(5)['position'], KF[-1]['position'])


@pytest.mark.parametrize('params, msg', [
	({}, 'give keyframes, or orbit'),
	({'keyframes': KF[:1]}, 'at least 2'),
	({'keyframes': 'x'}, 'at least 2'),
	({'keyframes': KF, 'target': [0, 0, 0]}, 'not both'),
	({'keyframes': [{'look_at': [0, 0, 0]}] * 2}, 'needs a position'),
	({'keyframes': [{'position': [0, 0, 0]}] * 2}, 'exactly one of euler or look_at'),
	({'keyframes': [{'position': [0, 0, 0], 'euler': [0, 0, 0], 'look_at': [1, 1, 1]}] * 2}, 'exactly one'),
	({'keyframes': [KF[0], dict(KF[1], look_at=None, euler=[0, 0, 0])]}, 'not a mix'),
	({'keyframes': [dict(KF[0]), dict(KF[1], t=None)]}, 'on every keyframe or on none'),
	({'keyframes': [dict(f, position=[0, 0, 0]) for f in (dict(KF[0], t=1), dict(KF[1], t=1))]}, 'strictly increasing'),
	({'keyframes': [dict(KF[0], t=-1), KF[1]]}, 'strictly increasing'),
	({'keyframes': [{k: v for k, v in f.items() if k != 't'} for f in KF]}, 'give duration'),
	({'keyframes': KF, 'interp': 'cubic'}, 'interp must be'),
	({'keyframes': [dict(KF[0], position=[1, 2])] + KF[1:]}, 'expected 3 numbers')])
def test_bad_paths_are_clear_errors(vm, params, msg):
	with pytest.raises(ValueError, match=msg):
		vm._tour_path(params)


def test_orbit_around_a_point_circles_at_constant_radius_and_looks_at_the_centre(vm):
	path = vm._tour_path({'target': [1, 2, 3], 'radius': 10, 'elevation_deg': 30, 'degrees': 360,
	                      'start_yaw_deg': 0, 'duration': 8})
	assert path.duration == 8 and path.kind == 'orbit'
	for t in (0, 1, 2.5, 4, 7.9, 8):
		pose = path.at(t)
		d = [a - b for a, b in zip(pose['position'], [1, 2, 3])]
		assert math.sqrt(sum(x * x for x in d)) == pytest.approx(10)
		assert d[1] == pytest.approx(10 * math.sin(math.radians(30)))
		assert pose['look_at'] == [1, 2, 3]
	assert close(path.at(0)['position'], [1, 2 + 5, 3 + 10 * math.cos(math.radians(30))])       # yaw 0 = +z side
	assert close(path.at(2)['position'], [1 + 10 * math.cos(math.radians(30)), 7, 3], 1e-6)     # a quarter turn: +x
	assert close(path.at(0)['position'], path.at(8)['position'], 1e-6)                          # full circle closes


def test_orbit_partial_arc_and_negative_direction(vm):
	half = vm._tour_path({'target': [0, 0, 0], 'radius': 5, 'elevation_deg': 0, 'degrees': 180,
	                      'start_yaw_deg': 0, 'duration': 4})
	assert close(half.at(0)['position'], [0, 0, 5]) and close(half.at(4)['position'], [0, 0, -5], 1e-6)
	rev = vm._tour_path({'target': [0, 0, 0], 'radius': 5, 'elevation_deg': 0, 'degrees': -90,
	                     'start_yaw_deg': 0, 'duration': 4})
	assert close(rev.at(4)['position'], [-5, 0, 0], 1e-6)


def test_orbit_defaults_for_a_node_use_its_bounding_box(vm, monkeypatch):
	bb = types.SimpleNamespace(center=[0, 1, 0], size=[2, 1, 4], valid=lambda: True)
	node = vm.viz_stub.VizNode()
	node.getBoundingBox = lambda mode: bb
	vm.viz_stub.ABS_GLOBAL = 1
	monkeypatch.setattr(vm, '_resolve_node', lambda ref: node)
	path = vm._tour_path({'orbit': 'crane', 'elevation_deg': 0, 'start_yaw_deg': 0})
	assert path.duration == 8                                         # default
	assert close(path.at(0)['position'], [0, 1, 2 * 2.8])            # half of the largest side x 2.8
	assert vm._tour_path({'orbit': 'crane', 'radius': 9, 'elevation_deg': 0, 'start_yaw_deg': 0}).at(0)['position'][2] == pytest.approx(9)
	with pytest.raises(ValueError, match='more than 0'):
		vm._tour_path({'target': [0, 0, 0], 'duration': -1})


# --- the command in the app ---------------------------------------------------------

class FakeView(object):
	def __init__(self):
		self.pos, self.eul, self.look, self.log = [1, 2, 3], [10, 20, 30], None, []

	def getPosition(self):
		return list(self.pos)

	def getEuler(self):
		return list(self.eul)

	def setPosition(self, p):
		self.pos = list(p)

	def setEuler(self, e):
		self.eul, self.look = list(e), None

	def lookAt(self, p):
		self.look = list(p)


@pytest.fixture
def app_view(vm, monkeypatch):
	"""vizmcp with a fake view, recorder, hold hook and clock: 0.25 s passes per frame."""
	viz, clock = vm.viz_stub, {'now': 1000.0}
	viz.MainView = FakeView()
	viz.rec = []
	viz.window = types.SimpleNamespace(startRecording=lambda p: (viz.rec.append(('start', p)), open(p, 'wb').write(b'x' * 50)),
	                                   stopRecording=lambda: viz.rec.append(('stop',)))
	vm.time = types.SimpleNamespace(time=lambda: clock['now'])
	holds = []
	vm.vizact.onupdate = lambda prio, fn: (holds.append(types.SimpleNamespace(fn=fn, removed=False,
	                                                                           remove=lambda: None)), holds[-1])[1]
	vm.holds, vm.clock = holds, clock

	def frame_driver(gen):
		"""Like the pump: advance the generator one frame at a time, running the hold afterwards."""
		for _ in range(2000):
			try:
				next(gen)
			except StopIteration as e:
				return e.value
			clock['now'] += 0.25
			viz.frame += 1
			if vm._State.hold_apply is not None:
				vm._State.hold_apply()
		raise AssertionError('never finished')
	vm.frame_driver = frame_driver
	viz.getFrameTime = lambda: clock['now']
	yield vm
	vm._set_hold(None)
	vm._State.hold_apply = None


def test_record_tour_moves_the_view_while_recording_and_restores_it(app_view, tmp_path):
	vm, view = app_view, app_view.viz_stub.MainView
	seen = []
	orig = view.setPosition

	def spy(p):
		orig(p)
		seen.append((vm.time.time(), list(p), bool(vm.viz_stub.rec) and vm.viz_stub.rec[-1][0] == 'start'))
	view.setPosition = spy
	out = str(tmp_path / 'tour.avi')
	res = vm.frame_driver(vm.cmd_record_tour({'keyframes': [{'position': [0, 0, 0], 'euler': [0, 0, 0], 't': 0},
	                                                          {'position': [10, 0, 0], 'euler': [90, 0, 0], 't': 2}],
	                                          'interp': 'linear', 'out_path': out}))
	assert vm.viz_stub.rec == [('start', out), ('stop',)]
	assert res['path'] == out and res['bytes'] == 50 and res['duration'] == 2 and res['path_kind'] == 'keyframes'
	assert res['seconds'] >= 2
	during = [s for s in seen if s[2]]
	assert during, 'the camera was never moved while recording'
	xs = [pos[0] for _, pos, _ in during]
	assert xs == sorted(xs) and xs[0] < 3 and xs[-1] == pytest.approx(10)       # sweeps 0 -> 10 over the 2 s
	for t, pos, _ in during:                                          # linear path, 5 per second since the recording began
		assert pos[0] == pytest.approx(min(5.0 * (t - 1000.0 - 3 * 0.25 - 0.25), 10.0), abs=1.3)
	assert view.getPosition() == [1, 2, 3] and view.getEuler() == [10, 20, 30]      # the user's camera is back
	assert vm._State.hold_apply is None and vm._State.recording is None


def test_record_tour_gives_a_previous_hold_back(app_view, tmp_path):
	vm = app_view
	prev = lambda: None
	vm._set_hold(prev)
	vm.frame_driver(vm.cmd_record_tour({'target': [0, 0, 0], 'radius': 5, 'duration': 1, 'out_path': str(tmp_path / 't.avi')}))
	assert vm._State.hold_apply is prev


def test_record_tour_orbit_looks_at_the_target(app_view, tmp_path):
	vm, view = app_view, app_view.viz_stub.MainView
	looks = []
	orig = view.lookAt
	view.lookAt = lambda p: (looks.append(list(p)), orig(p))[1]
	vm.frame_driver(vm.cmd_record_tour({'target': [4, 5, 6], 'radius': 5, 'duration': 1, 'out_path': str(tmp_path / 't.avi')}))
	assert looks and all(l == [4, 5, 6] for l in looks)


def test_record_tour_restores_the_view_when_recording_fails(app_view, tmp_path):
	vm, viz = app_view, app_view.viz_stub

	def broken(path):
		raise RuntimeError('recorder broke')
	viz.window.startRecording = broken
	with pytest.raises(RuntimeError, match='recorder broke'):
		vm.frame_driver(vm.cmd_record_tour({'target': [0, 0, 0], 'radius': 5, 'duration': 1, 'out_path': str(tmp_path / 't.avi')}))
	assert viz.MainView.getPosition() == [1, 2, 3] and vm._State.hold_apply is None


def test_record_tour_refuses_while_another_recording_runs(app_view, tmp_path):
	app_view._State.recording = {'path': 'other.avi', 't0': 0}
	try:
		with pytest.raises(ValueError, match='already recording to other.avi'):
			app_view.frame_driver(app_view.cmd_record_tour({'target': [0, 0, 0], 'radius': 5, 'duration': 1}))
	finally:
		app_view._State.recording = None


def test_record_video_still_works_through_the_shared_recorder(app_view, tmp_path):
	out = str(tmp_path / 'v.avi')
	res = app_view.frame_driver(app_view.cmd_record_video({'seconds': 1, 'path': out}))
	assert res['path'] == out and res['bytes'] == 50 and res['frames'] >= 1
	assert app_view.viz_stub.rec == [('start', out), ('stop',)]
	assert app_view._State.recording is None


def test_dry_run_captures_frames_along_the_path_and_restores_the_view(app_view, monkeypatch):
	vm, view = app_view, app_view.viz_stub.MainView
	views = []

	def fake_multi(p):
		views.extend(p['views'])
		return {'shots': [{'path': 'x%d.png' % i, 'view': v} for i, v in enumerate(p['views'])]}
		yield
	monkeypatch.setattr(vm, 'cmd_multi_view', fake_multi)
	res = vm.frame_driver(vm.cmd_record_tour({'keyframes': KF, 'dry_run': True, 'count': 5}))
	assert [v['label'] for v in views] == ['t = 0.00 s', 't = 1.00 s', 't = 2.00 s', 't = 3.00 s', 't = 4.00 s']
	assert close(views[2]['position'], KF[1]['position']) and views[2]['look_at'] == [0, 1, 10]
	assert res['dry_run'] is True and res['duration'] == 4 and len(res['shots']) == 5
	assert not app_view.viz_stub.rec                                   # nothing recorded


def test_dry_run_uses_the_real_multi_view_and_leaves_the_camera_alone(app_view, monkeypatch):
	vm, view = app_view, app_view.viz_stub.MainView
	shots = iter(range(100))

	def fake_capture(path=None):
		yield
		return {'path': 'shot%d.png' % next(shots), 'frame': 1}
	monkeypatch.setattr(vm, '_capture', fake_capture)
	res = vm.frame_driver(vm.cmd_record_tour({'target': [0, 0, 0], 'radius': 5, 'duration': 4, 'dry_run': True, 'count': 3}))
	assert len(res['shots']) == 3 and res['shots'][1]['view']['label'] == 't = 2.00 s'
	assert view.getPosition() == [1, 2, 3] and view.getEuler() == [10, 20, 30]


def test_dry_run_clamps_count(app_view, monkeypatch):
	got = []

	def fake_multi(p):
		got.append(len(p['views']))
		return {'shots': []}
		yield
	monkeypatch.setattr(app_view, 'cmd_multi_view', fake_multi)
	for count in (99, 0):
		app_view.frame_driver(app_view.cmd_record_tour({'target': [0, 0, 0], 'radius': 5, 'dry_run': True, 'count': count}))
	assert got == [12, 2]


def test_record_tour_is_registered(vm):
	assert 'record_tour' in vm.COMMANDS


# --- the server tool ---------------------------------------------------------------------

@pytest.fixture
def tour_app(app, tmp_path, monkeypatch):
	exe = make_fake_ffmpeg(tmp_path)
	monkeypatch.setenv('VIZMCP_FFMPEG', exe)
	monkeypatch.setenv('FAKE_FFMPEG_LOG', str(tmp_path / 'ffmpeg.log'))

	def handler(req):
		p = req['params']
		if p.get('dry_run'):
			shots = []
			for i in range(p['count']):
				path = str(tmp_path / ('s%d.png' % i))
				Image.new('RGB', (160, 120), (i * 20, 50, 90)).save(path)
				shots.append({'path': path, 'view': {'position': [i, 0, 0], 'look_at': [0, 0, 0], 'label': 't = %d s' % i}})
			return {'ok': True, 'result': {'shots': shots, 'dry_run': True, 'duration': 4.0, 'path': 'orbit of 360 degrees'}}
		path = p.get('out_path') or str(tmp_path / 'auto_tour.avi')
		open(path, 'wb').write(b'\0' * 3000)
		return {'ok': True, 'result': {'path': path, 'bytes': 3000, 'seconds': 4.1, 'frames': 90,
		                               'duration': 4.0, 'path_kind': 'orbit'}}
	app['handler'] = handler
	return app


def test_record_tour_orbit_returns_an_mp4_and_forwards_the_path(tour_app, tmp_path):
	res = json.loads(run(S.mcp.call_tool('record_tour', {'orbit': 'crane', 'degrees': 180, 'duration': 4,
	                                                     'elevation_deg': 10, 'crf': 20})).content[0].text)
	assert res['format'] == 'mp4' and res['mp4_converted'] and res['path'].endswith('.mp4') and os.path.getsize(res['path']) > 0
	assert res['path_kind'] == 'orbit' and not (tmp_path / 'auto_tour.avi').exists()
	params = sent(tour_app, 'record_tour')[0]['params']
	assert params['orbit'] == 'crane' and params['degrees'] == 180 and params['duration'] == 4
	assert params['interp'] == 'catmull' and 'keyframes' not in params and 'out_path' not in params
	assert 'dry_run' not in params


def test_record_tour_keyframes_with_a_user_path(tour_app, tmp_path):
	target = tmp_path / 'fly.mp4'
	res = json.loads(run(S.mcp.call_tool('record_tour', {'keyframes': KF, 'path': str(target), 'interp': 'smoothstep'})).content[0].text)
	assert res['path'] == str(target) and target.exists()
	params = sent(tour_app, 'record_tour')[0]['params']
	assert params['keyframes'] == KF and params['out_path'] == str(target.with_suffix('.avi'))
	assert params['interp'] == 'smoothstep'


def test_record_tour_waits_long_enough_for_the_path(tour_app):
	run(S.mcp.call_tool('record_tour', {'keyframes': KF}))
	assert sent(tour_app, 'record_tour')[0]['params']['timeout_s'] == 4 + 120          # last keyframe t


def test_record_tour_avi_format_and_failed_conversion_keep_the_avi(tour_app, tmp_path, monkeypatch):
	res = json.loads(run(S.mcp.call_tool('record_tour', {'target': [0, 0, 0], 'format': 'avi'})).content[0].text)
	assert res['format'] == 'avi' and os.path.exists(res['path'])
	monkeypatch.setenv('FAKE_FFMPEG_FAIL', 'nope')
	res = json.loads(run(S.mcp.call_tool('record_tour', {'target': [0, 0, 0]})).content[0].text)
	assert res['mp4_converted'] is False and 'nope' in res['mp4_error'] and os.path.exists(res['path'])


def test_record_tour_dry_run_returns_a_filmstrip_and_records_nothing(tour_app):
	res = run(S.mcp.call_tool('record_tour', {'target': [0, 0, 0], 'radius': 5, 'dry_run': True, 'count': 4, 'preview_size': 600}))
	kinds = [c.type for c in res.content]
	assert kinds == ['image', 'text']
	text = res.content[1].text
	assert 'dry run, nothing recorded' in text and 't = 3 s: camera [3, 0, 0] -> [0, 0, 0]' in text
	params = sent(tour_app, 'record_tour')[0]['params']
	assert params['dry_run'] is True and params['count'] == 4
	assert 'out_path' not in params


@pytest.mark.parametrize('args, msg', [
	({}, 'Give keyframes, or orbit / target'),
	({'keyframes': KF, 'orbit': 'x'}, 'not both'),
	({'target': [0, 0, 0], 'interp': 'bezier'}, 'interp must be'),
	({'target': [0, 0, 0], 'duration': 0}, 'duration must be'),
	({'target': [0, 0, 0], 'format': 'gif'}, 'format must be')])
def test_record_tour_argument_errors_do_not_touch_the_app(tour_app, args, msg):
	with pytest.raises(ToolError, match=msg):
		run(S.mcp.call_tool('record_tour', args))
	assert not sent(tour_app, 'record_tour')


def test_record_tour_vizard_error_reaches_the_client(app):
	app['handler'] = lambda req: {'ok': False, 'error': 'ValueError: keyframes[1] needs a position'}
	with pytest.raises(ToolError, match='keyframes\\[1\\] needs a position'):
		run(S.mcp.call_tool('record_tour', {'keyframes': KF}))
