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
    assert out == {"reviewed": 1, "skipped_large": 0, "failed": 0, "merged_unreviewed": 0}
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
    assert out == {"reviewed": 0, "skipped_large": 1, "failed": 0, "merged_unreviewed": 0}

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
    assert out == {"reviewed": 1, "skipped_large": 0, "failed": 1, "merged_unreviewed": 0}
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
    import tempfile
    assert run.call_args.kwargs["cwd"] == tempfile.gettempdir()
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
    assert out == {"reviewed": 0, "skipped_large": 1, "failed": 0, "merged_unreviewed": 0}
    title, lines, at_user = fk.posts[0]
    assert title.startswith("⚠️ Review skipped: big")
    assert at_user == "bob"
    flat = [s for line in lines for s in line]
    assert "https://gitlab.example.com/g/app/-/merge_requests/6" in [s.get("href") for s in flat]
    assert any("How to get it reviewed" in s.get("text", "") and "forge-guard:full-review" in s.get("text", "") for s in flat)
    body = parse_qs([c for c in responses.calls if "/notes" in c.request.url and c.request.body][-1].request.body)["body"][0]
    assert "forge-guard:full-review" in body
    # a new push to the same still-oversized MR: note refreshed, no second alert
    responses.reset(); _big_mr(sha="h3")
    out = run_review_tick(GitLab(cfg), st, fk, cfg)
    assert out == {"reviewed": 0, "skipped_large": 1, "failed": 0, "merged_unreviewed": 0}
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
    assert out == {"reviewed": 1, "skipped_large": 0, "failed": 0, "merged_unreviewed": 0}
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
    assert out == {"reviewed": 0, "skipped_large": 1, "failed": 0, "merged_unreviewed": 0}
    rc.assert_not_called()
    body = parse_qs([c for c in responses.calls if "/notes" in c.request.url and c.request.body][-1].request.body)["body"][0]
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
    assert out == {"reviewed": 0, "skipped_large": 1, "failed": 0, "merged_unreviewed": 0}
    rc.assert_not_called()
    assert not any("/notes" in c.request.url and c.request.method == "POST" for c in responses.calls)
    title, lines, at_user = fk.posts[0]
    assert title.startswith("⚠️ Review skipped: big feature") and "truncated" in title
    flat = [s for line in lines for s in line]
    assert any("1 of 3" in s.get("text", "") for s in flat)
    assert any("How to get it reviewed" in s.get("text", "") and "split the MR" in s.get("text", "") for s in flat)
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

@responses.activate
def test_blank_file_diffs_without_overflow_reviewed_normally(tmp_path):
    responses.get(f"{API}/merge_requests", json=[{
        "iid": 9, "project_id": 7, "sha": "h9", "title": "pdf feature", "description": "",
        "target_branch": "main", "author": {"username": "dd"}, "labels": [],
        "updated_at": "2026-08-07T11:00:00Z",
        "web_url": "https://gitlab.internal.example/g/app/-/merge_requests/9"}],
        headers={"X-Next-Page": ""})
    responses.get(f"{API}/projects/7", json={
        "path_with_namespace": "g/app",
        "web_url": "https://gitlab.internal.example/g/app"})
    responses.get(f"{API}/projects/7/merge_requests/9/changes", json={
        "changes_count": 3, "overflow": False,
        "changes": [{"new_path": "src/a.ts", "diff": "+ code"},
                    {"new_path": "fixture.pdf", "diff": ""},
                    {"new_path": "package-lock.json", "diff": "", "collapsed": True}]})
    responses.get(f"{API}/projects/7/merge_requests/9/notes", json=[],
                  headers={"X-Next-Page": ""})
    responses.post(f"{API}/projects/7/merge_requests/9/notes", json={"id": 11})
    responses.post(f"{API}/projects/7/merge_requests/9/approve", json={})
    cfg = load_config(dict(BASE, FORGEGUARD_STATE=str(tmp_path / "s.json")))
    fk = FakeFeishu()
    verdict = {"verdict": "clean", "summary": "ok", "issues": [], "tests_opinion": "fine"}
    with patch("forgeguard.review.run_claude", return_value=verdict):
        out = run_review_tick(GitLab(cfg), State.load(cfg.state_path), fk, cfg)
    assert out == {"reviewed": 1, "skipped_large": 0, "failed": 0, "merged_unreviewed": 0}
    assert fk.posts[0][0] == "✅ Approved: pdf feature"

