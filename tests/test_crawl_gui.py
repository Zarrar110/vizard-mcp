"""crawl_gui: the server tool against a simulated app (buttons that change the picture, log lines,
raise exceptions, or do nothing), plus its pure helpers."""

import asyncio
import json
import os

import pytest
from PIL import Image

from mcp.server.mcpserver.exceptions import ToolError
from test_new_tools import app, sent          # noqa: F401  (fixture)
from vizard_mcp import report, server as S


def run(coro):
	return asyncio.run(coro)


def crawl(args=None):
	return json.loads(run(S.mcp.call_tool('crawl_gui', args or {})).content[0].text)


def widget(wid, text, **kw):
	w = {'id': wid, 'names': [], 'type': 'VizButton', 'kind': 'button', 'text': text, 'visible': True,
	     'enabled': True, 'center': [0.5, 0.5], 'size': [0.1, 0.05], 'on_screen': True}
	w.update(kw)
	return w


class SimApp(object):
	"""What the fake vizmcp does: a window whose colour a click can change, a console, a snapshot."""

	def __init__(self, tmp_path, widgets):
		self.tmp = tmp_path
		self.widgets = widgets
		self.color = (30, 60, 90)
		self.base = self.color
		self.lines = []           # (seq, stream, text)
		self.clicked = []
		self.restores = 0
		self.snapshots = {}
		self.actions = {}         # button id -> function(app)
		self.shots = 0

	def log(self, stream, text):
		self.lines.append((len(self.lines) + 1, stream, text))

	def handler(self, req):
		cmd, p = req['cmd'], req['params']
		if cmd == 'ping':
			return {'ok': True, 'result': 'pong'}
		if cmd == 'gui_list':
			ws = [w for w in self.widgets if w['visible']] if p.get('visible_only') else self.widgets
			return {'ok': True, 'result': {'widgets': [w for w in ws if w['kind'] in (p.get('kinds') or [w['kind']])],
			                               'world_widgets': [], 'notes': []}}
		if cmd == 'snapshot_save':
			self.snapshots[p['name']] = {'color': self.color}
			return {'ok': True, 'result': {'name': p['name'], 'nodes': len(p.get('nodes') or [])}}
		if cmd == 'snapshot_restore':
			self.restores += 1
			self.color = self.snapshots[p['name']]['color']
			return {'ok': True, 'result': {'name': p['name'], 'restored': 1, 'missing': [], 'failed': []}}
		if cmd == 'screenshot':
			self.shots += 1
			path = p.get('path') or str(self.tmp / ('auto_%d.png' % self.shots))
			Image.new('RGB', (64, 48), self.color).save(path, 'PNG')
			return {'ok': True, 'result': {'path': path, 'frame': self.shots, 'window_size': [64, 48]}}
		if cmd == 'click_button':
			wid = p['id']
			self.clicked.append(wid)
			if wid not in self.actions:
				return {'ok': False, 'error': 'ValueError: no 2D widget with id %d' % wid}
			self.actions[wid](self)
			return {'ok': True, 'result': {'widget': {'id': wid}, 'click': {}}}
		if cmd == 'wait':
			return {'ok': True, 'result': {'frames': p.get('frames')}}
		if cmd == 'get_log':
			rows = [r for r in self.lines if r[0] > p.get('since', 0)][-p.get('limit', 200):]
			return {'ok': True, 'result': {'last_seq': len(self.lines),
			                               'lines': [{'seq': s, 'stream': st, 'text': t} for s, st, t in rows]}}
		return {'ok': False, 'error': 'unexpected command %s' % cmd}


@pytest.fixture
def sim(app, tmp_path):
	widgets = [widget(1, 'Reset ball'), widget(2, 'Do nothing'), widget(3, 'Crash'), widget(4, 'Quit'),
	           widget(5, 'Hidden one', visible=False), widget(6, 'Disabled', enabled=False), widget(7, 'Close dialog'),
	           widget(8, 'Delete all'), widget(9, None), widget(10, 'Off', on_screen=False),
	           widget(11, 'Slider', kind='slider')]
	s = SimApp(tmp_path, widgets)

	def reset(a):
		a.color = (200, 40, 40)
		a.log('out', 'ball dropped (#2)')

	def crash(a):
		a.log('out', 'before')
		for t in ('Traceback (most recent call last):', '  File "app.py", line 3, in on_click',
		          '    1 / 0', 'ZeroDivisionError: division by zero'):
			a.log('err', t)
	s.actions = {1: reset, 2: lambda a: None, 3: crash, 4: lambda a: pytest.fail('Quit must never be clicked'),
	             7: lambda a: pytest.fail('Close must never be clicked'), 8: lambda a: pytest.fail('Delete must never be clicked')}
	app['handler'] = s.handler
	app['sim'] = s
	return s


