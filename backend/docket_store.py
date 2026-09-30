"""Read the bundled docket and local, immutable PDF revisions."""

from __future__ import annotations

from copy import deepcopy
from datetime import date
import hashlib
import json
import os
from pathlib import Path
from threading import Lock
from uuid import uuid4

from backend.diff.pdf import compare_tokens, read_pdf
from backend.storage import docket_file, upload_dir


MAX_PDF_BYTES = 25 * 1024 * 1024
STATUSES = {"draft_proposal", "proposed", "final"}
_lock = Lock()


def load_manifest(docket_dir: Path) -> dict:
    # Seed metadata stays versioned; local uploads and generated comparisons overlay it.
    manifest = json.loads((docket_dir / "manifest.json").read_text(encoding="utf-8"))
    additions = upload_dir(docket_dir) / "manifest.json"
    if additions.is_file():
        extra = json.loads(additions.read_text(encoding="utf-8"))
        manifest["versions"].update(extra.get("versions", {}))
        for item in extra.get("comparisons", []):
            if item.get("generated_prepared"):
                index = next((index for index, existing in enumerate(manifest["comparisons"])
                              if existing["old"] == item["old"] and existing["new"] == item["new"]
                              and existing.get("legacy_direct") == item.get("legacy_direct")), None)
                if index is not None:
                    manifest["comparisons"][index] = item
                    continue
            manifest["comparisons"].append(item)
    return manifest


