"""LAN / remote app: vizmcp's bind and token rules, the client's target switching, the connect tool,
launch_app refusing a remote target, and doctor's view of the target."""

import asyncio
import json
import socket
import threading
import time
import types

import pytest

from conftest import FakeVizmcp, free_port
from mcp.server.mcpserver.exceptions import ToolError
from test_vizard_side import vm            # noqa: F401  (fixture)
from vizard_mcp import doctor, server as S
from vizard_mcp.client import NotConnected, VizardClient, VizardError, is_local_host


def run(coro):
	return asyncio.run(coro)


def call(name, args=None):
	return json.loads(run(S.mcp.call_tool(name, args or {})).content[0].text)


# --- the client -----------------------------------------------------------------------

@pytest.mark.parametrize('host,local', [
	('127.0.0.1', True), ('127.5.6.7', True), ('localhost', True), ('LOCALHOST', True), ('::1', True), ('[::1]', True),
	('192.168.1.20', False), ('0.0.0.0', False), ('exhibition-pc', False), ('exhibition-pc.local', False),
	('', False), (None, False)])
def test_is_local_host(host, local):
	assert is_local_host(host) is local


def test_configure_keeps_unset_values_and_clears_token():
	c = VizardClient(host='127.0.0.1', port=1000, token='t')
	c.configure(host=' pc ')
	assert (c.host, c.port, c.token) == ('pc', 1000, 't')
	c.configure(port='2000')
	assert (c.host, c.port, c.token) == ('pc', 2000, 't')
	c.configure(token='')
	assert c.token is None and c.is_local is False
	c.configure(host='127.0.0.1')
	assert c.is_local is True


def test_configure_drops_the_connection(fake):
	other = FakeVizmcp()
	try:
		c = VizardClient(port=fake.port)
		c.call('x')
		assert c._sock is not None
		c.configure(port=other.port)
		assert c._sock is None
		c.call('y')
		assert [r['cmd'] for r in fake.requests] == ['x'] and [r['cmd'] for r in other.requests] == ['y']
		c.close()
	finally:
		other.close()


def test_probe_states(fake):
	assert VizardClient(port=fake.port).probe() == ('ok', 'a vizmcp app answered')
	srv = FakeVizmcp(token='secret')
	try:
		state, detail = VizardClient(port=srv.port, token='wrong').probe()
		assert state == 'bad_token' and 'token' in detail
		assert VizardClient(port=srv.port, token='secret').probe()[0] == 'ok'
	finally:
		srv.close()
	state, detail = VizardClient(port=free_port()).probe()
	assert state == 'no_listener' and 'launch_app' in detail


def test_probe_on_something_that_is_not_vizmcp():
	s = socket.socket()
	s.bind(('127.0.0.1', 0))
	s.listen(1)

	def junk():
		conn, _ = s.accept()
		conn.recv(1000)
		conn.sendall(b'HTTP/1.1 400 nope\r\n')
		conn.close()
	threading.Thread(target=junk, daemon=True).start()
	try:
		assert VizardClient(port=s.getsockname()[1]).probe()[0] == 'error'
	finally:
		s.close()


def test_remote_connection_error_points_at_firewall_and_token(monkeypatch):
	import vizard_mcp.client as cm
	monkeypatch.setattr(cm.socket, 'create_connection', lambda *a, **k: (_ for _ in ()).throw(OSError('timed out')))
	with pytest.raises(NotConnected) as e:
		VizardClient(host='192.0.2.1', port=free_port()).call('ping')
	assert '192.0.2.1' in str(e.value) and 'firewall' in str(e.value) and 'launch_app' not in str(e.value)
	with pytest.raises(NotConnected, match='launch_app'):
		VizardClient(port=free_port()).call('ping')


# --- vizmcp: bind and token ------------------------------------------------------------

