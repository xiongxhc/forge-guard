# forge-guard — MR enforcement, quality gate, and AI review for the org GitLab

**Date:** 2026-08-07
**Status:** approved design
**Motivation:** a reckless direct merge to `develop` on internal-tool cost ~thousands of dollars. Nothing
today prevents direct pushes/merges to long-lived branches, requires tests with
feature changes, or reviews MRs. forge-guard closes all three gaps on GitLab **CE**
(no Premium features available: no required approvals, no push rules, no
group-level protected branches).

## Decisions (from brainstorming, 2026-08-07)

- GitLab tier: **CE / free** — enforcement = protected branches + "merge only if
  pipeline succeeds" + external commit status. Approvals exist but cannot be required.
- Gate strength: **mechanical checks hard-block (CI-enforced); AI review advisory**.
  AI review runs on the operator Mac via `claude -p` (subscription, never API keys)
  and must never be a merge dependency, because the Mac is not always on the office
  network.
- Scope: **every project on the instance**, new projects auto-covered by the sweep.
- Notifications: Feishu group **"Gitlab Review Notification"**, @-mentioning the MR
  author (GitLab→Feishu identity via the teammem roster), **always including the MR
  URL and head-commit URL**.
- Protected branch names (config, default): `main, master, prod, production,
  develop, uat`. `release/*` deliberately unprotected — the release flow pushes
  there directly; release→main/prod merges still cross a protected branch and
  therefore still require an MR.

## Architecture — three lanes, one agent

Monorepo `forge-guard/` (Tier 2, not-yet-extracted agent). Deterministic Python +
thin `claude -p` seam, launchd ticks, fleet config at
`~/.config/forge-guard/forge-guard.env`. No always-on service; polling only.

### Lane 1 — protect-sweep (hourly tick; pure GitLab settings)

For every project the token reaches:

1. For each configured branch name that exists in the project: ensure protected
   with `push_access_level=0` (No one), `merge_access_level=30` (Developers+),
   `allow_force_push=false`. Re-protect on drift.
2. Ensure project setting `only_allow_merge_if_pipeline_succeeds=true` **only
   after** the project has the quality-gate CI include (lane 2), never before —
   flipping it on a project with no pipeline would block all merges.
3. Detect violations since last tick. The sweep stores each target branch's tip
   SHA per project; on the next tick it classifies every tip movement:
   - **Force push:** stored tip is no longer an ancestor of the new tip
     (non-fast-forward move). Protection blocks these going forward, but Owners
     and admins can override or briefly unprotect — every occurrence is caught
     and alerted regardless of who did it.
   - **Merge without MR:** for each new commit landed on a target branch, query
     the commit's associated merge requests
     (`/repository/commits/:sha/merge_requests`); commits with **no merged MR**
     targeting that branch = a direct push or local merge — alerted with
     pusher, branch, and rebased commit URL, @-mentioning the pusher in Feishu.
   - Both are **reported, never auto-reverted** — reverting a shared branch is
     its own incident.

State: per-project branch-tip SHAs + last-seen event cursor in a local state
file. Violations and sweep errors → Feishu alert; routine protection
changes/drift corrections are counted in the summary but NOT alerted
(operator preference, 2026-08-10 — keep the group review-focused).
Idempotent: a second run makes zero changes and sends zero alerts.

### Lane 2 — mechanical quality gate (hard; runs in GitLab CI, not on the Mac)

Central repo `forge/quality-gate` holds one CI template. The sweep injects
`include: {project: forge/quality-gate, file: gate.yml}` into each project — via
MR when `.gitlab-ci.yml` exists, via direct commit of a minimal one when absent.
The gate job runs on MR pipelines and fails when:

- the MR contains feature commits (`feat:` conventional commits, or diff adds
  non-test source files beyond a small threshold) **and zero test-file changes**
  (per-language test-path conventions; align with acme-conventions at
  implementation time);
- lint/build for the detected language fails (best-effort, per-language matrix
  kept deliberately small at first).

