"""Local reference discovery and DOCX reading; never calls an inference provider."""

import hashlib
from pathlib import Path
from unittest.mock import patch
from zipfile import ZIP_DEFLATED, ZipFile

import git
import pytest

from aider.coders import Coder
from aider.documents import DocumentReadError, read_docx
from aider.io import InputOutput
from aider.models import Model
from aider.terminal_ui import context_status, toolbar


def document(path, body=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    body = body or '<w:p><w:r><w:t>Build Bannerhold in Godot.</w:t></w:r></w:p>'
    with ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", '<w:document xmlns:w="urn:word"><w:body>'
                         + body + '</w:body></w:document>')
    return path


@pytest.fixture
def coder(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    git.Repo.init(tmp_path).close()
    instance = Coder.create(
        Model("gpt-3.5-turbo"), "diff", io=InputOutput(pretty=False),
        map_tokens=0, auto_commits=False, enable_agent_nav=True, enable_l2_paging=True,
    )
    yield instance
    instance.repo.repo.close()


def test_docx_extracts_paragraphs_headings_tables_and_hyperlink_labels(tmp_path):
    body = '''<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>Bannerhold</w:t></w:r></w:p>
      <w:p><w:r><w:t>Build</w:t><w:tab/><w:t>a castle</w:t><w:br/></w:r>
      <w:hyperlink><w:r><w:t>Godot</w:t></w:r></w:hyperlink>
      <w:del><w:r><w:delText>deleted</w:delText></w:r></w:del></w:p>
      <w:tbl><w:tr><w:tc><w:p><w:r><w:t>Gold</w:t></w:r></w:p></w:tc>
      <w:tc><w:p><w:r><w:t>100</w:t></w:r></w:p></w:tc></w:tr></w:tbl>'''
    path = document(tmp_path / "design.docx", body)
    before = path.read_bytes()
    text = read_docx(path)
    assert "# Bannerhold" in text
    assert "Build\ta castle\nGodot" in text
    assert "Gold | 100" in text and "deleted" not in text
    assert "images and layout are not included" in text
    assert path.read_bytes() == before


def test_docx_rejects_invalid_archives_dtd_and_oversized_xml(tmp_path, monkeypatch):
    path = tmp_path / "invalid.docx"
    path.write_bytes(b"not a ZIP")
    with pytest.raises(DocumentReadError):
        read_docx(path)
    with ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", '<!DOCTYPE x [<!ENTITY x "unsafe">]>'
                         '<document><body><p><t>&x;</t></p></body></document>')
    with pytest.raises(DocumentReadError, match="entities"):
        read_docx(path)
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr(
            "word/document.xml", "<document><body><p><t>text</t></p></body></document>"
        )
    with ZipFile(path) as archive:
        part = archive.getinfo("word/document.xml")
        offset = part.header_offset + 30 + len(part.filename.encode()) + len(part.extra)
    corrupt = bytearray(path.read_bytes())
    corrupt[offset] = 0xff  # Invalid DEFLATE block; must not escape the reader as zlib.error.
    path.write_bytes(corrupt)
    with pytest.raises(DocumentReadError):
        read_docx(path)
    path = document(tmp_path / "large.docx")
    monkeypatch.setattr("aider.documents.MAX_XML_BYTES", 20)
    with pytest.raises(DocumentReadError, match="parsing limit"):
        read_docx(path)


@pytest.mark.parametrize("name", ["Bannerhold_Dynasty_Game_Design.docx", "Game Design.DOCX"])
def test_explicit_user_filename_loads_untracked_docx_without_paste_or_extra_confirmation(
    coder, name
):
    path = document(Path(coder.root) / name)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    assert not coder.repo.get_tracked_files()
    with (patch.object(coder.io, "confirm_ask", side_effect=AssertionError("confirmation")),
          patch.object(coder.main_model, "send_completion", side_effect=AssertionError("API"))):
        result = coder.preproc_user_input(f"{name} is the doc")
        assert "read-only reference" in result
        assert "Build Bannerhold in Godot." in coder.get_read_only_files_content()
    assert str(path) in coder.abs_read_only_fnames and not coder.abs_fnames
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    status = context_status(coder)
    assert status["read_only_files"] == 1 and status["source_tokens"]["read-only"] > 0
    assert "RO 1" in "".join(value for _, value in toolbar(status))


def test_workspace_tools_find_and_read_untracked_docs_without_edit_permission(coder):
    path = document(Path(coder.root) / "docs/Game Design.docx")
    Path(".gitignore").write_text("ignored.docx\n", encoding="utf-8")
    document(Path(coder.root) / "ignored.docx")
    nav = coder.agent_navigator
    listing = nav.handle('```aider-nav\n/list_files "*.docx"\n```')
    assert "docs/Game Design.docx" in listing and "ignored.docx" not in listing
    result = nav.handle('```aider-nav\n/read_file "docs/Game Design.docx"\n```')
    assert "read-only reference" in result
    assert str(path) in coder.abs_read_only_fnames
    assert not coder.allowed_to_edit(str(path))
    assert not coder.abs_fnames
    assert "limited to this repository" in nav.execute([("read_file", ["../outside.docx"])])
    assert "ignored" in nav.execute([("read_file", ["ignored.docx"])])
    coder.ignore_mentions.add("docs/Game Design.docx")
    assert "previously declined" in nav.execute([("read_file", ["docs/Game Design.docx"])])
    Path("untracked.py").write_text("private_source = True\n")
    assert "not a tracked" in nav.execute([("read_file", ["untracked.py"])])


def test_ambiguous_document_basename_requires_a_relative_path(coder):
    document(Path(coder.root) / "first/design.docx")
    path = document(Path(coder.root) / "second/design.docx")
    result = coder.preproc_user_input("Read design.docx")
    assert "Ambiguous" in result and not coder.abs_read_only_fnames
    coder.preproc_user_input("Read second/design.docx")
    assert coder.abs_read_only_fnames == {str(path)}


def test_restrictive_user_instruction_does_not_automatically_read_a_document(coder):
    path = document(Path(coder.root) / "private.docx")
    coder.repo.repo.index.add([str(path)])
    with patch.object(coder.io, "confirm_ask", side_effect=AssertionError("unexpected load")):
        coder.preproc_user_input("Do not read private.docx; just explain what tools you have.")
    assert not coder.abs_read_only_fnames and not coder.abs_fnames


def test_add_and_positional_docx_are_read_only_and_writes_are_blocked(coder):
    path = document(Path(coder.root) / "design.docx")
    before = path.read_bytes()
    coder.commands.cmd_add("design.docx")
    assert str(path) in coder.abs_read_only_fnames and not coder.abs_fnames
    with pytest.raises(PermissionError, match="read-only"):
        coder.io.write_text(path, "replacement")
    assert path.read_bytes() == before
    other = Coder.create(
        Model("gpt-3.5-turbo"), "diff", io=InputOutput(pretty=False),
        fnames=[str(path)], map_tokens=0, auto_commits=False,
    )
    try:
        assert str(path) in other.abs_read_only_fnames and not other.abs_fnames
    finally:
        other.repo.repo.close()
    coder.commands.cmd_add("missing.docx")
    assert not Path("missing.docx").exists()
