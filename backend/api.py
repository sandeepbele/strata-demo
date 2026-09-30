"""Small disk-backed HTTP adapter for the existing Strata prototype."""

from __future__ import annotations

from difflib import unified_diff
import hashlib
import json
from pathlib import Path
from typing import Literal

import pdfplumber
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel

from backend.reviewer.agent import ReviewContext, load_obligations, read_change
from backend import workflow
from backend.docket_store import MAX_PDF_BYTES, load_manifest
from backend.obligation_store import document_path
from backend.storage import PROJECTS, DOCKETS, docket_file


app = FastAPI(title="Strata API", version="0.1.0")


class DecisionRequest(BaseModel):
    run_id: str | None
    obligation_id: str
    action: Literal["accept", "reject", "route_to_legal"]


class ObligationRequest(BaseModel):
    title: str
    text: str


def _project(project_id: str) -> tuple[Path, dict]:
    if not project_id or "/" in project_id or project_id in (".", ".."):
        raise HTTPException(404, "Project not found")
    path = PROJECTS / project_id
    if not path.is_dir() or not (path / "project.json").is_file():
        raise HTTPException(404, "Project not found")
    return path, json.loads((path / "project.json").read_text(encoding="utf-8"))


def _context(project_id: str, old: str | None = None, new: str | None = None) -> ReviewContext:
    project_dir, project = _project(project_id)
    if not project.get("dockets"):
        raise HTTPException(404, "Project has no linked docket")
    docket_id = project["dockets"][0]
    if "/" in docket_id or docket_id in (".", ".."):
        raise HTTPException(400, "Invalid docket link")
    try:
        # Metadata reads need the version list, but no PDF comparison corpus.
        return ReviewContext.load(project_dir, DOCKETS / docket_id, old, new,
                                  metadata_only=old is None)
    except (FileNotFoundError, ValueError, KeyError) as exc:
        raise HTTPException(409, f"Project source data is unavailable: {exc}") from exc


