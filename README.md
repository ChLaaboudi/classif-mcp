# classif-mcp

An MCP server that links statistical classifications across the world, EU and
national levels, as in Figure 1 of the NACE Rev. 2.1 methodology. The aim is
that one question returns the answer at each level.

It queries each node at its source and reports, for every link, where that link
is stated.

| Level    | Source                                                    | How it is reached                                |
|----------|-----------------------------------------------------------|--------------------------------------------------|
| World    | Caliper (FAO), for ISIC and CPC as published there        | SPARQL endpoint `caliper`                        |
| World    | UNSD files, for ISIC Rev. 5, CPC Ver. 3.0 and correspondences | fetched from UNSD and converted live, endpoint `unsd` |
| World    | UNSD trade classifications: SITC Rev. 4, BEC Rev. 5, HS 2017 (older SITC and BEC versions optional) | converted live, endpoints `unsd_trade` and `unsd_trade_older` |
| World    | UNSD purpose classifications: COICOP 2018 with its explanatory notes, COICOP 2018→1999, COFOG, COPNI, COPP | converted live, endpoint `unsd_purpose` |
| World    | PDF-only classifications such as EBOPS 2010               | transcribed with `classif-pdf`, endpoint `extracted` |
| EU       | Cellar / EU Vocabularies, for NACE, CPA, PRODCOM, CN and HS | SPARQL endpoint `cellar` |
| National | INSEE, for NAF and CPF                                    | SPARQL endpoint `insee`                          |
| National | I14Y (Switzerland), for NOGA                              | the existing `i14y-mcp` server, used alongside   |

## Three tiers of sources

How a classification is brought in depends on how its publisher releases it:

| Tier | Published as | How classif-mcp uses it | Provenance shown |
|------|--------------|-------------------------|------------------|
| 1 | RDF (SKOS/XKOS) with a SPARQL endpoint | Queried live; the publisher's URIs | **published** |
| 2 | CSV, TXT, Excel (.xlsx/.xls) or JSON | Fetched from the publisher and converted in memory, rechecked every `refresh_hours` (`type = "file"`); or converted once with `classif-unsd` (`type = "local"`) | **converted** |
| 3 | PDF only | Transcribed once with `classif-pdf`. Two independent readings are compared, and a person checks only the rows where they differ or a check fails. The PDF is watched for new versions. | **extracted** |

Two rules apply:

1. **Always use the highest tier available.** If a classification exists as RDF, never convert its file. Published URIs are what let links from other publishers connect.
2. **Every answer shows its tier.** A fourth label, **derived**, marks links computed from codes by `match_by_code`, such as NAF 62.10Y → NACE 62.10. A derived link is never an official correspondence.

## Install

Requires Python 3.11 or later.

```
cd classif-mcp
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[tools]"     # ".[tools]" adds .xls and PDF support; the server alone needs only "."
```

## Tier 2: structured files, live

A `type = "file"` endpoint fetches each file from its publisher and converts it in memory. Every `refresh_hours`, it asks the publisher whether the file has changed, using an ETag or Last-Modified header, so an unchanged file costs one small request.

A new version is used only if it passes these checks:
- it converts without error;
- it has the `required_columns`;
- it has at least `min_items` items;
- its size changed by no more than `max_change` (default 25%).

If a check fails, or the source can't be reached, the server keeps serving the last good copy, then the cached copy, then the `fallback` file, in that order. The answer says which copy is in use, and why.

```toml
[endpoints.unsd]
type = "file"
refresh_hours = 24
uri_prefixes = ["http://classif-mcp.local/unsd/"]

[[endpoints.unsd.sources]]
url = "https://unstats.un.org/.../ISIC_Rev_5_english_structure.csv"
id = "isic-rev5"
label = "ISIC Rev. 5"
required_columns = ["ISIC Rev 5 Code", "ISIC Rev 5 Title"]
min_items = 800
fallback = "data/isic-rev5.ttl"
```

Each source accepts the same options as the `classif-unsd` command, written as TOML keys rather than `--options`:
- `mode`, which is `scheme` or `correspondence`;
- `code_col`, `label_col` and `parent_col`;
- `source_col`, `target_col` and `note_col`;
- `source_base` and `target_base`;
- `sheet`, `no_header`, `filter_col`, `filter_value`, `fill_down` and `key_style`;
- `format`, needed when the URL has no file extension.

`list_endpoints` shows each file's state: when it was fetched, its publication date, and any warning.

## Tier 2: convert a file once

The same conversion is also available as a command, to keep a copy on disk. These copies serve as fallbacks, or for files the server can't reach.

1. Download the file, for example from https://unstats.un.org/unsd/classifications/Econ.
2. Run `--dry-run` first to check which columns are detected.

Options for awkward files:

| Problem | Option |
|---------|--------|
| No header row (e.g. SITC Rev. 1 .xls) | `--no-header --code-col 0 --label-col 2` |
| Code given only on the first row of each group (e.g. EBOPS 2010–CPC 2.0) | `--fill-down "EBOPS code"` |
| Dotted codes whose dots matter (EBOPS 3.2.1, where "1.0" ≠ "10") | `--key-style dotted` |
| Parent given in a column, not implied by the code | `--parent-col parent` |
| Several versions in one sheet | `--filter-col Version --filter-value HS2022` |
| JSON (UN Comtrade reference files: `id`, `text`, `parent`) | read directly; keys are detected |
| Fixed-width TXT ("Code", spaces, then "Description", as for COFOG) | detected automatically |
| Aggregate rows that aren't items (e.g. "TOTAL") | `--skip-codes TOTAL` |
| Explanatory-note columns (e.g. COICOP 2018) | `--notes "intro=core,includes=inclusion,alsoIncludes=additional,excludes=exclusion"`. Notes are written as `xkos:ExplanatoryNote` resources with `xkos:plainText`, as INSEE and Eurostat publish them. |

When a file names a parent that it doesn't contain, the converter warns, and the live server reports it as `source_data_note`. The item stays at the top level, as published. A missing level is never invented.

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

## Tier 3: transcribe a PDF-only classification

A PDF classification's content belongs to its custodian, and nobody else can validate it. What can be checked is narrower: that the transcription is a **faithful copy of the PDF**, with codes, titles and hierarchy as printed. `classif-pdf` records exactly that and nothing more.

```
classif-pdf text EBOPS2010_english.pdf -o pages.json      # 1. page texts
classif-pdf draft pages.json -o draftA.csv                # 2. LLM reading via the Claude API (ANTHROPIC_API_KEY)
#   no API key? classif-pdf prompt pages.json -o prompt.txt -> paste into a Claude chat,
#   save the JSON answer, then: classif-pdf import answer.json -o draftA.csv
classif-pdf compare draftA.csv draftB.csv -o draft.csv    # optional: a second, independent LLM reading
classif-pdf check draft.csv pages.json -o review.csv      # 3. checks against the PDF text
#   open review.csv in Excel; only rows with an empty status need a person:
#   set "status" to matches_pdf or reject
classif-pdf finalize review.csv --id ebops2010 --label "EBOPS 2010" \
  --checked-by "Your name" --pdf EBOPS2010_english.pdf --source-url "https://..." \
  --anomalies anomalies.txt -o data/ebops2010.ttl         # 4. Turtle + report for the custodian
```

**Two independent readings**

`check` compares the LLM reading with a rule-based reading of the PDF text. A row is accepted automatically, as "matches_pdf (auto)", when two conditions hold:
- the code is followed in the PDF by exactly that title;
- no check flags the row.

With `compare`, two LLM readings are also compared, and every difference is flagged.

**Flags**
- `READINGS_DIFFER(…)` and `ONLY_IN_ONE_READING`: the two LLM readings disagree.
- `CODE_NOT_IN_PAGE` and `CODE_NOT_IN_PDF`: the code isn't where the reading says it is.
- `TITLE_DIFFERS(score)` and `TITLE_NOT_IN_PDF`: the title doesn't match the PDF text.
- `DUPLICATE_CODE`: the same code appears more than once.
- `PARENT_NOT_FOUND`, `PARENT_SKIPS_LEVEL`, `PARENT_NOT_PREFIX` and `PARENT_MISSING`: the hierarchy doesn't fit the codes.
- `POSSIBLY_MISSING`: a line of the PDF starts with a code that the reading lacks.

**Anomalies in the source are never corrected**

A code printed twice is served twice, as printed, under distinct URIs, with an editorial note on each. Anomalies are listed in `--anomalies`, a ready-to-send report for the custodian. For UNSD classifications, the address is chl@un.org.

**Recorded provenance**

The scheme's note states how the transcription was checked: how many rows automatically, how many by a person, by whom and when. It also states that the content is the custodian's and is not endorsed by them in this form.

`--unchecked` serves a transcription that no person has checked, labelled as such on every answer.

**Change watch**

With `--pdf` and `--source-url`, the Turtle file records the PDF's SHA-256 fingerprint. An endpoint with `check_source = true` compares it every `refresh_hours` with the PDF as currently published. If it changed, every answer says "SOURCE PDF CHANGED since the transcription check; re-check needed".

**Tested on EBOPS 2010** (UNSD, PDF only, 152 items):
- 138 rows were accepted automatically, and 18 were left for a person.
- The open rows were the alternative presentations (3a, 4a), the memorandum item 4.0, a source anomaly, an LLM parent error, and 4 page-header lines.
- The source anomaly was code 3.3.3, printed twice: under Air transport, where 3.2.3 would be expected, and under Other modes of transport.
- The LLM parent error was 3.2.1.a placed under 3.2 instead of 3.2.1. A second reading disagreed, which flagged it again.

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

