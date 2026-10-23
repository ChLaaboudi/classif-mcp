"""Convert UNSD classification files (CSV / Excel) to SKOS/XKOS Turtle.

UNSD publishes ISIC Rev. 5, CPC Ver. 3.0 and their correspondence tables as
CSV/Excel, not as linked data. This converter turns them into Turtle that
classif-mcp serves as a "local" endpoint, so the same tools work across
Caliper, Cellar, INSEE and these files.

Provenance is explicit: every generated scheme or correspondence carries
dct:source (the UNSD file URL) and a note that it is a conversion, not an
official LOD publication. URIs are minted under a base you choose; the default
is a local namespace, so that no URI is minted on UNSD's behalf.

Usage
-----
  # A classification (structure file)
  classif-unsd scheme ISIC_Rev_5_english_structure.csv \
      --id isic-rev5 --label "ISIC Rev. 5" \
      --source-url https://unstats.un.org/unsd/classifications/Econ/Download/In%20Text/ISIC_Rev_5_english_structure.csv \
      -o data/isic-rev5.ttl

  # A correspondence table
  classif-unsd correspondence ISIC_Rev4_to_ISIC_Rev5_Correspondence_Table-17Jan2025.xlsx \
      --id isic4-isic5 --label "ISIC Rev. 4 - ISIC Rev. 5" \
      --source-base http://classif-mcp.local/unsd/isic-rev4/ \
      --target-base http://classif-mcp.local/unsd/isic-rev5/ \
      -o data/isic4-isic5.ttl

Column detection is automatic (first column whose header mentions "code",
then "title"/"description"/"label"); override with --code-col / --label-col,
or --source-col / --target-col for correspondences. Run with --dry-run first
to see which columns and how many rows were detected.
"""

from __future__ import annotations

import argparse
import csv
import io
import re
import sys
from pathlib import Path

DEFAULT_BASE = "http://classif-mcp.local/unsd/"


# --------------------------------------------------------------------------- reading


