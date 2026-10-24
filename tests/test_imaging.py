import io

from PIL import Image

from vizard_mcp import imaging


def solid(size, color):
	return Image.new('RGB', size, color)


def test_load_converts_to_rgb(tmp_path):
	p = tmp_path / 'a.png'
	Image.new('RGBA', (30, 20), (1, 2, 3, 128)).save(p)
	im = imaging.load(str(p))
	assert im.mode == 'RGB' and im.size == (30, 20)


def test_fit_downscales_and_keeps_original():
	im = solid((400, 200), (0, 0, 0))
	out = imaging.fit(im, 100)
	assert out.size == (100, 50) and im.size == (400, 200)
	assert imaging.fit(im, 0) is im
	assert imaging.fit(im, 1000) is im


def test_to_jpeg_roundtrip():
	data = imaging.to_jpeg(solid((64, 48), (200, 10, 10)))
	im = Image.open(io.BytesIO(data))
	assert im.format == 'JPEG' and im.size == (64, 48)


def test_grid_layout():
	for n, (cols, rows) in {1: (1, 1), 2: (2, 1), 4: (2, 2), 5: (3, 2), 6: (3, 2)}.items():
		imgs = [solid((100, 60), (50 * i, 0, 0)) for i in range(n)]
		sheet = imaging.grid(imgs, ['view %d' % i for i in range(n)], 0)
		assert sheet.mode == 'RGB'
		assert sheet.size == (cols * 100, rows * 60)


def test_grid_fits_max_size():
	imgs = [solid((300, 200), (0, 90, 0)) for _ in range(4)]
	sheet = imaging.grid(imgs, list('abcd'), 400)
	assert max(sheet.size) == 400


def test_annotate_draws_without_touching_input():
	im = solid((320, 240), (255, 255, 255))
	items = [{'name': 'crane.hook', 'screen': [0.5, 0.5]},
	         {'name': 'crane.base', 'screen': [0.5, 0.5]},     # overlapping tags get nudged
	         {'name': 'ball', 'screen': [0.1, 0.9]}]
	out = imaging.annotate(im, items)
	assert out.size == im.size and out.mode == 'RGB'
	assert im.getpixel((160, 120)) == (255, 255, 255)
	assert out.getpixel((160, 120)) != (255, 255, 255)          # dot at the centre
	assert imaging.annotate(im, []).size == im.size


def test_line_chart_size_and_mode():
	t = [i * 0.1 for i in range(50)]
	chart = imaging.line_chart('watch "ball"', t, [('y', [v * v for v in t]), ('vy', [-v for v in t])])
	assert chart.size == (1100, 620) and chart.mode == 'RGB'
	assert len(chart.getcolors(maxcolors=1 << 20)) > 10            # not blank
	assert imaging.line_chart('t', t, [('y', t)], size=(500, 300)).size == (500, 300)


def test_line_chart_degenerate_inputs():
	assert imaging.line_chart('one point', [0.0], [('y', [3.0])]).size == (1100, 620)
	assert imaging.line_chart('flat', [0, 1, 2], [('y', [5, 5, 5])]).mode == 'RGB'
	assert imaging.line_chart('zero', [0, 1], [('y', [0, 0])]).mode == 'RGB'


def test_nice_ticks_cover_range():
	ticks = imaging._nice_ticks(0.0, 9.7)
	assert ticks[0] <= 0.0 and ticks[-1] >= 9.7
	assert ticks == sorted(ticks)
	assert len(imaging._nice_ticks(3, 3)) >= 2
