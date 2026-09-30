# Strata local review workspace

Strata is a local prototype for reviewing how changes in regulatory filings may affect a company's internal obligations.

The repository includes:

- a React/Vite frontend
- a FastAPI backend
- a PDF comparison tool
- a reviewer workflow that turns candidate filing changes into draft impact assessments

The prepared demo uses three CPUC filings and a synthetic company project. Human review is still required; reviewer output is a draft.

The current implemented scope is described in `PRD_v4.md` and `TDD_v3.md`.


## Prerequisites

Before running the app, ensure you have the following installed:

- Python 3.10 or higher
- Node.js 20 or higher, and pnpm
- OpenRouter API key (for the reviewer agent)


## 1. Setup

Clone from GitHub:

```bash
git clone https://github.com/sandeepbele/strata-demo.git
cd strata-demo
```

From the repository root, create a Python environment and install dependencies:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Create a local environment file:

```bash
cp .env.example .env
```

Add your OpenRouter key to `.env`:

```text
OPENROUTER_API_KEY=...
```

You can optionally set `OPENROUTER_MODEL` to a model that supports tool calling and structured output.

`.env` is ignored by Git and should remain private.

## 2. Run the app

Use two terminals from the repository root.

### Terminal 1 — backend

```bash
.venv/bin/python -m uvicorn backend.api:app --host 127.0.0.1 --port 8000
```

### Terminal 2 — frontend

```bash
cd frontend
pnpm install
pnpm dev
```

Open:

```text
http://127.0.0.1:5173
```

Vite proxies `/api` requests to the local FastAPI server.

## 3. Run the prepared demo

For a quick exploration, the prototype comes with a prepared demo project, `precanned_demo_resource_procurement`, with synthetic obligations and three filings from CPUC docket `R.25-06-019`. The [fixture sources and hashes](data/seed/fixtures/cpuc/README.md) identify the bundled PDFs.

The demo docket contains:

- `v1` — January 14, 2026 proposal
- `v1a` — February 24, 2026 revised proposal
- `v2` — March 5, 2026 final decision


### Demo flow

1. Open the app at `http://127.0.0.1:5173`. Select `precanned_demo_resource_procurement` from the project list.
2. Start on the **Obligations** view and read the synthetic company obligations.
3. Open the **Docket**. Initially only `v1` is visible.
4. Click **Introduce new version** to reveal `v1a`.
5. Strata compares `v1` with `v1a` and starts one background review per obligation.
6. Return to **Obligations** or use the Reviews rail to watch review status and flagged-obligation counts. Review takes few seconds per obligation. It updates review status on UI.
7. On a flagged obligation, click on "Review findings".
8. On right side drawer, review the draft impact assessment with:
   - cited filing changes
   - the draft finding
   - collapsed reasoning
   - proposed actions
   - open questions
9. Open a citation to see the extracted source page, highlighted changed lines, and the corresponding text from the other version.
10. Record a manager decision: **Accept**, **Reject**, or **Route to Legal**.
11. Return to the Docket and click **Introduce new version** to reveal `v2`.
12. Review the final-stage comparison and resulting assessments.

Earlier review runs remain available in the Reviews rail.

### Replay the demo

Use **Reset to v1** in the Docket.

Reset:

- hides `v1a` and `v2`
- clears saved review runs for the prepared demo
- preserves the bundled PDFs and locally generated comparisons
- preserves the current obligation document, including obligations added locally

The prepared-demo reset is disabled after a manual PDF upload so uploaded filing history is not accidentally hidden or erased.

## 4. Add your own filing

Use **Add PDF** in the Docket.

Provide:

- PDF
- title
- filing date
- source status: Draft proposal, Proposed, or Final

Choose:

- **New filing** for an independent tracked document
- **Revision of existing filing** to compare it against a prior version and start impact review

Uploads are stored under:

```text
data/local/uploads/<docket-id>/
```

Current upload limits:

- maximum 25 MB
- PDF must contain extractable text
- OCR is not implemented
- URL ingestion is not implemented

## 5. Run the reviewer directly

To review one synthetic obligation from the CLI:

```bash
.venv/bin/python -m backend.reviewer.agent \
  --project data/seed/projects/project-1 \
  --obligation OBL-01 \
  --output /tmp/obl-01-impact.json
```

Omit `--obligation` to review all obligations.

Add `--trace-tools` to print tool names, change IDs, and retry categories for diagnosis.

The reviewer output is a draft impact assessment with:

- outcome
- explanation
- proposed action
- reviewer role
- open questions
- validated change citations

A failed citation check raises an error instead of emitting a verified result.

## 6. Compare two PDFs directly

```bash
.venv/bin/python -m backend.diff \
  data/seed/fixtures/cpuc/proposed-2026-01-14.pdf \
  data/seed/fixtures/cpuc/final-2026-03-05.pdf \
  --json data/reports/cpuc-changes.json \
  --markdown data/reports/cpuc-changes.md
```

The JSON contains all detected candidates with old/new text, page and line locations, source hashes, extraction warnings, and review-priority signals.

The Markdown report shows the top 40 candidates by default.

Useful options:

```bash
--top 100
--focus 2031
```

`--focus` can be repeated.

## 7. Run tests

```bash
PYDANTIC_AI_NO_BANNER=1 .venv/bin/python -m unittest discover -s tests -v
```

The tests cover retrieval cases, citations, reviewer tool use, and workflow behavior. They are offline checks and do not measure how often a live model reaches the correct impact judgment.

## 8. Important limitations

This prototype intentionally keeps several boundaries visible:

- reviewer output requires human review
- retrieval is lexical and can miss relevant changes when terminology differs
- PDF extraction can produce reading-order errors, especially around tables and footnotes
- image-only PDFs require OCR, which is not implemented
- citation pages should be checked against the original PDF before treating them as evidence
- **Route to Legal** records local workflow state only; it sends no notification
- workflow state is stored in local JSON files; there is no database
- editing or deleting existing obligations, project creation, impact briefs, and general activity history are not implemented

Workflow state and review history are stored under:

```text
data/local/workflows/<project-id>.json
```

Locally added obligations are stored under:

```text
data/local/obligations/<project-id>.md
```

The bundled project files remain unchanged.

`data/seed/` contains versioned demo inputs. `data/local/` and `data/reports/` are ignored by Git. The former contains saved uploads and review state; the latter contains generated CLI reports.
