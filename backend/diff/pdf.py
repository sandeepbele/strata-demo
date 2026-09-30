#!/usr/bin/env python3
"""Find candidate text changes between two versions of a PDF.

The comparison uses unique runs of matching words as anchors. Page numbers,
line breaks, and paragraph numbering are never used as document identities.
All findings retain page and line references to the extracted source text.
"""

from __future__ import annotations

import argparse
from bisect import bisect_left
from collections import Counter
from dataclasses import dataclass
from difflib import SequenceMatcher
import hashlib
import json
from pathlib import Path
import re
import sys
import unicodedata

from backend.storage import ROOT

try:
    import pdfplumber
except ImportError as exc:
    raise SystemExit("Install dependencies with: python -m pip install -r requirements.txt") from exc


TOKEN_RE = re.compile(r"\d[\d,]*(?:\.\d+)?|[^\W\d_]+(?:['’][^\W\d_]+)*|[^\s]", re.UNICODE)
NUMBER_RE = re.compile(r"^\d[\d,]*(?:\.\d+)?$")
HIGH_SIGNAL = {
    "shall", "must", "required", "requires", "requirement", "deadline",
    "eligible", "eligibility", "except", "unless", "minimum", "maximum",
    "at least", "no more", "order", "ordered", "procurement",
}


@dataclass(frozen=True)
class Line:
    text: str
    page: int
    number: int
    top: float
    bottom: float
    page_height: float


@dataclass(frozen=True)
class Token:
    text: str
    key: str
    page: int
    line: int
    top: float
    bottom: float
    line_text: str
    char_start: int
    char_end: int


def normalize(text: str) -> str:
    # Align on normalized keys while retaining each token's original source text.
    return (unicodedata.normalize("NFKC", text).casefold()
            .replace("’", "'").replace("–", "-").replace("—", "-")
            .replace("\uf0b7", "•"))


def furniture_key(text: str) -> str:
    """Collapse changing page numbers only when detecting repeated margins."""
    return re.sub(r"\d+", "#", normalize(" ".join(text.split())))


def read_pdf(path: Path) -> tuple[list[Token], dict]:
    pages: list[list[Line]] = []
    with pdfplumber.open(path) as pdf:
        for page_no, page in enumerate(pdf.pages, 1):
            lines = page.extract_text_lines(return_chars=False)
            pages.append([
                Line(
                    text=" ".join(item["text"].split()),
                    page=page_no,
                    number=line_no,
                    top=float(item["top"]),
                    bottom=float(item["bottom"]),
                    page_height=float(page.height),
                )
                for line_no, item in enumerate(lines, 1)
                if item["text"].strip()
            ])

    # A repeated string in the top/bottom margin is usually a running header,
    # footer, or page number. Preserve the original file for source inspection.
    margin_counts: Counter[str] = Counter()
    for page_lines in pages:
        seen = {
            furniture_key(line.text)
            for line in page_lines
            if line.top < 75 or line.bottom > line.page_height - 45
        }
        margin_counts.update(seen)
    furniture = {key for key, count in margin_counts.items() if count >= 3 and key}

    tokens: list[Token] = []
    warnings: list[str] = []
    ignored_lines = 0
    for page_no, page_lines in enumerate(pages, 1):
        before = len(tokens)
        for line in page_lines:
            in_margin = line.top < 75 or line.bottom > line.page_height - 45
            if in_margin and furniture_key(line.text) in furniture:
                ignored_lines += 1
                continue
            for match in TOKEN_RE.finditer(line.text):
                word = match.group()
                tokens.append(Token(word, normalize(word), page_no, line.number,
                                    line.top, line.bottom, line.text, match.start(), match.end()))
        if len(tokens) - before < 20:
            warnings.append(f"Page {page_no} has fewer than 20 extracted tokens; inspect for sparse, scanned, or image-only content.")

    resolved = path.resolve()
    try:
        source_path = str(resolved.relative_to(ROOT))
    except ValueError:
        source_path = str(resolved)
    metadata = {
        "path": source_path,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "pages": len(pages),
        "extracted_tokens": len(tokens),
        "ignored_repeated_margin_lines": ignored_lines,
        "warnings": warnings,
    }
    return tokens, metadata


