"""Convert Vizard's uncompressed AVI recordings to H.264 MP4, outside Vizard.

Vizard's recorder (viz.window.startRecording) writes uncompressed AVI, about 25 MB per
second at 800x600. The conversion runs here in the server, in normal Python, with whatever
ffmpeg can be found.
"""

import os
import shutil
import subprocess


class VideoError(RuntimeError):
	"""The recording could not be converted; the message says why."""


NO_FFMPEG = ('ffmpeg not found. Put ffmpeg on PATH, set VIZMCP_FFMPEG to its full path, '
             'or run: pip install "vizard-mcp[video]"')


def find_ffmpeg():
	"""Return (path, source) for the first ffmpeg found, or None.

	Order: the VIZMCP_FFMPEG environment variable, PATH, the imageio-ffmpeg package."""
	env = os.environ.get('VIZMCP_FFMPEG')
	if env:
		found = env if os.path.isfile(env) else shutil.which(env)
		if found:
			return found, 'VIZMCP_FFMPEG'
	on_path = shutil.which('ffmpeg')
	if on_path:
		return on_path, 'PATH'
	try:
		import imageio_ffmpeg
		exe = imageio_ffmpeg.get_ffmpeg_exe()
		if exe and os.path.isfile(exe):
			return exe, 'imageio-ffmpeg'
	except Exception:           # not installed, or no binary bundled for this platform
		pass
	return None


def build_command(ffmpeg, src, dst, crf=23, fps=None, max_size=None):
	"""The ffmpeg command line: H.264, yuv420p, even dimensions, moov atom first."""
	filters = []
	if fps:
		filters.append('fps=%s' % fps)
	if max_size:
		# shrink to fit a max_size box, keeping the aspect ratio; never enlarge
		filters.append("scale=w='min(iw,%d)':h='min(ih,%d)':force_original_aspect_ratio=decrease" % (max_size, max_size))
	filters.append('scale=trunc(iw/2)*2:trunc(ih/2)*2')       # yuv420p needs even width and height
	return [ffmpeg, '-hide_banner', '-nostdin', '-y', '-i', src, '-vf', ','.join(filters),
	        '-c:v', 'libx264', '-preset', 'medium', '-crf', str(crf), '-pix_fmt', 'yuv420p',
	        '-an', '-movflags', '+faststart', dst]


def convert_to_mp4(src, dst, crf=23, fps=None, max_size=None, timeout_s=900.0):
	"""Convert `src` (AVI) to `dst` (MP4). Returns {'path', 'bytes', 'ffmpeg', 'ffmpeg_source'}.
	Raises VideoError, leaving `src` untouched and no partial `dst` behind."""
	found = find_ffmpeg()
	if found is None:
		raise VideoError(NO_FFMPEG)
	ffmpeg, source = found
	if not os.path.isfile(src) or os.path.getsize(src) == 0:
		raise VideoError('the recording %s is missing or empty' % src)
	cmd = build_command(ffmpeg, src, dst, crf, fps, max_size)
	try:
		proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
		                      stderr=subprocess.PIPE, timeout=timeout_s)
	except subprocess.TimeoutExpired:
		_remove(dst)
		raise VideoError('ffmpeg did not finish within %d s' % timeout_s)
	except OSError as e:
		raise VideoError('could not run ffmpeg (%s): %s' % (ffmpeg, e))
	if proc.returncode != 0:
		_remove(dst)
		tail = proc.stderr.decode('utf-8', 'replace').strip().splitlines()[-6:]
		raise VideoError('ffmpeg (%s) exited with code %d:\n%s' % (ffmpeg, proc.returncode, '\n'.join(tail)))
	if not os.path.isfile(dst) or os.path.getsize(dst) == 0:
		_remove(dst)
		raise VideoError('ffmpeg (%s) reported success but wrote no output' % ffmpeg)
	return {'path': dst, 'bytes': os.path.getsize(dst), 'ffmpeg': ffmpeg, 'ffmpeg_source': source}


def _remove(path):
	try:
		os.remove(path)
	except OSError:
		pass
