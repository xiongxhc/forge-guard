# DeepSeek Harness fail-closed sandbox model — reference

**Date:** 2026-08-15 · **Status:** reference; ideas 2 (skip/fail Feishu
alerts) and 4 (subprocess env scrub) implemented 2026-08-15 for the
diff-only lane — the sandbox items await review-depth item 3 ·
**Source:** [deepseek-ai/deepseek-harness](https://github.com/deepseek-ai/deepseek-harness)
(MIT, developer preview, open-sourced 2026-08-13; file paths below are relative
to that repo and pinned to the preview state — verify before citing later)

## Why this note exists

Roadmap "Review depth" item 3 plans agentic review in a shallow clone: a
`claude -p` process with tools, checked out at the MR head, chasing callers
across files. That process executes model-chosen commands over hostile input
(arbitrary MR content from 82 projects), on the same host that holds forge-guard's
GitLab PATs and Feishu credentials. DeepSeek Harness (dsh) shipped a small,
well-argued isolation layer for exactly this shape of problem. This note
records the parts worth copying and the parts that don't transfer, so the
item-3 design doesn't rediscover them.

## What dsh actually does (corrected from press coverage)

Press framed it as a permission engine; the code says otherwise. There is no
per-command allowlist, no command-text parsing, no prefix matching anywhere.
Mediation is kernel confinement plus one-shot escalation:

- Three modes only — `read-only`, `workspace-write`, `danger-full-access`
  (`packages/sandbox/sandbox/src/index.ts`). Modes govern **file effects
  only**; network, process, and device restrictions are explicitly out of
  scope at every layer.
- Per-platform runner chains (`packages/sandbox/sandbox-local/src/index.ts`):
  Linux `bwrap` → Landlock, macOS Seatbelt (`sandbox-exec -p` with generated
  SBPL), Windows `WRITE_RESTRICTED` restricted token. The command argv is
  wrapped, never parsed: `confine(['bash','-c',cmd])` returns
  `[runner, ...profile, '--', 'bash', '-c', cmd]`.
- Escalation is per-call: the bash tool schema gains a `sandbox_permissions`
  enum plus a required `justification`; a grant applies to that one call and
  is validated at execution time, not schema time
  (`packages/shell/tool-bash/src/index.ts`,
  `packages/sandbox/sandbox/src/escalation.ts`).

## The five transferable ideas

1. **Probe by running, not by detecting.** Backend selection executes a real
   confined `true` under each candidate runner and requires exit 0
   (`sandbox-local/src/index.ts` — `defaultProbeBwrap`, `defaultProbeSeatbelt`).
   Feature detection lies; a functional probe can't. If no runner probes
   usable, `confine()` throws `SANDBOX_UNAVAILABLE` — "silent unconfined
   passthrough is forbidden" is stated in the `SandboxProvider` contract
   itself.

2. **Runner failure outranks denial.** If the sandbox runner fails before the
   command runs (spawn ENOENT/EACCES attributable to the runner, matched
   stderr rules), that is an infrastructure error, not a command result
   (`packages/shell/bash-sandbox/src/index.ts:102` — "Runner failure outranks
   denial because the command did not run"). For forge-guard: a deep review
   whose sandbox setup failed must surface as `review-skipped: sandbox
   unavailable`, never as a clean "no findings" — the two are
   indistinguishable to a reader of the MR note otherwise.

3. **Enforcement is a reported fact, not a boolean.**
   `SandboxEnforcement = 'full' | 'partial'` travels with every result
   (older Landlock ABI and the Windows ACL mechanism are honest about being
   `partial`). If item 3 ever runs degraded (e.g. no bwrap on the runner
   host), the review note should carry that fact.

4. **Scrub the environment before spawning.** `scrubbedParentEnv()`
   (`packages/subprocess/subprocess/src/index.ts:60`) drops credential-shaped
   variable names case-insensitively before any child spawn; explicit `env`
   entries merge back after the scrub as a deliberate opt-in. forge-guard's
   review subprocess needs at most a read-only clone token — it must not
   inherit the sweep PAT, the admin PAT path, or Feishu secrets from the lane
   runner's environment.

5. **Monotonic deny.** dsh layers an extensible allow/deny/ask waterfall
   under a second `ToolGuard` layer that can only deny — "listener ordering
   cannot turn a denial back into permission"
   (`packages/core/tools/src/index.ts:704`). Any future forge-guard hook
   chain (pre-review filters, size caps, branch gates) should keep the same
   property: a later check can tighten, never loosen.

## What does not transfer

- **Network confinement — dsh doesn't have it, and item 3 needs it discussed
  anyway.** A bwrap/Seatbelt-confined child opens sockets freely. The review
  agent must reach the LLM endpoint, so full network isolation is off the
  table; the compensations are the env scrub (idea 4) and a clone token
  scoped to read-only on the one project under review.
- **Per-command policy.** dsh deliberately has none; forge-guard also doesn't
  need one — the review agent's write surface should simply be the disposable
  clone directory (`workspace-write` semantics), which the existing
  `FORGEGUARD_*` config can express as "writes confined to the run dir".
- **The plugin/microkernel apparatus.** Cordis, hot reload, the effect
  calculus — interesting, irrelevant to a Python CI bot.

## Concrete hooks into the roadmap

When item 3 ("agentic review in a shallow clone") gets specced:

- macOS runner host → wrap `claude -p` with `sandbox-exec -p` and a generated
  profile allowing writes only under the clone dir (dsh's SBPL template in
  `sandbox-local/src/profiles.ts` is a working starting point); Linux → bwrap
  with `--ro-bind / /` + `--bind <clone> <clone>` + `--tmpfs /tmp`.
- Probe the wrapper at lane startup (run `true` confined); on probe failure,
  disable the deep tier for that tick and alert once — do not fall back to
  unconfined review.
- Record `enforcement` and `sandbox` outcome in the item-0 instrumentation
  log next to tokens/duration, so degraded runs are auditable.
- Spawn the review process with a scrubbed env: only the clone-scoped
  read-only token and the LLM auth it actually needs.
