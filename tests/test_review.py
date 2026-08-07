import json, responses
from unittest.mock import patch
from forgeguard.config import load_config
from forgeguard.gitlab import GitLab
from forgeguard.state import State
from forgeguard.review import run_claude, run_review_tick
from tests.test_config import BASE
from tests.test_cli import FakeFeishu

API = "https://gitlab.example.com/api/v4"

def test_run_claude_parses_json_from_noise():
    fake = type("R", (), {"returncode": 0,
                          "stdout": 'note\n{"verdict":"clean","summary":"ok","issues":[],"tests_opinion":"fine"}\n',
                          "stderr": ""})()
    with patch("forgeguard.review.subprocess.run", return_value=fake):
        assert run_claude("x")["verdict"] == "clean"

@responses.activate
def test_review_tick_posts_note_approves_and_notifies(tmp_path):
    responses.get(f"{API}/merge_requests", json=[{
        "iid": 5, "project_id": 7, "sha": "head1", "title": "feat: x",
        "description": "", "target_branch": "develop",
        "author": {"username": "alice"}, "updated_at": "2026-08-07T10:00:00Z",
        "web_url": "https://gitlab.internal.example/g/app/-/merge_requests/5"}],
        headers={"X-Next-Page": ""})
    responses.get(f"{API}/projects/7", json={
        "path_with_namespace": "g/app",
        "web_url": "https://gitlab.internal.example/g/app"})
    responses.get(f"{API}/projects/7/merge_requests/5/changes",
                  json={"changes": [{"diff": "+ hello"}]})
    responses.get(f"{API}/projects/7/merge_requests/5/notes", json=[],
                  headers={"X-Next-Page": ""})
    responses.post(f"{API}/projects/7/merge_requests/5/notes", json={"id": 1})
    responses.post(f"{API}/projects/7/merge_requests/5/approve", json={})
    cfg = load_config(dict(BASE, FORGEGUARD_STATE=str(tmp_path / "s.json")))
    st, fk = State.load(cfg.state_path), FakeFeishu()
    verdict = {"verdict": "clean", "summary": "ok", "issues": [], "tests_opinion": "has tests"}
    with patch("forgeguard.review.run_claude", return_value=verdict):
        out = run_review_tick(GitLab(cfg), st, fk, cfg)
    assert out == {"reviewed": 1, "skipped_large": 0}
    assert fk.sent[0][1] == "alice"
    assert "https://gitlab.example.com/g/app/-/merge_requests/5" in fk.sent[0][0]
    assert "https://gitlab.example.com/g/app/-/commit/head1" in fk.sent[0][0]
    assert st.get_cursor("reviewed:7:5") == "head1"
    assert st.get_cursor("mr_updated_after") == "2026-08-07T10:00:00Z"

@responses.activate
def test_oversized_diff_skipped(tmp_path):
    responses.get(f"{API}/merge_requests", json=[{
        "iid": 6, "project_id": 7, "sha": "h2", "title": "big", "description": "",
        "target_branch": "main", "author": {"username": "bob"},
        "updated_at": "2026-08-07T11:00:00Z",
        "web_url": "https://gitlab.internal.example/g/app/-/merge_requests/6"}],
        headers={"X-Next-Page": ""})
    responses.get(f"{API}/projects/7", json={
        "path_with_namespace": "g/app",
        "web_url": "https://gitlab.internal.example/g/app"})
    responses.get(f"{API}/projects/7/merge_requests/6/changes",
                  json={"changes": [{"diff": "x" * 400_000}]})
    responses.get(f"{API}/projects/7/merge_requests/6/notes", json=[],
                  headers={"X-Next-Page": ""})
    responses.post(f"{API}/projects/7/merge_requests/6/notes", json={"id": 2})
    cfg = load_config(dict(BASE, FORGEGUARD_STATE=str(tmp_path / "s.json")))
    out = run_review_tick(GitLab(cfg), State.load(cfg.state_path), FakeFeishu(), cfg)
    assert out == {"reviewed": 0, "skipped_large": 1}