@pytest.fixture
def started(vm, monkeypatch):
	"""vizmcp's start() with stand-ins for vizact and the frame pump; stops it afterwards."""
	removed = []
	vm.vizact = types.SimpleNamespace(onupdate=lambda prio, fn: types.SimpleNamespace(remove=lambda: removed.append(1)),
	                                  onexit=lambda fn: None)
	vm._vr_restore = lambda: None
	for k in ('VIZMCP_BIND', 'VIZMCP_TOKEN', 'VIZMCP_PORT'):
		monkeypatch.delenv(k, raising=False)
	vm._server = None
	yield vm
	vm.stop()


def pump_until(vm, stop):
	while not stop.is_set():
		vm._pump()
		time.sleep(0.005)


def test_start_defaults_to_loopback(started, capsys):
	port = free_port()
	assert started.start(port=port) == port
	assert started._server.host == '127.0.0.1'
	assert 'listening on 127.0.0.1:%d' % port in capsys.readouterr().out


def test_start_refuses_a_network_bind_without_token(started, monkeypatch, capsys):
	for how in ('arg', 'env'):
		if how == 'env':
			monkeypatch.setenv('VIZMCP_BIND', '0.0.0.0')
		assert started.start(port=free_port(), host='0.0.0.0' if how == 'arg' else None) is None
		assert started._server is None
		out = capsys.readouterr().out
		assert 'NOT listening' in out and 'VIZMCP_TOKEN' in out


def test_start_with_bind_and_token_listens_and_enforces_the_token(started, monkeypatch):
	port = free_port()
	monkeypatch.setenv('VIZMCP_BIND', '0.0.0.0')
	monkeypatch.setenv('VIZMCP_TOKEN', 'exhibition-secret')
	assert started.start(port=port) == port
	assert started._server.host == '0.0.0.0'
	monkeypatch.delenv('VIZMCP_TOKEN')       # the clients below must not pick the token up from here
	stop = threading.Event()
	threading.Thread(target=pump_until, args=(started, stop), daemon=True).start()
	try:
		with pytest.raises(VizardError, match='token'):
			VizardClient(port=port, token='nope').call('ping')
		with pytest.raises(VizardError, match='token'):
			VizardClient(port=port, token='').call('ping')
		c = VizardClient(port=port, token='exhibition-secret')
		assert c.call('ping')['pong'] is True
		c.close()
	finally:
		stop.set()


def test_loopback_names_need_no_token(started, monkeypatch):
	for host in ('localhost', '127.0.0.1', '::1'):
		if host == '::1':
			continue                      # an IPv4 socket cannot bind it; the rule is covered by _is_loopback
		monkeypatch.setenv('VIZMCP_BIND', host)
		port = free_port()
		assert started.start(port=port) == port
		started.stop()
		started._server = None
	assert started._is_loopback('::1') and not started._is_loopback('10.0.0.5') and not started._is_loopback('pc')


def test_token_compare_is_exact(started):
	srv = started._Server('127.0.0.1', 0, 'abc')
	for tok in (None, '', 'ab', 'abcd', 5, ['abc']):
		reply = srv._handle(json.dumps({'id': 1, 'cmd': 'ping', 'token': tok}).encode())
		assert reply['ok'] is False and 'token' in reply['error']


# --- the connect tool and launch_app --------------------------------------------------

@pytest.fixture
def remote(monkeypatch):
	"""A fake vizmcp 'on another PC': connections to the name exhibition-pc go to it."""
	fake = FakeVizmcp(token='secret')
	import vizard_mcp.client as cm
	real = cm.socket.create_connection

	def create(addr, *a, **k):
		host, port = addr
		return real(('127.0.0.1', port) if host == 'exhibition-pc' else addr, *a, **k)
	monkeypatch.setattr(cm.socket, 'create_connection', create)
	monkeypatch.setattr(S, 'client', VizardClient(port=free_port()))
	monkeypatch.setitem(S._launched, 'proc', None)
	yield fake
	S.client.close()
	fake.close()


