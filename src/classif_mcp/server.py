"""classif-mcp: an MCP server over statistical-classification SPARQL endpoints.

Each endpoint (Caliper, INSEE, optionally Cellar, ...) is declared in
endpoints.toml. The tools assume the common SKOS/XKOS model used for
statistical classifications and keep provenance explicit:

  * published   - a link stated in the RDF of an endpoint (skos:*Match,
                  XKOS concept associations)
  * converted   - a link read from a publisher's CSV/Excel/JSON file converted to
                  SKOS/XKOS, live (type = "file") or kept on disk (type = "local")
  * extracted   - transcribed from a PDF with classif-pdf; the source PDF is
                  watched for changes (check_source = true)
  * derived     - a link computed by this server from codes (match_by_code);
                  never an official correspondence
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
import tomllib
from pathlib import Path
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

from .live import RDF_LOCK, LiveEndpoint, RdfStore, SourceWatch

PREFIXES = """PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
PREFIX xkos: <http://rdf-vocabulary.ddialliance.org/xkos#>
PREFIX dc: <http://purl.org/dc/elements/1.1/>
PREFIX dct: <http://purl.org/dc/terms/>
"""

# Code literal: classifications use skos:notation, dc:identifier or dct:identifier.
CODE_PATTERN = """
  OPTIONAL {{ {v} skos:notation ?_n1 }}
  OPTIONAL {{ {v} dc:identifier ?_n2 }}
  OPTIONAL {{ {v} dct:identifier ?_n3 }}
  BIND(COALESCE(?_n1, ?_n2, ?_n3) AS {out})
