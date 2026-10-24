"""MP4 conversion and record_video / record_tour output handling, against a fake ffmpeg."""

import asyncio
import json
import os
import shutil
import subprocess
import sys
import types

import pytest

from conftest import FakeVizmcp
from fake_tools import make_fake_ffmpeg
from mcp.server.mcpserver.exceptions import ToolError
from test_new_tools import app, sent          # noqa: F401  (fixture)
from vizard_mcp import server as S
from vizard_mcp import video


def run(coro):
	return asyncio.run(coro)


@pytest.fixture
def ff(tmp_path, monkeypatch):
	"""A fake ffmpeg selected through VIZMCP_FFMPEG; ff.calls() lists the argument lists it got."""
	exe = make_fake_ffmpeg(tmp_path)
	log = tmp_path / 'ffmpeg.log'
	monkeypatch.setenv('VIZMCP_FFMPEG', exe)
	monkeypatch.setenv('FAKE_FFMPEG_LOG', str(log))
	for k in ('FAKE_FFMPEG_FAIL', 'FAKE_FFMPEG_NO_OUTPUT'):
		monkeypatch.delenv(k, raising=False)

	def calls():
		return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
	return types.SimpleNamespace(exe=exe, calls=calls)


def avi(tmp_path, name='clip.avi', size=5000):
	p = tmp_path / name
	p.write_bytes(b'RIFF' + b'\0' * size)
	return str(p)


# --- finding ffmpeg ------------------------------------------------------------

def test_find_ffmpeg_prefers_env_then_path_then_imageio(tmp_path, monkeypatch):
	env_exe = make_fake_ffmpeg(tmp_path, 'envff')
	path_dir = tmp_path / 'bin'
	path_dir.mkdir()
	path_exe = make_fake_ffmpeg(path_dir, 'ffmpeg')
	io_exe = make_fake_ffmpeg(tmp_path, 'ioff')
	monkeypatch.setitem(sys.modules, 'imageio_ffmpeg', types.SimpleNamespace(get_ffmpeg_exe=lambda: io_exe))
	monkeypatch.setenv('PATH', str(path_dir))
	monkeypatch.setenv('VIZMCP_FFMPEG', env_exe)
	assert video.find_ffmpeg() == (env_exe, 'VIZMCP_FFMPEG')
	monkeypatch.delenv('VIZMCP_FFMPEG')
	found = video.find_ffmpeg()
	assert found[1] == 'PATH' and os.path.samefile(found[0], path_exe)
	monkeypatch.setenv('PATH', str(tmp_path / 'empty'))
	assert video.find_ffmpeg() == (io_exe, 'imageio-ffmpeg')


def test_find_ffmpeg_skips_a_bad_env_value_and_missing_everything(tmp_path, monkeypatch):
	monkeypatch.setenv('VIZMCP_FFMPEG', str(tmp_path / 'nope'))
	monkeypatch.setenv('PATH', str(tmp_path / 'empty'))
	monkeypatch.setitem(sys.modules, 'imageio_ffmpeg', None)         # import fails
	assert video.find_ffmpeg() is None


def test_imageio_without_a_binary_is_not_an_ffmpeg(tmp_path, monkeypatch):
	def boom():
		raise RuntimeError('no binary for this platform')
	monkeypatch.delenv('VIZMCP_FFMPEG', raising=False)
	monkeypatch.setenv('PATH', str(tmp_path / 'empty'))
	monkeypatch.setitem(sys.modules, 'imageio_ffmpeg', types.SimpleNamespace(get_ffmpeg_exe=boom))
	assert video.find_ffmpeg() is None


# --- conversion ----------------------------------------------------------------

def test_command_line_is_h264_yuv420p_even_faststart(ff, tmp_path):
	src = avi(tmp_path)
	out = video.convert_to_mp4(src, str(tmp_path / 'clip.mp4'))
	assert out['ffmpeg_source'] == 'VIZMCP_FFMPEG' and out['bytes'] > 0
	args = ff.calls()[0]
	assert args[args.index('-i') + 1] == src and args[-1] == str(tmp_path / 'clip.mp4')
	assert args[args.index('-c:v') + 1] == 'libx264'
	assert args[args.index('-pix_fmt') + 1] == 'yuv420p'
	assert args[args.index('-crf') + 1] == '23'
	assert args[args.index('-movflags') + 1] == '+faststart'
	vf = args[args.index('-vf') + 1]
	assert vf == 'scale=trunc(iw/2)*2:trunc(ih/2)*2'
	assert '-an' in args


