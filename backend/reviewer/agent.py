"""Review one supplied obligation against a versioned source comparison."""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass, replace
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
from typing import Literal

import pdfplumber
from backend.docket_store import ensure_prepared_comparison, load_manifest
from backend.storage import ROOT, PROJECTS, DOCKETS, docket_file
from dotenv import dotenv_values
from pydantic import BaseModel, Field
from pydantic_ai import Agent, ModelRetry, RunContext, UsageLimits, capture_run_messages
from pydantic_ai.messages import RetryPromptPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.openrouter import OpenRouterModel
from pydantic_ai.providers.openrouter import OpenRouterProvider
from pydantic_ai_harness import ToolOutputLimits


WORD_RE = re.compile(r"[a-z]+|\d[\d,]*(?:\.\d+)?", re.I)
HEADING_RE = re.compile(r"^##\s+(OBL-[\w-]+)\s+-\s+(.+?)\s*$", re.M)
STOPWORDS = {"the", "and", "for", "from", "with", "that", "this", "our", "are", "due", "may"}


class Citation(BaseModel):
    change_id: str
    side: Literal["old", "new"]


class ImpactAssessment(BaseModel):
    project_id: str
    obligation_id: str
    outcome: Literal["affected", "not_affected", "needs_review"]
    summary: str
    rationale: str
    proposed_action: str
    reviewer_role: str
    open_questions: list[str] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)


@dataclass(frozen=True)
class Obligation:
    id: str
    title: str
    text: str


def load_obligations(path: Path) -> list[Obligation]:
    markdown = path.read_text(encoding="utf-8")
    matches = list(HEADING_RE.finditer(markdown))
    if not matches:
        raise ValueError(f"No OBL headings found in {path}")
    obligations = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(markdown)
        obligations.append(Obligation(match[1], match[2], markdown[match.end():end].strip()))
    if len({item.id for item in obligations}) != len(obligations):
        raise ValueError("Duplicate obligation ID")
    return obligations


def _inside(base: Path, relative: str) -> Path:
    resolved = (base / relative).resolve()
    if not resolved.is_relative_to(base.resolve()):
        raise ValueError("Path escapes source directory")
    return resolved


@dataclass
class ReviewContext:
    project: dict
    manifest: dict
    comparison: dict | None
    changes: dict
    docket_dir: Path
    obligation: Obligation | None = None

    @classmethod
    def load(cls, project_dir: Path, docket_dir: Path, old_version: str | None = None,
             new_version: str | None = None, *, metadata_only: bool = False) -> "ReviewContext":
        project_dir = project_dir.resolve()
        docket_dir = docket_dir.resolve()
        project = json.loads((project_dir / "project.json").read_text())
        manifest = load_manifest(docket_dir)
        if manifest["docket_id"] not in project["dockets"]:
            raise ValueError(f"Project {project['id']} is not linked to {manifest['docket_id']}")
        if (old_version is None) != (new_version is None):
            raise ValueError("Both comparison versions are required")
        if metadata_only:
            comparison = None
        elif old_version:
            comparison = next((item for item in manifest["comparisons"]
                               if item["old"] == old_version and item["new"] == new_version), None)
        else:
            comparison = next((item for item in manifest["comparisons"] if item.get("legacy_direct")),
                              manifest["comparisons"][0] if manifest["comparisons"] else None)
        if comparison is None and not metadata_only and (old_version is not None or manifest["comparisons"]):
            raise ValueError("Source comparison not found")
        changes = {"changes": []}
        if comparison:
            # A missing prepared corpus can be rebuilt; its source hashes must still match.
            if (not docket_file(docket_dir, comparison["changes"]).is_file()
                    and not comparison.get("manual_upload") and not comparison.get("backfill")):
                ensure_prepared_comparison(docket_dir, comparison["old"], comparison["new"])
                manifest = load_manifest(docket_dir)
                comparison = next(item for item in manifest["comparisons"]
                                  if item["old"] == comparison["old"] and item["new"] == comparison["new"]
                                  and item.get("legacy_direct") == comparison.get("legacy_direct"))
            changes = json.loads(docket_file(docket_dir, comparison["changes"]).read_text())
            for side, version in (("old", comparison["old"]), ("new", comparison["new"])):
                expected = manifest["versions"][version]["sha256"]
                if changes[side]["sha256"] != expected:
                    raise ValueError(f"Change corpus {side} hash does not match manifest")
        return cls(project, manifest, comparison, changes, docket_dir)

    @property
    def by_id(self) -> dict[str, dict]:
        return {change["id"]: change for change in self.changes["changes"]}

    def source_version(self, side: str) -> tuple[str, dict]:
        if side not in ("old", "new"):
            raise ValueError("side must be old or new")
        comparison = self.comparison
        version = comparison[side]
        return version, self.manifest["versions"][version]


