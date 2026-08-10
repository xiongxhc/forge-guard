# forge-guard

> **Tier 2 · deployment overlay + agent source** — enforces MR-only merges,
> detects force pushes and merges-without-MR, and posts advisory AI reviews
> for the org GitLab. Estate map:
> [../docs/agent-estate-architecture.md](../docs/agent-estate-architecture.md).

## What it is

The org's GitLab is CE (free tier): no required approvals, no push rules, no
group-level protected branches. Nothing stopped a direct push or a local
merge to `develop` from landing straight in production — a reckless direct
merge to `develop` on internal-tool cost roughly thousands of dollars before forge-guard existed.
forge-guard closes that gap with three lanes, one agent, no always-on
service — everything is a launchd tick against the GitLab API.

- **Lane 1 — protect-sweep** (hourly). For every project the token reaches,
  ensures each configured branch is protected (no direct push, Developers+
  can merge, no force push), re-protects on drift, and diffs each branch tip
  against the last tick to classify force pushes and merges-without-MR.
  Every protection change, drift correction, and violation is reported to
  Feishu — never auto-reverted; reverting a shared branch is its own
  incident. Also flips on `only_allow_merge_if_pipeline_succeeds`, but only
  once a project has the quality-gate CI include (lane 2) — flipping it
  earlier would block all merges on a project with no pipeline.
- **Lane 2 — quality gate** (mechanical, runs in GitLab CI, not on the Mac).
  A central CI template living in `acme-group/common/ci-tools`
  (`ci/quality-gate.gitlab-ci.yml`, following that repo's central-include
  model), added to each project's `.gitlab-ci.yml`, hard-fails MR pipelines
  that add feature commits with no test-file changes. Combined with
  `only_allow_merge_if_pipeline_succeeds`, this is the only lane that
  actually blocks a merge, and it has no dependency on the operator Mac.
  Escape hatch: a `Gate-Skip: <reason>` commit trailer makes the gate pass
  but fires a loud Feishu alert — auditable, never silent.
