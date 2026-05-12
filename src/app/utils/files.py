# src/app/utils/files.py
"""Normalize file inputs for the Gemini client.

The underlying ``gemini-webapi`` library decides the upload MIME type from
the filename extension only — for in-memory bytes without an explicit
``filename`` it falls back to ``application/octet-stream``, which Google's
upload endpoint rejects for media (images, video, audio, PDF, ...).

To work around that, every byte payload we accept is wrapped in a
``FileBlob`` that carries the original (or synthesized) filename, and
``materialize_files`` writes each blob to a tempdir with that filename
before the call so ``mimetypes.guess_type`` can identify it. The tempdir
is removed when the request finishes.
"""
from __future__ import annotations

import base64
import binascii
import mimetypes
import shutil
import tempfile
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, AsyncIterator, List, Optional, Tuple, Union
from urllib.parse import urlparse

import httpx
from fastapi import HTTPException, Request, UploadFile

from app.config import CONFIG


def get_max_upload_size() -> int:
    """Maximum allowed total upload bytes per request; 0 disables the check."""
    try:
        mb = CONFIG.getint("Server", "max_upload_size_mb", fallback=100)
    except (ValueError, TypeError):
        mb = 100
    return max(0, mb) * 1024 * 1024


def check_content_length(request: Request, max_bytes: int) -> None:
    if not max_bytes:
        return
    cl = request.headers.get("content-length")
    if cl and cl.isdigit() and int(cl) > max_bytes:
        raise HTTPException(
            status_code=413,
            detail=(
                f"request body too large: {cl} bytes exceeds "
                f"max_upload_size_mb={max_bytes // (1024 * 1024)}"
            ),
        )


def _entry_size(entry: Any) -> int:
    if isinstance(entry, FileBlob):
        return len(entry.data)
    if isinstance(entry, (bytes, bytearray)):
        return len(entry)
    return 0  # local Path entries aren't user uploads


def enforce_total_size(files: Optional[List[Any]], max_bytes: int) -> None:
    if not files or not max_bytes:
        return
    total = sum(_entry_size(f) for f in files)
    if total > max_bytes:
        raise HTTPException(
            status_code=413,
            detail=(
                f"uploaded files total {total} bytes, exceeds "
                f"max_upload_size_mb={max_bytes // (1024 * 1024)}"
            ),
        )


@dataclass
class FileBlob:
    """In-memory file with a known (or synthesized) name."""
    data: bytes
    filename: Optional[str] = None


# What endpoints accumulate and hand to materialize_files.
FileEntry = Union[Path, FileBlob, bytes]


def _ext_for_mime(mime: Optional[str]) -> str:
    if not mime:
        return ".bin"
    return mimetypes.guess_extension(mime.split(";", 1)[0].strip()) or ".bin"


def _decode_base64(payload: str) -> bytes:
    try:
        return base64.b64decode(payload, validate=False)
    except (binascii.Error, ValueError) as e:
        raise HTTPException(status_code=400, detail=f"invalid base64 file content: {e}")


def _decode_data_url(s: str) -> Tuple[bytes, Optional[str]]:
    """Decode a ``data:<mime>;base64,<payload>`` URL.

    For non data-URL strings, treat the whole string as a base64 payload.
    Returns ``(bytes, mime or None)``.
    """
    stripped = s.lstrip()
    if stripped.lower().startswith("data:") and "," in stripped:
        header, payload = stripped.split(",", 1)
        # header looks like "data:image/png;base64"
        mime = header[5:].split(";", 1)[0].strip() or None
        return _decode_base64(payload), mime
    return _decode_base64(stripped), None


def _synthesize_filename(prefix: str, mime: Optional[str], idx: int) -> str:
    return f"{prefix}-{idx}{_ext_for_mime(mime)}"


def _resolve_one(entry: Any, idx: int) -> FileEntry:
    if isinstance(entry, (bytes, bytearray)):
        return FileBlob(bytes(entry))
    if isinstance(entry, str):
        # legacy: treat plain string as a local path on the server
        return Path(entry)
    if isinstance(entry, dict):
        b64 = entry.get("content_base64") or entry.get("data")
        if not b64:
            raise HTTPException(
                status_code=400,
                detail="file entry object must contain 'content_base64'",
            )
        data, data_url_mime = _decode_data_url(str(b64))
        filename = entry.get("filename")
        if not filename:
            mime = entry.get("mime_type") or data_url_mime
            filename = _synthesize_filename("upload", mime, idx)
        return FileBlob(data, str(filename))
    raise HTTPException(
        status_code=400,
        detail=f"unsupported file entry type: {type(entry).__name__}",
    )


