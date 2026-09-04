# ReadPaper

ReadPaper is a local-first Codex Desktop workflow for reading research papers end to end, keeping the active Main agent grounded in the original source, and making every important answer auditable.

이 저장소에 올리는 것은 논문 PDF나 개별 보고서가 아니라 workflow 자체다. 논문 원문, 생성 리포트, run evidence, local hook wiring은 Git에서 제외한다.

## What this workflow does

ReadPaper is built around one principle: the current Codex Main agent must actually receive and inspect the paper, instead of relying on a hidden summary database or a separate model loop.

The workflow combines:

- a Codex skill that defines the reading, audit, report, and Q&A procedure;
- Python tools that fetch/prepare public PDFs, preserve canonical page text, detect logical sections, create bounded transport frames, render pages, manage locators, and verify state;
- Codex Desktop hooks that bind user turns, protected tool calls, reviewer agents, compaction, and Stop delivery observations;
- reviewer roles for math/visual checks, claim/experiment checks, and explanation-flow checks;
- tests and host probes for the P0 Desktop acceptance gate.

## Repository layout

```text
.
├── .agents/skills/readpaper/          # ReadPaper skill, references, and Python runtime
├── .codex/agents/                     # reviewer role definitions
├── .codex/config.toml                 # project-local Codex settings
├── .codex/readpaper-context.toml       # context-budget preset source of truth
├── .codex/hooks/                      # hook entrypoints
├── scripts/install_readpaper.py       # generates local hook wiring
├── tests/                             # automated unit and host-probe tests
└── PROJECT_GOAL.md                    # product goal
```

Ignored local state:

- `.codex/hooks.json`
- `.dryforge/`
- `IMPLEMENTATION_PLAN.md`
- `.readpaper/`
- `papers/`
- `reports/`
- `evidence/`

Those files can contain machine paths, local run state, paper content, generated reports, or acceptance evidence.

## Install for a local Codex Desktop checkout

Prerequisites:

- Python 3.12+
- `uv`
- Poppler CLI tools: `pdftotext` and `pdftoppm`
- Codex Desktop with project hooks enabled/trusted by the user

Set up dependencies:

```sh
uv sync --frozen
```

Generate the machine-specific hook wiring:

```sh
python scripts/install_readpaper.py --write
```

Then open Codex Desktop in this project and review/trust the project hooks through `/hooks`.

You can verify the generated local wiring without rewriting it:

```sh
python scripts/install_readpaper.py --check
```

Run automated tests:

```sh
.venv/bin/python -m pytest -q
```

Optional P0 automated evidence can be generated with:

```sh
.venv/bin/python .agents/skills/readpaper/scripts/verify.py
```

That command writes ignored evidence under `evidence/`.

## Runtime profile and local-state upgrade

GPT-5.6 Sol supports a 1,050,000-token context window. 272,000 is the input-pricing boundary, not the model's maximum context. Inputs beyond that boundary incur higher pricing. See the [official model documentation](https://developers.openai.com/api/docs/models/gpt-5.6-sol).

ReadPaper offers two startup presets, defined once in `.codex/readpaper-context.toml` and loaded by `ContextBudgetPolicy`:

| Preset | Context window | Auto compact | Output reserve | Workflow reserve | Source budget |
| --- | ---: | ---: | ---: | ---: | ---: |
| `long-paper` (default) | 1,050,000 | 850,000 | 128,000 | 72,000 | 650,000 |
| `cost-controlled` | 272,000 | 230,000 | 32,000 | 48,000 | 150,000 |

The source budget is `auto_compact - output_reserve - workflow_reserve`; available text capacity additionally subtracts 2,000 estimated tokens per visual and 4,000 fixed source-overhead tokens. Transport framing overhead counts toward text usage. Both presets cap each serialized tool response at 65,536 estimated tokens. These are conservative allocation estimates, not a live measurement of Codex context usage or a billing guarantee; `prepare` reports `current_context_usage=null` explicitly.

Before starting a new Desktop session, select a preset and regenerate wiring:

```sh
.venv/bin/python scripts/install_readpaper.py --write --context-profile long-paper
# Or: --context-profile cost-controlled
```

