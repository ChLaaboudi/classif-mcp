"""Convert classification files (CSV, TXT, XLSX, XLS, JSON) to SKOS/XKOS Turtle.

Tier 2 of the classif-mcp logic: a classification that is not published as
RDF but as a structured file is converted once, then served by classif-mcp as
a type = "local" endpoint with the same tools as the SPARQL endpoints.

Provenance is explicit: every generated scheme or correspondence carries
dct:source (the file URL) and a note saying how it was produced. URIs are
minted under a base you choose; the default is a local namespace, so that no
URI is minted on the publisher's behalf.

Usage
-----
  # A classification
  classif-unsd scheme ISIC_Rev_5_english_structure.csv --id isic-rev5 --label "ISIC Rev. 5" \
      --source-url https://unstats.un.org/... -o data/isic-rev5.ttl

  # A correspondence table
  classif-unsd correspondence ISIC_Rev4_to_ISIC_Rev5.xlsx --id isic4-isic5 \
      --label "ISIC Rev. 4 - ISIC Rev. 5" \
      --source-col "ISIC Rev. 4 Code" --target-col "ISIC Rev. 5 Code" \
      --note-col "Type of change in GSIM (blank means no change)" \
      --source-base https://unstats.un.org/classifications/ISIC/rev4/ \
      --target-base http://classif-mcp.local/unsd/isic-rev5/ -o data/isic4-isic5.ttl

Options for awkward files
-------------------------
  --no-header            the file has no header row; give columns by index (0, 1, ...)
  --sheet NAME           Excel sheet (default: first)
  --filter-col/--filter-value
                         keep only rows where that column equals the value
                         (combined workbooks holding several versions)
  --fill-down COLS       comma-separated columns whose blank cells repeat the
                         value above (grouped correspondence tables)
  --parent-col COL       take each item's parent from a column instead of the code
  --key-style dotted     keep separators in codes (EBOPS 3.2.1, COICOP 01.1.1);
                         default "compact" strips them (ISIC 0111, CPC 01111)
  --notes SPEC           explanatory-note columns written as XKOS notes, e.g.
                         "intro=core,includes=inclusion,alsoIncludes=additional,excludes=exclusion"
  Fixed-width TXT files ("Code   Description", as for COFOG) are detected automatically.
  JSON (UN Comtrade reference files: {"results": [{"id", "text", "parent"}]})
                         is read directly; keys are detected, or set them with
                         --code-col / --label-col / --parent-col.

Run with --dry-run first to see which columns and how many rows were detected.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
from pathlib import Path

DEFAULT_BASE = "http://classif-mcp.local/unsd/"


# --------------------------------------------------------------------------- reading


def _cell(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def _read_json(path: Path) -> tuple[list[str], list[list[str]]]:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if isinstance(data, dict):
        lists = [v for v in data.values() if isinstance(v, list) and v and isinstance(v[0], dict)]
        if not lists:
            sys.exit("No list of records found in the JSON file.")
        data = lists[0]
    header = list(dict.fromkeys(k for rec in data for k in rec))
    return header, [[_cell(rec.get(k)) for k in header] for rec in data]


def read_table(path: Path, sheet: str | None = None, no_header: bool = False,
               header_row: int | None = None) -> tuple[list[str], list[list[str]]]:
    """Return (header, rows) from a CSV/TXT, XLSX, XLS or JSON file."""
    suffix = path.suffix.lower()
    if suffix == ".json":
        return _read_json(path)
    if suffix in (".xlsx", ".xlsm"):
        import openpyxl

        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        ws = wb[sheet] if sheet else wb.worksheets[0]
        all_rows = [[_cell(v) for v in r] for r in ws.iter_rows(values_only=True)]
    elif suffix == ".xls":
        import xlrd

        wb = xlrd.open_workbook(str(path))
        ws = wb.sheet_by_name(sheet) if sheet else wb.sheet_by_index(0)
        all_rows = [[_cell(v) for v in ws.row_values(i)] for i in range(ws.nrows)]
    else:
        raw = path.read_bytes()
        for enc in ("utf-8-sig", "cp1252", "latin-1"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        first = next((l for l in text.splitlines() if l.strip()), "")
        if suffix == ".txt" and not re.search(r"[,;\t|]", first) and re.search(r"\S\s{2,}\S", first):
            # fixed-width "Code          Description" layout (UNSD COFOG, COPNI, COPP, COICOP 1999)
            all_rows = [re.split(r"\s{2,}", l.strip(), maxsplit=1) for l in text.splitlines() if l.strip()]
        else:
            # standard CSV quoting (RFC 4180); only the delimiter is detected, from the
            # header line: full dialect sniffing misreads quotes inside long text cells
            counts = {d: first.count(d) for d in (",", ";", "\t", "|")}
            delim = max(counts, key=counts.get) if max(counts.values()) else ","

            class dialect(csv.excel):
                delimiter = delim
            # newline="": keep line breaks inside quoted cells (multi-line notes, as in I14Y exports)
            all_rows = [[c.strip() for c in r] for r in csv.reader(io.StringIO(text, newline=""), dialect)]
    all_rows = [r for r in all_rows if any(r)]
    if no_header:
        width = max(len(r) for r in all_rows)
        return [str(i) for i in range(width)], all_rows
    if header_row:  # 1-based, counted over non-empty rows
        return all_rows[header_row - 1], all_rows[header_row:]
    for i, r in enumerate(all_rows[:20]):  # header = first row mentioning "code"
        if any("code" in c.lower() for c in r):
            return r, all_rows[i + 1:]
    return all_rows[0], all_rows[1:]


def pick(header: list[str], wanted: str | None, patterns: list[str], skip: set[int] = frozenset()) -> int:
    if wanted is not None:
        for i, h in enumerate(header):
            if h.lower() == wanted.lower():
                return i
        if wanted.isdigit() and int(wanted) < len(header):
            return int(wanted)
        sys.exit(f"Column {wanted!r} not found in header {header}")
    for pat in patterns:  # prefer plain code columns over e.g. "Code including Section"
        for pass_ in (0, 1):
            for i, h in enumerate(header):
                if i in skip or not re.search(pat, h, re.IGNORECASE):
                    continue
                if pass_ == 0 and re.search(r"includ", h, re.IGNORECASE):
                    continue
                return i
    sys.exit(f"Could not detect a column matching {patterns} in header {header}; use an override option.")


def prepare_rows(rows: list[list[str]], header: list[str], filter_col: str | None, filter_value: str | None,
                 fill_down: str | None) -> list[list[str]]:
    if filter_col:
        fi = pick(header, filter_col, [])
        rows = [r for r in rows if len(r) > fi and r[fi] == (filter_value or "")]
    if fill_down:
        cols = [pick(header, c.strip(), []) for c in fill_down.split(",") if c.strip()]
        last: dict[int, str] = {}
        out = []
        for r in rows:
            r = list(r) + [""] * (len(header) - len(r))
            for c in cols:
                if r[c]:
                    last[c] = r[c]
                else:
                    r[c] = last.get(c, "")
            out.append(r)
        rows = out
    return rows


# --------------------------------------------------------------------------- codes


def code_key(code: str, style: str = "compact") -> str:
    """URI-safe key of a code. compact: '01.1.1' -> '0111'; dotted: '6.1.1 a' -> '6.1.1.a'."""
    code = (code or "").strip()
    if style == "dotted":
        key = re.sub(r"[^0-9A-Za-z]+", ".", code).strip(".")
        return key
    return re.sub(r"[^0-9A-Za-z]", "", code)


def strip_code_prefix(label: str, code: str) -> str:
    """'01 - Animals; live' -> 'Animals; live' (Comtrade-style labels)."""
    return re.sub(rf"^\s*{re.escape(code)}\s*[-–:]\s*", "", label)


# --------------------------------------------------------------------------- turtle

TTL_PREFIXES = """@prefix skos: <http://www.w3.org/2004/02/skos/core#> .
@prefix xkos: <http://rdf-vocabulary.ddialliance.org/xkos#> .
@prefix dct: <http://purl.org/dc/terms/> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

