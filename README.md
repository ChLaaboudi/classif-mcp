# classif-mcp

An MCP server that links the integrated system of economic classifications across the world, EU and national levels, as in Figure 1 of the NACE Rev. 2.1 methodology (section 1.1). The aim is that one question returns the answer at each level.

It queries each node at its source and reports, for every link, where that link is stated. It is restricted to the classifications in Figure 1: searches, scheme lists and links outside them are not returned.

**Live connector:** `https://classif-mcp.onrender.com/mcp` (add it as a custom connector, with no sign-in). To wake the server before use, open `https://classif-mcp.onrender.com/health`.

| Level | Classifications | Source | Tier | Endpoint |
|---|---|---|---|---|
| World | ISIC Rev. 4, CPC Ver. 2.1 and 2.0 | Caliper (FAO), SPARQL | 1 | `caliper` |
| World | ISIC Rev. 5, CPC Ver. 3.0, SITC Rev. 4, and the tables ISIC Rev. 4 → Rev. 5, CPC 2.1 → HS 2017, HS 2022 ↔ HS 2017 and HS 2022 → SITC Rev. 4 | UNSD files, converted live | 2 | `unsd` |
| World / EU | HS 2022 and HS 2017 (published inside the CN 2026 and CN 2017 graphs) | Cellar, SPARQL | 1 | `cellar` |
| EU | NACE Rev. 2.1 and Rev. 2, CPA 2.2 and 2.1, PRODCOM 2026, CN 2026 | Cellar (EU Vocabularies), SPARQL | 1 | `cellar` |
| National (FR) | NAF 2025, NAF rév. 2, CPF rév. 2.1, and the national product lists for production surveys: EAP 2025 (annual survey) and EMB 2026 (monthly branch surveys) | INSEE, SPARQL | 1 | `insee` |
| National (CH) | NOGA 2025 | FSO code list on I14Y, converted live | 2 | `i14y` |

Every source is read from its publisher, in one of two ways:
- **queried at question time** (tier 1): Caliper, Cellar and INSEE, through their SPARQL endpoints;
- **fetched and converted** (tier 2): the UNSD files, and NOGA through the I14Y API's CSV export. Each file is converted to SKOS/XKOS in memory and checked for a new version every 24 hours.

Previous versions are kept where they carry the published links between generations:

NACE Rev. 2.1 → NACE Rev. 2 → ISIC Rev. 4 → ISIC Rev. 5

## Correspondence tables of the integrated system

These are the tables in UNSD's diagram of the integrated system of economic statistical classifications, in their most recent versions:

| Link | Source | Tier | Links |
|---|---|---|---|
| ISIC Rev. 4 ↔ CPC 2.1 | Caliper, as linked data (`closeMatch` / `exactMatch`). It has the same 2,663 pairs as UNSD's `isic4-cpc21.txt`, so the file is not converted. UNSD's readme notes that ISIC divisions 45–47 are not covered. | 1 | 2,663 |
| ISIC Rev. 4 → Rev. 5 | UNSD table, with the GSIM type of change | 2 | 605 |
| CPC 2.1 → HS 2017 | UNSD `CPC21-HS2017.csv`, with its CPC and HS "partial" flags. It points to Caliper's CPC URIs and to Cellar's HS 2017 items. | 2 | 5,843 |
| HS 2022 ↔ HS 2017 | UNSD correlation table, with the relationship (1:1, n:1, 1:n, n:n). It bridges the CPC table (HS 2017) to the SITC table (HS 2022). | 2 | 15,657 |
| HS 2022 → SITC Rev. 4 | UNSD correlation table, with the relationship. It points to Cellar's HS 2022 items. | 2 | 13,191 |
| ISIC Rev. 4 → NACE Rev. 2, NACE Rev. 2 → CPA, CPA → CN/HS, PRODCOM ↔ CN | Caliper and Cellar, as linked data | 1 | |

**HS items in Cellar.** An HS subheading is identified by the CN item of the same code: the code is padded with zeros and followed by the suffix "80" under the CN graph. For example, HS 2022 9403 60 is `http://data.europa.eu/xsp/cn2026/940360000080`. The converter writes this with `target_suffix` / `source_suffix = "000080"`.

