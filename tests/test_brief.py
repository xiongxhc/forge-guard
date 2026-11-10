import os
import responses
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from forgeguard.config import load_config
from forgeguard.gitlab import GitLab
import forgeguard.brief as brief
from forgeguard.brief import load_brief, run_brief_sweep, _brief_path
from tests.test_config import BASE

API = "https://gitlab.example.com/api/v4"

def _cfg(tmp_path, **extra):
    return load_config(dict(BASE, FORGEGUARD_STATE=str(tmp_path / "s.json"),
                            FORGEGUARD_BRIEF_DIR=str(tmp_path / "briefs"), **extra))

def _write_brief(cfg, project_path, sha, text):
    os.makedirs(cfg.brief_dir, exist_ok=True)
    with open(_brief_path(cfg, project_path), "w") as f:
        f.write(f"<!-- forge-guard-brief {sha} -->\n{text}\n")

def _snapshot_fixtures():
    responses.get(f"{API}/projects/7/repository/tree", json=[
        {"path": "main.py", "type": "blob", "mode": "100644"},
        {"path": "sdk", "type": "blob", "mode": "120000"},
        {"path": "../escape.py", "type": "blob", "mode": "100644"},
        {"path": "vendor", "type": "tree", "mode": "040000"}])
    responses.get(f"{API}/projects/7/repository/files/main.py/raw", body=b"print('hi')\n")

def _project_fixtures(head="abc123"):
    responses.get(f"{API}/projects", json=[{
        "id": 7, "path_with_namespace": "g/app", "default_branch": "main"}],
        headers={"X-Next-Page": ""})
    responses.get(f"{API}/projects/7/repository/branches/main",
                  json={"commit": {"id": head}})

def test_codex_brief_runs_read_only_in_snapshot(monkeypatch, tmp_path):
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured.update(kwargs)
        output_path = cmd[cmd.index("--output-last-message") + 1]
        with open(output_path, "w", encoding="utf-8") as f:
            f.write("# Service\nRead-only brief.\n")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setenv("FORGEGUARD_GITLAB_TOKEN", "must-not-leak")
    monkeypatch.setattr(brief.subprocess, "run", fake_run)
    runner = getattr(brief, "run_codex_brief", None)
    assert callable(runner), "Codex brief runner is missing"

    text = runner("inspect", cwd=str(snapshot), model="gpt-5.6-sol")

    assert text == "# Service\nRead-only brief."
    assert captured["input"] == "inspect"
    assert captured["cwd"] == str(snapshot)
    assert captured["timeout"] == 600
    assert "FORGEGUARD_GITLAB_TOKEN" not in captured["env"]
    cmd = captured["cmd"]
    assert cmd[1] == "exec"
    assert cmd[cmd.index("--model") + 1] == "gpt-5.6-sol"
    assert cmd[cmd.index("--sandbox") + 1] == "read-only"
    assert "--ephemeral" in cmd
    assert "--ignore-user-config" in cmd and "--ignore-rules" in cmd
    assert "--skip-git-repo-check" in cmd
    assert "shell_tool" not in cmd

@responses.activate
def test_sweep_routes_generation_to_codex_provider(tmp_path):
    _project_fixtures()
    _snapshot_fixtures()
    cfg = _cfg(tmp_path, FORGEGUARD_REVIEW_PROVIDER="codex")
    with patch("forgeguard.brief.run_codex_brief",
               return_value="Codex brief.") as codex, \
         patch("forgeguard.brief.run_claude_brief",
               return_value="Claude brief.") as claude:
        out = run_brief_sweep(GitLab(cfg), cfg)
    assert out["generated"] == 1
    codex.assert_called_once()
    assert brief.BRIEF_PROMPT in codex.call_args.args[0]
    assert "Snapshot coverage" in codex.call_args.args[0]
    assert codex.call_args.kwargs["model"] == "gpt-5.6-sol"
    claude.assert_not_called()
    assert load_brief(cfg, "g/app").endswith("Codex brief.")

def test_load_brief_strips_marker(tmp_path):
    cfg = _cfg(tmp_path)
    _write_brief(cfg, "g/app", "abc123", "Django service.\nRoutes need auth.")
    assert load_brief(cfg, "g/app") == "Django service.\nRoutes need auth."
    assert load_brief(cfg, "g/other") is None

