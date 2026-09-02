from __future__ import annotations
import json, os, re, shutil, subprocess, tempfile
from datetime import datetime
from urllib.parse import quote
import requests
from .brief import load_brief
from .config import Config
from .gitlab import GitLab, GitLabError, rebase_url
from .state import State

MARKER = "<!-- forge-guard-review -->"


PROMPT = """You are reviewing a GitLab merge request for an internal team.
Respond with ONLY a JSON object, no prose, matching:
{{"verdict":"clean"|"issues","summary":"...","issues":[{{"severity":"high|medium|low","file":"...","note":"..."}}],"tests_opinion":"..."}}
Rules:
- Flag ONLY defects where you can state a concrete failure: wrong behavior,
  crash, data loss, security hole, or a specific maintenance trap. Each issue's
  note MUST name that concrete consequence.
- NO style, taste, naming, or formatting comments. NO speculation. If you are
  not sure something is a real problem, OMIT it — a missed nitpick costs
  nothing; a false alarm costs trust.
- Judge ONLY the change shown in the diff. Project rules and full file
  contents, when provided, are context for judging the change; pre-existing
  issues outside the diff are out of scope.
- Code outranks docs. Project rules files may be stale; if a stated rule
  contradicts what the file contents show, trust the code and never raise
  an issue on the rule's authority alone.
- verdict "issues" only if a high/medium issue survives the rules above, OR
  feature changes clearly lack test changes.
- Be terse. Max 4 issues, most severe first.

MR title: {title}
Target branch: {target}
Description:
{description}

{context}Diff:
{diff}
"""

SKEPTIC_PROMPT = """You previously reviewed a merge request and reported these issues:
{issues}

Re-examine each against the material below as a skeptical senior engineer.
KEEP an issue only if you can defend its concrete failure scenario; DROP
anything that is a style preference, speculative, or that you cannot fully
justify from the diff and provided context alone. Respond with ONLY the same
JSON schema containing the surviving issues. If none survive and tests are
adequate, verdict is "clean".
{{"verdict":"clean"|"issues","summary":"...","issues":[{{"severity":"high|medium|low","file":"...","note":"..."}}],"tests_opinion":"..."}}

{context}Diff:
{diff}
"""

RULES_FILES = (".forgeguard.md", "CLAUDE.md")
RULES_CAP = 16_000       # bytes of the rules file injected into the prompt
FILE_CAP = 100_000       # per-file content cap in files mode

def _fetch_raw(gl: GitLab, pid: int, path: str, ref: str) -> str | None:
    # Context is advisory: any fetch failure degrades the review to less
    # context, never to no review.
    try:
        return gl.get_raw(f"/projects/{pid}/repository/files/"
                          f"{quote(path, safe='')}/raw", ok404=True, ref=ref)
    except (GitLabError, requests.RequestException):
        return None

def _build_context(gl: GitLab, pid: int, sha: str, files: list, cap: int,
                   brief: str | None = None) -> str:
    parts = []
    if brief:
        parts.append("Project brief (auto-generated from this codebase; the "
                     "code outranks it where they disagree):\n" + brief)
    for name in RULES_FILES:
        if rules := _fetch_raw(gl, pid, name, sha):
            parts.append(f"Project review rules ({name} in the repo — apply "
                         f"them when judging this change):\n{rules[:RULES_CAP]}")
            break
    budget, blobs, omitted = cap, [], []
    for c in files:
        if c.get("deleted_file"):
            continue
        path = c.get("new_path") or c.get("old_path") or ""
        content = _fetch_raw(gl, pid, path, sha)
        if content is None or "\x00" in content[:8192]:
            continue
        if (size := len(content.encode())) > FILE_CAP or size > budget:
            omitted.append(path)
            continue
        budget -= size
        blobs.append(f"==== {path} ====\n{content}")
    if blobs or omitted:
        head = "Full content of each changed file at the MR head commit:"
        if omitted:
            head += (f"\n[{len(omitted)} changed file(s) omitted for size: "
                     + ", ".join(omitted[:10]) + "]")
        parts.append("\n".join([head] + ["\n" + b for b in blobs]))
    return "\n\n".join(parts) + "\n\n" if parts else ""

_CRED_SEGMENTS = {"TOKEN", "SECRET", "KEY", "APIKEY", "PASSWORD", "PASSWD",
                  "CREDENTIAL", "CREDENTIALS", "PAT"}

