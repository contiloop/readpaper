# Answer-grounding contract

Read this before recording `explanation_finalized` or `answer_grounding`, including for follow-up Q&A. Both records must belong to the pending answer, current response attempt and its root Main execution. Resume an interrupted attempt through the lifecycle; do not reuse its evidence as a new attempt.

## Final content and paper-derived claims

Record the exact user-facing Markdown in `explanation_finalized.final_content`, its UTF-8 SHA-256 as `final_content_sha256`, and the important paper-derived claim groups in `paper_claims`. A group may span a paragraph or related statements; do not split every sentence unnecessarily. Distinguish source claims from Main inference in the text. Main and reviewers remain responsible for identifying the important groups and checking whether the source actually supports their meaning.

```json
{
  "answer_id": "<ans_id>",
  "response_attempt_id": "<rsp_id>",
  "final_content": "<exact final Markdown>",
  "final_content_sha256": "<64 lowercase hex>",
  "paper_claims": [
    {
      "claim_id": "claim_1",
      "char_start": 0,
      "char_end": 42,
      "claim_text_sha256": "<hash of final_content[0:42]>"
    }
  ]
}
```

Offsets are Python Unicode character offsets, start-inclusive/end-exclusive, not UTF-8 byte offsets. Hash exact text without normalizing whitespace, math delimiters or line endings. Claim IDs must be unique; each span must be nonempty, within the final text, and match its hash. The content hash must match the entire final text. Save the returned `record_id`.

## Confirm and reopen source

Record `locator_confirmation` with the canonical `locator_id` and full `locator` union object. Confirmation validates bundle, artifact/ref, locked scope and page/image membership. A text span's bounds and hash must match the canonical page text in the inventory. Invalid confirmations return `INVALID_LOCATOR` without emitting a confirmation event.

After `answer --begin` or the current `answer --resume`, reopen the needed source with protected `read` calls or successful `view_image` opens. Use observer-generated `SOURCE_FRAME_EMITTED` / `VISUAL_OPEN_OBSERVED` event IDs from this run's `events.jsonl`; do not invent them or substitute a `RENDER_CREATED` event. The observer binds each reopen to its answer/attempt, root execution and stream/epoch.

For a text-span locator, cited frames must collectively cover its entire character range without gaps and match the inventory frame hashes. A page locator requires its entire canonical page text or a page-image open; an empty/scanned page requires visual opening. Object and image-region locators require opening the corresponding visual, not just nearby extracted text. Rendering alone never counts.

## Bind claims to the fixed finalization

After source reopening and finalization recording, record `answer_grounding`:

```json
{
  "answer_id": "<ans_id>",
  "response_attempt_id": "<rsp_id>",
  "finalization_record_id": "<rec_id returned above>",
  "final_content_sha256": "<same final content hash>",
  "claim_bindings": [
    {
      "claim_id": "claim_1",
      "claim_text_sha256": "<same claim span hash>",
      "confirmed_locator_ids": ["<loc_id>"],
      "source_reopen_event_ids": ["<ev_id>"]
    }
  ]
}
```

Every declared paper claim needs exactly one binding with matching claim hash, one or more confirmed locators, and their covering reopen events. All reopen events must be successful root Main observations after the current attempt started and before grounding, in Main's current stream/epoch. Existing locator confirmations may be reused, but earlier-attempt or earlier-epoch reopen events may not. The full grounding schema is fixed; an optional `entity_id` is allowed for record identity.

Grounding refers to the latest observed `explanation_finalized` record for this answer/attempt, not merely any record with the same content hash. A new finalization requires new grounding even if its text is unchanged. A genuine retry of an identical record uses the original client request ID; new observations or revised records use fresh IDs.

## Check, recovery and completion

Both record admission and `check --answer-id` run the same grounding validator. Resolve `answer_grounding_*` blockers instead of adding a minimal hash-only record. Such legacy records no longer pass. Confirmations with invalid source hashes must be replaced with canonical locators; do not edit the immutable records or source inventory.

If Main compacts before content finalization, reopen the claim-relevant sources and reground in the new epoch. Initial answer-required reports additionally follow the full-source context recovery in `workflow.md`. A compaction between `check` and `answer --finalize` also rejects the commit. Only send content after the new check is ready and finalization succeeds.

Content completion pins the validated finalization/grounding record IDs and context proof. Later delivery checks retain that historical proof, while each new answer must build its own attempt-specific evidence.

This gate validates the source-evidence chain and coverage for the declared claims. It does not determine semantic entailment or automatically prove that every important claim was declared. Do not equate mechanical success with semantic certainty; Main reading and independent audits still perform that judgment.