def _tokens(value: str) -> set[str]:
    # Unique normalized terms: repeated words do not increase a match score.
    return {match.group().casefold() for match in WORD_RE.finditer(value)
            if (len(match.group()) > 2 or match.group().isdigit()) and match.group().casefold() not in STOPWORDS}


def _excerpt(value: str, terms: set[str], width: int = 460) -> str:
    compact = " ".join(value.split())
    positions = [match.start() for match in WORD_RE.finditer(compact) if match.group().casefold() in terms]
    start = max(0, (positions[0] if positions else 0) - 100)
    return compact[start:start + width]


def search_changes(data: ReviewContext, query: str, limit: int = 8, *, context_text: str = "") -> list[dict]:
    """Interleave lexical results for the query and supplied obligation context."""
    if not 1 <= limit <= 20:
        raise ValueError("limit must be between 1 and 20")
    records = []
    # Score every change; ingest priority never removes candidates from search.
    for change in data.changes["changes"]:
        sides = [change.get("old") or {}, change.get("new") or {}]
        changed = " ".join(side.get("text", "") for side in sides)
        context = " ".join(side.get("context", "") for side in sides)
        records.append((change, changed, context, _tokens(changed), _tokens(context)))
    n = len(records)

    def rank(text: str) -> tuple[set[str], list[tuple[float, dict]]]:
        terms = _tokens(text)
        if not terms:
            return terms, []
        # Document frequency counts candidate changes containing a term on either side.
        document_frequency = {term: sum(term in changed or term in context for _, _, _, changed, context in records)
                              for term in terms}
        ranked = []
        for change, changed, context, changed_terms, context_terms in records:
            # This is an IDF-like lexical heuristic, not BM25 or semantic search.
            # Length penalties keep a large span from winning just for containing more terms.
            changed_length = max(1, len(WORD_RE.findall(changed)))
            context_length = max(1, len(WORD_RE.findall(context)))
            score = 0.0
            for term in terms:
                rarity = math.log(1 + (n - document_frequency[term] + 0.5) / (document_frequency[term] + 0.5))
                # Directly changed text weighs more than nearby unchanged context.
                if term in changed_terms:
                    score += rarity * 3 / (1 + changed_length / 180)
                elif term in context_terms:
                    score += rarity * 0.6 / (1 + context_length / 80)
            if score:
                ranked.append((score, change))
        # The ID breaks ties reproducibly; score is retrieval order, not impact.
        ranked.sort(key=lambda pair: (-pair[0], pair[1]["id"]))
        return terms, ranked

    query_terms, query_ranked = rank(query)
    context_terms, context_ranked = rank(context_text)
    selected: list[tuple[float, dict, set[str], str, int]] = []
    seen: set[str] = set()
    for index in range(max(len(query_ranked), len(context_ranked))):
        # Alternate routes so obligation wording can surface a candidate the query missed.
        for route, terms, ranked in (("query", query_terms, query_ranked),
                                     ("obligation_context", context_terms, context_ranked)):
            if index >= len(ranked):
                continue
            score, change = ranked[index]
            if change["id"] not in seen:
                selected.append((score, change, terms, route, index + 1))
                seen.add(change["id"])
                if len(selected) == limit:
                    break
        if len(selected) == limit:
            break
    # Each score belongs to its matching route; interleaving does not compare route scores.
    return [{
        "change_id": change["id"], "retrieval_score": score,
        "match_route": route, "route_rank": route_rank,
        "alignment": change["alignment"],
        "old_page": (change.get("old") or {}).get("page_start"),
        "new_page": (change.get("new") or {}).get("page_start"),
        "old_excerpt": _excerpt((change.get("old") or {}).get("text", ""), terms),
        "new_excerpt": _excerpt((change.get("new") or {}).get("text", ""), terms),
    } for score, change, terms, route, route_rank in selected]


