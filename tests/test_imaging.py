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






