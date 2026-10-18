from __future__ import annotations
import os, subprocess, tempfile
from collections import deque
from pathlib import PurePosixPath
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

SNAPSHOT_MAX_FILES = 160
SNAPSHOT_MAX_BYTES = 10 * 1024 * 1024
SNAPSHOT_FILE_BYTES = 256 * 1024
SNAPSHOT_MAX_REQUESTS = 240
SNAPSHOT_PAGE_SIZE = 100
SNAPSHOT_SKIP_DIRS = {".git", "node_modules", "vendor", ".venv", "venv",
                      "__pycache__", ".cache", ".next", "dist", "build", ".fvm"}
SNAPSHOT_SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico",
                          ".pdf", ".zip", ".gz", ".tar", ".jar", ".war",
                          ".mp4", ".mp3", ".woff", ".woff2", ".ttf", ".map",
                          ".lock", ".min.js", ".min.css"}


def _snapshot(gl: GitLab, pid: int, head: str, root: str) -> str:
    # Breadth first keeps root manifests and representative source ahead of deep trees.
    pending = deque([("", 1)])
    seen_dirs = {""}
    files = size = requests = skipped = 0
    while pending and requests < SNAPSHOT_MAX_REQUESTS and files < SNAPSHOT_MAX_FILES:
        path, page = pending.popleft()
        entries = gl.get(f"/projects/{pid}/repository/tree", ref=head, path=path,
                         per_page=SNAPSHOT_PAGE_SIZE, page=page, recursive=False)
        requests += 1
        if len(entries) == SNAPSHOT_PAGE_SIZE:
            pending.append((path, page + 1))
        # Files first; root readmes/configs are collected before descending.
        for entry in sorted(entries, key=lambda e: (e.get("type") == "tree", e.get("path", ""))):
            name = entry.get("path", "")
            parts = PurePosixPath(name).parts
            if (not parts or name.startswith("/") or ".." in parts or "\\" in name
                    or any(part in SNAPSHOT_SKIP_DIRS for part in parts)
                    or str(PurePosixPath(name)) != name):
                skipped += 1
                continue
            if entry.get("type") == "tree":
                if name not in seen_dirs:
                    seen_dirs.add(name)
                    pending.append((name, 1))
                continue
            if (entry.get("type") != "blob" or entry.get("mode") not in {"100644", "100755"}
                    or any(name.lower().endswith(ext) for ext in SNAPSHOT_SKIP_SUFFIXES)):
                skipped += 1
                continue
            if (requests >= SNAPSHOT_MAX_REQUESTS or files >= SNAPSHOT_MAX_FILES
                    or size >= SNAPSHOT_MAX_BYTES):
                skipped += 1
                continue
            data = gl.get_bytes_bounded(
                f"/projects/{pid}/repository/files/{quote(name, safe='')}/raw",
                min(SNAPSHOT_FILE_BYTES, SNAPSHOT_MAX_BYTES - size), ref=head)
            requests += 1
            if data is None or b"\0" in data:
                skipped += 1
                continue
            try:
                data.decode("utf-8")
            except UnicodeDecodeError:
                skipped += 1
                continue
            target = os.path.join(root, *parts)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "wb") as f:
                f.write(data)
            files += 1
            size += len(data)
    if not files or not size:
        raise RuntimeError("no usable source files in bounded snapshot")
    return (f"Snapshot coverage: sampled {files} files, {size} bytes at {head}; "
            f"{requests} API requests, {skipped} encountered entries omitted. "
            f"Limits: {SNAPSHOT_MAX_FILES} files, {SNAPSHOT_MAX_BYTES} bytes total, "
            f"{SNAPSHOT_FILE_BYTES} bytes/file, {SNAPSHOT_MAX_REQUESTS} requests. "
            "Known generated/vendor/cache paths, binary, oversized, symlink and submodule entries "
            "are omitted; traversal may stop at limits, so unseen paths are not counted. "
            "This is partial repository context; infer conventions only from included files.")

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

def run_codex_brief(prompt: str, cwd: str, model: str) -> str:
    # Lazy import: review.py imports this module for load_brief.
    from .review import _codex_binary, _scrubbed_env
    with tempfile.TemporaryDirectory(prefix="forgeguard-codex-brief-") as tmp:
        output_path = os.path.join(tmp, "brief.md")
        cmd = [_codex_binary(), "exec", "--model", model,
               "--config", 'model_reasoning_effort="high"',
               "--disable", "multi_agent",
               "--config", "tools.view_image=false",
               "--config", 'web_search="disabled"',
               "--ephemeral", "--sandbox", "read-only",
               "--ignore-user-config", "--ignore-rules",
               "--skip-git-repo-check", "--json",
               "--output-last-message", output_path, "-"]
        r = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                           timeout=600, env=_scrubbed_env(), cwd=cwd)
        detail = "\n".join(p for p in (r.stderr, r.stdout) if p).strip()
        if r.returncode != 0:
            raise RuntimeError(f"codex exited {r.returncode}: {detail[:200]}")
        try:
            with open(output_path, encoding="utf-8") as f:
                text = f.read().strip()
        except OSError as e:
            raise RuntimeError("codex returned no brief output") from e
        if not text:
            raise RuntimeError("codex returned no brief output")
        return text

def run_brief_provider(prompt: str, cfg: Config, cwd: str) -> str:
    if cfg.review_provider == "codex":
        return run_codex_brief(prompt, cwd=cwd, model=cfg.review_model)
    return run_claude_brief(prompt, cwd=cwd)

def _generate(gl: GitLab, cfg: Config, pid: int, project_path: str, head: str) -> None:
    with tempfile.TemporaryDirectory(prefix="forgeguard-brief-") as root:
        coverage = _snapshot(gl, pid, head, root)
        text = run_brief_provider(BRIEF_PROMPT + "\n" + coverage, cfg, cwd=root)
    if not text:
        raise RuntimeError("empty brief output")
    os.makedirs(cfg.brief_dir, exist_ok=True)
    path = _brief_path(cfg, project_path)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(f"{MARKER_PREFIX}{head} -->\n{(coverage + chr(10) + chr(10) + text)[:BRIEF_CAP]}\n")
    os.replace(tmp_path, path)

def run_brief_sweep(gl: GitLab, cfg: Config, seen: set[int] | None = None) -> dict:
    out = {"projects": 0, "generated": 0, "fresh": 0, "failed": 0}
    for project in gl.get_all("/projects", archived=False, membership=True):
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
                subprocess.TimeoutExpired) as e:
            out["failed"] += 1
            print(f"brief failed: {path} — {type(e).__name__}: {str(e)[:160]}")
            continue
    return out
