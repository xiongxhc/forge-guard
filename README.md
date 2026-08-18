# forge-guard

Branch-protection enforcement and advisory AI merge-request review for
self-hosted **GitLab CE** — the free tier, where you get no required
approvals, no push rules, and no group-level protected branches.

Without those, nothing stops a direct push or a local merge from landing
straight in a production branch. forge-guard closes that gap with three
lanes, no always-on service — everything is a scheduled tick (launchd on
macOS; cron works the same way) against the GitLab API, plus one central CI
template.

- **Lane 1 — protect-sweep** (hourly). For every project the token reaches,
  ensures each configured branch is protected (no direct push, Developers+
  can merge, no force push), re-protects on drift, and diffs each branch tip
  against the last tick to classify force pushes and merges-without-MR.
  Every protection change, drift correction, and violation is reported to
  Feishu — never auto-reverted; reverting a shared branch is its own
  incident. Also flips on `only_allow_merge_if_pipeline_succeeds`, but only
  once a project has the quality-gate CI include (lane 2) — flipping it
  earlier would block all merges on a project with no pipeline.
- **Lane 2 — quality gate** (mechanical, runs in GitLab CI, not on the
  operator machine). A central CI template (`ci-template/`), hosted in a
  shared `ci-tools` project and added to each project's `.gitlab-ci.yml`,
  hard-fails MR pipelines that add feature commits with no test-file
  changes. Combined with `only_allow_merge_if_pipeline_succeeds`, this is
  the only lane that actually blocks a merge, and it has no dependency on
  the operator machine. Escape hatch: a `Gate-Skip: <reason>` commit
  trailer makes the gate pass but fires a loud Feishu alert — auditable,
  never silent.