def resolve_json_files(files: Optional[List[Any]]) -> Optional[List[FileEntry]]:
    """Resolve a JSON ``files`` array (mix of path strings and base64 dicts)."""
    if not files:
        return None
    out = [_resolve_one(f, i) for i, f in enumerate(files)]
    return out or None


def _looks_like_upload(value: Any) -> bool:
    """Detect a FastAPI/Starlette UploadFile robustly.

    Some FastAPI/Starlette version combos surface an UploadFile subclass
    that fails ``isinstance(value, UploadFile)`` against the one imported
    here. Duck-type instead: it's not a plain string, and it has read()
    plus a filename attribute.
    """
    if isinstance(value, str):
        return False
    return (
        hasattr(value, "read")
        and callable(getattr(value, "read"))
        and hasattr(value, "filename")
    )


async def _read_upload(u: Any) -> FileBlob:
    try:
        data = await u.read()
    finally:
        close = getattr(u, "close", None)
        if callable(close):
            try:
                result = close()
                if hasattr(result, "__await__"):
                    await result
            except Exception:
                pass
    filename = getattr(u, "filename", None)
    return FileBlob(data, filename or None)


def _filename_from_url(url: str, mime: Optional[str], idx: int) -> str:
    try:
        path = PurePosixPath(urlparse(url).path)
        name = path.name
    except Exception:
        name = ""
    if name and "." in name:
        return name
    return _synthesize_filename("upload", mime, idx)


async def _fetch_url(url: str, idx: int, max_bytes: int = 0) -> FileBlob:
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=60.0) as c:
            async with c.stream("GET", url) as r:
                r.raise_for_status()
                if max_bytes:
                    cl = r.headers.get("content-length")
                    if cl and cl.isdigit() and int(cl) > max_bytes:
                        raise HTTPException(
                            status_code=413,
                            detail=f"fetched url exceeds max_upload size: {url}",
                        )
                mime = r.headers.get("content-type", "").split(";", 1)[0].strip() or None
                buf = bytearray()
                async for chunk in r.aiter_bytes():
                    buf.extend(chunk)
                    if max_bytes and len(buf) > max_bytes:
                        raise HTTPException(
                            status_code=413,
                            detail=f"fetched url exceeds max_upload size: {url}",
                        )
                return FileBlob(bytes(buf), _filename_from_url(url, mime, idx))
    except httpx.HTTPError as e:
        raise HTTPException(status_code=400, detail=f"failed to fetch file url: {e}")


async def resolve_openai_content_parts(
    messages: List[dict],
    max_bytes: int = 0,
) -> Tuple[List[dict], List[FileEntry]]:
    """Walk OpenAI-style messages with multimodal ``content`` arrays.

    Flattens every list-shaped ``content`` to text and extracts file
    payloads from ``image_url`` / ``input_image`` / ``image`` / ``file``
    parts (data: URLs decoded; http(s):// URLs fetched).
    """
    new_messages: List[dict] = []
    files: List[FileEntry] = []
    counter = 0
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            new_messages.append(msg)
            continue
        text_parts: List[str] = []
        for part in content:
            if isinstance(part, str):
                text_parts.append(part)
                continue
            if not isinstance(part, dict):
                continue
            ptype = part.get("type")
            if ptype in ("text", "input_text") and part.get("text"):
                text_parts.append(str(part["text"]))
            elif ptype in ("image_url", "image", "input_image"):
                url = part.get("image_url")
                if isinstance(url, dict):
                    url = url.get("url")
                if not url:
                    url = part.get("url")
                if not isinstance(url, str):
                    continue
                if url.lower().startswith("data:"):
                    data, mime = _decode_data_url(url)
                    files.append(FileBlob(data, _synthesize_filename("image", mime, counter)))
                elif url.startswith(("http://", "https://")):
                    files.append(await _fetch_url(url, counter, max_bytes))
                else:
                    data, mime = _decode_data_url(url)
                    files.append(FileBlob(data, _synthesize_filename("image", mime, counter)))
                counter += 1
            elif ptype == "file" and isinstance(part.get("file"), dict):
                fdata = part["file"]
                fname = fdata.get("filename")
                if fdata.get("file_data"):
                    data, mime = _decode_data_url(str(fdata["file_data"]))
                    files.append(FileBlob(data, fname or _synthesize_filename("file", mime, counter)))
                elif fdata.get("file_url"):
                    blob = await _fetch_url(str(fdata["file_url"]), counter, max_bytes)
                    if fname:
                        blob.filename = fname
                    files.append(blob)
                counter += 1
        merged = "\n".join(t for t in text_parts if t)
        new_msg = dict(msg)
        new_msg["content"] = merged
        new_messages.append(new_msg)
        enforce_total_size(files, max_bytes)
    return new_messages, files