"""

MATCH_PROPS = ["exactMatch", "closeMatch", "broadMatch", "narrowMatch", "relatedMatch"]

WRITE_KEYWORDS = re.compile(
    r"\b(INSERT|DELETE|LOAD|CLEAR|CREATE|DROP|COPY|MOVE|ADD)\b", re.IGNORECASE
)

# --------------------------------------------------------------------------- config


CONFIG_PATH = Path(
    os.environ.get("CLASSIF_MCP_CONFIG", Path(__file__).resolve().parents[2] / "endpoints.toml")
)


def _load_config() -> dict[str, dict[str, Any]]:
    with open(CONFIG_PATH, "rb") as f:
        raw = tomllib.load(f).get("endpoints", {})
    return {name: cfg for name, cfg in raw.items() if cfg.get("enabled", True)}


ENDPOINTS = _load_config()
logging.getLogger("httpx").setLevel(logging.WARNING)

# type = "local": Turtle files on disk.  type = "file": files fetched live from
# their publisher (live.LiveEndpoint).  A local endpoint with check_source = true
# also compares the PDFs it was transcribed from with what is published now.
_LOCAL_GRAPHS: dict[str, Any] = {}
_LOCAL_INIT = threading.Lock()
_LIVE = {n: LiveEndpoint(n, c, CONFIG_PATH.parent) for n, c in ENDPOINTS.items() if c.get("type") == "file"}
_WATCH = {n: SourceWatch(float(c.get("refresh_hours", 24)))
          for n, c in ENDPOINTS.items() if c.get("type") == "local" and c.get("check_source")}


def _local_graph(name: str):
    """Load (once) the Turtle files of a type="local" endpoint into memory."""
    with _LOCAL_INIT:
        if name not in _LOCAL_GRAPHS:
            g = RdfStore()
            with RDF_LOCK:
                for f in ENDPOINTS[name].get("files", []):
                    p = Path(f)
                    if not p.is_absolute():
                        p = CONFIG_PATH.parent / p
                    if p.exists():
                        g.load_turtle(path=p)
            _LOCAL_GRAPHS[name] = g
    return _LOCAL_GRAPHS[name]


def _local_query(name: str, query: str) -> list[dict[str, str]]:
    g = _local_graph(name)
    if name in _WATCH:
        try:
            _WATCH[name].check(g)
        except Exception:  # a failed watch must never block answers
            pass
    return g.select(query)


def _provenance(name: str) -> str:
    """Provenance label of an endpoint, with its live state where there is one."""
    base = ENDPOINTS[name].get("provenance", "published")
    if name in _LIVE:
        # one short state per source: live, or the copy in use and why (details: list_endpoints)
        parts = []
        for s in _LIVE[name].status():
            st = s["status"]
            parts.append(f"{s['id']}: live" if st.startswith("live") else f"{s['id']}: {st}")
        if all(p.endswith(": live") for p in parts):
            return f"{base} [live from source]"
        return f"{base} [{'; '.join(parts)}]"
    if name in _WATCH and _WATCH[name].summary():
        return f"{base} [{_WATCH[name].summary()}]"
    return base
TIMEOUT = float(os.environ.get("CLASSIF_MCP_TIMEOUT", "45"))
MAX_ROWS = int(os.environ.get("CLASSIF_MCP_MAX_ROWS", "200"))

mcp = FastMCP("classif-mcp")

# --------------------------------------------------------------------------- helpers


def _lit(text: str) -> str:
    """Escape a user string for use inside a SPARQL string literal."""
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def _iri(uri: str) -> str:
    # http(s) URIs, plus other absolute IRIs that some publishers use as identifiers
    # (INSEE's survey lists: "eap:produit/3092120000", "emb:produit/3091Z1R0012")
    if not re.match(r"^(https?://|[a-z][a-z0-9+.-]*:)[^\s<>\"{}|\\^`]+$", uri):
        raise ValueError(f"Not a valid IRI: {uri!r}")
    return f"<{uri}>"


def _norm_code(code: str) -> str:
    return re.sub(r"[^0-9A-Za-z]", "", code or "").upper()


def _endpoint_for_uri(uri: str) -> str | None:
    """The endpoint whose longest uri_prefix matches (so .../unsd/sitc wins over .../unsd/)."""
    best, best_len = None, -1
    for name, cfg in ENDPOINTS.items():
        for p in cfg.get("uri_prefixes", []):
            if uri.startswith(p) and len(p) > best_len:
                best, best_len = name, len(p)
    return best


def _scope(name: str) -> list[str]:
    """Schemes an endpoint is restricted to (config key "schemes"); [] = no restriction."""
    return ENDPOINTS.get(name, {}).get("schemes", [])


def _scope_values(name: str, var: str) -> str:
    s = _scope(name)
    return f"VALUES {var} {{ {' '.join(_iri(u) for u in s)} }}" if s else ""


def _check_scope(scheme: str) -> None:
    ep = _endpoint_for_uri(scheme)
    if ep and _scope(ep) and scheme not in _scope(ep):
        raise ValueError(f"{scheme} is outside the classifications served here. "
                         f"Schemes of {ep}: {_scope(ep)}")


def _in_scope(uri: str, scheme: str = "") -> bool:
    """Is a link target inside the configured network? Unknown publishers are out;
    a known scheme outside an endpoint's allowlist is out."""
    ep = _endpoint_for_uri(uri)
    if not ep:
        return False
    return not (scheme and _scope(ep) and scheme not in _scope(ep))


def _check_endpoint(name: str) -> None:
    if name not in ENDPOINTS:
        raise ValueError(f"Unknown or disabled endpoint {name!r}. Known: {sorted(ENDPOINTS)}")


