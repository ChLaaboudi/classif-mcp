"""classif-mcp: an MCP server over statistical-classification SPARQL endpoints.

Each endpoint (Caliper, INSEE, optionally Cellar, ...) is declared in
endpoints.toml. The tools assume the common SKOS/XKOS model used for
statistical classifications and keep provenance explicit:

  * published   - a link stated in the RDF of an endpoint (skos:*Match,
                  XKOS concept associations)
  * converted   - a link read from a UNSD CSV/Excel file converted to SKOS/XKOS
                  by classif-unsd (type = "local" endpoints)
  * derived     - a link computed by this server from codes (match_by_code);
                  never an official correspondence
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import tomllib
from pathlib import Path
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

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
_LOCAL_GRAPHS: dict[str, Any] = {}


def _local_graph(name: str):
    """Load (once) the Turtle files of a type="local" endpoint into memory."""
    if name not in _LOCAL_GRAPHS:
        import rdflib

        g = rdflib.Graph()
        for f in ENDPOINTS[name].get("files", []):
            p = Path(f)
            if not p.is_absolute():
                p = CONFIG_PATH.parent / p
            if p.exists():
                g.parse(p)
        _LOCAL_GRAPHS[name] = g
    return _LOCAL_GRAPHS[name]


def _local_query(name: str, query: str) -> list[dict[str, str]]:
    res = _local_graph(name).query(query)
    return [{str(k): str(v) for k, v in row.asdict().items()} for row in res]
TIMEOUT = float(os.environ.get("CLASSIF_MCP_TIMEOUT", "45"))
MAX_ROWS = int(os.environ.get("CLASSIF_MCP_MAX_ROWS", "200"))

mcp = FastMCP("classif-mcp")

# --------------------------------------------------------------------------- helpers


def _lit(text: str) -> str:
    """Escape a user string for use inside a SPARQL string literal."""
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def _iri(uri: str) -> str:
    if not re.match(r"^https?://[^\s<>\"{}|\\^`]+$", uri):
        raise ValueError(f"Not a valid http(s) URI: {uri!r}")
    return f"<{uri}>"


def _norm_code(code: str) -> str:
    return re.sub(r"[^0-9A-Za-z]", "", code or "").upper()


def _endpoint_for_uri(uri: str) -> str | None:
    for name, cfg in ENDPOINTS.items():
        if any(uri.startswith(p) for p in cfg.get("uri_prefixes", [])):
            return name
    return None


def _check_endpoint(name: str) -> None:
    if name not in ENDPOINTS:
        raise ValueError(f"Unknown or disabled endpoint {name!r}. Known: {sorted(ENDPOINTS)}")


async def _sparql(endpoint: str, query: str) -> list[dict[str, str]]:
    """Run a SELECT on one endpoint; return rows as {var: value}."""
    _check_endpoint(endpoint)
    if ENDPOINTS[endpoint].get("type") == "local":
        return await asyncio.to_thread(_local_query, endpoint, query)
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
    """List the configured classification endpoints, with their level
    (world / eu / national) and the URI prefixes each one publishes."""
    return [
        {
            "endpoint": name,
            "label": cfg.get("label", ""),
            "level": cfg.get("level", ""),
            "country": cfg.get("country"),
            "url": cfg.get("url") or "local files: " + ", ".join(cfg.get("files", [])),
            "type": cfg.get("type", "sparql"),
            "uri_prefixes": cfg.get("uri_prefixes", []),
        }
        for name, cfg in ENDPOINTS.items()
    ]


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
    q = f"""{PREFIXES}
SELECT DISTINCT ?scheme ?label WHERE {{
  ?scheme a skos:ConceptScheme .
  OPTIONAL {{ ?scheme skos:prefLabel ?label .
             FILTER(LANGMATCHES(LANG(?label), "{_lit(lang)}") || LANG(?label) = "") }}
  {flt}
}} ORDER BY ?scheme LIMIT {min(limit, MAX_ROWS)}"""
    out: dict[str, Any] = {"schemes": [], "errors": {}}
    names = _targets(endpoint)
    results = await asyncio.gather(*(_sparql_safe(n, q) for n in names))
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
    scheme_clause = f"?c skos:inScheme {_iri(scheme)} ." if scheme else ""
    if scheme and not endpoint:
        endpoint = _endpoint_for_uri(scheme)
    code_filter = (
        f'|| UCASE(REPLACE(STR(?code), "[^0-9A-Za-z]", "")) = "{code}"' if code else ""
    )
    q = f"""{PREFIXES}
