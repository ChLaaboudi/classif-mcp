"""Live sources for classif-mcp.

1. type = "file" endpoints (tier 2, live): each source file (CSV, TXT, XLSX,
   XLS, JSON) is downloaded from its publisher, converted in memory with the
   same converter as `classif-unsd`, and cached. On every refresh the server
   asks the publisher whether the file changed (ETag / Last-Modified), so an
   unchanged file costs one small request. A new version is accepted only if it
   passes checks (it converts, has the required columns, has at least
   min_items items, and its size did not jump by more than max_change);
   otherwise the last good copy is kept and a warning is reported.

2. Source watch for tier 3 (extracted from PDF): a scheme that records the
   SHA-256 of the PDF it was transcribed from is compared with the PDF as
   currently published; a change is reported with every answer.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import tempfile
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import httpx
import rdflib

from .unsd_to_skos import ConversionError, convert, read_table

log = logging.getLogger("classif_mcp.live")

# rdflib's SPARQL parser is not thread-safe: every parse/query of an in-memory
# graph, in this module and in server.py, holds this lock.
RDF_LOCK = threading.Lock()

CM = "http://classif-mcp.local/ns#"  # namespace of classif-mcp's own bookkeeping properties
UA = {"User-Agent": "classif-mcp/0.2 (statistical classifications MCP server)"}


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


# --------------------------------------------------------------------------- tier 2, live


class LiveSource:
    """One file at a URL, converted with the given options."""

    def __init__(self, spec: dict[str, Any], cache_dir: Path, base_dir: Path):
        self.spec = dict(spec)
        self.url: str = self.spec.pop("url")
        self.mode: str = self.spec.pop("mode", "scheme")
        self.min_items: int = int(self.spec.pop("min_items", 1))
        self.max_change: float = float(self.spec.pop("max_change", 0.25))
        self.required_columns: list[str] = self.spec.pop("required_columns", [])
        self.format: str | None = self.spec.pop("format", None)  # e.g. ".xlsx" when the URL has no extension
        fb = self.spec.pop("fallback", None)
        self.fallback = (base_dir / fb) if fb and not Path(fb).is_absolute() else (Path(fb) if fb else None)
        self.spec.setdefault("source_url", self.url)
        self.spec.setdefault("prov_note", "Converted live by classif-mcp from the file published at the "
                             "source URL. Not an official linked-data publication.")
        self.cache_file = cache_dir / f"{self.spec['id']}.ttl"
        self.etag: str | None = None
        self.last_modified: str | None = None
        self.ttl: str | None = None
        self.n: int | None = None
        self.status = "not loaded"
        self.warning: str | None = None
        self.data_note: str | None = None  # anomaly found in the source data itself

    def _suffix(self) -> str:
        if self.format:
            return self.format if self.format.startswith(".") else "." + self.format
        name = unquote(urlparse(self.url).path).lower()
        for s in (".csv", ".txt", ".xlsx", ".xlsm", ".xls", ".json"):
            if name.endswith(s):
                return s
        return ".csv"

    def refresh(self, client: httpx.Client) -> bool:
        """Check the publisher; convert a new version if there is one. Returns True if the content changed."""
        headers = dict(UA)
        if self.ttl is not None:
            if self.etag:
                headers["If-None-Match"] = self.etag
            if self.last_modified:
                headers["If-Modified-Since"] = self.last_modified
        try:
            r = client.get(self.url, headers=headers)
            if r.status_code == 304:
                self.status = f"live, unchanged at source (checked {_now()})"
                self.warning = None
                return False
            r.raise_for_status()
        except httpx.HTTPStatusError as e:
            return self._fail(f"source answered HTTP {e.response.status_code}")
        except Exception as e:
            return self._fail(f"source unreachable ({type(e).__name__})")

        with tempfile.TemporaryDirectory() as td:  # downloaded file is data, kept apart from code
            f = Path(td) / f"source{self._suffix()}"
            f.write_bytes(r.content)
            try:
                if self.required_columns:
                    header, _ = read_table(f, self.spec.get("sheet"), self.spec.get("no_header", False))
                    missing = [c for c in self.required_columns if c not in header]
                    if missing:
                        return self._fail(f"new version rejected: columns missing {missing}")
                ttl, n, info = convert(f, self.mode, **self.spec)
            except (ConversionError, Exception) as e:
                return self._fail(f"new version rejected: conversion failed ({e})")
        if n < self.min_items:
            return self._fail(f"new version rejected: {n} items, expected at least {self.min_items}")
        if self.n and abs(n - self.n) / self.n > self.max_change:
            return self._fail(f"new version rejected: {self.n} -> {n} items (more than "
                              f"{self.max_change:.0%} change; raise max_change if this is expected)")
        try:
            with RDF_LOCK:
                rdflib.Graph().parse(data=ttl, format="turtle")
        except Exception as e:
            return self._fail(f"new version rejected: invalid Turtle ({e})")
        self.ttl, self.n = ttl, n
        self.data_note = next((l[len("WARNING: "):] for l in info.splitlines() if l.startswith("WARNING")), None)
        self.etag, self.last_modified = r.headers.get("ETag"), r.headers.get("Last-Modified")
        self.status = f"live, fetched {_now()}" + (f", published {self.last_modified}" if self.last_modified else "")
        self.warning = None
        try:
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            self.cache_file.write_text(ttl, encoding="utf-8")
        except OSError:
            pass
        return True

    def _fail(self, why: str) -> bool:
        self.warning = why
        if self.ttl is not None:
            self.status = f"last good copy kept ({why})"
            return False
        for label, f in (("cached copy", self.cache_file), ("fallback file", self.fallback)):
            if f and f.exists():
                self.ttl = f.read_text(encoding="utf-8")
                self.status = f"{label} {f.name} in use ({why})"
                return True
        self.status = f"unavailable ({why})"
        return False


class LiveEndpoint:
    def __init__(self, name: str, cfg: dict[str, Any], config_dir: Path):
        self.name = name
        self.refresh_s = float(cfg.get("refresh_hours", 24)) * 3600
        cache = Path(cfg.get("cache_dir", ".cache"))
        cache = cache if cache.is_absolute() else config_dir / cache
        self.sources = [LiveSource(s, cache / name, config_dir) for s in cfg.get("sources", [])]
        self.graph = rdflib.Graph()
        self.loaded_at = 0.0
        self.refresh_lock = threading.Lock()

    def ensure_fresh(self) -> None:
        if self.loaded_at and time.time() - self.loaded_at < self.refresh_s:
            return
        with self.refresh_lock:
            if self.loaded_at and time.time() - self.loaded_at < self.refresh_s:
                return
            with httpx.Client(timeout=120, follow_redirects=True) as client:
                changed = [s.refresh(client) for s in self.sources]
            if any(changed) or not self.loaded_at:
                g = rdflib.Graph()
                with RDF_LOCK:
                    for s in self.sources:
                        if s.ttl:
                            g.parse(data=s.ttl, format="turtle")
                self.graph = g
            self.loaded_at = time.time()

    def query(self, q: str) -> list[dict[str, str]]:
        self.ensure_fresh()
        with RDF_LOCK:
            return [{str(k): str(v) for k, v in row.asdict().items()} for row in self.graph.query(q)]

    def status(self) -> list[dict[str, Any]]:
        return [{"url": s.url, "id": s.spec.get("id"), "items": s.n, "status": s.status,
                 **({"warning": s.warning} if s.warning else {}),
                 **({"source_data_note": s.data_note} if s.data_note else {})} for s in self.sources]


# --------------------------------------------------------------------------- tier 3, source watch


class SourceWatch:
    """Compare PDFs recorded in a graph (dct:source + cm:sourceSha256) with what is published now."""

    def __init__(self, refresh_hours: float = 24):
        self.refresh_s = refresh_hours * 3600
        self.checked_at = 0.0
        self.results: list[dict[str, str]] = []
        self.lock = threading.Lock()

    def check(self, graph: rdflib.Graph) -> None:
        if self.checked_at and time.time() - self.checked_at < self.refresh_s:
            return
        with self.lock:
            if self.checked_at and time.time() - self.checked_at < self.refresh_s:
                return
            q = f"""SELECT ?scheme ?url ?sha WHERE {{ ?scheme <http://purl.org/dc/terms/source> ?url ;
                    <{CM}sourceSha256> ?sha . }}"""
            with RDF_LOCK:
                rows = [(str(a), str(b), str(c)) for a, b, c in graph.query(q)]
            results = []
            with httpx.Client(timeout=120, follow_redirects=True) as client:
                for scheme, url, sha in rows:
                    try:
                        r = client.get(url, headers=UA)
                        r.raise_for_status()
                        now = hashlib.sha256(r.content).hexdigest()
                        state = ("source PDF unchanged since the transcription check" if now == sha else
                                 "SOURCE PDF CHANGED since the transcription check; re-check needed")
                    except Exception as e:
                        state = f"source PDF could not be checked ({type(e).__name__})"
                    results.append({"scheme": scheme, "source": url, "state": f"{state} (checked {_now()})"})
            self.results = results
            self.checked_at = time.time()

    def summary(self) -> str | None:
        if not self.results:
            return None
        changed = [r for r in self.results if "CHANGED" in r["state"]]
        return "; ".join(r["state"] for r in (changed or self.results[:1]))
