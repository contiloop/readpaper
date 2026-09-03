# G0 Desktop host probe

This probe inspects the installed Codex Desktop binary and its generated
app-server schemas without changing `.codex/config.toml` or installing hooks.
It checks whether the documented fields needed for event-specific semantic
keys are available. A host-provided callback, root-execution, context-stream,
aggregate, or executed-effort ID is recorded as a capability boundary rather
than treated as a mandatory release field.

Run it from the repository root:

```sh
uv run python tests/host_probe/probe_current_desktop.py
```

Use `--require-static-ready` to fail when a required semantic-key or actor-
correlation field is missing or has the wrong schema. This command is static-
only and therefore never marks G0 as passed. The separate live probe described
in `.dryforge/plan.md` must validate its own event log before the release gate
can pass; caller-authored JSON is not accepted as live proof. This probe never
records prompt text, assistant-message text, tool payloads, full process
commands, or credentials.

Static readiness is not G0 completion. A reviewed temporary project hook and a
new Desktop session must still demonstrate:

- `Stop` block followed by a nonce-matching continuation and Main tool call;
- identical/concurrent Stop payload replay reusing one logical transaction;
- crash replay with at most one continuation counter and authorized effect;
- one-use nonce claim if the host presents a duplicate continuation prompt;
- no nested continuation for `stop_hook_active=true`;
- no automatic repair after an ordinary user prompt or session restart.

The product does not claim that the host creates exactly one raw callback or
continuation prompt. It requires at most one authorized repair effect.

Install the reviewed live probe only after the static gate is ready:

```sh
uv run python tests/host_probe/manage_live_probe.py install
```

The installer records the protected paths before installation, keeps any
byte-for-byte backups in a private mode-0700 temporary directory, atomically
installs the project hook plus fail-closed probe CLI/parser, and prints the
exact prompt for a new Desktop task. The hook stores raw callback payloads only
in that private temporary directory. Repository evidence contains hashes and
correlation metadata, not prompt, assistant-message, command, or tool-output
text.

Inspect current sanitized state with `status`, write one immutable live
evidence snapshot with `snapshot`, and use `restore` only after the live gate
has a terminal result. Restore first checks that every installed probe file
still has its installation hash; it refuses to overwrite external changes.

Run the complete non-mutating verification set and record exit codes plus
stdout/stderr hashes with:

```sh
uv run python tests/host_probe/run_verification.py
```

Each invocation writes a new immutable `runs/<timestamp>-<pid>/` evidence
directory. It stores command IDs, exit codes, line counts, and hashes only—not
raw argv or stdout/stderr tails.
