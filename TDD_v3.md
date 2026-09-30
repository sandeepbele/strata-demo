# Strata — Technical Design (v0.2)

**Status:** Implemented local prototype, 2026-09-29.

This document describes the current implementation of the prototype in `PRD_v4.md`.

## Architecture

Strata is a local web application with four main parts:

| Part | Implementation | Responsibility |
| --- | --- | --- |
| Frontend | React, TypeScript, Vite | Show obligations, docket files, review status, findings, citations, and manager decisions. |
| API | FastAPI | Serve project and docket data, accept uploads and decisions, and expose review results and source files. |
| PDF comparison | Python, `pdfplumber` | Extract text from two PDF versions and produce candidate changed passages with source locations. |
| Reviewer Agent | PydanticAI + OpenRouter | Search candidate changes and draft one impact assessment per obligation. |
| Local storage | JSON, Markdown, PDF files | Store uploads, review runs, obligation additions, and manager decisions. |

There is no database, separate worker service, authentication layer, or hosted backend.

## Why these components

The prototype uses React/Vite for the UI and FastAPI for the backend. This keeps the existing Python PDF comparison and reviewer code in one place while giving the UI a simple API to work with.

Project and workflow state is stored in local JSON and Markdown files. That is sufficient for a single-user local prototype and keeps the data easy to inspect. A production system would need a database and a more durable job system.

Reviews currently run in a local background thread. If the process stops while a review is running, the run is marked interrupted and can be retried.

Change retrieval is currently lexical. It works well when the obligation and filing use similar terms, but can miss relevant passages expressed differently.

PydanticAI is used as harness for review agent. The reviewer runs through OpenRouter using the configured model. Model output is always treated as a draft; citations are attached from the saved source data and the final decision remains with the user.

## Demo Setup

I have implemeted a prepared demo with a synthetic project and obligations. The demo uses official filings from CPUC docket `R.25-06-019` and a fictional company, Redwood Valley Energy. User can introduced revised proposals on by one by clicking on "Introduce new version" button under docket. New revision triggers obligation review. User can reset to v1 by clicking on "Reset to v1" button.

This demo flow is disabled once user uploads PDF manually.

## Repository data

Seed projects are stored under:

```text
data/seed/projects/<project-id>/
```

Docket PDFs and prepared comparison metadata are stored under:

```text
data/seed/dockets/<docket-id>/
```

Manual uploads and generated comparisons, including prepared comparisons produced on introduction, are stored under:

```text
data/local/uploads/<docket-id>/
```

Review workflow state is stored under:

```text
data/local/workflows/<project-id>.json
```

Locally added obligations are stored under:

```text
data/local/obligations/<project-id>.md
```

The docket manifest records filing versions, dates, recorded status, source information, and PDF hashes.

## Filing ingestion

A PDF can be added as either:

- a new filing
- a revision of an existing filing

For a revision, the user selects the earlier version it revises.

Before accepting an upload, the API:

- limits the file to 25 MB
- checks that it is a PDF
- checks that text can be extracted
- rejects duplicate PDF bytes
- computes a SHA-256 hash

A new filing is stored without comparison or impact review until a later revision is added.

A revision triggers PDF comparison and review.

## PDF comparison

The comparison step extracts text from both PDF versions and identifies candidate changed passages.

Each candidate stores:

- old text
- new text
- page and line locations
- source version information

The implementation removes repeated page-margin text, aligns matching text between versions, and compares the gaps between those anchors.

Large or uncertain regions can be marked `coarse`.

Page and line numbers identify locations inside a source version. They are not stable identifiers across versions.

A priority score can be used to order candidates for review, but it is not a measure of legal importance.

## Review flow

When a filing revision is introduced:

1. The app starts one review for each project obligation.
2. The reviewer searches the candidate changes using:
   - a focused model-generated query
   - the full obligation text
3. The reviewer can inspect a candidate change and nearby source text.
4. The reviewer returns one of:
   - `affected`
   - `needs_review`
   - `not_affected`
5. The result can include:
   - explanation
   - proposed action
   - reviewer role
   - open questions
   - cited change IDs

Before saving the result, the application validates that cited changes exist and that the reviewer inspected the cited source context.

The application then attaches the exact saved source text, version, page, status, hash, and source metadata to each citation.

These checks validate citation integrity only.

## Draft and final filing behavior

The uploaded filing status is supplied by the user.

For a **Draft proposal** or **Proposed** filing:

- findings remain preliminary
- output validation prevents an `affected` result that directly instructs the user to update company records

For a **Final** filing:

- the reviewer may propose an update when the cited final text and available company context support it



## Review state and decisions

Each review run is saved as a snapshot.

A failed or interrupted review remains visible and can be retried. A retry creates a new run instead of replacing the earlier one.

Manager decisions are stored with the review run and obligation.

Supported decisions are:

- Accept
- Reject
- Route to Legal

If the manager changes a decision, the latest choice becomes current while earlier choices remain in local history.

**Route to Legal** records local state only. It does not send an external notification.

## Source integrity

PDF hashes are stored with source metadata.

When source content or comparison data is loaded, the application checks the recorded hashes.

This protects the prototype from accidentally associating review evidence with a different local PDF. It is not a tamper-proof audit system.

## Failure handling

The UI exposes review failures without displaying raw provider error messages.

Expected failure cases include:

- missing OpenRouter configuration
- provider errors
- invalid reviewer output
- citation validation failure
- interrupted local process

These failures remain retryable where possible.

## Demo reset

For the prepared demo, **Reset to v1**:

- hides later prepared filings
- removes saved review runs and decisions for that project
- keeps the prepared PDFs
- keeps locally added obligations

Reset is disabled after a manual PDF upload so uploaded work is not hidden or removed by the demo reset.

## Verification

Run the backend tests with:

```bash
PYDANTIC_AI_NO_BANNER=1 .venv/bin/python -m unittest discover -s tests -v
```

The tests cover the prepared fixtures, workflow behavior, saved decisions, retrieval cases, and citation validation.


Build the frontend with:

```bash
cd frontend
pnpm build
```


## Known limitations

The current prototype has a few important limitations:

- Out of scope items described in PRD.
- Ellaborate CRUD interfaces for obligations, projects, and dockets are not implemented.
- PDF text extraction can be incomplete or out of reading order, and scanned PDFs require OCR.
- Lexical retrieval can miss relevant passages that use different terminology.
- Reviewer agent may exceed its tool call budget (currently set to 20)
- Local file-based state is designed for a single-user prototype, not concurrent or production use.

The current implementation is intended for a local demonstration rather than hosted production deployment.


See `README.md` for setup and demo instructions.
