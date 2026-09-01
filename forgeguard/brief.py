from __future__ import annotations
import io, os, subprocess, tarfile, tempfile
from urllib.parse import quote
from .config import Config
from .gitlab import GitLab, GitLabError

MARKER_PREFIX = "<!-- forge-guard-brief "

BRIEF_PROMPT = """Explore this repository (it is a read-only snapshot) and write a
review brief for it: background a reviewer needs to judge a merge request
against this codebase. Cover, tersely:
- What the service/application does, and its stack.
- Layout: what lives where; which directories are generated or vendored
  code that should never draw review comments.
- Conventions and invariants you can actually observe in the code — the
  kind a diff can silently break (e.g. how routes get auth, how schema
  changes are migrated, how errors are surfaced). Cite the pattern, not
  style preferences.
Output ONLY the brief as plain markdown, at most 4000 characters. No
preamble, no code fences around the whole thing, no questions.
"""

BRIEF_CAP = 8_000          # stored brief size cap, chars
REFRESH_COMMITS = 30       # regenerate after this many commits on the default branch
MAX_PER_RUN = 20           # generation is minutes per project; bound one sweep run

def _brief_path(cfg: Config, project_path: str) -> str:
    return os.path.join(cfg.brief_dir, project_path.replace("/", "__") + ".md")

def load_brief(cfg: Config, project_path: str) -> str | None:
    # Injection side (used by the review lane): marker line stripped.
    try:
        with open(_brief_path(cfg, project_path), encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return None
    lines = text.splitlines()
    if lines and lines[0].startswith(MARKER_PREFIX):
        lines = lines[1:]
    return "\n".join(lines).strip()[:BRIEF_CAP] or None

def _brief_sha(cfg: Config, project_path: str) -> str | None:
    try:
        with open(_brief_path(cfg, project_path), encoding="utf-8") as f:
            first = f.readline()
    except OSError:
        return None
    if first.startswith(MARKER_PREFIX):
        return first[len(MARKER_PREFIX):].strip(" ->\n") or None
    return None

def _needs_refresh(gl: GitLab, pid: int, old_sha: str | None, head: str) -> bool:
    if old_sha is None:
        return True
    if old_sha == head:
        return False
    try:
        cmp = gl.get(f"/projects/{pid}/repository/compare",
                     **{"from": old_sha, "to": head})
        return len(cmp.get("commits", [])) >= REFRESH_COMMITS
    except GitLabError:
        return True    # force-pushed / rewritten history: brief basis is gone

def run_claude_brief(prompt: str, cwd: str) -> str:
    # Lazy import: review.py imports this module for load_brief.
    from .review import _claude_binary, _scrubbed_env
    r = subprocess.run([_claude_binary(), "-p", "--output-format", "text",
                        "--strict-mcp-config", "--setting-sources=",
                        "--allowedTools", "Read,Glob,Grep"],
                       input=prompt, capture_output=True, text=True, timeout=600,
                       env=_scrubbed_env(), cwd=cwd)
    if r.returncode != 0:
        raise RuntimeError(f"claude exited {r.returncode}: {r.stderr[:200]}")
    return r.stdout.strip()

def _generate(gl: GitLab, cfg: Config, pid: int, project_path: str, head: str) -> None:
    data = gl.get_bytes(f"/projects/{pid}/repository/archive.tar.gz", sha=head)
    with tempfile.TemporaryDirectory() as tmp:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            tar.extractall(tmp, filter="data")
        roots = os.listdir(tmp)
        root = os.path.join(tmp, roots[0]) if len(roots) == 1 else tmp
        text = run_claude_brief(BRIEF_PROMPT, cwd=root)
    if not text:
        raise RuntimeError("empty brief output")
    os.makedirs(cfg.brief_dir, exist_ok=True)
    path = _brief_path(cfg, project_path)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(f"{MARKER_PREFIX}{head} -->\n{text[:BRIEF_CAP]}\n")
    os.replace(tmp_path, path)

def run_brief_sweep(gl: GitLab, cfg: Config, seen: set[int] | None = None) -> dict:
    out = {"projects": 0, "generated": 0, "fresh": 0, "failed": 0}
    for project in gl.get_all("/projects", archived=False):
        path = project["path_with_namespace"]
        if path in cfg.exclude or not project.get("default_branch"):
            continue
        if seen is not None:
            if project["id"] in seen:
                continue
            seen.add(project["id"])
        out["projects"] += 1
        pid = project["id"]
        try:
            branch = gl.get(f"/projects/{pid}/repository/branches/"
                            f"{quote(project['default_branch'], safe='')}", ok404=True)
            if not branch:
                continue
            head = branch["commit"]["id"]
            if not _needs_refresh(gl, pid, _brief_sha(cfg, path), head):
                out["fresh"] += 1
                continue
            if out["generated"] >= MAX_PER_RUN:
                continue    # remainder converges on later runs
            _generate(gl, cfg, pid, path, head)
            out["generated"] += 1
        except (RuntimeError, KeyError, OSError, GitLabError,
                subprocess.TimeoutExpired, tarfile.TarError) as e:
            out["failed"] += 1
            print(f"brief failed: {path} — {type(e).__name__}: {str(e)[:160]}")
            continue
    return out