ARGS = {'nodes': ['ball'], 'settle_frames': 3}


def test_clicks_safe_buttons_and_reports_changes_errors_and_log(sim, tmp_path):
	res = crawl(dict(ARGS, save_dir=str(tmp_path / 'out')))
	assert sim.clicked == [1, 2, 3]                       # Quit / Close / Delete / hidden / disabled / off-screen / no text: never
	rows = {r['label']: r for r in res['results']}
	assert rows['Reset ball']['changed'] is True and rows['Reset ball']['changed_pct'] == 100.0
	assert rows['Reset ball']['log'] == ['ball dropped (#2)'] and rows['Reset ball']['new_log_lines'] == 1
	assert rows['Do nothing']['changed'] is False and rows['Do nothing']['changed_pct'] == 0 and rows['Do nothing']['new_log_lines'] == 0
	crash = rows['Crash']
	assert len(crash['errors']) == 1 and crash['errors'][0].startswith('Traceback') and 'ZeroDivisionError' in crash['errors'][0]
	assert crash['new_log_lines'] == 5 and crash['changed'] is False
	s = res['summary']
	assert (s['buttons_found'], s['clicked'], s['changed'], s['with_errors'], s['restore_issues']) == (3, 3, 1, 1, 0)


def test_skipped_buttons_say_why(sim):
	res = crawl(ARGS)
	why = {x['label']: x['reason'] for x in res['skipped']}
	assert why['Quit'] == why['Close dialog'] == why['Delete all'] == 'matches the deny pattern'
	assert why['Disabled'] == why['Off'] == 'disabled or off screen'
	assert 'deny list' in why[None]
	assert 'Hidden one' not in why and 'Slider' not in why


def test_each_click_is_undone_from_the_snapshot(sim):
	res = crawl(ARGS)
	assert sim.restores == 3 and sim.color == sim.base
	assert [r['restore']['restored'] for r in res['results']] == [1, 1, 1]
	assert list(sim.snapshots) == ['__crawl_gui__'] and res['snapshot'] == '__crawl_gui__'
	# every button starts from the restored state: the second one sees no leftover red
	assert res['results'][1]['changed_pct'] == 0


def test_snapshot_arguments_are_passed_through(sim, app):
	crawl({'nodes': ['ball', 'crane'], 'named': True, 'pattern': 'LAB', 'root': 'crane', 'max_nodes': 7,
	       'state': 'LAB.sim.state()', 'restore_code': 'LAB.sim.set_state(_state)', 'label': 'reset'})
	p = sent(app, 'snapshot_save')[0]['params']
	assert p == {'name': '__crawl_gui__', 'nodes': ['ball', 'crane'], 'named': True, 'pattern': 'LAB', 'root': 'crane',
	             'max_nodes': 7, 'state': 'LAB.sim.state()', 'restore_code': 'LAB.sim.set_state(_state)'}
	assert len(sent(app, 'snapshot_save')) == 1


def test_restore_needs_something_to_snapshot(sim, app):
	with pytest.raises(ToolError, match='needs something to snapshot'):
		crawl({})
	assert not sent(app, 'click_button')


def test_restore_false_clicks_without_snapshot(sim, app):
	res = crawl({'restore': False})
	assert sim.clicked == [1, 2, 3] and not sent(app, 'snapshot_save') and not sent(app, 'snapshot_restore')
	assert res['snapshot'] is None and res['results'][0]['restore'] is None
	assert sim.color != sim.base and res['results'][1]['changed'] is False       # the red from button 1 stays


def test_label_filter_max_and_deny_override(sim):
	res = crawl(dict(ARGS, label='^(reset|crash)'))
	assert [r['label'] for r in res['results']] == ['Reset ball', 'Crash']
	sim.clicked.clear()
	res = crawl(dict(ARGS, max_buttons=1))
	assert sim.clicked == [1] and {'id': 2, 'label': 'Do nothing', 'reason': 'over max_buttons'} in res['skipped']
	assert res['summary']['buttons_found'] == 3
	# deny can be changed: with a custom pattern "Quit" is clicked, "Crash" is not
	sim.actions[4] = lambda a: None
	sim.clicked.clear()
	crawl(dict(ARGS, deny='crash', label='quit|crash'))
	assert sim.clicked == [4]
	# and an explicit label does not bypass the default deny list
	sim.clicked.clear()
	res = crawl(dict(ARGS, label='quit'))
	assert sim.clicked == [] and res['results'] == []


