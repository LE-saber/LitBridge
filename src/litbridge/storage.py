"""Profile-scoped SQLite metadata cache and content-addressed artifacts."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
from defusedxml import ElementTree as ET
from litbridge.errors import BridgeError, Code
from litbridge.models import Artifact, Paper, digest, merge_papers, same_work


def validate_content(data: bytes, fmt: str) -> None:
    if fmt == "html":
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(data, 'html.parser')
        body = soup.select_one('article, main, [role="main"]') or soup.body
        if body is None or len(body.get_text(' ', strip=True)) < 20 or soup.select_one('input[type="password"], #captcha, #challenge-form'):
            raise BridgeError(Code.INVALID_CONTENT, 'Local HTML is not a recognizable saved article')
        return
    head = data[:512].lstrip().lower()
    if b"<html" in head or b"<!doctype html" in head:
        raise BridgeError(Code.INVALID_CONTENT, "Received an HTML page instead of full text",
                          action="Check login, entitlement or human verification in the browser")
    if fmt == "pdf":
        if not data.startswith(b"%PDF-") or len(data) < 128 or b"%%EOF" not in data[-4096:]:
            raise BridgeError(Code.INVALID_CONTENT, "Response is not a PDF")
        return
    if fmt != "xml":
        raise BridgeError(Code.UNSUPPORTED, "Only PDF and XML downloads are supported")
    try:
        root = ET.fromstring(data)
    except Exception as exc:
        raise BridgeError(Code.INVALID_CONTENT, "Response is not safe, well-formed XML") from exc
    bodies = [e for e in root.iter() if e.tag.rsplit("}", 1)[-1] in ("body", "originalText")]
    if not bodies or not any(len("".join(e.itertext()).strip()) >= 20 for e in bodies):
        raise BridgeError(Code.INVALID_CONTENT, "XML does not contain a full-text body")


class Store:
    SCHEMA = 1

    def __init__(self, home: Path, profile: str = "default"):
        self.home = home.expanduser().resolve() / digest(profile)[:16]
        self.home.mkdir(parents=True, exist_ok=True)
        self.downloads = self.home / "downloads"
        if self.downloads.is_symlink():
            raise BridgeError(Code.STORAGE, "Downloads directory must not be a symlink")
        self.downloads.mkdir(mode=0o700, exist_ok=True)
        self.db = sqlite3.connect(self.home / "metadata.sqlite", timeout=10)
        self.db.execute("PRAGMA journal_mode=WAL")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, self.SCHEMA):
            self.db.close()
            raise BridgeError(Code.INCOMPATIBLE, "Unsupported database schema; use another data directory")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS papers (id TEXT PRIMARY KEY, data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS aliases (alias TEXT PRIMARY KEY, paper_id TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, expires REAL NOT NULL, data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS artifacts (id TEXT PRIMARY KEY, data TEXT NOT NULL);
        PRAGMA user_version=1;
        """)

    def close(self) -> None:
        self.db.close()

    def get(self, identifier: str) -> Paper | None:
        row = self.db.execute("SELECT data FROM papers WHERE id=?", (identifier,)).fetchone()
        if row is None:
            row = self.db.execute("SELECT p.data FROM aliases a JOIN papers p ON p.id=a.paper_id WHERE a.alias=?",
                                  (identifier,)).fetchone()
        return Paper.model_validate_json(row[0]) if row else None

    def put(self, paper: Paper) -> Paper:
        paper = paper.canonicalized()
        aliases = [paper.id, paper.identity()]
        aliases += [f"{s.provider}:{s.record_id}" for s in paper.sources]
        known: dict[str, Paper] = {}
        for alias in aliases:
            old = self.get(alias)
            if old and same_work(old, paper):
                known[old.id] = old
                paper = merge_papers(old, paper)
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO papers VALUES (?,?)", (paper.id, paper.model_dump_json()))
            for old_id in known:
                if old_id != paper.id:
                    self.db.execute("UPDATE aliases SET paper_id=? WHERE paper_id=?", (paper.id, old_id))
                    self.db.execute("DELETE FROM papers WHERE id=?", (old_id,))
            for alias in set(aliases + [paper.id, paper.identity()] + list(known)):
                prior = self.get(alias)
                if prior and prior.doi and paper.doi and prior.doi != paper.doi:
                    # A bad upstream source ID must not overwrite a different DOI alias.
                    continue
                self.db.execute("INSERT OR REPLACE INTO aliases VALUES (?,?)", (alias, paper.id))
        return paper

    def cache_get(self, key: str):
        row = self.db.execute("SELECT expires,data FROM cache WHERE key=?", (key,)).fetchone()
        if row and row[0] > time.time():
            return json.loads(row[1])
        return None

    def cache_put(self, key: str, data, ttl: float) -> None:
        if ttl <= 0:
            return
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO cache VALUES (?,?,?)",
                            (key, time.time() + ttl, json.dumps(data, ensure_ascii=False)))
            self.db.execute("DELETE FROM cache WHERE expires<=?", (time.time(),))

    def clear_cache(self) -> int:
        with self.db:
            n = self.db.execute("SELECT count(*) FROM cache").fetchone()[0]
            self.db.execute("DELETE FROM cache")
        return n

    def save(self, paper_id: str, provider: str, fmt: str, url: str, data: bytes) -> Artifact:
        if len(data) > 32 * 1024 * 1024:
            raise BridgeError(Code.TOO_LARGE, 'Artifact exceeds 32 MiB')
        validate_content(data, fmt)
        sha = hashlib.sha256(data).hexdigest()
        name = f"{sha}.{fmt}"
        root = self.downloads.resolve()
        if self.downloads.is_symlink() or root.parent != self.home:
            raise BridgeError(Code.STORAGE, "Downloads directory changed outside configured home")
        target = root / name
        if target.is_symlink():
            raise BridgeError(Code.STORAGE, "Artifact target must not be a symlink")
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(dir=root, suffix=".part", delete=False) as f:
                temp_path = Path(f.name)
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_path, target)
        except OSError as exc:
            raise BridgeError(Code.STORAGE, "Could not atomically save downloaded full text") from exc
        finally:
            if temp_path:
                temp_path.unlink(missing_ok=True)
        # Include paper and provider so byte-identical artifacts retain their provenance.
        artifact = Artifact(id="artifact_" + digest(paper_id + provider + sha)[:24],
                            paper_id=paper_id, provider=provider, format=fmt, path=str(target),
                            sha256=sha, size=len(data), source_url=url)
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO artifacts VALUES (?,?)",
                            (artifact.id, artifact.model_dump_json()))
        return artifact

    def read(self, artifact_id: str, offset: int = 0, max_chars: int = 8000) -> dict:
        if offset < 0 or not 1 <= max_chars <= 20000:
            raise BridgeError(Code.INVALID_INPUT, "offset must be nonnegative and max_chars 1..20000")
        row = self.db.execute("SELECT data FROM artifacts WHERE id=?", (artifact_id,)).fetchone()
        if not row:
            raise BridgeError(Code.NOT_FOUND, "Unknown artifact ID")
        artifact = Artifact.model_validate_json(row[0])
        path = Path(artifact.path)
        if path.is_symlink() or path.resolve().parent != self.downloads.resolve():
            raise BridgeError(Code.STORAGE, "Artifact is outside configured downloads directory")
        try:
            if path.stat().st_size != artifact.size or artifact.size > 32 * 1024 * 1024:
                raise BridgeError(Code.INVALID_CONTENT, 'Artifact size changed outside its recorded bound')
            data = path.read_bytes()
        except OSError as exc:
            raise BridgeError(Code.STORAGE, "Artifact file is missing or unreadable") from exc
        if hashlib.sha256(data).hexdigest() != artifact.sha256:
            raise BridgeError(Code.INVALID_CONTENT, "Artifact checksum mismatch")
        if artifact.format != "xml":
            raise BridgeError(Code.UNSUPPORTED, "PDF is downloaded; PDF text extraction is outside MVP")
        root = ET.fromstring(data)
        bodies = [e for e in root.iter() if e.tag.rsplit("}", 1)[-1] in ("body", "originalText")]
        # Use the first full-text body; do not include repeated abstract or references wrappers.
        text = " ".join(" ".join(bodies[0].itertext()).split())
        end = min(len(text), offset + max_chars)
        return {"artifact": artifact.model_dump(), "text": text[offset:end], "offset": offset,
                "total_chars": len(text), "next_offset": end if end < len(text) else None,
                "untrusted_content": True}