@responses.activate
def test_sweep_generates_missing_brief(tmp_path):
    _project_fixtures()
    _snapshot_fixtures()
    cfg = _cfg(tmp_path)
    with patch("forgeguard.brief.run_claude_brief", return_value="A Django app.") as rc:
        out = run_brief_sweep(GitLab(cfg), cfg)
    assert out == {"projects": 1, "generated": 1, "fresh": 0, "failed": 0}
    assert load_brief(cfg, "g/app").endswith("A Django app.")
    with open(_brief_path(cfg, "g/app")) as f:
        assert f.readline().strip() == "<!-- forge-guard-brief abc123 -->"
    projects_call = next(call for call in responses.calls
                         if urlsplit(call.request.url).path.endswith("/projects"))
    assert parse_qs(urlsplit(projects_call.request.url).query)["membership"] == ["True"]

@responses.activate
def test_sweep_ignores_symlinks_unsafe_paths_and_vendor(tmp_path):
    _project_fixtures()
    _snapshot_fixtures()
    cfg = _cfg(tmp_path)

    def inspect_snapshot(prompt, cwd):
        assert os.path.isfile(os.path.join(cwd, "main.py"))
        assert not os.path.lexists(os.path.join(cwd, ".fvm", "flutter_sdk"))
        return "A Flutter app."

    with patch("forgeguard.brief.run_claude_brief", side_effect=inspect_snapshot):
        out = run_brief_sweep(GitLab(cfg), cfg)

    assert out == {"projects": 1, "generated": 1, "fresh": 0, "failed": 0}

@responses.activate
def test_sweep_skips_fresh_brief(tmp_path):
    _project_fixtures(head="abc123")
    cfg = _cfg(tmp_path)
    _write_brief(cfg, "g/app", "abc123", "old brief")
    with patch("forgeguard.brief.run_claude_brief") as rc:
        out = run_brief_sweep(GitLab(cfg), cfg)
    rc.assert_not_called()
    assert out == {"projects": 1, "generated": 0, "fresh": 1, "failed": 0}

@responses.activate
def test_sweep_keeps_brief_under_commit_threshold(tmp_path):
    _project_fixtures(head="def456")
    responses.get(f"{API}/projects/7/repository/compare",
                  json={"commits": [{"id": f"c{i}"} for i in range(5)]})
    cfg = _cfg(tmp_path)
    _write_brief(cfg, "g/app", "abc123", "old brief")
    with patch("forgeguard.brief.run_claude_brief") as rc:
        out = run_brief_sweep(GitLab(cfg), cfg)
    rc.assert_not_called()
    assert out["fresh"] == 1
    assert load_brief(cfg, "g/app") == "old brief"

@responses.activate
def test_sweep_regenerates_stale_brief(tmp_path):
    _project_fixtures(head="def456")
    responses.get(f"{API}/projects/7/repository/compare",
                  json={"commits": [{"id": f"c{i}"} for i in range(30)]})
    _snapshot_fixtures()
    cfg = _cfg(tmp_path)
    _write_brief(cfg, "g/app", "abc123", "old brief")
    with patch("forgeguard.brief.run_claude_brief", return_value="new brief"):
        out = run_brief_sweep(GitLab(cfg), cfg)
    assert out["generated"] == 1
    assert load_brief(cfg, "g/app").endswith("new brief")

@responses.activate
def test_sweep_failure_keeps_old_brief_and_counts(tmp_path):
    _project_fixtures(head="def456")
    responses.get(f"{API}/projects/7/repository/compare", status=404)  # rewritten
    _snapshot_fixtures()
    cfg = _cfg(tmp_path)
    _write_brief(cfg, "g/app", "abc123", "old brief")
    with patch("forgeguard.brief.run_claude_brief",
               side_effect=RuntimeError("claude died")):
        out = run_brief_sweep(GitLab(cfg), cfg)
    assert out["failed"] == 1
    assert load_brief(cfg, "g/app") == "old brief"

