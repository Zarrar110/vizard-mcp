"""make_report: the HTML building blocks, and the tool against a fake vizmcp."""

import asyncio
import base64
import io
import json
import re

import pytest
from PIL import Image

from mcp.server.mcpserver.exceptions import ToolError
from test_gui_vr_validate import validate_result
from test_new_tools import app, sent          # noqa: F401  (fixture)
from vizard_mcp import report, server as S


def run(coro):
	return asyncio.run(coro)


def tool(name, args):
	return json.loads(run(S.mcp.call_tool(name, args)).content[0].text)


# --- text ------------------------------------------------------------------------------

def test_markdown_paragraphs_lists_headings_and_inline_styles():
	html = report.markdown('# Setup\n\nFirst **bold** and *soft* with `code`.\nSame paragraph.\n\n'
	                       '- one\n- two\n\n1. a\n2) b\n\nSee [docs](https://example.org/x?a=1&b=2).')
	assert '<h3>Setup</h3>' in html
	assert '<p>First <strong>bold</strong> and <em>soft</em> with <code>code</code>. Same paragraph.</p>' in html
	assert '<ul><li>one</li><li>two</li></ul>' in html and '<ol><li>a</li><li>b</li></ol>' in html
	assert '<a href="https://example.org/x?a=1&amp;b=2">docs</a>' in html


def test_markdown_escapes_html_and_refuses_script_links():
	html = report.markdown('<script>alert(1)</script> & "q"\n\n[x](javascript:alert(1)) `<b>`')
	assert '<script>' not in html and '&lt;script&gt;' in html and '&amp;' in html
	assert '<a ' not in html and '<code>&lt;b&gt;</code>' in html


def test_markdown_does_not_style_inside_code_and_leaves_stray_stars():
	html = report.markdown('`a *b* c` and 2 * 3 * 4 and snake_case_name')
	assert '<code>a *b* c</code>' in html and '2 * 3 * 4' in html and '<em>' not in html


def test_markdown_empty_text_makes_no_paragraphs():
	assert report.markdown('  \n\n  ') == ''


# --- pieces ----------------------------------------------------------------------------

def test_badge_states():
	assert 'badge pass">PASS' in report.badge(True) and 'badge fail">FAIL' in report.badge(False)
	assert 'badge none">NO VERDICT' in report.badge(None)


def test_figure_inlines_the_image_as_a_data_uri():
	html = report.figure(b'\x89PNGdata', 'image/png', 'A *chart*')
	m = re.search(r'src="data:image/png;base64,([^"]+)"', html)
	assert base64.b64decode(m.group(1)) == b'\x89PNGdata'
	assert '<figcaption>A <em>chart</em></figcaption>' in html


def test_tables_escape_cells_and_keyvalue_accepts_dict_or_pairs():
	assert '&lt;b&gt;' in report.keyvalue({'k': '<b>'}) and '<b>' not in report.keyvalue({'k': '<b>'})
	assert report.keyvalue([('a', 1), ('b', 2.5)]) == report.keyvalue({'a': 1, 'b': 2.5})
	t = report.table(['Col', 'n'], [['x', 3]], 'cap')
	assert '<th>Col</th><th class="num">n</th>' in t and '<td class="num">3</td>' in t


def test_fmt():
	assert report.fmt(None) == '-' and report.fmt(True) == 'yes' and report.fmt(0.1 + 0.2) == '0.3'
	assert report.fmt(1234567.0) == '1.23457e+06' and report.fmt([1, 2.5]) == '1, 2.5'


def test_document_is_self_contained_and_printable():
	doc = report.document('T <1>', 'Zarrar Abbas', '2026-01-02', [('Vizard', '8.3.1')], ['<h2>x</h2>'])
	assert doc.startswith('<!DOCTYPE html>') and '<title>T &lt;1&gt;</title>' in doc
	assert 'Zarrar Abbas &middot; 2026-01-02' in doc and '8.3.1' in doc
	assert '@media print' in doc and '@page' in doc and 'break-inside: avoid' in doc
	assert not re.search(r'(src|href)="(?!data:)https?://', doc) and '<link' not in doc and '<script' not in doc


# --- the tool ------------------------------------------------------------------------

def png(path, color=(30, 90, 160), size=(60, 40)):
	Image.new('RGB', size, color).save(path)
	return str(path)