SELECT DISTINCT ?c ?code ?label ?scheme WHERE {{
  {scheme_clause}
  ?c skos:prefLabel ?label .
  FILTER(LANGMATCHES(LANG(?label), "{_lit(lang)}") || LANG(?label) = "")
  OPTIONAL {{ ?c skos:inScheme ?scheme }}
  {CODE_PATTERN.format(v="?c", out="?code")}
  FILTER(CONTAINS(LCASE(STR(?label)), "{t}") {code_filter})
}} LIMIT {min(limit, MAX_ROWS)}"""
    out: dict[str, Any] = {"concepts": [], "errors": {}}
    names = _targets(endpoint)
    results = await asyncio.gather(*(_sparql_safe(n, q) for n in names))
    for name, (rows, err) in zip(names, results):
        if err:
            out["errors"][name] = err
        for r in rows:
            out["concepts"].append(
                {"endpoint": name, "uri": r["c"], "code": r.get("code", ""),
                 "label": r.get("label", ""), "scheme": r.get("scheme", "")}
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
    rows = await _sparql(endpoint, q)
    props: dict[str, list[str]] = {}
    for r in rows:
        key = re.split(r"[#/]", r["p"])[-1]
        props.setdefault(key, [])
        if r["o"] not in props[key]:
            props[key].append(r["o"])
    return {"endpoint": endpoint, "uri": uri, "properties": props}


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
        pattern = f"{_iri(uri)} skos:broader+ ?x ."
    elif direction == "down":
        pattern = f"{{ ?x skos:broader {_iri(uri)} }} UNION {{ {_iri(uri)} skos:narrower ?x }}"
    else:
        raise ValueError('direction must be "up" or "down"')
    q = f"""{PREFIXES}
SELECT DISTINCT ?x ?code ?label WHERE {{
  {pattern}
  OPTIONAL {{ ?x skos:prefLabel ?label .
             FILTER(LANGMATCHES(LANG(?label), "{_lit(lang)}") || LANG(?label) = "") }}
  {CODE_PATTERN.format(v="?x", out="?code")}
}} ORDER BY ?code LIMIT {MAX_ROWS}"""
    rows = await _sparql(endpoint, q)
    if direction == "up":  # top-down: section, division, group, ...
        rows.sort(key=lambda r: (len(_norm_code(r.get("code", ""))), r.get("code", "")))
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
                "provenance": ENDPOINTS[name].get("provenance", "published"),
                "stated_at": name,
                "relation": r["rel"].split("#")[-1],
                "direction": r["dir"],
                "target": r["t"],
                "target_scheme": r.get("ts", ""),
                "target_code": r.get("code", ""),
                "target_label": r.get("label", ""),
                **({"note": r["note"]} if r.get("note") else {}),
            })
    return out


@mcp.tool()
async def match_by_code(
    code: str, target_scheme: str, endpoint: str | None = None, lang: str = "en"
) -> dict[str, Any]:
    """DERIVE a candidate link by code, e.g. NAF "62.10Z" -> NACE "62.10".

    Tries the full code, then shorter prefixes, against the codes of
    `target_scheme`. Use it only where the national classification is built
    on the European one by extending its codes, and only when no published
    link exists. Results are marked provenance="derived" and must not be
    presented as official correspondences.
    """
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
    return {
        "provenance": "derived (code prefix match, not a published correspondence)",
        "source_code": code,
        "target_scheme": target_scheme,
        "match": None if not best else {
            "uri": best["c"], "code": best.get("code", ""), "label": best.get("label", ""),
            "matched_on": best["k"],
        },
    }


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
        mcp.settings.host = os.environ.get("HOST", "127.0.0.1")
        mcp.settings.port = int(os.environ.get("PORT", "8000"))
        mcp.run(transport="streamable-http")
    else:
        mcp.run()


if __name__ == "__main__":
    main()