def read_change(data: ReviewContext, change_id: str) -> dict:
    change = data.by_id.get(change_id)
    if change is None:
        raise ValueError(f"Unknown change ID: {change_id}")
    result = {"change_id": change_id, "kind": change["kind"], "alignment": change["alignment"]}
    for side in ("old", "new"):
        version, meta = data.source_version(side)
        span = change.get(side) or {}
        value = span.get("text", "")
        context = span.get("context", "")
        result[side] = {
            "version": version, "status": meta["status"], "source_url": meta["source_url"],
            "page_start": span.get("page_start"), "page_end": span.get("page_end"),
            "location_pages": sorted({item["page"] for item in span.get("locations") or []}),
            "locations": (span.get("locations") or [])[:10],
            "text": value[:12000], "text_truncated": len(value) > 12000,
            "context": context[:2000], "context_truncated": len(context) > 2000,
        }
    return result


def read_source_window(data: ReviewContext, change_id: str, side: Literal["old", "new"], radius_lines: int = 5, page: int | None = None) -> dict:
    """Return extracted lines surrounding a changed location in its actual PDF."""
    if not 0 <= radius_lines <= 12:
        raise ValueError("radius_lines must be between 0 and 12")
    change = data.by_id.get(change_id)
    if change is None:
        raise ValueError(f"Unknown change ID: {change_id}")
    span = change.get(side) or {}
    locations = span.get("locations") or []
    if not locations:
        raise ValueError(f"No {side} source location for {change_id}")
    version, meta = data.source_version(side)
    pdf_path = docket_file(data.docket_dir, meta["file"])
    if hashlib.sha256(pdf_path.read_bytes()).hexdigest() != meta["sha256"]:
        raise ValueError(f"PDF hash mismatch for {version}")
    candidates = [location for location in locations if page is None or location["page"] == page]
    if not candidates:
        raise ValueError(f"No {side} change location on page {page}")
    first = candidates[0]
    page_number, line_number = first["page"], first["line"]
    with pdfplumber.open(pdf_path) as pdf:
        lines = pdf.pages[page_number - 1].extract_text_lines(return_chars=False)
    start = max(0, line_number - 1 - radius_lines)
    end = min(len(lines), line_number + radius_lines)
    return {
        "change_id": change_id, "side": side, "version": version,
        "status": meta["status"], "source_url": meta["source_url"],
        "sha256": meta["sha256"], "pdf_page": page_number,
        "lines": [{"line": index + 1, "text": lines[index]["text"]} for index in range(start, end)],
    }


def validate_assessment(assessment: ImpactAssessment, data: ReviewContext, obligation: Obligation) -> None:
    if assessment.project_id != data.project["id"] or assessment.obligation_id != obligation.id:
        raise ValueError("Assessment project or obligation ID does not match input")
    new_status = data.manifest["versions"][data.comparison["new"]].get("status")
    # Nonfinal sources may prompt review; proposed_action cannot direct a record edit.
    if new_status != "final":
        if assessment.outcome == "affected":
            raise ValueError("A nonfinal source can flag a possible impact for review, not an affected obligation")
        if re.search(r"\b(?:update|revise|amend|modify|replace|edit|add|remove|delete|rewrite|implement|adopt)\b", assessment.proposed_action, re.I):
            raise ValueError("A nonfinal source action must consider or prepare, not direct a record update")
    if assessment.outcome in ("affected", "needs_review") and not assessment.citations:
        raise ValueError("Affected or needs-review assessment requires a citation")
    if assessment.outcome == "needs_review" and not assessment.open_questions:
        raise ValueError("Needs-review assessment requires an open question")
    for citation in assessment.citations:
        change = data.by_id.get(citation.change_id)
        if change is None:
            raise ValueError(f"Unknown cited change: {citation.change_id}")
        span = change.get(citation.side) or {}
        if not span.get("text", "").strip():
            raise ValueError(f"No changed text on {citation.change_id} {citation.side}")