**Other UNSD classifications**, checked against the real files:

| Classification | Tier | Notes |
|---|---|---|
| COICOP 1999 | 1 (Caliper) | UNSD's TXT is not used. Its range codes such as "01-12" would also collide with "01.1.2". |
| ICATUS 2016 | 1 (Caliper) | |
| HS 2022 | 1 (Cellar) | Published inside the CN graph. |
| SITC Rev. 4 / 3 / 2 | 2 | `SITCCodeandDescription.xlsx`, one sheet per revision, with an explicit Parent Code column. It has the same content as the UN Comtrade JSON. |
| SITC Rev. 1 | 2 | `.xls` without a header row. |
| BEC Rev. 5 / 4 | 2 | `BECCodeandDescription.xlsx`. |
| HS 2017 | 2 | `HSCodeandDescription.xlsx`, sheet HS17. |
| COICOP 2018 | 2 | With intro, includes, also-includes and excludes as XKOS notes. |
| COICOP 2018 → 1999 | 2 | 458 links, pointing to Caliper's COICOP 1999 URIs. All 174 targets exist there. |
| COFOG, COPNI, COPP | 2 | Fixed-width TXT. |
| EBOPS 2010 / 2002, Classification of Business Functions, ISIC Rev. 1, ISIC 1949, SITC 1950 | 3 | PDF only. |

**Anomalies found in the UNSD files**, to report to chl@un.org:
- **SITC Rev. 4 sheet:** 12 items name divisions 09, 28 and 91 as their parent, but those three division rows are missing from the sheet.
- **SITC Rev. 2 sheet:** the first data row is labelled "SIT" instead of "S2" in the Classification column.

**Memory**
- All UNSD data loaded together uses about 200 MB.
- The defaults (`unsd`, `unsd_trade`, `unsd_purpose`) use about 135 MB plus the server itself, which fits Render's free tier (512 MB).
- `unsd_trade_older` is off by default and adds about 50 MB.
- Endpoints load on their first query.

**Labels across publishers.** `get_correspondences` fetches a missing label from the endpoint that publishes the target. For example, COICOP 2018 → 1999 shows Caliper's 1999 label, and ISIC Rev. 4 → NACE Rev. 2 shows Cellar's NACE label.

**Endpoint URLs** are given without a trailing slash. The trailing-slash forms on Caliper and INSEE serve the web query UI, not the API.

**One fact to know for any demo.** ISIC Rev. 5 has **no class 62.10**. UNSD broke ISIC Rev. 4 class 6201 down (GSIM RC4.1) into:
- 6211, development of video games, video game software and video game software tools
- 6219, other computer programming activities

NACE Rev. 2.1 kept a single class, 62.10. The codes look alike, but EU 62.10 ≠ UN 62.10. Without a published NACE 2.1–ISIC 5 table, the only traceable path between them goes through the previous versions:

NACE 2.1 62.10 → NACE 2 62.01 → ISIC 4 6201 → ISIC 5 6211 / 6219

## Status and limits (v0.1)

**Tested:**
- all tools over the MCP protocol, both locally and as a remote HTTP server, against the live Caliper, Cellar and INSEE endpoints;
- `classif-unsd` on the real UNSD files: ISIC Rev. 5, CPC Ver. 3.0 and ISIC 4→5 (CSV/XLSX), SITC Rev. 1 (.xls, no header) and EBOPS 2010–CPC 2.0 (.xls, grouped rows);
- `classif-pdf` on the real EBOPS 2010 PDF, including `compare` with a second, simulated reading;
- live `file` endpoints against unstats.un.org. This covered first fetch, conditional refresh (304), a renamed column, a missing file, a size jump, and fallback to the last good copy. The live result is identical to the converted files: 29,869 triples;
- the PDF change watch, against the real EBOPS PDF and against a tampered fingerprint.

**Not tested against real files:**
- JSON input was tested only on a sample in the UN Comtrade format, because comtradeapi.un.org wasn't reachable from the test environment.
- The Claude API call in `classif-pdf draft` was tested with a simulated response. Its request format follows the Messages API.

Known limits:

- **Search speed.** On large endpoints, `find_concepts` without a `scheme` can be slow. Pass a scheme URI.
- **Following links.** `get_correspondences` returns one hop at a time. The LLM chains the hops, and should say when a path goes through earlier versions.
- **UNSD page.** It was only partly checked for further correspondence tables involving ISIC Rev. 5 or CPC Ver. 3.0. None were found in the part read.
- **Data reuse.** The files in `data/` are conversions of UNSD files. UNSD's terms of use apply to them.
