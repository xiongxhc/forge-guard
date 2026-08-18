import json, responses
from urllib.parse import parse_qs
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
    assert out == {"reviewed": 1, "skipped_large": 0, "failed": 0}
    title, lines, at_user = fk.posts[0]
    assert title == "✅ Approved: feat: x"
    assert at_user == "alice"
    flat = [s for line in lines for s in line]
    hrefs = [s["href"] for s in flat if s.get("tag") == "a"]
    assert "https://gitlab.example.com/g/app/-/merge_requests/5" in hrefs
    assert "https://gitlab.example.com/g/app/-/commit/head1" in hrefs
    assert any(s.get("text") == "head commit: " for s in flat)
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
    assert out == {"reviewed": 0, "skipped_large": 1, "failed": 0}

@responses.activate
def test_failing_mr_skipped_cursor_held(tmp_path):
    responses.get(f"{API}/merge_requests", json=[
        {"iid": 5, "project_id": 7, "sha": "h1", "title": "bad", "description": "",
         "target_branch": "main", "author": {"username": "alice"},
         "updated_at": "2026-08-07T10:00:00Z",
         "web_url": "https://gitlab.internal.example/g/app/-/merge_requests/5"},
        {"iid": 6, "project_id": 7, "sha": "h2", "title": "good", "description": "",
         "target_branch": "main", "author": {"username": "bob"},
         "updated_at": "2026-08-07T11:00:00Z",
         "web_url": "https://gitlab.internal.example/g/app/-/merge_requests/6"}],
        headers={"X-Next-Page": ""})
    responses.get(f"{API}/projects/7", json={
        "path_with_namespace": "g/app",
        "web_url": "https://gitlab.internal.example/g/app"})
    responses.get(f"{API}/projects/7/merge_requests/5/changes",
                  json={"changes": [{"diff": "+ a"}]})
    responses.get(f"{API}/projects/7/merge_requests/6/changes",
                  json={"changes": [{"diff": "+ b"}]})
    responses.get(f"{API}/projects/7/merge_requests/6/notes", json=[],
                  headers={"X-Next-Page": ""})
    responses.post(f"{API}/projects/7/merge_requests/6/notes", json={"id": 3})
    responses.post(f"{API}/projects/7/merge_requests/6/approve", json={})
    cfg = load_config(dict(BASE, FORGEGUARD_STATE=str(tmp_path / "s.json")))
    st = State.load(cfg.state_path)
    verdict = {"verdict": "clean", "summary": "ok", "issues": [], "tests_opinion": "fine"}
    with patch("forgeguard.review.run_claude",
               side_effect=[RuntimeError("claude died"), verdict]):
        out = run_review_tick(GitLab(cfg), st, FakeFeishu(), cfg)
    assert out == {"reviewed": 1, "skipped_large": 0, "failed": 1}
    assert st.get_cursor("reviewed:7:6") == "h2"
    assert st.get_cursor("reviewed:7:5") is None
    assert st.get_cursor("mr_updated_after") is None

def test_run_claude_scrubs_credentials_from_child_env():
    fake = type("R", (), {"returncode": 0,
                          "stdout": '{"verdict":"clean","summary":"ok","issues":[],"tests_opinion":"fine"}',
                          "stderr": ""})()
    env = {"HOME": "/home/x", "PATH": "/usr/bin",
           "FORGEGUARD_GITLAB_TOKEN": "glpat-secret",
           "FORGEGUARD_FEISHU_APP_SECRET": "fs",
           "ANTHROPIC_API_KEY": "sk-x", "MY_PASSWORD": "p", "AWS_PAT": "a"}
    with patch.dict("os.environ", env, clear=True), \
         patch("forgeguard.review.subprocess.run", return_value=fake) as run:
        run_claude("x")
    child_env = run.call_args.kwargs["env"]
    assert child_env["HOME"] == "/home/x"
    assert child_env["PATH"] == "/usr/bin"
    for k in ("FORGEGUARD_GITLAB_TOKEN", "FORGEGUARD_FEISHU_APP_SECRET",
              "ANTHROPIC_API_KEY", "MY_PASSWORD", "AWS_PAT"):
        assert k not in child_env

