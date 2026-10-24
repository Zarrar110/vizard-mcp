import os

import pytest

from vizard_mcp import api_index

VIZ = b'''\xef\xbb\xbf"""viz module"""
VERSION = "8.3"
lower = 5

def go(mode=0, *args, **kw):
    """Start the world."""

def _private():
    pass

class VizNode(object):
    """A scene node."""
    def getBoundingBox(self, mode=1):
        """Return the bounding box of the node."""
    def _hidden(self):
        pass
    def __init__(self, id):
        pass
'''

VIZACT = '''
def onupdate(priority, func, *args):
    """Call 'func' every update frame."""

async def later():
    pass
'''


@pytest.fixture
def root(tmp_path, monkeypatch):
	monkeypatch.setattr(api_index, '_INDEX', None)
	r = tmp_path / 'Vizard8' / 'python'
	r.mkdir(parents=True)
	(r / 'viz.py').write_bytes(VIZ)            # UTF-8 BOM, would break a plain utf-8 read in ast.parse
	(r / 'vizact.py').write_text(VIZACT, encoding='utf-8')
	(r / 'broken.py').write_text('def (:\n', encoding='utf-8')
	pkg = r / 'vizfx'
	pkg.mkdir()
	(pkg / '__init__.py').write_text('def addChild(path):\n    """Load a model."""\n', encoding='utf-8')
	(pkg / 'helper.py').write_text('def nothere(): pass\n', encoding='utf-8')   # not indexed (not __init__)
	return str(r)


def names(entries):
	return [e['name'] for e in entries]


def test_vizard_python_dir():
	p = api_index.vizard_python_dir(os.path.join('C:', os.sep, 'Viz', 'bin', 'winviz.exe'))
	assert os.path.normpath(p) == os.path.normpath(os.path.join('C:', os.sep, 'Viz', 'python'))


def test_build_indexes_public_symbols_bom_file_included(root):
	n = names(api_index.build(root))
	assert {'viz.go', 'viz.VizNode', 'viz.VizNode.getBoundingBox', 'viz.VizNode.__init__',
	        'viz.VERSION', 'vizact.onupdate', 'vizact.later', 'vizfx.addChild'} <= set(n)
	assert not any('_private' in x or '_hidden' in x or 'lower' in x or 'broken' in x or 'nothere' in x for x in n)


def test_entry_fields(root):
	e = {x['name']: x for x in api_index.build(root)}
	assert e['viz.go']['signature'] == '(mode=0, *args, **kw)'
	assert e['viz.go']['kind'] == 'function' and e['viz.go']['doc'] == 'Start the world.'
	assert e['viz.VizNode']['kind'] == 'class' and e['viz.VizNode']['signature'] == '(object)'
	assert e['viz.VizNode.getBoundingBox']['kind'] == 'method'
	assert e['viz.VERSION']['kind'] == 'constant' and e['viz.VERSION']['signature'] == " = '8.3'"
	assert e['viz.go']['file'].endswith('viz.py') and e['viz.go']['line'] == 5


def test_lookup(root):
	assert names(api_index.lookup(root, 'vizact.onupdate')) == ['vizact.onupdate']
	assert names(api_index.lookup(root, 'VizNode.getBoundingBox')) == ['viz.VizNode.getBoundingBox']
	assert api_index.lookup(root, 'nonexistent') == []


def test_search_ranking_and_filters(root):
	hits = names(api_index.search(root, 'bounding'))
	assert hits == ['viz.VizNode.getBoundingBox']
	assert names(api_index.search(root, 'onupdate'))[0] == 'vizact.onupdate'
	assert 'vizact.onupdate' in names(api_index.search(root, 'update frame'))     # docstring match
	assert names(api_index.search(root, 'GO', kinds=['function'])) == ['viz.go']
	assert api_index.search(root, 'go', kinds=['class']) == []
	assert len(api_index.search(root, 'viz', limit=2)) == 2


def test_search_prefers_priority_module(root):
	hits = names(api_index.search(root, 'i'))
	assert hits.index('viz.VERSION') < hits.index('vizfx.addChild')


def test_index_is_cached(root):
	api_index.get_index(root)
	assert api_index.get_index('/nonexistent') is api_index.get_index(root)