def _successful_tool_calls(messages: list) -> list[ToolCallPart]:
    completed = {part.tool_call_id for message in messages for part in message.parts
                 if isinstance(part, ToolReturnPart) and part.outcome == "success"}
    return [part for message in messages for part in message.parts
            if isinstance(part, ToolCallPart) and part.tool_call_id in completed]


def serialize_assessment(assessment: ImpactAssessment, data: ReviewContext) -> dict:
    """Add exact source text, identity, and location to model-selected citations."""
    result = assessment.model_dump()
    result["review_policy_version"] = 1
    for citation in result["citations"]:
        # The model selects an ID and side; source text and identity come from the corpus.
        version, meta = data.source_version(citation["side"])
        span = data.by_id[citation["change_id"]].get(citation["side"]) or {}
        citation.update({
            "version": version, "status": meta["status"],
            "source_url": meta["source_url"], "source_sha256": meta["sha256"],
            "pdf_page_start": span.get("page_start"), "pdf_page_end": span.get("page_end"),
            "quote": span["text"][:3000], "quote_truncated": len(span["text"]) > 3000,
        })
    return result


INSTRUCTIONS = """You review how a change between two source versions may affect one supplied obligation.
Produce a draft impact assessment for a human reviewer. Use only the supplied project, obligation,
comparison metadata, change records, and source passages as evidence.
Work within this evidence budget: make one or two focused searches using several distinctive terms;
make a third search only if those find no plausible candidate. Inspect at most three of the most
relevant changes, favoring governing text over summaries or commentary. For each change you
cite, read its PDF source window on the cited side. Then produce the assessment. Stop searching once
one or two well-supported citations answer the obligation; do not survey the whole source.
The search tool returns separate matches for your query and the supplied obligation context; inspect
both routes because the two documents may use different words for a related concept.
Search both old and new text. Do not use retrieval score or ingest priority as relevance,
certainty, or an impact decision. Use each version's recorded status and date; recency alone does not
establish authority or applicability. The new version's status controls the recommendation stage:
for a draft proposal or proposed filing, return needs_review for a plausible impact (with a cited
change and open question), or not_affected if no relationship is found. Describe the proposal for
consideration, monitoring, or conditional preparation. Do not say the obligation is affected or
recommend updating, revising, amending, modifying, or replacing company records yet. For a final
filing, recommend a human-reviewed obligation update only when the cited final text supports a
direct impact and the supplied company facts support applicability. Otherwise use needs_review
for unresolved applicability or not_affected for no relationship. A final label alone is insufficient.
The recorded status may be user-entered; flag a conflict with source text for human review.
Do not infer project facts or applicability absent from the
provided evidence. State missing evidence as an open question.
Use proposed_action for concrete steps a human can take. Use open_questions for facts or decisions
still unresolved by the evidence; do not repeat an action as an open question.
Return affected only for a well-supported direct impact; needs_review for plausible but unresolved
applicability; not_affected when no relationship is found after the focused searches. Cite the
change ID and old/new side you inspected; exact changed text is attached by the program. Explain the connection between the source
change and the supplied obligation. Do not claim an exhaustive search from lexical retrieval alone. Keep actions concrete
and assign a human reviewer role. All output is a draft for human review."""