- **Lane 3 — AI review** (advisory, 15-minute tick on the operator Mac).
  Polls open MRs targeting protected branches, runs `claude -p` (subscription,
  never API keys) over the diff and MR description, posts a single upserted
  review note (edited in place on re-review, never spammed), approves clean
  MRs (a visible signal even though CE can't require it), and posts a Feishu
  summary with the MR URL and head-commit URL.

## Environment variables

Loaded from `~/.config/forge-guard/forge-guard.env`, sourced by both
launchd wrappers before the CLI runs.

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `FORGEGUARD_GITLAB_URL` | yes | — | Forge base URL (`https://gitlab.example.com`). Also the URL-rebase target — GitLab returns the dead `gitlab.internal.example` in `web_url`; every URL surfaced anywhere is rebased to this host. |
| `FORGEGUARD_GITLAB_TOKEN` | yes | — | Admin PAT. The existing Maintainer token cannot set protection on projects it doesn't maintain; instance-wide sweep coverage needs admin. |
| `FORGEGUARD_FEISHU_APP_ID` | yes | — | Feishu app ID for the tenant-access-token exchange. |
| `FORGEGUARD_FEISHU_APP_SECRET` | yes | — | Feishu app secret. |
| `FORGEGUARD_FEISHU_CHAT_ID` | yes | — | Chat ID of the "Gitlab Review Notification" group. |
| `FORGEGUARD_BRANCHES` | no | `main,master,prod,production,develop,uat` | Comma-separated protected branch names. `release/*` is deliberately excluded — the release flow pushes there directly; release→main/prod merges still cross a protected branch. |
| `FORGEGUARD_EXCLUDE` | no | *(empty)* | Comma-separated project-path denylist (archived/sandbox projects the sweep should skip). |
| `FORGEGUARD_USERMAP` | no | `~/.config/forge-guard/forge-guard-usermap.json` | Path to the GitLab-username → Feishu-open_id JSON map used for @-mentions. |
| `FORGEGUARD_STATE` | no | `~/.local/share/forge-guard/state.json` | Path to the sweep/review state file (branch-tip SHAs, event cursors). |
| `FORGEGUARD_DIFF_CAP` | no | `300000` | Max diff size in bytes lane 3 will send to `claude -p`; oversized MRs get a "too large for auto-review" note instead of a truncated, hallucination-prone review. |
| `REQUESTS_CA_BUNDLE` | no | *(system default)* | Path to the private CA bundle needed to reach the forge over TLS. |

## Setup runbook

1. **Create the env file.** `~/.config/forge-guard/forge-guard.env` —
   set at minimum `FORGEGUARD_GITLAB_URL`, `FORGEGUARD_GITLAB_TOKEN` (admin
   PAT — mint one on the forge; the current Maintainer token can't protect
   foreign projects), `FORGEGUARD_FEISHU_APP_ID`, `FORGEGUARD_FEISHU_APP_SECRET`,
   `FORGEGUARD_FEISHU_CHAT_ID`, and `REQUESTS_CA_BUNDLE` if the forge sits
   behind the private CA.
2. **Create the Feishu group and get its chat_id.** Create a group named
   "Gitlab Review Notification", add the bot to it, then capture the
   group's `chat_id` (via the Feishu API or bot logs) into
   `FORGEGUARD_FEISHU_CHAT_ID`.
3. **Build the usermap from the teammem roster.** The roster
   (`team-memory-agent/config/roster.yaml`) already maps each member's
   `gitlab:` username(s) to their `feishu:` open_id(s). Flatten that into
   `forge-guard-usermap.json` — a flat `{"gitlab_username": "feishu_open_id"}`
   object — at the path `FORGEGUARD_USERMAP` points to (default
   `~/.config/forge-guard/forge-guard-usermap.json`). Members with no
   entry still get alerted, just without the `@`-mention; forge-guard flags
   the missing mapping once per unmapped user.
4. **Install dependencies.** The launchd ticks run the checkout's virtualenv:
   ```sh
   cd forge-guard && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
   ```
5. **Install the plists.** Copy each `.example` file from `launchd/` to
   `~/Library/LaunchAgents/`, stripping the `.example` suffix, substituting
   `__REPO__` for the absolute path to this checkout and `__HOME__` for
   your home directory:
   ```sh
   for f in forge-guard/launchd/*.plist.example; do
     dest=~/Library/LaunchAgents/$(basename "${f%.example}")
     sed "s#__REPO__#$(pwd)#g; s#__HOME__#$HOME#g" "$f" > "$dest"
   done
   mkdir -p ~/Library/Logs/ForgeGuard
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.forgeguard.sweep.plist
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.forgeguard.review.plist
   ```
6. **Watch the first sweep** in `~/Library/Logs/ForgeGuard/sweep.log` and
   confirm protection/violation messages land in the Feishu group.

## Rollout order

The four lanes/steps go live in this order, not all at once:

1. **Protect-sweep** on the existing Maintainer token's reach, for immediate
   protection — admin PAT requested in parallel for full instance coverage.
2. **Feishu group + alert wiring.**
3. **AI review lane.**
4. **Quality-gate CI include — last**, announced to the team first (it
   touches every repo and changes merge behavior). Runner availability
   verified and gate piloted live 2026-08-10 (see Limitations); the
   fleet-wide include rollout (`inject-gate --apply`) is the remaining
   step.

## Limitations

- **Hourly detection latency.** The protect-sweep only runs once an hour, so
  a force push or merge-without-MR can sit undetected (though not
  un-preventable — protection itself is enforced continuously by GitLab) for
  up to an hour before it's classified and alerted.
- **GitLab CE means approvals can't be required.** The AI review lane
  approves clean MRs as a visible signal, but CE has no required-approval
  rule to hook it to — it's advisory, never a merge gate. Only the
  mechanical quality gate (lane 2) actually blocks a merge.
- **Quality-gate is deployed and piloted, not yet fleet-wide.** The gate
  lives in `acme-group/common/ci-tools` (`quality-gate/` tool +
  `ci/quality-gate.gitlab-ci.yml` consumer template; `ci-template/` here is
  a synced reference copy). Piloted live on acme-sdk 2026-08-10 on the
  group k8s runner: a `feat:` MR with no tests failed the gate, adding a
  test file made it pass. Per-consumer requirements: the project must be on
  ci-tools' job-token allowlist (`projects/75/job_token_scope/allowlist`),
  and repos with a custom `stages:` list override the job's `stage:`
  (`.pre` is deliberately not used — GitLab never creates a pipeline that
  holds only `.pre` jobs, and in most repos the gate is the only MR job).
  Remaining: implement `inject-gate --apply` for the fleet rollout, the
  Gate-Skip Feishu audit alert, and the per-project
  `only_allow_merge_if_pipeline_succeeds` flip — announced to the team
  first.
- **Mac offline pauses lanes 1 and 3, not enforcement.** Protect-sweep and
  AI review both run on the operator Mac; when it's off the office network,
  both lanes simply skip their tick and catch up from cursors next time.
  Branch protection (already applied) and the CI quality gate (once
  deployed) keep enforcing on the GitLab side regardless — nothing new gets
  protected until the next successful sweep, but existing protection never
  lapses.
- **Violations are reported, never reverted.** A force push or a merge
  without an MR gets an alert with the pusher, branch, and commit URL — it
  is never rolled back automatically. Reverting a shared branch is its own
  incident with its own blast radius.
- **`Gate-Skip: <reason>` is the audited escape hatch.** It's the only way
  to land an MR that the quality gate would otherwise block, and using it
  fires a loud Feishu alert every time — it's meant to be visible, not
  silent, and every use should be reviewable after the fact.