"""


def lit(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ").replace("\r", " ").strip() + '"'


def lit_text(s: str) -> str:
    """Literal keeping line breaks (explanatory notes often hold lists)."""
    s = s.replace("\r\n", "\n").replace("\r", "\n").strip()
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


# column-name -> XKOS explanatory note type, for --notes "intro=core,includes=inclusion,..."
NOTE_TYPES = {"core": "coreContentNote", "additional": "additionalContentNote",
              "inclusion": "inclusionNote", "exclusion": "exclusionNote"}


def _pairs(spec):
    return spec.items() if isinstance(spec, dict) else (p.split("=", 1) for p in spec.split(",") if "=" in p)


def parse_notes(spec, header: list[str], default_lang: str = "en") -> list[tuple[int, str, str]]:
    """'intro=core,Excl_fr=exclusion@fr' (or a dict) -> [(column index, xkos property, language)]."""
    if not spec:
        return []
    out = []
    for col, kind in _pairs(spec):
        kind, _, nlang = kind.strip().partition("@")
        kind = kind.lower().replace("note", "").replace("content", "")
        if kind not in NOTE_TYPES:
            raise ConversionError(f"unknown note type {kind!r}; use one of {sorted(NOTE_TYPES)}")
        out.append((pick(header, col.strip(), []), NOTE_TYPES[kind], nlang or default_lang))
    return out


def parse_labels(spec, header: list[str]) -> list[tuple[int, str]]:
    """'Name_en=en,Name_fr=fr' (or a dict) -> [(column index, language)]; the first is the main label."""
    if not spec:
        return []
    return [(pick(header, col.strip(), []), lang.strip()) for col, lang in _pairs(spec)]


def provenance(source_url: str | None, what: str, note: str | None = None) -> str:
    note = note or (f"{what} converted from a published file by classif-mcp. "
                    "Not an official linked-data publication; check against the source file.")
    out = f"    skos:note {lit(note)}@en"
    if source_url:
        out += f" ;\n    dct:source <{source_url}>"
    return out


def build_scheme(rows, code_i, label_i, base, sid, label, lang, source_url,
                 parent_i: int | None = None, key_style: str = "compact",
                 prov_note: str | None = None, key_i: int | None = None, note_i: int | None = None,
                 scheme_extra: str = "", notes: list[tuple] | None = None,
                 labels: list[tuple[int, str]] | None = None) -> tuple[str, int]:
    """key_i: column holding an explicit URI key (used when the source prints the
    same code twice); note_i: column written as skos:editorialNote on each item;
    scheme_extra: extra Turtle predicate-object lines for the scheme;
    notes: [(column, xkos property[, language])] written as xkos:ExplanatoryNote resources;
    labels: [(column, language)] extra skos:prefLabel columns (multilingual sources)."""
    notes = [n if len(n) == 3 else (n[0], n[1], lang) for n in (notes or [])]
    labels = labels or []
    scheme = f"<{base}{sid}/scheme>"
    out = [TTL_PREFIXES, f"{scheme} a skos:ConceptScheme ;\n    skos:prefLabel {lit(label)}@{lang} ;\n"
           f"{provenance(source_url, 'Classification', prov_note)}{scheme_extra} .\n"]
    items: list[tuple] = []  # (key, code, title, parent_raw, note, [(xkos property, text)])
    for r in rows:
        if len(r) <= max(code_i, label_i):
            continue
        code = r[code_i].strip()
        k = (r[key_i].strip() if key_i is not None and len(r) > key_i and r[key_i].strip()
             else code_key(code, key_style))
        if not k:
            continue
        title = strip_code_prefix(r[label_i].strip(), code)
        praw = r[parent_i].strip() if parent_i is not None and len(r) > parent_i else ""
        note = r[note_i].strip() if note_i is not None and len(r) > note_i else ""
        xnotes = [(prop, nlang, r[ci].strip()) for ci, prop, nlang in notes
                  if len(r) > ci and r[ci].strip() and r[ci].strip().lower() not in ("none", "na")]
        xlabels = [(llang, strip_code_prefix(r[ci].strip(), code)) for ci, llang in labels
                   if ci != label_i and len(r) > ci and r[ci].strip() and r[ci].strip().lower() != "na"]
        items.append((k, code, title, praw, note, xnotes, xlabels))
    keys = {k for k, *_ in items}
    seen: list[str] = []
    last_section: str | None = None
    n = 0
    for k, code, title, praw, note, xnotes, xlabels in items:
        parent = None
        if parent_i is not None:
            pk = code_key(praw, key_style)
            parent = pk if pk in keys and pk != k else None
        elif key_style == "dotted":
            segs = k.split(".")
            while len(segs) > 1 and parent is None:
                segs = segs[:-1]
                cand = ".".join(segs)
                parent = cand if cand in keys else None
        else:
            if k.isalpha():
                last_section = k
            else:
                pref = [s for s in seen if k.startswith(s) and s != k and not s.isalpha()]
                parent = max(pref, key=len) if pref else last_section
        seen.append(k)
        uri = f"<{base}{sid}/{k}>"
        triples = [f"{uri} a skos:Concept", f"    skos:inScheme {scheme}",
                   f"    skos:notation {lit(code)}", f"    skos:prefLabel {lit(title)}@{lang}"]
        triples.append(f"    skos:broader <{base}{sid}/{parent}>" if parent else f"    skos:topConceptOf {scheme}")
        if note:
            triples.append(f"    skos:editorialNote {lit(note)}@en")
        for llang, ltext in xlabels:
            triples.append(f"    skos:prefLabel {lit(ltext)}@{llang}")
        note_blocks = []
        for prop, nlang, text in xnotes:  # XKOS explanatory notes, modelled as INSEE and Eurostat do
            nuri = f"<{base}{sid}/{k}/note/{prop}" + (f"/{nlang}>" if nlang != lang else ">")
            triples.append(f"    xkos:{prop} {nuri}")
            note_blocks.append(f"{nuri} a xkos:ExplanatoryNote ;\n    xkos:plainText {lit_text(text)}@{nlang} .\n")
        out.append(" ;\n".join(triples) + " .\n")
        out.extend(note_blocks)
        n += 1
    return "\n".join(out), n


def build_correspondence(rows, src_i, tgt_i, src_base, tgt_base, cid, label, source_url,
                         note_i=None, key_style: str = "compact", header: list[str] | None = None,
                         src_suffix: str = "", tgt_suffix: str = "") -> tuple[str, int]:
    """note_i: one column index or a list; several columns are written as "name=value; ...".
    src_suffix / tgt_suffix: appended to the code in the URI (e.g. Cellar's HS items:
    http://data.europa.eu/xsp/cn2026/ + 010129 + 000080)."""
    corr = f"<{DEFAULT_BASE}correspondence/{cid}>"
    out = [TTL_PREFIXES, f"{corr} a xkos:Correspondence ;\n    skos:prefLabel {lit(label)}@en ;\n"
           f"{provenance(source_url, 'Correspondence table')} .\n"]
    note_is = [] if note_i is None else (note_i if isinstance(note_i, list) else [note_i])
    pairs: dict[tuple[str, str], str] = {}
    for r in rows:
        if len(r) <= max(src_i, tgt_i):
            continue
        s, t = code_key(r[src_i], key_style), code_key(r[tgt_i], key_style)
        if s and t:
            vals = [(header[c] if header and len(note_is) > 1 else "", r[c].strip())
                    for c in note_is if len(r) > c and r[c].strip()]
            note = "; ".join(f"{h}={v}" if h else v for h, v in vals)
            pairs.setdefault((s, t), note)
    for i, ((s, t), note) in enumerate(sorted(pairs.items())):
        a = f"<{DEFAULT_BASE}correspondence/{cid}/{i}>"
        extra = f" ;\n    rdfs:comment {lit(note)}@en" if note else ""
        out.append(f"{a} a xkos:ConceptAssociation ;\n    xkos:sourceConcept <{src_base}{s}{src_suffix}> ;\n"
                   f"    xkos:targetConcept <{tgt_base}{t}{tgt_suffix}>{extra} .\n{corr} xkos:madeOf {a} .\n")
    return "\n".join(out), len(pairs)


# --------------------------------------------------------------------------- api


class ConversionError(ValueError):
    pass


def convert(path: Path, mode: str = "scheme", *, id: str, label: str, lang: str = "en",
            base: str = DEFAULT_BASE, code_col=None, label_col=None, parent_col=None,
            source_base: str | None = None, target_base: str | None = None,
            source_col=None, target_col=None, note_col=None, sheet=None, no_header: bool = False,
            filter_col=None, filter_value=None, fill_down=None, key_style: str = "compact",
            source_url: str | None = None, prov_note: str | None = None,
            notes=None, skip_codes=None, labels=None, header_row: int | None = None,
            source_suffix: str = "", target_suffix: str = "") -> tuple[str, int, str]:
    """Convert one file to Turtle. Returns (turtle, number of items or links, description).
    Used by the classif-unsd command and by live "file" endpoints in the server."""
    try:
        header, rows = read_table(Path(path), sheet, no_header, header_row)
        rows = prepare_rows(rows, header, filter_col, filter_value, fill_down)
        if mode == "scheme":
            ci = pick(header, code_col, [r"code", r"^id$"])
            li = pick(header, label_col, [r"title", r"desc", r"label", r"name", r"^text$"], {ci})
            pi = pick(header, parent_col, []) if parent_col else (
                header.index("parent") if "parent" in header else None)
            note_cols = parse_notes(notes, header, lang)
            label_cols = parse_labels(labels, header)
            skip = ({c.strip() for c in skip_codes.split(",")} if isinstance(skip_codes, str)
                    else set(skip_codes or []))
            if skip:  # aggregates such as "TOTAL" that are not classification items
                rows = [r for r in rows if not (len(r) > ci and r[ci].strip() in skip)]
            info = (f"header={header}\ncode={header[ci]!r} label={header[li]!r} "
                    f"parent={header[pi] if pi is not None else None!r} "
                    f"labels={[(header[c], l) for c, l in label_cols]} "
                    f"notes={[(header[c], p, l) for c, p, l in note_cols]} rows={len(rows)}")
            if pi is not None:  # parents named in the file but absent from it: report, never invent
                codes = {r[ci].strip() for r in rows if len(r) > ci}
                missing = sorted({r[pi].strip() for r in rows if len(r) > pi and r[pi].strip()
                                  and r[pi].strip() not in codes and r[pi].strip() not in skip})
                if missing:
                    n_orph = sum(1 for r in rows if len(r) > pi and r[pi].strip() in missing)
                    info += (f"\nWARNING: {n_orph} items name a parent that is not in the file "
                             f"({', '.join(missing[:10])}); they are kept at the top level, as published.")
            ttl, n = build_scheme(rows, ci, li, base.rstrip("/") + "/", id, label, lang, source_url,
                                  pi, key_style, prov_note, notes=note_cols, labels=label_cols)
        elif mode == "correspondence":
            if not (source_base and target_base):
                raise ConversionError("source_base and target_base are required for a correspondence")
            si = pick(header, source_col, [r"code"])
            ti = pick(header, target_col, [r"code"], {si})
            note_names = ([c.strip() for c in note_col.split(",")] if isinstance(note_col, str)
                          else list(note_col or []))
            # a single name may itself contain commas: try it whole first
            if isinstance(note_col, str) and note_col in header:
                note_names = [note_col]
            ni = [pick(header, c, []) for c in note_names]
            info = (f"header={header}\nsource={header[si]!r} target={header[ti]!r} "
                    f"notes={[header[i] for i in ni]} rows={len(rows)}")
            ttl, n = build_correspondence(rows, si, ti, source_base, target_base, id, label,
                                          source_url, ni or None, key_style, header,
                                          source_suffix, target_suffix)
        else:
            raise ConversionError(f"unknown mode {mode!r}")
    except SystemExit as e:  # pick() exits with a message in CLI use
        raise ConversionError(str(e)) from None
    return ttl, n, info


# --------------------------------------------------------------------------- cli


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="classif-unsd", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="mode", required=True)

    s = sub.add_parser("scheme", help="convert a classification structure file")
    s.add_argument("file", type=Path)
    s.add_argument("--id", required=True, help="short id used in URIs, e.g. isic-rev5")
    s.add_argument("--label", required=True)
    s.add_argument("--lang", default="en")
    s.add_argument("--base", default=DEFAULT_BASE)
    s.add_argument("--code-col"); s.add_argument("--label-col"); s.add_argument("--parent-col")
    s.add_argument("--labels", help='extra label columns by language, e.g. "Name_fr=fr,Name_de=de"')
    s.add_argument("--skip-codes", help='comma-separated codes to leave out, e.g. "TOTAL"')
    s.add_argument("--notes", help='explanatory notes as XKOS, e.g. "intro=core,includes=inclusion,'
                                   'alsoIncludes=additional,excludes=exclusion"')

    c = sub.add_parser("correspondence", help="convert a correspondence table")
    c.add_argument("file", type=Path)
    c.add_argument("--id", required=True)
    c.add_argument("--label", required=True)
    c.add_argument("--source-base", required=True, help="URI base of source items, ending with /")
    c.add_argument("--target-base", required=True, help="URI base of target items, ending with /")
    c.add_argument("--source-col"); c.add_argument("--target-col")
    c.add_argument("--source-suffix", default="", help="appended to source codes in URIs")
    c.add_argument("--target-suffix", default="", help="appended to target codes in URIs, e.g. 000080 "
                                                       "for Cellar's HS items")
    c.add_argument("--note-col", help="optional column(s), comma-separated, kept as rdfs:comment on each link, "
                                      "e.g. the GSIM type of change")

    for p in (s, c):
        p.add_argument("--sheet", help="Excel sheet name (default: first)")
        p.add_argument("--no-header", action="store_true", help="the file has no header row")
        p.add_argument("--header-row", type=int, help="1-based row holding the column names "
                                                      "(default: first row mentioning 'code')")
        p.add_argument("--filter-col"); p.add_argument("--filter-value")
        p.add_argument("--fill-down", help="comma-separated columns to fill down")
        p.add_argument("--key-style", choices=["compact", "dotted"], default="compact")
        p.add_argument("--source-url", help="URL of the source file, recorded as dct:source")
        p.add_argument("-o", "--output", type=Path)
        p.add_argument("--dry-run", action="store_true")

    a = ap.parse_args(argv)
    opts = {k: v for k, v in vars(a).items() if k not in ("mode", "file", "output", "dry_run")}
    try:
        ttl, n, info = convert(a.file, a.mode, **opts)
    except ConversionError as e:
        sys.exit(str(e))
    print(info, file=sys.stderr)
    print(f"{n} items/links generated", file=sys.stderr)
    if a.dry_run:
        return
    if a.output:
        a.output.parent.mkdir(parents=True, exist_ok=True)
        a.output.write_text(ttl, encoding="utf-8")
        print(f"written {a.output}", file=sys.stderr)
    else:
        sys.stdout.write(ttl)


if __name__ == "__main__":
    main()
