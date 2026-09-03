# ReadPaper command workflow

All mutating or context-bearing calls require a fresh UUIDv4-shaped `cr_<32 lowercase hex>` client request ID. Retrying an identical lost response uses the same ID; a genuinely new read, render, record, or response attempt uses a new ID.

## New paper

1. Call `prepare <source> --task-id <task> --user-turn-id <turn> --client-request-id <cr>`.
2. Inspect the returned artifact list, unsupported items, warnings, unit inventory, visual inventory, and budget estimate. Preparation is not reading.
3. In the same root response execution call `answer <run> --begin ...` before any read/render.
4. Show the proposed scope. Full scope includes the main paper, references, appendices, and all supported public supplementary items. A reduced scope requires the user's exact exclusion turn and structured excluded refs.
5. Lock it with `record --kind scope_confirmation`.
6. Read fixed batches serially in inventory order. Validate each returned unit ID, full content hash, page/chunk/char/byte bounds, and all three markers. A truncated or mismatched envelope is a blocker.
7. Render and actually open every required visual unit. Record the printed-label state for each PDF page after opening it.
8. Repeat all required text and visual units in one uninterrupted Main synthesis epoch, then record the understanding note.
9. Run both content audit roles through source-first and note-comparison stages. Resolve every finding from reopened confirmed source locations.
10. Draft the answer for the current response attempt, run flow audit when required, remediate, finalize, reopen relevant locators, and record grounding.
11. Call `check`. Resolve blockers until it returns `ready_to_finalize_content`.
12. Call `answer <run> --finalize --answer-id <answer> ...`. This commits content completion and, for the initial answer, run completion without claiming UI delivery.
13. Return the exact finalized content. Stop observation upgrades delivery to `sent_verified`; an unavailable observation remains a nonblocking delivery warning.

## Follow-up question

Call `answer --begin` before reading any source. Open only the confirmed locations needed for this question in the current attempt and epoch. Create a new draft/finalization/grounding chain, run `check`, then `answer --finalize`. Whole-paper coverage and content audits remain inherited from the immutable complete run and are not repeated.

## Presentation-only artifact edit

When the requested change is limited to formatting, figure embedding, links, headings, or layout and introduces no new paper claim, do not open an answer lifecycle. Edit the report directly and validate the changed artifact, including local image existence and Markdown/render correctness. If a diff changes a claim, number, equation meaning, limitation, or interpretation, switch to the semantic follow-up route.

## Pause, resume, and deletion

- A pause keeps local evidence but clears active execution authority.
- Resume requires a separate user turn. It restores the saved phase, not model memory or live context.
- If answer content is interrupted, resume or abandon it explicitly before starting another paper question.
- `pending_observation` and `delivery_unknown` are delivery metadata, not content blockers. A new user turn converts an unobserved prior delivery to `delivery_unknown` and may begin normally.
- Deletion uses preview and exact observed preview, then a separate exact `DELETE <paper-id> <request-id>` user turn. A preview is never approval.
