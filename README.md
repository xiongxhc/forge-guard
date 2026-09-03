# forge-guard

Branch-protection enforcement and advisory AI merge-request review for
self-hosted **GitLab CE** — the free tier, where you get no required
approvals, no push rules, and no group-level protected branches.

Without those, nothing stops a direct push or a local merge from landing
straight in a production branch. forge-guard closes that gap with three
lanes, no always-on service — everything is a scheduled tick (systemd/launchd on
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
- **Lane 3 — AI review** (advisory, periodic tick — 5 minutes as
  deployed — on the operator
  machine). Polls open MRs targeting protected branches, runs a selectable
  CLI reviewer (`claude -p` by default, or `codex exec`) over the
  diff and MR description — in the default `files` mode also over the full
  content of each changed file at the MR head plus the project's own review
  rules (`.forgeguard.md`, falling back to `CLAUDE.md`, fetched from the
  repo at that SHA), so the reviewer sees whole files and per-project
  conventions, not just hunks — posts a single upserted review note (edited
  in place on re-review, never spammed), approves clean MRs (a visible signal
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
  mistaken for a clean one. MRs merged before the tick could review
  their final head are flagged to Feishu as merged-without-review
  (CE cannot require approvals, so fast merges skip the advisory
  review; this makes the skip visible). The model subprocess runs with a
  credential-scrubbed environment (no `FORGEGUARD_*` or secret-shaped
  variables). Codex also runs without shell, multi-agent, web-search, or local
  image tools, so untrusted MR text cannot turn the reviewer into a host-file
  reader.

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
- For lane 3: the selected reviewer CLI logged in (`claude` by default, or
  `codex` when `FORGEGUARD_REVIEW_PROVIDER=codex`).

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
| `FORGEGUARD_BRANCHES` | no | `main,master,prod,production,develop,dev,uat` | Comma-separated protected branch names. An entry containing `*`/`?` is a tail-anchored fnmatch glob (GitLab's wildcard convention), e.g. `*/uat` covers `adaa/uat`; plain entries always match exactly, so `dev` never catches `dev-tooling`. The list is global; only projects actually having a branch are affected. `release/*` is deliberately excluded by default — release flows often push there directly; release→main/prod merges still cross a protected branch. |
| `FORGEGUARD_REVIEW_BRANCHES` | no | *(empty)* | Extra branch names/globs the **review lane only** covers (e.g. `release/*,uat-*`) — MRs targeting them get AI reviews, but the branches are not protected and moves are not classified. |
| `FORGEGUARD_EXCLUDE` | no | *(empty)* | Comma-separated project-path denylist (archived/sandbox projects, or data repos written by automation that must keep direct push). |
| `FORGEGUARD_USERMAP` | no | `~/.config/forge-guard/usermap.json` | Path to the GitLab-username → Feishu-open_id JSON map used for @-mentions. |
| `FORGEGUARD_STATE` | no | `~/.local/share/forge-guard/state.json` | Path to the sweep/review state file (branch-tip SHAs, event cursors). |
| `FORGEGUARD_REVIEW_PROVIDER` | no | `claude` | CLI used for MR reviews: `claude` or `codex`. Codex is pinned to `gpt-5.6-sol`; completed Feishu review cards show the actual reviewer as `review by <model>`. This does not change weekly brief generation, which still uses Claude. |
| `FORGEGUARD_CODEX_BIN` | no | discovered from `PATH` | Optional absolute path to the Codex CLI. Codex reviews run at high reasoning with ephemeral sessions, read-only sandboxing, ignored user config/rules, disabled local-read tools, and a structured verdict schema. |
| `FORGEGUARD_DIFF_CAP` | no | `300000` | Max diff size in bytes lane 3 will send to the reviewer; oversized MRs get a "too large for auto-review" note instead of a truncated, hallucination-prone review. |
| `FORGEGUARD_DIFF_CAP_FULL` | no | `1000000` | Hard cap for MRs carrying the full-review label (below). |
| `FORGEGUARD_FULL_REVIEW_LABEL` | no | `forge-guard:full-review` | GitLab label an author adds to an oversized MR to request a review anyway, up to `FORGEGUARD_DIFF_CAP_FULL`. |
| `FORGEGUARD_REVIEW_MODE` | no | `files` | Review context mode. `files`: the prompt carries, besides the diff, the full content of every changed file at the MR head SHA and the project's review-rules file (`.forgeguard.md` at the repo root, else `CLAUDE.md`; first 16 KB). `diff`: diff and MR description only (the pre-mode behavior). Context is advisory — any context fetch failure degrades that review to less context, never to no review. |
| `FORGEGUARD_CONTEXT_CAP` | no | `600000` | Byte budget in `files` mode for fetched file contents. The diff spends the same budget, so a large (labelled) diff leaves less room for file content and the total prompt stays bounded. Files over 100 KB each, binary files, and files past the budget are listed as omitted in the prompt rather than silently dropped. |
| `FORGEGUARD_BRIEF_DIR` | no | `~/.local/share/forge-guard/briefs` | Where the brief sweep stores per-project auto-generated review briefs, and where `files`-mode reviews look for one to inject. |
| `FORGEGUARD_FOOTER` | no | `⚙️ auto-review is advisory` | Last line of every review card in Feishu. Set to your own wording (bilingual, a link to a policy page, …) or to an empty string to drop the line. |
| `FORGEGUARD_LIMIT_WARN` | no | `0.95` | Utilization of the Claude usage window (0–1) at which the review lane posts a one-time Feishu warning with the window's reset time. Set above 1 to disable. Codex does not currently expose equivalent utilization telemetry here. |
| `REQUESTS_CA_BUNDLE` | no | *(system default)* | Path to a private CA bundle if your GitLab sits behind one. |

## Per-project review rules (`.forgeguard.md`)

In `files` mode the review prompt includes a rules file from the reviewed
repo itself: `.forgeguard.md` at the repo root, or `CLAUDE.md` if that's
absent. It is fetched at the MR's head SHA, so every review sees the version
current for that branch, and it changes the way everything else in the repo
changes — through an MR the team can see. Keep it short (only the first
16 KB is injected) and state things a reviewer can act on:

- **Technical watch-outs** — invariants a diff can silently break
  ("every route must go through the permission middleware", "schema
  changes require a paired migration").
- **Project context** — what the service does, which directories are
  generated code or vendored and should not draw review comments.
- **Conventions with a failure mode** — not style preferences; the review
  prompt discards those anyway.

The file is maintained by people, not appended to by the reviewer — rules
earn their place by being written down deliberately, and stale rules are
pruned the same way. Where nobody maintains such a file, the review does
not depend on one: the durable context is the changed files' own content,
which is fetched from the code at the head SHA and cannot drift from
reality. The prompt also instructs the reviewer that code outranks docs —
a rule contradicted by the visible code is never grounds for an issue on
its own.

## Auto-generated project briefs (`forgeguard brief`)

For fleets where nobody writes rules files, the brief sweep generates the
context instead. `forgeguard brief` walks every non-excluded project and,
where the brief is missing or the default branch has moved ≥30 commits
since it was generated, downloads a snapshot of the repo (archive at the
head SHA, no clone, no credentials on disk), runs the Claude CLI over it
with read-only tools, and stores a ≤4 KB review brief — what the service
does, layout, which directories are generated/vendored, and the observable
conventions a diff can silently break — under `FORGEGUARD_BRIEF_DIR`.
`files`-mode reviews inject a project's brief automatically whenever one
exists.

Briefs are a cache, not source: regenerated from scratch (never appended
to) so staleness cannot accumulate, bounded by the refresh threshold, and
kept out of every repo. Generation costs minutes per project, so one run
generates at most 20 briefs and the remainder converges on later runs —
schedule it weekly (`systemd/forgeguard-brief.*.example`). A failed
generation keeps the previous brief and is counted in the run summary.

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
5. **Install the schedules** — systemd user timers on Linux, launchd on
   macOS (the shipped examples run `forgeguard.cli sweep` hourly and
   `forgeguard.cli review` every 15 minutes; the review interval is one
   line in the timer/plist — 5 minutes is fine on a dedicated box).

   **Linux (systemd):** copy each `.example` file from `systemd/` to
   `~/.config/systemd/user/`, stripping the `.example` suffix and
   substituting `__REPO__` for the absolute path to this checkout:
   ```sh
   for f in systemd/*.example; do
     dest=~/.config/systemd/user/$(basename "${f%.example}")
     sed "s#__REPO__#$(pwd)#g" "$f" > "$dest"
   done
   systemctl --user daemon-reload
   systemctl --user enable --now forgeguard-review.timer forgeguard-sweep.timer
   loginctl enable-linger $USER   # keep timers running with no login session
   ```
   Tick output lands in the journal: `journalctl --user -u forgeguard-review`.

   **macOS (launchd):** copy each `.example` file from `launchd/` to
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
6. **Watch the first sweep** (journal on Linux,
   `~/Library/Logs/ForgeGuard/sweep.log` on macOS) and confirm
   protection/violation messages land in the Feishu group.

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
- **Reviewer usage limits pause the review lane, not the rest.** When the
  selected CLI reports a usage limit, the tick stops at the first rejected
  review, holds its cursor, and reports the reset time when the provider
  supplies one. Claude also exposes rolling-window utilization, so Forge Guard
  can warn before that window fills through `FORGEGUARD_LIMIT_WARN`. Later
  ticks retry quietly until the limit resets, then review the backlog in order.
  Sweep and quality gate don't use the review provider and keep running.
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