async def _sparql(endpoint: str, query: str) -> list[dict[str, str]]:
    """Run a SELECT on one endpoint; return rows as {var: value}."""
    _check_endpoint(endpoint)
    if ENDPOINTS[endpoint].get("type") == "local":
        return await asyncio.to_thread(_local_query, endpoint, query)
    if ENDPOINTS[endpoint].get("type") == "file":
        return await asyncio.to_thread(_LIVE[endpoint].query, query)
    url = ENDPOINTS[endpoint]["url"]
    headers = {
        "Accept": "application/sparql-results+json",
        "User-Agent": "classif-mcp/0.1 (statistical classifications MCP server)",
    }
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
        if len(query) < 1800:
            r = await client.get(url, params={"query": query}, headers=headers)
        else:
            r = await client.post(url, data={"query": query}, headers=headers)
        r.raise_for_status()
        data = r.json()
    return [
        {k: v.get("value", "") for k, v in b.items()}
        for b in data.get("results", {}).get("bindings", [])
    ]


async def _sparql_safe(endpoint: str, query: str) -> tuple[list[dict[str, str]], str | None]:
    try:
        return await _sparql(endpoint, query), None
    except Exception as e:  # report per-endpoint failures instead of failing the tool
        return [], f"{type(e).__name__}: {e}"


def _targets(endpoint: str | None) -> list[str]:
    if endpoint:
        _check_endpoint(endpoint)
        return [endpoint]
    return list(ENDPOINTS)


# --------------------------------------------------------------------------- tools


@mcp.tool()
async def list_endpoints() -> list[dict[str, Any]]:
    """List the configured classification endpoints: level (world / eu / national),
    type (sparql = published RDF, file = converted live from the publisher's file,
    local = converted or extracted copy), provenance, and for live files and
    watched PDFs their current state (fetched, unchanged, fallback, changed ...)."""
    out = []
    for name, cfg in ENDPOINTS.items():
        t = cfg.get("type", "sparql")
        where = (cfg.get("url") if t == "sparql" else
                 [s.get("url") for s in cfg.get("sources", [])] if t == "file" else
                 "local files: " + ", ".join(cfg.get("files", [])))
        item = {"endpoint": name, "label": cfg.get("label", ""), "level": cfg.get("level", ""),
                "country": cfg.get("country"), "type": t, "source": where,
                "provenance": _provenance(name), "uri_prefixes": cfg.get("uri_prefixes", [])}
        if _scope(name):
            item["schemes"] = _scope(name)
        if name in _LIVE:
            item["live_status"] = _LIVE[name].status()
        if name in _WATCH and _WATCH[name].results:
            item["source_watch"] = _WATCH[name].results
        out.append(item)
    return out


@mcp.tool()
async def list_schemes(
    endpoint: str | None = None, text: str | None = None, lang: str = "en", limit: int = 100
) -> dict[str, Any]:
    """List classification schemes (skos:ConceptScheme) on one or all endpoints.

    text: optional case-insensitive filter on the scheme label or URI (e.g. "ISIC", "NAF").
    lang: preferred label language; labels without a language tag are also accepted.
    """
    flt = ""
    if text:
        t = _lit(text.lower())
        flt = f'FILTER(CONTAINS(LCASE(STR(?label)), "{t}") || CONTAINS(LCASE(STR(?scheme)), "{t}"))'

    def q(name: str) -> str:
        return f"""{PREFIXES}
SELECT DISTINCT ?scheme ?label WHERE {{
  {_scope_values(name, "?scheme")}
  ?scheme a skos:ConceptScheme .
  OPTIONAL {{ ?scheme skos:prefLabel ?label .
             FILTER(LANGMATCHES(LANG(?label), "{_lit(lang)}") || LANG(?label) = "") }}
  {flt}
}} ORDER BY ?scheme LIMIT {min(limit, MAX_ROWS)}"""
    out: dict[str, Any] = {"schemes": [], "errors": {}}
    names = _targets(endpoint)
    results = await asyncio.gather(*(_sparql_safe(n, q(n)) for n in names))
    for name, (rows, err) in zip(names, results):
        if err:
            out["errors"][name] = err
        for r in rows:
            out["schemes"].append({"endpoint": name, "scheme": r["scheme"], "label": r.get("label", "")})
    return out


