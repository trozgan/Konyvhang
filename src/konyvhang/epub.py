"""EPUB as a ZIP file: locate the OPF, read the spine, write a modified copy."""

import posixpath
import re
import zipfile
from dataclasses import dataclass
from html.entities import name2codepoint
from urllib.parse import unquote

from lxml import etree

NS = {
    "c": "urn:oasis:names:tc:opendocument:xmlns:container",
    "opf": "http://www.idpf.org/2007/opf",
    "dc": "http://purl.org/dc/elements/1.1/",
    "ncx": "http://www.daisy.org/z3986/2005/ncx/",
    "x": "http://www.w3.org/1999/xhtml",
}
XHTML_TYPES = {"application/xhtml+xml", "text/html"}
XML_ENTITIES = {"amp", "lt", "gt", "quot", "apos"}


@dataclass
class Book:
    opf_path: str
    spine: list[str]  # XHTML documents to translate, in reading order
    nav_path: str | None
    ncx_path: str | None


def parse_xml(data: bytes) -> etree._ElementTree:
    """Parse XHTML/XML. HTML named entities such as &nbsp; become numeric references first."""

    def fix(m: re.Match) -> bytes:
        name = m.group(1).decode()
        if name in XML_ENTITIES or name not in name2codepoint:
            return m.group(0)
        return f"&#{name2codepoint[name]};".encode()

    data = re.sub(rb"&([A-Za-z][A-Za-z0-9]*);", fix, data)
    parser = etree.XMLParser(resolve_entities=False, load_dtd=False, no_network=True, huge_tree=True)
    return etree.ElementTree(etree.fromstring(data, parser))


def serialize(tree: etree._ElementTree) -> bytes:
    return etree.tostring(tree, xml_declaration=True, encoding="utf-8", doctype=tree.docinfo.doctype or None)


def read_book(zf: zipfile.ZipFile) -> Book:
    container = parse_xml(zf.read("META-INF/container.xml"))
    opf_path = container.find(".//c:rootfile", NS).get("full-path")
    opf = parse_xml(zf.read(opf_path))
    base = posixpath.dirname(opf_path)

    def resolve(href: str) -> str:  # hrefs are URLs: "ch%201.xhtml" is the ZIP entry "ch 1.xhtml"
        return posixpath.normpath(posixpath.join(base, unquote(href.split("#", maxsplit=1)[0])))

    items = {}
    nav_path = ncx_path = None
    for item in opf.iterfind(".//opf:manifest/opf:item", NS):
        items[item.get("id")] = item
        if "nav" in (item.get("properties") or "").split():
            nav_path = resolve(item.get("href"))
        if item.get("media-type") == "application/x-dtbncx+xml":
            ncx_path = resolve(item.get("href"))

    spine = []
    for ref in opf.iterfind(".//opf:spine/opf:itemref", NS):
        item = items.get(ref.get("idref"))
        if item is None or item.get("media-type") not in XHTML_TYPES:
            continue
        path = resolve(item.get("href"))
        if path != nav_path:  # the TOC is translated separately as labels
            spine.append(path)
    return Book(opf_path, spine, nav_path, ncx_path)


def write_epub(src: str, dest: str, replaced: dict[str, bytes]) -> None:
    """Copy the EPUB entry by entry; entries in `replaced` get new content.

    `mimetype` goes first and uncompressed, as the OCF spec requires.
    """
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dest, "w") as zout:
        infos = sorted(zin.infolist(), key=lambda i: i.filename != "mimetype")
        for info in infos:
            data = replaced.get(info.filename, zin.read(info))
            if info.filename == "mimetype":
                zout.writestr(info, data, compress_type=zipfile.ZIP_STORED)
            else:
                zout.writestr(info, data, compress_type=info.compress_type)
