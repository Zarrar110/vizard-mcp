"""record_tour mode "steps": the stepping command in the app (against a stand-in viz module), the
PNG-sequence encoder, and the server tool (against a fake vizmcp and a fake ffmpeg)."""

import asyncio
import json
import os
import shutil
import subprocess

import pytest
from PIL import Image

from fake_tools import make_fake_ffmpeg
from mcp.server.mcpserver.exceptions import ToolError
from test_new_tools import app, sent          # noqa: F401  (fixture)
from test_tour import app_view, close, KF     # noqa: F401  (fixtures and data)
from test_vizard_side import vm               # noqa: F401  (fixture)
from vizard_mcp import server as S, video


def run(coro):
	return asyncio.run(coro)


def tool(name, args):
	return json.loads(run(S.mcp.call_tool(name, args)).content[0].text)


# --- the command in the app ---------------------------------------------------------

@pytest.fixture
def stepping(app_view, monkeypatch, tmp_path):
	"""app_view plus a capture that writes a tiny PNG and notes the held camera pose and frame."""
	vm, view = app_view, app_view.viz_stub.MainView
	vm._State.work_dir = str(tmp_path / 'work')
	shots = []

	def fake_capture(path=None):
		yield
		Image.new('RGB', (8, 6), (len(shots) * 10 % 255, 0, 0)).save(path)
		shots.append({'path': path, 'frame': vm.viz_stub.frame, 'pos': view.getPosition()})
		return {'path': path, 'bytes': os.path.getsize(path), 'frame': vm.viz_stub.frame, 'window_size': [8, 6]}
	monkeypatch.setattr(vm, '_capture', fake_capture)
	vm.shots = shots
	return vm


LINEAR = [{'position': [0, 0, 0], 'euler': [0, 0, 0], 't': 0}, {'position': [10, 0, 0], 'euler': [0, 0, 0], 't': 2}]


def test_steps_capture_fps_times_duration_numbered_frames_at_exact_times(stepping):
	vm = stepping
	res = vm.frame_driver(vm.cmd_record_tour({'mode': 'steps', 'keyframes': LINEAR, 'interp': 'linear', 'fps': 10}))
	assert res['frames'] == 20 and res['fps'] == 10 and res['duration'] == 2 and res['mode'] == 'steps'
	names = sorted(os.listdir(res['frames_dir']))
	assert names == [res['frame_pattern'] % k for k in range(20)] and names[0] == 'frame_00000.png'
	assert [s['path'] for s in vm.shots] == [os.path.join(res['frames_dir'], n) for n in names]
	for k, s in enumerate(vm.shots):                        # frame k shows the camera at t = k / fps (5 m/s here)
		assert s['pos'][0] == pytest.approx(5.0 * k / 10)
	assert res['wall_s'] > 0 and res['wall_s_per_frame'] > 0 and res['window_size'] == [8, 6]
	assert res['path_kind'] == 'keyframes' and 'warning' not in res
	assert vm.viz_stub.rec == []                            # the recorder is not used


def test_steps_default_fps_is_30_and_orbit_looks_at_the_target(stepping):
	vm, view = stepping, stepping.viz_stub.MainView
	looks = []
	orig = view.lookAt
	view.lookAt = lambda p: (looks.append(list(p)), orig(p))[1]
	res = vm.frame_driver(vm.cmd_record_tour({'mode': 'steps', 'target': [4, 5, 6], 'radius': 5, 'duration': 1}))
	assert res['frames'] == 30 and res['fps'] == 30 and res['path_kind'] == 'orbit'
	assert looks and all(l == [4, 5, 6] for l in looks)


def test_steps_wait_settle_frames_before_every_capture(stepping):
	vm = stepping
	vm.frame_driver(vm.cmd_record_tour({'mode': 'steps', 'keyframes': LINEAR, 'fps': 2, 'settle_frames': 5}))
	frames = [s['frame'] for s in vm.shots]
	assert len(frames) == 4
	assert all(b - a >= 5 for a, b in zip(frames, frames[1:]))
	vm.shots.clear()
	vm.frame_driver(vm.cmd_record_tour({'mode': 'steps', 'keyframes': LINEAR, 'fps': 2, 'settle_frames': 0}))
	assert len(vm.shots) == 4


