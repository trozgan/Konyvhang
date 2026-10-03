"""Find translatable blocks in XHTML, turn them into model markup, write translations back.

A segment is a block element (p, h1, li, ...) that holds text and no nested block.
Inside a segment every inline element becomes `<tag n="k">`, where k indexes the
original element. The model sees no classes or styles, and the original attributes
come back from the source document when the translation is written back.
"""

import copy
import html
import itertools
import re

from lxml import etree

BLOCK_TAGS = {
    "p",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "li",
    "dt",
    "dd",
    "td",
    "th",
    "caption",
    "figcaption",
    "blockquote",
    "pre",
    "div",
    "section",
    "article",
    "aside",
    "header",
    "footer",
    "figure",
    "ul",
    "ol",
    "dl",
    "table",
    "tr",
    "tbody",
    "thead",
    "tfoot",
    "nav",
    "hr",
    "body",
}
# Inline elements whose content is not text: sent empty, restored whole.
OPAQUE_TAGS = {"svg", "math", "img", "image", "object", "video", "audio", "iframe"}
LETTER = re.compile(r"[^\W\d_]")
EPUB_TYPE = "{http://www.idpf.org/2007/ops}type"
# Reference matter: copied as is, not translated or read aloud. The citations keep their
# original titles anyway, and an index points at print pages.
REFERENCE_TYPES = {"bibliography", "index"}


def local(el) -> str:
    return etree.QName(el).localname if isinstance(el.tag, str) else ""


def semantics(el) -> set[str]:
    """epub:type and ARIA role values of an element, e.g. {"footnote", "doc-footnote"}."""
    values = set((el.get(EPUB_TYPE) or "").split()) | set((el.get("role") or "").split())
    return values | {v.removeprefix("doc-") for v in values}


def in_reference_matter(el: etree._Element) -> bool:
    return any(semantics(a) & REFERENCE_TYPES for a in [el, *el.iterancestors()])


def find_segments(tree: etree._ElementTree) -> list[etree._Element]:
    body = next((el for el in tree.iter() if local(el) == "body"), None)
    if body is None:
        return []
    segments = []
    for el in body.iter():
        if local(el) not in BLOCK_TAGS or local(el) in {"body", "hr"}:
            continue
        if any(local(d) in BLOCK_TAGS for d in el.iterdescendants()):
            continue
        if LETTER.search("".join(el.itertext())):
            segments.append(el)
    return segments


def inline_elements(segment: etree._Element) -> list[etree._Element]:
    """Inline descendants in document order; list index + 1 is the `n` attribute."""
    out = []

    def walk(el):
        for child in el:
            if not isinstance(child.tag, str):  # comments, processing instructions
                continue
            out.append(child)
            if local(child) not in OPAQUE_TAGS:
                walk(child)

    walk(segment)
    return out


def to_markup(segment: etree._Element) -> str:
    """Inner content of a segment as namespace-free markup with numbered inline tags.

    The walk visits elements in the same order as inline_elements(), so a counter gives
    the same numbers. (Keying by id() is unsafe: lxml element proxies are recreated, and
    a freed proxy's id can be reused by another element.)
    """
    counter = itertools.count(1)

    def build(src, dst):
        dst.text = src.text
        for child in src:
            if not isinstance(child.tag, str):
                continue
            new = etree.SubElement(dst, local(child), n=str(next(counter)))
            if local(child) not in OPAQUE_TAGS:
                build(child, new)
            new.tail = child.tail
        return dst

    root = build(segment, etree.Element("seg"))
    xml = etree.tostring(root, encoding="unicode")
    return re.sub(r"^<seg>|</seg>$|^<seg/>$", "", xml)


def apply_translation(segment: etree._Element, translated: etree._Element) -> None:
    """Replace the segment's content with `translated` (a parsed <seg> element).

    Each `<tag n="k">` in the translation becomes a copy of original inline element k.
    """
    originals = inline_elements(segment)

    def build(src, dst):
        dst.text = src.text
        for child in src:
            orig = originals[int(child.get("n")) - 1]
            if local(orig) in OPAQUE_TAGS:
                new = copy.deepcopy(orig)
                dst.append(new)
            else:
                new = etree.SubElement(dst, orig.tag, dict(orig.attrib), nsmap=None)
                build(child, new)
            new.tail = child.tail

    for child in list(segment):
        segment.remove(child)
    build(translated, segment)


def plain_text(markup: str) -> str:
    return html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", markup)).strip())
