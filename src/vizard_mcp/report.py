"""Build the self-contained HTML report behind make_report.

Everything here is pure: text and bytes in, an HTML string out. Images are inlined as base64
data URIs and the stylesheet is embedded, so the file can be moved, mailed or opened offline,
and the browser's Print > Save as PDF gives the PDF.
"""

import base64
import html
import re

CSS = """\
:root { --ink: #1c1f24; --muted: #5b6470; --rule: #d5d9df; --soft: #f4f6f8;
        --pass: #17692f; --pass-bg: #e3f4e8; --fail: #a11d1d; --fail-bg: #fbe5e5; --none: #4a5260; --none-bg: #e9ecf0; }
* { box-sizing: border-box; }
body { margin: 0; background: #fff; color: var(--ink);
       font: 11pt/1.5 "Segoe UI", system-ui, -apple-system, Arial, sans-serif; }
main { max-width: 190mm; margin: 0 auto; padding: 14mm 8mm 18mm; }
header { border-bottom: 2px solid var(--ink); padding-bottom: 6mm; margin-bottom: 8mm; }
h1 { font-size: 22pt; line-height: 1.2; margin: 0 0 2mm; }
h2 { font-size: 15pt; margin: 9mm 0 3mm; padding-bottom: 1mm; border-bottom: 1px solid var(--rule); }
h3 { font-size: 12.5pt; margin: 6mm 0 2mm; }
h4, h5 { font-size: 11pt; margin: 4mm 0 1mm; }
p { margin: 0 0 3mm; }
.byline { color: var(--muted); margin: 0 0 4mm; }
code { font: 10pt Consolas, "DejaVu Sans Mono", monospace; background: var(--soft); padding: 0 1mm; border-radius: 1mm; }
a { color: #0b4fa3; }
table { border-collapse: collapse; width: 100%; margin: 2mm 0 3mm; font-size: 10pt; }
th, td { border: 1px solid var(--rule); padding: 1.2mm 2.4mm; text-align: left; vertical-align: top; }
th { background: var(--soft); font-weight: 600; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
table.kv th { width: 34%; }
figure { margin: 3mm 0 5mm; text-align: center; }
figure img { max-width: 100%; height: auto; border: 1px solid var(--rule); }
figcaption { color: var(--muted); font-size: 9.5pt; margin-top: 1.5mm; }
.badge { display: inline-block; font-weight: 700; letter-spacing: .04em; padding: 0.6mm 3.2mm; border-radius: 1.2mm; border: 1px solid; }
.badge.pass { color: var(--pass); background: var(--pass-bg); border-color: var(--pass); }
.badge.fail { color: var(--fail); background: var(--fail-bg); border-color: var(--fail); }
.badge.none { color: var(--none); background: var(--none-bg); border-color: var(--none); }
.verdict { margin: 0 0 2mm; }
.note { color: var(--muted); font-size: 9.5pt; }
footer { margin-top: 10mm; padding-top: 2mm; border-top: 1px solid var(--rule); color: var(--muted); font-size: 9pt; }
@page { size: A4; margin: 14mm 12mm; }
@media print {
  main { max-width: none; padding: 0; }
  h1, h2, h3, h4, h5, figure, tr, .verdict { break-inside: avoid; }
  h2, h3, h4, h5 { break-after: avoid; }
  figure img { max-height: 120mm; width: auto; }
  a { color: inherit; text-decoration: none; }
}
"""


def esc(x):
	return html.escape('' if x is None else str(x), quote=True)


_SAFE_LINK = re.compile(r'^(https?://|mailto:)', re.I)


def _inline(text):
	"""Escape, then apply `code`, **bold**, *italic* and [text](http link)."""
	t = esc(text)
	codes = []

	def keep(m):
		codes.append('<code>%s</code>' % m.group(1))
		return '\x00%d\x00' % (len(codes) - 1)
	t = re.sub(r'`([^`]+)`', keep, t)
	t = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', t)
	t = re.sub(r'(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])', r'<em>\1</em>', t)

	def link(m):
		url = html.unescape(m.group(2))
		if not _SAFE_LINK.match(url):
			return m.group(0)
		return '<a href="%s">%s</a>' % (esc(url), m.group(1))
	t = re.sub(r'\[([^\]]+)\]\(([^)\s]+)\)', link, t)
	return re.sub(r'\x00(\d+)\x00', lambda m: codes[int(m.group(1))], t)


