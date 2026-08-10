from __future__ import annotations
import sys
from .config import Config, load_config
from .feishu import Feishu
from .gitlab import GitLab, GitLabError, rebase_url
from .protect import classify_move, ensure_protection
from .state import State

def _commit_url(project: dict, sha: str, base: str) -> str:
    return rebase_url(f"{project['web_url']}/-/commit/{sha}", base)

def run_sweep(gl: GitLab, state: State, feishu, cfg: Config,
              seen: set[int] | None = None) -> dict:
    # seen dedupes projects across per-token runs (tokens' views overlap).
    out = {"projects": 0, "protected": 0, "force_push": 0, "unmr": 0, "errors": 0}
    try:
        for project in gl.get_all("/projects", archived=False):
            if project["path_with_namespace"] in cfg.exclude:
                continue
            if seen is not None:
                if project["id"] in seen:
                    continue
                seen.add(project["id"])
            out["projects"] += 1
            pid, path = project["id"], project["path_with_namespace"]
            try:
                branches = gl.get_all(f"/projects/{pid}/repository/branches")
                for b in branches:
                    name = b["name"]
                    if name not in cfg.branches:
                        continue
                    if ensure_protection(gl, pid, name):
                        out["protected"] += 1
                    new_tip, old_tip = b["commit"]["id"], state.get_tip(pid, name)
                    if old_tip and old_tip != new_tip:
                        move = classify_move(gl, pid, name, old_tip, new_tip)
                        if move["kind"] == "force_push":
                            out["force_push"] += 1
                            feishu.notify(
                                f"⚠️ FORCE PUSH on {path} {name} "
                                f"(last author {b['commit'].get('author_name', '?')}): "
                                f"{_commit_url(project, new_tip, cfg.gitlab_url)}")
                        else:
                            for sha in move["unmr_commits"]:
                                out["unmr"] += 1
                                feishu.notify(
                                    f"⚠️ merge without MR on {path} {name} "
                                    f"(last author {b['commit'].get('author_name', '?')}): "
                                    f"{_commit_url(project, sha, cfg.gitlab_url)}",
                                    at_gitlab_user=b["commit"].get("author_name"))
                    state.set_tip(pid, name, new_tip)
            except GitLabError:
                out["errors"] += 1
                continue
        if out["errors"] > 0:
            feishu.notify(f"⚠️ sweep: {out['errors']} project(s) failed (GitLab errors) — coverage incomplete")
    finally:
        state.save(cfg.state_path)
    return out

def main(argv=None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if not args or args[0] not in {"sweep", "review", "inject-gate"}:
        print("usage: forgeguard sweep|review|inject-gate", file=sys.stderr)
        return 2
    cfg = load_config()
    state = State.load(cfg.state_path)
    clients = [GitLab(cfg)] + [GitLab(cfg, token=t) for t in cfg.extra_tokens]
    if args[0] == "sweep":
        feishu, seen = Feishu(cfg), set()
        total = {"projects": 0, "protected": 0, "force_push": 0, "unmr": 0, "errors": 0}
        for gl in clients:
            for k, v in run_sweep(gl, state, feishu, cfg, seen=seen).items():
                total[k] += v
        print(total)
        return 0
    if args[0] == "review":
        from .review import run_review_tick
        feishu = Feishu(cfg)
        total = {"reviewed": 0, "skipped_large": 0, "failed": 0}
        for i, gl in enumerate(clients):
            key = "mr_updated_after" if i == 0 else f"mr_updated_after:{i}"
            for k, v in run_review_tick(gl, state, feishu, cfg, cursor_key=key).items():
                total[k] += v
        print(total)
        return 0
    from .review import inject_gate
    return inject_gate(clients[0], cfg, apply="--apply" in args)

if __name__ == "__main__":
    raise SystemExit(main())
