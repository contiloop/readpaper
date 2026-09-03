# Independent audit contracts

Reserve each reviewer assignment before spawning it. Pass only the reservation nonce, exact assignment digest, immutable source inventory/path+hash references, locator schema, and required result schema. Do not pass parent conversation history.

## Content audits

- `math_visual`: read all required text in one reviewer epoch; open every equation/figure/table/algorithm candidate page and every extraction-warning page. Check definitions, conditions, axes, legends, rows, columns, units, and appendix links.
- `claim_experiment`: read all required text in one reviewer epoch; open method, experiment, result, limitation, and appendix-connection pages. Check whether claims match design, results, scope, and limitations.

Each role first returns `source_first` without seeing Main's note. The same reviewer then receives the fixed note version for `note_comparison`.

`note_comparison` is a delta review, not a second whole-paper read. Reuse that reviewer's immutable source-first coverage, locator map, and findings; compare every substantive note claim against that map and reopen only cited, conflicting, ambiguous, extraction-warning, or note-revision locations. Repeat the full source pass only after reviewer context loss, invalid source-first coverage, a changed source bundle, or a materially rewritten understanding note. A partial, cancelled, failed, mismatched, or unbound reviewer is not a pass.

Main must reopen confirmed source locations and disposition each finding. Accepted factual/coverage issues require descendant remediation records and reviewer recheck. Rejected findings require reopened source evidence. Only genuine source ambiguity may remain interpretive; coverage gaps and source conflicts remain blocking.

## Flow audit

Use `explanation_flow` only when the fixed condition says it is required. Give it the exact question, requested level, full required text inventory, draft-dependent visuals, fixed note, and fixed draft—never the parent conversation.

Logic errors and required missing connections are blocking. Optional improvements are advisory. Accepted findings require a child draft; blocking findings also require a new audit of that child draft with resolved recheck status. A reviewer replacement may occur once and must reread the full assigned source and revised draft.