def info_handler(req):
	if req['cmd'] == 'info':
		return {'ok': True, 'result': {'script': 'C:/expo/main.py', 'vizard_version': '8.3.1', 'vizmcp_version': '1.0.0',
		                               'listening': '0.0.0.0:%d' % 1, 'window_size': [1920, 1080], 'node_count': 12,
		                               'commands': ['ping']}}
	return {'ok': True, 'result': 'pong'}


def test_connect_reports_the_remote_app_and_never_echoes_the_token(remote):
	remote.handler = info_handler
	out = call('connect', {'host': 'exhibition-pc', 'port': remote.port, 'token': 'secret'})
	assert out['reachable'] is True and out['local'] is False and out['token_set'] is True
	assert out['host'] == 'exhibition-pc' and out['port'] == remote.port
	assert out['app']['script'] == 'C:/expo/main.py' and out['app']['node_count'] == 12
	assert 'secret' not in json.dumps(out)
	assert all(r.get('token') == 'secret' for r in remote.requests)
	st = call('vizard_status')
	assert st['connected'] is True and st['target'] == 'exhibition-pc:%d' % remote.port


def test_connect_without_arguments_reports_the_current_target(remote):
	remote.handler = info_handler
	call('connect', {'host': 'exhibition-pc', 'port': remote.port, 'token': 'secret'})
	again = call('connect')
	assert again['reachable'] is True and again['host'] == 'exhibition-pc' and again['token_set'] is True


def test_connect_with_wrong_token_says_so(remote):
	out = call('connect', {'host': 'exhibition-pc', 'port': remote.port, 'token': 'wrong'})
	assert out['reachable'] is False and 'rejected the token' in out['problem'] and 'VIZMCP_TOKEN' in out['hint']
	out = call('connect', {'token': 'secret'})            # only the token changes
	assert out['reachable'] is True and out['host'] == 'exhibition-pc'


def test_connect_with_no_listener_and_no_token(remote):
	out = call('connect', {'host': 'exhibition-pc', 'port': free_port(), 'token': ''})
	assert out['reachable'] is False and out['problem'] == 'nothing is listening there'
	assert 'firewall' in out['detail'] and 'warning' in out and out['token_set'] is False


def test_connect_back_to_this_pc(remote):
	call('connect', {'host': 'exhibition-pc', 'port': remote.port, 'token': 'secret'})
	local = FakeVizmcp()
	try:
		out = call('connect', {'host': '127.0.0.1', 'port': local.port, 'token': ''})
		assert out['reachable'] is True and out['local'] is True and 'warning' not in out
	finally:
		local.close()


def test_connect_validates_arguments(remote):
	for bad in ({'host': '  '}, {'port': 0}, {'port': 70000}):
		with pytest.raises(ToolError):
			call('connect', bad)


def test_launch_app_refuses_a_remote_target(remote, monkeypatch, tmp_path):
	import subprocess
	monkeypatch.setattr(subprocess, 'Popen', lambda *a, **k: pytest.fail('must not start a process'))
	script = tmp_path / 'app.py'
	script.write_text('pass')
	call('connect', {'host': 'exhibition-pc', 'port': remote.port, 'token': 'secret'})
	with pytest.raises(ToolError, match=r'another PC.*connect'):
		call('launch_app', {'script': str(script)})
	assert not [r for r in remote.requests if r['cmd'] != 'ping' and r['cmd'] != 'info']


