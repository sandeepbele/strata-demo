"""Local, append-only obligation document edits for the demo workspace."""

from __future__ import annotations

import os
from pathlib import Path
import re
from uuid import uuid4

from backend.reviewer.agent import Obligation, _inside, load_obligations
from backend.storage import OBLIGATIONS


DATA = OBLIGATIONS
RESERVED_HEADING = re.compile(r"^##\s+OBL-[\w-]+\s+-\s+", re.M)


def document_path(project_dir: Path, obligations_file: str) -> Path:
    source = _inside(project_dir, obligations_file)
    saved = DATA / f"{project_dir.name}.md"
    return saved if saved.is_file() else source


def append_obligation(project_dir: Path, obligations_file: str,
                      title: str, text: str) -> Obligation:
    title = " ".join(title.split())
    body = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not title or len(title) > 160:
        raise ValueError("Enter a title of at most 160 characters")
    if not body or len(body) > 20000:
        raise ValueError("Enter obligation text of at most 20,000 characters")
    if RESERVED_HEADING.search(body):
        raise ValueError("Enter obligation text without an OBL heading; the app assigns its ID")

    source = document_path(project_dir, obligations_file)
    existing = load_obligations(source)
    numbers = [int(match[1]) for item in existing
               if (match := re.fullmatch(r"OBL-(\d+)", item.id))]
    number = max(numbers, default=0) + 1
    identifier = f"OBL-{number:02d}"
    while identifier in {item.id for item in existing}:
        number += 1
        identifier = f"OBL-{number:02d}"

    saved = DATA / f"{project_dir.name}.md"
    saved.parent.mkdir(parents=True, exist_ok=True)
    current = source.read_text(encoding="utf-8").rstrip()
    updated = f"{current}\n\n## {identifier} - {title}\n\n{body}\n"
    temporary = saved.with_name(f"{saved.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(updated, encoding="utf-8")
        os.replace(temporary, saved)
    finally:
        temporary.unlink(missing_ok=True)
    return Obligation(identifier, title, body)