def markdown(text):
	"""Markdown-ish text to HTML: blank-line separated paragraphs, # headings (as h3..h5),
	"- " / "* " bullet lists, "1. " numbered lists, and inline code, bold, italic and links.
	Raw HTML is always escaped."""
	out = []
	for block in re.split(r'\n\s*\n', str(text).strip()):
		lines = [l.rstrip() for l in block.strip().splitlines() if l.strip()]
		if not lines:
			continue
		m = re.match(r'^(#{1,3})\s+(.*)$', lines[0])
		if m:
			level = 2 + len(m.group(1))
			out.append('<h%d>%s</h%d>' % (level, _inline(m.group(2)), level))
			lines = lines[1:]
			if not lines:
				continue
		if all(re.match(r'^[-*]\s+', l) for l in lines):
			out.append('<ul>%s</ul>' % ''.join('<li>%s</li>' % _inline(re.sub(r'^[-*]\s+', '', l)) for l in lines))
		elif all(re.match(r'^\d+[.)]\s+', l) for l in lines):
			out.append('<ol>%s</ol>' % ''.join('<li>%s</li>' % _inline(re.sub(r'^\d+[.)]\s+', '', l)) for l in lines))
		else:
			out.append('<p>%s</p>' % _inline(' '.join(l.strip() for l in lines)))
	return '\n'.join(out)


def heading(text, level=2):
	level = min(max(int(level), 2), 5)
	return '<h%d>%s</h%d>' % (level, esc(text), level)


def figure(data, mime, caption=None, alt=None):
	uri = 'data:%s;base64,%s' % (mime, base64.b64encode(data).decode('ascii'))
	cap = '<figcaption>%s</figcaption>' % _inline(caption) if caption else ''
	return '<figure><img src="%s" alt="%s">%s</figure>' % (uri, esc(alt or caption or ''), cap)


def fmt(v):
	"""A number or value for a table cell."""
	if v is None:
		return '-'
	if isinstance(v, bool):
		return 'yes' if v else 'no'
	if isinstance(v, float):
		return '%.6g' % v
	if isinstance(v, (list, tuple)):
		return ', '.join(fmt(x) for x in v)
	return str(v)


def table(head, rows, caption=None, numeric_from=1):
	"""A table with a header row; columns from index `numeric_from` are right-aligned."""
	def cell(tag, i, v):
		cls = ' class="num"' if i >= numeric_from else ''
		return '<%s%s>%s</%s>' % (tag, cls, esc(v), tag)
	body = ''.join('<tr>%s</tr>' % ''.join(cell('td', i, v) for i, v in enumerate(r)) for r in rows)
	cap = '<p class="note">%s</p>' % _inline(caption) if caption else ''
	return '%s<table><thead><tr>%s</tr></thead><tbody>%s</tbody></table>' % (
		cap, ''.join(cell('th', i, h) for i, h in enumerate(head)), body)


def keyvalue(items, caption=None):
	"""items: dict or list of (key, value) pairs."""
	pairs = list(items.items()) if isinstance(items, dict) else [tuple(i) for i in items]
	body = ''.join('<tr><th>%s</th><td>%s</td></tr>' % (esc(k), esc(fmt(v))) for k, v in pairs)
	cap = '<p class="note">%s</p>' % _inline(caption) if caption else ''
	return '%s<table class="kv"><tbody>%s</tbody></table>' % (cap, body)


def badge(passed):
	"""PASS / FAIL, or NO VERDICT when `passed` is None (no tolerance was given)."""
	label, cls = {True: ('PASS', 'pass'), False: ('FAIL', 'fail')}.get(passed, ('NO VERDICT', 'none'))
	return '<span class="badge %s">%s</span>' % (cls, label)


def document(title, author, date, info, body, generator=None):
	"""The whole page. info: list of (label, value) lines about the app and Vizard for the header."""
	meta = ' &middot; '.join(x for x in (esc(author) if author else '', esc(date) if date else '') if x)
	info_html = keyvalue(info) if info else ''
	foot = '<footer>%s</footer>' % esc(generator) if generator else ''
	return ('<!DOCTYPE html>\n<html lang="en"><head><meta charset="utf-8">\n'
	        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
	        '<title>%s</title>\n<style>\n%s</style></head>\n<body><main>\n'
	        '<header><h1>%s</h1><p class="byline">%s</p>%s</header>\n%s\n%s\n</main></body></html>\n') % (
		esc(title), CSS, esc(title), meta, info_html, '\n'.join(body), foot)