def test_launched_app_only_gets_vizmcp_bind_when_there_is_a_token(monkeypatch, tmp_path):
	import subprocess
	seen = {}

	class Stop(Exception):
		pass

	def popen(cmd, **kw):
		seen['env'] = kw['env']
		raise Stop()
	monkeypatch.setattr(subprocess, 'Popen', popen)
	monkeypatch.setattr(S, 'find_winviz', lambda: 'winviz.exe')
	monkeypatch.setenv('VIZMCP_BIND', '0.0.0.0')
	script = tmp_path / 'app.py'
	script.write_text('pass')
	for token, expect_bind in (('t0k', True), (None, False)):
		monkeypatch.setattr(S, 'client', VizardClient(port=free_port(), token=token))
		with pytest.raises(Stop):
			S.launch_app(str(script))
		assert ('VIZMCP_BIND' in seen['env']) is expect_bind
		assert seen['env'].get('VIZMCP_TOKEN') == token


# --- doctor ------------------------------------------------------------------------------

@pytest.fixture
def home(tmp_path, monkeypatch):
	for k in ('VIZMCP_HOST', 'VIZMCP_PORT', 'VIZMCP_TOKEN', 'VIZMCP_BIND'):
		monkeypatch.delenv(k, raising=False)


def test_doctor_remote_target_answers(home, remote, monkeypatch):
	monkeypatch.setenv('VIZMCP_HOST', 'exhibition-pc')
	monkeypatch.setenv('VIZMCP_PORT', str(remote.port))
	monkeypatch.setenv('VIZMCP_TOKEN', 'secret')
	c = doctor.check_port()
	assert c.status == 'PASS' and 'exhibition-pc:%d' % remote.port in c.detail and 'answers' in c.detail


def test_doctor_remote_target_without_token_warns(home, monkeypatch):
	fake = FakeVizmcp()
	monkeypatch.setenv('VIZMCP_HOST', 'exhibition-pc')
	monkeypatch.setenv('VIZMCP_PORT', str(fake.port))
	import vizard_mcp.client as cm
	real = cm.socket.create_connection
	monkeypatch.setattr(cm.socket, 'create_connection',
	                    lambda addr, *a, **k: real(('127.0.0.1', addr[1]) if addr[0] == 'exhibition-pc' else addr, *a, **k))
	try:
		c = doctor.check_port()
		assert c.status == 'WARN' and 'VIZMCP_TOKEN' in c.hint
	finally:
		fake.close()


def test_doctor_remote_target_down_or_wrong_token_fails(home, monkeypatch):
	monkeypatch.setenv('VIZMCP_HOST', '192.0.2.77')
	monkeypatch.setenv('VIZMCP_PORT', str(free_port()))
	import vizard_mcp.client as cm
	monkeypatch.setattr(cm.socket, 'create_connection', lambda *a, **k: (_ for _ in ()).throw(OSError('timed out')))
	c = doctor.check_port()
	assert c.status == 'FAIL' and '192.0.2.77' in c.detail and 'firewall' in c.hint.lower()

	srv = FakeVizmcp(token='right')
	try:
		monkeypatch.undo()
		monkeypatch.setenv('VIZMCP_HOST', 'localhost')
		monkeypatch.setattr(doctor, 'is_local_host', lambda h: False)      # treat it as a remote target
		monkeypatch.setenv('VIZMCP_PORT', str(srv.port))
		monkeypatch.setenv('VIZMCP_TOKEN', 'wrong')
		c = doctor.check_port()
		assert c.status == 'FAIL' and 'rejected the token' in c.detail
	finally:
		srv.close()


def test_doctor_local_target_text_is_unchanged(home, monkeypatch):
	monkeypatch.setenv('VIZMCP_PORT', str(free_port()))      # 9877 may be taken on the dev machine
	c = doctor.check_port()
	assert c.status == 'PASS' and 'is free' in c.detail


def test_doctor_exposure_check(home, monkeypatch):
	assert doctor.check_exposure() is None
	monkeypatch.setenv('VIZMCP_BIND', '127.0.0.1')
	assert doctor.check_exposure() is None
	monkeypatch.setenv('VIZMCP_BIND', '0.0.0.0')
	c = doctor.check_exposure()
	assert c.status == 'WARN' and 'without VIZMCP_TOKEN' in c.detail
	monkeypatch.setenv('VIZMCP_TOKEN', 'x')
	assert doctor.check_exposure().status == 'PASS'


