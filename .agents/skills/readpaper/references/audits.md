# Independent audit contracts

Reserve each reviewer assignment before spawning it. Pass only the reservation nonce, exact assignment digest, immutable source inventory/path+hash references, locator schema, and required result schema. Do not pass parent conversation history.

## Content audits

- `math_visual`: read all required section frames in one reviewer epoch; open every equation/figure/table/algorithm candidate page and every extraction-warning page. Check definitions, conditions, axes, legends, rows, columns, units, and appendix links.
- `claim_experiment`: read all required text in one reviewer epoch; open method, experiment, result, limitation, and appendix-connection pages. Check whether claims match design, results, scope, and limitations.

Each role first returns `source_first` without seeing Main's note. The same reviewer then receives the fixed note version for `note_comparison`.

`note_comparison` is a delta review, not a second whole-paper read. Reuse that reviewer's immutable source-first coverage, locator map, and findings; compare every substantive note claim against that map and reopen only cited, conflicting, ambiguous, extraction-warning, or note-revision locations. Repeat the full source pass only after reviewer context loss, invalid source-first coverage, a changed source bundle, or a materially rewritten understanding note. A partial, cancelled, failed, mismatched, or unbound reviewer is not a pass.

Main must reopen confirmed source locations and disposition each finding. Accepted factual/coverage issues require descendant remediation records and reviewer recheck. Rejected findings require reopened source evidence. Only genuine source ambiguity may remain interpretive; coverage gaps and source conflicts remain blocking.

### Enforced content-finding evidence

Every recorded content finding remains active until resolved; replacing an audit with an empty result does not erase earlier findings. `check` emits `audit_finding_unresolved:<finding_id>` and a reason in `pending_finding_reasons` when evidence is missing or mismatched.

Main must record `locator_confirmation` with `locator_id` and the full `locator` object from the locator union. The canonical locator ID and bundle must match. Before disposition, reopen those locations using observed `read` or `view_image` calls after the finding's audit-result event. A text frame must cover the confirmed page/span; object/image locators require visual opening. Reopen events must belong to root Main in the disposition's stream/epoch, not to a reviewer or an older context.

A `finding_disposition` payload includes:

```json
{
  "finding_id": "<cf_id>",
  "disposition": "accepted",
  "rationale": "<source-grounded explanation>",
  "confirmed_locator_ids": ["<loc_id>"],
  "source_reopen_event_ids": ["<observed event_id>"],
  "remediation_record_ids": ["<child note or draft record_id>"]
}
```

All finding locators must be included. `rejected` requires confirmed locators, fresh reopening and rationale but no remediation. `interpretive` is allowed only for an `interpretive_ambiguity` finding, with the same source evidence. Other unresolved outcomes remain blockers.

For `accepted`, `partially_accepted` and `modified`, create a changed descendant `understanding_note` or `explanation_draft` after reopening and before disposition, in the same Main epoch. Preserve the entity, set `version_id` and `parent_version_id`, and supply the changed `content_sha256`. The parent must have an observed Main version event; a note correction must descend from the audited `note_version_id` when one is specified. Do not invent a new unrelated note or reuse an unchanged hash as remediation.

Reserve a new attempt for the same reviewer role to inspect that child. Its bound latest `audit_result` must include the finding in `recheck_finding_ids` and a matching `recheck_results` entry with `finding_id`, `status: "resolved"`, and `remediation_record_id`. The result must occur after remediation. Merely asserting `resolved` without the reviewer reservation/binding and exact child reference is insufficient.

## Flow audit

Use `explanation_flow` only when the fixed condition says it is required. Give it the exact question, requested level, full required text inventory, draft-dependent visuals, fixed note, and fixed draft—never the parent conversation.

Logic errors and required missing connections are blocking. Optional improvements are advisory. Accepted findings require a child draft; blocking findings also require a new audit of that child draft with resolved recheck status. A reviewer replacement may occur once and must reread the full assigned source and revised draft.
