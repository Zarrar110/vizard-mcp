"""End-to-end test against real Vizard: launches examples/demo_scene.py through
the MCP tool functions, exercises every tool, checks the physics the demo
simulates, and quits the app.

    python tests/live_test.py

Needs a real Vizard install. Under pytest it is skipped unless VIZMCP_LIVE=1.
"""

import csv
import json
import os
import sys

if __name__ != '__main__' and os.environ.get('VIZMCP_LIVE') != '1':
	import pytest
	pytest.skip('needs a real Vizard install; set VIZMCP_LIVE=1 to run', allow_module_level=True)

from vizard_mcp import server as S

HERE = os.path.dirname(os.path.abspath(__file__))
DEMO = os.path.join(HERE, '..', 'examples', 'demo_scene.py')
OUT = os.path.join(HERE, 'out')
failures = []


def check(name, cond, detail=''):
	print('%s %s %s' % ('PASS' if cond else 'FAIL', name, str(detail)[:300]))
	if not cond:
		failures.append(name)


def j(s):
	return json.loads(s)


def info(name, detail=''):
	print('INFO %s %s' % (name, str(detail)[:300]))


def button_center():
	"""Where the demo's Reset button is: centre as 0..1 from the top-left of the window,
	asked of the app itself (Vizard's 2D coordinates have their origin bottom-left)."""
	rc = j(S.run_code('c = reset_button.getBoundingBox().center\n'
	                  '[c[0], 1.0 - c[1]]'))
	if rc.get('error'):
		return None, rc['error'].strip().splitlines()[-1]
	return json.loads(rc['value'].replace("'", '"')), None


def save(img, name):
	with open(os.path.join(OUT, name), 'wb') as f:
		f.write(img.data)


os.makedirs(OUT, exist_ok=True)

# --- offline ---------------------------------------------------------------
api = S.vizard_api('getBoundingBox')
check('vizard_api search', 'viz.VizNode.getBoundingBox(self, mode=ABS_PARENT' in api, api.splitlines()[0])
api = S.vizard_api(name='vizact.onupdate')
check('vizard_api lookup', "Call 'func' every update frame" in api)

launched = j(S.launch_app(DEMO, restart=True))
check('launch', launched.get('frame', 0) > 0, 'startup %ss, vizard %s, vizmcp %s'
      % (launched.get('startup_s'), launched.get('vizard_version'), launched.get('vizmcp_version')))