**Checked** (26 October 2026):
- every CPC code (1,617) exists in Caliper;
- every HS 2017 code (5,387) exists in Cellar;
- every SITC code (2,969) exists in the converted SITC Rev. 4;
- every HS 2022 code exists in Cellar except "999999", UN Comtrade's code for commodities not specified, which isn't an HS code.

**Not available from UNSD:** ISIC Rev. 5 ↔ CPC Ver. 3.0, or CPC Ver. 3.0 ↔ HS 2022.

**Example chain:** ISIC Rev. 4 3100 (furniture) → CPC 2.1 38140 → HS 2017 9403.60 → HS 2022 9403 60 → SITC Rev. 4 82159 "Furniture, n.e.s., of wood".

Each endpoint's `schemes` list in `endpoints.toml` defines the scope.

## Three tiers of sources

How a classification is brought in depends on how its publisher releases it:

| Tier | Published as | 5-star open data | How classif-mcp uses it | Provenance shown |
|------|--------------|------------------|-------------------------|------------------|
| 1 | RDF (SKOS/XKOS) with a SPARQL endpoint | 4–5 ★ | Queried live; the publisher's URIs | **published** |
| 2 | CSV, TXT, Excel (.xlsx/.xls) or JSON | 2–3 ★ | Fetched from the publisher and converted in memory, rechecked every `refresh_hours` (`type = "file"`); or converted once with `classif-unsd` (`type = "local"`) | **converted** |
| 3 | PDF only | 1 ★ | Transcribed once with `classif-pdf`. Two independent readings are compared, and a person checks only the rows where they differ or a check fails. The PDF is watched for new versions. No Figure 1 classification needs this tier. | **extracted** |

The tier says how directly a machine can use a classification as published (1 = most direct). The star column refers to Tim Berners-Lee's 5-star scheme for open data: structured data (2 ★), open format (3 ★), URIs (4 ★), linked to other data (5 ★).

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
   - Alternatively, create a Python web service (no Dockerfile), as the live instance does:
     - Build command: `pip install .`
     - Start command: `classif-mcp`
     - Environment variables: `CLASSIF_MCP_TRANSPORT=http`, `HOST=0.0.0.0` and `CLASSIF_MCP_CONFIG=/opt/render/project/src/endpoints.toml`.
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
| `match_by_code` | Return a derived, code-based candidate link, in either direction. Upwards it covers NAF 62.10Y → NACE 62.10 and EAP 3092120000 → PRODCOM 30.92.12.00. Downwards it returns national extensions of a code: PRODCOM 30.92.12.00 → EAP 3092120000, NACE 62.10 → NAF 62.10Y. It is marked as derived. |
| `run_sparql` | Run a read-only SELECT as an escape hatch. |

## Demo prompts

**Activity: computer programming**

"Use only the classif-mcp connector: where is computer programming classified at
world, EU and national level (France and Switzerland), for the 2025 generation of
classifications? For each level give the code, label and URI. Say for each link
whether it is published, converted or derived."

Expected path: NAF 62.10Y and NOGA 621000 → NACE Rev. 2.1 62.10 (derived) → NACE Rev. 2 62.01 → ISIC Rev. 4 6201 → ISIC Rev. 5 6211 / 6219 (breakdown).

**Product: e-bikes**

"Use only the classif-mcp connector: starting from CN 2026 8711 60 10 (electric
bicycles), follow the links to HS and SITC, then to PRODCOM and its French national
lists (EAP, EMB) and to NAF. For each step give the code, label and URI, and say
whether the link is published, converted or derived."

Expected path: CN 2026 8711 60 10 → HS 2022 8711 60 → SITC Rev. 4 78519; CN → PRODCOM 2026 30.92.12.00 → EAP 2025 3092120000 (derived); EMB 2026 3091Z1R0012 → NAF 2025 30.92Y (published).

## Configuration

Endpoints are declared in `endpoints.toml`:

- To add a national SPARQL endpoint, add one block for it.
- To add more converted files, list them under `files` of a `type = "local"` endpoint.
- Environment variables:
  - `CLASSIF_MCP_TIMEOUT`: request timeout in seconds, default 45.
  - `CLASSIF_MCP_MAX_ROWS`: maximum rows per query, default 200.

## Verified against the live sources (26 October 2026)

**Caliper**
- Holds ISIC Rev. 4 and CPC v2.0 and v2.1. Does **not** hold ISIC Rev. 5 or CPC Ver. 3.0.
- Codes are in `skos:notation`.
- Publishes 996 `skos:closeMatch` links from ISIC Rev. 4 to NACE Rev. 2, and 1,242 from CPC v2.1 to CPA 2.1.

**Cellar**
- NACE Rev. 2.1 links back to NACE Rev. 2 (`closeMatch`, plus XKOS associations).
- NACE Rev. 2 links to ISIC Rev. 4 and to CPA.
- CPA, PRODCOM 2026, CN 2026 and HS 2022 are linked to each other.
- There is **no** published link between NACE Rev. 2.1 and ISIC Rev. 5.

**INSEE**
- Publishes NAF 2025, NAF rév. 2 and CPF rév. 2.1, with `skos:notation`, labels in French and English, and XKOS notes.
- Publishes a NAF rév. 2 → CPF rév. 2.1 correspondence.
- CPF rév. 2.1 (5,522 items) is the most recent CPF on rdf.insee.fr.
- There is **no** link from NAF or CPF to NACE or CPA, so NAF→NACE and CPF→CPA are *derived* links (`match_by_code`).
- **EAP 2025** (annual production survey) and **EMB 2026** (monthly branch surveys) are INSEE's national product lists for production statistics, the French counterparts of PRODCOM.
  - EAP codes extend PRODCOM codes by two digits (3092120000 ↔ 30.92.12.00). There is no published link to PRODCOM, so the link is derived.
  - EMB items carry published `skos:relatedMatch` links to NAF 2025. For example, EMB 3091Z1R0012 "vélos … à assistance électrique", still coded under NAF rév. 2 30.91Z (motorcycles), links to NAF 2025 30.92Y (bicycles).
- **Anomalies to report to INSEE:**
  - 5,466 EAP and 794 EMB items have non-http identifiers (`eap:produit/3092120000`, `emb:produit/3091Z1R0012`), apparently unexpanded prefixes. They can't be dereferenced or reused by other publishers. The server accepts and routes them as they are.
  - EAP items point (`skos:definition`) to note identifiers that have no content.

**I14Y (FSO)**
- NOGA 2025 is code list `nogaCode` version 2.0.1, valid from 6 March 2026, status PreferredStandard.
- Its CSV export has 1,845 items: 22 sections, 87 divisions, 287 groups, 651 classes (the same as NACE Rev. 2.1) and 798 Swiss 6-digit types.
- It has an explicit ParentCode, labels in de/fr/it/en, and includes, includes-also and excludes notes.
- There is no link column to NACE, so NOGA→NACE is *derived*: 621000 → 62.10.
- The older code lists NOGA_SECTION … NOGA_TYPE (version 3.x, valid from 2008) describe NOGA 2008 and are not used.

**UNSD files**, converted live:
- ISIC Rev. 5: 830 items, with a complete hierarchy.
- CPC Ver. 3.0: 4,586 items.
- SITC Rev. 4: 4,011 items, from `SITCCodeandDescription.xlsx`, sheet SITC4, with an explicit Parent Code column.
- ISIC Rev. 4→5: 605 links, each carrying the GSIM type of change. Its Rev. 4 side points to Caliper's ISIC Rev. 4 URIs, so the chain connects.
- CPC 2.1→HS 2017, HS 2022↔HS 2017 and HS 2022→SITC Rev. 4: see "Correspondence tables" above.

**Out of scope here** (not in Figure 1): BEC, COICOP, COFOG, COPNI, COPP, EBOPS, the Classification of Business Functions, and the FAO classifications. The converter and `classif-pdf` support them if the scope is widened.