def build_agent(model: OpenRouterModel) -> Agent[ReviewContext, ImpactAssessment]:
    agent = Agent(
        model, deps_type=ReviewContext, output_type=ImpactAssessment,
        instructions=INSTRUCTIONS, capabilities=[ToolOutputLimits()], retries={"output": 2},
    )

    @agent.output_validator
    def validate_evidence(ctx: RunContext[ReviewContext], output: ImpactAssessment) -> ImpactAssessment:
        try:
            validate_assessment(output, ctx.deps, Obligation(output.obligation_id, "", ""))
        except ValueError as exc:
            raise ModelRetry(
                "The draft assessment failed evidence validation: " + str(exc) +
                ". Correct or remove unsupported citations; use evidence already read before calling more tools."
            ) from exc
        calls = _successful_tool_calls(ctx.messages)
        missing = []
        for citation in output.citations:
            if not any(call.tool_name == "read_change_tool" and
                       call.args_as_dict().get("change_id") == citation.change_id for call in calls):
                missing.append(f"read_change_tool({citation.change_id})")
            if not any(call.tool_name == "read_source_window_tool" and
                       call.args_as_dict().get("change_id") == citation.change_id and
                       call.args_as_dict().get("side") == citation.side for call in calls):
                missing.append(f"read_source_window_tool({citation.change_id}, {citation.side})")
        if missing:
            # One retry message lists all missing inspections for a focused repair.
            raise ModelRetry("Inspect cited evidence before finalizing, or remove unsupported citations: " +
                             ", ".join(dict.fromkeys(missing)))
        return output

    @agent.tool
    def search_changes_tool(ctx: RunContext[ReviewContext], query: str, limit: int = 8) -> list[dict]:
        """Search both source versions using the query and the supplied obligation as separate routes."""
        obligation = ctx.deps.obligation
        context_text = obligation.title + " " + obligation.text if obligation else ""
        return search_changes(ctx.deps, query, limit, context_text=context_text)

    @agent.tool
    def read_change_tool(ctx: RunContext[ReviewContext], change_id: str) -> dict:
        """Read one change's old and new passages, version metadata, and source locations."""
        return read_change(ctx.deps, change_id)

    @agent.tool
    def read_source_window_tool(ctx: RunContext[ReviewContext], change_id: str, side: Literal["old", "new"], radius_lines: int = 5, page: int | None = None) -> dict:
        """Read nearby lines from a verified source PDF; the selected page must contain this change."""
        return read_source_window(ctx.deps, change_id, side, radius_lines, page)

    return agent


def _tool_trace(messages: list) -> str:
    calls = [part for message in messages for part in message.parts
             if isinstance(part, ToolCallPart) and part.tool_name != "final_result"]
    counts = Counter(part.tool_name for part in calls)
    inspected = [part.args_as_dict().get("change_id") for part in calls
                 if part.tool_name == "read_change_tool"]
    sources = [(part.args_as_dict().get("change_id"), part.args_as_dict().get("side"))
               for part in calls if part.tool_name == "read_source_window_tool"]
    retries = []
    for message in messages:
        for part in message.parts:
            if not isinstance(part, RetryPromptPart):
                continue
            content = part.content
            if isinstance(content, list):
                retries.append("schema_validation")
            elif "Inspect cited evidence" in content:
                retries.append("cited_evidence_not_read")
            elif "draft assessment failed evidence validation" in content:
                retries.append("invalid_citation_or_assessment")
            else:
                retries.append("other_output_retry")
    return (f"tool calls={dict(counts)}; changed passages={inspected}; "
            f"source windows={sources}; output retries={retries}")