@mcp.tool()
async def find_concepts(
    text: str,
    scheme: str | None = None,
    endpoint: str | None = None,
    lang: str = "en",
    limit: int = 25,
) -> dict[str, Any]:
    """Find classification items by label words or by code.

    text: words from the label ("computer programming") or a code ("62.10", "6210", "62.10Z").
    scheme: restrict to one scheme URI (strongly recommended on large endpoints).
    endpoint: restrict to one endpoint; by default all endpoints are searched
              (or the one that publishes `scheme`).
    """
    t = _lit(text.strip().lower())
    code = _lit(_norm_code(text))
    if scheme and not endpoint:
        endpoint = _endpoint_for_uri(scheme)
    if scheme:
        _check_scope(scheme)
    code_filter = (
        f'|| UCASE(REPLACE(STR(?code), "[^0-9A-Za-z]", "")) = "{code}"' if code else ""
    )

    def q(name: str) -> str:
        scheme_clause = (f"BIND({_iri(scheme)} AS ?scheme) ?c skos:inScheme ?scheme ." if scheme else
                         f"{_scope_values(name, '?scheme')} ?c skos:inScheme ?scheme ." if _scope(name) else
                         "OPTIONAL { ?c skos:inScheme ?scheme }")
        return f"""{PREFIXES}
SELECT DISTINCT ?c ?code ?label ?scheme WHERE {{
  {scheme_clause}
  ?c skos:prefLabel ?label .
  FILTER(LANGMATCHES(LANG(?label), "{_lit(lang)}") || LANG(?label) = "")
  {CODE_PATTERN.format(v="?c", out="?code")}
  FILTER(CONTAINS(LCASE(STR(?label)), "{t}") {code_filter})
}} LIMIT {min(limit, MAX_ROWS)}"""
    out: dict[str, Any] = {"concepts": [], "errors": {}}
    names = _targets(endpoint)
    results = await asyncio.gather(*(_sparql_safe(n, q(n)) for n in names))
    for name, (rows, err) in zip(names, results):
        if err:
            out["errors"][name] = err
        for r in rows:
            out["concepts"].append(
                {"endpoint": name, "uri": r["c"], "code": r.get("code", ""),
                 "label": r.get("label", ""), "scheme": r.get("scheme", ""),
                 "provenance": _provenance(name)}
            )
    return out


@mcp.tool()
async def get_concept(uri: str, endpoint: str | None = None, lang: str = "en") -> dict[str, Any]:
    """Describe one classification item: code, labels, scheme, level, parent,
    children count, explanatory notes (XKOS core/additional content, inclusions,
    exclusions) and any other literal properties in the requested language."""
    endpoint = endpoint or _endpoint_for_uri(uri)
    if not endpoint:
        raise ValueError(f"No configured endpoint publishes {uri}; pass endpoint=...")
    q = f"""{PREFIXES}
SELECT ?p ?o WHERE {{ {_iri(uri)} ?p ?o .
  FILTER(!isLiteral(?o) || LANG(?o) = "" || LANGMATCHES(LANG(?o), "{_lit(lang)}")) }}
LIMIT 500"""
    # XKOS explanatory notes are resources (xkos:ExplanatoryNote) holding xkos:plainText,
    # as published by INSEE and Eurostat and generated by classif-unsd --notes
    qn = f"""{PREFIXES}
SELECT ?p ?text (LANG(?text) AS ?lang) ?nlang WHERE {{
  {_iri(uri)} ?p ?n . ?n xkos:plainText ?text .
  OPTIONAL {{ ?n dct:language ?nlang }}
}} LIMIT 100"""
    rows, nrows = await asyncio.gather(_sparql(endpoint, q), _sparql_safe(endpoint, qn))
    props: dict[str, list[str]] = {}
    for r in rows:
        key = re.split(r"[#/]", r["p"])[-1]
        props.setdefault(key, [])
        if r["o"] not in props[key]:
            props[key].append(r["o"])
    out: dict[str, Any] = {"endpoint": endpoint, "uri": uri, "properties": props}
    notes: dict[str, list[dict[str, str]]] = {}
    for r in nrows[0]:
        nl = (r.get("lang") or str(r.get("nlang", "")).rsplit("/", 1)[-1]).lower()
        notes.setdefault(re.split(r"[#/]", r["p"])[-1], []).append({"lang": nl, "text": r["text"]})
    if notes:  # requested language first; other languages only when it has none
        out["explanatory_notes"] = {
            k: ([n for n in v if n["lang"].startswith(lang.lower())] or v) for k, v in notes.items()}
    return out


