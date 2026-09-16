import json, time, pytest, responses
from pathlib import Path
from urllib.parse import parse_qs
from unittest.mock import patch
from forgeguard.config import load_config
from forgeguard.gitlab import GitLab
from forgeguard.state import State
from forgeguard import review
from forgeguard.review import ClaudeLimit, run_claude, run_review_tick
from tests.test_config import BASE
from tests.test_cli import FakeFeishu

API = "https://gitlab.example.com/api/v4"

CLEAN = '{"verdict":"clean","summary":"ok","issues":[],"tests_opinion":"fine"}'
RATE = {"status": "allowed_warning", "resetsAt": 1788348600,
        "rateLimitType": "five_hour", "utilization": 0.98}

def _stream(result_text, rate=None, is_error=False):
    # claude -p --output-format stream-json: one JSON object per line.
    lines = [json.dumps({"type": "system", "subtype": "init"})]
    if rate:
        lines.append(json.dumps({"type": "rate_limit_event", "rate_limit_info": rate}))
    lines.append(json.dumps({"type": "result", "is_error": is_error,
                             "subtype": "error" if is_error else "success",
                             "result": result_text}))
    return "\n".join(lines) + "\n"

def _proc(stdout, returncode=0, stderr=""):
    return type("R", (), {"returncode": returncode, "stdout": stdout, "stderr": stderr})()

def test_run_claude_parses_json_from_noise():
    with patch("forgeguard.review.subprocess.run", return_value=_proc(_stream("note\n" + CLEAN))):
        assert run_claude("x")["verdict"] == "clean"

def test_run_claude_records_latest_rate_limit_info():
    review.RATE_LIMIT.clear()
    with patch("forgeguard.review.subprocess.run", return_value=_proc(_stream(CLEAN, RATE))):
        run_claude("x")
    assert review.RATE_LIMIT == RATE

def test_run_claude_raises_limit_with_reset_time():
    rate = dict(RATE, status="rejected", utilization=1.0)
    fake = _proc(_stream("You've hit your usage limit.", rate, is_error=True), returncode=1)
    with patch("forgeguard.review.subprocess.run", return_value=fake):
        with pytest.raises(ClaudeLimit) as e:
            run_claude("x")
    assert e.value.resets_at == 1788348600
    assert e.value.window == "five_hour"

@pytest.mark.parametrize("text", [
    "Claude AI usage limit reached",
    "You've hit your session limit · resets 3:30pm (Asia/Dubai)",   # seen on the box 2026-09-02
])
def test_run_claude_raises_limit_from_error_text_without_event(text):
    fake = _proc(_stream(text, is_error=True), returncode=0)
    with patch("forgeguard.review.subprocess.run", return_value=fake):
        with pytest.raises(ClaudeLimit) as e:
            run_claude("x")
    assert e.value.resets_at is None
    assert e.value.detail == text

def test_run_claude_timeout_is_a_runtime_error_not_a_crash():
    import subprocess
    with patch("forgeguard.review.subprocess.run",
               side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=300)):
        with pytest.raises(RuntimeError, match="timed out"):
            run_claude("x")

def test_limit_message_falls_back_to_claude_wording(tmp_path):
    cfg = load_config(dict(BASE, FORGEGUARD_STATE=str(tmp_path / "s.json")))
    st, fk = State.load(cfg.state_path), FakeFeishu()
    with responses.RequestsMock() as rs:
        rs.get(f"{API}/merge_requests", json=[{
            "iid": 5, "project_id": 7, "sha": "h1", "title": "one", "description": "",
            "target_branch": "main", "author": {"username": "alice"},
            "updated_at": "2026-08-07T10:00:00Z",
            "web_url": "https://gitlab.internal.example/g/app/-/merge_requests/5"}],
            headers={"X-Next-Page": ""})
        rs.get(f"{API}/projects/7", json={"path_with_namespace": "g/app",
                                          "web_url": "https://gitlab.internal.example/g/app"})
        rs.get(f"{API}/projects/7/merge_requests/5/changes", json={"changes": [{"diff": "+ a"}]})
        with patch("forgeguard.review.run_claude",
                   side_effect=ClaudeLimit(None, detail="You've hit your session limit · resets 3:30pm (Asia/Dubai)")):
            run_review_tick(GitLab(cfg), st, fk, cfg)
    assert "resets 3:30pm" in fk.sent[0][0]

