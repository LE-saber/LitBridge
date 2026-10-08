"""Version 1 capability-oriented contracts. Provider results are untrusted data."""
from __future__ import annotations
import hashlib
import re
import unicodedata
from datetime import datetime, timezone
from typing import Any, Literal
from urllib.parse import unquote
from pydantic import BaseModel, ConfigDict, Field, field_validator


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_doi(value: str | None) -> str | None:
    if not value:
        return None
    value = unquote(value.strip())
    value = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi\s*:\s*)", "", value, flags=re.I)
    # Do not strip punctuation from the suffix: it can legally be part of a DOI.
    value = value.strip().lower()
    return value if re.fullmatch(r"10\.\d{4,9}/[^\s\x00-\x1f]+", value) else None


def normalized_text(value: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKC", value).casefold() if c.isalnum())


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Query(Model):
    text: str = Field(min_length=1, max_length=1000)
    limit: int = Field(default=10, ge=1, le=50)
    mode: Literal["simple", "native"] = "simple"
    cursor: str | None = Field(default=None, max_length=4096)

    @field_validator("text")
    @classmethod
    def not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("query must not be blank")
        return value.strip()


class Reference(Model):
    doi: str | None = None
    title: str | None = None
    text: str | None = None
    source_id: str | None = None

    @field_validator("doi", mode="before")
    @classmethod
    def clean_doi(cls, value: Any) -> str | None:
        return normalize_doi(value) if isinstance(value, str) else None


class Source(Model):
    provider: str
    record_id: str
    url: str
    retrieved_at: str = Field(default_factory=now_iso)
    # Small per-source snapshot retains conflicting metadata instead of overwriting it.
    title: str = ""
    doi: str | None = None
    year: int | None = None
    authors: list[str] = Field(default_factory=list)


class Link(Model):
    provider: str
    url: str
    format: Literal["pdf", "xml"]
    access: Literal["open_access", "unknown"] = "unknown"
    evidence: str = "Unverified full-text link"


class Paper(Model):
    id: str = ""
    doi: str | None = None
    title: str
    authors: list[str] = Field(default_factory=list)
    year: int | None = None
    venue: str | None = None
    abstract: str | None = None
    sources: list[Source] = Field(default_factory=list)
    links: list[Link] = Field(default_factory=list)
    references: list[Reference] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)

    @field_validator("doi", mode="before")
    @classmethod
    def clean_doi(cls, value: Any) -> str | None:
        return normalize_doi(value) if isinstance(value, str) else None

    def identity(self) -> str:
        if self.doi:
            return "doi:" + self.doi
        if self.sources:
            s = sorted(self.sources, key=lambda x: (x.provider, x.record_id))[0]
            return f"source:{s.provider}:{s.record_id}"
        return "title:" + normalized_text(self.title)

    def canonicalized(self) -> Paper:
        return self.model_copy(update={"id": "paper_" + digest(self.identity())[:24]})


class ProviderInfo(Model):
    id: str
    name: str
    version: str = "0.1.0"
    protocol: str = "1.0"
    capabilities: list[str]
    state: Literal["ready", "not_configured", "experimental", "planned", "incompatible"] = "ready"
    requirements: list[str] = Field(default_factory=list)
    note: str = ""


class SearchPage(Model):
    provider: str
    papers: list[Paper]
    next_cursor: str | None = None
    total: int | None = None
    query_sent: str
    semantics: str
    warnings: list[str] = Field(default_factory=list)


class Candidate(Model):
    provider: str
    url: str
    format: Literal["pdf", "xml"]
    access: Literal["open_access", "unknown"]
    evidence: str
    # A metadata claim is never reported as a verified institutional entitlement.
    verified: bool = False


class Artifact(Model):
    id: str
    paper_id: str
    provider: str
    format: Literal["pdf", "xml", "html"]
    path: str
    sha256: str
    size: int
    retrieved_at: str = Field(default_factory=now_iso)
    source_url: str


def same_work(a: Paper, b: Paper) -> bool:
    if a.doi and b.doi:
        return a.doi == b.doi
    sa = {(s.provider, s.record_id) for s in a.sources}
    sb = {(s.provider, s.record_id) for s in b.sources}
    if sa & sb:
        return True
    title = normalized_text(a.title)
    # Conservative fallback, intentionally not a fuzzy title match.
    return bool(len(title) >= 20 and title == normalized_text(b.title)
                and a.year is not None and a.year == b.year and a.authors and b.authors
                and normalized_text(a.authors[0]) == normalized_text(b.authors[0]))


def merge_papers(a: Paper, b: Paper) -> Paper:
    if not same_work(a, b):
        raise ValueError("cannot merge incompatible paper identities")
    out = a.model_copy(deep=True)
    for field in ("title", "authors", "year", "venue", "abstract", "doi"):
        old, new = getattr(out, field), getattr(b, field)
        if not old and new:
            setattr(out, field, new)
        elif old and new and old != new and field != "abstract":
            out.conflicts.append(field)
        elif field == "abstract" and new and len(new) > len(old or ""):
            out.abstract = new
    for field, key in (("sources", lambda x: (x.provider, x.record_id)),
                       ("links", lambda x: (x.provider, x.url, x.format)),
                       ("references", lambda x: (x.doi, x.title, x.text, x.source_id))):
        values = {key(v): v for v in getattr(out, field)}
        for value in getattr(b, field):
            values[key(value)] = value
        setattr(out, field, list(values.values()))
    out.conflicts = sorted(set(out.conflicts + b.conflicts))
    return out.canonicalized()
