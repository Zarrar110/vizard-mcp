"""Server side of watch_export, screenshot_diff and scene_stats, against a fake vizmcp."""

import asyncio
import json

import pytest

from conftest import FakeVizmcp
from mcp.server.mcpserver.exceptions import ToolError
from vizard_mcp import server as S
from vizard_mcp.client import VizardClient


def run(coro):
	return asyncio.run(coro)


@pytest.fixture
def app(monkeypatch):
	"""Install a fake vizmcp whose handler can be swapped per test."""
	state = {'handler': None}
	fake = FakeVizmcp(lambda req: state['handler'](req) if req['cmd'] != 'ping' else {'ok': True, 'result': 'pong'})
	monkeypatch.setattr(S, 'client', VizardClient(port=fake.port))
	state['fake'] = fake
	yield state
	S.client.close()
	fake.close()


def sent(app, cmd):
	return [r for r in app['fake'].requests if r['cmd'] == cmd]


# --- watch_export -----------------------------------------------------------









# --- scene_stats ------------------------------------------------------------

def test_scene_stats_forwards_top_and_returns_json(app):
	stats = {'nodes': 5, 'visible_nodes': 4, 'top_nodes': []}
	app['handler'] = lambda req: {'ok': True, 'result': stats}
	assert json.loads(run(S.mcp.call_tool('scene_stats', {'top': 3})).content[0].text) == stats
	assert sent(app, 'scene_stats')[0]['params'] == {'top': 3}


def test_scene_stats_default_top_and_validation(app):
	app['handler'] = lambda req: {'ok': True, 'result': {}}
	run(S.mcp.call_tool('scene_stats', {}))
	assert sent(app, 'scene_stats')[0]['params'] == {'top': 10}
	with pytest.raises(ToolError, match='top must be'):
		run(S.mcp.call_tool('scene_stats', {'top': -1}))


# --- screenshot_diff --------------------------------------------------------



