@mcp.tool()
async def get_hierarchy(
    uri: str, direction: str = "up", endpoint: str | None = None, lang: str = "en"
) -> dict[str, Any]:
    """Walk the hierarchy of a classification item.

    direction: "up" returns all ancestors (section, division, group, ...);
               "down" returns the direct children.
    """
    endpoint = endpoint or _endpoint_for_uri(uri)
    if not endpoint:
        raise ValueError(f"No configured endpoint publishes {uri}; pass endpoint=...")
    if direction == "up":
        # fixed-length steps (1..8 levels), not skos:broader+ : on large stores such as
        # Cellar the open-ended path times out, fixed paths are answered at once
        steps = [f"{{ {_iri(uri)} {'/'.join(['skos:broader'] * n)} ?x . BIND({n} AS ?d) }}"
                 for n in range(1, 9)]
        pattern = " UNION ".join(steps)
    elif direction == "down":
        pattern = f"{{ ?x skos:broader {_iri(uri)} }} UNION {{ {_iri(uri)} skos:narrower ?x }}"
    else:
        raise ValueError('direction must be "up" or "down"')
    q = f"""{PREFIXES}
SELECT DISTINCT ?x ?code ?label ?d WHERE {{
  {pattern}
  OPTIONAL {{ ?x skos:prefLabel ?label .
             FILTER(LANGMATCHES(LANG(?label), "{_lit(lang)}") || LANG(?label) = "") }}
  {CODE_PATTERN.format(v="?x", out="?code")}
}} ORDER BY ?code LIMIT {MAX_ROWS}"""
    rows = await _sparql(endpoint, q)
    if direction == "up":  # top-down: section, division, group, ... (furthest ancestor first)
        rows.sort(key=lambda r: (-int(r.get("d") or 0), r.get("code", "")))
        seen, uniq = set(), []
        for r in rows:  # an ancestor reached at two depths is listed once
            if r["x"] not in seen:
                seen.add(r["x"])
                uniq.append(r)
        rows = uniq
    return {
        "endpoint": endpoint, "uri": uri, "direction": direction,
        "items": [{"uri": r["x"], "code": r.get("code", ""), "label": r.get("label", "")} for r in rows],
    }