def test_run_claude_other_errors_stay_runtime_errors():
    fake = _proc(_stream("API Error: 500 boom", is_error=True), returncode=1, stderr="boom")
    with patch("forgeguard.review.subprocess.run", return_value=fake):
        with pytest.raises(RuntimeError) as e:
            run_claude("x")
    assert not isinstance(e.value, ClaudeLimit)

def test_run_codex_uses_read_only_ephemeral_structured_output(monkeypatch):
    monkeypatch.setenv("FORGEGUARD_CODEX_BIN", "/opt/codex")
    monkeypatch.setenv("FORGEGUARD_GITLAB_TOKEN", "must-not-leak")

    def fake_run(cmd, **kwargs):
        schema_path = Path(cmd[cmd.index("--output-schema") + 1])
        output_path = Path(cmd[cmd.index("--output-last-message") + 1])
        schema = json.loads(schema_path.read_text())
        assert schema["required"] == ["verdict", "summary", "issues", "tests_opinion"]
        assert schema["properties"]["issues"]["maxItems"] == 4
        output_path.write_text(CLEAN)
        assert kwargs["input"] == "review this"
        assert "FORGEGUARD_GITLAB_TOKEN" not in kwargs["env"]
        return _proc('{"type":"thread.started"}\n')

    with patch("forgeguard.review.subprocess.run", side_effect=fake_run):
        verdict = review.run_codex("review this", "gpt-5.6-sol")

    assert verdict["verdict"] == "clean"

def test_run_codex_command_pins_model_and_high_reasoning(monkeypatch):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        Path(cmd[cmd.index("--output-last-message") + 1]).write_text(CLEAN)
        return _proc("")

    with patch("forgeguard.review.subprocess.run", side_effect=fake_run):
        review.run_codex("x", "gpt-5.6-sol")

    assert seen["cmd"][:3] == [review._codex_binary(), "exec", "--model"]
    assert seen["cmd"][3] == "gpt-5.6-sol"
    assert ["--sandbox", "read-only"] == seen["cmd"][
        seen["cmd"].index("--sandbox"):seen["cmd"].index("--sandbox") + 2]
    assert "--ephemeral" in seen["cmd"]
    assert "--ignore-user-config" in seen["cmd"]
    assert "--ignore-rules" in seen["cmd"]
    assert "--skip-git-repo-check" in seen["cmd"]
    assert 'model_reasoning_effort="high"' in seen["cmd"]
    pairs = list(zip(seen["cmd"], seen["cmd"][1:]))
    assert ("--disable", "shell_tool") in pairs
    assert ("--disable", "multi_agent") in pairs
    assert ("--config", "tools.view_image=false") in pairs
    assert ("--config", 'web_search="disabled"') in pairs
    assert seen["cmd"][-1] == "-"

def test_run_codex_invalid_or_missing_result_fails_closed():
    def invalid(cmd, **kwargs):
        Path(cmd[cmd.index("--output-last-message") + 1]).write_text("not json")
        return _proc("")

    with patch("forgeguard.review.subprocess.run", side_effect=invalid):
        with pytest.raises(RuntimeError, match="unparseable codex output"):
            review.run_codex("x", "gpt-5.6-sol")

    with patch("forgeguard.review.subprocess.run", return_value=_proc("")):
        with pytest.raises(RuntimeError, match="no structured output"):
            review.run_codex("x", "gpt-5.6-sol")

def test_run_codex_quota_failure_raises_provider_limit():
    fake = _proc('{"type":"error","message":"You have hit your usage limit"}\n',
                 returncode=1, stderr="warning: retry disabled")
    with patch("forgeguard.review.subprocess.run", return_value=fake):
        with pytest.raises(ClaudeLimit) as e:
            review.run_codex("x", "gpt-5.6-sol")
    assert e.value.provider == "Codex"
    assert e.value.resets_at is None

