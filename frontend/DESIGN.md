# Strata interface direction

## Purpose
A regulatory manager begins with the company's obligation text. A docket filing is introduced later; review findings attach to the relevant obligation and lead back to exact source changes.

## Layout
- A slim project header and two destinations: Project artifacts and Docket.
- Project artifacts uses a file list and a centered, readable document page. No summary cards or candidate-change feed.
- Findings sit in the obligation margin. Opening one shows a review panel with reasoning and a vertical set of cited source changes.
- The docket shows visible document versions. A prepared final filing is introduced with one explicit action. The source view can show the PDF or an extracted, Git-style passage diff for a cited change.
- On narrow screens, the file list becomes a short row and the finding panel becomes a full-width sheet.

## Visual language
- Background `#f3f4f2`, paper `#ffffff`, ink `#20262b`, secondary text `#66717a`, rule `#dbe0df`, accent `#3d5f91`.
- IBM Plex Sans for interface and document text; IBM Plex Mono for version tags, change IDs, and diff lines.
- Use whitespace and thin dividers for structure. Reserve solid color for the primary action and selected state. Diff red and green indicate removed and added text only.
- Controls need visible focus, sufficient contrast, and at least 44px touch targets on mobile.

## Content rules
- Use source titles, dates, statuses, and obligation text from stored files. Label assessments as drafts.
- Do not use dashboard metrics, invented statuses, filler claims, emojis, decorative gradients, or perpetual animation.
- A review result does not edit the obligation. Its current version remains v1 until a separate human decision changes it.