def read_table(path: Path, sheet: str | None = None) -> tuple[list[str], list[list[str]]]:
    """Return (header, rows) from a CSV/TXT or XLSX file."""
    if path.suffix.lower() in (".xlsx", ".xlsm"):
        import openpyxl

        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        ws = wb[sheet] if sheet else wb.worksheets[0]
        all_rows = [["" if v is None else str(v).strip() for v in r] for r in ws.iter_rows(values_only=True)]
    else:
        raw = path.read_bytes()
        for enc in ("utf-8-sig", "cp1252", "latin-1"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        dialect = csv.Sniffer().sniff(text[:5000], delimiters=",;\t|")
        all_rows = [[c.strip() for c in r] for r in csv.reader(io.StringIO(text), dialect)]
    all_rows = [r for r in all_rows if any(r)]
    # header = first row that has a cell mentioning "code"
    for i, r in enumerate(all_rows[:20]):
        if any("code" in c.lower() for c in r):
            return r, all_rows[i + 1:]
    return all_rows[0], all_rows[1:]


def pick(header: list[str], wanted: str | None, patterns: list[str], skip: set[int] = frozenset()) -> int:
    if wanted is not None:
        if wanted.isdigit():
            return int(wanted)
        for i, h in enumerate(header):
            if h.lower() == wanted.lower():
                return i
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


# --------------------------------------------------------------------------- turtle

TTL_PREFIXES = """@prefix skos: <http://www.w3.org/2004/02/skos/core#> .
@prefix xkos: <http://rdf-vocabulary.ddialliance.org/xkos#> .
@prefix dct: <http://purl.org/dc/terms/> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

"""


def lit(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ").strip() + '"'


def code_key(code: str) -> str:
    return re.sub(r"[^0-9A-Za-z]", "", code)


def provenance(source_url: str | None, what: str) -> str:
    note = (f"{what} converted from a UNSD file by classif-mcp. "
            "Not an official linked-data publication; check against the source file.")
    out = f"    skos:note {lit(note)}@en"
    if source_url:
        out += f" ;\n    dct:source <{source_url}>"
    return out


def build_scheme(header, rows, code_i, label_i, base, sid, label, lang, source_url) -> tuple[str, int]:
    scheme = f"<{base}{sid}/scheme>"
    out = [TTL_PREFIXES, f"{scheme} a skos:ConceptScheme ;\n    skos:prefLabel {lit(label)}@{lang} ;\n"
           f"{provenance(source_url, 'Classification')} .\n"]
    seen: list[str] = []
    last_section: str | None = None
    n = 0
    for r in rows:
        if len(r) <= max(code_i, label_i):
            continue
        code, title = r[code_i].strip(), r[label_i].strip()
        k = code_key(code)
        if not k:
            continue
        uri = f"<{base}{sid}/{k}>"
        # parent = longest earlier code that is a prefix; else the last section letter
        parent = None
        if k.isalpha():
            last_section = k
        else:
            pref = [s for s in seen if k.startswith(s) and s != k and not s.isalpha()]
            parent = max(pref, key=len) if pref else last_section
        seen.append(k)
        triples = [f"{uri} a skos:Concept", f"    skos:inScheme {scheme}",
                   f"    skos:notation {lit(code)}", f"    skos:prefLabel {lit(title)}@{lang}"]
        if parent:
            triples.append(f"    skos:broader <{base}{sid}/{parent}>")
        else:
            triples.append(f"    skos:topConceptOf {scheme}")
        out.append(" ;\n".join(triples) + " .\n")
        n += 1
    return "\n".join(out), n


def build_correspondence(header, rows, src_i, tgt_i, src_base, tgt_base, cid, label, source_url,
                         note_i: int | None = None) -> tuple[str, int]:
    corr = f"<{DEFAULT_BASE}correspondence/{cid}>"
    out = [TTL_PREFIXES, f"{corr} a xkos:Correspondence ;\n    skos:prefLabel {lit(label)}@en ;\n"
           f"{provenance(source_url, 'Correspondence table')} .\n"]
    pairs: dict[tuple[str, str], str] = {}
    for r in rows:
        if len(r) <= max(src_i, tgt_i):
            continue
        s, t = code_key(r[src_i]), code_key(r[tgt_i])
        if s and t:
            note = r[note_i].strip() if note_i is not None and len(r) > note_i else ""
            pairs.setdefault((s, t), note)
    for i, ((s, t), note) in enumerate(sorted(pairs.items())):
        a = f"<{DEFAULT_BASE}correspondence/{cid}/{i}>"
        extra = f" ;\n    rdfs:comment {lit(note)}@en" if note else ""
        out.append(f"{a} a xkos:ConceptAssociation ;\n    xkos:sourceConcept <{src_base}{s}> ;\n"
                   f"    xkos:targetConcept <{tgt_base}{t}>{extra} .\n{corr} xkos:madeOf {a} .\n")
    return "\n".join(out), len(pairs)


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
    s.add_argument("--code-col"); s.add_argument("--label-col")

    c = sub.add_parser("correspondence", help="convert a correspondence table")
    c.add_argument("file", type=Path)
    c.add_argument("--id", required=True)
    c.add_argument("--label", required=True)
    c.add_argument("--source-base", required=True, help="URI base of source items, ending with /")
    c.add_argument("--target-base", required=True, help="URI base of target items, ending with /")
    c.add_argument("--source-col"); c.add_argument("--target-col")
    c.add_argument("--note-col", help="optional column kept as rdfs:comment on each link, "
                                      "e.g. the GSIM type of change")

    for p in (s, c):
        p.add_argument("--sheet", help="Excel sheet name (default: first)")
        p.add_argument("--source-url", help="URL of the UNSD file, recorded as dct:source")
        p.add_argument("-o", "--output", type=Path)
        p.add_argument("--dry-run", action="store_true")

    a = ap.parse_args(argv)
    header, rows = read_table(a.file, a.sheet)

    if a.mode == "scheme":
        ci = pick(header, a.code_col, [r"code"])
        li = pick(header, a.label_col, [r"title", r"desc", r"label", r"name"], {ci})
        print(f"header={header}\ncode column={header[ci]!r}  label column={header[li]!r}  rows={len(rows)}",
              file=sys.stderr)
        ttl, n = build_scheme(header, rows, ci, li, a.base.rstrip("/") + "/", a.id, a.label, a.lang, a.source_url)
    else:
        si = pick(header, a.source_col, [r"code"])
        ti = pick(header, a.target_col, [r"code"], {si})
        ni = pick(header, a.note_col, []) if a.note_col else None
        print(f"header={header}\nsource column={header[si]!r}  target column={header[ti]!r}  "
              f"note column={header[ni] if ni is not None else None!r}  rows={len(rows)}", file=sys.stderr)
        ttl, n = build_correspondence(header, rows, si, ti, a.source_base, a.target_base, a.id, a.label,
                                      a.source_url, ni)

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
