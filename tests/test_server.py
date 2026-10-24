import asyncio
import json
import sys

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from conftest import FakeVizmcp, free_port
from mcp.server.mcpserver.exceptions import ToolError
from vizard_mcp import server as S
from vizard_mcp.client import VizardClient

EXPECTED = {
	'vizard_status', 'launch_app', 'quit_app', 'reload_module', 'scene_tree', 'find_nodes', 'node_info',
	'set_node', 'run_code', 'get_log', 'vizconnect_info', 'vizard_api', 'screenshot', 'multi_view',
	'filmstrip', 'record_video', 'pick', 'project', 'camera', 'send_key', 'click', 'wait', 'wait_until',
	'watch_start', 'watch_read', 'watch_plot', 'profile', 'call_command', 'watch_export',
	'screenshot_diff', 'scene_stats', 'gui_list', 'click_button', 'vr_simulate', 'validate',
	'record_tour', 'doctor', 'snapshot_save', 'snapshot_restore', 'snapshot_list', 'make_report',
	'connect', 'asset_check', 'crawl_gui'}


def run(coro):
	return asyncio.run(coro)


@pytest.fixture
def no_app(monkeypatch):
	monkeypatch.setattr(S, 'client', VizardClient(port=free_port()))
	monkeypatch.setitem(S._launched, 'proc', None)


def test_all_44_tools_register_with_valid_schemas():
	tools = run(S.mcp.list_tools())
	assert len(tools) == 44
	assert {t.name for t in tools} == EXPECTED
	for t in tools:
		assert t.description, t.name
		schema = t.input_schema
		assert schema['type'] == 'object', t.name
		props = schema.get('properties', {})
		assert set(schema.get('required', [])) <= set(props), t.name


def test_required_parameters():
	req = {t.name: set(t.input_schema.get('required', [])) for t in run(S.mcp.list_tools())}
	assert req['launch_app'] == {'script'}
	assert req['watch_start'] == {'expressions'}
	assert req['pick'] == {'x', 'y'}
	assert req['vizard_status'] == set()
	assert req['watch_export'] == {'name', 'path'}
	assert req['screenshot_diff'] == {'reference'}
	assert req['scene_stats'] == set()


def test_tool_call_without_app_returns_clean_error(no_app):
	with pytest.raises(ToolError, match='No Vizard app is listening'):
		run(S.mcp.call_tool('scene_tree', {}))


def test_vizard_status_without_app(no_app):
	res = run(S.mcp.call_tool('vizard_status', {}))
	assert not res.is_error
	assert json.loads(res.content[0].text) == {'connected': False, 'host': '127.0.0.1', 'port': S.client.port}


def test_quit_app_without_app(no_app):
	res = run(S.mcp.call_tool('quit_app', {}))
	assert res.content[0].text == 'No app was running.'


def test_invalid_arguments_are_errors(no_app):
	with pytest.raises(Exception):
		run(S.mcp.call_tool('pick', {'x': 'left'}))


def test_tools_forward_to_fake_vizard(monkeypatch):
	def handler(req):
		if req['cmd'] == 'ping':
			return {'ok': True, 'result': 'pong'}
		if req['cmd'] == 'info':
			return {'ok': True, 'result': {'script': 'demo.py', 'frame': 7}}
		if req['cmd'] == 'run_code':
			return {'ok': False, 'error': 'NameError: x'}
		return {'ok': True, 'result': req}
	fake = FakeVizmcp(handler)
	monkeypatch.setattr(S, 'client', VizardClient(port=fake.port))
	try:
		st = json.loads(run(S.mcp.call_tool('vizard_status', {})).content[0].text)
		assert st['connected'] is True and st['frame'] == 7
		with pytest.raises(ToolError, match='Vizard reported an error:\nNameError: x'):
			run(S.mcp.call_tool('run_code', {'code': 'x'}))
		run(S.mcp.call_tool('scene_tree', {'depth': 1}))
		sent = [r for r in fake.requests if r['cmd'] == 'scene_tree'][0]
		assert sent['params'] == {'depth': 1, 'max_nodes': 400, 'only_named': False}     # None values dropped
	finally:
		S.client.close()
		fake.close()


def test_vizard_api_without_vizard_is_clean_error(monkeypatch):
	monkeypatch.delenv('VIZARD_EXE', raising=False)
	monkeypatch.setenv('ProgramFiles', '/nonexistent')
	monkeypatch.setenv('ProgramFiles(x86)', '/nonexistent')
	with pytest.raises(ToolError, match='winviz.exe not found'):
		run(S.mcp.call_tool('vizard_api', {'query': 'x'}))


def test_launch_app_missing_script(tmp_path):
	with pytest.raises(ToolError, match='No such script'):
		run(S.mcp.call_tool('launch_app', {'script': str(tmp_path / 'nope.py')}))


def test_find_winviz_env_override(tmp_path, monkeypatch):
	exe = tmp_path / 'winviz.exe'
	exe.write_text('')
	monkeypatch.setenv('VIZARD_EXE', str(exe))
	assert S.find_winviz() == str(exe)


def test_stdio_handshake_lists_tools_and_status():
	async def go():
		params = StdioServerParameters(command=sys.executable, args=['-m', 'vizard_mcp'],
		                               env={'VIZMCP_PORT': str(free_port())})
		async with stdio_client(params) as (r, w):
			async with ClientSession(r, w) as s:
				await s.initialize()
				names = {t.name for t in (await s.list_tools()).tools}
				status = await s.call_tool('vizard_status', {})
				return names, status
	names, status = run(go())
	assert names == EXPECTED
	assert json.loads(status.content[0].text)['connected'] is False


def test_error_message_reaches_the_client_over_stdio():
	"""Regression: a plain RuntimeError is replaced by a generic message on the wire."""
	async def go():
		params = StdioServerParameters(command=sys.executable, args=['-m', 'vizard_mcp'],
		                               env={'VIZMCP_PORT': str(free_port())})
		async with stdio_client(params) as (r, w):
			async with ClientSession(r, w) as s:
				await s.initialize()
				return await s.call_tool('scene_tree', {})
	res = run(go())
	assert res.is_error
	assert 'No Vizard app is listening' in res.content[0].text


def test_version_flag(capsys, monkeypatch):
	monkeypatch.setattr(sys, 'argv', ['vizard-mcp', '--version'])
	S.main()
	assert capsys.readouterr().out.strip() == 'vizard-mcp 1.0.0'


def test_tool_errors_are_runtime_errors_for_direct_callers(no_app):
	with pytest.raises(RuntimeError, match='No Vizard app is listening'):
		S.scene_tree()