@mcp.tool()
async def get_correspondences(
    uri: str, target_scheme: str | None = None, lang: str = "en"
) -> dict[str, Any]:
    """Return the PUBLISHED links of a classification item, searched on every
    configured endpoint (a link NACE->ISIC may be stored at either end).

    Covers SKOS mapping properties (exactMatch, closeMatch, broadMatch,
    narrowMatch, relatedMatch) in both directions and XKOS concept associations
    (xkos:sourceConcept / xkos:targetConcept). Each link reports the endpoint
    that states it, so provenance is traceable.

    target_scheme: optional scheme URI to keep only links into that scheme.
    """
    i = _iri(uri)
    values = " ".join(f"skos:{p}" for p in MATCH_PROPS)
    scheme_filter = f"FILTER(?ts = {_iri(target_scheme)})" if target_scheme else ""
    q = f"""{PREFIXES}
SELECT DISTINCT ?rel ?dir ?t ?ts ?code ?label ?note WHERE {{
  {{
    VALUES ?p {{ {values} }}
    {{ {i} ?p ?t . BIND("forward" AS ?dir) }} UNION {{ ?t ?p {i} . BIND("inverse" AS ?dir) }}
    BIND(STR(?p) AS ?rel)
  }} UNION {{
    {{ ?a xkos:sourceConcept {i} ; xkos:targetConcept ?t . BIND("forward" AS ?dir) }}
    UNION
    {{ ?a xkos:targetConcept {i} ; xkos:sourceConcept ?t . BIND("inverse" AS ?dir) }}
    BIND("xkos:ConceptAssociation" AS ?rel)
    OPTIONAL {{ ?a <http://www.w3.org/2000/01/rdf-schema#comment> ?note }}
  }}
  OPTIONAL {{ ?t skos:inScheme ?ts }}
  OPTIONAL {{ ?t skos:prefLabel ?label .
             FILTER(LANGMATCHES(LANG(?label), "{_lit(lang)}") || LANG(?label) = "") }}
  {CODE_PATTERN.format(v="?t", out="?code")}
  {scheme_filter}
}} LIMIT {MAX_ROWS}"""
    out: dict[str, Any] = {"uri": uri, "links": [], "errors": {}}
    names = list(ENDPOINTS)
    results = await asyncio.gather(*(_sparql_safe(n, q) for n in names))
    seen: set[tuple[str, str, str]] = set()
    for name, (rows, err) in zip(names, results):
        if err:
            out["errors"][name] = err
        for r in rows:
            # a link stated in both directions (or in SKOS and XKOS) is reported once per endpoint
            key = (name, r["rel"].split("#")[-1], r["t"])
            if key in seen:
                continue
            seen.add(key)
            out["links"].append({
                "provenance": _provenance(name),
                "stated_at": name,
                "relation": r["rel"].split("#")[-1],
                "direction": r["dir"],
                "target": r["t"],
                "target_scheme": r.get("ts", ""),
                "target_code": r.get("code", ""),
                "target_label": r.get("label", ""),
                **({"note": r["note"]} if r.get("note") else {}),
            })
    await _fill_target_labels(out["links"], lang)
    before = len(out["links"])
    out["links"] = [l for l in out["links"] if _in_scope(l["target"], l["target_scheme"])]
    if before > len(out["links"]):
        out["out_of_scope_links_hidden"] = before - len(out["links"])
    return out


async def _fill_target_labels(links: list[dict[str, Any]], lang: str) -> None:
    """A link stated by one publisher often points into another's classification
    (COICOP 2018 -> Caliper's COICOP 1999, ISIC Rev. 4 -> NACE). Fetch the missing
    code, label and scheme of such targets from the endpoint that publishes them."""
    by_ep: dict[str, set[str]] = {}
    for l in links:
        ep = _endpoint_for_uri(l["target"])
        # look up targets with no label, and every target of a scoped endpoint: an item
        # can belong to several schemes (a Cellar HS item is also a CN item), and the
        # scheme reported must be the one in scope
        if ep and (not l["target_label"] or _scope(ep)):
            by_ep.setdefault(ep, set()).add(l["target"])
    if not by_ep:
        return

    async def lookup(ep: str, uris: set[str]):
        values = " ".join(_iri(u) for u in list(uris)[:150])
        q = f"""{PREFIXES}
SELECT ?t ?code ?label ?ts WHERE {{
  VALUES ?t {{ {values} }}
  OPTIONAL {{ ?t skos:prefLabel ?label .
             FILTER(LANGMATCHES(LANG(?label), "{_lit(lang)}") || LANG(?label) = "") }}
  OPTIONAL {{ ?t skos:inScheme ?ts }}
  {CODE_PATTERN.format(v="?t", out="?code")}
}}"""
        rows, _ = await _sparql_safe(ep, q)
        return rows

    found: dict[str, dict[str, str]] = {}
    schemes: dict[str, set[str]] = {}
    for rows in await asyncio.gather(*(lookup(ep, uris) for ep, uris in by_ep.items())):
        for r in rows:
            if r.get("ts"):
                schemes.setdefault(r["t"], set()).add(r["ts"])
            if r.get("label") or r.get("code"):
                found.setdefault(r["t"], r)
    for l in links:
        f = found.get(l["target"])
        if f and not l["target_label"]:
            l["target_label"] = f.get("label", "")
            l["target_code"] = l["target_code"] or f.get("code", "")
        ep = _endpoint_for_uri(l["target"])
        all_s = schemes.get(l["target"], set())
        # in configuration order, so that an item in two schemes is reported in the
        # first listed (HS 2022 before CN 2026 for an HS subheading)
        allowed = [x for x in _scope(ep) if x in all_s] if ep and _scope(ep) else []
        if allowed:  # report the in-scope scheme (e.g. hs2022 rather than cn2026 for an HS item)
            l["target_scheme"] = allowed[0]
        elif all_s and not l["target_scheme"]:
            l["target_scheme"] = sorted(all_s)[0]