- **Lane 3 — AI review** (advisory, 15-minute tick on the operator
  machine). Polls open MRs targeting protected branches, runs the
  [Claude Code](https://claude.com/claude-code) CLI (`claude -p`) over the
  diff and MR description, posts a single upserted review note (edited in
  place on re-review, never spammed), approves clean MRs (a visible signal
  even though CE can't require it), and posts a Feishu summary with the MR
  URL and head-commit URL. A skeptical second pass filters likely false
  positives before anything is posted. Fail-closed visibility: a failed
  review fires a Feishu alert once per MR head, and an oversized MR fires
  one alert per MR (subsequent pushes only refresh the MR note) telling
  the author how to opt into a full review by label, and when GitLab
  truncates a large MR's diff (blank per-file diffs, `changes_count`
  "N+") no review is run at all — a review of a fragment reads like a
  verdict on the MR — only an alert and, if one exists, the stale review
  note rewritten to "not reviewed" — so a missing review is never
  mistaken for a clean one — and the `claude -p`
  subprocess runs with a credential-scrubbed environment (no `FORGEGUARD_*`
  or secret-shaped variables).

Notifications currently target [Feishu/Lark](https://www.larksuite.com/);
the notifier is a single small module (`forgeguard/feishu.py`) if you want
to adapt it to Slack or plain webhooks.

## Requirements

- Python 3.12+
- A GitLab personal access token with `api` scope. An **admin** PAT gives
  instance-wide sweep coverage; a Maintainer token covers only the projects
  it maintains (extra Maintainer tokens can be stacked via
  `FORGEGUARD_GITLAB_EXTRA_TOKENS`).
- A Feishu custom app (app ID + secret) with permission to post to a group
  chat.
- For lane 3: the `claude` CLI, logged in.

## Environment variables

Loaded from `~/.config/forge-guard/forge-guard.env`, sourced by the
launchd/cron wrappers before the CLI runs.

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `FORGEGUARD_GITLAB_URL` | yes | — | GitLab base URL. Also the URL-rebase target — if your instance's configured hostname is unreachable (GitLab returns it in `web_url`), every URL surfaced anywhere is rebased to this host. |
| `FORGEGUARD_GITLAB_TOKEN` | yes | — | Primary PAT (admin for instance-wide coverage; see Requirements). |
| `FORGEGUARD_GITLAB_EXTRA_TOKENS` | no | — | Comma-separated extra Maintainer tokens for groups the primary token can't reach. Sweep and review run once per token; projects are deduped by id, review MR cursors are per-token, and per-MR SHA cursors prevent duplicate reviews across overlapping token views. |
| `FORGEGUARD_FEISHU_APP_ID` | yes | — | Feishu app ID for the tenant-access-token exchange. |
| `FORGEGUARD_FEISHU_APP_SECRET` | yes | — | Feishu app secret. |
| `FORGEGUARD_FEISHU_CHAT_ID` | yes | — | Chat ID of the notification group. |
| `FORGEGUARD_BRANCHES` | no | `main,master,prod,production,develop,dev,uat` | Comma-separated protected branch names. The list is global; only projects actually having a branch are affected. `release/*` is deliberately excluded by default — release flows often push there directly; release→main/prod merges still cross a protected branch. |
| `FORGEGUARD_EXCLUDE` | no | *(empty)* | Comma-separated project-path denylist (archived/sandbox projects, or data repos written by automation that must keep direct push). |
| `FORGEGUARD_USERMAP` | no | `~/.config/forge-guard/usermap.json` | Path to the GitLab-username → Feishu-open_id JSON map used for @-mentions. |
| `FORGEGUARD_STATE` | no | `~/.local/share/forge-guard/state.json` | Path to the sweep/review state file (branch-tip SHAs, event cursors). |
| `FORGEGUARD_DIFF_CAP` | no | `300000` | Max diff size in bytes lane 3 will send to `claude -p`; oversized MRs get a "too large for auto-review" note instead of a truncated, hallucination-prone review. |
| `FORGEGUARD_DIFF_CAP_FULL` | no | `1000000` | Hard cap for MRs carrying the full-review label (below). |
| `FORGEGUARD_FULL_REVIEW_LABEL` | no | `forge-guard:full-review` | GitLab label an author adds to an oversized MR to request a review anyway, up to `FORGEGUARD_DIFF_CAP_FULL`. |
| `REQUESTS_CA_BUNDLE` | no | *(system default)* | Path to a private CA bundle if your GitLab sits behind one. |

## Setup runbook

1. **Create the env file.** `~/.config/forge-guard/forge-guard.env` — set
   at minimum `FORGEGUARD_GITLAB_URL`, `FORGEGUARD_GITLAB_TOKEN`,
   `FORGEGUARD_FEISHU_APP_ID`, `FORGEGUARD_FEISHU_APP_SECRET`,
   `FORGEGUARD_FEISHU_CHAT_ID`, and `REQUESTS_CA_BUNDLE` if needed.
2. **Create the Feishu group and get its chat_id.** Create a notification
   group, add the bot to it, then capture the group's `chat_id` (via the
   Feishu API or bot logs) into `FORGEGUARD_FEISHU_CHAT_ID`.
3. **Build the usermap** — a flat `{"gitlab_username": "feishu_open_id"}`
   JSON object at the path `FORGEGUARD_USERMAP` points to. Lookups fall
   back to a normalized match (case, spaces, dots, hyphens ignored), so a
   git author name like `lin tianhua` still resolves the `lintianhua`
   entry. Members with no entry still get alerted, just without the
   `@`-mention; forge-guard flags each missing mapping once.
4. **Install dependencies.** The scheduled ticks run the checkout's
   virtualenv:
   ```sh
   python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
   ```
5. **Install the plists** (macOS; on Linux, equivalent cron entries for
   `forgeguard.cli sweep` hourly and `forgeguard.cli review` every 15
   minutes). Copy each `.example` file from `launchd/` to
   `~/Library/LaunchAgents/`, stripping the `.example` suffix, substituting
   `__REPO__` for the absolute path to this checkout and `__HOME__` for
   your home directory:
   ```sh
   for f in launchd/*.plist.example; do
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

The lanes go live in this order, not all at once:

1. **Protect-sweep**, for immediate protection on whatever the token
   reaches.
2. **Feishu group + alert wiring.**
3. **AI review lane.**
4. **Quality-gate CI include — last**, announced to the team first (it
   touches every repo and changes merge behavior). Host the
   `ci-template/` contents in a shared `ci-tools` project, add each
   consumer project to that project's job-token allowlist, then add the
   include to each consumer's `.gitlab-ci.yml`.

## Limitations

- **Hourly detection latency.** The protect-sweep only runs once an hour,
  so a force push or merge-without-MR can sit undetected (though not
  un-preventable — protection itself is enforced continuously by GitLab)
  for up to an hour before it's classified and alerted.
- **GitLab CE means approvals can't be required.** The AI review lane
  approves clean MRs as a visible signal, but CE has no required-approval
  rule to hook it to — it's advisory, never a merge gate. Only the
  mechanical quality gate (lane 2) actually blocks a merge.
- **Operator machine offline pauses lanes 1 and 3, not enforcement.**
  Protect-sweep and AI review both run on the operator machine; when it
  can't reach GitLab, both lanes simply skip their tick and catch up from
  cursors next time. Branch protection already applied and the CI quality
  gate keep enforcing on the GitLab side regardless — nothing new gets
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

## Development

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest
```

## License

Apache-2.0