Failure message states exactly what is missing. Combined with
`only_allow_merge_if_pipeline_succeeds`, this hard-blocks the merge with no
dependency on the operator Mac. **Escape hatch:** commit trailer
`Gate-Skip: <reason>` makes the gate pass but triggers a loud Feishu alert —
auditable, not silent.

### Lane 3 — AI review (advisory; 15-min tick on the operator Mac)

Poll open MRs (new or with new head SHA since cursor) targeting protected
branches, instance-wide. For each: `claude -p` on diff + MR title/description →
structured verdict (issues found, test-coverage opinion, quality notes) →

- post as a single MR discussion (edit/replace own previous note on re-review;
  never spam);
- **approve** the MR when clean (visible signal; CE cannot require it);
- Feishu post to "Gitlab Review Notification": @author, verdict summary, MR URL,
  head commit URL.

Off-network tick exits silently; next tick catches up from cursors. Diffs are
size-capped (config, default ~300 KB) — oversized MRs get a "too large for
auto-review" note instead of a truncated hallucination-prone review.

## Feishu notification contract

- Group: "Gitlab Review Notification" (created once, bot added; chat_id in env).
- @mention: GitLab username → Feishu open_id via the teammem roster mapping;
  unmapped author → post without @ and flag the missing mapping once per user.
- **URL rebasing (hard rule):** GitLab returns `web_url` on its `external_url`
  (the dead `gitlab.internal.example`). Every URL surfaced anywhere — Feishu, MR
  comments — is rebased: take the forge's *path*, force scheme+host to
  `https://gitlab.example.com/`. Same lesson as dev-agent M7.
- Event → message matrix: review verdict — "✅ approved: …" or "📝 review left
  for {author}: … — N issue(s)" (with URLs), gate failure, Gate-Skip used,
  **force push detected**, **merge without MR detected** (both @pusher with
  commit URL), sweep error. Routine protection changes are deliberately silent.

## Config (`~/.config/forge-guard/forge-guard.env`)

- `FORGEGUARD_GITLAB_URL=https://gitlab.example.com` — also the rebase target.
- `FORGEGUARD_GITLAB_TOKEN` — **admin PAT** (the existing Maintainer token cannot
  set protection on projects it does not maintain; instance-wide scope needs admin).
- `FORGEGUARD_BRANCHES=main,master,prod,production,develop,uat`
- `REQUESTS_CA_BUNDLE` — private CA (existing bundle).
- `FORGEGUARD_FEISHU_APP_ID/SECRET`, `FORGEGUARD_FEISHU_CHAT_ID`.
- `FORGEGUARD_EXCLUDE` — project-path denylist (archived/sandbox projects).

## Failure modes

- **Mac offline:** lanes 1+3 skip; protection and CI gate keep enforcing. Nothing
  new gets protected until next successful sweep — acceptable (hour-scale gap).
- **No shared runners** (unverified — network was down during design): gate
  pipelines would sit pending and block all merges. Therefore lane 2 rollout is
  gated on runner verification; interim option is registering one Docker runner.
- **Admin PAT unavailable:** sweep degrades to projects the Maintainer token can
  reach and Feishu-alerts the coverage gap.
- **claude CLI failure:** review skipped with cursor not advanced; retried next tick.

## Rollout order

1. Protect-sweep on the existing Maintainer token's reach (immediate protection),
   admin PAT requested in parallel.
2. Feishu group + alert wiring.
3. AI review lane.
4. Quality-gate CI include — **last**, announced to the team first (it touches
   every repo and changes merge behavior), after runner availability is verified.

## To verify when the office network is reachable

- Confirm CE via `/api/v4/license` (design assumes CE; Premium only makes things
  easier — native approvals could replace the status dance later).
- Shared runner availability (`/api/v4/runners/all`).
- Whether an admin PAT can be minted (token owner is currently Maintainer-level).
- The internal-tool incident specifics, to add a regression-shaped check to the gate
  if applicable.
