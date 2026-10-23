"""Tier 3: classifications published only as PDF.

The content of such a classification belongs to its custodian; nobody else can
validate it. What can be checked is narrower: that the transcription is a
faithful copy of the PDF (codes, titles and hierarchy as printed). This tool
records exactly that, and nothing more.

  classif-pdf text     EBOPS2010.pdf -o pages.json         # 1. page texts
  classif-pdf draft    pages.json -o draft.csv             # 2. LLM reading (Claude API, ANTHROPIC_API_KEY)
      or, without an API key:
  classif-pdf prompt   pages.json -o prompt.txt            #    paste into a Claude chat,
  classif-pdf import   answer.json -o draft.csv            #    save the JSON answer, import it
  classif-pdf compare  draftA.csv draftB.csv -o draft.csv  # 2b. optional: two independent LLM readings
  classif-pdf check    draft.csv pages.json -o review.csv  # 3. checks against the PDF text
      Rows read identically by the LLM and by the rule-based reading of the
      PDF text, with no flag, are accepted automatically ("matches_pdf (auto)").
      A person looks only at the rest: status = matches_pdf / reject.
  classif-pdf finalize review.csv --id ebops2010 --label "EBOPS 2010" \
      --checked-by "Name" --pdf EBOPS2010.pdf --source-url https://... \
      --anomalies anomalies.txt -o data/ebops2010.ttl     # 4. Turtle + report for the custodian
      --unchecked: serve without a person's check, labelled as such.

Anomalies of the source (e.g. a code printed twice) are kept as printed, never
corrected, and listed in the anomalies report to send to the custodian.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import difflib
import hashlib
import json
import os
import re
import sys
from pathlib import Path

from .unsd_to_skos import DEFAULT_BASE, build_scheme, code_key

PDF_BASE = DEFAULT_BASE.replace("/unsd/", "/pdf/")  # http://classif-mcp.local/pdf/
CM = "http://classif-mcp.local/ns#"
FIELDS = ["code", "title", "parent", "kind", "page", "note", "flags", "status", "source_issue", "reviewer_note"]
OK = {"matches_pdf", "matches_pdf (auto)", "validated", "ok"}  # last two: files made with v0.1
REJECT = {"reject", "rejected"}

PROMPT = """You are transcribing a statistical classification from the text of PDF pages, so that it
can be published as linked data. A person will compare your output with the PDF.

Return ONLY a JSON array, no other text. One element per classification item:
  {{"code": "...", "title": "...", "parent": "...", "kind": "...", "page": N, "note": "..."}}

Rules:
- Copy codes exactly as printed (keep dots, letters and spaces such as "6.1.1 a").
- Copy titles exactly as printed, but rejoin words hyphenated across a line break
  ("re-\\nceived" -> "received") and join titles that wrap onto the next line.
- Leave cross-references such as "(see para. 3.71)" out of the title and put them in "note".
- "parent": the code of the item this one falls under, from the numbering and layout;
  "" for top-level items. Do not invent parents that are not printed.
- "kind": "item" for a regular item; "supplementary" for items introduced by "Of which";
  "alternative" for items in an alternative presentation; "grouping" for complementary
  or memorandum groupings.
- Ignore page numbers, running headers and footers, and introductory prose.
- Do NOT correct anything that looks like an error in the source (duplicate or
  out-of-sequence codes): copy it as printed and describe the issue in "note".
- Do not add items that are not printed.

