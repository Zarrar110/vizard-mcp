"""TCP client for the vizmcp module running inside Vizard."""

import itertools
import json
import os
import socket
import threading

DEFAULT_HOST = '127.0.0.1'
DEFAULT_PORT = 9877


class VizardError(RuntimeError):
	"""The command reached Vizard but failed there."""


class NotConnected(RuntimeError):
	"""No Vizard app with vizmcp is listening."""


class VizardClient(object):
	def __init__(self, host=None, port=None, token=None, timeout=90.0):
		self.host = host or os.environ.get('VIZMCP_HOST', DEFAULT_HOST)
		self.port = int(port or os.environ.get('VIZMCP_PORT', DEFAULT_PORT))
		self.token = token or os.environ.get('VIZMCP_TOKEN') or None
		self.timeout = timeout
		self._sock = None
		self._file = None
		self._ids = itertools.count(1)
		self._lock = threading.Lock()

	def close(self):
		for x in (self._file, self._sock):
			try:
				if x is not None:
					x.close()
			except OSError:
				pass
		self._sock = self._file = None

	def _connect(self):
		try:
			s = socket.create_connection((self.host, self.port), timeout=3.0)
		except OSError as e:
			raise NotConnected(
				'No Vizard app is listening on %s:%d (%s). Start one with the launch_app tool, '
				'or add "import vizmcp; vizmcp.start()" to the script.' % (self.host, self.port, e))
		s.settimeout(self.timeout)
		self._sock = s
		self._file = s.makefile('rb')

	def call(self, cmd, timeout=None, **params):
		"""Send one command and return its result. Reconnects once if the
		previous app went away."""
		with self._lock:
			for attempt in (0, 1):
				if self._sock is None:
					self._connect()
				req = {'id': next(self._ids), 'cmd': cmd, 'params': params}
				if self.token:
					req['token'] = self.token
				try:
					self._sock.settimeout(timeout or self.timeout)
					self._sock.sendall((json.dumps(req) + '\n').encode('utf-8'))
					line = self._file.readline()
				except (OSError, socket.timeout) as e:
					self.close()
					if attempt == 0 and not isinstance(e, socket.timeout):
						continue
					raise NotConnected('Lost connection to Vizard during %r: %s' % (cmd, e))
				if not line:
					self.close()
					if attempt == 0:
						continue
					raise NotConnected('Vizard closed the connection during %r' % cmd)
				reply = json.loads(line.decode('utf-8'))
				if not reply.get('ok'):
					raise VizardError(reply.get('error', 'unknown error'))
				return reply.get('result')

	def ping(self):
		try:
			self.call('ping', timeout=3.0)
			return True
		except (NotConnected, VizardError):
			self.close()
			return False