def _obligations(project_dir: Path, project: dict) -> list:
    try:
        return load_obligations(document_path(project_dir, project["obligations_file"]))
    except (FileNotFoundError, ValueError, KeyError) as exc:
        raise HTTPException(409, f"Project obligations are unavailable: {exc}") from exc


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/api/projects")
def list_projects() -> list[dict]:
    projects = []
    for path in sorted(PROJECTS.glob("*/project.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        projects.append({key: data.get(key) for key in ("id", "name", "organization", "kind", "dockets")})
    return projects


@app.get("/api/projects/{project_id}")
def get_project(project_id: str) -> dict:
    project_dir, project = _project(project_id)
    obligations = [item.__dict__ for item in _obligations(project_dir, project)]
    dockets = []
    for docket_id in project.get("dockets", []):
        manifest_path = DOCKETS / docket_id / "manifest.json"
        if manifest_path.is_file():
            manifest = load_manifest(DOCKETS / docket_id)
            dockets.append({key: manifest[key] for key in ("docket_id", "agency", "title", "versions", "comparisons")})
    return {**project, "obligations": obligations, "docket_details": dockets}


@app.get("/api/projects/{project_id}/workflow")
def get_workflow(project_id: str) -> dict:
    project_dir, project = _project(project_id)
    obligations = _obligations(project_dir, project)
    context = _context(project_id) if project.get("dockets") else None
    return workflow.get_state(project_id, context, len(obligations))


@app.post("/api/projects/{project_id}/obligations")
def add_obligation(project_id: str, request: ObligationRequest) -> dict:
    project_dir, project = _project(project_id)
    obligations = _obligations(project_dir, project)
    context = _context(project_id) if project.get("dockets") else None
    try:
        obligation, state = workflow.add_obligation(
            project_id, context, project_dir, project["obligations_file"],
            len(obligations), request.title, request.text,
        )
    except ValueError as exc:
        raise HTTPException(409 if "review" in str(exc) else 422, str(exc)) from exc
    return {"obligation": obligation, "workflow": state}


@app.post("/api/projects/{project_id}/obligations/{obligation_id}/review")
def review_obligation(project_id: str, obligation_id: str) -> dict:
    project_dir, project = _project(project_id)
    obligations = _obligations(project_dir, project)
    obligation = next((item for item in obligations if item.id == obligation_id), None)
    if obligation is None:
        raise HTTPException(404, "Obligation not found")
    context = _context(project_id)
    try:
        return workflow.review_existing_obligation(project_id, context, obligation, len(obligations))
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/projects/{project_id}/introduce-change")
def introduce_change(project_id: str) -> dict:
    project_dir, project = _project(project_id)
    context = _context(project_id)
    obligations = _obligations(project_dir, project)
    return workflow.introduce(project_id, context, obligations)


@app.post("/api/projects/{project_id}/revisions")
@app.post("/api/projects/{project_id}/filings")
async def add_docket_filing(
    project_id: str, request: Request,
    revises: str | None = Query(default=None, min_length=1),
    title: str = Query(min_length=1, max_length=160),
    original_filename: str = Query(min_length=1, max_length=255),
    issued_on: str = Query(min_length=10, max_length=10),
    status: Literal["draft_proposal", "proposed", "final"] = "draft_proposal",
) -> dict:
    project_dir, project = _project(project_id)
    context = _context(project_id)
    state = workflow.get_state(project_id, context, len(_obligations(project_dir, project)))
    if state["review"]["status"] == "reviewing":
        raise HTTPException(409, "Wait for the current review to finish")
    if revises is not None and revises not in state["visible_versions"]:
        raise HTTPException(409, "Select a filing already visible in this docket")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_PDF_BYTES:
            raise HTTPException(413, "PDF exceeds the 25 MB limit")
    try:
        # PDF extraction and token comparison are synchronous, so run them off the event loop.
        version, updated = await run_in_threadpool(
            workflow.add_uploaded_document, project_id, context, _obligations(project_dir, project),
            revises, title, issued_on, status, original_filename, bytes(body),
        )
    except ValueError as exc:
        raise HTTPException(409 if "already" in str(exc) else 422, str(exc)) from exc
    return {"version": version, "workflow": updated}


@app.post("/api/projects/{project_id}/reset-demo")
def reset_demo(project_id: str) -> dict:
    project_dir, project = _project(project_id)
    context = _context(project_id)
    obligations = _obligations(project_dir, project)
    try:
        return workflow.reset(project_id, context, len(obligations))
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/projects/{project_id}/decisions")
def record_decision(project_id: str, decision: DecisionRequest) -> dict:
    project_dir, project = _project(project_id)
    context = _context(project_id)
    obligations = _obligations(project_dir, project)
    try:
        return workflow.decide(project_id, context, len(obligations),
                               decision.run_id, decision.obligation_id, decision.action)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


def _require_visible_comparison(project_id: str, data: ReviewContext) -> None:
    state = workflow.get_state(project_id, data, 0)
    if not data.comparison or data.comparison["new"] not in state["visible_versions"]:
        raise HTTPException(404, "No document comparison is visible yet")


def _selected_context(project_id: str, old: str | None, new: str | None) -> ReviewContext:
    if (old is None) != (new is None):
        raise HTTPException(400, "Both comparison versions are required")
    if old:
        return _context(project_id, old, new)
    data = _context(project_id)
    state = workflow.get_state(project_id, data, 0)
    latest = state["runs"][-1] if state["runs"] else None
    pair = latest.get("comparison") if latest else None
    # Without an explicit pair, show the latest saved review's source comparison.
    return _context(project_id, pair["old"], pair["new"]) if pair else data


@app.get("/api/projects/{project_id}/changes")
def list_changes(
    project_id: str,
    old: str | None = None,
    new: str | None = None,
    query: str = Query(default="", max_length=200),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=30, ge=1, le=100),
) -> dict:
    data = _selected_context(project_id, old, new)
    _require_visible_comparison(project_id, data)
    changes = data.changes["changes"]
    if query.strip():
        # The UI searches the full corpus. The agent's bounded, obligation-aware
        # retrieval remains in search_changes for assessment runs.
        words = query.casefold().split()
        changes = [change for change in changes if all(
            word in (change["id"] + " " + " ".join((change.get(side) or {}).get(field, "")
                             for side in ("old", "new") for field in ("text", "context"))).casefold()
            for word in words
        )]
    else:
        changes = sorted(changes, key=lambda item: (-item["priority_score"], item["id"]))
    start = (page - 1) * page_size
    return {
        "total": len(changes), "page": page, "page_size": page_size,
        "items": [{
            "id": item["id"], "kind": item["kind"], "alignment": item["alignment"],
            "priority_score": item["priority_score"], "priority_reasons": item["priority_reasons"],
            "old_page": (item.get("old") or {}).get("page_start"),
            "new_page": (item.get("new") or {}).get("page_start"),
            "old_excerpt": " ".join((item.get("old") or {}).get("text", "").split())[:360],
            "new_excerpt": " ".join((item.get("new") or {}).get("text", "").split())[:360],
        } for item in changes[start:start + page_size]],
    }


@app.get("/api/projects/{project_id}/changes/{change_id}")
def get_change(project_id: str, change_id: str, old: str | None = None, new: str | None = None) -> dict:
    try:
        data = _selected_context(project_id, old, new)
        _require_visible_comparison(project_id, data)
        detail = read_change(data, change_id)
        # These coordinates belong to their own source version. They let the
        # reader highlight a change in a full page without treating page/line
        # numbers as identities across versions.
        for side in ("old", "new"):
            detail[side]["locations"] = [
                {"page": item["page"], "line": item["line"]}
                for item in (data.by_id[change_id].get(side) or {}).get("locations", [])
            ]
        lines = list(unified_diff(
            detail["old"]["text"].splitlines(), detail["new"]["text"].splitlines(),
            fromfile=f"{detail['old']['version']} · {detail['old']['status']}",
            tofile=f"{detail['new']['version']} · {detail['new']['status']}",
            lineterm="", n=3,
        ))
        detail["diff_lines"] = [{
            "type": "meta" if line.startswith(("---", "+++", "@@")) else
                    "removed" if line.startswith("-") else
                    "added" if line.startswith("+") else "context",
            "text": line,
        } for line in lines[:250]]
        detail["diff_truncated"] = len(lines) > 250
        return detail
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.get("/api/projects/{project_id}/sources/{version}")
def get_source(project_id: str, version: str):
    data, meta, path = _visible_source(project_id, version)
    return FileResponse(path, media_type="application/pdf",
                        filename=meta.get("original_filename", f"{data.manifest['docket_id']}-{version}.pdf"),
                        content_disposition_type="inline")


def _visible_source(project_id: str, version: str) -> tuple[ReviewContext, dict, Path]:
    data = _context(project_id)
    meta = data.manifest["versions"].get(version)
    if meta is None:
        raise HTTPException(404, "Source version not found")
    state = workflow.get_state(project_id, data, 0)
    if version not in state["visible_versions"]:
        raise HTTPException(404, "Source version is not in this docket yet")
    path = docket_file(data.docket_dir, meta["file"])
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != meta["sha256"]:
        raise HTTPException(409, "Source PDF is missing or does not match its recorded hash")
    return data, meta, path


@app.get("/api/projects/{project_id}/sources/{version}/pages/{page}")
def get_source_page(project_id: str, version: str, page: int) -> dict:
    _, meta, path = _visible_source(project_id, version)
    with pdfplumber.open(path) as pdf:
        if page < 1 or page > len(pdf.pages):
            raise HTTPException(404, "Source page not found")
        lines = pdf.pages[page - 1].extract_text_lines(return_chars=False)
        return {"version": version, "status": meta["status"], "page": page,
                "page_count": len(pdf.pages), "lines": [line["text"] for line in lines]}