def review(data: ReviewContext, obligation: Obligation, agent: Agent, trace_tools: bool = False) -> ImpactAssessment:
    comparison = data.comparison
    old_meta = data.manifest["versions"][comparison["old"]]
    new_meta = data.manifest["versions"][comparison["new"]]
    prompt = json.dumps({
        "project_id": data.project["id"], "project_name": data.project["name"],
        "docket_id": data.manifest["docket_id"], "obligation_id": obligation.id,
        "obligation_title": obligation.title, "obligation_text": obligation.text,
        "source_title": data.manifest.get("title"),
        "comparison": {
            "old_version": comparison["old"], "old_status": old_meta.get("status", "").replace("_", " "), "old_date": old_meta.get("date"),
            "new_version": comparison["new"], "new_status": new_meta.get("status", "").replace("_", " "), "new_date": new_meta.get("date"),
        },
    })
    with capture_run_messages() as captured:
        try:
            result = agent.run_sync(prompt, deps=replace(data, obligation=obligation),
                                    usage_limits=UsageLimits(request_limit=12, tool_calls_limit=20))
        except Exception:
            if trace_tools:
                print(_tool_trace(captured), file=sys.stderr)
            raise
    if trace_tools:
        print(_tool_trace(result.all_messages()), file=sys.stderr)
    assessment = ImpactAssessment.model_validate(result.output)
    validate_assessment(assessment, data, obligation)
    messages = result.all_messages()
    calls = _successful_tool_calls(messages)
    if not any(call.tool_name == "search_changes_tool" for call in calls):
        raise ValueError("Assessment did not search the change corpus")
    for citation in assessment.citations:
        if not any(call.tool_name == "read_change_tool" and
                   call.args_as_dict().get("change_id") == citation.change_id for call in calls):
            raise ValueError(f"Cited change was not inspected: {citation.change_id}")
        if not any(call.tool_name == "read_source_window_tool" and
                   call.args_as_dict().get("change_id") == citation.change_id and
                   call.args_as_dict().get("side") == citation.side for call in calls):
            raise ValueError(f"Cited PDF passage was not inspected: {citation.change_id} {citation.side}")
    return assessment


def read_provider_settings(dotenv_path: Path = ROOT / ".env") -> tuple[str | None, str]:
    """Read only the provider settings; process environment takes precedence."""
    file_values = dotenv_values(dotenv_path) if dotenv_path.is_file() else {}
    key = os.environ["OPENROUTER_API_KEY"] if "OPENROUTER_API_KEY" in os.environ else file_values.get("OPENROUTER_API_KEY")
    model = os.environ["OPENROUTER_MODEL"] if "OPENROUTER_MODEL" in os.environ else file_values.get("OPENROUTER_MODEL")
    return key, model or "openai/gpt-4.1-mini"


def main() -> None:
    from backend.obligation_store import document_path

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=PROJECTS / "project-1")
    parser.add_argument("--docket", type=Path, default=DOCKETS / "R.25-06-019")
    parser.add_argument("--obligation", help="Review one obligation ID; default: all")
    parser.add_argument("--output", type=Path, help="Write JSON instead of printing it")
    parser.add_argument("--trace-tools", action="store_true", help="Print only tool names and change IDs for diagnosis")
    args = parser.parse_args()
    data = ReviewContext.load(args.project, args.docket)
    obligations = load_obligations(document_path(args.project.resolve(), data.project["obligations_file"]))
    if args.obligation:
        obligations = [item for item in obligations if item.id == args.obligation]
        if not obligations:
            parser.error(f"Unknown obligation: {args.obligation}")
    key, model_name = read_provider_settings()
    if not key:
        parser.error("OPENROUTER_API_KEY is required in the environment or repository .env")
    agent = build_agent(OpenRouterModel(model_name, provider=OpenRouterProvider(api_key=key)))
    assessments = [serialize_assessment(review(data, item, agent, trace_tools=args.trace_tools), data) for item in obligations]
    comparison = data.comparison
    output = json.dumps({"project_id": data.project["id"], "docket_id": data.manifest["docket_id"],
                         "old_version": comparison["old"], "new_version": comparison["new"], "status": "draft",
                         "assessments": assessments}, indent=2)
    if args.output:
        args.output.write_text(output + "\n", encoding="utf-8")
    else:
        print(output)


if __name__ == "__main__":
    main()