@responses.activate
def test_review_covers_glob_matched_target_branch(tmp_path):
    responses.get(f"{API}/merge_requests", json=[{
        "iid": 12, "project_id": 7, "sha": "h12", "title": "dev to uat", "description": "",
        "target_branch": "adaa/uat", "author": {"username": "ws"}, "labels": [],
        "updated_at": "2026-08-07T11:00:00Z",
        "web_url": "https://gitlab.internal.example/g/app/-/merge_requests/12"}],
        headers={"X-Next-Page": ""})
    responses.get(f"{API}/projects/7", json={
        "path_with_namespace": "g/app",
        "web_url": "https://gitlab.internal.example/g/app"})
    responses.get(f"{API}/projects/7/merge_requests/12/changes",
                  json={"changes_count": 1, "changes": [{"new_path": "a", "diff": "+ x"}]})
    responses.get(f"{API}/projects/7/merge_requests/12/notes", json=[],
                  headers={"X-Next-Page": ""})
    responses.post(f"{API}/projects/7/merge_requests/12/notes", json={"id": 13})
    responses.post(f"{API}/projects/7/merge_requests/12/approve", json={})
    cfg = load_config(dict(BASE, FORGEGUARD_STATE=str(tmp_path / "s.json"),
                           FORGEGUARD_BRANCHES="dev,uat,*/uat"))
    verdict = {"verdict": "clean", "summary": "ok", "issues": [], "tests_opinion": "fine"}
    with patch("forgeguard.review.run_claude", return_value=verdict):
        out = run_review_tick(GitLab(cfg), State.load(cfg.state_path), FakeFeishu(), cfg)
    assert out == {"reviewed": 1, "skipped_large": 0, "failed": 0, "merged_unreviewed": 0}

@responses.activate
def test_review_branch_list_reviews_release_mr(tmp_path):
    responses.get(f"{API}/merge_requests", json=[{
        "iid": 21, "project_id": 7, "sha": "h21", "title": "to release", "description": "",
        "target_branch": "release/2.1.0", "author": {"username": "ws"}, "labels": [],
        "updated_at": "2026-08-07T11:00:00Z",
        "web_url": "https://gitlab.internal.example/g/app/-/merge_requests/21"}],
        headers={"X-Next-Page": ""})
    responses.get(f"{API}/projects/7", json={
        "path_with_namespace": "g/app",
        "web_url": "https://gitlab.internal.example/g/app"})
    responses.get(f"{API}/projects/7/merge_requests/21/changes",
                  json={"changes_count": 1, "changes": [{"new_path": "a", "diff": "+ x"}]})
    responses.get(f"{API}/projects/7/merge_requests/21/notes", json=[],
                  headers={"X-Next-Page": ""})
    responses.post(f"{API}/projects/7/merge_requests/21/notes", json={"id": 1})
    responses.post(f"{API}/projects/7/merge_requests/21/approve", json={})
    cfg = load_config(dict(BASE, FORGEGUARD_STATE=str(tmp_path / "s.json"),
                           FORGEGUARD_REVIEW_BRANCHES="release/*"))
    verdict = {"verdict": "clean", "summary": "ok", "issues": [], "tests_opinion": "fine"}
    with patch("forgeguard.review.run_claude", return_value=verdict):
        out = run_review_tick(GitLab(cfg), State.load(cfg.state_path), FakeFeishu(), cfg)
    assert out["reviewed"] == 1

def _merged_mr_fixture(sha="m1"):
    responses.get(f"{API}/merge_requests",
                  json=[], headers={"X-Next-Page": ""},
                  match=[responses.matchers.query_param_matcher({"state": "opened"}, strict_match=False)])
    responses.get(f"{API}/merge_requests", json=[{
        "iid": 30, "project_id": 7, "sha": sha, "title": "fast merge", "description": "",
        "target_branch": "dev", "author": {"username": "spd"}, "labels": [],
        "updated_at": "2026-08-07T12:00:00Z",
        "web_url": "https://gitlab.internal.example/g/app/-/merge_requests/30"}],
        headers={"X-Next-Page": ""},
        match=[responses.matchers.query_param_matcher({"state": "merged"}, strict_match=False)])
    responses.get(f"{API}/projects/7", json={
        "path_with_namespace": "g/app",
        "web_url": "https://gitlab.internal.example/g/app"})

