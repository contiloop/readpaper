# ReadPaper

ReadPaper is a local-first Codex Desktop workflow for reading research papers end to end, keeping the active Main agent grounded in the original source, and making every important answer auditable.

이 저장소에 올리는 것은 논문 PDF나 개별 보고서가 아니라 workflow 자체다. 논문 원문, 생성 리포트, run evidence, local hook wiring은 Git에서 제외한다.

## What this workflow does

ReadPaper is built around one principle: the current Codex Main agent must actually receive and inspect the paper, instead of relying on a hidden summary database or a separate model loop.

The workflow combines:

- a Codex skill that defines the reading, audit, report, and Q&A procedure;
- Python tools that fetch/prepare public PDFs, extract page-bounded text, render pages, manage locators, and verify state;
- Codex Desktop hooks that bind user turns, protected tool calls, reviewer agents, compaction, and Stop delivery observations;
- reviewer roles for math/visual checks, claim/experiment checks, and explanation-flow checks;
- tests and host probes for the P0 Desktop acceptance gate.

## Repository layout

```text
.
├── .agents/skills/readpaper/          # ReadPaper skill, references, and Python runtime
├── .codex/agents/                     # reviewer role definitions
├── .codex/config.toml                 # project-local Codex settings
├── .codex/hooks/                      # hook entrypoints
├── .dryforge/spec.md                  # product/specification contract
├── .dryforge/plan.md                  # implementation/acceptance plan
├── scripts/install_readpaper.py       # generates local hook wiring
├── tests/                             # automated unit and host-probe tests
├── PROJECT_GOAL.md                    # product goal
└── IMPLEMENTATION_PLAN.md             # historical design notes
```

Ignored local state:

- `.codex/hooks.json`
- `.dryforge/handoff.md`
- `.dryforge/wiring-manifest.json`
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

## Use

Inside a Codex Desktop task opened in this project, ask to read a paper from a public PDF URL or local PDF path. The project-local `readpaper` skill is the workflow authority.

For a new whole-paper request, the expected flow is:

1. prepare the source and proposed full scope;
2. begin an answer attempt and lock the reading scope;
3. read every required text batch in order;
4. render/open required visual pages;
5. write an understanding note;
6. run independent reviewer audits;
7. ground the answer/report in confirmed locators;
8. finalize the answer before sending it.

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