def test_run_codex_timeout_and_nonzero_exit_are_review_failures():
    import subprocess
    with patch("forgeguard.review.subprocess.run",
               side_effect=subprocess.TimeoutExpired(cmd="codex", timeout=300)):
        with pytest.raises(RuntimeError, match="codex timed out after 300s"):
            review.run_codex("x", "gpt-5.6-sol")

    with patch("forgeguard.review.subprocess.run",
               return_value=_proc("", returncode=2, stderr="network failed")):
        with pytest.raises(RuntimeError, match="codex exited 2: network failed"):
            review.run_codex("x", "gpt-5.6-sol")

@pytest.mark.parametrize("verdict,issues", [
    ("clean", [{"severity": "high", "file": "app.py", "note": "breaks"}]),
    ("issues", []),
])
def test_run_codex_rejects_semantically_inconsistent_verdict(verdict, issues):
    inconsistent = json.dumps({"verdict": verdict, "summary": "ok",
                               "issues": issues, "tests_opinion": "fine"})

    def fake_run(cmd, **kwargs):
        Path(cmd[cmd.index("--output-last-message") + 1]).write_text(inconsistent)
        return _proc("")

    with patch("forgeguard.review.subprocess.run", side_effect=fake_run):
        with pytest.raises(RuntimeError, match="inconsistent codex verdict"):
            review.run_codex("x", "gpt-5.6-sol")

def _two_mrs():
    responses.get(f"{API}/merge_requests", json=[
        {"iid": 5, "project_id": 7, "sha": "h1", "title": "one", "description": "",
         "target_branch": "main", "author": {"username": "alice"},
         "updated_at": "2026-08-07T10:00:00Z",
         "web_url": "https://gitlab.internal.example/g/app/-/merge_requests/5"},
        {"iid": 6, "project_id": 7, "sha": "h2", "title": "two", "description": "",
         "target_branch": "main", "author": {"username": "bob"},
         "updated_at": "2026-08-07T11:00:00Z",
         "web_url": "https://gitlab.internal.example/g/app/-/merge_requests/6"}],
        headers={"X-Next-Page": ""})
    responses.get(f"{API}/projects/7", json={
        "path_with_namespace": "g/app",
        "web_url": "https://gitlab.internal.example/g/app"})
    for iid in (5, 6):
        responses.get(f"{API}/projects/7/merge_requests/{iid}/changes",
                      json={"changes": [{"diff": "+ a"}]})

@responses.activate
def test_limit_pauses_tick_and_alerts_once_with_resume_time(tmp_path, monkeypatch):
    monkeypatch.setenv("TZ", "Asia/Dubai"); time.tzset()
    _two_mrs()
    cfg = load_config(dict(BASE, FORGEGUARD_STATE=str(tmp_path / "s.json")))
    st, fk = State.load(cfg.state_path), FakeFeishu()
    with patch("forgeguard.review.run_claude",
               side_effect=ClaudeLimit(1788348600, "five_hour")) as rc:
        out = run_review_tick(GitLab(cfg), st, fk, cfg)
        run_review_tick(GitLab(cfg), st, fk, cfg)
    assert out == {"reviewed": 0, "skipped_large": 0, "failed": 1, "merged_unreviewed": 0}
    assert rc.call_count == 2          # one attempt per tick, second MR never tried
    assert st.get_cursor("mr_updated_after") is None
    paused = [t for t, _ in fk.sent if "paused" in t]
    assert len(paused) == 1
    assert "15:30" in paused[0] and "2 MR" in paused[0]
    assert st.flagged("limit:Claude:five_hour:1788348600")
    assert not [t for t, _ in fk.sent if "review failed" in t]
    assert "review by" not in str(fk.sent)