@responses.activate
def test_snapshot_bounds_files_and_preserves_coverage(tmp_path, monkeypatch):
    _project_fixtures()
    _snapshot_fixtures()
    monkeypatch.setattr(brief, "SNAPSHOT_MAX_FILES", 1)
    cfg = _cfg(tmp_path)
    def inspect(prompt, cwd):
        assert os.listdir(cwd) == ["main.py"]
        assert "Snapshot coverage" in prompt
        return "useful" * 2000
    with patch("forgeguard.brief.run_claude_brief", side_effect=inspect):
        assert run_brief_sweep(GitLab(cfg), cfg)["generated"] == 1
    text = load_brief(cfg, "g/app")
    assert text.startswith("Snapshot coverage")
    assert len(text) <= brief.BRIEF_CAP
    assert all("/repository/archive" not in urlsplit(call.request.url).path for call in responses.calls)
    assert all(parse_qs(urlsplit(call.request.url).query).get("ref") == ["abc123"]
               for call in responses.calls if "/repository/tree" in call.request.url or "/files/" in call.request.url)

@responses.activate
def test_empty_snapshot_does_not_replace_brief(tmp_path):
    _project_fixtures()
    responses.get(f"{API}/projects/7/repository/tree", json=[])
    cfg = _cfg(tmp_path)
    _write_brief(cfg, "g/app", "old-sha", "previous brief")
    responses.get(f"{API}/projects/7/repository/compare", status=404)
    with patch("forgeguard.brief.run_claude_brief") as provider:
        assert run_brief_sweep(GitLab(cfg), cfg)["failed"] == 1
    provider.assert_not_called()
    assert load_brief(cfg, "g/app") == "previous brief"

def test_snapshot_caps_requests_including_tree_pagination(tmp_path, monkeypatch):
    class Repository:
        requests = 0
        def get(self, endpoint, **params):
            self.requests += 1
            return [{"path": f"folder{params['page']}", "type": "tree"}]
    repository = Repository()
    monkeypatch.setattr(brief, "SNAPSHOT_MAX_REQUESTS", 3)
    monkeypatch.setattr(brief, "SNAPSHOT_PAGE_SIZE", 1)
    import pytest
    with pytest.raises(RuntimeError, match="no usable source"):
        brief._snapshot(repository, 7, "head", str(tmp_path))
    assert repository.requests == 3


def test_snapshot_caps_bytes_skips_oversize_and_unsafe_entries(tmp_path, monkeypatch):
    class Repository:
        paths = []
        def get(self, endpoint, **params):
            return [{"path": name, "type": kind, "mode": mode} for name, kind, mode in [
                ("../escape", "blob", "100644"), ("link", "blob", "120000"),
                ("sub", "commit", "160000"), ("a.txt", "blob", "100644"),
                ("b.py", "blob", "100644"), ("c.py", "blob", "100644"),
                ("d.py", "blob", "100644")]]
        def get_bytes_bounded(self, endpoint, cap, **params):
            self.paths.append((endpoint, cap))
            content = b"oversized" if "a.txt" in endpoint else b"code"
            return content if len(content) <= cap else None
    repository = Repository()
    monkeypatch.setattr(brief, "SNAPSHOT_FILE_BYTES", 4)
    monkeypatch.setattr(brief, "SNAPSHOT_MAX_BYTES", 6)
    note = brief._snapshot(repository, 7, "head", str(tmp_path))
    assert list(p.name for p in tmp_path.iterdir()) == ["b.py"]
    assert (tmp_path / "b.py").read_bytes() == b"code"
    assert [cap for endpoint, cap in repository.paths] == [4, 4, 2, 2]
    assert "1 files, 4 bytes" in note


def test_snapshot_stops_at_file_limit(tmp_path, monkeypatch):
    class Repository:
        paths = []
        def get(self, endpoint, **params):
            return [{"path": name, "type": "blob", "mode": "100644"}
                    for name in ["a.py", "b.py", "c.py"]]
        def get_bytes_bounded(self, endpoint, cap, **params):
            self.paths.append(endpoint)
            return b"code"
    repository = Repository()
    monkeypatch.setattr(brief, "SNAPSHOT_MAX_FILES", 2)
    brief._snapshot(repository, 7, "head", str(tmp_path))
    assert len(repository.paths) == 2
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a.py", "b.py"]
