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

## Deploy as a public connector (Render, free tier)

Everything below is done in a web browser. Nothing is installed locally.

1. **Put the code on GitHub.**
   - Create a GitHub account if you don't have one.
   - Click New repository, name it `classif-mcp`, and make it Public.
   - Choose "uploading an existing file", drag in the *contents* of this folder, and click Commit. The contents are `src`, `data`, `Dockerfile`, `render.yaml`, `pyproject.toml`, `endpoints.toml` and `README.md`.
2. **Deploy on Render.**
   - Sign up at render.com with your GitHub account.
   - Click New → Blueprint, select the `classif-mcp` repository and click Apply.
   - Render builds the Dockerfile and gives the service an address such as `https://classif-mcp-xxxx.onrender.com`.
3. **Check that it runs.** Open `https://classif-mcp-xxxx.onrender.com/health`. It should show `{"status":"ok", ...}`.
4. **Add it to Claude.**
   - Go to Customize › Connectors › + Add › Add custom connector.
   - Enter the address followed by `/mcp`, for example `https://classif-mcp-xxxx.onrender.com/mcp`.
   - Choose "No sign in".

**Notes**
- **Sleeping.** Render's free tier puts the service to sleep after a period without traffic, and the first call then takes about a minute. Before a presentation, open the `/health` address to wake it.
- **Public access.** The address can be called by anyone who has it. The server only reads public classification data and has no write tools.
- **Other hosts.** Any host that runs a Dockerfile works the same way (Railway, Fly.io, Google Cloud Run, Hugging Face Spaces). The server listens on `$PORT` and serves MCP at `/mcp`.

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

## Verified against the live sources (23 October 2026)

**Caliper**
- Holds ISIC Rev. 4, CPC v2.0 and v2.1, COICOP 1999 and the FAO classifications.
- Does **not** hold ISIC Rev. 5 or CPC Ver. 3.0.
- Codes are in `skos:notation`.
- Publishes 996 `skos:closeMatch` links from ISIC Rev. 4 to NACE Rev. 2 (`data.europa.eu/ux2/nace2/...`) and 1,242 from CPC v2.1 to CPA 2.1.
- Holds 31 XKOS correspondence tables.

**Cellar**
- NACE Rev. 2.1 links back to NACE Rev. 2 (`closeMatch`, plus XKOS associations).
- NACE Rev. 2 links to ISIC Rev. 4, CPA, and the Classification of Business Functions.
- There is **no** published link between NACE Rev. 2.1 and ISIC Rev. 5.

**INSEE**
- Publishes NAF 2025 (`http://id.insee.fr/codes/naf2025/naf`), with `skos:notation`, labels in French and English, and XKOS notes.
- There is **no** link from NAF 2025 to NACE, so NAF→NACE is a *derived* link (`match_by_code`).

**UNSD files**, converted with `classif-unsd` into `data/`:
- ISIC Rev. 5: 830 items, with a complete hierarchy.
- CPC Ver. 3.0: 4,586 items.
- ISIC Rev. 4→5: 605 links, each carrying the GSIM type of change.
- For the correspondence, run with `--source-col "ISIC Rev. 4 Code" --target-col "ISIC Rev. 5 Code"`. Its Rev. 4 side points to Caliper's published ISIC Rev. 4 URIs, so the chain connects.

**Endpoint URLs** are given without a trailing slash. The trailing-slash forms on Caliper and INSEE serve the web query UI, not the API.

**One fact to know for any demo.** ISIC Rev. 5 has **no class 62.10**. UNSD broke ISIC Rev. 4 class 6201 down (GSIM RC4.1) into:
- 6211, development of video games, video game software and video game software tools
- 6219, other computer programming activities

NACE Rev. 2.1 kept a single class, 62.10. The codes look alike, but EU 62.10 ≠ UN 62.10. Without a published NACE 2.1–ISIC 5 table, the only traceable path between them goes through the previous versions:

NACE 2.1 62.10 → NACE 2 62.01 → ISIC 4 6201 → ISIC 5 6211 / 6219

## Status and limits (v0.1)

**Tested:** all tools, and the converter, over the MCP protocol against the live Caliper, Cellar and INSEE endpoints, and against the real UNSD files.

Known limits:

- **Search speed.** On large endpoints, `find_concepts` without a `scheme` can be slow. Pass a scheme URI.
- **Following links.** `get_correspondences` returns one hop at a time. The LLM chains the hops, and should say when a path goes through earlier versions.
- **UNSD page.** It was only partly checked for further correspondence tables involving ISIC Rev. 5 or CPC Ver. 3.0. None were found in the part read.
- **Data reuse.** The files in `data/` are conversions of UNSD files. UNSD's terms of use apply to them.