@responses.activate
def test_near_limit_warns_once_per_window(tmp_path, monkeypatch):
    monkeypatch.setenv("TZ", "Asia/Dubai"); time.tzset()
    _two_mrs()
    for iid in (5, 6):
        responses.get(f"{API}/projects/7/merge_requests/{iid}/notes", json=[],
                      headers={"X-Next-Page": ""})
        responses.post(f"{API}/projects/7/merge_requests/{iid}/notes", json={"id": iid})
        responses.post(f"{API}/projects/7/merge_requests/{iid}/approve", json={})
    cfg = load_config(dict(BASE, FORGEGUARD_STATE=str(tmp_path / "s.json")))
    st, fk = State.load(cfg.state_path), FakeFeishu()
    verdict = {"verdict": "clean", "summary": "ok", "issues": [], "tests_opinion": "fine"}
    # Below-threshold payloads carry utilization only inside unifiedWindows.
    rate = {"status": "allowed", "resetsAt": 1788348600, "rateLimitType": "five_hour",
            "unifiedWindows": {"five_hour": {"utilization": 0.98, "resetsAt": 1788348600}}}
    with patch("forgeguard.review.run_claude", return_value=verdict), \
         patch.dict(review.RATE_LIMIT, rate, clear=True):
        out = run_review_tick(GitLab(cfg), st, fk, cfg)
    assert out["reviewed"] == 2
    warns = [t for t, _ in fk.sent if "98%" in t]
    assert len(warns) == 1 and "15:30" in warns[0]

def test_limit_warn_threshold_configurable():
    assert load_config(BASE).limit_warn == 0.95
    assert load_config(dict(BASE, FORGEGUARD_LIMIT_WARN="0.8")).limit_warn == 0.8

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
    cfg = load_config(dict(BASE, FORGEGUARD_STATE=str(tmp_path / "s.json"),
                           FORGEGUARD_REVIEW_PROVIDER="codex"))
    st, fk = State.load(cfg.state_path), FakeFeishu()
    st.flag_once("limit:Codex:weekly:unknown")
    verdict = {"verdict": "clean", "summary": "ok", "issues": [], "tests_opinion": "has tests"}
    with patch("forgeguard.review.run_codex", return_value=verdict) as rc, \
         patch("forgeguard.review.run_claude") as claude:
        out = run_review_tick(GitLab(cfg), st, fk, cfg)
    rc.assert_called_once()
    assert rc.call_args.args[1] == "gpt-5.6-sol"
    claude.assert_not_called()
    assert out == {"reviewed": 1, "skipped_large": 0, "failed": 0, "merged_unreviewed": 0}
    title, lines, at_user = fk.posts[0]
    assert title == "✅ Approved: feat: x"
    assert at_user == "alice"
    flat = [s for line in lines for s in line]
    hrefs = [s["href"] for s in flat if s.get("tag") == "a"]
    assert "https://gitlab.example.com/g/app/-/merge_requests/5" in hrefs
    assert "https://gitlab.example.com/g/app/-/commit/head1" in hrefs
    assert any(s.get("text") == "head commit: " for s in flat)
    assert [{"tag": "text", "text": "review by gpt-5.6-sol"}] in lines
    assert lines[-1] == [{"tag": "text", "text": "⚙️ auto-review is advisory"}]
    note_call = next(c for c in responses.calls
                     if c.request.method == "POST" and c.request.url.endswith("/notes"))
    assert "review by" not in parse_qs(note_call.request.body)["body"][0]
    assert st.get_cursor("reviewed:7:5") == "head1"
    assert st.get_cursor("mr_updated_after") == "2026-08-07T10:00:00Z"
    assert not st.flagged("limit:Codex:weekly:unknown")

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
    fake = _proc(_stream(CLEAN))
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
    assert "review by" not in str(fk.sent)

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
    assert "review by" not in str(fk.posts)
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

