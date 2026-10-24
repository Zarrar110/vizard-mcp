"""`vizard-mcp doctor` and `vizard-mcp install`, with a temporary HOME / APPDATA and fake programs."""

import asyncio
import json
import os
import re
import socket
import subprocess
import sys

import pytest

from conftest import FakeVizmcp, free_port
from fake_tools import make_fake_ffmpeg
from vizard_mcp import cli, doctor, install
from vizard_mcp import server as S


@pytest.fixture
def home(tmp_path, monkeypatch):
	"""An isolated user: HOME / USERPROFILE / APPDATA in tmp, no Vizard, no ffmpeg, a free port."""
	h = tmp_path / 'home'
	appdata = h / 'AppData' / 'Roaming'
	appdata.mkdir(parents=True)
	for k in ('HOME', 'USERPROFILE'):
		monkeypatch.setenv(k, str(h))
	monkeypatch.setenv('APPDATA', str(appdata))
	monkeypatch.delenv('CLAUDE_CONFIG_DIR', raising=False)
	for k in ('VIZARD_EXE', 'VIZMCP_FFMPEG', 'VIZMCP_TOKEN'):
		monkeypatch.delenv(k, raising=False)
	monkeypatch.setenv('ProgramFiles', str(tmp_path / 'nope1'))
	monkeypatch.setenv('ProgramFiles(x86)', str(tmp_path / 'nope2'))
	monkeypatch.setenv('PATH', str(tmp_path / 'emptybin'))
	monkeypatch.setitem(sys.modules, 'imageio_ffmpeg', None)
	monkeypatch.setenv('VIZMCP_PORT', str(free_port()))
	return h


def code_cfg(home):
	return home / '.claude.json'


def desktop_cfg(home):
	return home / 'AppData' / 'Roaming' / 'Claude' / 'claude_desktop_config.json'


def by_name(checks):
	out = {}
	for c in checks:
		out.setdefault(c.name, []).append(c)
	return out


def install_fake_vizard(tmp_path, monkeypatch, folder='Vizard8'):
	exe = tmp_path / 'pf' / 'WorldViz' / folder / 'bin' / 'winviz.exe'
	exe.parent.mkdir(parents=True)
	exe.write_text('')
	monkeypatch.setenv('ProgramFiles', str(tmp_path / 'pf'))
	return str(exe)


def make_exe(tmp_path, name='vizard-mcp'):
	p = tmp_path / 'venv' / 'Scripts' / name
	p.parent.mkdir(parents=True, exist_ok=True)
	p.write_text('')
	return str(p)


# ============================== doctor ====================================================

def test_python_and_packages_pass_here(home):
	c = by_name(doctor.run_checks())
	assert c['Python'][0].status == 'PASS' and c['mcp package'][0].status == 'PASS' and c['Pillow'][0].status == 'PASS'


def test_old_python_fails(home, monkeypatch):
	monkeypatch.setattr(sys, 'version_info', (3, 9, 1, 'final', 0))
	c = doctor.check_python()
	assert c.status == 'FAIL' and '3.9.1' in c.detail and '3.10' in c.hint


def test_package_versions(monkeypatch):
	versions = {'mcp': '2.1.0', 'pillow': '9.5.0'}
	monkeypatch.setattr(doctor.importlib.metadata, 'version', lambda d: versions[d])
	assert doctor._package_check('mcp', 'mcp package', (2, 3), (3, 0)).status == 'WARN'
	assert 'pip install "mcp>=2.3,<3.0"' in doctor._package_check('mcp', 'mcp package', (2, 3), (3, 0)).hint
	versions['mcp'] = '3.0.0'
	assert doctor._package_check('mcp', 'mcp package', (2, 3), (3, 0)).status == 'WARN'
	versions['mcp'] = '2.3.0'
	assert doctor._package_check('mcp', 'mcp package', (2, 3), (3, 0)).status == 'PASS'
	assert doctor._package_check('pillow', 'Pillow', (10, 0)).status == 'WARN'


def test_missing_package_fails(monkeypatch):
	def missing(d):
		raise doctor.importlib.metadata.PackageNotFoundError(d)
	monkeypatch.setattr(doctor.importlib.metadata, 'version', missing)
	c = doctor._package_check('pillow', 'Pillow', (10, 0))
	assert c.status == 'FAIL' and 'pip install' in c.hint