def watch_result(**extra):
	cols = ['t', 'frame', 'h', 'v']
	samples = [[i / 10.0, i, 2.0 - i * 0.1, -i * 0.5] for i in range(12)]
	return dict({'name': 'bounce', 'status': {'active': False, 'samples': 12, 'stop_reason': 'until met'},
	             'columns': cols, 'expressions': {'h': 'x'}, 'samples': samples, 'csv': '/tmp/w.csv',
	             'stats': {'h': {'n': 12, 'first': 2.0, 'last': 0.9, 'min': 0.9, 't_min': 1.1, 'max': 2.0, 't_max': 0.0, 'mean': 1.45},
	                       'v': {'n': 12, 'first': 0.0, 'last': -5.5, 'min': -5.5, 't_min': 1.1, 'max': 0.0, 't_max': 0.0, 'mean': -2.75}}},
	            **extra)


@pytest.fixture
def report_app(app, tmp_path):
	shot = png(tmp_path / 'shot.png', size=(320, 200))
	info = {'vizmcp_version': '1.0.0', 'vizard_version': '8.3.1', 'python': '3.8.2', 'script': 'C:/labs/demo_scene.py',
	        'window_size': [800, 600], 'fps': 58.26, 'node_count': 41}

	def handler(req):
		cmd = req['cmd']
		if cmd == 'info':
			return {'ok': True, 'result': info}
		if cmd == 'watch_read':
			return {'ok': True, 'result': watch_result()}
		if cmd == 'validate':
			return {'ok': True, 'result': validate_result(
				watch='bounce', measured='h', theory='h0 - v**2/(2*g)', constants={'h0': 2.0, 'g': 9.81}, skipped=0,
				mean_abs_error=0.004, rms_error=0.005, bias=0.002, max_pct_error=1.2, mean_pct_error=0.5, t_of_max_error=0.7,
				abs_tol=0.02, pct_tol=None, passing_samples=20, failing_samples=0, pass_pct=100.0, min_pass_pct=100.0)}
		if cmd == 'screenshot':
			return {'ok': True, 'result': {'path': shot, 'frame': 5, 'window_size': [320, 200]}}
		raise AssertionError('unexpected command %s' % cmd)
	app['handler'] = handler
	app['tmp'] = tmp_path
	app['info'] = info
	return app


def read(res):
	with open(res['path'], encoding='utf-8') as f:
		return f.read()


FULL = [
	{'type': 'heading', 'text': 'Drop test <A>', 'level': 2},
	{'type': 'text', 'text': 'The ball was dropped from **2 m**.\n\n- bounce 1\n- bounce 2'},
	{'type': 'watch', 'name': 'bounce', 'columns': ['h']},
	{'type': 'validate', 'watch': 'bounce', 'measured': 'h', 'theory': 'h0 - v**2/(2*g)',
	 'constants': {'h0': 2.0, 'g': 9.81}, 'abs_tol': 0.02},
	{'type': 'screenshot', 'caption': 'End state'},
	{'type': 'keyvalue', 'items': {'Restitution': 0.8, 'Drop height': '2 m'}},
]


def test_full_report_has_every_section_inline_and_the_header(report_app):
	out = report_app['tmp'] / 'rep' / 'r.html'
	res = tool('make_report', {'sections': FULL, 'path': str(out), 'title': 'Drop test', 'date': '2026-05-06'})
	html = read(res)
	assert res['path'] == str(out) and res['bytes'] == len(html.encode()) and res['sections'] == 6 and res['images'] == 3
	assert html.startswith('<!DOCTYPE html>') and '<title>Drop test</title>' in html
	assert 'Zarrar Abbas &middot; 2026-05-06' in html                      # default author
	for fact in ('8.3.1', 'C:/labs/demo_scene.py', '800x600', '58.3 fps', '>41<'):
		assert fact in html
	assert '<h2>Drop test &lt;A&gt;</h2>' in html and '<strong>2 m</strong>' in html and '<li>bounce 2</li>' in html
	assert 'badge pass">PASS' in html and '20 of 20 samples within tolerance' in html
	assert 'h0 - v**2/(2*g)' in html and 'h0 = 2, g = 9.81' in html and '0.02 absolute' in html
	assert '<th>Restitution</th><td>0.8</td>' in html and 'End state' in html
	imgs = re.findall(r'src="data:image/(png|jpeg);base64,([^"]+)"', html)
	assert sorted(k for k, _ in imgs) == ['jpeg', 'png', 'png']                # screenshot as JPEG, two charts as PNG
	for kind, data in imgs:
		assert Image.open(io.BytesIO(base64.b64decode(data))).size[0] > 100
	assert not re.search(r'(src|href)="(?!data:)', html.replace('href="https', ''))
	assert res['validations'] == [{'watch': 'bounce', 'measured': 'h', 'passed': True, 'samples': 20,
	                               'max_abs_error': 0.01, 'section': 4}]
	assert res['all_validations_passed'] is True


