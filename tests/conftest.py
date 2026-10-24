import json
import socket
import threading

import pytest

# test_mcp_stdio.py is a script that runs at import; it is covered by test_server.py.
collect_ignore = ['test_mcp_stdio.py']


def free_port():
	s = socket.socket()
	s.bind(('127.0.0.1', 0))
	port = s.getsockname()[1]
	s.close()
	return port


class FakeVizmcp(object):
	"""Speaks the vizmcp protocol (JSON line in, JSON line out) like
	vizard_side/vizmcp.py's _Server, with a pluggable handler."""

	def __init__(self, handler=None, token=None):
		self.handler = handler or self.default_handler
		self.token = token
		self.requests = []
		self.sock = socket.socket()
		self.sock.bind(('127.0.0.1', 0))
		self.sock.listen(4)
		self.port = self.sock.getsockname()[1]
		self.conns = []
		threading.Thread(target=self._accept, daemon=True).start()

	@staticmethod
	def default_handler(req):
		if req['cmd'] == 'ping':
			return {'ok': True, 'result': 'pong'}
		if req['cmd'] == 'boom':
			return {'ok': False, 'error': 'Traceback: boom'}
		return {'ok': True, 'result': {'cmd': req['cmd'], 'params': req['params']}}

	def _accept(self):
		while True:
			try:
				conn, _ = self.sock.accept()
			except OSError:
				return
			self.conns.append(conn)
			threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

	def _serve(self, conn):
		f = conn.makefile('rb')
		try:
			for raw in f:
				if not raw.strip():
					continue
				try:
					req = json.loads(raw.decode('utf-8'))
				except ValueError as e:
					reply = {'id': None, 'ok': False, 'error': 'bad JSON: %s' % e}
				else:
					self.requests.append(req)
					if self.token and req.get('token') != self.token:
						reply = {'id': req.get('id'), 'ok': False, 'error': 'bad or missing token'}
					else:
						reply = self.handler(req)
						if reply is None:        # handler wants silence (timeout test)
							continue
						reply = dict(reply, id=req.get('id'))
				conn.sendall((json.dumps(reply) + '\n').encode('utf-8'))
		except OSError:
			pass

	def drop_clients(self):
		for c in self.conns:
			try:
				c.shutdown(socket.SHUT_RDWR)   # the serving thread's makefile keeps the fd alive
			except OSError:
				pass
			c.close()
		self.conns = []

	def close(self):
		try:
			self.sock.shutdown(socket.SHUT_RDWR)   # wakes accept() so the port really closes
		except OSError:
			pass
		self.sock.close()
		self.drop_clients()


@pytest.fixture
def fake():
	srv = FakeVizmcp()
	yield srv
	srv.close()