def _merged_mr_fixture(sha="m1", merged_at="2026-08-07T12:00:00Z",
                       updated_at="2026-08-07T12:00:00Z", opened=None):
    responses.get(f"{API}/merge_requests",
                  json=opened or [], headers={"X-Next-Page": ""},
                  match=[responses.matchers.query_param_matcher({"state": "opened"}, strict_match=False)])
    responses.get(f"{API}/merge_requests", json=[{
        "iid": 30, "project_id": 7, "sha": sha, "title": "fast merge", "description": "",
        "target_branch": "dev", "author": {"username": "spd"}, "labels": [],
        "updated_at": updated_at, "merged_at": merged_at,
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
    assert "review by" not in str(fk.posts)
    assert "7 Aug 2026, 12:00 UTC" in str(fk.posts)
    assert "No recorded ForgeGuard review" in str(fk.posts)
    assert "periodic tick" not in str(fk.posts)
    responses.reset(); _merged_mr_fixture()
    out = run_review_tick(GitLab(cfg), st, fk, cfg)
    assert len(fk.posts) == 1                      # deduped


@pytest.mark.parametrize("merged_at", [
    "2026-07-02T20:11:39.762+08:00", None, "bad-date", "2026-08-07T10:00:00",
])
@responses.activate
def test_old_or_unverifiable_merge_updated_today_is_not_alerted(tmp_path, merged_at):
    _merged_mr_fixture(merged_at=merged_at, updated_at="2026-09-16T17:29:15.349+08:00")
    cfg = load_config(dict(BASE, FORGEGUARD_STATE=str(tmp_path / "s.json")))
    st, fk = State(), FakeFeishu()
    st.set_cursor("mr_updated_after:merged", "2026-09-16T09:00:00Z")
    out = run_review_tick(GitLab(cfg), st, fk, cfg)
    assert out["merged_unreviewed"] == 0
    assert fk.posts == [] and st.d["flags"] == []
    assert st.get_cursor("mr_updated_after:merged") == "2026-09-16T17:29:15.349+08:00"


@pytest.mark.parametrize("merged_at, expected", [
    ("2026-08-07T07:30:00Z", 1),
    ("2026-08-07T10:59:59+04:00", 0),
    ("2026-08-07T07:00:00Z", 0),
    ("2026-08-07T07:00:00.001Z", 1),
])
@responses.activate
def test_merge_window_compares_instants_including_offsets_and_boundary(tmp_path, merged_at, expected):
    _merged_mr_fixture(merged_at=merged_at)
    cfg = load_config(dict(BASE, FORGEGUARD_STATE=str(tmp_path / "s.json")))
    st, fk = State(), FakeFeishu()
    st.set_cursor("mr_updated_after:merged", "2026-08-07T11:00:00+04:00")
    out = run_review_tick(GitLab(cfg), st, fk, cfg)
    assert out["merged_unreviewed"] == expected
    assert len(fk.posts) == expected


@responses.activate
def test_first_merged_scan_retains_boundary_before_opened_cursor_advances(tmp_path):
    _merged_mr_fixture(opened=[{"project_id": 7, "iid": 31, "sha": "already",
        "target_branch": "dev", "updated_at": "2026-08-07T13:00:00Z"}])
    cfg = load_config(dict(BASE, FORGEGUARD_STATE=str(tmp_path / "s.json")))
    st, fk = State(), FakeFeishu()
    st.set_cursor("mr_updated_after", "2026-08-07T00:00:00Z")
    st.set_cursor("reviewed:7:31", "already")
    out = run_review_tick(GitLab(cfg), st, fk, cfg)
    assert st.get_cursor("mr_updated_after") == "2026-08-07T13:00:00Z"
    assert out["merged_unreviewed"] == 1 and len(fk.posts) == 1


@responses.activate
def test_merged_update_cursor_never_moves_backwards_across_offsets(tmp_path):
    _merged_mr_fixture(merged_at="2026-07-02T12:00:00Z",
                       updated_at="2026-08-07T11:30:00+04:00")
    cfg = load_config(dict(BASE, FORGEGUARD_STATE=str(tmp_path / "s.json")))
    st, fk = State(), FakeFeishu()
    st.set_cursor("mr_updated_after:merged", "2026-08-07T08:00:00Z")
    run_review_tick(GitLab(cfg), st, fk, cfg)
    assert st.get_cursor("mr_updated_after:merged") == "2026-08-07T08:00:00Z"
    assert fk.posts == []

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
        "merged_at": "2026-08-07T12:00:00Z",
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
    assert "7 Aug 2026, 12:00 UTC" in str(lines)
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
def test_codex_files_mode_claude_md_fallback_and_skeptic_gets_context(tmp_path):
    _ctx_mr()
    responses.get(f"{API}/projects/7/repository/files/.forgeguard.md/raw",
                  status=404)
    responses.get(f"{API}/projects/7/repository/files/CLAUDE.md/raw",
                  body="Use the centralized API client.")
    responses.get(f"{API}/projects/7/repository/files/src%2Fapp.py/raw",
                  body="def handler(): pass")
    cfg = load_config(dict(BASE, FORGEGUARD_STATE=str(tmp_path / "s.json"),
                           FORGEGUARD_REVIEW_PROVIDER="codex"))
    bad = {"verdict": "issues", "summary": "s",
           "issues": [{"severity": "high", "file": "src/app.py", "note": "n"}],
           "tests_opinion": "f"}
    clean = {"verdict": "clean", "summary": "ok", "issues": [], "tests_opinion": "fine"}
    with patch("forgeguard.review.run_codex", side_effect=[bad, clean]) as rc:
        run_review_tick(GitLab(cfg), State.load(cfg.state_path), FakeFeishu(), cfg)
    first, skeptic = rc.call_args_list[0].args[0], rc.call_args_list[1].args[0]
    assert all(call.args[1] == "gpt-5.6-sol" for call in rc.call_args_list)
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

@responses.activate
def test_files_mode_injects_project_brief(tmp_path):
    import os
    _ctx_mr()
    responses.get(f"{API}/projects/7/repository/files/src%2Fapp.py/raw",
                  body="def handler(): pass")
    brief_dir = tmp_path / "briefs"
    os.makedirs(brief_dir)
    (brief_dir / "g__app.md").write_text(
        "<!-- forge-guard-brief abc -->\nDjango app; routes need auth.\n")
    cfg = load_config(dict(BASE, FORGEGUARD_STATE=str(tmp_path / "s.json"),
                           FORGEGUARD_BRIEF_DIR=str(brief_dir)))
    verdict = {"verdict": "clean", "summary": "ok", "issues": [], "tests_opinion": "fine"}
    with patch("forgeguard.review.run_claude", return_value=verdict) as rc:
        out = run_review_tick(GitLab(cfg), State.load(cfg.state_path), FakeFeishu(), cfg)
    assert out["reviewed"] == 1
    prompt = rc.call_args.args[0]
    assert "Project brief (auto-generated" in prompt
    assert "Django app; routes need auth." in prompt
    assert "forge-guard-brief abc" not in prompt          # marker stripped

@responses.activate
def test_footer_configurable_and_droppable(tmp_path):
    verdict = {"verdict": "clean", "summary": "ok", "issues": [], "tests_opinion": "fine"}
    for footer, expect in (("仅供参考", [{"tag": "text", "text": "仅供参考"}]), ("", None)):
        responses.reset(); _two_mrs()
        for iid in (5, 6):
            responses.get(f"{API}/projects/7/merge_requests/{iid}/notes", json=[],
                          headers={"X-Next-Page": ""})
            responses.post(f"{API}/projects/7/merge_requests/{iid}/notes", json={"id": iid})
            responses.post(f"{API}/projects/7/merge_requests/{iid}/approve", json={})
        cfg = load_config(dict(BASE, FORGEGUARD_STATE=str(tmp_path / f"s{len(footer)}.json"),
                               FORGEGUARD_FOOTER=footer))
        fk = FakeFeishu()
        with patch("forgeguard.review.run_claude", return_value=verdict):
            run_review_tick(GitLab(cfg), State.load(cfg.state_path), fk, cfg)
        last = fk.posts[0][1][-1]
        assert (last == expect) if expect else (last[0]["text"] == "review by claude")
