import threading

import pytest

from conftest import FakeVizmcp, free_port
from vizard_mcp.client import DEFAULT_PORT, NotConnected, VizardClient, VizardError


def make(fake, **kw):
	c = VizardClient(port=fake.port, **kw)
	return c


def test_defaults(monkeypatch):
	for k in ('VIZMCP_HOST', 'VIZMCP_PORT', 'VIZMCP_TOKEN'):
		monkeypatch.delenv(k, raising=False)
	c = VizardClient()
	assert (c.host, c.port, c.token) == ('127.0.0.1', 9877, None)
	assert DEFAULT_PORT == 9877


def test_env_settings(monkeypatch):
	monkeypatch.setenv('VIZMCP_PORT', '1234')
	monkeypatch.setenv('VIZMCP_TOKEN', 'abc')
	c = VizardClient()
	assert (c.port, c.token) == (1234, 'abc')


def test_success_and_request_shape(fake):
	c = make(fake)
	assert c.call('scene_tree', depth=2, root=None) == {'cmd': 'scene_tree',
	                                                    'params': {'depth': 2, 'root': None}}
	req = fake.requests[0]
	assert req['cmd'] == 'scene_tree' and req['params'] == {'depth': 2, 'root': None}
	assert 'token' not in req
	c.call('x')
	assert fake.requests[1]['id'] == req['id'] + 1
	c.close()


def test_ping(fake):
	c = make(fake)
	assert c.ping() is True
	c.close()


def test_error_reply_raises_vizard_error(fake):
	c = make(fake)
	with pytest.raises(VizardError, match='boom'):
		c.call('boom')
	assert c.call('ok_after_error')['cmd'] == 'ok_after_error'     # connection still usable
	c.close()


def test_error_reply_without_message():
	srv = FakeVizmcp(lambda req: {'ok': False})
	try:
		with pytest.raises(VizardError, match='unknown error'):
			VizardClient(port=srv.port).call('x')
	finally:
		srv.close()


def test_token_sent_and_checked():
	srv = FakeVizmcp(token='secret')
	try:
		assert VizardClient(port=srv.port, token='secret').call('x')['cmd'] == 'x'
		with pytest.raises(VizardError, match='token'):
			VizardClient(port=srv.port, token='wrong').call('x')
		with pytest.raises(VizardError, match='token'):
			VizardClient(port=srv.port).call('x')
	finally:
		srv.close()


def test_connection_refused():
	c = VizardClient(port=free_port())
	with pytest.raises(NotConnected, match='No Vizard app is listening'):
		c.call('info')
	assert c.ping() is False


def test_timeout():
	srv = FakeVizmcp(lambda req: None)
	try:
		c = VizardClient(port=srv.port)
		with pytest.raises(NotConnected, match='Lost connection'):
			c.call('info', timeout=0.3)
		assert c._sock is None            # dropped, so next call reconnects
		assert len(srv.requests) == 1     # a timeout is not retried
	finally:
		srv.close()


def test_reconnects_once_after_app_restart(fake):
	c = make(fake)
	c.call('a')
	fake.drop_clients()
	assert c.call('b')['cmd'] == 'b'
	c.close()


def test_server_closing_without_reply_raises_after_retry():
	srv = FakeVizmcp(lambda req: None)
	try:
		c = VizardClient(port=srv.port)
		threading.Timer(0.2, srv.drop_clients).start()
		with pytest.raises(NotConnected):
			c.call('x', timeout=5)
	finally:
		srv.close()


def test_app_gone_for_good(fake):
	c = make(fake)
	c.call('a')
	fake.close()
	with pytest.raises(NotConnected):
		c.call('b')
	assert c.ping() is False