def test_watch_section_summarises_columns_and_skips_time_columns(report_app):
	res = tool('make_report', {'sections': [{'type': 'watch', 'name': 'bounce', 'columns': ['t', 'h', 'v'],
	                                         'caption': 'My caption'}], 'path': str(report_app['tmp'] / 'w.html')})
	html = read(res)
	assert 'My caption' in html and '<td>h</td>' in html and '<td>v</td>' in html and '<td>t</td>' not in html
	assert '0.9 (t=1.1)' in html and '<td class="num">1.45</td>' in html
	assert sent(report_app, 'watch_read')[0]['params'] == {'name': 'bounce', 'max_points': 1500}


def test_watch_section_default_caption_and_all_columns(report_app):
	html = read(tool('make_report', {'sections': [{'type': 'watch', 'name': 'bounce'}], 'path': str(report_app['tmp'] / 'w.html')}))
	assert 'Watch &quot;bounce&quot;: 12 samples, stopped (until met).' in html and '<td>h</td>' in html and '<td>v</td>' in html


def test_validate_section_forwards_its_arguments(report_app):
	tool('make_report', {'sections': [dict(FULL[3], pct_tol=2, t_min=0.1, t_max=1.5, min_pass_pct=90)],
	                     'path': str(report_app['tmp'] / 'v.html')})
	p = sent(report_app, 'validate')[0]['params']
	assert p['watch'] == 'bounce' and p['constants'] == {'h0': 2.0, 'g': 9.81} and p['abs_tol'] == 0.02 and p['pct_tol'] == 2
	assert p['t_min'] == 0.1 and p['t_max'] == 1.5 and p['min_pass_pct'] == 90


@pytest.mark.parametrize('verdict, badge', [(False, 'badge fail">FAIL'), (None, 'badge none">NO VERDICT')])
def test_validate_failure_and_missing_verdict_badges(report_app, verdict, badge):
	inner = report_app['handler']

	def handler(req):
		res = inner(req)
		if req['cmd'] == 'validate':
			r = res['result']
			if verdict is None:
				for k in ('passed', 'passing_samples', 'pass_pct', 'abs_tol', 'pct_tol'):
					r.pop(k, None)
			else:
				r.update(passed=False, passing_samples=17, pass_pct=85.0, min_pass_pct=100.0,
				         first_failure={'t': 1.5, 'measured': 1.0, 'theory': 0.9})
		return res
	report_app['handler'] = handler
	res = tool('make_report', {'sections': [FULL[3]], 'path': str(report_app['tmp'] / 'f.html')})
	html = read(res)
	assert badge in html and res['validations'][0]['passed'] is verdict
	if verdict is False:
		assert '17 of 20 samples within tolerance' in html and 'First failure' in html and res['all_validations_passed'] is False
	else:
		assert 'no pass/fail verdict' in html and res['all_validations_passed'] is None


def test_image_section_embeds_a_png_and_a_jpeg_and_shrinks_big_ones(report_app):
	tmp = report_app['tmp']
	big = png(tmp / 'big.png', size=(900, 600))
	jpg = tmp / 'photo.jpg'
	Image.new('RGB', (80, 50), (200, 100, 0)).save(str(jpg))
	html = read(tool('make_report', {'sections': [{'type': 'image', 'path': big, 'caption': 'Big one'},
	                                              {'type': 'image', 'path': str(jpg)}],
	                                 'max_image_size': 300, 'path': str(tmp / 'i.html')}))
	imgs = re.findall(r'src="data:image/(png|jpeg);base64,([^"]+)"', html)
	assert [k for k, _ in imgs] == ['png', 'jpeg']
	assert max(Image.open(io.BytesIO(base64.b64decode(imgs[0][1]))).size) == 300
	assert 'Big one' in html and '<figcaption>photo.jpg</figcaption>' in html
	assert not any(r['cmd'] != 'info' for r in report_app['fake'].requests)    # no app screenshot was taken


