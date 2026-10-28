"""Display templates for classif-mcp answers, built on SKOS/XKOS.

Each template renders one kind of SKOS/XKOS object as Markdown, the same way
for every classification and publisher:

  item card        skos:Concept: classification, code, label, broader item, URI,
                   provenance, then the XKOS explanatory notes
  hierarchy        skos:broader path, from the top level down to the item
  correspondences  SKOS mapping properties and xkos:ConceptAssociation, one row per
                   link, with the provenance of each link
  derived match    a code-based candidate link, marked as not official

Provenance is a fixed field of every template, so it cannot be left out of an answer.
The templates only lay out what the tools returned: they never add or reword content.
"""

from __future__ import annotations

from typing import Any

PROVENANCE_LEGEND = (
    "*Provenance: **published** = linked data at the publisher · **converted** = converted "
    "from the publisher's file · **derived** = matched by code, not an official link.*"
)

# XKOS / SKOS note properties -> heading, in display order
NOTE_HEADINGS = [
    ("scopeNote", "Scope"),
    ("coreContentNote", "Includes"),
    ("inclusionNote", "Includes"),
    ("additionalContentNote", "Also includes"),
    ("exclusionNote", "Excludes"),
    ("caseLaw", "Case law"),
]

RELATIONS = {  # (relation, direction) -> wording seen from the item displayed
    ("exactMatch", "forward"): "exact match", ("exactMatch", "inverse"): "exact match",
    ("closeMatch", "forward"): "close match", ("closeMatch", "inverse"): "close match",
    ("broadMatch", "forward"): "broader match", ("broadMatch", "inverse"): "narrower match",
    ("narrowMatch", "forward"): "narrower match", ("narrowMatch", "inverse"): "broader match",
    ("relatedMatch", "forward"): "related match", ("relatedMatch", "inverse"): "related match",
    ("xkos:ConceptAssociation", "forward"): "correspondence →",
    ("xkos:ConceptAssociation", "inverse"): "correspondence ←",
}


def _cell(text: Any) -> str:
    """Text safe inside a Markdown table cell."""
    return str(text or "").replace("|", "\\|").replace("\n", " ").strip()


def _code_label(code: str, label: str) -> str:
    code, label = (code or "").strip(), (label or "").strip()
    if code and label.startswith(code + " "):  # publishers that repeat the code in the label
        label = label[len(code):].strip()
    return " – ".join(x for x in (code, label) if x) or "(no code or label)"


def short_provenance(full: str) -> str:
    """'converted live from the UNSD file (...) [live from source]' -> 'converted'."""
    word = (full or "published").split()[0].strip("(:,")
    return word if word in ("published", "converted", "extracted", "derived") else full


def item_card(item: dict[str, Any]) -> str:
    """item: uri, code, label, scheme_label, broader {code, label}, provenance, publisher,
    notes {note property: [(text, language), ...]}, lang (requested language),
    note_source (optional)."""
    title = _code_label(item.get("code", ""), item.get("label", ""))
    scheme = item.get("scheme_label") or ""
    lines = [f"#### {scheme + ' · ' if scheme else ''}{title}", "", "| | |", "|---|---|"]
    lines.append(f"| Classification | {_cell(scheme) or '—'} |")
    lines.append(f"| Code | {_cell(item.get('code')) or '—'} |")
    lines.append(f"| Label | {_cell(_code_label('', item.get('label', '')) if not item.get('code') else _code_label(item['code'], item.get('label', '')).split(' – ', 1)[-1])} |")
    b = item.get("broader")
    lines.append(f"| Broader item | {_cell(_code_label(b.get('code', ''), b.get('label', ''))) if b else '— (top level)'} |")
    lines.append(f"| URI | {item.get('uri', '')} |")
    lines.append(f"| Provenance | **{short_provenance(item.get('provenance', ''))}** · "
                 f"{_cell(item.get('publisher'))} |")
    notes = item.get("notes") or {}
    shown = set()
    want = (item.get("lang") or "").lower()
    for prop, heading in NOTE_HEADINGS:
        for entry in notes.get(prop, []):
            text, nlang = entry if isinstance(entry, tuple) else (entry, "")
            if (heading, text) in shown:
                continue
            shown.add((heading, text))
            # say so when the note is only published in another language
            other = f" ({nlang}, not published in {want})" if nlang and want and not nlang.startswith(want) else ""
            lines += ["", f"**{heading}**{other}", "", text.strip()]
    if notes and item.get("note_source"):
        lines += ["", f"*Explanatory notes: source {item['note_source']}*"]
    if not notes:
        lines += ["", "*No explanatory notes published for this item.*"]
    return "\n".join(lines)