def test_crf_fps_and_max_size_reach_ffmpeg(ff, tmp_path):
	video.convert_to_mp4(avi(tmp_path), str(tmp_path / 'c.mp4'), crf=30, fps=24, max_size=640)
	args = ff.calls()[0]
	assert args[args.index('-crf') + 1] == '30'
	vf = args[args.index('-vf') + 1].split(',scale=')
	assert vf[0] == 'fps=24'
	assert "min(iw,640)" in args[args.index('-vf') + 1] and "min(ih,640)" in args[args.index('-vf') + 1]
	assert args[args.index('-vf') + 1].endswith('scale=trunc(iw/2)*2:trunc(ih/2)*2')


def test_failure_raises_with_stderr_and_leaves_no_partial_mp4(ff, tmp_path, monkeypatch):
	monkeypatch.setenv('FAKE_FFMPEG_FAIL', 'bad codec')
	src = avi(tmp_path)
	with pytest.raises(video.VideoError, match='(?s)exited with code 1.*boom: bad codec'):
		video.convert_to_mp4(src, str(tmp_path / 'c.mp4'))
	assert os.path.exists(src) and not (tmp_path / 'c.mp4').exists()


def test_success_without_output_is_a_failure(ff, tmp_path, monkeypatch):
	monkeypatch.setenv('FAKE_FFMPEG_NO_OUTPUT', '1')
	with pytest.raises(video.VideoError, match='wrote no output'):
		video.convert_to_mp4(avi(tmp_path), str(tmp_path / 'c.mp4'))


def test_missing_ffmpeg_and_empty_recording(tmp_path, monkeypatch):
	monkeypatch.delenv('VIZMCP_FFMPEG', raising=False)
	monkeypatch.setenv('PATH', str(tmp_path / 'empty'))
	monkeypatch.setitem(sys.modules, 'imageio_ffmpeg', None)
	with pytest.raises(video.VideoError, match=r'ffmpeg not found.*VIZMCP_FFMPEG.*vizard-mcp\[video\]'):
		video.convert_to_mp4(avi(tmp_path), str(tmp_path / 'c.mp4'))


def test_empty_recording_is_rejected(ff, tmp_path):
	empty = tmp_path / 'e.avi'
	empty.write_bytes(b'')
	with pytest.raises(video.VideoError, match='missing or empty'):
		video.convert_to_mp4(str(empty), str(tmp_path / 'e.mp4'))
	assert ff.calls() == []


@pytest.mark.skipif(shutil.which('ffmpeg') is None or shutil.which('ffprobe') is None, reason='needs real ffmpeg')
def test_real_ffmpeg_makes_even_yuv420p_h264_from_an_odd_size_avi(tmp_path, monkeypatch):
	monkeypatch.delenv('VIZMCP_FFMPEG', raising=False)
	src = str(tmp_path / 'odd.avi')
	subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc=size=321x241:rate=15:duration=1',
	                '-c:v', 'rawvideo', src], check=True)
	out = video.convert_to_mp4(src, str(tmp_path / 'odd.mp4'), crf=28, max_size=200)
	probe = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries',
	                        'stream=codec_name,pix_fmt,width,height', '-of', 'json', out['path']],
	                       check=True, stdout=subprocess.PIPE).stdout
	st = json.loads(probe)['streams'][0]
	assert st['codec_name'] == 'h264' and st['pix_fmt'] == 'yuv420p'
	assert st['width'] % 2 == 0 and st['height'] % 2 == 0 and max(st['width'], st['height']) <= 200
	with open(out['path'], 'rb') as f:
		data = f.read()
	assert 0 <= data.find(b'moov') < data.find(b'mdat')          # +faststart: the index comes first


# --- record_video tool ----------------------------------------------------------

