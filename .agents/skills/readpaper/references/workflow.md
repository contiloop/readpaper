# ReadPaper command workflow

All mutating or context-bearing calls require a fresh UUIDv4-shaped `cr_<32 lowercase hex>` client request ID. Retrying an identical lost response uses the same ID; a genuinely new read, render, record, or response attempt uses a new ID.

## New paper

1. Call `prepare <source> --task-id <task> --user-turn-id <turn> --client-request-id <cr>`. Add `--ingest-only` only when the user explicitly wants the paper read now without a report or answer in this turn; ordinary preparation is answer-required.
2. Inspect the returned artifact list, unsupported items, warnings, section/frame inventory, visual inventory, and residency estimate. If `inventory_inline=false`, obtain section/frame/visual metadata and per-page warnings from `inventory_path`, and the full artifact list including unsupported/excluded candidates from `bundle_manifest_path`, in bounded slices. Full source ranges and hashes live in the inventory; `prepare` returns compact metadata and enforces the serialized envelope limit. Preparation is not reading.
3. Show the proposed scope. Full scope includes the main paper, references, appendices, and all supported public supplementary items. A reduced scope requires the user's exact exclusion turn and structured excluded refs.
4. Lock it with `record --kind scope_confirmation`.
5. Read logical sections in source order. If a section has multiple transport frames, read those frames consecutively in `frame_index` order before moving on. Validate every frame ID, full content hash, source ranges, and all three markers. A truncated or mismatched envelope is a blocker.
6. Render and actually open every required visual unit. Record the printed-label state for each PDF page after opening it.
7. Repeat all required section frames and visual units in one uninterrupted Main synthesis epoch, then record the understanding note.
8. Run both content audit roles through source-first and note-comparison stages. Resolve every finding from reopened confirmed source locations.
9. Call `check` without an answer ID. Resolve blockers until it returns `reading_ready`.
10. Call `run <run> --finalize-reading --task-id <task> --user-turn-id <turn> --client-request-id <cr>`. Confirm `run_state=read_complete` and `active_run_released=true`; the current run remains available for later answers. This also stores Main's reading-finalization stream/epoch.
11. If this is an ingest-only run, acknowledge reading completion without making paper-content claims and stop here. Otherwise call `answer <run> --begin ...` before composing the user-facing response.
12. Draft the answer for the current response attempt, run flow audit when required, and remediate. Follow [grounding.md](grounding.md) to record exact final content and claim spans, confirm canonical locators, reopen their sources in this attempt/epoch, and bind each claim to the fixed finalization record and observed reopen events.
13. Call `check --answer-id <answer>`. Resolve blockers until it returns `ready_to_finalize_content`.
14. Call `answer <run> --finalize --answer-id <answer> ...`. This commits answer-content completion without changing the run's `read_complete` state or claiming UI delivery.
15. Return the exact finalized content. Stop observation upgrades delivery to `sent_verified`; an unavailable observation remains a nonblocking delivery warning.

## Follow-up question

Call `answer --begin` on the current `read_complete` run before answer-specific source reopening. Open only the confirmed locations needed for this question in the current attempt and epoch. Create a new draft/finalization/grounding chain, run `check --answer-id`, then `answer --finalize`. Multiple answers may attach to the same completed reading run. Whole-paper coverage and content audits remain inherited for ingest-only runs and answers after the initial report.

The [grounding contract](grounding.md) applies to these answers too: previous-attempt or previous-epoch source opens do not count, and a new finalization record requires new grounding even when its content hash is unchanged.

## Initial-report context recovery

The first answer of an ordinary answer-required run must begin and finalize in the stream/epoch saved by reading finalization. An abandoned answer does not relax this rule. If compaction or a session change intervenes, `answer --begin` rejects the stale proof and an existing answer's check reports `reading_context_refresh_required`:

1. Reopen all required section frames and visual units in Main's current epoch.
2. Run `check` without an answer ID; existing valid note/audit evidence is retained, but full current-epoch source emission is required again.
3. When it returns `reading_ready`, call `run --finalize-reading` again to refresh the context proof without creating a new run.
4. Begin the first answer, or continue the existing authorized response attempt; review its draft against the reopened source and record fresh current-epoch grounding before `check --answer-id` and `answer --finalize`.

Completion also fails while Main compaction is in progress. Allocation estimates and epoch emission evidence do not prove host memory residency.

## Stop repair

Follow the exact one-shot command returned by Stop. For visual repairs, execute `render`, open the successful response's `data.path` with `view_image`, then rerun `check`. The repair is `awaiting_visual_open` after rendering and `completed` only after the matching Main image-open event. A failed image open does not count. The budget is reserved at request time to prevent duplicate automatic continuations; a new run resets its own allowance.

## Presentation-only artifact edit

When the requested change is limited to formatting, figure embedding, links, headings, or layout and introduces no new paper claim, do not open an answer lifecycle. Edit the report directly and validate the changed artifact, including local image existence and Markdown/render correctness. If a diff changes a claim, number, equation meaning, limitation, or interpretation, switch to the semantic follow-up route.

## Pause, resume, and deletion

- A pause keeps local evidence but clears active execution authority.
- Resume requires a separate user turn. It restores the saved phase, not model memory or live context.
- If answer content is interrupted, resume or abandon it explicitly before starting another paper question.
- `pending_observation` and `delivery_unknown` are delivery metadata, not content blockers. A new user turn converts an unobserved prior delivery to `delivery_unknown` and may begin normally.
- Deletion uses preview and exact observed preview, then a separate exact `DELETE <paper-id> <request-id>` user turn. A preview is never approval.
