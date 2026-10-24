"""Stand-in programs for the tests."""

import os
import stat
import sys
import textwrap

FAKE_FFMPEG = textwrap.dedent('''\
	import json, os, sys
	args = sys.argv[1:]
	with open(os.environ['FAKE_FFMPEG_LOG'], 'a') as f:
		f.write(json.dumps(args) + '\\n')
	if os.environ.get('FAKE_FFMPEG_FAIL'):
		sys.stderr.write('boom: ' + os.environ['FAKE_FFMPEG_FAIL'] + '\\n')
		sys.exit(1)
	if os.environ.get('FAKE_FFMPEG_NO_OUTPUT'):
		sys.exit(0)
	with open(args[-1], 'wb') as f:
		f.write(b'\\x00\\x00\\x00\\x18ftypmp42' + b'x' * 100)
''')


def make_fake_ffmpeg(folder, name='fake_ffmpeg'):
	"""A program that logs its arguments (to $FAKE_FFMPEG_LOG) and writes a small file as its
	last argument. Returns its path; works on Windows too."""
	script = os.path.join(str(folder), name + '.py')
	with open(script, 'w') as f:
		f.write(FAKE_FFMPEG)
	if os.name == 'nt':
		exe = os.path.join(str(folder), name + '.cmd')
		with open(exe, 'w') as f:
			f.write('@"%s" "%s" %%*\r\n' % (sys.executable, script))
	else:
		exe = os.path.join(str(folder), name)
		with open(exe, 'w') as f:
			f.write('#!%s\n' % sys.executable + FAKE_FFMPEG)
		os.chmod(exe, os.stat(exe).st_mode | stat.S_IEXEC)
	return exe