def fake_recorder(app, tmp_path):
	"""A vizmcp that 'records' by writing a file where it is told to (or at a default path)."""
	def handler(req):
		p = req['params']
		path = p.get('path') or str(tmp_path / 'auto_video.avi')
		with open(path, 'wb') as f:
			f.write(b'\0' * 4000)
		return {'ok': True, 'result': {'path': path, 'bytes': 4000, 'seconds': p['seconds'], 'frames': 120}}
	app['handler'] = handler


def test_record_video_defaults_to_mp4_and_deletes_the_avi(app, ff, tmp_path):
	fake_recorder(app, tmp_path)
	res = json.loads(run(S.mcp.call_tool('record_video', {'seconds': 2})).content[0].text)
	assert res['format'] == 'mp4' and res['mp4_converted'] is True and res['avi_deleted'] is True
	assert res['path'] == str(tmp_path / 'auto_video.mp4') and os.path.getsize(res['path']) > 0
	assert not (tmp_path / 'auto_video.avi').exists()
	assert res['avi_bytes'] == 4000 and res['ffmpeg_source'] == 'VIZMCP_FFMPEG' and res['frames'] == 120
	assert 'path' not in sent(app, 'record_video')[0]['params']         # Vizard picks its own temp name


def test_record_video_user_path_gets_an_avi_beside_the_mp4(app, ff, tmp_path):
	fake_recorder(app, tmp_path)
	target = tmp_path / 'out' / 'demo.mp4'
	target.parent.mkdir()
	res = json.loads(run(S.mcp.call_tool('record_video', {'seconds': 1, 'path': str(target), 'crf': 18,
	                                                      'fps': 30, 'max_size': 800})).content[0].text)
	assert sent(app, 'record_video')[0]['params']['path'] == str(target.with_suffix('.avi'))
	assert res['path'] == str(target) and target.exists() and not target.with_suffix('.avi').exists()
	args = ff.calls()[0]
	assert args[args.index('-crf') + 1] == '18' and 'fps=30' in args[args.index('-vf') + 1]


def test_record_video_avi_format_keeps_the_avi_and_never_calls_ffmpeg(app, ff, tmp_path):
	fake_recorder(app, tmp_path)
	res = json.loads(run(S.mcp.call_tool('record_video', {'seconds': 1, 'format': 'avi',
	                                                      'path': str(tmp_path / 'raw.avi')})).content[0].text)
	assert res['format'] == 'avi' and res['path'] == str(tmp_path / 'raw.avi') and os.path.exists(res['path'])
	assert 'mp4_converted' not in res and ff.calls() == []


def test_record_video_keeps_the_avi_and_says_why_when_ffmpeg_fails(app, ff, tmp_path, monkeypatch):
	fake_recorder(app, tmp_path)
	monkeypatch.setenv('FAKE_FFMPEG_FAIL', 'no encoder')
	res = json.loads(run(S.mcp.call_tool('record_video', {'seconds': 1})).content[0].text)
	assert res['mp4_converted'] is False and res['format'] == 'avi'
	assert 'no encoder' in res['mp4_error'] and 'kept at' in res['mp4_error']
	assert os.path.exists(res['path']) and res['path'].endswith('.avi')


def test_record_video_without_any_ffmpeg_keeps_the_avi(app, tmp_path, monkeypatch):
	fake_recorder(app, tmp_path)
	monkeypatch.delenv('VIZMCP_FFMPEG', raising=False)
	monkeypatch.setenv('PATH', str(tmp_path / 'empty'))
	monkeypatch.setitem(sys.modules, 'imageio_ffmpeg', None)
	res = json.loads(run(S.mcp.call_tool('record_video', {'seconds': 1})).content[0].text)
	assert res['mp4_converted'] is False and 'ffmpeg not found' in res['mp4_error'] and os.path.exists(res['path'])


@pytest.mark.parametrize('args, msg', [
	({'format': 'mov'}, 'format must be'), ({'crf': 60}, 'crf must be'), ({'fps': 0}, 'fps must be'),
	({'max_size': 4}, 'max_size must be')])
def test_record_video_rejects_bad_options_before_recording(app, args, msg):
	with pytest.raises(ToolError, match=msg):
		run(S.mcp.call_tool('record_video', dict({'seconds': 1}, **args)))
	assert not sent(app, 'record_video')
