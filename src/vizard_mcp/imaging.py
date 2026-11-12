"""Image helpers for the MCP side: downscaling, grids, labels and line charts."""

import io

from PIL import Image, ImageDraw, ImageFont

PALETTE = [(31, 119, 180), (214, 39, 40), (44, 160, 44), (255, 127, 14), (148, 103, 189),
           (140, 86, 75), (227, 119, 194), (23, 190, 207), (188, 189, 34), (127, 127, 127)]


def _font(size):
	for name in ('segoeui.ttf', 'arial.ttf', 'DejaVuSans.ttf'):
		try:
			return ImageFont.truetype(name, size)
		except OSError:
			pass
	return ImageFont.load_default()


def load(path):
	with Image.open(path) as im:
		return im.convert('RGB')


def fit(im, max_size):
	if max_size and max(im.size) > max_size:
		im = im.copy()
		im.thumbnail((max_size, max_size))
	return im


def to_jpeg(im, quality=85):
	buf = io.BytesIO()
	im.save(buf, 'JPEG', quality=quality)
	return buf.getvalue()


def to_png(im):
	buf = io.BytesIO()
	im.save(buf, 'PNG')
	return buf.getvalue()


def _text_box(draw, xy, text, font, fg=(255, 255, 255), bg=(0, 0, 0, 170), anchor='la'):
	x, y = xy
	l, t, r, b = draw.textbbox((x, y), text, font=font, anchor=anchor)
	draw.rectangle([l - 3, t - 2, r + 3, b + 2], fill=bg)
	draw.text((x, y), text, font=font, fill=fg, anchor=anchor)
	return (l - 3, t - 2, r + 3, b + 2)




def annotate(im, items):
	"""Draw a dot and a name tag for each item {'name', 'screen': [x, y] in 0..1}."""
	im = im.copy()
	draw = ImageDraw.Draw(im, 'RGBA')
	font = _font(max(12, im.size[1] // 45))
	taken = []
	for k, it in enumerate(items):
		x, y = it['screen'][0] * im.size[0], it['screen'][1] * im.size[1]
		color = PALETTE[k % len(PALETTE)]
		draw.ellipse([x - 4, y - 4, x + 4, y + 4], fill=color, outline=(255, 255, 255))
		ty = y - 8
		box = draw.textbbox((x + 8, ty), it['name'], font=font, anchor='lb')
		for _ in range(12):     # nudge the tag down until it does not overlap an earlier one
			if not any(box[0] < o[2] and box[2] > o[0] and box[1] < o[3] and box[3] > o[1] for o in taken):
				break
			ty += box[3] - box[1] + 4
			box = draw.textbbox((x + 8, ty), it['name'], font=font, anchor='lb')
		if ty != y - 8:
			draw.line([x, y, x + 8, ty - 4], fill=color, width=1)
		taken.append(_text_box(draw, (x + 8, ty), it['name'], font, bg=color + (210,), anchor='lb'))
	return im






