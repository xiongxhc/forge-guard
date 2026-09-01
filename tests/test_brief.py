import io, os, tarfile
import responses
from unittest.mock import patch
from forgeguard.config import load_config
from forgeguard.gitlab import GitLab
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

def _archive_bytes():
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = b"print('hi')\n"
        info = tarfile.TarInfo("app-abc123/main.py")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()

def _project_fixtures(head="abc123"):
    responses.get(f"{API}/projects", json=[{
        "id": 7, "path_with_namespace": "g/app", "default_branch": "main"}],
        headers={"X-Next-Page": ""})
    responses.get(f"{API}/projects/7/repository/branches/main",
                  json={"commit": {"id": head}})

def test_load_brief_strips_marker(tmp_path):
    cfg = _cfg(tmp_path)
    _write_brief(cfg, "g/app", "abc123", "Django service.\nRoutes need auth.")
    assert load_brief(cfg, "g/app") == "Django service.\nRoutes need auth."
    assert load_brief(cfg, "g/other") is None

@responses.activate
def test_sweep_generates_missing_brief(tmp_path):
    _project_fixtures()
    responses.get(f"{API}/projects/7/repository/archive.tar.gz",
                  body=_archive_bytes())
    cfg = _cfg(tmp_path)
    with patch("forgeguard.brief.run_claude_brief", return_value="A Django app.") as rc:
        out = run_brief_sweep(GitLab(cfg), cfg)
    assert out == {"projects": 1, "generated": 1, "fresh": 0, "failed": 0}
    assert rc.call_args.kwargs["cwd"].endswith("app-abc123")
    assert load_brief(cfg, "g/app") == "A Django app."
    with open(_brief_path(cfg, "g/app")) as f:
        assert f.readline().strip() == "<!-- forge-guard-brief abc123 -->"

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
    responses.get(f"{API}/projects/7/repository/archive.tar.gz",
                  body=_archive_bytes())
    cfg = _cfg(tmp_path)
    _write_brief(cfg, "g/app", "abc123", "old brief")
    with patch("forgeguard.brief.run_claude_brief", return_value="new brief"):
        out = run_brief_sweep(GitLab(cfg), cfg)
    assert out["generated"] == 1
    assert load_brief(cfg, "g/app") == "new brief"

@responses.activate
def test_sweep_failure_keeps_old_brief_and_counts(tmp_path):
    _project_fixtures(head="def456")
    responses.get(f"{API}/projects/7/repository/compare", status=404)  # rewritten
    responses.get(f"{API}/projects/7/repository/archive.tar.gz",
                  body=_archive_bytes())
    cfg = _cfg(tmp_path)
    _write_brief(cfg, "g/app", "abc123", "old brief")
    with patch("forgeguard.brief.run_claude_brief",
               side_effect=RuntimeError("claude died")):
        out = run_brief_sweep(GitLab(cfg), cfg)
    assert out["failed"] == 1
    assert load_brief(cfg, "g/app") == "old brief"
