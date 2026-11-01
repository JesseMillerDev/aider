"""Read-only DOCX text extraction without Word, downloads, or archive extraction."""

import re
import zlib
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile

MAX_XML_BYTES = 32 * 1024 * 1024
NOTICE = (
    "[Read-only DOCX body text: paragraphs and tables; images and layout are not included. "
    "Headers, footnotes and comments are omitted.]"
)


class DocumentReadError(ValueError):
    pass


def is_docx(path):
    return str(path).lower().endswith(".docx")


class _NoDTD(ET.TreeBuilder):
    def doctype(self, *args):
        raise DocumentReadError("DOCX XML document types/entities are not supported.")


def _name(element):
    return element.tag.rsplit("}", 1)[-1]


def _text(element):
    if _name(element) in {"del", "moveFrom"}:
        return ""
    if _name(element) == "t":
        return element.text or ""
    if _name(element) == "tab":
        return "\t"
    if _name(element) in {"br", "cr"}:
        return "\n"
    return "".join(_text(child) for child in element)


def _blocks(element):
    for child in element:
        kind = _name(child)
        if kind in {"del", "moveFrom"}:
            continue
        if kind == "p":
            text = _text(child)
            style = next((n for n in child.iter() if _name(n) == "pStyle"), None)
            if style is not None:
                value = next((v for k, v in style.attrib.items() if k.endswith("}val")), "")
                heading = re.fullmatch(r"Heading\s*([1-6])", value, re.I)
                if heading:
                    text = "#" * int(heading[1]) + " " + text
            if text.strip():
                yield text
        elif kind == "tbl":
            for row in child:
                if _name(row) != "tr":
                    continue
                cells = [" / ".join(_blocks(cell)) for cell in row if _name(cell) == "tc"]
                yield " | ".join(cells)
        else:
            yield from _blocks(child)


def read_docx(path):
    try:
        with ZipFile(path) as archive:
            part = archive.getinfo("word/document.xml")
            if part.file_size > MAX_XML_BYTES:
                raise DocumentReadError("DOCX main XML exceeds the supported 32 MiB parsing limit.")
            with archive.open(part) as source:
                data = source.read(MAX_XML_BYTES + 1)
            if len(data) > MAX_XML_BYTES:
                raise DocumentReadError("DOCX main XML exceeds the supported parsing limit.")
        root = ET.fromstring(data, parser=ET.XMLParser(target=_NoDTD()))
        body = next((node for node in root if _name(node) == "body"), None)
        if body is None:
            raise DocumentReadError("DOCX has no document body.")
        text = "\n\n".join(_blocks(body))
        if not text.strip():
            raise DocumentReadError("DOCX contains no extractable paragraph or table text.")
        return NOTICE + "\n\n" + text + "\n"
    except (BadZipFile, KeyError, ET.ParseError, RuntimeError, RecursionError,
            UnicodeError, zlib.error) as err:
        raise DocumentReadError(f"Cannot extract DOCX text: {type(err).__name__}.") from err