def unique_anchor_pairs(old: list[str], new: list[str], width: int = 8) -> list[tuple[int, int]]:
    """Match unique token shingles, then keep their longest ordered chain."""
    if min(len(old), len(new)) < width:
        return []
    # Only runs unique in each PDF become anchors; repeated boilerplate is ambiguous.
    old_runs = [tuple(old[i:i + width]) for i in range(len(old) - width + 1)]
    new_runs = [tuple(new[i:i + width]) for i in range(len(new) - width + 1)]
    old_counts = Counter(old_runs)
    new_counts = Counter(new_runs)
    new_positions = {run: i for i, run in enumerate(new_runs) if new_counts[run] == 1}
    candidates = [
        (i, new_positions[run])
        for i, run in enumerate(old_runs)
        if old_counts[run] == 1 and run in new_positions
    ]
    if not candidates:
        return []

    # Keep anchors in both documents' reading order; inserted pages may shift
    # their absolute offsets. Tails and predecessors reconstruct that chain.
    tails: list[int] = []
    tail_indexes: list[int] = []
    previous = [-1] * len(candidates)
    for index, (_, new_pos) in enumerate(candidates):
        place = bisect_left(tails, new_pos)
        if place:
            previous[index] = tail_indexes[place - 1]
        if place == len(tails):
            tails.append(new_pos)
            tail_indexes.append(index)
        else:
            tails[place] = new_pos
            tail_indexes[place] = index
    chain: list[tuple[int, int]] = []
    index = tail_indexes[-1]
    while index >= 0:
        chain.append(candidates[index])
        index = previous[index]
    chain.reverse()

    # Overlapping shingles represent one unchanged region. Conflicting overlap
    # can occur around reordered text; keep only non-conflicting matches.
    kept: list[tuple[int, int]] = []
    last_old_end = last_new_end = -1
    last_delta: int | None = None
    for old_pos, new_pos in chain:
        delta = new_pos - old_pos
        if old_pos < last_old_end or new_pos < last_new_end:
            if delta != last_delta:
                continue
        kept.append((old_pos, new_pos))
        last_old_end = max(last_old_end, old_pos + width)
        last_new_end = max(last_new_end, new_pos + width)
        last_delta = delta
    return kept


def equal_blocks(old: list[str], new: list[str], width: int = 8) -> list[tuple[int, int, int]]:
    anchors = unique_anchor_pairs(old, new, width)
    if not anchors:
        return []
    blocks: list[tuple[int, int, int]] = []
    for old_pos, new_pos in anchors:
        if blocks:
            prev_old, prev_new, size = blocks[-1]
            # Overlapping anchors with the same offset describe one unchanged run.
            if new_pos - old_pos == prev_new - prev_old and old_pos <= prev_old + size:
                blocks[-1] = (prev_old, prev_new, max(size, old_pos + width - prev_old))
                continue
        blocks.append((old_pos, new_pos, width))
    return blocks


def raw_changes(old: list[str], new: list[str], max_window: int = 1800) -> list[tuple[int, int, int, int, bool]]:
    """Return old/new half-open ranges and whether alignment was coarse."""
    blocks = equal_blocks(old, new)
    changes: list[tuple[int, int, int, int, bool]] = []
    old_cursor = new_cursor = 0
    # Diff only the gaps between matched runs, then include the trailing gap.
    for old_start, new_start, size in [*blocks, (len(old), len(new), 0)]:
        if old_start > old_cursor or new_start > new_cursor:
            old_gap = old[old_cursor:old_start]
            new_gap = new[new_cursor:new_start]
            # Large unmatched regions stay coarse instead of claiming precise alignment.
            if max(len(old_gap), len(new_gap)) > max_window:
                changes.append((old_cursor, old_start, new_cursor, new_start, True))
            else:
                # SequenceMatcher refines only this bounded local gap.
                matcher = SequenceMatcher(None, old_gap, new_gap, autojunk=False)
                for tag, a0, a1, b0, b1 in matcher.get_opcodes():
                    if tag != "equal":
                        changes.append((old_cursor + a0, old_cursor + a1,
                                        new_cursor + b0, new_cursor + b1, False))
        old_cursor, new_cursor = old_start + size, new_start + size
    return changes