**Anomaly found in the UNSD files**, to report to chl@un.org: in the SITC Rev. 4 sheet, 12 items name divisions 09, 28 and 91 as their parent, but those three division rows are missing.

**Memory.** The in-memory store is Oxigraph, a compiled RDF store with full SPARQL. With every source and table loaded (about 264,000 triples), the server uses about 280 MB, within Render's free tier (512 MB). The same data held in rdflib needed 485 MB. Endpoints load on their first query, which takes about 10 seconds for all UNSD files.

**Labels across publishers.** `get_correspondences` fetches the label and scheme of a target from the endpoint that publishes it. For example, ISIC Rev. 4 → NACE Rev. 2 shows Cellar's NACE label. An item in two schemes, such as an HS subheading that is also a CN item, is reported in the first scheme listed in `endpoints.toml`.

**Hidden links.** Links into classifications outside the scope are not returned; the response counts them as `out_of_scope_links_hidden`. Examples are FAO's forest products classification, the Classification of Business Functions, and FAO mirror URIs. Some of Caliper's CPC 2.1 → CPA 2.1 links also use an older Publications Office URI form (`publications.europa.eu/resource/authority/cpa21/…`). These are hidden, while the same links with `data.europa.eu` URIs are shown.

**Endpoint URLs** are given without a trailing slash. The trailing-slash forms on Caliper and INSEE serve the web query UI, not the API.

**One fact to know for any demo.** ISIC Rev. 5 has **no class 62.10**. UNSD broke ISIC Rev. 4 class 6201 down (GSIM RC4.1) into:
- 6211, development of video games, video game software and video game software tools;
- 6219, other computer programming activities.

NACE Rev. 2.1 (and NOGA 2025, NAF 2025) kept a single class, 62.10. Without a published NACE 2.1–ISIC 5 table, the only traceable path between them goes through the previous versions:

NACE 2.1 62.10 → NACE 2 62.01 → ISIC 4 6201 → ISIC 5 6211 / 6219

## Status and limits (v0.1)

**Tested:**
- all tools over the MCP protocol, both locally and as a remote HTTP server, against the live Caliper, Cellar and INSEE endpoints;
- `classif-unsd` on the real UNSD files: ISIC Rev. 5, CPC Ver. 3.0 and ISIC 4→5 (CSV/XLSX), SITC Rev. 1 (.xls, no header) and EBOPS 2010–CPC 2.0 (.xls, grouped rows);
- `classif-pdf` on the real EBOPS 2010 PDF, including `compare` with a second, simulated reading;
- the full chain ISIC → CPC → HS 2017 → HS 2022 → SITC, over HTTP with concurrent calls;
- the e-bike chain CN 2026 → HS 2022 → SITC Rev. 4 and CN 2026 → PRODCOM 2026 → EAP 2025 / EMB 2026 → NAF 2025;
- live `file` endpoints against unstats.un.org and api.i14y.admin.ch (NOGA 2025). This covered first fetch, conditional refresh (304), a renamed column, a missing file, a size jump, and fallback to the last good copy. The live result is identical to the converted files: 29,869 triples;
- the PDF change watch, against the real EBOPS PDF and against a tampered fingerprint.

**Not tested against real files:**
- JSON input was tested only on a sample in the UN Comtrade format, because comtradeapi.un.org wasn't reachable from the test environment.
- The Claude API call in `classif-pdf draft` was tested with a simulated response. Its request format follows the Messages API.

Known limits:

- **Search speed.** On the remote SPARQL endpoints, `find_concepts` without a `scheme` can take 20–30 seconds. Pass a scheme URI.
- **Following links.** `get_correspondences` returns one hop at a time. The LLM chains the hops, and should say when a path goes through earlier versions.
- **UNSD page.** It was only partly checked for further correspondence tables involving ISIC Rev. 5 or CPC Ver. 3.0. None were found in the part read.
- **Data reuse.** The files in `data/` are conversions of UNSD and FSO files. The publishers' terms of use apply to them.
- **NOGA version.** The I14Y URL names one version of the code list. When the FSO publishes a new one, replace the concept id in `endpoints.toml`.