def test_winviz_missing_fails_found_passes_with_version(home, tmp_path, monkeypatch):
	c = doctor.check_winviz()
	assert c.status == 'FAIL' and 'VIZARD_EXE' in c.hint
	exe = install_fake_vizard(tmp_path, monkeypatch, 'Vizard8')
	c = doctor.check_winviz()
	assert c.status == 'PASS' and exe in c.detail and 'Vizard 8 folder' in c.detail


def test_winviz_from_env_override_has_no_version_guess(home, tmp_path, monkeypatch):
	exe = tmp_path / 'custom' / 'winviz.exe'
	exe.parent.mkdir()
	exe.write_text('')
	monkeypatch.setenv('VIZARD_EXE', str(exe))
	c = doctor.check_winviz()
	assert c.status == 'PASS' and 'folder' not in c.detail


@pytest.mark.parametrize('path, version', [
	(r'C:\Program Files\WorldViz\Vizard8\bin\winviz.exe', '8'),
	('/x/WorldViz/Vizard8.3/bin/winviz.exe', '8.3'),
	('/x/Vizard 7/bin/winviz.exe', '7'),
	('/x/viz/bin/winviz.exe', None)])
def test_vizard_version_from_folder(path, version):
	from vizard_mcp import winviz
	assert winviz.version_from_path(path) == version


def test_port_free_passes(home):
	c = doctor.check_port()
	assert c.status == 'PASS' and 'is free' in c.detail


def test_port_with_vizmcp_app_passes(home, monkeypatch):
	fake = FakeVizmcp()
	monkeypatch.setenv('VIZMCP_PORT', str(fake.port))
	try:
		c = doctor.check_port()
		assert c.status == 'PASS' and 'vizmcp app is answering' in c.detail
	finally:
		fake.close()


def test_port_taken_by_something_else_warns(home, monkeypatch):
	srv = socket.socket()
	srv.bind(('127.0.0.1', 0))
	srv.listen(1)
	monkeypatch.setenv('VIZMCP_PORT', str(srv.getsockname()[1]))
	monkeypatch.setattr(doctor.VizardClient, 'ping', lambda self: False)        # it does not speak the protocol
	try:
		c = doctor.check_port()
		assert c.status == 'WARN' and 'in use' in c.detail and 'VIZMCP_PORT' in c.hint
	finally:
		srv.close()


def test_bad_port_value_fails(home, monkeypatch):
	monkeypatch.setenv('VIZMCP_PORT', 'abc')
	assert doctor.check_port().status == 'FAIL'


def test_ffmpeg_warn_then_pass(home, tmp_path, monkeypatch):
	c = doctor.check_ffmpeg()
	assert c.status == 'WARN' and 'vizard-mcp[video]' in c.hint
	monkeypatch.setenv('VIZMCP_FFMPEG', make_fake_ffmpeg(tmp_path))
	c = doctor.check_ffmpeg()
	assert c.status == 'PASS' and 'VIZMCP_FFMPEG' in c.detail


def test_nothing_registered_warns_for_both_clients(home):
	checks = doctor.check_registration('Claude Code', install.claude_code_config_path(), True) + \
	    doctor.check_registration('Claude Desktop', install.claude_desktop_config_path())
	assert [c.status for c in checks] == ['WARN', 'WARN']
	assert all('vizard-mcp install' in c.hint for c in checks)


def test_registered_with_existing_command_passes(home, tmp_path):
	exe = make_exe(tmp_path)
	code_cfg(home).write_text(json.dumps({'mcpServers': {'vizard': {'type': 'stdio', 'command': exe, 'args': []}}}))
	desktop_cfg(home).parent.mkdir(parents=True)
	desktop_cfg(home).write_text(json.dumps({'mcpServers': {'my-name': {'command': exe, 'args': []}}}))
	checks = doctor.check_registration('Claude Code', install.claude_code_config_path(), True) + \
	    doctor.check_registration('Claude Desktop', install.claude_desktop_config_path())
	assert [c.status for c in checks] == ['PASS', 'PASS']             # found by command, whatever the name


def test_registered_with_missing_command_fails(home, tmp_path):
	desktop_cfg(home).parent.mkdir(parents=True)
	desktop_cfg(home).write_text(json.dumps({'mcpServers': {'vizard': {'command': str(tmp_path / 'gone' / 'vizard-mcp.exe')}}}))
	(c,) = doctor.check_registration('Claude Desktop', install.claude_desktop_config_path())
	assert c.status == 'FAIL' and 'does not exist' in c.detail and 'vizard-mcp install' in c.hint