@responses.activate
def test_merged_without_review_alerts_once(tmp_path):
    _merged_mr_fixture()
    cfg = load_config(dict(BASE, FORGEGUARD_STATE=str(tmp_path / "s.json")))
    st, fk = State.load(cfg.state_path), FakeFeishu()
    st.set_cursor("mr_updated_after", "2026-08-07T00:00:00Z")
    out = run_review_tick(GitLab(cfg), st, fk, cfg)
    assert out["merged_unreviewed"] == 1
    title, lines, at_user = fk.posts[0]
    assert title.startswith("⚠️ Merged without review: fast merge")
    assert at_user == "spd"
    flat = [s for line in lines for s in line]
    assert "https://gitlab.example.com/g/app/-/merge_requests/30" in [s.get("href") for s in flat]
    responses.reset(); _merged_mr_fixture()
    out = run_review_tick(GitLab(cfg), st, fk, cfg)
    assert len(fk.posts) == 1                      # deduped

@responses.activate
def test_merged_with_review_not_alerted(tmp_path):
    _merged_mr_fixture()
    cfg = load_config(dict(BASE, FORGEGUARD_STATE=str(tmp_path / "s.json")))
    st, fk = State.load(cfg.state_path), FakeFeishu()
    st.set_cursor("mr_updated_after", "2026-08-07T00:00:00Z")
    st.set_cursor("reviewed:7:30", "m1")           # we reviewed this head
    out = run_review_tick(GitLab(cfg), st, fk, cfg)
    assert out["merged_unreviewed"] == 0
    assert fk.posts == []

@responses.activate
def test_merged_check_baselines_without_cursor(tmp_path):
    responses.get(f"{API}/merge_requests", json=[], headers={"X-Next-Page": ""})
    cfg = load_config(dict(BASE, FORGEGUARD_STATE=str(tmp_path / "s.json")))
    st, fk = State.load(cfg.state_path), FakeFeishu()
    out = run_review_tick(GitLab(cfg), st, fk, cfg)   # no cursors at all yet
    assert out["merged_unreviewed"] == 0
    assert fk.posts == [] and fk.sent == []

def _merged_mrs_fixture(n):
    responses.get(f"{API}/merge_requests",
                  json=[], headers={"X-Next-Page": ""},
                  match=[responses.matchers.query_param_matcher({"state": "opened"}, strict_match=False)])
    responses.get(f"{API}/merge_requests", json=[{
        "iid": 30 + i, "project_id": 7, "sha": f"m{i}", "title": f"fast merge {i}",
        "description": "", "target_branch": "dev",
        "author": {"username": f"spd{i}"}, "labels": [],
        "updated_at": "2026-08-07T12:00:00Z",
        "web_url": f"https://gitlab.internal.example/g/app/-/merge_requests/{30 + i}"}
        for i in range(n)],
        headers={"X-Next-Page": ""},
        match=[responses.matchers.query_param_matcher({"state": "merged"}, strict_match=False)])
    responses.get(f"{API}/projects/7", json={
        "path_with_namespace": "g/app",
        "web_url": "https://gitlab.internal.example/g/app"})

@responses.activate
def test_merged_without_review_batches_over_three(tmp_path):
    _merged_mrs_fixture(4)
    cfg = load_config(dict(BASE, FORGEGUARD_STATE=str(tmp_path / "s.json")))
    st, fk = State.load(cfg.state_path), FakeFeishu()
    fk.usermap = {"spd0": "ou_1"}
    st.set_cursor("mr_updated_after", "2026-08-07T00:00:00Z")
    out = run_review_tick(GitLab(cfg), st, fk, cfg)
    assert out["merged_unreviewed"] == 4
    assert len(fk.posts) == 1                      # one combined message
    title, lines, at_user = fk.posts[0]
    assert title == "⚠️ Merged without review: 4 MRs"
    assert len(lines) == 4
    flat = [s for line in lines for s in line]
    links = [s for s in flat if s.get("tag") == "a"]
    hrefs = [s["href"] for s in links]
    for iid in (30, 31, 32, 33):
        assert f"https://gitlab.example.com/g/app/-/merge_requests/{iid}" in hrefs
    assert links[0]["text"] == "g/app!30"          # project disambiguates rows
    assert any(s.get("text", "").endswith("fast merge 2") for s in flat)
    assert {"tag": "at", "user_id": "ou_1"} in flat      # mapped author @-tagged
    assert any(s.get("text") == "@spd1" for s in flat)   # unmapped falls back
    responses.reset(); _merged_mrs_fixture(4)
    run_review_tick(GitLab(cfg), st, fk, cfg)
    assert len(fk.posts) == 1                      # deduped on second tick