def test_a_click_that_fails_is_recorded_and_the_crawl_goes_on(sim):
	del sim.actions[2]
	res = crawl(ARGS)
	row = res['results'][1]
	assert row['clicked'] is False and 'no 2D widget' in row['click_error'] and row['changed'] is None
	assert sim.clicked == [1, 2, 3] and res['summary']['clicked'] == 2 and res['summary']['with_errors'] == 2
	assert res['table']['rows'][1][2] == 'not clicked'


def test_table_text_and_artifacts(sim, tmp_path):
	out = tmp_path / 'shots'
	res = crawl(dict(ARGS, save_dir=str(out)))
	assert res['table']['columns'] == ['Button', 'Changed %', 'Changed', 'Errors', 'New log lines']
	assert res['table']['rows'][0] == ['Reset ball', 100.0, 'yes', 0, 1]
	assert res['table']['rows'][2] == ['Crash', 0.0, 'no', 1, 5]
	assert res['text_table'].splitlines()[0].split() == ['Button', 'Changed', '%', 'Changed', 'Errors', 'New', 'log', 'lines']
	assert res['output_dir'] == str(out)
	assert sorted(os.listdir(out)) == ['01_before.png', '01_diff.png', '02_before.png', '02_diff.png', '03_before.png', '03_diff.png']


def test_change_threshold_and_pixel_tolerance(sim):
	sim.actions[1] = lambda a: setattr(a, 'color', (32, 62, 92))           # +2 per channel
	assert crawl(dict(ARGS, label='reset'))['results'][0]['changed'] is False             # inside the tolerance of 8
	assert crawl(dict(ARGS, label='reset', pixel_tolerance=0))['results'][0]['changed'] is True
	assert crawl(dict(ARGS, label='reset', pixel_tolerance=0, change_threshold_pct=100))['results'][0]['changed'] is False


def test_no_buttons_means_no_snapshot_and_an_empty_table(app, tmp_path):
	app['handler'] = SimApp(tmp_path, []).handler
	res = crawl(ARGS)
	assert res['results'] == [] and res['summary']['buttons_found'] == 0 and not sent(app, 'snapshot_save')


def test_argument_checks(sim, app):
	for bad in ({'max_buttons': 0}, {'settle_frames': -1}, {'pixel_tolerance': 300}, {'change_threshold_pct': -1},
	            {'label': '('}, {'deny': '['}):
		with pytest.raises(ToolError):
			crawl(dict(ARGS, **bad))
	assert not sent(app, 'click_button')


def test_log_exceptions_helper():
	def err(*lines):
		return [{'stream': 'err', 'text': t} for t in lines]
	tb = err('Traceback (most recent call last):', '  File "a.py", line 1', 'KeyError: 3')
	assert S._log_exceptions(tb) == ['Traceback (most recent call last):\n  File "a.py", line 1\nKeyError: 3']
	two = tb + [{'stream': 'out', 'text': 'fine'}] + tb
	assert len(S._log_exceptions(two)) == 2
	assert S._log_exceptions(err('ValueError: bare', 'warning: something')) == ['ValueError: bare']
	assert S._log_exceptions([{'stream': 'out', 'text': 'ValueError: only printed'}]) == []
	assert len(S._log_exceptions(err('Traceback (most recent call last):', '  File "x"'))) == 1          # cut off
	assert S._log_exceptions([]) == []


def test_html_fragment_and_make_report_section(sim, app, tmp_path):
	res = crawl(dict(ARGS, html=True))
	assert 'Reset ball' in res['html_section'] and 'ZeroDivisionError' in res['html_section'] and 'Quit' in res['html_section']
	app['handler'] = lambda req: sim.handler(req) if req['cmd'] != 'info' else {'ok': True, 'result': {}}
	out = tmp_path / 'r.html'
	run(S.mcp.call_tool('make_report', {'path': str(out), 'sections': [{'type': 'crawl_gui', 'caption': 'GUI smoke test'}]}))
	html = out.read_text(encoding='utf-8')
	assert 'Reset ball' in html and 'Python exceptions' in html
	n = len(sent(app, 'click_button'))
	run(S.mcp.call_tool('make_report', {'path': str(out), 'sections': [{'type': 'crawl_gui', 'result': res}]}))
	assert len(sent(app, 'click_button')) == n                  # a report never clicks


def test_make_report_crawl_section_needs_a_result(monkeypatch):
	monkeypatch.setattr(S, '_last', {})
	with pytest.raises(ToolError, match='run the crawl_gui tool first'):
		run(S.mcp.call_tool('make_report', {'sections': [{'type': 'crawl_gui'}]}))