@mcp.tool()
async def match_by_code(
    code: str, target_scheme: str, endpoint: str | None = None, lang: str = "en"
) -> dict[str, Any]:
    """DERIVE a candidate link by code, in either direction:

    up    national -> EU: the full code, then shorter prefixes, against the codes of
          `target_scheme` (NAF "62.10Y" -> NACE "62.10"; EAP "3092120000" -> PRODCOM "30.92.12.00");
    down  EU -> national: when nothing matches upwards, the codes of `target_scheme`
          that extend the given code (NACE "62.10" -> NAF "62.10Y";
          PRODCOM "30.92.12.00" -> EAP "3092120000"; NACE "6210" -> NOGA "621000").

    Use it only where the national classification is built on the European one by
    extending its codes, and only when no published link exists. Results are marked
    provenance="derived" and must not be presented as official correspondences.
    """
    _check_scope(target_scheme)
    endpoint = endpoint or _endpoint_for_uri(target_scheme)
    if not endpoint:
        raise ValueError(f"No configured endpoint publishes {target_scheme}; pass endpoint=...")
    norm = _norm_code(code)
    candidates = [norm[:n] for n in range(len(norm), 0, -1)]
    values = " ".join(f'"{_lit(c)}"' for c in candidates)
    q = f"""{PREFIXES}
SELECT DISTINCT ?c ?code ?label ?k WHERE {{
  VALUES ?k {{ {values} }}
  ?c skos:inScheme {_iri(target_scheme)} .
  {CODE_PATTERN.format(v="?c", out="?code")}
  FILTER(UCASE(REPLACE(STR(?code), "[^0-9A-Za-z]", "")) = ?k)
  OPTIONAL {{ ?c skos:prefLabel ?label .
             FILTER(LANGMATCHES(LANG(?label), "{_lit(lang)}") || LANG(?label) = "") }}
}} LIMIT 50"""
    rows = await _sparql(endpoint, q)
    rows.sort(key=lambda r: -len(r["k"]))
    best = rows[0] if rows else None
    out: dict[str, Any] = {
        "provenance": "derived (code prefix match, not a published correspondence)",
        "source_code": code,
        "target_scheme": target_scheme,
        "match": None if not best else {
            "uri": best["c"], "code": best.get("code", ""), "label": best.get("label", ""),
            "matched_on": best["k"], "direction": "up",
        },
    }
    exact = bool(best) and len(best["k"]) == len(norm)
    # down: target codes that extend the given code (national subdivisions); listed
    # even after an exact match (NACE 62.10 -> NAF 62.10, with subclass 62.10Y)
    qd = f"""{PREFIXES}
SELECT DISTINCT ?c ?code ?label WHERE {{
  ?c skos:inScheme {_iri(target_scheme)} .
  {CODE_PATTERN.format(v="?c", out="?code")}
  FILTER(STRSTARTS(UCASE(REPLACE(STR(?code), "[^0-9A-Za-z]", "")), "{_lit(norm)}"))
  OPTIONAL {{ ?c skos:prefLabel ?label .
             FILTER(LANGMATCHES(LANG(?label), "{_lit(lang)}") || LANG(?label) = "") }}
}} LIMIT 30"""
    down, _ = await _sparql_safe(endpoint, qd)
    down = [r for r in down if len(_norm_code(r.get("code", ""))) > len(norm)]
    if down:
        down.sort(key=lambda r: (len(_norm_code(r.get("code", ""))), r.get("code", "")))
        out["extensions"] = [{"uri": r["c"], "code": r.get("code", ""), "label": r.get("label", ""),
                              "direction": "down"} for r in down]
        if not exact:
            # a national item extending the full code is closer than a shorter code above it
            # (PRODCOM 30.92.12.00 -> EAP 3092120000, not the aggregate EAP 3092)
            if out["match"]:
                out["broader_match"] = out["match"]
            r = down[0]
            out["match"] = {"uri": r["c"], "code": r.get("code", ""), "label": r.get("label", ""),
                            "matched_on": norm, "direction": "down"}
    return out


