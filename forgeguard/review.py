from __future__ import annotations
import json, os, shutil, subprocess, tempfile
from .config import Config
from .gitlab import GitLab, GitLabError, rebase_url
from .state import State

MARKER = "<!-- forge-guard-review -->"

TRIAL_NOTE = ("⚙️ trial: auto-review currently runs on CX's workstation — "
              "best-effort availability, advisory only / 试运行阶段，评审服务暂跑在CX工作机上，仅供参考")

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
- verdict "issues" only if a high/medium issue survives the rules above, OR
  feature changes clearly lack test changes.
- Be terse. Max 4 issues, most severe first.

MR title: {title}
Target branch: {target}
Description:
{description}

Diff:
{diff}
"""

SKEPTIC_PROMPT = """You previously reviewed a merge request and reported these issues:
{issues}

Re-examine each against the diff below as a skeptical senior engineer. KEEP an
issue only if you can defend its concrete failure scenario; DROP anything that
is a style preference, speculative, or that you cannot fully justify from the
diff alone. Respond with ONLY the same JSON schema containing the surviving
issues. If none survive and tests are adequate, verdict is "clean".
{{"verdict":"clean"|"issues","summary":"...","issues":[{{"severity":"high|medium|low","file":"...","note":"..."}}],"tests_opinion":"..."}}

Diff:
{diff}
"""

_CRED_SEGMENTS = {"TOKEN", "SECRET", "KEY", "APIKEY", "PASSWORD", "PASSWD",
                  "CREDENTIAL", "CREDENTIALS", "PAT"}

def _scrubbed_env() -> dict[str, str]:
    # The review subprocess is model-driven; it must not inherit forge-guard's
    # GitLab/Feishu credentials or other secret-shaped env vars. Segment match
    # (split on "_") so PATH survives while AWS_PAT / ANTHROPIC_API_KEY don't.
    return {k: v for k, v in os.environ.items()
            if not (k.startswith("FORGEGUARD_")
                    or _CRED_SEGMENTS & set(k.upper().split("_")))}

def run_claude(prompt: str) -> dict:
    # Absolute binary: launchd PATH lacks ~/.local/bin. Flags per fleet
    # schedule-lib: without --strict-mcp-config --setting-sources= a scheduled
    # claude -p loads the claude-mem MCP stack and deadlocks on shared chroma.
    binary = (os.environ.get("FORGEGUARD_CLAUDE_BIN")
              or shutil.which("claude")
              or os.path.expanduser("~/.local/bin/claude"))
    r = subprocess.run([binary, "-p", "--output-format", "text",
                        "--strict-mcp-config", "--setting-sources="],
                       input=prompt, capture_output=True, text=True, timeout=300,
                       env=_scrubbed_env(), cwd=tempfile.gettempdir())
    if r.returncode != 0:
        raise RuntimeError(f"claude exited {r.returncode}: {r.stderr[:200]}")
    out = r.stdout
    try:
        return json.loads(out[out.index("{"):out.rindex("}") + 1])
    except (ValueError, json.JSONDecodeError) as e:
        raise RuntimeError(f"unparseable claude output: {out[:200]}") from e

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
        for mr in mrs:
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
                    v = run_claude(PROMPT.format(title=mr["title"], target=mr["target_branch"],
                                                 description=mr.get("description") or "",
                                                 diff=diff))
                    if v["verdict"] != "clean" and v.get("issues"):
                        v = run_claude(SKEPTIC_PROMPT.format(
                            issues=json.dumps(v["issues"], ensure_ascii=False),
                            diff=diff))
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
                          {"tag": "a", "text": sha[:8], "href": commit_url}],
                         [{"tag": "text", "text": TRIAL_NOTE}]],
                        at_gitlab_user=author)
                    if not feishu.open_id(author) and state.flag_once(f"usermap:{author}"):
                        feishu.notify(f"ℹ️ no Feishu mapping for GitLab user '{author}' — "
                                      f"add to forge-guard-usermap.json to enable @mentions")
                    out["reviewed"] += 1
                state.set_cursor(f"reviewed:{pid}:{iid}", sha)
                max_updated = max(max_updated, mr["updated_at"])
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