def test_steps_restore_the_camera_and_the_previous_hold(stepping):
	vm, view = stepping, stepping.viz_stub.MainView
	prev = lambda: None
	vm._set_hold(prev)
	vm.frame_driver(vm.cmd_record_tour({'mode': 'steps', 'keyframes': LINEAR, 'fps': 4}))
	assert vm._State.hold_apply is prev
	assert view.getPosition() == [1, 2, 3] and view.getEuler() == [10, 20, 30]


def test_steps_restore_the_camera_when_a_capture_fails(stepping):
	vm, view = stepping, stepping.viz_stub.MainView

	def broken(path=None):
		yield
		raise RuntimeError('capture broke')
	vm._capture = broken
	with pytest.raises(RuntimeError, match='capture broke'):
		vm.frame_driver(vm.cmd_record_tour({'mode': 'steps', 'keyframes': LINEAR, 'fps': 4}))
	assert view.getPosition() == [1, 2, 3] and view.getEuler() == [10, 20, 30] and vm._State.hold_apply is None


def test_steps_warn_when_the_window_is_resized(stepping, monkeypatch):
	vm, n = stepping, [0]
	inner = vm._capture

	def resizing(path=None):
		info = yield from inner(path)
		n[0] += 1
		info['window_size'] = [8 + n[0] // 3, 6]
		return info
	monkeypatch.setattr(vm, '_capture', resizing)
	res = vm.frame_driver(vm.cmd_record_tour({'mode': 'steps', 'keyframes': LINEAR, 'fps': 4}))
	assert 'changed size' in res['warning']


@pytest.mark.parametrize('extra, msg', [
	({'mode': 'frames'}, 'mode must be'),
	({'fps': 300}, 'fps must be'),
	({'fps': -1}, 'fps must be'),
	({'settle_frames': -1}, 'settle_frames'),
	({'fps': 240, 'duration': 1000}, 'too many')])
def test_steps_argument_errors(stepping, extra, msg):
	with pytest.raises(ValueError, match=msg):
		stepping.frame_driver(stepping.cmd_record_tour(dict({'mode': 'steps', 'keyframes': LINEAR}, **extra)))
	assert not stepping.shots


def test_dry_run_ignores_steps_mode(stepping, monkeypatch):
	def fake_multi(p):
		return {'shots': []}
		yield
	monkeypatch.setattr(stepping, 'cmd_multi_view', fake_multi)
	res = stepping.frame_driver(stepping.cmd_record_tour({'mode': 'steps', 'keyframes': KF, 'dry_run': True, 'count': 3}))
	assert res['dry_run'] is True and 'frames_dir' not in res


# --- the encoder ----------------------------------------------------------------------

@pytest.fixture
def ff(tmp_path, monkeypatch):
	monkeypatch.setenv('VIZMCP_FFMPEG', make_fake_ffmpeg(tmp_path))
	monkeypatch.setenv('FAKE_FFMPEG_LOG', str(tmp_path / 'ffmpeg.log'))
	monkeypatch.delenv('FAKE_FFMPEG_FAIL', raising=False)

	class Log(object):
		def calls(self):
			try:
				return [json.loads(l) for l in open(str(tmp_path / 'ffmpeg.log')) if l.strip()]
			except OSError:
				return []
	return Log()


def frames_in(folder, n, size=(9, 7)):
	os.makedirs(str(folder), exist_ok=True)
	for k in range(n):
		Image.new('RGB', size, (k * 20 % 255, 80, 120)).save(os.path.join(str(folder), 'frame_%05d.png' % k))
	return str(folder)


def test_sequence_command_reads_the_pattern_at_the_requested_rate_and_keeps_the_encoding_flags(ff, tmp_path):
	folder = frames_in(tmp_path / 'fr', 3)
	out = video.encode_sequence(folder, 'frame_%05d.png', str(tmp_path / 'o.mp4'), 24, crf=20, max_size=640)
	args = ff.calls()[0]
	i = args.index('-framerate')
	assert args[i + 1] == '24' and args[i + 2] == '-i' and args[i + 3] == os.path.join(folder, 'frame_%05d.png')
	assert args[args.index('-crf') + 1] == '20' and args[args.index('-pix_fmt') + 1] == 'yuv420p'
	assert '+faststart' in args and 'libx264' in args and args[-1] == str(tmp_path / 'o.mp4')
	vf = args[args.index('-vf') + 1]
	assert 'trunc(iw/2)*2' in vf and 'min(iw,640)' in vf and 'fps=' not in vf
	assert out['bytes'] > 0 and out['ffmpeg_source'] == 'VIZMCP_FFMPEG'
	assert len(os.listdir(folder)) == 3                       # the encoder never deletes frames


def test_fractional_fps_and_failures(ff, tmp_path, monkeypatch):
	folder = frames_in(tmp_path / 'fr', 2)
	video.encode_sequence(folder, 'frame_%05d.png', str(tmp_path / 'o.mp4'), 29.97)
	assert ff.calls()[0][ff.calls()[0].index('-framerate') + 1] == '29.97'
	with pytest.raises(video.VideoError, match='no frames to encode'):
		video.encode_sequence(str(tmp_path / 'nothing'), 'frame_%05d.png', str(tmp_path / 'x.mp4'), 30)
	monkeypatch.setenv('FAKE_FFMPEG_FAIL', 'bad input')
	with pytest.raises(video.VideoError, match='bad input'):
		video.encode_sequence(folder, 'frame_%05d.png', str(tmp_path / 'f.mp4'), 30)
	assert not (tmp_path / 'f.mp4').exists()


@pytest.mark.skipif(shutil.which('ffmpeg') is None or shutil.which('ffprobe') is None, reason='needs real ffmpeg')
def test_real_ffmpeg_encodes_the_sequence_with_exactly_one_video_frame_per_png(tmp_path, monkeypatch):
	monkeypatch.delenv('VIZMCP_FFMPEG', raising=False)
	folder = frames_in(tmp_path / 'fr', 24, size=(321, 241))
	out = video.encode_sequence(folder, 'frame_%05d.png', str(tmp_path / 'seq.mp4'), 12, max_size=200)
	probe = json.loads(subprocess.run(
		['ffprobe', '-v', 'error', '-count_frames', '-select_streams', 'v:0', '-show_entries',
		 'stream=codec_name,pix_fmt,width,height,nb_read_frames,r_frame_rate:format=duration', '-of', 'json', out['path']],
		check=True, stdout=subprocess.PIPE).stdout)
	st = probe['streams'][0]
	assert st['codec_name'] == 'h264' and st['pix_fmt'] == 'yuv420p' and int(st['nb_read_frames']) == 24
	assert st['r_frame_rate'] == '12/1' and float(probe['format']['duration']) == pytest.approx(2.0, abs=0.01)
	assert st['width'] % 2 == 0 and st['height'] % 2 == 0 and max(st['width'], st['height']) <= 200


# --- the server tool ------------------------------------------------------------------

@pytest.fixture
def steps_app(app, ff, tmp_path):
	def handler(req):
		p = req['params']
		assert p['mode'] == 'steps'
		folder = frames_in(tmp_path / ('frames_%d' % len(app['fake'].requests)), round(p['fps'] * (p.get('duration') or 2)))
		return {'ok': True, 'result': {
			'mode': 'steps', 'frames_dir': folder, 'frame_pattern': 'frame_%05d.png',
			'frames': round(p['fps'] * (p.get('duration') or 2)), 'fps': p['fps'], 'settle_frames': p['settle_frames'],
			'duration': p.get('duration') or 2.0, 'wall_s': 12.5, 'wall_s_per_frame': 0.2, 'window_size': [9, 7],
			'path_kind': 'orbit', 'path_description': 'orbit'}}
	app['handler'] = handler
	app['tmp'] = tmp_path
	return app


def test_steps_tool_encodes_the_frames_reports_the_cost_and_cleans_up(steps_app, ff):
	tmp = steps_app['tmp']
	target = tmp / 'smooth.mp4'
	res = tool('record_tour', {'target': [0, 0, 0], 'radius': 5, 'duration': 2, 'mode': 'steps', 'fps': 15,
	                           'path': str(target), 'crf': 18, 'max_size': 480})
	assert res['format'] == 'mp4' and res['mp4_converted'] and res['path'] == str(target) and target.exists()
	assert res['frames'] == 30 and res['fps'] == 15 and res['wall_s'] == 12.5 and res['frames_deleted'] is True
	assert 'frames_dir' not in res and 'wall time' in res['simulation_note'] and '12.5 s' in res['simulation_note']
	assert not (tmp / 'frames_1').exists()                      # PNGs and their folder are gone
	params = sent(steps_app, 'record_tour')[0]['params']
	assert params['mode'] == 'steps' and params['fps'] == 15 and params['settle_frames'] == 2 and params['duration'] == 2
	assert 'out_path' not in params
	args = ff.calls()[0]
	assert args[args.index('-framerate') + 1] == '15' and args[args.index('-crf') + 1] == '18'
	assert 'min(iw,480)' in args[args.index('-vf') + 1]


def test_steps_tool_defaults_to_30_fps_and_names_the_mp4_next_to_the_frames(steps_app):
	res = tool('record_tour', {'target': [0, 0, 0], 'duration': 2, 'mode': 'steps'})
	params = sent(steps_app, 'record_tour')[0]['params']
	assert params['fps'] == 30 and res['frames'] == 60
	assert res['path'] == str(steps_app['tmp'] / 'frames_1.mp4') and os.path.exists(res['path'])


def test_steps_tool_forwards_settle_frames_and_waits_long_enough(steps_app):
	tool('record_tour', {'keyframes': KF, 'mode': 'steps', 'fps': 10, 'settle_frames': 4})
	params = sent(steps_app, 'record_tour')[0]['params']
	assert params['settle_frames'] == 4 and params['keyframes'] == KF
	assert params['timeout_s'] >= 40 * 0.3 * 7 + 120           # 4 s at 10 fps = 40 frames


def test_steps_tool_keeps_the_frames_when_encoding_fails(steps_app, monkeypatch):
	monkeypatch.setenv('FAKE_FFMPEG_FAIL', 'nope')
	res = tool('record_tour', {'target': [0, 0, 0], 'duration': 1, 'mode': 'steps', 'fps': 5})
	assert res['mp4_converted'] is False and 'nope' in res['mp4_error'] and res['format'] == 'png'
	assert os.path.isdir(res['frames_dir']) and len(os.listdir(res['frames_dir'])) == 5
	assert res['frames_dir'] in res['mp4_error'] and 'wall time' in res['simulation_note']


def test_steps_tool_without_ffmpeg_keeps_the_frames(steps_app, monkeypatch, tmp_path):
	monkeypatch.delenv('VIZMCP_FFMPEG', raising=False)
	monkeypatch.setenv('PATH', str(tmp_path / 'empty'))
	monkeypatch.setitem(__import__('sys').modules, 'imageio_ffmpeg', None)
	res = tool('record_tour', {'target': [0, 0, 0], 'duration': 1, 'mode': 'steps', 'fps': 4})
	assert res['mp4_converted'] is False and 'ffmpeg not found' in res['mp4_error'] and os.path.isdir(res['frames_dir'])


def test_realtime_stays_the_default_and_sends_no_mode(app, tmp_path, ff):
	def handler(req):
		path = req['params'].get('out_path') or str(tmp_path / 'auto.avi')
		open(path, 'wb').write(b'\0' * 100)
		return {'ok': True, 'result': {'path': path, 'bytes': 100, 'seconds': 1, 'frames': 9, 'duration': 1}}
	app['handler'] = handler
	res = tool('record_tour', {'target': [0, 0, 0], 'duration': 1})
	assert res['mp4_converted'] and 'mode' not in sent(app, 'record_tour')[0]['params']


@pytest.mark.parametrize('args, msg', [
	({'mode': 'turbo'}, 'mode must be'),
	({'mode': 'steps', 'format': 'avi'}, 'MP4 only'),
	({'mode': 'steps', 'settle_frames': 61}, 'settle_frames'),
	({'mode': 'steps', 'settle_frames': -1}, 'settle_frames'),
	({'mode': 'steps', 'fps': 0}, 'fps must be'),
	({'mode': 'steps', 'fps': 240, 'duration': 600}, 'more than the limit')])
def test_steps_tool_argument_errors_do_not_touch_the_app(app, args, msg):
	with pytest.raises(ToolError, match=msg):
		run(S.mcp.call_tool('record_tour', dict({'target': [0, 0, 0], 'duration': 5}, **args)))
	assert not sent(app, 'record_tour')


def test_a_steps_dry_run_still_previews_and_records_nothing(app, tmp_path):
	def handler(req):
		shots = []
		for i in range(req['params']['count']):
			p = str(tmp_path / ('d%d.png' % i))
			Image.new('RGB', (40, 30)).save(p)
			shots.append({'path': p, 'view': {'position': [i, 0, 0], 'look_at': [0, 0, 0]}})
		return {'ok': True, 'result': {'shots': shots, 'duration': 2.0, 'path': 'orbit'}}
	app['handler'] = handler
	res = run(S.mcp.call_tool('record_tour', {'target': [0, 0, 0], 'mode': 'steps', 'dry_run': True, 'count': 3}))
	assert [c.type for c in res.content] == ['image', 'text']
	assert 'mode' not in sent(app, 'record_tour')[0]['params']
