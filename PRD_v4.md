# Strata — Product Requirements (v0.3)

## Purpose

Strata helps a regulatory affairs manager review changes in a filing against a project's written obligations.

For each obligation, the prototype can:

- identify candidate filing changes
- show the supporting source text and PDF page
- draft a possible impact and next action
- save the manager's decision

The manager must verify the source and interpretation before acting.

The target user and workflow are product assumptions and have not been validated with a regulatory team.

An alert can establish that a filing arrived, and search can locate matching terms. The manager still needs to determine which version and status govern a passage, what actually changed, whether a company obligation is implicated, and who should decide the next step. This is the narrow change-to-action problem the prototype demonstrates.

## Demo scope

The prepared demo uses official PDFs from CPUC docket `R.25-06-019` and a fictional company, Redwood Valley Energy.

The company's project and obligations are synthetic.

Aim is to show that the protype can diff two large filings, extarct and store changes then review each obligation against these changes to see if any action is needed.


## User flow

1. Open the sample project - "precanned_demo_resource_procurrement" and review its obligations.
2. Open the linked docket.
3. Add document revisions one at a time.
4. When a filing revision is introduced, compare it with the previous version and start a draft review for each obligation.
5. Open a flagged obligation to review:
   - cited filing changes
   - draft finding
   - proposed action
   - reviewer role
   - open questions
6. Open a citation to inspect the extracted source page and original PDF.
7. Record a decision: **Accept**, **Reject**, or **Route to Legal**.


## Requirements

### Sources

The prototype must:

- keep each PDF with its date, recorded status, and SHA-256
- show the prepared filings and locally uploaded PDFs in the linked docket
- reject duplicate PDF bytes
- reject PDFs without extractable text

For manual uploads, the user provides the filing date and status: **Draft proposal**, **Proposed**, or **Final**.

### Comparison

For a filing revision, the prototype must:

- compare the old and new PDFs
- produce candidate changed passages
- preserve source page locations
- keep all detected candidates available for review

Any internal priority score is only for ordering review. It is not a statement of legal importance.

### Review

For each obligation, the prototype must use the obligation text and candidate filing changes to draft:

- an outcome
- an explanation
- a proposed action
- a reviewer role
- open questions
- source citations

For draft or proposed filings, the output may identify a possible impact for review but must not directly recommend updating company records.

For final filings, an update may be proposed only when the cited final text and available company context support it.

### Evidence

A cited result must point to an actual changed passage.

The reviewer must inspect the cited change and nearby source text before returning it.

The saved comparison and filing metadata provide the cited text, source version, page, and source identity.

### Decisions and history

The prototype must:

- let the manager record a decision on the latest review result
- preserve previous review runs
- preserve earlier decisions when a decision is changed
- allow a failed review to be retried without removing the earlier run

## Current limitations

This is a local, single-user prototype.

It does not currently support:

- authentication
- shared review
- Legal notifications
- creating projects in the UI
- linking a different docket in the UI
- editing or deleting existing obligations
- updating project plans or source records from a review decision
- a general activity feed

It only supports pdf as document source.

PDF extraction can misread tables, reading order, or scanned pages. The original PDF remains the source of record.

Does not track remote sources.

It does not extracts obligations from project plans or other documents. Obligations must be entered manually.


## Eval design

These are proposed evals. First label a small set of filing changes and company records with a domain expert. Track evidence recall among the top retrieved candidates, source-location and citation accuracy, unsupported impact claims, expert override rate, and the share of findings that reach a reviewed decision. Measure time from new filing to reviewed decision and model cost/latency per obligation. Keep retrieval misses, citation failures, and interpretation errors separate.

The first user-discovery step is to observe a regulatory affairs manager reviewing a real filing revision, identify the internal record they actually change, and test whether a cited draft helps without lowering trust.


## Release definition

This release is a working **local demonstration** of the workflow above.

For the prepared demo:

- the backend and frontend must run locally
- the Python tests and frontend build must pass
- the prepared filing sequence must be reviewable end to end
- citations must open the relevant source context
- manager decisions must be saved locally

This release does **not** claim:

- legal correctness
- production readiness
- complete retrieval
- live-model accuracy
- multi-user operation

Run and test instructions are in `README.md`.