@responses.activate
def test_failed_review_alerts_feishu_once_per_head(tmp_path):
    responses.get(f"{API}/merge_requests", json=[{
        "iid": 5, "project_id": 7, "sha": "h1", "title": "bad", "description": "",
        "target_branch": "main", "author": {"username": "alice"},
        "updated_at": "2026-08-07T10:00:00Z",
        "web_url": "https://gitlab.internal.example/g/app/-/merge_requests/5"}],
        headers={"X-Next-Page": ""})
    responses.get(f"{API}/projects/7", json={
        "path_with_namespace": "g/app",
        "web_url": "https://gitlab.internal.example/g/app"})
    responses.get(f"{API}/projects/7/merge_requests/5/changes",
                  json={"changes": [{"diff": "+ a"}]})
    cfg = load_config(dict(BASE, FORGEGUARD_STATE=str(tmp_path / "s.json")))
    st, fk = State.load(cfg.state_path), FakeFeishu()
    with patch("forgeguard.review.run_claude",
               side_effect=RuntimeError("claude died")):
        run_review_tick(GitLab(cfg), st, fk, cfg)
        run_review_tick(GitLab(cfg), st, fk, cfg)
    alerts = [t for t, _ in fk.sent if "review failed" in t]
    assert len(alerts) == 1
    assert "https://gitlab.example.com/g/app/-/merge_requests/5" in alerts[0]
    assert "claude died" in alerts[0]

