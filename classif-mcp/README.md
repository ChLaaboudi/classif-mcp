# classif-mcp

An MCP server that links statistical classifications across the world, EU and
national levels, as in Figure 1 of the NACE Rev. 2.1 methodology. The aim is
that one question returns the answer at each level.

It queries each node at its source and reports, for every link, where that link
is stated.

| Level    | Source                                                    | How it is reached                                |
|----------|-----------------------------------------------------------|--------------------------------------------------|
| World    | Caliper (FAO), for ISIC and CPC as published there        | SPARQL endpoint `caliper`                        |
| World    | UNSD files, for ISIC Rev. 5, CPC Ver. 3.0 and correspondences | converted with `classif-unsd`, endpoint `unsd` (local) |
| EU       | Cellar / EU Vocabularies, for NACE, CPA, PRODCOM, CN and HS | the EUR-Lex Cellar MCP, or endpoint `cellar` here (off by default) |
| National | INSEE, for NAF and CPF                                    | SPARQL endpoint `insee`                          |
| National | I14Y (Switzerland), for NOGA                              | the existing `i14y-mcp` server, used alongside   |

## Provenance

Every link the server returns says how it was obtained:

- **published**: the link is stated in the RDF of an official endpoint (`skos:*Match`, or an XKOS concept association).
- **converted**: the link is read from a UNSD CSV or Excel file that was converted to SKOS/XKOS. The source file URL is recorded as `dct:source`.
- **derived**: the link is computed by `match_by_code` from the codes, for example NAF 62.10Y → NACE 62.10. It is never an official correspondence.

## Install

Requires Python 3.11 or later.

```
cd classif-mcp
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e .
```

## Convert the UNSD files (one-off)

UNSD publishes these classifications as CSV or Excel, not as linked data.

1. Download the files from https://unstats.un.org/unsd/classifications/Econ.
2. Run `--dry-run` first to check which columns are detected.

```
classif-unsd scheme ISIC_Rev_5_english_structure.csv --id isic-rev5 --label "ISIC Rev. 5" \
  --source-url "https://unstats.un.org/unsd/classifications/Econ/Download/In%20Text/ISIC_Rev_5_english_structure.csv" \
  -o data/isic-rev5.ttl

classif-unsd scheme CPC_Ver_3.0_Structure_30Jun2025.csv --id cpc-v3 --label "CPC Ver. 3.0" \
  --source-url "https://unstats.un.org/unsd/classifications/Econ/Download/In%20Text/CPC_Ver_3.0_Structure_30Jun2025.csv" \
  -o data/cpc-v3.ttl

classif-unsd correspondence ISIC_Rev4_to_ISIC_Rev5_Correspondence_Table-17Jan2025.xlsx \
  --id isic4-isic5 --label "ISIC Rev. 4 - ISIC Rev. 5" \
  --source-base http://classif-mcp.local/unsd/isic-rev4/ \
  --target-base http://classif-mcp.local/unsd/isic-rev5/ \
  --source-url "https://unstats.un.org/unsd/classifications/Econ/tables/ISIC/ISIC_Rev4_to_ISIC_Rev5_Correspondence_Table-17Jan2025.xlsx" \
  -o data/isic4-isic5.ttl
```

**The EU–UN bridge.** Converting a NACE Rev. 2.1 → ISIC Rev. 5 correspondence table creates the link between NACE and ISIC. Use `--source-base http://data.europa.eu/ux2/nace2.1/` so that the links point to the official NACE URIs. Then add the output file to `files` in `endpoints.toml`.

**URIs.** The UNSD items are minted under `http://classif-mcp.local/unsd/`, a local namespace. No URI is minted on UNSD's behalf. If Caliper or UNSD later publish ISIC Rev. 5 as linked data, switch to their URIs.

**PDF-only classifications** (for example EBOPS or the Classification of Business Functions) are not converted automatically.

1. Extract the code and title columns to a CSV. An LLM can help with the extraction, but a person must check the result against the PDF.
2. Run `classif-unsd scheme` on that CSV.

## Run with Claude Desktop

Add the server to `claude_desktop_config.json`, using absolute paths:

```json
{
  "mcpServers": {
    "classif-mcp": {
      "command": "/path/to/classif-mcp/.venv/bin/classif-mcp",
      "env": { "CLASSIF_MCP_CONFIG": "/path/to/classif-mcp/endpoints.toml" }
    }
  }
}
```

To serve it over HTTP instead, for example as a remote connector, set `CLASSIF_MCP_TRANSPORT=http`, plus `HOST` and `PORT` if needed. The server then answers on `/mcp`.

## Tools

| Tool | Purpose |
|------|---------|
| `list_endpoints` | List the configured nodes, with their level and URI prefixes. |
| `list_schemes` | List the classification schemes on one endpoint or all of them, with an optional text filter. |
| `find_concepts` | Search items by label words or by code. Codes are normalised, so `62.10`, `6210` and `62.10Z` are all accepted. |
| `get_concept` | Return the labels, code, parent and notes of one item. |
| `get_hierarchy` | Return all ancestors (`up`) or the direct children (`down`). |
| `get_correspondences` | Return all links of an item, searched on **every** endpoint and in both directions, each with its provenance. |
| `match_by_code` | Return a derived, code-based candidate link. It is marked as such. |
| `run_sparql` | Run a read-only SELECT as an escape hatch. |

## Demo prompt

"Where is computer programming classified at world, EU and national level, for
the 2025 generation of classifications? For each level give the code, label and
URI. Say for each link whether it is published, converted or derived."

## Configuration

Endpoints are declared in `endpoints.toml`:

- To add a national SPARQL endpoint, add one block for it.
- To add more converted files, list them under `files` of a `type = "local"` endpoint.
- Environment variables:
  - `CLASSIF_MCP_TIMEOUT`: request timeout in seconds, default 45.
  - `CLASSIF_MCP_MAX_ROWS`: maximum rows per query, default 200.

## Status and limits (v0.1)

**Tested:** all tools, and the converter, over the MCP protocol against sample graphs and sample CSV/XLSX files with the same structure as the UNSD ones.

**Not tested against the live Caliper, INSEE or Cellar endpoints.** Check these points first:

- **Codes.** The server reads codes from `skos:notation`, `dc:identifier` and `dct:identifier`. Confirm that Caliper and INSEE use one of these.
- **URI prefixes.** Confirm the prefixes in `endpoints.toml`, especially Caliper's.
- **Search speed.** On large endpoints, `find_concepts` without a `scheme` can be slow. Pass a scheme URI.
- **Column detection.** The UNSD files could not be inspected here. Run `--dry-run` and override `--code-col` / `--label-col` if needed.
- **Correspondence flags.** `classif-unsd correspondence` keeps only code pairs. UNSD "partial" flags are not carried over.
