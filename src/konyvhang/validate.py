"""Parse a model response into segments and check it against the source."""

import re

from lxml import etree

BARE_AMP = re.compile(r"&(?!#\d+;|#x[0-9a-fA-F]+;|amp;|lt;|gt;|quot;|apos;)")


class ValidationError(Exception):
    pass


def parse_segments(text: str) -> dict[str, etree._Element]:
    """Extract `<seg id="...">` elements from model output, keyed by id."""
    start, end = text.find("<seg"), text.rfind("</seg>")
    if start < 0 or end < 0:
        raise ValidationError("A válaszban nincs <seg> elem.")
    body = BARE_AMP.sub("&amp;", text[start : end + len("</seg>")])
    try:
        root = etree.fromstring(f"<root>{body}</root>")
    except etree.XMLSyntaxError as e:
        raise ValidationError(f"A válasz nem jól formált XML: {e}") from None
    segs = {}
    for seg in root.iterfind("seg"):
        sid = seg.get("id")
        if sid in segs:
            raise ValidationError(f"A {sid} azonosítójú szegmens többször szerepel.")
        segs[sid] = seg
    return segs


def inline_signature(seg: etree._Element) -> list[tuple[str, str]]:
    return sorted((el.get("n") or "", el.tag) for el in seg.iterdescendants() if isinstance(el.tag, str))


def check(source: dict[str, str], translated: dict[str, etree._Element]) -> None:
    """`source` maps id to source markup. Raises ValidationError listing every problem."""
    problems = []
    missing = [sid for sid in source if sid not in translated]
    extra = [sid for sid in translated if sid not in source]
    if missing:
        problems.append(f"Hiányzó szegmensek: {', '.join(missing)}.")
    if extra:
        problems.append(f"Fölösleges szegmensek: {', '.join(extra)}.")
    for sid, markup in source.items():
        if sid not in translated:
            continue
        expected = inline_signature(etree.fromstring(f"<seg>{markup}</seg>"))
        got = inline_signature(translated[sid])
        if got != expected:
            problems.append(
                f"A {sid} szegmens címkéi eltérnek. Várt: {expected}, kapott: {got}."
            )
    if problems:
        raise ValidationError("\n".join(problems))


def parse_and_check(text: str, source: dict[str, str]) -> dict[str, etree._Element]:
    translated = parse_segments(text)
    check(source, translated)
    return translated
