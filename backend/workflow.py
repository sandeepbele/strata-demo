"""Disk-backed demonstration workflow: publish a prepared docket version and review obligations."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from threading import Lock, Thread
from uuid import uuid4

from pydantic_ai.models.openrouter import OpenRouterModel
from pydantic_ai.providers.openrouter import OpenRouterProvider

from backend.reviewer.agent import (
    ROOT, ReviewContext, build_agent, read_provider_settings,
    review, serialize_assessment,
)
from backend.docket_store import add_filing, add_revision, ensure_backfill_comparison, ensure_prepared_comparison
from backend.obligation_store import append_obligation
from backend.storage import WORKFLOWS

DATA = WORKFLOWS
_lock = Lock()
# Active workers live only in this process; saved runs live in project JSON.
_active: dict[str, str] = {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _path(project_id: str) -> Path:
    return DATA / f"{project_id}.json"


def _write(project_id: str, state: dict) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    destination = _path(project_id)
    temporary = destination.with_name(f"{destination.name}.{uuid4()}.tmp")
    temporary.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    # Replace the complete JSON file so readers never see a partial write.
    os.replace(temporary, destination)


def _default(project_id: str, context: ReviewContext | None, obligation_count: int) -> dict:
    # The first prepared pair supplies the starting PDF; no diff is loaded yet.
    comparison = next((item for item in context.manifest["comparisons"] if not item.get("legacy_direct")), None) if context else None
    return {
        "project_id": project_id,
        "visible_version": comparison["old"] if comparison else None,
        "visible_versions": [comparison["old"]] if comparison else [],
        "obligation_version": "v1",
        "review": {"status": "idle", "completed": 0, "total": obligation_count,
                   "started_at": None, "finished_at": None, "error": None, "run_id": None},
        "results": [],
        "runs": [],
        "decisions": [],
    }


def _snapshot(state: dict, context: ReviewContext | None, trigger: str,
              obligation_ids: list[str] | None = None) -> dict:
    comparison = context.comparison if context else None
    # History records the pair and results, not a copy of the change corpus.
    return {
        "run_id": state["review"].get("run_id"),
        "trigger": trigger,
        "obligation_version": state["obligation_version"],
        "obligation_ids": obligation_ids,
        "comparison": {
            "docket_id": context.manifest["docket_id"],
            "old": comparison["old"], "new": comparison["new"],
        } if comparison else None,
        "review": deepcopy(state["review"]),
        "results": deepcopy(state["results"]),
    }


def _ensure_runs(state: dict, context: ReviewContext | None) -> dict:
    if "runs" not in state:
        state["runs"] = []
        if state["review"]["status"] != "idle":
            # Older saved workflows did not record why a run was started.
            state["runs"].append(_snapshot(state, context, "unknown"))
    return state


def _sync_current_run(state: dict) -> None:
    run_id = state["review"].get("run_id")
    for run in reversed(state["runs"]):
        if run["run_id"] == run_id:
            run["review"] = deepcopy(state["review"])
            run["results"] = deepcopy(state["results"])
            return


def get_state(project_id: str, context: ReviewContext | None, obligation_count: int) -> dict:
    path = _path(project_id)
    state = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else _default(project_id, context, obligation_count)
    _ensure_runs(state, context)
    state.setdefault("decisions", [])
    if "visible_versions" not in state:
        versions = list(context.manifest["versions"]) if context else []
        current = state.get("visible_version")
        state["visible_versions"] = versions[:versions.index(current) + 1] if current in versions else []
    # A saved 'reviewing' state cannot resume after its worker process exits.
    if state["review"]["status"] == "reviewing" and project_id not in _active:
        state["review"] = {**state["review"], "status": "interrupted",
                           "error": "Review stopped before completion. Retry to run it again."}
        _sync_current_run(state)
    return state


def _run(project_id: str, run_id: str, context: ReviewContext, obligations: list,
         key: str, model_name: str) -> None:
    # The comparison JSON is already loaded in context before this worker starts.
    try:
        with _lock:
            if _active.get(project_id) != run_id:
                return
        agent = build_agent(OpenRouterModel(model_name, provider=OpenRouterProvider(api_key=key)))
        for obligation in obligations:
            with _lock:
                if _active.get(project_id) != run_id:
                    return
            try:
                assessment = serialize_assessment(review(context, obligation, agent), context)
                result = {"obligation_id": obligation.id, "status": "complete", "assessment": assessment}
            except Exception:
                # Provider errors may contain sensitive request data; keep the UI error generic.
                result = {"obligation_id": obligation.id, "status": "failed",
                          "error": "Review failed for this obligation. Retry the review."}
            with _lock:
                state = json.loads(_path(project_id).read_text(encoding="utf-8"))
                # A reset or newer run makes this worker's result stale.
                if state["review"]["run_id"] != run_id:
                    return
                state["results"].append(result)
                state["review"]["completed"] += 1
                _sync_current_run(state)
                _write(project_id, state)
        with _lock:
            state = json.loads(_path(project_id).read_text(encoding="utf-8"))
            if state["review"]["run_id"] == run_id:
                failures = any(item["status"] == "failed" for item in state["results"])
                state["review"].update(status="partial" if failures else "completed",
                                       finished_at=_now(),
                                       error="Some obligations could not be reviewed. Retry to rerun all." if failures else None)
                _sync_current_run(state)
                _write(project_id, state)
    except Exception:
        with _lock:
            state = json.loads(_path(project_id).read_text(encoding="utf-8"))
            if state["review"]["run_id"] == run_id:
                state["review"].update(status="failed", finished_at=_now(),
                                       error="Review could not start. Retry after checking the backend log.")
                _sync_current_run(state)
                _write(project_id, state)
    finally:
        with _lock:
            if _active.get(project_id) == run_id:
                _active.pop(project_id)


def reset(project_id: str, context: ReviewContext, obligation_count: int) -> dict:
    with _lock:
        if any(item.get("uploaded") for item in context.manifest["versions"].values()):
            raise ValueError("Demo reset is unavailable after a manual upload; uploaded files and reviews are preserved")
        _active.pop(project_id, None)
        previous = get_state(project_id, context, obligation_count)
        state = _default(project_id, context, obligation_count)
        state["obligation_version"] = previous["obligation_version"]
        _write(project_id, state)
        return state


def add_obligation(project_id: str, context: ReviewContext | None, project_dir: Path,
                   obligations_file: str, obligation_count: int,
                   title: str, text: str) -> tuple[dict, dict]:
    launch = None
    with _lock:
        state = get_state(project_id, context, obligation_count)
        if state["review"]["status"] == "reviewing":
            raise ValueError("Wait for the current review to finish")
        backfill = _backfill_context(project_id, context, state)
        obligation = append_obligation(project_dir, obligations_file, title, text)
        current = state["obligation_version"]
        state["obligation_version"] = f"v{int(current[1:]) + 1}"
        if backfill:
            state, launch = _begin_locked(project_id, state, backfill, [obligation], "obligation_added")
        elif state["review"]["status"] == "idle":
            state["review"]["total"] = obligation_count + 1
            _write(project_id, state)
        else:
            _write(project_id, state)
    if launch:
        Thread(target=_run, args=launch, daemon=True).start()
    return obligation.__dict__, state


def _backfill_context(project_id: str, context: ReviewContext | None,
                      state: dict) -> ReviewContext | None:
    if context is None or not state["visible_versions"]:
        return None
    current = state["visible_version"]
    if current not in context.manifest["versions"]:
        return None
    filing_id = context.manifest["versions"][current].get("filing_id")
    lineage = [version for version in state["visible_versions"]
               if context.manifest["versions"].get(version, {}).get("filing_id") == filing_id]
    if len(lineage) < 2:
        return None
    # Compare the first visible version with the current one so a newly added
    # obligation sees earlier changes in this filing's lineage.
    first = lineage[0]
    # Use an existing pair entry, or compute and save a direct comparison.
    ensure_backfill_comparison(context.docket_dir, first, current)
    # The reviewer reads candidates from the selected comparison JSON.
    return ReviewContext.load(ROOT / "data" / "seed" / "projects" / project_id, context.docket_dir, first, current)


def review_existing_obligation(project_id: str, context: ReviewContext,
                               obligation: object, obligation_count: int) -> dict:
    """Backfill one existing obligation without changing its document version."""
    with _lock:
        state = get_state(project_id, context, obligation_count)
        if state["review"]["status"] == "reviewing":
            raise ValueError("Wait for the current review to finish")
        backfill = _backfill_context(project_id, context, state)
        if backfill is None:
            raise ValueError("Add a revision of the current filing before reviewing")
        state, launch = _begin_locked(project_id, state, backfill, [obligation], "obligation_added")
    if launch:
        Thread(target=_run, args=launch, daemon=True).start()
    return state


def decide(project_id: str, context: ReviewContext, obligation_count: int,
           run_id: str | None, obligation_id: str, action: str) -> dict:
    if action not in ("accept", "reject", "route_to_legal"):
        raise ValueError("Invalid decision action")
    with _lock:
        state = get_state(project_id, context, obligation_count)
        latest = state["runs"][-1] if state["runs"] else None
        if latest is None or latest["run_id"] != run_id:
            raise ValueError("Only the latest review can receive a decision")
        result = next((item for item in latest["results"] if item["obligation_id"] == obligation_id), None)
        if (result is None or result["status"] != "complete" or not result.get("assessment")
                or result["assessment"]["outcome"] == "not_affected"):
            raise ValueError("This obligation has no proposal to decide")
        previous = next((item for item in reversed(state["decisions"])
                         if item["run_id"] == run_id and item["obligation_id"] == obligation_id), None)
        if previous and previous["action"] == action:
            return state
        # Later choices append a new decision; they do not rewrite the prior one.
        state["decisions"].append({"id": str(uuid4()), "run_id": run_id,
                                   "obligation_id": obligation_id, "action": action,
                                   "actor_label": "Local manager",
                                   "decided_at": _now()})
        _write(project_id, state)
        return state


def introduce(project_id: str, context: ReviewContext, obligations: list) -> dict:
    with _lock:
        state = get_state(project_id, context, len(obligations))
        # These manifest entries define the demo sequence; their JSON files may
        # not exist until a version is introduced.
        steps = [item for item in context.manifest["comparisons"]
                 if not item.get("legacy_direct") and not item.get("manual_upload")
                 and not item.get("backfill")]
        latest = state["runs"][-1] if state["runs"] else None
        if state["review"]["status"] == "reviewing":
            return state
        if state["review"]["status"] in ("failed", "partial", "interrupted") and latest:
            # Retry the same pair and obligation scope before advancing the demo.
            pair = latest.get("comparison")
            comparison = next((item for item in context.manifest["comparisons"]
                               if item["old"] == pair["old"] and item["new"] == pair["new"]), None) if pair else context.comparison
            trigger = "retry"
            if latest.get("obligation_ids"):
                obligations = [item for item in obligations if item.id in latest["obligation_ids"]]
        else:
            # Advance only to the next pair whose old version is already visible.
            comparison = next((item for item in steps if item["old"] in state["visible_versions"]
                               and item["new"] not in state["visible_versions"]), None)
            trigger = "new_version"
        if comparison is None:
            return state
        if not comparison.get("manual_upload") and not comparison.get("backfill"):
            # First introduction recomputes into data/local/uploads/<docket-id>/;
            # retry reuses that JSON unless it has gone missing.
            ensure_prepared_comparison(context.docket_dir, comparison["old"], comparison["new"],
                                       generate=trigger == "new_version")
        # ReviewContext.load follows the merged manifest's 'changes' path to
        # read the saved JSON and check its old/new hashes against the manifest.
        context = ReviewContext.load(ROOT / "data" / "seed" / "projects" / project_id, context.docket_dir,
                                     comparison["old"], comparison["new"])
        # Save the visible version and review run before starting the model worker.
        state, launch = _begin_locked(project_id, state, context, obligations, trigger)
    if launch:
        Thread(target=_run, args=launch, daemon=True).start()
    return state


def _begin_locked(project_id: str, state: dict, context: ReviewContext,
                  obligations: list, trigger: str) -> tuple[dict, tuple | None]:
    new_version = context.comparison["new"]
    state["visible_version"] = new_version
    if new_version not in state["visible_versions"]:
        state["visible_versions"].append(new_version)
    key, model_name = read_provider_settings()
    run_id = str(uuid4())
    started_at = _now()
    state["review"] = {"status": "reviewing" if key else "failed", "completed": 0,
                       "total": len(obligations), "started_at": started_at,
                       "finished_at": None if key else started_at,
                       "error": None if key else "Set OPENROUTER_API_KEY in the root .env, then retry the review.",
                       "run_id": run_id}
    state["results"] = []
    state["runs"].append(_snapshot(state, context, trigger,
                                   [item.id for item in obligations]))
    if key:
        _active[project_id] = run_id
    # The new version is visible even when no provider key can start a review.
    _write(project_id, state)
    return state, (project_id, run_id, context, obligations, key, model_name) if key else None


def add_uploaded_document(project_id: str, context: ReviewContext, obligations: list,
                          revises: str | None, title: str, issued_on: str,
                          status: str, original_filename: str,
                          pdf_bytes: bytes) -> tuple[str, dict]:
    with _lock:
        state = get_state(project_id, context, len(obligations))
        if state["review"]["status"] == "reviewing":
            raise ValueError("Wait for the current review to finish")
        if revises is None:
            # An independent filing has no old version, so there is no diff to review.
            version = add_filing(context.docket_dir, title, issued_on, status, original_filename, pdf_bytes)
            state["visible_version"] = version
            state["visible_versions"].append(version)
            _write(project_id, state)
            return version, state
        if revises not in state["visible_versions"]:
            raise ValueError("The filing being revised is not visible in this project")
        # add_revision writes the PDF and pair JSON under data/local/uploads/
        # before publishing the manifest entry; load that JSON for review.
        version = add_revision(context.docket_dir, revises, title, issued_on, status, original_filename, pdf_bytes)
        context = ReviewContext.load(ROOT / "data" / "seed" / "projects" / project_id, context.docket_dir,
                                     revises, version)
        state, launch = _begin_locked(project_id, state, context, obligations, "manual_upload")
    if launch:
        Thread(target=_run, args=launch, daemon=True).start()
    return version, state