def group_changes(
    changes: list[tuple[int, int, int, int, bool]], max_between: int = 10,
) -> list[tuple[int, int, int, int, bool]]:
    """Group edits separated by at most a short token gap on both sides."""
    grouped: list[tuple[int, int, int, int, bool]] = []
    for item in changes:
        if grouped:
            a0, a1, b0, b1, coarse = grouped[-1]
            c0, c1, d0, d1, next_coarse = item
            old_between, new_between = c0 - a1, d0 - b1
            # Merge nearby token edits on both sides; page boundaries are not checked.
            if (not coarse and not next_coarse and
                    0 <= old_between <= max_between and
                    0 <= new_between <= max_between and
                    max(old_between, new_between) <= max_between):
                grouped[-1] = (a0, c1, b0, d1, False)
                continue
        grouped.append(item)
    return grouped


def extracted_text(tokens: list[Token]) -> str:
    """Reconstruct a source excerpt from the parser's original line strings."""
    if not tokens:
        return ""
    lines: list[str] = []
    first = last = tokens[0]
    for token in tokens[1:]:
        if (token.page, token.line) != (last.page, last.line):
            lines.append(first.line_text[first.char_start:last.char_end])
            first = token
        last = token
    lines.append(first.line_text[first.char_start:last.char_end])
    return "\n".join(lines)


def source_span(tokens: list[Token], start: int, end: int, context: int = 24) -> dict | None:
    if start == end:
        return None
    selected = tokens[start:end]
    locations = []
    last_page = last_line = None
    for token in selected:
        if (token.page, token.line) != (last_page, last_line):
            locations.append({"page": token.page, "line": token.line,
                              "top": round(token.top, 1), "bottom": round(token.bottom, 1)})
            last_page, last_line = token.page, token.line
    # Coordinates belong to this PDF version; the token range identifies the diff span.
    return {
        "token_start": start,
        "token_end": end,
        "page_start": selected[0].page,
        "page_end": selected[-1].page,
        "locations": locations,
        "text": extracted_text(selected),
        "context": extracted_text(tokens[max(0, start - context):min(len(tokens), end + context)]),
    }


def score_change(old_tokens: list[Token], new_tokens: list[Token], coarse: bool) -> tuple[int, list[str]]:
    # Coarse spans remain available but get no fine-grained ranking signals.
    if coarse:
        return 0, ["coarse_alignment"]
    old_keys = [t.key for t in old_tokens]
    new_keys = [t.key for t in new_tokens]
    keys = old_keys + new_keys
    reasons = []
    score = 0
    if [k for k in old_keys if NUMBER_RE.match(k)] != [k for k in new_keys if NUMBER_RE.match(k)]:
        score += 5
        reasons.append("number_changed")
    signal = sorted(set(keys) & HIGH_SIGNAL)
    if signal:
        score += 3
        reasons.append("requirement_language")
    if {"shall", "must", "may", "not", "unless", "except"} & set(keys):
        score += 3
        reasons.append("modal_or_exception_language")
    if len(old_keys) + len(new_keys) > 50:
        score += 1
        reasons.append("long_change")
    return score, reasons