@mcp.tool()
async def run_sparql(endpoint: str, query: str) -> dict[str, Any]:
    """Run a read-only SPARQL SELECT on one endpoint (escape hatch).
    The skos:, xkos:, dc: and dct: prefixes are added automatically.
    Only SELECT queries are accepted; a LIMIT is added if missing."""
    body = re.sub(r"#[^\n]*", "", query)
    head = re.sub(r"(?is)^\s*((PREFIX|BASE)\s+[^\n]*\s*)*", "", body).lstrip()
    if not head.upper().startswith("SELECT") or WRITE_KEYWORDS.search(body):
        raise ValueError("Only read-only SELECT queries are accepted.")
    if not re.search(r"\bLIMIT\s+\d+", body, re.IGNORECASE):
        query = f"{query}\nLIMIT {MAX_ROWS}"
    rows = await _sparql(endpoint, PREFIXES + query)
    return {"endpoint": endpoint, "rows": rows[:MAX_ROWS], "count": len(rows)}


@mcp.custom_route("/health", methods=["GET"])
async def health(request):  # liveness probe for hosting platforms
    from starlette.responses import JSONResponse

    return JSONResponse({"status": "ok", "endpoints": list(ENDPOINTS)})


def main() -> None:
    transport = os.environ.get("CLASSIF_MCP_TRANSPORT", "stdio")
    if transport == "http":
        from mcp.server.transport_security import TransportSecuritySettings

        mcp.settings.host = os.environ.get("HOST", "127.0.0.1")
        mcp.settings.port = int(os.environ.get("PORT", "8000"))
        # The SDK only accepts Host: localhost by default (DNS-rebinding protection),
        # which rejects a public deployment with HTTP 421. Accept the public host
        # names listed in CLASSIF_MCP_ALLOWED_HOSTS (comma-separated), or, for a
        # public bind without that list, turn the check off: the server is
        # read-only and serves public data.
        allowed = [h.strip() for h in os.environ.get("CLASSIF_MCP_ALLOWED_HOSTS", "").split(",") if h.strip()]
        if allowed:
            mcp.settings.transport_security = TransportSecuritySettings(
                enable_dns_rebinding_protection=True,
                allowed_hosts=allowed + [f"{h}:*" for h in allowed] + ["localhost:*", "127.0.0.1:*"],
                allowed_origins=[f"https://{h}" for h in allowed] + ["https://claude.ai"],
            )
        elif mcp.settings.host not in ("127.0.0.1", "localhost", "::1"):
            mcp.settings.transport_security = TransportSecuritySettings(
                enable_dns_rebinding_protection=False
            )
        mcp.run(transport="streamable-http")
    else:
        mcp.run()


if __name__ == "__main__":
    main()
