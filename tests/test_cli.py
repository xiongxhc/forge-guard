import responses
from forgeguard.config import load_config
from forgeguard.gitlab import GitLab
from forgeguard.state import State
from forgeguard.cli import run_sweep
from tests.test_config import BASE

API = "https://gitlab.example.com/api/v4"

class FakeFeishu:
    usermap: dict = {}
    def open_id(self, name): return self.usermap.get(name or "")
    def __init__(self): self.sent, self.posts = [], []
    def notify(self, text, at_gitlab_user=None): self.sent.append((text, at_gitlab_user))
    def notify_post(self, title, lines, at_gitlab_user=None):
        self.posts.append((title, lines, at_gitlab_user))

def _project_fixtures(tip):
    responses.get(f"{API}/projects", json=[
        {"id": 7, "path_with_namespace": "g/app",
         "web_url": "https://gitlab.internal.example/g/app"}],
        headers={"X-Next-Page": ""})
    responses.get(f"{API}/projects/7/repository/branches", json=[
        {"name": "main", "commit": {"id": tip, "author_name": "alice"}},
        {"name": "feature/x", "commit": {"id": "zzz"}}])
    responses.get(f"{API}/projects/7/protected_branches/main", json={
        "name": "main", "allow_force_push": False,
        "push_access_levels": [{"access_level": 0}],
        "merge_access_levels": [{"access_level": 30}]})

@responses.activate
def test_first_run_records_tip_silently(tmp_path):
    _project_fixtures("t1")
    cfg = load_config(dict(BASE, FORGEGUARD_STATE=str(tmp_path / "s.json")))
    st, fk = State.load(cfg.state_path), FakeFeishu()
    out = run_sweep(GitLab(cfg), st, fk, cfg)
    assert out == {"projects": 1, "protected": 0, "force_push": 0, "unmr": 0, "errors": 0}
    assert st.get_tip(7, "main") == "t1"
    assert fk.sent == []

@responses.activate
def test_force_push_alerts(tmp_path):
    _project_fixtures("t2")
    responses.get(f"{API}/projects/7/repository/merge_base", json={"id": "not-t1"})
    cfg = load_config(dict(BASE, FORGEGUARD_STATE=str(tmp_path / "s.json")))
    st = State.load(cfg.state_path)
    st.set_tip(7, "main", "t1")
    fk = FakeFeishu()
    out = run_sweep(GitLab(cfg), st, fk, cfg)
    assert out["force_push"] == 1
    title, lines, _ = fk.posts[0]
    assert title == "⚠️ Force push: g/app main"
    flat = [s for line in lines for s in line]
    links = [s for s in flat if s.get("tag") == "a"]
    assert links[0]["href"].startswith("https://gitlab.example.com/")
    assert links[0]["text"] == "t2"[:8]
    assert any("alice" in s.get("text", "") for s in flat)
    assert st.get_tip(7, "main") == "t2"

@responses.activate
def test_failing_project_tolerated_and_alerted(tmp_path):
    responses.get(f"{API}/projects", json=[
        {"id": 7, "path_with_namespace": "g/bad",
         "web_url": "https://gitlab.internal.example/g/bad"},
        {"id": 8, "path_with_namespace": "g/app",
         "web_url": "https://gitlab.internal.example/g/app"}],
        headers={"X-Next-Page": ""})
    responses.get(f"{API}/projects/7/repository/branches",
                  json={"message": "403 Forbidden"}, status=403)
    responses.get(f"{API}/projects/8/repository/branches", json=[
        {"name": "main", "commit": {"id": "t1", "author_name": "alice"}}])
    responses.get(f"{API}/projects/8/protected_branches/main", json={
        "name": "main", "allow_force_push": False,
        "push_access_levels": [{"access_level": 0}],
        "merge_access_levels": [{"access_level": 30}]})
    cfg = load_config(dict(BASE, FORGEGUARD_STATE=str(tmp_path / "s.json")))
    st, fk = State.load(cfg.state_path), FakeFeishu()
    out = run_sweep(GitLab(cfg), st, fk, cfg)
    assert out == {"projects": 2, "protected": 0, "force_push": 0, "unmr": 0, "errors": 1}
    assert st.get_tip(8, "main") == "t1"
    assert len(fk.sent) == 1
    assert "1 project(s) failed" in fk.sent[0][0]

@responses.activate
def test_sweep_seen_set_dedupes_projects(tmp_path):
    _project_fixtures("t9")
    cfg = load_config(dict(BASE, FORGEGUARD_STATE=str(tmp_path / "s.json")))
    st, fk = State.load(cfg.state_path), FakeFeishu()
    seen = {7}
    out = run_sweep(GitLab(cfg), st, fk, cfg, seen=seen)
    assert out["projects"] == 0
    assert st.get_tip(7, "main") is None

@responses.activate
def test_sweep_populates_seen_set(tmp_path):
    _project_fixtures("t9")
    cfg = load_config(dict(BASE, FORGEGUARD_STATE=str(tmp_path / "s.json")))
    seen: set = set()
    run_sweep(GitLab(cfg), State.load(cfg.state_path), FakeFeishu(), cfg, seen=seen)
    assert seen == {7}


@responses.activate
def test_sweep_protects_glob_matched_branch(tmp_path):
    responses.get(f"{API}/projects", json=[
        {"id": 7, "path_with_namespace": "g/app",
         "web_url": "https://gitlab.internal.example/g/app"}],
        headers={"X-Next-Page": ""})
    responses.get(f"{API}/projects/7/repository/branches", json=[
        {"name": "adaa/uat", "commit": {"id": "t1", "author_name": "alice"}},
        {"name": "feature/devtools", "commit": {"id": "t2", "author_name": "alice"}}],
        headers={"X-Next-Page": ""})
    responses.get(f"{API}/projects/7/protected_branches/adaa%2Fuat", json={}, status=404)
    responses.post(f"{API}/projects/7/protected_branches", json={})
    cfg = load_config(dict(BASE, FORGEGUARD_STATE=str(tmp_path / "s.json"),
                           FORGEGUARD_BRANCHES="uat,*/uat"))
    st = State.load(cfg.state_path)
    out = run_sweep(GitLab(cfg), st, FakeFeishu(), cfg)
    assert out["protected"] == 1                       # adaa/uat protected
    assert st.get_tip(7, "adaa/uat") == "t1"
    assert st.get_tip(7, "feature/devtools") is None   # feature branch untouched