def test_python_dash_m_registration_is_checked_by_its_python(home, tmp_path):
	code_cfg(home).write_text(json.dumps({'mcpServers': {'viz': {'command': sys.executable, 'args': ['-m', 'vizard_mcp']}}}))
	(c,) = doctor.check_registration('Claude Code', install.claude_code_config_path(), True)
	assert c.status == 'PASS'


def test_project_scoped_registration_is_found(home, tmp_path):
	exe = make_exe(tmp_path)
	code_cfg(home).write_text(json.dumps({'projects': {'/work/lab': {'mcpServers': {'vizard': {'command': exe}}}}}))
	(c,) = doctor.check_registration('Claude Code', install.claude_code_config_path(), True)
	assert c.status == 'PASS' and '/work/lab' in c.detail


def test_other_servers_are_ignored(home):
	code_cfg(home).write_text(json.dumps({'mcpServers': {'github': {'command': 'npx', 'args': ['gh-mcp']}}}))
	(c,) = doctor.check_registration('Claude Code', install.claude_code_config_path(), True)
	assert c.status == 'WARN'


def test_broken_json_fails_with_a_hint(home):
	code_cfg(home).write_text('{ not json')
	(c,) = doctor.check_registration('Claude Code', install.claude_code_config_path(), True)
	assert c.status == 'FAIL' and 'not valid JSON' in c.detail


def test_report_format_and_exit_code(home, tmp_path, monkeypatch):
	lines = []
	code = doctor.main([], out=lines.append)
	text = lines[0]
	assert code == 1                                                   # no Vizard here: FAIL
	assert text.startswith('vizard-mcp 1.0.0 doctor')
	assert '[FAIL] Vizard (winviz.exe): not found' in text and '       fix: ' in text
	assert '[PASS] Python:' in text and '[WARN] ffmpeg' in text
	assert text.rstrip().splitlines()[-1].endswith('failed') and ' failed' in text
	install_fake_vizard(tmp_path, monkeypatch)
	lines.clear()
	assert doctor.main([], out=lines.append) == 0                      # warnings alone do not fail
	assert '0 failed' in lines[0] and 'warnings' in lines[0]


def test_pass_lines_have_no_fix_hint(home):
	checks = [doctor.Check('PASS', 'x', 'fine', 'unused hint')]
	assert 'fix:' not in doctor.render(checks)


def test_doctor_makes_no_network_calls(home, monkeypatch):
	"""The only connections doctor opens are to 127.0.0.1 (the vizmcp port check)."""
	real = socket.socket.connect
	hosts = []

	def spy(self, addr):
		hosts.append(addr[0])
		return real(self, addr)
	monkeypatch.setattr(socket.socket, 'connect', spy)
	doctor.run_checks()
	assert hosts and set(hosts) == {'127.0.0.1'}


def test_doctor_rejects_arguments(home):
	lines = []
	assert doctor.main(['--bogus'], out=lines.append) == 2


def test_doctor_tool_returns_the_report(home):
	from mcp.server.mcpserver.exceptions import ToolError  # noqa: F401
	res = asyncio.run(S.mcp.call_tool('doctor', {}))
	assert 'vizard-mcp 1.0.0 doctor' in res.content[0].text and '[PASS] Python' in res.content[0].text


def test_cli_doctor_exit_code_and_output(home, capfd, monkeypatch):
	with pytest.raises(SystemExit) as e:
		cli.main(['doctor'])
	assert e.value.code == 1
	assert '[FAIL] Vizard (winviz.exe)' in capfd.readouterr().out


