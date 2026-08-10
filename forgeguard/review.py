from __future__ import annotations
import json, subprocess
from .config import Config
from .gitlab import GitLab, GitLabError, rebase_url
from .state import State

MARKER = "<!-- forge-guard-review -->"

TRIAL_NOTE = ("⚙️ trial: auto-review currently runs on CX's workstation — "
              "best-effort availability, advisory only / 试运行阶段，评审服务暂跑在CX工作机上，仅供参考")

PROMPT = """You are reviewing a GitLab merge request for an internal team.
Respond with ONLY a JSON object, no prose, matching:
{{"verdict":"clean"|"issues","summary":"...","issues":[{{"severity":"high|medium|low","file":"...","note":"..."}}],"tests_opinion":"..."}}
Rules: verdict "issues" if any high/medium issue OR feature changes lack test changes.
Be terse. Max 6 issues, most severe first.

MR title: {title}
Target branch: {target}
Description:
{description}

Diff:
{diff}
"""

def run_claude(prompt: str) -> dict:
    r = subprocess.run(["claude", "-p", "--output-format", "text"],
                       input=prompt, capture_output=True, text=True, timeout=300)
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

def _render_note(v: dict) -> str:
    lines = [MARKER, f"**forge-guard review — {v['verdict']}**", "", v["summary"], ""]
    for i in v.get("issues", []):
        lines.append(f"- **{i['severity']}** `{i['file']}` — {i['note']}")
    lines += ["", f"_Tests: {v.get('tests_opinion', '')}_"]
    return "\n".join(lines)

def run_review_tick(gl: GitLab, state: State, feishu, cfg: Config) -> dict:
    out = {"reviewed": 0, "skipped_large": 0, "failed": 0}
    params = {"scope": "all", "state": "opened"}
    cursor = state.get_cursor("mr_updated_after")
    if cursor:
        params["updated_after"] = cursor
    mrs = [m for m in gl.get_all("/merge_requests", **params)
           if m["target_branch"] in cfg.branches]
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
                diff = "\n".join(c.get("diff", "") for c in changes.get("changes", []))
                mr_url = rebase_url(mr["web_url"], cfg.gitlab_url)
                commit_url = rebase_url(f"{project['web_url']}/-/commit/{sha}", cfg.gitlab_url)
                if len(diff.encode()) > cfg.diff_cap_bytes:
                    _upsert_note(gl, pid, iid, f"{MARKER}\nMR too large for auto-review "
                                               f"({len(diff.encode())} bytes > cap).")
                    out["skipped_large"] += 1
                else:
                    v = run_claude(PROMPT.format(title=mr["title"], target=mr["target_branch"],
                                                 description=mr.get("description") or "",
                                                 diff=diff))
                    _upsert_note(gl, pid, iid, _render_note(v))
                    if v["verdict"] == "clean":
                        try:
                            gl.post(f"/projects/{pid}/merge_requests/{iid}/approve")
                        except GitLabError:
                            pass
                    author = mr["author"]["username"]
                    if v["verdict"] == "clean":
                        head = f"✅ approved: {mr['title']} (by {author})"
                    else:
                        n = len(v.get("issues", []))
                        head = f"📝 review left for {author}: {mr['title']} — {n} issue(s)"
                    feishu.notify(
                        f"{head}\n{v['summary']}\nMR: {mr_url}\ncommit: {commit_url}\n"
                        f"{TRIAL_NOTE}",
                        at_gitlab_user=author)
                    if author not in feishu.usermap and state.flag_once(f"usermap:{author}"):
                        feishu.notify(f"ℹ️ no Feishu mapping for GitLab user '{author}' — "
                                      f"add to forge-guard-usermap.json to enable @mentions")
                    out["reviewed"] += 1
                state.set_cursor(f"reviewed:{pid}:{iid}", sha)
                max_updated = max(max_updated, mr["updated_at"])
            except (RuntimeError, KeyError, GitLabError):
                out["failed"] += 1
                continue
        if max_updated and out["failed"] == 0:
            state.set_cursor("mr_updated_after", max_updated)
    finally:
        state.save(cfg.state_path)
    return out

def inject_gate(gl: GitLab, cfg: Config, apply: bool) -> int:
    if apply:
        print("inject-gate --apply is gated on runner verification (see spec "
              "'To verify'); run without --apply for a dry-run list.")
        return 2
    print("inject-gate: dry-run — deployment steps land with the ci-template "
          "rollout (plan Task 8).")
    return 0