try:
	# --- inspection --------------------------------------------------------
	st = j(S.vizard_status())
	check('status', st['connected'] and st['script'].endswith('demo_scene.py') and 'watch_start' in st['commands'])

	tree = j(S.scene_tree(depth=2, only_named=True))
	names = sorted(n for node in tree['nodes'] for n in node['names'])
	check('scene_tree names', {'ground', 'turntable', 'ball', 'label'} <= set(names), names)
	tt = [n for n in tree['nodes'] if 'turntable' in n['names']][0]
	kid_names = sorted(n for c in tt.get('children', []) for n in c['names'])
	check('scene_tree children', kid_names == ['base', 'beam', 'column'], kid_names)

	fn = j(S.find_nodes('table'))
	check('find_nodes', [n['names'][0] for n in fn['nodes']] == ['turntable'], fn)

	ni = j(S.node_info('beam'))
	check('node_info bbox', ni['bbox'] is not None and abs(ni['bbox']['size'][1] - 0.2) < 1e-3, ni['bbox'])

	sn = j(S.set_node('label', color=[1, 0.9, 0.2]))
	check('set_node', sn['names'] == ['label'])

	rc = j(S.run_code("print('hello from claude')\nx = 6 * 7\nx"))
	check('run_code value', rc['value'] == '42' and 'hello from claude' in rc['stdout'], rc)
	rc = j(S.run_code("1/0"))
	check('run_code error', 'ZeroDivisionError' in (rc.get('error') or ''))

	lg = j(S.get_log(contains='demo scene ready'))
	check('get_log', len(lg['lines']) == 1, lg['lines'])

	vc = j(S.vizconnect_info())
	check('vizconnect_info (not used)', vc == {'loaded': False}, vc)

	try:
		S.node_info('no_such_thing')
		check('bad node raises', False)
	except RuntimeError as e:
		check('bad node raises', 'NameError' in str(e))

	# --- physics through a watch -------------------------------------------
	S.run_code('drop.reset()')
	w = j(S.watch_start({'h': 'drop.height', 'v': 'drop.velocity', 'pos': 'ball.getPosition()'},
	                    name='bounce', until='drop.bounces >= 2'))
	check('watch_start columns', w['columns'] == ['h', 'v', 'pos.x', 'pos.y', 'pos.z'], w['columns'])
	u = j(S.wait_until('drop.bounces >= 2', limit_s=10))
	check('wait_until', u['met'], u)
	S.wait(frames=3)
	r = j(S.watch_read('bounce', max_points=0))
	check('watch stopped by until', 'until condition met' in (r['status']['stop_reason'] or ''), r['status'])
	with open(r['csv']) as f:
		rows = [list(map(float, row)) for row in list(csv.reader(f))[1:]]
	# columns: t, frame, h, v, pos.x, pos.y, pos.z
	v = [row[3] for row in rows]
	first_bounce = next(i for i in range(1, len(v)) if v[i - 1] < 0 <= v[i])
	peak = max(row[2] for row in rows[first_bounce:])
	check('physics: bounce peak = e^2 h0 = 1.28 m', abs(peak - 1.28) < 0.02, 'measured %.4f m' % peak)
	t_impact = rows[first_bounce][0]
	check('physics: first impact at sqrt(2h/g) = 0.639 s', abs(t_impact - 0.6386) < 0.04, 'measured %.3f s' % t_impact)
	plot = S.watch_plot('bounce', columns=['h'], title='Ball height (m), e = 0.8')
	save(plot[0], 'watch_plot.jpg')
	check('watch_plot', len(plot[0].data) > 5000)

	# --- seeing ------------------------------------------------------------
	shot = S.screenshot(max_size=800, annotate=True, save_to=os.path.join(OUT, 'demo.png'))
	save(shot[0], 'demo_annotated.jpg')
	check('screenshot annotate', 'labelled' in shot[1] and os.path.getsize(os.path.join(OUT, 'demo.png')) > 1000, shot[1])

	cam_before = j(S.camera())
	mv = S.multi_view(orbit='turntable', count=4)
	save(mv[0], 'multi_view.jpg')
	cam_after = j(S.camera())
	check('multi_view restores camera', cam_before == cam_after, (cam_before, cam_after))

	fs = S.filmstrip(count=4, interval_s=0.3)
	save(fs[0], 'filmstrip.jpg')
	check('filmstrip', 'frames' in fs[1], fs[1])

	pr = j(S.project(points=[[0, 0, -20]], nodes=['beam', 'ball']))
	scr = [it['screen'] for it in pr['items']]
	check('project', scr[0] is None and scr[1] is not None and scr[2] is not None, scr)
	pk = j(S.pick(x=scr[1][0], y=scr[1][1]))
	check('pick finds the beam at its projected center', pk['hit'] and 'beam' in pk.get('node', {}).get('names', []), pk)
	pk = j(S.pick(x=0.5, y=0.02))
	check('pick sky = no hit', not pk['hit'], pk)

	vid = j(S.record_video(seconds=2, path=os.path.join(OUT, 'demo.avi')))
	check('record_video', vid['bytes'] > 10000, vid)
	os.remove(vid['path'])      # uncompressed AVI, tens of MB

	# --- driving -----------------------------------------------------------
	j(S.send_key(' ', hold_frames=3))
	running = j(S.run_code('table.running'))['value']
	check('send_key -> onkeydown', running == 'False', running)
	held = j(S.run_code("viz.key.isDown('q')"))['value']
	check('isDown false when not held', held == 'False')
	S.send_key(' ')
	j(S.send_key('KEY_F12'))
	check('send_key constant name', True)

	drops = int(j(S.run_code('drop.drops'))['value'])
	try:
		size = j(S.vizard_status())['window_size']
		center, why = button_center()
		if center is None:
			# the bounding box is not available: sweep the top-left of the window instead.
			info('button rect unavailable, scanning for the button', why)
			targets = [[fx, fy] for fy in (0.04, 0.08, 0.12, 0.16) for fx in (0.04, 0.08, 0.12, 0.16, 0.2)]
		else:
			targets = [center]
		ck = None
		for x, y in targets:
			ck = j(S.click(x=x, y=y))
			S.wait(frames=5)
			drops2 = int(j(S.run_code('drop.drops'))['value'])
			if drops2 != drops:
				break
		check('click GUI button', drops2 == drops + 1,
		      'window %s, button at (%.3f, %.3f) -> pixel %s of %s; %s -> %s' % (
			      size, x, y, ck['client_pixel'], ck['client_size'], drops, drops2))
		check('click reports foreground state', 'was_foreground' in ck, ck)
	except RuntimeError as e:
		check('click GUI button', False, e)
	try:
		S.click(x=1.5, y=0.5)
		check('click rejects x outside 0..1', False)
	except RuntimeError as e:
		check('click rejects x outside 0..1', '0..1' in str(e), e)

	# --- reporting and regression tools --------------------------------------
	exp_csv = os.path.join(OUT, 'bounce_export.csv')
	ex = j(S.watch_export('bounce', exp_csv))
	check('watch_export csv', ex['format'] == 'csv' and ex['rows'] == len(rows) and os.path.getsize(exp_csv) > 100, ex)
	with open(exp_csv) as f:
		exp_rows = list(csv.reader(f))
	check('watch_export csv matches the recorded data',
	      exp_rows[0] == ['t', 'frame', 'h', 'v', 'pos.x', 'pos.y', 'pos.z']
	      and [float(c) for c in exp_rows[1]] == rows[0] and len(exp_rows) == len(rows) + 1, exp_rows[:2])
	exp_json = os.path.join(OUT, 'bounce_export.json')
	ex = j(S.watch_export('bounce', exp_json, columns=['t', 'h']))
	with open(exp_json) as f:
		doc = json.load(f)
	check('watch_export json + column subset', doc['columns'] == ['t', 'h'] and len(doc['rows']) == len(rows)
	      and doc['rows'][0] == [rows[0][0], rows[0][2]] and doc['expressions']['h'] == 'drop.height', doc['columns'])
	try:
		S.watch_export('bounce', exp_csv, columns=['nope'])
		check('watch_export unknown column raises', False)
	except RuntimeError as e:
		check('watch_export unknown column raises', 'unknown columns' in str(e), e)

	# screenshot_diff: freeze the scene so two captures should match, then change it.
	S.run_code('table.running = False\ndrop.update = lambda dt: None')
	S.wait(frames=10)
	ref_png = os.path.join(OUT, 'diff_reference.png')
	S.screenshot_diff(ref_png, update_reference=True)
	check('screenshot_diff writes the reference', os.path.getsize(ref_png) > 1000)
	same = S.screenshot_diff(ref_png, threshold_pct=0.5, save_diff_to=os.path.join(OUT, 'diff_same.png'))
	same_j = j(same[1])
	check('screenshot_diff: unchanged scene passes', same_j['passed'], same_j)
	S.set_node('beam', visible=False)
	S.wait(frames=5)
	changed = S.screenshot_diff(ref_png, threshold_pct=0.5, save_diff_to=os.path.join(OUT, 'diff_changed.png'))
	save(changed[0], 'diff_changed.jpg')
	ch_j = j(changed[1])
	check('screenshot_diff: hidden beam fails and is boxed',
	      ch_j['passed'] is False and ch_j['diff_pct'] > 0.5 and ch_j['bbox'] is not None, ch_j)
	S.set_node('beam', visible=True)
	S.run_code('del drop.update\ntable.running = True')

	n_total = int(j(S.run_code('len(viz.getNodeList())'))['value'])
	st1 = j(S.scene_stats(top=5))
	check('scene_stats node count matches Vizard', st1['nodes'] == n_total and 0 < st1['visible_nodes'] <= n_total, st1['nodes'])
	check('scene_stats top nodes', 0 < len(st1['top_nodes']) <= 5 and all('id' in n for n in st1['top_nodes']), st1['top_nodes'])
	S.set_node('label', visible=False)
	st2 = j(S.scene_stats(top=0))
	check('scene_stats sees a hidden node', st2['visible_nodes'] < st1['visible_nodes'] and st2['top_nodes'] == [],
	      (st1['visible_nodes'], st2['visible_nodes']))
	S.set_node('label', visible=True)
	info('scene_stats triangles/textures/lights', {k: st1[k] for k in ('triangles', 'triangles_visible', 'textures', 'lights', 'ranked_by')})
	info('scene_stats notes', st1['notes'])
	check('scene_stats counts triangles via getStats', (st1['triangles'] or 0) > 0 and st1['ranked_by'] == 'triangles'
	      and 0 < st1['triangles_visible'] <= st1['triangles'], st1['triangles'])
	# Triangle, texture and light accessors are probed by name; list what this Vizard really has.
	for q in ('triangle', 'getTexture', 'light'):
		info('vizard_api %r' % q, S.vizard_api(q, limit=8).replace('\n', ' | '))

	cam = j(S.camera(position=[0, 6, -9], look_at=[0, 1, 0], hold=True))
	check('camera hold', abs(cam['position'][1] - 6) < 1e-4 and cam['held'], cam)
	S.camera(release=True)

	pf = j(S.profile(frames=60))
	top_funcs = [t['function'] for t in pf['top'][:15]]
	check('profile sees app code', 'update' in top_funcs, (pf['frame_ms'], pf['python_ms_per_frame'], top_funcs[:6]))
finally:
	print('QUIT', S.quit_app())

print('FAILURES', failures)
if __name__ == '__main__':
	sys.exit(1 if failures else 0)
assert not failures, failures