@responses.activate
def test_merged_without_review_batch_caps_at_fifteen_rows(tmp_path):
    _merged_mrs_fixture(17)
    cfg = load_config(dict(BASE, FORGEGUARD_STATE=str(tmp_path / "s.json")))
    st, fk = State.load(cfg.state_path), FakeFeishu()
    st.set_cursor("mr_updated_after", "2026-08-07T00:00:00Z")
    out = run_review_tick(GitLab(cfg), st, fk, cfg)
    assert out["merged_unreviewed"] == 17
    assert len(fk.posts) == 1
    title, lines, _ = fk.posts[0]
    assert title == "⚠️ Merged without review: 17 MRs"
    assert len(lines) == 16                        # 15 rows + overflow line
    assert lines[-1] == [{"tag": "text", "text": "…and 2 more"}]

@responses.activate
def test_merged_without_review_three_or_fewer_stays_individual(tmp_path):
    _merged_mrs_fixture(3)
    cfg = load_config(dict(BASE, FORGEGUARD_STATE=str(tmp_path / "s.json")))
    st, fk = State.load(cfg.state_path), FakeFeishu()
    st.set_cursor("mr_updated_after", "2026-08-07T00:00:00Z")
    out = run_review_tick(GitLab(cfg), st, fk, cfg)
    assert out["merged_unreviewed"] == 3
    assert len(fk.posts) == 3
    assert all(t.startswith("⚠️ Merged without review: fast merge") for t, _, _ in fk.posts)

@responses.activate
def test_failed_alert_post_leaves_batch_unflagged_for_retry(tmp_path):
    import pytest
    class FailingFeishu(FakeFeishu):
        def notify_post(self, *a, **k): raise RuntimeError("feishu down")
    _merged_mrs_fixture(4)
    cfg = load_config(dict(BASE, FORGEGUARD_STATE=str(tmp_path / "s.json")))
    st = State.load(cfg.state_path)
    st.set_cursor("mr_updated_after", "2026-08-07T00:00:00Z")
    with pytest.raises(RuntimeError):
        run_review_tick(GitLab(cfg), st, FailingFeishu(), cfg)
    st2 = State.load(cfg.state_path)              # finally-saved state from failed tick
    responses.reset(); _merged_mrs_fixture(4)
    fk = FakeFeishu()
    out = run_review_tick(GitLab(cfg), st2, fk, cfg)
    assert out["merged_unreviewed"] == 4
    assert len(fk.posts) == 1                     # batch retried, nothing lost
    assert fk.posts[0][0] == "⚠️ Merged without review: 4 MRs"

def _ctx_mr():
    responses.get(f"{API}/merge_requests", json=[{
        "iid": 40, "project_id": 7, "sha": "h40", "title": "ctx", "description": "",
        "target_branch": "main", "author": {"username": "cx"}, "labels": [],
        "updated_at": "2026-09-01T10:00:00Z",
        "web_url": "https://gitlab.internal.example/g/app/-/merge_requests/40"}],
        headers={"X-Next-Page": ""})
    responses.get(f"{API}/projects/7", json={
        "path_with_namespace": "g/app",
        "web_url": "https://gitlab.internal.example/g/app"})
    responses.get(f"{API}/projects/7/merge_requests/40/changes", json={
        "changes_count": 2,
        "changes": [{"new_path": "src/app.py", "diff": "+ handler()"},
                    {"new_path": "gone.py", "diff": "- old", "deleted_file": True}]})
    responses.get(f"{API}/projects/7/merge_requests/40/notes", json=[],
                  headers={"X-Next-Page": ""})
    responses.post(f"{API}/projects/7/merge_requests/40/notes", json={"id": 9})
    responses.post(f"{API}/projects/7/merge_requests/40/approve", json={})

@responses.activate
def test_files_mode_injects_rules_and_file_content(tmp_path):
    _ctx_mr()
    responses.get(f"{API}/projects/7/repository/files/.forgeguard.md/raw",
                  body="No new endpoints without permission middleware.")
    responses.get(f"{API}/projects/7/repository/files/src%2Fapp.py/raw",
                  body="def handler(): pass")
    cfg = load_config(dict(BASE, FORGEGUARD_STATE=str(tmp_path / "s.json")))
    assert cfg.review_mode == "files"
    verdict = {"verdict": "clean", "summary": "ok", "issues": [], "tests_opinion": "fine"}
    with patch("forgeguard.review.run_claude", return_value=verdict) as rc:
        out = run_review_tick(GitLab(cfg), State.load(cfg.state_path), FakeFeishu(), cfg)
    assert out["reviewed"] == 1
    prompt = rc.call_args.args[0]
    assert "Project review rules (.forgeguard.md" in prompt
    assert "No new endpoints without permission middleware." in prompt
    assert "==== src/app.py ====\ndef handler(): pass" in prompt
    assert "gone.py ====" not in prompt                 # deleted file not fetched
    raw = [c.request.url for c in responses.calls if "/repository/files/" in c.request.url]
    assert not any("gone.py" in u for u in raw)