def _big_mr(labels=None, sha="h2"):
    responses.get(f"{API}/merge_requests", json=[{
        "iid": 6, "project_id": 7, "sha": sha, "title": "big", "description": "",
        "target_branch": "main", "author": {"username": "bob"},
        "labels": labels or [], "updated_at": "2026-08-07T11:00:00Z",
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

@responses.activate
def test_oversized_diff_alerts_once_per_mr_with_label_hint(tmp_path):
    _big_mr()
    cfg = load_config(dict(BASE, FORGEGUARD_STATE=str(tmp_path / "s.json")))
    st, fk = State.load(cfg.state_path), FakeFeishu()
    out = run_review_tick(GitLab(cfg), st, fk, cfg)
    assert out == {"reviewed": 0, "skipped_large": 1, "failed": 0}
    title, lines, at_user = fk.posts[0]
    assert title.startswith("⚠️ Review skipped: big")
    assert at_user == "bob"
    flat = [s for line in lines for s in line]
    assert "https://gitlab.example.com/g/app/-/merge_requests/6" in [s.get("href") for s in flat]
    assert any("forge-guard:full-review" in s.get("text", "") for s in flat)
    body = parse_qs(responses.calls[-1].request.body)["body"][0]
    assert "forge-guard:full-review" in body
    # a new push to the same still-oversized MR: note refreshed, no second alert
    responses.reset(); _big_mr(sha="h3")
    out = run_review_tick(GitLab(cfg), st, fk, cfg)
    assert out == {"reviewed": 0, "skipped_large": 1, "failed": 0}
    assert len(fk.posts) == 1
    assert st.get_cursor("reviewed:7:6") == "h3"

@responses.activate
def test_full_review_label_raises_cap(tmp_path):
    _big_mr(labels=["forge-guard:full-review"])
    responses.post(f"{API}/projects/7/merge_requests/6/approve", json={})
    cfg = load_config(dict(BASE, FORGEGUARD_STATE=str(tmp_path / "s.json")))
    fk = FakeFeishu()
    verdict = {"verdict": "clean", "summary": "ok", "issues": [], "tests_opinion": "fine"}
    with patch("forgeguard.review.run_claude", return_value=verdict) as rc:
        out = run_review_tick(GitLab(cfg), State.load(cfg.state_path), fk, cfg)
    assert out == {"reviewed": 1, "skipped_large": 0, "failed": 0}
    assert "x" * 400_000 in rc.call_args.args[0]
    assert fk.posts[0][0] == "✅ Approved: big"

@responses.activate
def test_full_review_label_still_over_hard_cap(tmp_path):
    _big_mr(labels=["forge-guard:full-review"])
    cfg = load_config(dict(BASE, FORGEGUARD_STATE=str(tmp_path / "s.json"),
                           FORGEGUARD_DIFF_CAP_FULL="350000"))
    fk = FakeFeishu()
    with patch("forgeguard.review.run_claude") as rc:
        out = run_review_tick(GitLab(cfg), State.load(cfg.state_path), fk, cfg)
    assert out == {"reviewed": 0, "skipped_large": 1, "failed": 0}
    rc.assert_not_called()
    body = parse_qs(responses.calls[-1].request.body)["body"][0]
    assert "even with" in body and "350000" in body

def _truncated_mr(existing_note=None):
    responses.get(f"{API}/merge_requests", json=[{
        "iid": 8, "project_id": 7, "sha": "h8", "title": "big feature", "description": "",
        "target_branch": "main", "author": {"username": "bob"}, "labels": [],
        "updated_at": "2026-08-07T11:00:00Z",
        "web_url": "https://gitlab.internal.example/g/app/-/merge_requests/8"}],
        headers={"X-Next-Page": ""})
    responses.get(f"{API}/projects/7", json={
        "path_with_namespace": "g/app",
        "web_url": "https://gitlab.internal.example/g/app"})
    responses.get(f"{API}/projects/7/merge_requests/8/changes", json={
        "changes_count": "3+",
        "changes": [{"new_path": "docs/a.md", "diff": "+ docs"},
                    {"new_path": "src/Main.java", "diff": ""},
                    {"new_path": "old.txt", "diff": "", "renamed_file": True}]})
    responses.get(f"{API}/projects/7/merge_requests/8/notes",
                  json=[existing_note] if existing_note else [], headers={"X-Next-Page": ""})

@responses.activate
def test_truncated_diff_not_reviewed_and_alerted_once(tmp_path):
    _truncated_mr()
    cfg = load_config(dict(BASE, FORGEGUARD_STATE=str(tmp_path / "s.json")))
    st, fk = State.load(cfg.state_path), FakeFeishu()
    with patch("forgeguard.review.run_claude") as rc:
        out = run_review_tick(GitLab(cfg), st, fk, cfg)
    assert out == {"reviewed": 0, "skipped_large": 1, "failed": 0}
    rc.assert_not_called()
    assert not any("/notes" in c.request.url and c.request.method == "POST" for c in responses.calls)
    title, lines, at_user = fk.posts[0]
    assert title.startswith("⚠️ Review skipped: big feature") and "truncated" in title
    flat = [s for line in lines for s in line]
    assert any("1 of 3" in s.get("text", "") for s in flat)
    assert st.get_cursor("reviewed:7:8") == "h8"
    responses.reset(); _truncated_mr()
    with patch("forgeguard.review.run_claude"):
        run_review_tick(GitLab(cfg), st, fk, cfg)
    assert len(fk.posts) == 1

@responses.activate
def test_truncated_diff_replaces_stale_review_note(tmp_path):
    _truncated_mr(existing_note={"id": 42, "body": "<!-- forge-guard-review -->\n**forge-guard review — issues**\nold"})
    responses.put(f"{API}/projects/7/merge_requests/8/notes/42", json={"id": 42})
    cfg = load_config(dict(BASE, FORGEGUARD_STATE=str(tmp_path / "s.json")))
    with patch("forgeguard.review.run_claude"):
        run_review_tick(GitLab(cfg), State.load(cfg.state_path), FakeFeishu(), cfg)
    puts = [c for c in responses.calls if c.request.method == "PUT"]
    assert len(puts) == 1
    body = parse_qs(puts[0].request.body)["body"][0]
    assert "not reviewed" in body and "truncated" in body and "1 of 3" in body