def _scrubbed_env() -> dict[str, str]:
    # The review subprocess is model-driven; it must not inherit forge-guard's
    # GitLab/Feishu credentials or other secret-shaped env vars. Segment match
    # (split on "_") so PATH survives while AWS_PAT / ANTHROPIC_API_KEY don't.
    return {k: v for k, v in os.environ.items()
            if not (k.startswith("FORGEGUARD_")
                    or _CRED_SEGMENTS & set(k.upper().split("_")))}

def _claude_binary() -> str:
    # Absolute binary: launchd PATH lacks ~/.local/bin.
    return (os.environ.get("FORGEGUARD_CLAUDE_BIN")
            or shutil.which("claude")
            or os.path.expanduser("~/.local/bin/claude"))

class ClaudeLimit(RuntimeError):
    """Claude usage limit hit. resets_at: unix time the window resets, or None;
    detail: Claude's own wording (carries the reset time when resets_at is None)."""
    def __init__(self, resets_at, window: str = "usage", detail: str = ""):
        self.resets_at, self.window, self.detail = resets_at, window, detail
        super().__init__(f"Claude {window.replace('_', '-')} usage limit reached")

# Claude Code's limit wording has varied ("usage limit reached", "You've hit
# your session limit · resets 3:30pm"); match the family, not one phrase.
_LIMIT_TEXT = re.compile(r"(usage|session|weekly|rate) limit|hit your [^.\n]{0,30}limit|limit reached", re.I)

# Most recent rate_limit_info reported by claude -p (utilization, resetsAt,
# rateLimitType, status). Module state on purpose: the tick reads it after a
# review to warn before the limit is actually hit.
RATE_LIMIT: dict = {}

def fmt_reset(ts) -> str:
    if not ts:
        return "an unknown time"
    t = datetime.fromtimestamp(ts).astimezone()
    today = datetime.now().astimezone().date()
    return t.strftime("%H:%M") if t.date() == today else t.strftime("%a %d %b %H:%M")

def run_claude(prompt: str) -> dict:
    # Flags per fleet schedule-lib: without --strict-mcp-config
    # --setting-sources= a scheduled claude -p loads the claude-mem MCP
    # stack and deadlocks on shared chroma. stream-json (needs --verbose)
    # is the only output mode that carries the rate_limit_event, which is
    # how the tick learns the reset time when the usage limit is hit.
    try:
        r = subprocess.run([_claude_binary(), "-p", "--output-format", "stream-json",
                            "--verbose", "--strict-mcp-config", "--setting-sources="],
                           input=prompt, capture_output=True, text=True, timeout=300,
                           env=_scrubbed_env(), cwd=tempfile.gettempdir())
    except subprocess.TimeoutExpired as e:
        # Must be a RuntimeError: anything else escapes the tick and kills it.
        raise RuntimeError(f"claude timed out after {e.timeout:.0f}s") from e
    result, rate = None, None
    for line in r.stdout.splitlines():
        try:
            o = json.loads(line)
        except ValueError:
            continue
        if o.get("type") == "rate_limit_event":
            rate = o.get("rate_limit_info") or rate
        elif o.get("type") == "result":
            result = o
    if rate:
        RATE_LIMIT.clear()
        RATE_LIMIT.update(rate)
    rate = rate or {}
    text = str((result or {}).get("result") or "")
    failed = r.returncode != 0 or result is None or result.get("is_error")
    if failed and (rate.get("status") not in (None, "allowed", "allowed_warning")
                   or _LIMIT_TEXT.search(text + r.stderr)):
        raise ClaudeLimit(rate.get("resetsAt"), rate.get("rateLimitType", "usage"),
                          detail=(text or r.stderr).strip()[:120])
    if r.returncode != 0:
        raise RuntimeError(f"claude exited {r.returncode}: {r.stderr[:200]}")
    if result is None or result.get("is_error"):
        raise RuntimeError(f"claude error: {text[:200]}")
    try:
        return json.loads(text[text.index("{"):text.rindex("}") + 1])
    except (ValueError, json.JSONDecodeError) as e:
        raise RuntimeError(f"unparseable claude output: {text[:200]}") from e

def _warn_near_limit(state: State, feishu, cfg: Config) -> None:
    # Top-level utilization only appears once a threshold is crossed; the
    # per-window block is always there.
    window, resets = RATE_LIMIT.get("rateLimitType", "usage"), RATE_LIMIT.get("resetsAt")
    win = (RATE_LIMIT.get("unifiedWindows") or {}).get(window) or {}
    u = RATE_LIMIT.get("utilization") or win.get("utilization") or 0
    if u >= cfg.limit_warn and state.flag_once(f"limitwarn:{window}:{resets}"):
        feishu.notify(f"⚠️ forge-guard is at {u:.0%} of the Claude {window.replace('_', '-')} "
                      f"usage limit — it resets at {fmt_reset(resets)}. If it hits 100%, "
                      f"reviews pause and resume automatically after the reset.")