def test_doctor_runs_as_a_subprocess_without_importing_the_server(home, tmp_path):
	"""`python -m vizard_mcp doctor` -- and the doctor path must not need the mcp package to import."""
	env = dict(os.environ, VIZMCP_PORT=str(free_port()))
	probe = ("import sys; from vizard_mcp import cli; "
	         "import builtins; real = builtins.__import__\n"
	         "def guard(name, *a, **k):\n"
	         "    if name == 'mcp' or name.startswith('mcp.'): raise ImportError('mcp is broken')\n"
	         "    return real(name, *a, **k)\n"
	         "builtins.__import__ = guard\n"
	         "for m in [m for m in sys.modules if m == 'mcp' or m.startswith('mcp.')]: del sys.modules[m]\n"
	         "try:\n"
	         "    cli.main(['doctor'])\n"
	         "except SystemExit as e:\n"
	         "    print('EXIT', e.code)\n")
	out = subprocess.run([sys.executable, '-c', probe], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
	text = out.stdout.decode()
	# The exit code depends on the machine (a child process on Windows still finds a real
	# Vizard install, since environment names there ignore case); the in-process test above
	# covers it. Here it only matters that the doctor ran to the end without mcp.
	assert 'vizard-mcp 1.0.0 doctor' in text and re.search(r'EXIT [01]\b', text), out.stderr.decode()
	assert 'Traceback' not in out.stderr.decode()


# ============================== install ===================================================

def run_install(*args, out=None):
	lines = [] if out is None else out
	code = install.main(list(args), out=lines.append)
	return code, '\n'.join(lines)


def test_install_adds_to_both_clients_and_keeps_other_servers(home, tmp_path):
	exe = make_exe(tmp_path)
	code_cfg(home).write_text(json.dumps({'numStartups': 7, 'mcpServers': {'github': {'command': 'npx'}}}, indent=2))
	desktop_cfg(home).parent.mkdir(parents=True)
	desktop_cfg(home).write_text(json.dumps({'theme': 'dark'}))
	code, text = run_install('--command', exe)
	assert code == 0 and 'Added "vizard"' in text
	c = json.loads(code_cfg(home).read_text())
	assert c['numStartups'] == 7 and c['mcpServers']['github'] == {'command': 'npx'}
	assert c['mcpServers']['vizard'] == {'type': 'stdio', 'command': exe, 'args': [], 'env': {}}
	d = json.loads(desktop_cfg(home).read_text())
	assert d['theme'] == 'dark' and d['mcpServers']['vizard'] == {'command': exe, 'args': []}


def test_install_backs_up_first_and_the_backup_is_the_original(home, tmp_path):
	exe = make_exe(tmp_path)
	original = json.dumps({'mcpServers': {}}, indent=2)
	code_cfg(home).write_text(original)
	code, text = run_install('--claude-code', '--command', exe)
	baks = [p for p in home.iterdir() if '.claude.json.bak-' in p.name]
	assert code == 0 and len(baks) == 1 and baks[0].read_text() == original and 'backup:' in text


def test_install_is_idempotent(home, tmp_path):
	exe = make_exe(tmp_path)
	run_install('--claude-code', '--claude-desktop', '--command', exe)
	before = (code_cfg(home).read_text(), desktop_cfg(home).read_text())
	files_before = sorted(p.name for p in home.rglob('*') if p.is_file())
	code, text = run_install('--claude-code', '--claude-desktop', '--command', exe)
	assert code == 0 and text.count('Nothing to do') == 2
	assert (code_cfg(home).read_text(), desktop_cfg(home).read_text()) == before
	assert sorted(p.name for p in home.rglob('*') if p.is_file()) == files_before         # no new backup either


def test_install_new_files_need_no_backup(home, tmp_path):
	exe = make_exe(tmp_path)
	code, text = run_install('--claude-desktop', '--command', exe)
	assert code == 0 and desktop_cfg(home).exists() and 'backup' not in text
	assert not any('.bak' in p.name for p in home.rglob('*'))


def test_install_updates_a_stale_command_and_keeps_user_keys(home, tmp_path):
	exe = make_exe(tmp_path)
	code_cfg(home).write_text(json.dumps({'mcpServers': {'vizard': {'type': 'stdio', 'command': 'C:/old/vizard-mcp.exe',
	                                                                 'args': [], 'env': {'VIZMCP_TOKEN': 'secret'}}}}))
	code, text = run_install('--claude-code', '--command', exe)
	entry = json.loads(code_cfg(home).read_text())['mcpServers']['vizard']
	assert code == 0 and 'Updated "vizard"' in text
	assert entry['command'] == exe and entry['env'] == {'VIZMCP_TOKEN': 'secret'}


def test_dry_run_shows_the_change_and_writes_nothing(home, tmp_path):
	exe = make_exe(tmp_path)
	code_cfg(home).write_text(json.dumps({'mcpServers': {}}, indent=2))
	before = code_cfg(home).read_text()
	code, text = run_install('--claude-code', '--claude-desktop', '--command', exe, '--dry-run')
	assert code == 0 and 'Would add "vizard"' in text and 'Dry run: nothing was written.' in text
	assert '+    "vizard": {' in text and exe.replace('\\', '\\\\') in text
	assert code_cfg(home).read_text() == before and not desktop_cfg(home).exists()
	assert not any('.bak' in p.name for p in home.rglob('*'))


def test_default_targets_are_the_clients_that_exist(home, tmp_path):
	exe = make_exe(tmp_path)
	code, text = run_install('--command', exe)                          # neither client present
	assert code == 1 and 'looks installed' in text and not code_cfg(home).exists()
	desktop_cfg(home).parent.mkdir(parents=True)                        # Claude Desktop's folder exists
	code, text = run_install('--command', exe)
	assert code == 0 and desktop_cfg(home).exists() and not code_cfg(home).exists()
	(home / '.claude').mkdir()                                          # now Claude Code too
	run_install('--command', exe)
	assert 'vizard' in json.loads(code_cfg(home).read_text())['mcpServers']


def test_broken_json_is_not_touched(home, tmp_path):
	exe = make_exe(tmp_path)
	code_cfg(home).write_text('{ oops')
	code, text = run_install('--claude-code', '--command', exe)
	assert code == 1 and 'not valid JSON' in text and code_cfg(home).read_text() == '{ oops'
	assert not any('.bak' in p.name for p in home.iterdir())


def test_one_failing_client_does_not_stop_the_other(home, tmp_path):
	exe = make_exe(tmp_path)
	code_cfg(home).write_text('[1, 2]')
	code, text = run_install('--claude-code', '--claude-desktop', '--command', exe)
	assert code == 1 and 'does not hold a JSON object' in text and desktop_cfg(home).exists()


def test_empty_file_and_non_object_servers(home, tmp_path):
	exe = make_exe(tmp_path)
	code_cfg(home).write_text('')
	assert run_install('--claude-code', '--command', exe)[0] == 0
	code_cfg(home).write_text(json.dumps({'mcpServers': []}))
	code, text = run_install('--claude-code', '--command', exe)
	assert code == 1 and 'is not an object' in text


def test_custom_name_and_claude_config_dir(home, tmp_path, monkeypatch):
	exe = make_exe(tmp_path)
	cfg = tmp_path / 'cc'
	cfg.mkdir()
	monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(cfg))
	run_install('--claude-code', '--name', 'viz2', '--command', exe)
	assert 'viz2' in json.loads((cfg / '.claude.json').read_text())['mcpServers'] and not code_cfg(home).exists()