def ensure_prepared_comparison(docket_dir: Path, old_version: str, new_version: str,
                               *, generate: bool = False) -> dict:
    """Generate a prepared pair locally on introduction or when its saved corpus is missing."""
    with _lock:
        manifest = load_manifest(docket_dir)
        item = next((entry for entry in manifest["comparisons"]
                     if entry["old"] == old_version and entry["new"] == new_version
                     and not entry.get("manual_upload") and not entry.get("backfill")), None)
        if item is None:
            raise ValueError("Prepared comparison not found")
        if docket_file(docket_dir, item["changes"]).is_file() and not generate:
            return item

        versions = manifest["versions"]
        source_paths = []
        for version in (old_version, new_version):
            source = docket_file(docket_dir, versions[version]["file"])
            # Refuse to generate a corpus from bytes that differ from the manifest.
            if hashlib.sha256(source.read_bytes()).hexdigest() != versions[version]["sha256"]:
                raise ValueError("Source PDF does not match its recorded hash")
            source_paths.append(source)
        old_tokens, old_report = read_pdf(source_paths[0])
        new_tokens, new_report = read_pdf(source_paths[1])
        if not old_tokens or not new_tokens:
            raise ValueError("Both PDFs need extractable text")
        comparison_data = {
            "method": "unique 8-token anchors, ordered alignment, local token diff",
            "old": old_report, "new": new_report,
            "changes": compare_tokens(old_tokens, new_tokens),
        }
        uploads = upload_dir(docket_dir)
        uploads.mkdir(parents=True, exist_ok=True)
        pair_id = hashlib.sha256(f"{old_version}\0{new_version}".encode()).hexdigest()[:16]
        filename = f"prepared-{pair_id}.json"
        output = uploads / filename
        temporary = uploads / f"{filename}.{uuid4().hex}.tmp"
        generated = {**item, "changes": f"uploads/{filename}", "generated_prepared": True}
        additions_path = uploads / "manifest.json"
        additions = json.loads(additions_path.read_text(encoding="utf-8")) if additions_path.is_file() else {"versions": {}, "comparisons": []}
        additions["comparisons"] = [entry for entry in additions["comparisons"]
                                    if not (entry["old"] == old_version and entry["new"] == new_version
                                            and entry.get("generated_prepared"))]
        additions["comparisons"].append(generated)
        try:
            temporary.write_text(json.dumps(comparison_data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            os.replace(temporary, output)
            # Publish the overlay pointer only after the comparison file exists.
            manifest_tmp = uploads / f"manifest.{uuid4().hex}.tmp"
            manifest_tmp.write_text(json.dumps(additions, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            os.replace(manifest_tmp, additions_path)
        finally:
            temporary.unlink(missing_ok=True)
        return generated


def _validate_upload(title: str, issued_on: str, status: str, original_filename: str,
                     pdf_bytes: bytes) -> tuple[str, str]:
    title = title.strip()
    if not title or len(title) > 160:
        raise ValueError("Enter a title of at most 160 characters")
    original_filename = original_filename.strip()
    if (not original_filename or len(original_filename) > 255
            or not original_filename.lower().endswith(".pdf")
            or any(char in original_filename for char in ("/", "\\"))
            or any(ord(char) < 32 for char in original_filename)):
        raise ValueError("Enter the original PDF filename without a directory path")
    try:
        date.fromisoformat(issued_on)
    except ValueError as exc:
        raise ValueError("Enter an ISO filing date") from exc
    if status not in STATUSES:
        raise ValueError("Choose draft proposal, proposed, or final")
    if not pdf_bytes.startswith(b"%PDF-") or len(pdf_bytes) > MAX_PDF_BYTES:
        raise ValueError("Choose a PDF up to 25 MB")
    return title, original_filename


def _publish(upload_dir: Path, version: str, metadata: dict, comparison: dict | None = None) -> None:
    additions_path = upload_dir / "manifest.json"
    additions = json.loads(additions_path.read_text(encoding="utf-8")) if additions_path.is_file() else {"versions": {}, "comparisons": []}
    additions = deepcopy(additions)
    additions["versions"][version] = metadata
    if comparison:
        additions["comparisons"].append(comparison)
    temporary = additions_path.with_name(f"manifest.{uuid4().hex}.tmp")
    temporary.write_text(json.dumps(additions, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temporary, additions_path)


def ensure_backfill_comparison(docket_dir: Path, old_version: str, new_version: str) -> dict:
    """Return a hash-checked direct comparison for the visible filing lineage."""
    with _lock:
        manifest = load_manifest(docket_dir)
        versions = manifest["versions"]
        if old_version == new_version or old_version not in versions or new_version not in versions:
            raise ValueError("Two versions of the same filing are required for review")
        if versions[old_version].get("filing_id") != versions[new_version].get("filing_id"):
            raise ValueError("The selected PDFs are not revisions of the same filing")
        existing = next((item for item in manifest["comparisons"]
                         if item["old"] == old_version and item["new"] == new_version), None)
        if existing:
            return existing

        source_paths = []
        for version in (old_version, new_version):
            source = docket_file(docket_dir, versions[version]["file"])
            if hashlib.sha256(source.read_bytes()).hexdigest() != versions[version]["sha256"]:
                raise ValueError("Source PDF does not match its recorded hash")
            source_paths.append(source)
        old_tokens, old_report = read_pdf(source_paths[0])
        new_tokens, new_report = read_pdf(source_paths[1])
        if not old_tokens or not new_tokens:
            raise ValueError("Both PDFs need extractable text")
        comparison_data = {
            "method": "unique 8-token anchors, ordered alignment, local token diff",
            "old": old_report, "new": new_report,
            "changes": compare_tokens(old_tokens, new_tokens),
        }
        uploads = upload_dir(docket_dir)
        uploads.mkdir(parents=True, exist_ok=True)
        pair_id = hashlib.sha256(f"{old_version}\0{new_version}".encode()).hexdigest()[:16]
        filename = f"backfill-{pair_id}.json"
        output = uploads / filename
        temporary = uploads / f"{filename}.{uuid4().hex}.tmp"
        try:
            temporary.write_text(json.dumps(comparison_data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            os.replace(temporary, output)
            item = {"old": old_version, "new": new_version,
                    "changes": f"uploads/{filename}", "backfill": True}
            additions_path = uploads / "manifest.json"
            additions = json.loads(additions_path.read_text(encoding="utf-8")) if additions_path.is_file() else {"versions": {}, "comparisons": []}
            additions["comparisons"].append(item)
            manifest_tmp = uploads / f"manifest.{uuid4().hex}.tmp"
            manifest_tmp.write_text(json.dumps(additions, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            os.replace(manifest_tmp, additions_path)
        finally:
            temporary.unlink(missing_ok=True)
        return item


def add_filing(docket_dir: Path, title: str, issued_on: str,
               status: str, original_filename: str, pdf_bytes: bytes) -> str:
    """Create an independent tracked filing; it has no comparison yet."""
    title, original_filename = _validate_upload(title, issued_on, status, original_filename, pdf_bytes)
    uploads = upload_dir(docket_dir)
    uploads.mkdir(parents=True, exist_ok=True)
    version = "u-" + uuid4().hex[:12]
    pdf_path = uploads / f"{version}.pdf"
    pdf_path.write_bytes(pdf_bytes)
    try:
        with _lock:
            manifest = load_manifest(docket_dir)
            sha256 = hashlib.sha256(pdf_bytes).hexdigest()
            if any(meta["sha256"] == sha256 for meta in manifest["versions"].values()):
                raise ValueError("This PDF is already in the docket")
            try:
                tokens, _ = read_pdf(pdf_path)
            except Exception as exc:
                raise ValueError("Could not extract text from the uploaded PDF") from exc
            if not tokens:
                raise ValueError("The PDF needs extractable text; OCR is not available")
            _publish(uploads, version, {
                "file": f"uploads/{version}.pdf", "filing_id": "f-" + uuid4().hex[:12],
                "title": title, "original_filename": original_filename,
                "date": issued_on, "status": status,
                "sha256": sha256, "source_url": None, "revises": None, "uploaded": True,
            })
        return version
    except Exception:
        pdf_path.unlink(missing_ok=True)
        raise


def add_revision(docket_dir: Path, old_version: str, title: str, issued_on: str,
                 status: str, original_filename: str, pdf_bytes: bytes) -> str:
    """Save one user-declared revision and its full comparison before publishing metadata."""
    title, original_filename = _validate_upload(title, issued_on, status, original_filename, pdf_bytes)

    uploads = upload_dir(docket_dir)
    uploads.mkdir(parents=True, exist_ok=True)
    version = "u-" + uuid4().hex[:12]
    pdf_path = uploads / f"{version}.pdf"
    comparison_path = uploads / f"{old_version}-{version}.json"
    pdf_path.write_bytes(pdf_bytes)
    try:
        with _lock:
            manifest = load_manifest(docket_dir)
            if old_version not in manifest["versions"]:
                raise ValueError("The filing being revised no longer exists")
            old_meta = manifest["versions"][old_version]
            old_path = docket_file(docket_dir, old_meta["file"])
            if hashlib.sha256(old_path.read_bytes()).hexdigest() != old_meta["sha256"]:
                raise ValueError("The earlier PDF does not match its recorded hash")
            if any(meta["sha256"] == hashlib.sha256(pdf_bytes).hexdigest()
                   for meta in manifest["versions"].values()):
                raise ValueError("This PDF is already in the docket")
            try:
                old_tokens, old_report = read_pdf(old_path)
                new_tokens, new_report = read_pdf(pdf_path)
            except Exception as exc:
                raise ValueError("Could not extract text from the uploaded PDF") from exc
            if not old_tokens or not new_tokens:
                raise ValueError("Both PDFs need extractable text; OCR is not available")
            new_report["path"] = str(pdf_path)
            comparison = {
                "method": "unique 8-token anchors, ordered alignment, local token diff",
                "old": old_report, "new": new_report,
                "changes": compare_tokens(old_tokens, new_tokens),
            }
            comparison_path.write_text(json.dumps(comparison, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            # A revision keeps its parent's filing ID; an independent filing gets a new one.
            _publish(uploads, version, {
                "file": f"uploads/{version}.pdf", "title": title,
                "original_filename": original_filename,
                "filing_id": old_meta.get("filing_id", f"{manifest['docket_id']}:decision"),
                "date": issued_on, "status": status,
                "sha256": new_report["sha256"], "source_url": None,
                "revises": old_version, "uploaded": True,
            }, {
                "old": old_version, "new": version,
                "changes": f"uploads/{old_version}-{version}.json", "manual_upload": True,
            })
        return version
    except Exception:
        pdf_path.unlink(missing_ok=True)
        comparison_path.unlink(missing_ok=True)
        raise