# --- files written by the app on another PC ----------------------------------------------------

def png_bytes(color=(10, 120, 200)):
	import io
	from PIL import Image
	buf = io.BytesIO()
	Image.new('RGB', (32, 24), color).save(buf, 'PNG')
	return buf.getvalue()


def test_fetch_file_sends_images_only_and_within_the_limit(vm, tmp_path):
	import base64
	png = tmp_path / 'shot.png'
	png.write_bytes(png_bytes())
	res = vm.cmd_fetch_file({'path': str(png)})
	assert base64.b64decode(res['data']) == png.read_bytes() and res['bytes'] == png.stat().st_size
	for bad, match in (('secrets.txt', 'only sends'), (str(tmp_path / 'gone.png'), 'no such file'), ('', 'only sends')):
		with pytest.raises(ValueError, match=match):
			vm.cmd_fetch_file({'path': bad})
	(tmp_path / 'big.png').write_bytes(b'x' * 100)
	vm._FETCH_MAX = 10
	with pytest.raises(ValueError, match='limit'):
		vm.cmd_fetch_file({'path': str(tmp_path / 'big.png')})
	assert 'fetch_file' in vm.COMMANDS


def test_screenshot_from_a_remote_app_is_fetched_not_read_from_disk(remote, tmp_path):
	import base64
	data = base64.b64encode(png_bytes()).decode('ascii')

	def handler(req):
		if req['cmd'] == 'screenshot':
			assert 'path' not in req['params']          # the server's path means nothing on the other PC
			return {'ok': True, 'result': {'path': 'C:/Users/expo/AppData/Local/Temp/vizmcp/shot.png', 'frame': 3,
			                               'window_size': [32, 24]}}
		if req['cmd'] == 'fetch_file':
			assert req['params']['path'].endswith('shot.png')
			return {'ok': True, 'result': {'data': data}}
		return info_handler(req)
	remote.handler = handler
	call('connect', {'host': 'exhibition-pc', 'port': remote.port, 'token': 'secret'})
	save = tmp_path / 'kept' / 'copy.png'
	res = run(S.mcp.call_tool('screenshot', {'save_to': str(save)}))
	assert [c.type for c in res.content] == ['image', 'text']
	from PIL import Image
	assert Image.open(save).size == (32, 24)


def test_screenshot_diff_works_against_a_remote_app(remote, tmp_path):
	import base64
	data = base64.b64encode(png_bytes()).decode('ascii')
	remote.handler = lambda req: (
		{'ok': True, 'result': {'path': 'D:/x/shot.png', 'frame': 1, 'window_size': [32, 24]}} if req['cmd'] == 'screenshot'
		else {'ok': True, 'result': {'data': data}} if req['cmd'] == 'fetch_file' else info_handler(req))
	call('connect', {'host': 'exhibition-pc', 'port': remote.port, 'token': 'secret'})
	ref = tmp_path / 'ref.png'
	ref.write_bytes(png_bytes())
	res = run(S.mcp.call_tool('screenshot_diff', {'reference': str(ref), 'threshold_pct': 1}))
	out = json.loads(res.content[1].text)
	assert out['passed'] is True and out['diff_pct'] == 0


def test_videos_are_refused_for_a_remote_app_but_a_dry_run_works(remote):
	call('connect', {'host': 'exhibition-pc', 'port': remote.port, 'token': 'secret'})
	with pytest.raises(ToolError, match='record_video.*this server cannot fetch'):
		run(S.mcp.call_tool('record_video', {}))
	with pytest.raises(ToolError, match='record_tour.*this server cannot fetch'):
		run(S.mcp.call_tool('record_tour', {'orbit': 'ball'}))
	assert not [r for r in remote.requests if r['cmd'] in ('record_video', 'record_tour')]
