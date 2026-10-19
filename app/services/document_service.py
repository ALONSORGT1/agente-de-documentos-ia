"""Validate supported documents before transferring them to the search provider."""
from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
from pathlib import PurePath
import re
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile

from app.core.errors import DocumentError

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
SUPPORTED_MIMES = {"application/pdf", "text/plain", "application/vnd.google-apps.document", DOCX_MIME}
_WORD = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _docx_text(raw: bytes, max_bytes: int) -> str:
    """Extract text without executing Word content or expanding unbounded ZIP/XML."""
    try:
        with ZipFile(BytesIO(raw)) as archive:
            entries = archive.infolist()
            names = [entry.filename for entry in entries]
            limit = min(max_bytes * 8, 64 * 1024 * 1024)
            if (len(entries) > 2000 or len(set(names)) != len(names)
                    or sum(entry.file_size for entry in entries) > limit
                    or any(entry.flag_bits & 1 for entry in entries)):
                raise DocumentError("El Word está protegido o supera los límites de lectura segura.")
            if "[Content_Types].xml" not in names or "word/document.xml" not in names:
                raise DocumentError("El archivo no es un documento Word .docx válido.")
            parts = ["word/document.xml"] + sorted(name for name in names if re.fullmatch(
                r"word/(header\d+|footer\d+|footnotes|endnotes)\.xml", name))
            paragraphs = []
            total = 0
            for name in parts:
                with archive.open(name) as stream:
                    xml = stream.read(limit + 1)
                total += len(xml)
                if total > limit or b"\x00" in xml or b"<!DOCTYPE" in xml.upper() or b"<!ENTITY" in xml.upper():
                    raise DocumentError("El Word contiene una estructura XML no admitida.")
                root = ElementTree.fromstring(xml)
                for paragraph in root.iter(_WORD + "p"):
                    chunks = []
                    for node in paragraph.iter():
                        if node.tag == _WORD + "t":
                            chunks.append(node.text or "")
                        elif node.tag == _WORD + "tab":
                            chunks.append("\t")
                        elif node.tag in {_WORD + "br", _WORD + "cr"}:
                            chunks.append("\n")
                    paragraphs.append("".join(chunks))
            text = "\n".join(paragraphs)
    except DocumentError:
        raise
    except (BadZipFile, ElementTree.ParseError, KeyError, OSError, RuntimeError, ValueError):
        raise DocumentError("El Word no se puede leer o está dañado. Guarda una copia como .docx.") from None
    if not text.strip():
        raise DocumentError("El Word no contiene texto extraíble; si solo tiene imágenes, necesita OCR.")
    return text


@dataclass(frozen=True)
class PreparedDocument:
    content: bytes
    filename: str
    content_sha256: str
    text_characters: int


def prepare_document(raw: bytes, filename: str, mime_type: str, max_bytes: int) -> PreparedDocument:
    """Reject binary text and PDFs that require OCR; never send them blindly."""
    if not raw:
        raise DocumentError("El archivo está vacío.")
    if len(raw) > max_bytes:
        raise DocumentError("El archivo supera el tamaño máximo configurado.")
    suffix = ".txt"
    if mime_type == "application/pdf":
        from pypdf import PdfReader

        try:
            reader = PdfReader(BytesIO(raw))
            if reader.is_encrypted and not reader.decrypt(""):
                raise DocumentError("El PDF está protegido con contraseña.")
            text = "\n".join(page.extract_text() or "" for page in reader.pages)
        except DocumentError:
            raise
        except Exception as exc:
            raise DocumentError("El PDF no se puede leer o está dañado.") from exc
        if not text.strip():
            raise DocumentError("El PDF no contiene texto extraíble; necesita OCR.")
        content, suffix = raw, ".pdf"
    elif mime_type == DOCX_MIME:
        text = _docx_text(raw, max_bytes)
        content = text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
    elif mime_type == "text/plain":
        encoding = "utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
        try:
            text = raw.decode(encoding)
        except UnicodeError as exc:
            raise DocumentError("El TXT debe estar codificado como UTF-8 o UTF-16 con BOM.") from exc
        if any(ord(char) < 32 and char not in "\t\r\n\f" for char in text):
            raise DocumentError("El TXT contiene datos binarios o caracteres de control.")
        if not text.strip():
            raise DocumentError("El documento no contiene texto utilizable.")
        content = text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
    else:
        raise DocumentError("Formato de descarga no admitido.")
    if len(content) > max_bytes:
        raise DocumentError("El texto normalizado supera el tamaño máximo configurado.")
    # Keep the actual Drive name in the manifest; only sanitize the upload name.
    name = PurePath(filename.replace("\\", "/")).name
    stem = re.sub(r"[^\w .-]", "_", name.rsplit(".", 1)[0]).strip(" .")[:100] or "documento"
    return PreparedDocument(content, stem + suffix, sha256(content).hexdigest(), len(text.strip()))