def _upsert_note(gl: GitLab, pid: int, iid: int, body: str) -> None:
    for note in gl.get_all(f"/projects/{pid}/merge_requests/{iid}/notes"):
        if str(note.get("body", "")).startswith(MARKER):
            gl.put(f"/projects/{pid}/merge_requests/{iid}/notes/{note['id']}", body=body)
            return
    gl.post(f"/projects/{pid}/merge_requests/{iid}/notes", body=body)

def _replace_note(gl: GitLab, pid: int, iid: int, body: str) -> None:
    for note in gl.get_all(f"/projects/{pid}/merge_requests/{iid}/notes"):
        if str(note.get("body", "")).startswith(MARKER):
            gl.put(f"/projects/{pid}/merge_requests/{iid}/notes/{note['id']}", body=body)
            return

def _render_note(v: dict) -> str:
    lines = [MARKER, f"**forge-guard review — {v['verdict']}**", "", v["summary"], ""]
    for i in v.get("issues", []):
        lines.append(f"- **{i['severity']}** `{i['file']}` — {i['note']}")
    lines += ["", f"_Tests: {v.get('tests_opinion', '')}_"]
    return "\n".join(lines)

def run_review_tick(gl: GitLab, state: State, feishu, cfg: Config,
                    cursor_key: str = "mr_updated_after") -> dict:
    out = {"reviewed": 0, "skipped_large": 0, "failed": 0, "merged_unreviewed": 0}
    params = {"scope": "all", "state": "opened"}
    cursor = state.get_cursor(cursor_key)
    if cursor:
        params["updated_after"] = cursor
    mrs = [m for m in gl.get_all("/merge_requests", **params)
           if cfg.review_match(m["target_branch"])]
    max_updated = cursor or ""
    projects: dict[int, dict] = {}
    try:
        for i, mr in enumerate(mrs):
            pid, iid, sha = mr["project_id"], mr["iid"], mr["sha"]
            try:
                project = projects.setdefault(pid, gl.get(f"/projects/{pid}"))
                if project["path_with_namespace"] in cfg.exclude:
                    continue
                if state.get_cursor(f"reviewed:{pid}:{iid}") == sha:
                    max_updated = max(max_updated, mr["updated_at"])
                    continue
                changes = gl.get(f"/projects/{pid}/merge_requests/{iid}/changes")
                files = changes.get("changes", [])
                diff = "\n".join(c.get("diff", "") for c in files)
                # Truncation is only what GitLab itself marks (changes_count
                # "N+" / overflow); that payload is a fragment, not the MR, so
                # it is never reviewed. A blank per-file diff alone is normal —
                # binary, collapsed-generated, or moved files.
                partial = (str(changes.get("changes_count", "")).endswith("+")
                           or bool(changes.get("overflow")))
                coverage = f"{sum(1 for c in files if c.get('diff'))} of {len(files)}"
                mr_url = rebase_url(mr["web_url"], cfg.gitlab_url)
                commit_url = rebase_url(f"{project['web_url']}/-/commit/{sha}", cfg.gitlab_url)
                full = cfg.full_review_label in (mr.get("labels") or [])
                cap = cfg.diff_cap_full if full else cfg.diff_cap_bytes
                if partial:
                    # A review of a truncated payload reads like a verdict on the
                    # MR; don't produce one. Only an already-posted review note
                    # is rewritten so a stale verdict doesn't stand.
                    _replace_note(gl, pid, iid, f"{MARKER}\nMR not reviewed: GitLab truncated "
                                  f"the diff (content for {coverage} changed files) — too "
                                  f"large for the API to serve in full.")
                    if state.flag_once(f"skiplarge:{pid}:{iid}"):
                        feishu.notify_post(
                            f"⚠️ Review skipped: {mr['title']} — GitLab truncated the diff",
                            [[{"tag": "text", "text": "MR: "},
                              {"tag": "a", "text": f"!{iid}", "href": mr_url}],
                             [{"tag": "text", "text":
                               f"GitLab returned diff content for only {coverage} changed "
                               f"files, so no auto-review was run."}],
                             [{"tag": "text", "text":
                               "How to get it reviewed: GitLab's API can't serve this diff "
                               "in full, so the full-review label won't help — split the MR "
                               "into smaller MRs (e.g. code separate from generated docs/"
                               ".planning), each well under the current file count. Every "
                               "smaller MR is reviewed on its next push."}]],
                            at_gitlab_user=mr["author"]["username"])
                    out["skipped_large"] += 1
                elif (size := len(diff.encode())) > cap:
                    if full:
                        _upsert_note(gl, pid, iid, f"{MARKER}\nMR too large for auto-review "
                                     f"even with `{cfg.full_review_label}` ({size} bytes > "
                                     f"{cap} hard cap).")
                    else:
                        _upsert_note(gl, pid, iid, f"{MARKER}\nMR too large for auto-review "
                                     f"({size} bytes > {cap} cap). Add the label "
                                     f"`{cfg.full_review_label}` to request a full review "
                                     f"anyway (up to {cfg.diff_cap_full} bytes).")
                        # Alert once per MR, not per push: the note carries the
                        # instructions; a labelled MR takes the review path.
                        if state.flag_once(f"skiplarge:{pid}:{iid}"):
                            feishu.notify_post(
                                f"⚠️ Review skipped: {mr['title']} — diff {size // 1000} KB "
                                f"> {cap // 1000} KB cap",
                                [[{"tag": "text", "text": "MR: "},
                                  {"tag": "a", "text": f"!{iid}", "href": mr_url}],
                                 [{"tag": "text", "text":
                                   f"How to get it reviewed: open the MR → Labels (right "
                                   f"sidebar) → add `{cfg.full_review_label}`. forge-guard "
                                   f"reviews it on the next tick and on every later "
                                   f"push, up to {cfg.diff_cap_full // 1000} KB. Above that, "
                                   f"split the MR."}]],
                                at_gitlab_user=mr["author"]["username"])
                    out["skipped_large"] += 1
                else:
                    # The diff spends the same budget: a big (labelled) diff
                    # leaves less room for file content, so the total prompt
                    # stays bounded instead of stacking both caps.
                    budget = max(0, cfg.context_cap_bytes - len(diff.encode()))
                    context = (_build_context(
                        gl, pid, sha, files, budget,
                        brief=load_brief(cfg, project["path_with_namespace"]))
                        if cfg.review_mode == "files" else "")
                    v = run_claude(PROMPT.format(title=mr["title"], target=mr["target_branch"],
                                                 description=mr.get("description") or "",
                                                 context=context, diff=diff))
                    if v["verdict"] != "clean" and v.get("issues"):
                        v = run_claude(SKEPTIC_PROMPT.format(
                            issues=json.dumps(v["issues"], ensure_ascii=False),
                            context=context, diff=diff))
                    _upsert_note(gl, pid, iid, _render_note(v))
                    if v["verdict"] == "clean":
                        try:
                            gl.post(f"/projects/{pid}/merge_requests/{iid}/approve")
                        except GitLabError:
                            pass
                    author = mr["author"]["username"]
                    if v["verdict"] == "clean":
                        title = f"✅ Approved: {mr['title']}"
                    else:
                        n = len(v.get("issues", []))
                        title = f"📝 Review: {mr['title']} — {n} issue(s)"
                    feishu.notify_post(
                        title,
                        [[{"tag": "text", "text": v["summary"]}],
                         [{"tag": "text", "text": "MR: "},
                          {"tag": "a", "text": f"!{iid}", "href": mr_url}],
                         [{"tag": "text", "text": "head commit: "},
                          {"tag": "a", "text": sha[:8], "href": commit_url}]]
                        + ([[{"tag": "text", "text": cfg.footer}]] if cfg.footer else []),
                        at_gitlab_user=author)
                    if not feishu.open_id(author) and state.flag_once(f"usermap:{author}"):
                        feishu.notify(f"ℹ️ no Feishu mapping for GitLab user '{author}' — "
                                      f"add to forge-guard-usermap.json to enable @mentions")
                    out["reviewed"] += 1
                    _warn_near_limit(state, feishu, cfg)
                state.set_cursor(f"reviewed:{pid}:{iid}", sha)
                max_updated = max(max_updated, mr["updated_at"])
            except ClaudeLimit as e:
                # Every further call this tick would fail the same way; stop
                # here, hold the cursor, and say once per window when the
                # lane comes back. Next ticks retry silently until it does.
                out["failed"] += 1
                if state.flag_once(f"limit:{e.window}:{e.resets_at}"):
                    waiting = sum(1 for m in mrs[i:] if state.get_cursor(
                        f"reviewed:{m['project_id']}:{m['iid']}") != m["sha"])
                    when = (fmt_reset(e.resets_at) if e.resets_at
                            else f"an unknown time (Claude said: {e.detail})")
                    feishu.notify(f"⏸️ forge-guard paused: Claude {e.window.replace('_', '-')} "
                                  f"usage limit reached. Reviews resume at {when}; "
                                  f"{waiting} MR(s) waiting will be reviewed then.")
                break
            except (RuntimeError, KeyError, GitLabError) as e:
                out["failed"] += 1
                # Failure ≠ clean review: surface it once per MR head; the held
                # cursor retries the review itself every tick.
                if state.flag_once(f"reviewfail:{pid}:{iid}:{sha}"):
                    url = (rebase_url(mr["web_url"], cfg.gitlab_url)
                           if mr.get("web_url") else f"{pid}!{iid}")
                    feishu.notify(f"⚠️ review failed: {url} — {type(e).__name__}: "
                                  f"{str(e)[:160]} — will retry next tick")
                continue
        if max_updated and out["failed"] == 0:
            state.set_cursor(cursor_key, max_updated)
        _check_merged_without_review(gl, state, feishu, cfg, cursor_key, out)
    finally:
        state.save(cfg.state_path)
    return out

