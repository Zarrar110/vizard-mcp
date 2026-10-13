"""Image helpers for the MCP side: downscaling, grids, labels and line charts."""

import io
import math

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


def _text_box(draw, xy, text, font, fg=(255, 255, 255), bg=(0, 0, 0, 170), anchor='la'):
	x, y = xy
	l, t, r, b = draw.textbbox((x, y), text, font=font, anchor=anchor)
	draw.rectangle([l - 3, t - 2, r + 3, b + 2], fill=bg)
	draw.text((x, y), text, font=font, fill=fg, anchor=anchor)
	return (l - 3, t - 2, r + 3, b + 2)


def grid(images, captions, max_size):
	"""Tile images (same size) into a near-square grid with a caption on each."""
	n = len(images)
	cols = math.ceil(math.sqrt(n))
	rows = math.ceil(n / cols)
	w, h = images[0].size
	sheet = Image.new('RGB', (cols * w, rows * h), (30, 30, 30))
	draw = ImageDraw.Draw(sheet, 'RGBA')
	font = _font(max(14, h // 22))
	for i, (im, cap) in enumerate(zip(images, captions)):
		x, y = (i % cols) * w, (i // cols) * h
		sheet.paste(im.resize((w, h)), (x, y))
		_text_box(draw, (x + 8, y + 8), cap, font)
	return fit(sheet, max_size)


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


def _nice_ticks(lo, hi, n=5):
	if hi <= lo:
		hi = lo + 1.0
	raw = (hi - lo) / n
	mag = 10 ** math.floor(math.log10(raw))
	step = min((s * mag for s in (1, 2, 2.5, 5, 10) if s * mag >= raw), default=raw)
	ticks = [math.floor(lo / step) * step]
	while ticks[-1] < hi - 1e-12 * max(1.0, abs(hi)):
		ticks.append(ticks[-1] + step)
	return [round(t, 10) for t in ticks]


def line_chart(title, x, series, x_label='time (s)', size=(1100, 620)):
	"""series: list of (label, values). One shared y axis per chart."""
	W, H = size
	im = Image.new('RGB', size, (255, 255, 255))
	d = ImageDraw.Draw(im)
	f_title, f_axis = _font(22), _font(15)
	left, right, top, bottom = 90, 30, 60, 70
	all_y = [v for _, vals in series for v in vals]
	ylo, yhi = min(all_y), max(all_y)
	pad = (yhi - ylo) * 0.06 or abs(yhi) * 0.06 or 1.0
	yticks = _nice_ticks(ylo - pad, yhi + pad)
	xticks = _nice_ticks(min(x), max(x))
	x0, x1 = xticks[0], xticks[-1]
	y0, y1 = yticks[0], yticks[-1]

	def px(xv):
		return left + (xv - x0) / ((x1 - x0) or 1) * (W - left - right)

	def py(yv):
		return H - bottom - (yv - y0) / ((y1 - y0) or 1) * (H - top - bottom)

	d.text((left, 18), title, font=f_title, fill=(20, 20, 20))
	for t in yticks:
		d.line([left, py(t), W - right, py(t)], fill=(228, 228, 228))
		d.text((left - 8, py(t)), '%g' % t, font=f_axis, fill=(80, 80, 80), anchor='rm')
	for t in xticks:
		d.line([px(t), top, px(t), H - bottom], fill=(240, 240, 240))
		d.text((px(t), H - bottom + 8), '%g' % t, font=f_axis, fill=(80, 80, 80), anchor='mt')
	d.rectangle([left, top, W - right, H - bottom], outline=(120, 120, 120))
	d.text(((left + W - right) / 2, H - 28), x_label, font=f_axis, fill=(60, 60, 60), anchor='mt')

	lx, ly = left + 10, top + 10
	for k, (label, vals) in enumerate(series):
		color = PALETTE[k % len(PALETTE)]
		pts = [(px(a), py(b)) for a, b in zip(x, vals)]
		if len(pts) > 1:
			d.line(pts, fill=color, width=2)
		else:
			d.ellipse([pts[0][0] - 3, pts[0][1] - 3, pts[0][0] + 3, pts[0][1] + 3], fill=color)
		i_max = max(range(len(vals)), key=vals.__getitem__)
		d.ellipse([pts[i_max][0] - 4, pts[i_max][1] - 4, pts[i_max][0] + 4, pts[i_max][1] + 4], outline=color, width=2)
		tag = '%s  (max %.4g at t=%.3g)' % (label, vals[i_max], x[i_max])
		bb = d.textbbox((lx + 22, ly), tag, font=f_axis)
		d.line([lx, ly + 8, lx + 16, ly + 8], fill=color, width=3)
		d.text((lx + 22, ly), tag, font=f_axis, fill=(30, 30, 30))
		lx = bb[2] + 24
		if lx > W - 300:
			lx, ly = left + 10, ly + 22
	return im
