import re

import zipfile

from lxml import etree

from konyvhang import epub, segment


def load(epub_file, name):
    with zipfile.ZipFile(epub_file) as zf:
        return epub.parse_xml(zf.read(name))


def test_find_segments(epub_file):
    tree = load(epub_file, "OEBPS/text/ch1.xhtml")
    texts = [segment.plain_text(segment.to_markup(s)) for s in segment.find_segments(tree)]
    assert texts == [
        "Chapter One",
        "It was a dark and stormy night, 1 said Tom.",  # plain_text folds the nbsp
        "Mary looked up.“Really?” she asked & frowned.",
        "A quote here.",
        "1. A footnote.",
    ]  # "* * *" has no letters and is skipped


def test_nested_blocks_use_innermost(epub_file):
    tree = load(epub_file, "OEBPS/text/ch2.xhtml")
    assert [segment.local(s) for s in segment.find_segments(tree)] == ["h2", "li", "li"]


def test_markup_numbers_inline_elements_without_attributes(epub_file):
    tree = load(epub_file, "OEBPS/text/ch1.xhtml")
    seg = segment.find_segments(tree)[1]
    assert segment.to_markup(seg) == (
        'It was a <em n="1">dark</em> and stormy night, '
        '<a n="2"><sup n="3">1</sup></a> said <span n="4">Tom</span>.'
    )
    quote = segment.find_segments(tree)[3]
    assert segment.to_markup(quote) == 'A quote <img n="1"/> here.'


def test_identity_translation_keeps_document(epub_file):
    tree = load(epub_file, "OEBPS/text/ch1.xhtml")
    before = etree.tostring(tree)
    for seg in segment.find_segments(tree):
        segment.apply_translation(seg, etree.fromstring(f"<seg>{segment.to_markup(seg)}</seg>"))
    assert etree.tostring(tree) == before


def test_translation_may_reorder_inline_elements(epub_file):
    tree = load(epub_file, "OEBPS/text/ch1.xhtml")
    seg = segment.find_segments(tree)[1]
    new = etree.fromstring(
        '<seg><span n="4">Tom</span> mondta <a n="2"><sup n="3">1</sup></a>: <em n="1">sötét</em> éj volt.</seg>'
    )
    segment.apply_translation(seg, new)
    xml = etree.tostring(seg, encoding="unicode")
    assert '<span class="sc">Tom</span> mondta <a href="#n1" class="noteref"><sup>1</sup></a>' in xml
    assert "<em>sötét</em> éj volt.</p>" in xml
    assert 'class="first"' in xml


def test_markup_numbers_match_inline_elements_on_many_elements():
    # Many short-lived lxml proxies: id()-based numbering broke here.
    body = "".join(f'<span class="c{i}">w{i}<em>x</em></span> ' for i in range(300))
    tree = etree.ElementTree(etree.fromstring(f'<html xmlns="http://www.w3.org/1999/xhtml"><body><p>{body}</p></body></html>'))
    seg = segment.find_segments(tree)[0]
    markup = segment.to_markup(seg)
    tags = [segment.local(el) for el in segment.inline_elements(seg)]
    numbered = {int(n): t for t, n in re.findall(r'<(\w+) n="(\d+)"', markup)}
    assert [numbered[i + 1] for i in range(len(tags))] == tags
