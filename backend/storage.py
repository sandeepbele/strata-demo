"""Paths for versioned demo sources and local, unversioned workspace data."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
SEED = DATA / "seed"
PROJECTS = SEED / "projects"
DOCKETS = SEED / "dockets"
LOCAL = DATA / "local"
WORKFLOWS = LOCAL / "workflows"
OBLIGATIONS = LOCAL / "obligations"


def upload_dir(docket_dir: Path) -> Path:
    """Place local additions beside the seed tree, keyed by docket ID."""
    return docket_dir.parents[2] / "local" / "uploads" / docket_dir.name


def docket_file(docket_dir: Path, relative: str) -> Path:
    """Resolve a manifest path within either the seed docket or its local overlay."""
    parts = relative.split("/")
    if "\\" in relative or any(part in ("", ".", "..") for part in parts):
        raise ValueError("Invalid docket file path")
    if parts[0] == "uploads":
        base = upload_dir(docket_dir)
        suffix = parts[1:]
        if not suffix:
            raise ValueError("Invalid docket file path")
    else:
        base = docket_dir
        suffix = parts
    resolved = base.joinpath(*suffix).resolve()
    if not resolved.is_relative_to(base.resolve()):
        raise ValueError("Docket file path escapes source directory")
    return resolved