@asynccontextmanager
async def materialize_files(
    files: Optional[List[FileEntry]],
) -> AsyncIterator[Optional[List[Path]]]:
    """Materialize FileBlobs to a tempdir so the upstream library can
    detect their MIME from the filename extension; clean up on exit.
    """
    if not files:
        yield None
        return

    tmp_dir: Optional[Path] = None
    out: List[Any] = []
    try:
        for i, f in enumerate(files):
            if isinstance(f, Path):
                out.append(f)
                continue
            if isinstance(f, FileBlob):
                data, name = f.data, f.filename or f"upload-{i}.bin"
            elif isinstance(f, (bytes, bytearray)):
                data, name = bytes(f), f"upload-{i}.bin"
            else:
                raise HTTPException(
                    status_code=500, detail=f"unexpected file entry: {type(f).__name__}"
                )
            if tmp_dir is None:
                tmp_dir = Path(tempfile.mkdtemp(prefix="webai-upload-"))
            safe = Path(name).name or f"upload-{i}.bin"
            target = tmp_dir / safe
            # avoid collisions when two uploads share a name
            if target.exists():
                stem, suffix = Path(safe).stem, Path(safe).suffix
                target = tmp_dir / f"{stem}-{i}{suffix}"
            target.write_bytes(data)
            out.append(target)
        yield out or None
    finally:
        if tmp_dir and tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)


class GeminiCall:
    """Parsed inputs for the Gemini-style endpoints."""

    __slots__ = ("message", "model", "gem", "files")

    def __init__(
        self,
        message: str,
        model: str,
        gem: Optional[str],
        files: Optional[List[FileEntry]],
    ) -> None:
        self.message = message
        self.model = model
        self.gem = gem
        self.files = files


async def parse_gemini_call(
    request: Request, default_model: str = "gemini-3-flash"
) -> GeminiCall:
    """Parse a /gemini, /gemini-chat or /translate request body.

    Accepts ``application/json`` (existing schema, ``files`` may now mix
    path strings with ``{filename, content_base64, mime_type}`` objects)
    or ``multipart/form-data`` (``message``, ``model``, ``gem`` as form
    fields plus one or more ``files`` uploads).
    """
    max_bytes = get_max_upload_size()
    check_content_length(request, max_bytes)

    ct = (request.headers.get("content-type") or "").lower()
    if ct.startswith("multipart/form-data"):
        form = await request.form()
        # Diagnostic: dump form layout once per request so future weirdness
        # is easy to debug.
        try:
            from app.logger import logger as _log
            _log.debug(
                "multipart form items: "
                + ", ".join(
                    f"{k}={type(v).__name__}"
                    + (
                        f"(filename={getattr(v, 'filename', None)!r})"
                        if not isinstance(v, str)
                        else ""
                    )
                    for k, v in form.multi_items()
                )
            )
        except Exception:
            pass
        message = form.get("message")
        if not isinstance(message, str) or not message:
            raise HTTPException(status_code=400, detail="'message' form field is required")
        model = form.get("model") or default_model
        gem = form.get("gem") or None
        files: List[FileEntry] = []
        for _key, value in form.multi_items():
            if _looks_like_upload(value):
                files.append(await _read_upload(value))
                enforce_total_size(files, max_bytes)
        return GeminiCall(
            message=str(message),
            model=str(model),
            gem=str(gem) if gem else None,
            files=files or None,
        )

    try:
        body = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"invalid JSON body: {e}")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="request body must be a JSON object")
    message = body.get("message")
    if not message or not isinstance(message, str):
        raise HTTPException(status_code=400, detail="'message' is required")
    resolved = resolve_json_files(body.get("files"))
    enforce_total_size(resolved, max_bytes)
    return GeminiCall(
        message=message,
        model=str(body.get("model") or default_model),
        gem=body.get("gem"),
        files=resolved,
    )