def test_default_command_uses_this_python_when_no_script_exists(tmp_path, monkeypatch):
	monkeypatch.setattr(sys, 'executable', str(tmp_path / 'py' / 'python'))
	monkeypatch.setenv('PATH', str(tmp_path / 'emptybin'))
	assert install.default_command() == (str(tmp_path / 'py' / 'python'), ['-m', 'vizard_mcp'])
	scripts = tmp_path / 'py' / 'Scripts'
	scripts.mkdir(parents=True)
	(scripts / 'vizard-mcp.exe').write_text('')
	assert install.default_command() == (str(scripts / 'vizard-mcp.exe'), [])


def test_install_then_doctor_agree(home, tmp_path):
	exe = make_exe(tmp_path)
	run_install('--claude-code', '--claude-desktop', '--command', exe)
	c = by_name(doctor.run_checks())
	assert [x.status for x in c['Claude Code registration']] == ['PASS']
	assert [x.status for x in c['Claude Desktop registration']] == ['PASS']
	os.remove(exe)
	c = by_name(doctor.run_checks())
	assert [x.status for x in c['Claude Code registration']] == ['FAIL']


def test_cli_install_dry_run_via_main(home, tmp_path, capfd):
	exe = make_exe(tmp_path)
	with pytest.raises(SystemExit) as e:
		cli.main(['install', '--claude-code', '--dry-run', '--command', exe])
	assert e.value.code == 0 and 'Would add' in capfd.readouterr().out and not code_cfg(home).exists()


def test_cli_unknown_subcommand_and_help(capsys):
	with pytest.raises(SystemExit) as e:
		cli.main(['doctr'])
	assert e.value.code == 2 and 'vizard-mcp doctor' in capsys.readouterr().err
	cli.main(['--help'])
	assert 'vizard-mcp install' in capsys.readouterr().out


def test_cli_copy_module(tmp_path, capsys):
	cli.main(['copy-module', str(tmp_path)])
	assert (tmp_path / 'vizmcp.py').exists()


def test_install_treats_slash_styles_of_the_same_path_as_unchanged():
	have = {'type': 'stdio', 'command': 'C:/VizardMCP/.venv/Scripts/vizard-mcp.exe', 'args': [], 'env': {}}
	cmd = (r'C:\VizardMCP\.venv\Scripts\vizard-mcp.exe' if os.name == 'nt'
	       else 'C:/VizardMCP/.venv/Scripts/vizard-mcp.exe')
	assert install.wanted_entry('claude-code', cmd, [], have) == have