def hierarchy(scheme_label: str, path: list[dict[str, str]], item: dict[str, str]) -> str:
    """path: ancestors from the top level down; item: the item itself (code, label)."""
    lines = [f"**Hierarchy**{' · ' + scheme_label if scheme_label else ''}", ""]
    for depth, a in enumerate(path):
        lines.append(f"{'  ' * depth}- {_code_label(a.get('code', ''), a.get('label', ''))}")
    lines.append(f"{'  ' * len(path)}- **{_code_label(item.get('code', ''), item.get('label', ''))}**")
    return "\n".join(lines)


def children(scheme_label: str, parent: dict[str, str], items: list[dict[str, str]]) -> str:
    lines = [f"**{_code_label(parent.get('code', ''), parent.get('label', ''))}**"
             f"{' · ' + scheme_label if scheme_label else ''}: {len(items)} item(s) below", ""]
    lines += [f"- {_code_label(i.get('code', ''), i.get('label', ''))}" for i in items]
    return "\n".join(lines)


def correspondences(item_title: str, links: list[dict[str, Any]], hidden: int = 0) -> str:
    """links: relation, direction, target_scheme_label, target_code, target_label,
    provenance, publisher, note (optional)."""
    lines = [f"**Links of {item_title}**", ""]
    if not links:
        lines.append("*No published link found.*")
    else:
        lines += ["| Link | Classification | Code | Label | Provenance | Stated by |",
                  "|---|---|---|---|---|---|"]
        order = {"published": 0, "converted": 1, "extracted": 2, "derived": 3}
        for l in sorted(links, key=lambda l: (l.get("target_scheme_label", ""),
                                              order.get(short_provenance(l.get("provenance", "")), 9),
                                              l.get("target_code", ""))):
            rel = RELATIONS.get((l.get("relation", ""), l.get("direction", "")), l.get("relation", ""))
            if l.get("note"):
                rel += f" ({l['note']})"
            lines.append(f"| {_cell(rel)} | {_cell(l.get('target_scheme_label'))} | "
                         f"{_cell(l.get('target_code'))} | {_cell(_code_label(l.get('target_code', ''), l.get('target_label', '')).split(' – ', 1)[-1] if l.get('target_code') else l.get('target_label'))} | "
                         f"**{short_provenance(l.get('provenance', ''))}** | {_cell(l.get('publisher'))} |")
    if hidden:
        lines += ["", f"*{hidden} link(s) to classifications outside the scope are not shown.*"]
    lines += ["", PROVENANCE_LEGEND]
    return "\n".join(lines)


def derived_match(source_code: str, scheme_label: str, match: dict[str, Any] | None,
                  extensions: list[dict[str, Any]] | None = None) -> str:
    lines = [f"**Code match for {source_code}** → {scheme_label or 'target classification'}", ""]
    if not match:
        lines.append("*No code-based candidate found.*")
    else:
        lines += ["| Candidate | Matched on | Direction | Provenance |", "|---|---|---|---|",
                  f"| {_cell(_code_label(match.get('code', ''), match.get('label', '')))} | "
                  f"{_cell(match.get('matched_on'))} | {_cell(match.get('direction'))} | **derived** |"]
        for e in (extensions or [])[1:6]:
            lines.append(f"| {_cell(_code_label(e.get('code', ''), e.get('label', '')))} | "
                         f"prefix | down | **derived** |")
    lines += ["", "*Derived by code pattern: a candidate, not an official correspondence.*"]
    return "\n".join(lines)