def _check_merged_without_review(gl: GitLab, state: State, feishu, cfg: Config,
                                 cursor_key: str, out: dict) -> None:
    # Protection guarantees an MR, not a review of it: an author can merge
    # inside the tick window and the opened-state review never happens.
    # Surface those merges; the first tick only sets a baseline.
    merged_key = f"{cursor_key}:merged"
    baseline = state.get_cursor(merged_key) or state.get_cursor(cursor_key)
    if not baseline:
        return
    max_updated = baseline
    projects: dict[int, dict] = {}
    fresh: list[dict] = []
    fresh_keys: list[str] = []
    for mr in gl.get_all("/merge_requests", scope="all", state="merged",
                         updated_after=baseline):
        pid, iid, sha = mr["project_id"], mr["iid"], mr["sha"]
        max_updated = max(max_updated, mr["updated_at"])
        if not cfg.review_match(mr["target_branch"]):
            continue
        try:
            project = projects.setdefault(pid, gl.get(f"/projects/{pid}"))
        except GitLabError:
            continue
        if project["path_with_namespace"] in cfg.exclude:
            continue
        if state.get_cursor(f"reviewed:{pid}:{iid}") == sha:
            continue
        out["merged_unreviewed"] += 1
        # Peek only: flags are set after the Feishu post succeeds, so a failed
        # post leaves the alert eligible for retry on the next tick.
        key = f"noreview:{pid}:{iid}:{sha}"
        if not state.flagged(key) and key not in fresh_keys:
            fresh.append(mr)
            fresh_keys.append(key)
    if len(fresh) > 3:
        rows = []
        for mr in fresh[:15]:
            username = mr["author"]["username"]
            oid = feishu.open_id(username)
            who = ([{"tag": "at", "user_id": oid}] if oid
                   else [{"tag": "text", "text": f"@{username}"}])
            path = projects[mr["project_id"]]["path_with_namespace"]
            rows.append(who + [
                {"tag": "text", "text": " "},
                {"tag": "a", "text": f"{path}!{mr['iid']}",
                 "href": rebase_url(mr["web_url"], cfg.gitlab_url)},
                {"tag": "text", "text": f" {mr['title']}"}])
        if len(fresh) > 15:
            rows.append([{"tag": "text",
                          "text": f"…and {len(fresh) - 15} more"}])
        feishu.notify_post(
            f"⚠️ Merged without review: {len(fresh)} MRs", rows)
        for key in fresh_keys:
            state.flag_once(key)
    else:
        for mr, key in zip(fresh, fresh_keys):
            feishu.notify_post(
                f"⚠️ Merged without review: {mr['title']}",
                [[{"tag": "text", "text": "MR: "},
                  {"tag": "a", "text": f"!{mr['iid']}",
                   "href": rebase_url(mr["web_url"], cfg.gitlab_url)}],
                 [{"tag": "text", "text":
                   f"Merged into {mr['target_branch']} before forge-guard "
                   f"reviewed head {mr['sha'][:8]} — the auto-review runs on "
                   f"a periodic tick; merging within that window skips it."}]],
                at_gitlab_user=mr["author"]["username"])
            state.flag_once(key)
    state.set_cursor(merged_key, max_updated)

def inject_gate(gl: GitLab, cfg: Config, apply: bool) -> int:
    if apply:
        print("inject-gate --apply is gated on runner verification (see spec "
              "'To verify'); run without --apply for a dry-run list.")
        return 2
    print("inject-gate: dry-run — deployment steps land with the ci-template "
          "rollout (plan Task 8).")
    return 0