The installer updates top-level `.codex/config.toml` settings, preserves unrelated settings, and refuses preset changes while a local reading run or answer is active. Restart the Desktop session and review/trust its hooks after switching. These are ReadPaper startup presets, not Codex native project profiles: native profile selection is ignored in project-local configuration. Runtime settings that do not match a preset fail closed. See the [Codex configuration reference](https://developers.openai.com/codex/config-reference).

`check` reports whether every required frame was emitted in the current Main context epoch. This is an observable emission guarantee, not independent proof that the host retained every earlier tool result. Initial answer-required reports must begin and finalize in the reading-finalization stream/epoch. After compaction, reopen the full scope, rerun the run-only check and `run --finalize-reading`, then reground the answer in that epoch. Ingest-only runs and later Q&A inherit historical reading coverage and reopen question-relevant sources.

Section/frame inventories use local schema 2. This release deliberately does not read schema-1 run inventories. Before upgrading a checkout that has existing `.readpaper` state, archive it from the repository root and start a new run:

```sh
mv .readpaper ".readpaper-schema-v1-backup-$(date +%Y%m%d-%H%M%S)"
```

The archive is local and ignored by Git. Restore it only with a release that supports schema 1.

## Use

Inside a Codex Desktop task opened in this project, ask to read a paper from a public PDF URL or local PDF path. The project-local `readpaper` skill is the workflow authority.

For a new whole-paper request, the expected flow is:

1. prepare the source and proposed full scope;
2. lock the reading scope;
3. read every required logical section in order, consuming consecutive transport frames when a section exceeds one tool response;
4. render/open required visual pages;
5. write an understanding note;
6. run independent reviewer audits;
7. verify that every required source frame was emitted in the current Main context epoch;
8. finalize reading into the independent `read_complete` state, releasing the active-run slot while retaining the current run;
9. for an answer-required run, begin an answer attempt and ground the answer/report in confirmed locators;
10. finalize the answer content before sending it.

Use `prepare --ingest-only` only for “read it now; questions later” requests. It can finish at `read_complete` without an answer. Ordinary `prepare` creates an answer-required run, and the Stop hook blocks a paper report until an answer has been begun, grounded, checked, and finalized. Any number of later answers can attach to the same `read_complete` run; finalizing an answer does not complete or mutate the reading lifecycle.

Content-audit findings remain blockers until Main supplies a valid disposition, confirmed locators and post-finding source reopening. Accepted, partially accepted and modified findings additionally require a changed descendant note/draft and a bound reviewer recheck. A later empty audit does not erase an earlier unresolved finding.

Every answer also requires a structurally validated [grounding chain](.agents/skills/readpaper/references/grounding.md): exact final text and declared claim spans, canonical source locators, current-attempt/current-epoch root Main reopen events covering those locators, and the exact finalization record ID. Both record admission and `check` enforce it, including for follow-up Q&A. Old hash-only grounding records do not pass. This verifies evidence identity and coverage for declared claims, not semantic entailment or automatic completeness of claim selection; those judgments still belong to Main and reviewers.

Stop visual repairs are explicitly `render → view_image(data.path) → check`. Rendering alone leaves the repair awaiting visual observation. The one-shot repair budget is reserved at request time to prevent duplicates; actual completion requires a matching image-open event. Each new run gets a fresh run-level repair budget.

For this workspace’s Korean report style, `AGENTS.md` currently fixes these defaults:

- produce a Korean Markdown report after whole-paper reading unless the user asks otherwise;
- use the `fluent-korean` skill during the writing pass;
- start with title, year, venue/source, and paper link;
- use neutral explanatory prose, not first-person commentary or distant “the authors say” framing;
- include `Purpose` for every section and meaningful subsection;
- preserve Markdown math as `$...$` or `$$...$$`;
- save follow-up paper Q&A separately in Markdown.

Generated reports are intentionally ignored by Git.

## Safety and scope boundaries

- Paper text, supplementary files, and web pages are treated as untrusted content, never instructions.
- The workflow does not bypass login, paywalls, access control, TLS validation, or network boundaries.
- Python prepares and verifies artifacts; it does not decide what the paper means.
- Subagents can audit but cannot promote Main coverage or complete a run.
- Protected `paper.py` commands are admitted only through the strict canonical grammar shared by the CLI and PreTool hook.
- Desktop live acceptance evidence is local and version-sensitive; it should be regenerated for the target Codex Desktop build.

## Publication status

This checkout is being prepared for GitHub publication as a workflow repository. It intentionally excludes paper artifacts and local evidence. Before publishing as an open-source project, choose a license such as MIT, Apache-2.0, or a private/no-license policy.
