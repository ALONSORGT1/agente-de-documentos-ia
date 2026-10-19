from io import BytesIO
from zipfile import ZipFile, ZIP_DEFLATED

import pytest

from app.core.errors import DocumentError
from app.services.document_service import DOCX_MIME, prepare_document


def word(body, **parts):
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", body)
        for name, content in parts.items():
            archive.writestr(name, content)
    return output.getvalue()


def document(text):
    return '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>' + text + '</w:body></w:document>'


def test_docx_extracts_paragraphs_tables_and_accents_without_losing_source_name():
    raw = word(document('<w:p><w:r><w:t>Información útil</w:t><w:tab/><w:t>2026</w:t></w:r></w:p>'
                        '<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Celda de tabla</w:t></w:r></w:p></w:tc></w:tr></w:tbl>'))
    result = prepare_document(raw, '../Guía.docx', DOCX_MIME, 1024 * 1024)
    assert result.filename == 'Guía.txt'
    assert result.content.decode() == 'Información útil\t2026\nCelda de tabla'


def test_docx_repacked_with_same_text_has_same_hash():
    xml = document('<w:p><w:r><w:t>Texto idéntico</w:t></w:r></w:p>')
    first = prepare_document(word(xml), 'a.docx', DOCX_MIME, 1024 * 1024)
    second = prepare_document(word(xml, **{'docProps/core.xml': '<metadata/>'}), 'a.docx', DOCX_MIME, 1024 * 1024)
    assert first.content_sha256 == second.content_sha256


@pytest.mark.parametrize('raw', [b'not-a-word-file', word('<broken'), word(document('<w:p/>')),
    word('<!DOCTYPE x [<!ENTITY a "secret">]>' + document('<w:p><w:r><w:t>&a;</w:t></w:r></w:p>')),
    word(document('<w:p><w:r><w:t>text</w:t></w:r></w:p>').encode('utf-16'))])
def test_docx_rejects_corrupt_empty_and_entity_documents(raw):
    with pytest.raises(DocumentError):
        prepare_document(raw, 'archivo.docx', DOCX_MIME, 1024 * 1024)


def test_docx_rejects_archive_expansion_and_normalized_text_over_limit():
    raw = word(document('<w:p><w:r><w:t>' + 'a' * 20000 + '</w:t></w:r></w:p>'))
    with pytest.raises(DocumentError):
        prepare_document(raw, 'grande.docx', DOCX_MIME, 1024)


def test_docx_in_incremental_sync_preserves_original_and_reuses_upload(env):
    from app.services import sync_service as sync
    env.drive.items[0].update(mimeType=DOCX_MIME, name='Manual.docx')
    # FakeDrive exports native Google Docs only; DOCX is downloaded verbatim.
    env.drive.bodies[env.drive.items[0]['id']] = word(document('<w:p><w:r><w:t>Manual de operaciones</w:t></w:r></w:p>'))
    env.drive.get_bytes = lambda item: (env.drive.bodies[item['id']], item['name'], item['mimeType'])
    first = env.service.sync()
    assert first['stats']['added'] == 1
    assert first['documents'][0]['name'] == 'Manual.docx'
    assert first['documents'][0]['mime_type'] == DOCX_MIME
    assert env.service.sync()['stats']['unchanged'] == 1
    assert env.client.files.creates == 1
    assert next(iter(env.client.files.records.values())).content == b'Manual de operaciones'


def test_word_temporary_lock_files_are_skipped(env):
    env.drive.items[0].update(mimeType=DOCX_MIME, name='~$Manual.docx')
    result = env.service.sync()
    assert result['stats']['skipped'] == 1
    assert not result['stats']['errors']
    assert not env.drive.downloads and env.client.files.creates == 0


from test_sync_service import env  # Shared isolated provider fixture; no real APIs.