{pages}
"""

# a code at the start of a line: 3.1.1  10.1.1.2.4  3a.1  6.1.1 a  9.2.1.a  C.1  4.0
LINE_CODE = re.compile(
    r"^(?:Of which:\s*)?((?:[A-Z]\.)?\d+[a-z]?(?:\.\d+)*(?:\.?\s?[a-z](?=\s))?|[A-Z]\.\d+(?:\.\d+)*)\s+(\S.*)$"
)
SOURCE_ISSUE_HINT = re.compile(r"printed as|would be expected|duplicate|out[- ]of[- ]sequence|copied as printed", re.I)


# --------------------------------------------------------------------------- helpers


def norm(s: str, keep_hyphen: bool = False) -> str:
    # a hyphen at a line break is either a word split ("re-\\nceived") or a real
    # compound ("short-\\nterm"); checks compare against both readings
    s = re.sub(r"(\w)-\s*\n\s*(\w)", r"\1-\2" if keep_hyphen else r"\1\2", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip().lower()


def load_pages(path: Path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_csv(path: Path, rows: list[dict]) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as f:  # BOM: opens cleanly in Excel
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in FIELDS})


def read_csv(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8-sig") as f:
        return [{k: (v or "") for k, v in r.items()} for r in csv.DictReader(f)]


def parse_llm_json(text: str) -> list[dict]:
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if not m:
        raise ValueError("No JSON array found in the LLM answer.")
    rows = json.loads(m.group(0))
    return [{k: ("" if r.get(k) is None else str(r.get(k))) for k in ("code", "title", "parent", "kind", "page", "note")}
            for r in rows]


def chunks(pages: list[dict], max_chars: int) -> list[list[dict]]:
    out, cur, size = [], [], 0
    for p in pages:
        if cur and size + len(p["text"]) > max_chars:
            out.append(cur)
            cur, size = [], 0
        cur.append(p)
        size += len(p["text"])
    if cur:
        out.append(cur)
    return out


def render_pages(pages: list[dict]) -> str:
    return "\n\n".join(f"=== PAGE {p['page']} ===\n{p['text']}" for p in pages)


def _code_pattern(code: str) -> str:
    return re.escape(code.lower()).replace(r"\ ", r"\s*")


# --------------------------------------------------------------------------- 1-2: text and LLM reading


def cmd_text(pdf: Path, out: Path, first: int | None, last: int | None) -> None:
    import pdfplumber

    pages = []
    with pdfplumber.open(pdf) as doc:
        for i, page in enumerate(doc.pages, start=1):
            if (first and i < first) or (last and i > last):
                continue
            pages.append({"page": i, "text": page.extract_text() or ""})
    out.write_text(json.dumps(pages, ensure_ascii=False, indent=1), encoding="utf-8")
    empty = [p["page"] for p in pages if not p["text"].strip()]
    print(f"{len(pages)} pages written to {out}" + (f"; NO TEXT on pages {empty} (scanned? needs OCR)" if empty else ""),
          file=sys.stderr)


def cmd_prompt(pages_path: Path, out: Path | None, max_chars: int) -> None:
    parts = chunks(load_pages(pages_path), max_chars)
    texts = [PROMPT.format(pages=render_pages(c)) for c in parts]
    sep = "\n\n" + "#" * 70 + "\n# NEXT PROMPT (paste separately)\n" + "#" * 70 + "\n\n"
    text = sep.join(texts)
    if out:
        out.write_text(text, encoding="utf-8")
        print(f"{len(texts)} prompt(s) written to {out}", file=sys.stderr)
    else:
        sys.stdout.write(text)


def call_claude(prompt: str, model: str) -> str:
    import httpx

    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        sys.exit("ANTHROPIC_API_KEY is not set. Use `classif-pdf prompt` to do this step in a Claude chat instead.")
    r = httpx.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
        json={"model": model, "max_tokens": 16000, "messages": [{"role": "user", "content": prompt}]},
        timeout=300,
    )
    r.raise_for_status()
    return "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")


def cmd_draft(pages_path: Path, out: Path, model: str, max_chars: int) -> None:
    rows: list[dict] = []
    for i, part in enumerate(chunks(load_pages(pages_path), max_chars), start=1):
        print(f"LLM call {i} (pages {part[0]['page']}-{part[-1]['page']}) ...", file=sys.stderr)
        rows += parse_llm_json(call_claude(PROMPT.format(pages=render_pages(part)), model))
    for r in rows:
        r["note"] = (r.get("note", "") + f" [read by {model}]").strip()
    write_csv(out, rows)
    print(f"{len(rows)} rows drafted -> {out}", file=sys.stderr)


def cmd_import(answer: Path, out: Path, drafted_by: str) -> None:
    rows = parse_llm_json(answer.read_text(encoding="utf-8"))
    for r in rows:
        r["note"] = (r.get("note", "") + f" [read by {drafted_by}]").strip()
    write_csv(out, rows)
    print(f"{len(rows)} rows imported -> {out}", file=sys.stderr)


def cmd_compare(a_path: Path, b_path: Path, out: Path) -> None:
    """Merge two independent LLM readings; flag every difference for a person."""
    a, b = read_csv(a_path), read_csv(b_path)

    def index(rows):
        d: dict[str, list[dict]] = {}
        for r in rows:
            d.setdefault(re.sub(r"\s+", "", r["code"].lower()), []).append(r)
        return d

    ia, ib = index(a), index(b)
    merged, occ = [], {}
    for r in a:
        k = re.sub(r"\s+", "", r["code"].lower())
        others = ib.get(k, [])
        i = occ.get(k, 0)  # n-th occurrence of this code in reading A ↔ n-th in reading B
        occ[k] = i + 1
        r = dict(r)
        if i >= len(others):
            r["flags"] = "ONLY_IN_ONE_READING"
        else:
            o = others[i]
            diffs = [f for f in ("title", "parent", "kind") if norm(r.get(f, "")) != norm(o.get(f, ""))]
            if diffs:
                r["flags"] = "READINGS_DIFFER(" + ",".join(diffs) + ")"
                r["note"] = (r.get("note", "") + " | other reading: " +
                             "; ".join(f"{f}={o.get(f, '')!r}" for f in diffs)).strip(" |")
        merged.append(r)
    for k, rows in ib.items():
        if k not in ia:
            for r in rows:
                merged.append({**r, "flags": "ONLY_IN_ONE_READING"})
    write_csv(out, merged)
    n = sum(1 for r in merged if r.get("flags"))
    print(f"{len(merged)} rows merged; {n} differ between the two readings -> {out}", file=sys.stderr)


# --------------------------------------------------------------------------- 3: checks


def check_rows(rows: list[dict], pages: list[dict]) -> tuple[list[dict], list[dict]]:
    """Checks of each row against the PDF text. Returns (rows, possibly_missing).
    A row with no flag whose code is immediately followed by its title in the PDF
    text (an independent, rule-based reading) is accepted automatically."""
    by_page = {str(p["page"]): p["text"] for p in pages}
    all_text = "\n".join(p["text"] for p in pages)
    variants = {k: (norm(v), norm(v, True)) for k, v in by_page.items()}
    all_variants = (norm(all_text), norm(all_text, True))
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["code"].strip()] = counts.get(r["code"].strip(), 0) + 1
    codes = {r["code"].strip() for r in rows}
    dkeys = {code_key(c, "dotted") for c in codes}

    for r in rows:
        flags = [f for f in r.get("flags", "").split(";") if f.startswith(("READINGS_DIFFER", "ONLY_IN_ONE"))]
        code, title, parent = r["code"].strip(), r["title"].strip(), r.get("parent", "").strip()
        texts = variants.get(str(r.get("page", "")).strip(), all_variants)
        cpat = _code_pattern(code)
        if not any(re.search(rf"(?<![\w.]){cpat}(?![\w])", t) for t in texts):
            flags.append("CODE_NOT_IN_PAGE" if re.search(rf"(?<![\w.]){cpat}(?![\w])", all_variants[0])
                         else "CODE_NOT_IN_PDF")
        nt = norm(title)
        if nt and not any(nt in t for t in texts):
            best, L = 0.0, len(nt)
            for i in range(0, max(1, len(texts[0]) - L + 1), max(1, L // 4)):
                best = max(best, difflib.SequenceMatcher(None, nt, texts[0][i:i + L]).ratio())
                if best > 0.97:
                    break
            flags.append(f"TITLE_DIFFERS({best:.2f})" if best >= 0.75 else "TITLE_NOT_IN_PDF")
        if counts[code] > 1:
            flags.append(f"DUPLICATE_CODE(x{counts[code]})")
        if parent and parent not in codes:
            flags.append("PARENT_NOT_FOUND")
        if parent:
            dk, dp = code_key(code, "dotted"), code_key(parent, "dotted")
            prefixes = [c for c in dkeys if c != dk and dk.startswith(c + ".")]
            nearest = max(prefixes, key=len) if prefixes else None
            if nearest and nearest != dp:
                flags.append(f"PARENT_SKIPS_LEVEL(expected {nearest})" if dk.startswith(dp + ".")
                             else f"PARENT_NOT_PREFIX(expected {nearest})")
            elif not nearest and "." in dk and not dk.startswith(dp + "."):
                flags.append("PARENT_NOT_PREFIX")
        elif "." in code_key(code, "dotted"):
            prefixes = [c for c in dkeys if code_key(code, "dotted").startswith(c + ".")]
            if prefixes:
                flags.append(f"PARENT_MISSING(expected {max(prefixes, key=len)})")
        r["flags"] = ";".join(flags)
        anchored = bool(nt) and any(re.search(rf"(?<![\w.]){cpat}\s+{re.escape(nt)}", t) for t in texts)
        if not r.get("status"):
            r["status"] = "matches_pdf (auto)" if anchored and not flags else ""
        if not r.get("source_issue"):
            if counts[code] > 1:
                r["source_issue"] = f"code {code} printed {counts[code]} times"
            elif SOURCE_ISSUE_HINT.search(r.get("note", "")):
                r["source_issue"] = r["note"].split("[read by")[0].strip()
        r.setdefault("reviewer_note", "")

    missing = []
    read = {re.sub(r"\s+", "", c.lower()) for c in codes}
    for p in pages:
        for line in p["text"].splitlines():
            m = LINE_CODE.match(line.strip())
            if m and re.sub(r"\s+", "", m.group(1).lower()) not in read:
                missing.append({"code": m.group(1), "title": m.group(2), "page": p["page"],
                                "flags": "POSSIBLY_MISSING (line starts with a code; not in the reading)"})
    return rows, missing


def cmd_check(draft: Path, pages_path: Path, out: Path) -> None:
    rows, missing = check_rows(read_csv(draft), load_pages(pages_path))
    write_csv(out, rows + missing)
    auto = sum(1 for r in rows if r["status"] == "matches_pdf (auto)")
    todo = [r for r in rows + missing if not r.get("status")]
    print(f"{len(rows)} rows: {auto} accepted automatically; {len(todo)} for a person "
          f"({len(missing)} possibly missing lines) -> {out}", file=sys.stderr)
    for r in todo:
        print(f"  p{r['page']}  {r['code']:<12} {r['flags'] or '(no flag; title not found right after the code)'}"
              f"  | {r['title'][:55]}", file=sys.stderr)


# --------------------------------------------------------------------------- 4: Turtle


def cmd_finalize(review: Path, out: Path, sid: str, label: str, checked_by: str | None, pdf: Path | None,
                 source_url: str | None, anomalies: Path | None, base: str, lang: str, unchecked: bool) -> None:
    rows = read_csv(review)
    status = [r.get("status", "").strip().lower() for r in rows]
    pending = [r["code"] for r, s in zip(rows, status) if s not in OK | REJECT]
    if pending and not unchecked:
        sys.exit(f"{len(pending)} rows have no status (matches_pdf / reject), e.g. {pending[:8]}. "
                 "Check them, or use --unchecked to serve them labelled as not checked by a person.")
    if not unchecked and not checked_by:
        sys.exit("--checked-by is required (the person who compared the transcription with the PDF).")
    keep = [r for r, s in zip(rows, status)
            if s in OK or (unchecked and s == "" and "POSSIBLY_MISSING" not in r.get("flags", ""))]
    auto = sum(1 for r in keep if r.get("status", "").strip().lower() == "matches_pdf (auto)")
    manual = sum(1 for r in keep if r.get("status", "").strip().lower() in OK) - auto
    unchecked_n = len(keep) - auto - manual

    # a code printed twice in the source is served twice, as printed, under distinct URIs
    seen: dict[str, int] = {}
    table, issues = [], []
    for r in keep:
        k = code_key(r["code"], "dotted")
        seen[k] = seen.get(k, 0) + 1
        key = k if seen[k] == 1 else f"{k}-{seen[k]}"
        note = "; ".join(x for x in (
            f"{r['kind']} item" if r.get("kind") and r["kind"] != "item" else "",
            r.get("note", "").split("[read by")[0].strip(),
            f"Source anomaly: {r['source_issue']}" if r.get("source_issue") else "",
            "" if r.get("status", "").strip() else "Transcription not checked by a person.",
        ) if x)
        table.append([r["code"], r["title"], r.get("parent", ""), key, note])
        if r.get("source_issue"):
            issues.append(r)

    today = dt.date.today().isoformat()
    if unchecked and unchecked_n:
        how = (f"NOT checked by a person: {auto} rows passed the automatic checks "
               f"(two independent readings agree), {unchecked_n} rows are unchecked")
    else:
        how = (f"transcription checked against the PDF: {auto} rows automatically (two independent "
               f"readings agree), {manual} rows by {checked_by} on {today}")
    note = (f"Transcribed from the custodian's PDF with LLM assistance; {how}. The content is the "
            f"custodian's and is not endorsed by them in this form; anomalies in the source are kept as printed.")
    extra = ""
    if pdf:
        sha = hashlib.sha256(Path(pdf).read_bytes()).hexdigest()
        extra = f' ;\n    <{CM}sourceSha256> "{sha}"'
    ttl, n = build_scheme(table, 0, 1, base.rstrip("/") + "/", sid, label, lang, source_url,
                          parent_i=2, key_style="dotted", prov_note=note, key_i=3, note_i=4, scheme_extra=extra)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(ttl, encoding="utf-8")
    print(f"{n} items written to {out} ({len(rows) - len(keep)} rows left out); {note}", file=sys.stderr)
    if pdf and not source_url:
        print("Note: without --source-url the server cannot watch the PDF for changes.", file=sys.stderr)

    if anomalies:
        lines = [f"Anomalies found while transcribing {label}",
                 f"Source: {source_url or (pdf.name if pdf else 'PDF')}", f"Date: {today}", "",
                 "The following points were found in the published PDF. They have been kept as printed",
                 "in the transcription; we report them for your information.", ""]
        titles = {r["code"].strip(): r["title"] for r in keep}
        grouped: dict[str, list[dict]] = {}
        for r in issues:
            grouped.setdefault(r["code"].strip(), []).append(r)
        for code, group in grouped.items():
            where = ", ".join(
                f"page {r.get('page', '?')} under {r.get('parent') or 'top level'}"
                + (f" ({titles.get(r.get('parent', ''), '')})" if r.get("parent") in titles else "")
                for r in group)
            extra = "; ".join(dict.fromkeys(
                r["note"].split("[read by")[0].strip(" |") for r in group
                if SOURCE_ISSUE_HINT.search(r.get("note", ""))))
            lines.append(f"- Code {code} \"{group[0]['title']}\": {group[0]['source_issue']} ({where})."
                         + (f" {extra}" if extra else ""))
        if not issues:
            lines.append("- None.")
        anomalies.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"{len(issues)} source anomalies -> {anomalies}", file=sys.stderr)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="classif-pdf", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("text"); t.add_argument("pdf", type=Path); t.add_argument("-o", "--output", type=Path, required=True)
    t.add_argument("--first", type=int); t.add_argument("--last", type=int)
    pr = sub.add_parser("prompt"); pr.add_argument("pages", type=Path); pr.add_argument("-o", "--output", type=Path)
    pr.add_argument("--max-chars", type=int, default=40000)
    d = sub.add_parser("draft"); d.add_argument("pages", type=Path); d.add_argument("-o", "--output", type=Path, required=True)
    d.add_argument("--model", default=os.environ.get("CLASSIF_PDF_MODEL", "claude-sonnet-5-5"))
    d.add_argument("--max-chars", type=int, default=40000)
    im = sub.add_parser("import"); im.add_argument("answer", type=Path); im.add_argument("-o", "--output", type=Path, required=True)
    im.add_argument("--drafted-by", default="Claude (chat)")
    cp = sub.add_parser("compare"); cp.add_argument("a", type=Path); cp.add_argument("b", type=Path)
    cp.add_argument("-o", "--output", type=Path, required=True)
    c = sub.add_parser("check"); c.add_argument("draft", type=Path); c.add_argument("pages", type=Path)
    c.add_argument("-o", "--output", type=Path, required=True)
    f = sub.add_parser("finalize"); f.add_argument("review", type=Path); f.add_argument("-o", "--output", type=Path, required=True)
    f.add_argument("--id", required=True); f.add_argument("--label", required=True)
    f.add_argument("--checked-by", "--validated-by", dest="checked_by")
    f.add_argument("--pdf", type=Path, help="the PDF itself: its SHA-256 lets the server detect a new version")
    f.add_argument("--source-url"); f.add_argument("--anomalies", type=Path, help="write a report for the custodian")
    f.add_argument("--base", default=PDF_BASE); f.add_argument("--lang", default="en")
    f.add_argument("--unchecked", action="store_true", help="serve rows no person checked, labelled as such")
    a = ap.parse_args(argv)
    if a.cmd == "text":
        cmd_text(a.pdf, a.output, a.first, a.last)
    elif a.cmd == "prompt":
        cmd_prompt(a.pages, a.output, a.max_chars)
    elif a.cmd == "draft":
        cmd_draft(a.pages, a.output, a.model, a.max_chars)
    elif a.cmd == "import":
        cmd_import(a.answer, a.output, a.drafted_by)
    elif a.cmd == "compare":
        cmd_compare(a.a, a.b, a.output)
    elif a.cmd == "check":
        cmd_check(a.draft, a.pages, a.output)
    else:
        cmd_finalize(a.review, a.output, a.id, a.label, a.checked_by, a.pdf, a.source_url, a.anomalies,
                     a.base, a.lang, a.unchecked)


if __name__ == "__main__":
    main()