@responses.activate
def test_files_mode_claude_md_fallback_and_skeptic_gets_context(tmp_path):
    _ctx_mr()
    responses.get(f"{API}/projects/7/repository/files/.forgeguard.md/raw",
                  status=404)
    responses.get(f"{API}/projects/7/repository/files/CLAUDE.md/raw",
                  body="Use the centralized API client.")
    responses.get(f"{API}/projects/7/repository/files/src%2Fapp.py/raw",
                  body="def handler(): pass")
    cfg = load_config(dict(BASE, FORGEGUARD_STATE=str(tmp_path / "s.json")))
    bad = {"verdict": "issues", "summary": "s",
           "issues": [{"severity": "high", "file": "src/app.py", "note": "n"}],
           "tests_opinion": "f"}
    clean = {"verdict": "clean", "summary": "ok", "issues": [], "tests_opinion": "fine"}
    with patch("forgeguard.review.run_claude", side_effect=[bad, clean]) as rc:
        run_review_tick(GitLab(cfg), State.load(cfg.state_path), FakeFeishu(), cfg)
    first, skeptic = rc.call_args_list[0].args[0], rc.call_args_list[1].args[0]
    for prompt in (first, skeptic):
        assert "Project review rules (CLAUDE.md" in prompt
        assert "Use the centralized API client." in prompt
        assert "==== src/app.py ====" in prompt

@responses.activate
def test_diff_mode_fetches_no_context(tmp_path):
    _ctx_mr()
    cfg = load_config(dict(BASE, FORGEGUARD_STATE=str(tmp_path / "s.json"),
                           FORGEGUARD_REVIEW_MODE="diff"))
    verdict = {"verdict": "clean", "summary": "ok", "issues": [], "tests_opinion": "fine"}
    with patch("forgeguard.review.run_claude", return_value=verdict) as rc:
        out = run_review_tick(GitLab(cfg), State.load(cfg.state_path), FakeFeishu(), cfg)
    assert out["reviewed"] == 1
    assert not any("/repository/files/" in c.request.url for c in responses.calls)
    assert "Project review rules" not in rc.call_args.args[0]

@responses.activate
def test_files_mode_degrades_when_context_fetches_fail(tmp_path):
    _ctx_mr()
    responses.get(f"{API}/projects/7/repository/files/.forgeguard.md/raw", status=500)
    responses.get(f"{API}/projects/7/repository/files/CLAUDE.md/raw", status=404)
    responses.get(f"{API}/projects/7/repository/files/src%2Fapp.py/raw", status=500)
    cfg = load_config(dict(BASE, FORGEGUARD_STATE=str(tmp_path / "s.json")))
    verdict = {"verdict": "clean", "summary": "ok", "issues": [], "tests_opinion": "fine"}
    with patch("forgeguard.review.run_claude", return_value=verdict) as rc:
        out = run_review_tick(GitLab(cfg), State.load(cfg.state_path), FakeFeishu(), cfg)
    assert out == {"reviewed": 1, "skipped_large": 0, "failed": 0, "merged_unreviewed": 0}
    assert "Project review rules" not in rc.call_args.args[0]
    assert "+ handler()" in rc.call_args.args[0]        # diff still reviewed

@responses.activate
def test_files_mode_omits_oversized_file_content(tmp_path):
    _ctx_mr()
    responses.get(f"{API}/projects/7/repository/files/src%2Fapp.py/raw",
                  body="y" * 100_001)
    cfg = load_config(dict(BASE, FORGEGUARD_STATE=str(tmp_path / "s.json")))
    verdict = {"verdict": "clean", "summary": "ok", "issues": [], "tests_opinion": "fine"}
    with patch("forgeguard.review.run_claude", return_value=verdict) as rc:
        run_review_tick(GitLab(cfg), State.load(cfg.state_path), FakeFeishu(), cfg)
    prompt = rc.call_args.args[0]
    assert "omitted for size" in prompt and "src/app.py" in prompt
    assert "y" * 1000 not in prompt
