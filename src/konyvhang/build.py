"""Write translations back into the XHTML files and assemble the Hungarian EPUB."""

import json
import re
import shutil
import subprocess
import zipfile
from collections import defaultdict
from pathlib import Path

from lxml import etree

from . import epub, segment
from .epub import NS
from .workdir import WorkDir

XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"


def label_text(el: etree._Element) -> str:
    return re.sub(r"\s+", " ", "".join(el.itertext())).strip()


def label_elements(zf: zipfile.ZipFile, book: epub.Book) -> dict[str, tuple[etree._ElementTree, list]]:
    """Title, nav links and NCX labels, grouped by file: {path: (tree, [elements])}."""
    out = {}
    opf = epub.parse_xml(zf.read(book.opf_path))
    out[book.opf_path] = (opf, opf.findall(".//dc:title", NS))
    if book.nav_path:
        nav = epub.parse_xml(zf.read(book.nav_path))
        out[book.nav_path] = (nav, [a for a in nav.iterfind(".//x:nav//x:a", NS) if label_text(a)])
    if book.ncx_path:
        ncx = epub.parse_xml(zf.read(book.ncx_path))
        found = ncx.findall(".//ncx:navLabel/ncx:text", NS) + ncx.findall(".//ncx:docTitle/ncx:text", NS)
        out[book.ncx_path] = (ncx, [t for t in found if label_text(t)])
    return out


def label_sources(wd: WorkDir) -> list[tuple[str, str]]:
    with zipfile.ZipFile(wd.source) as zf:
        groups = label_elements(zf, epub.read_book(zf))
    return [(path, label_text(el)) for path, (_, els) in groups.items() for el in els]


def set_lang(tree: etree._ElementTree) -> None:
    root = tree.getroot()
    if root.get("lang") is not None or segment.local(root) == "html":
        root.set("lang", "hu")
    root.set(XML_LANG, "hu")
    for el in root.iter():
        if not isinstance(el.tag, str) or el is root:
            continue
        for attr in ("lang", XML_LANG):
            if el.get(attr) is not None:
                el.set(attr, "hu")


def build(wd: WorkDir, partial: bool = False, log=print) -> Path:
    chunks = wd.load_chunks()
    missing = [c["id"] for c in chunks if c["translation"] is None]
    if missing and not partial:
        raise SystemExit(
            f"{len(missing)} darab még nincs lefordítva (első: {missing[0]}). "
            "Futtasd a translate parancsot, vagy használd a --partial kapcsolót."
        )

    by_file: dict[str, dict[int, str]] = defaultdict(dict)
    for chunk in chunks:
        if chunk["translation"] is None:
            continue
        for seg, markup in zip(chunk["segments"], chunk["translation"], strict=True):
            by_file[seg["file"]][seg["idx"]] = markup

    labels = json.loads(wd.labels_path.read_text(encoding="utf-8")) if wd.labels_path.exists() else {}
    replaced: dict[str, bytes] = {}
    with zipfile.ZipFile(wd.source) as zf:
        book = epub.read_book(zf)
        for path in book.spine:  # every document gets lang="hu", even one without text (cover, title page)
            translations = by_file.get(path, {})
            tree = epub.parse_xml(zf.read(path))
            segs = segment.find_segments(tree)
            for idx, markup in translations.items():
                segment.apply_translation(segs[idx], etree.fromstring(f"<seg>{markup}</seg>"))
            set_lang(tree)
            replaced[path] = epub.serialize(tree)

        for path, (tree, els) in label_elements(zf, book).items():
            for el in els:
                hu = labels.get(label_text(el))
                if hu:
                    for child in list(el):
                        el.remove(child)
                    el.text = hu
            if path == book.opf_path:
                for lang in tree.iterfind(".//dc:language", NS):
                    lang.text = "hu"
            elif path == book.nav_path:
                set_lang(tree)
            replaced[path] = epub.serialize(tree)

    wd.out_dir.mkdir(exist_ok=True)
    out = wd.out_dir / f"{wd.book_id}.hu.epub"
    epub.write_epub(str(wd.source), str(out), replaced)
    log(f"Kész: {out}")

    if shutil.which("epubcheck"):
        result = subprocess.run(["epubcheck", str(out)], capture_output=True, text=True, encoding="utf-8", check=False)
        log(result.stdout.strip().splitlines()[-1] if result.stdout.strip() else result.stderr.strip())
    return out
