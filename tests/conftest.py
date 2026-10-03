import zipfile

import pytest

CONTAINER = """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>"""

OPF = """<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="uid">test-book</dc:identifier>
    <dc:title>The Test Book</dc:title>
    <dc:language>en</dc:language>
  </metadata>
  <manifest>
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
    <item id="ch1" href="text/ch1.xhtml" media-type="application/xhtml+xml"/>
    <item id="ch2" href="text/ch2.xhtml" media-type="application/xhtml+xml"/>
    <item id="css" href="style.css" media-type="text/css"/>
    <item id="img" href="img/pic.png" media-type="image/png"/>
  </manifest>
  <spine toc="ncx"><itemref idref="nav"/><itemref idref="ch1"/><itemref idref="ch2"/></spine>
</package>"""

NAV = """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="en">
<head><title>Contents</title></head>
<body><nav epub:type="toc"><ol>
  <li><a href="text/ch1.xhtml">Chapter One</a></li>
  <li><a href="text/ch2.xhtml">Chapter <span>Two</span></a></li>
</ol></nav></body></html>"""

NCX = """<?xml version="1.0" encoding="utf-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <docTitle><text>The Test Book</text></docTitle>
  <navMap>
    <navPoint id="p1"><navLabel><text>Chapter One</text></navLabel><content src="text/ch1.xhtml"/></navPoint>
    <navPoint id="p2"><navLabel><text>Chapter Two</text></navLabel><content src="text/ch2.xhtml"/></navPoint>
  </navMap>
</ncx>"""

CH1 = """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="en" lang="en">
<head><title>One</title><link rel="stylesheet" href="../style.css"/></head>
<body>
  <h1 class="title" id="c1">Chapter One</h1>
  <p class="first">It was a <em>dark</em> and&nbsp;stormy night, <a href="#n1" class="noteref"><sup>1</sup></a> said <span class="sc">Tom</span>.</p>
  <div class="scene"><p>Mary looked up.<br/>&#8220;Really?&#8221; she asked &amp; frowned.</p></div>
  <p class="center">* * *</p>
  <blockquote><p>A quote <img src="../img/pic.png" alt="pic"/> here.</p></blockquote>
  <!-- comment -->
  <p id="n1">1. A footnote.</p>
</body></html>"""

CH2 = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" lang="en">
<head><title>Two</title></head>
<body><section><h2>Chapter Two</h2><ul><li>First item</li><li>Second <strong>item</strong></li></ul></section></body></html>"""


def make_epub(path):
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        zf.writestr("META-INF/container.xml", CONTAINER, compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr("OEBPS/content.opf", OPF, compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr("OEBPS/nav.xhtml", NAV, compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr("OEBPS/toc.ncx", NCX, compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr("OEBPS/text/ch1.xhtml", CH1, compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr("OEBPS/text/ch2.xhtml", CH2, compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr("OEBPS/style.css", "p { margin: 0 }", compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr("OEBPS/img/pic.png", b"\x89PNG\r\n\x1a\nfake", compress_type=zipfile.ZIP_STORED)
    return path


@pytest.fixture
def epub_file(tmp_path):
    return make_epub(tmp_path / "book.epub")