def compare_tokens(old: list[Token], new: list[Token]) -> list[dict]:
    # Align normalized keys, then map each changed range back to original text.
    old_keys = [token.key for token in old]
    new_keys = [token.key for token in new]
    ranges = group_changes(raw_changes(old_keys, new_keys))
    findings = []
    # IDs follow source order within this comparison, not across PDF versions.
    for index, (a0, a1, b0, b1, coarse) in enumerate(ranges, 1):
        # Priority only orders human review; every candidate remains in the corpus.
        score, reasons = score_change(old[a0:a1], new[b0:b1], coarse)
        kind = "modified" if a0 < a1 and b0 < b1 else "deleted" if a0 < a1 else "added"
        findings.append({
            "id": f"change-{index:04d}",
            "kind": kind,
            "alignment": "coarse" if coarse else "token",
            "priority_score": score,
            "priority_reasons": reasons,
            "old": source_span(old, a0, a1),
            "new": source_span(new, b0, b1),
        })
    return findings


def short(text: str, limit: int = 320) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def markdown_report(report: dict, top: int, focus: list[str] | None = None) -> str:
    findings = report["changes"]
    focus = focus or []
    selected = [
        item for item in findings
        if not focus or any(
            term.casefold() in " ".join((item[side] or {}).get("text", "") for side in ("old", "new")).casefold()
            for term in focus
        )
    ]
    ranked = sorted(selected, key=lambda item: (-item["priority_score"], item["id"]))
    result = [
        "# PDF change candidates", "",
        f"Old: `{report['old']['path']}` ({report['old']['pages']} pages)", "",
        f"New: `{report['new']['path']}` ({report['new']['pages']} pages)", "",
        f"Found **{len(findings)} candidate changes**. Showing the top {min(top, len(selected))}"
        + (f" matching {', '.join(focus)}" if focus else "") + " by heuristic priority. "
        "Priority is for review order, not a finding of legal materiality.", "",
    ]
    for side in ("old", "new"):
        for warning in report[side]["warnings"]:
            result.append(f"- {side.title()} extraction warning: {warning}")
    if report["old"]["warnings"] or report["new"]["warnings"]:
        result.append("")
    for item in ranked[:top]:
        result.append(f"## {item['id']} · {item['kind']} · priority {item['priority_score']}")
        result.append("")
        if item["alignment"] == "coarse":
            result.append("Alignment is coarse; inspect both source regions before interpreting this change.")
            result.append("")
        for side in ("old", "new"):
            span = item[side]
            if span:
                pages = str(span["page_start"]) if span["page_start"] == span["page_end"] else f"{span['page_start']}–{span['page_end']}"
                result.append(f"**{side.title()} PDF page {pages}:** {short(span['text'])}")
                result.append("")
                result.append(f"Context: {short(span['context'], 500)}")
                result.append("")
        if item["priority_reasons"]:
            result.append("Signals: " + ", ".join(item["priority_reasons"]))
            result.append("")
    return "\n".join(result)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("old_pdf", type=Path)
    parser.add_argument("new_pdf", type=Path)
    parser.add_argument("--json", type=Path, help="Write all candidates and source locations to JSON")
    parser.add_argument("--markdown", type=Path, help="Write a ranked human-readable report")
    parser.add_argument("--top", type=int, default=40, help="Candidates to show in Markdown/stdout (default: 40)")
    parser.add_argument("--focus", action="append", default=[],
                        help="Show only candidates whose changed text contains this phrase; repeatable. JSON still includes all changes")
    args = parser.parse_args()
    for path in (args.old_pdf, args.new_pdf):
        if not path.is_file():
            parser.error(f"PDF not found: {path}")
    if args.top < 0:
        parser.error("--top must be nonnegative")
    old, old_meta = read_pdf(args.old_pdf)
    new, new_meta = read_pdf(args.new_pdf)
    if not old or not new:
        raise SystemExit("No extractable text in one or both PDFs. OCR is required before comparison.")
    report = {
        "method": "unique 8-token anchors, ordered alignment, local token diff",
        "extractor": f"pdfplumber {pdfplumber.__version__}",
        "old": old_meta,
        "new": new_meta,
        "changes": compare_tokens(old, new),
    }
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    md = markdown_report(report, args.top, args.focus)
    if args.markdown:
        args.markdown.parent.mkdir(parents=True, exist_ok=True)
        args.markdown.write_text(md, encoding="utf-8")
    else:
        print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