def test_screenshot_section_uses_annotate_and_label_limit(report_app):
	inner = report_app['handler']

	def handler(req):
		if req['cmd'] == 'screen_labels':
			return {'ok': True, 'result': {'items': [{'name': 'ball', 'screen': [0.5, 0.5]}], 'total_on_screen': 1}}
		return inner(req)
	report_app['handler'] = handler
	tool('make_report', {'sections': [{'type': 'screenshot', 'annotate': True, 'label_limit': 7}],
	                     'path': str(report_app['tmp'] / 's.html')})
	assert sent(report_app, 'screen_labels')[0]['params'] == {'limit': 7}


def test_report_without_the_app_still_builds_static_sections(app, tmp_path):
	app['handler'] = lambda req: {'ok': False, 'error': 'nope'}
	res = tool('make_report', {'sections': [{'type': 'text', 'text': 'Hello'}, {'type': 'table', 'items': [['a', 'b']]}],
	                           'path': str(tmp_path / 'x.html'), 'author': 'Someone Else', 'title': 'Static'})
	html = read(res)
	assert 'Hello' in html and 'Someone Else' in html and 'not connected when the report was made' in html
	assert res['images'] == 0 and res['validations'] == [] and 'all_validations_passed' not in res


def test_default_path_is_a_new_html_file_in_the_temp_folder(app, tmp_path, monkeypatch):
	import tempfile
	monkeypatch.setattr(tempfile, 'tempdir', str(tmp_path))
	app['handler'] = lambda req: {'ok': False, 'error': 'nope'}
	res = tool('make_report', {'sections': [{'type': 'text', 'text': 'x'}]})
	assert res['path'].startswith(str(tmp_path)) and res['path'].endswith('.html') and read(res)


def test_default_date_is_today(app, tmp_path):
	import time
	app['handler'] = lambda req: {'ok': False, 'error': 'nope'}
	html = read(tool('make_report', {'sections': [{'type': 'text', 'text': 'x'}], 'path': str(tmp_path / 'd.html')}))
	assert time.strftime('%Y-%m-%d') in html


def test_a_failing_section_stops_the_report_and_writes_nothing(report_app):
	inner = report_app['handler']
	report_app['handler'] = lambda req: ({'ok': False, 'error': "ValueError: no watch named 'bounce' (have: none)"}
	                                     if req['cmd'] == 'watch_read' else inner(req))
	out = report_app['tmp'] / 'no.html'
	with pytest.raises(ToolError, match=r"(?s)Report not written\. Section 3 \(watch\).*no watch named 'bounce'"):
		run(S.mcp.call_tool('make_report', {'sections': FULL, 'path': str(out)}))
	assert not out.exists()


@pytest.mark.parametrize('sections, msg', [
	([], 'sections is empty'),
	([{'text': 'x'}], 'section 1 must be an object'),
	([{'type': 'chart'}], "section 1: unknown type 'chart'"),
	([{'type': 'text', 'text': 'ok'}, {'type': 'heading'}], r'section 2 \(heading\) needs text'),
	([{'type': 'watch'}], r'section 1 \(watch\) needs name'),
	([{'type': 'validate', 'watch': 'w', 'measured': 'h'}], 'needs theory'),
	([{'type': 'text', 'text': 'x', 'colour': 'red'}], r'section 1 \(text\): unknown colour'),
	([{'type': 'image', 'path': '/no/such/file.png'}], 'no such file'),
	([{'type': 'keyvalue', 'items': 'a=b'}], 'items must be an object'),
	([{'type': 'keyvalue', 'items': [['a']]}], 'items must be an object')])
def test_argument_errors_are_found_before_the_app_is_asked_anything(app, tmp_path, sections, msg):
	with pytest.raises(ToolError, match=msg):
		run(S.mcp.call_tool('make_report', {'sections': sections, 'path': str(tmp_path / 'e.html')}))
	assert app['fake'].requests == [] or all(r['cmd'] == 'ping' for r in app['fake'].requests)
	assert not (tmp_path / 'e.html').exists()


def test_max_image_size_is_checked(app, tmp_path):
	with pytest.raises(ToolError, match='max_image_size'):
		run(S.mcp.call_tool('make_report', {'sections': [{'type': 'text', 'text': 'x'}], 'max_image_size': 4}))
