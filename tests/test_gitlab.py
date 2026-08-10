import responses
from forgeguard.config import load_config
from forgeguard.gitlab import GitLab, rebase_url
from tests.test_config import BASE

def _gl():
    return GitLab(load_config(BASE))

def test_rebase_url_forces_configured_host():
    assert rebase_url("https://gitlab.internal.example/g/p/-/commit/abc",
                      "https://gitlab.example.com") == "https://gitlab.example.com/g/p/-/commit/abc"

@responses.activate
def test_get_all_paginates():
    responses.get("https://gitlab.example.com/api/v4/projects", json=[{"id": 1}],
                  headers={"X-Next-Page": "2"},
                  match=[responses.matchers.query_param_matcher(
                      {"per_page": "100", "page": "1"}, strict_match=False)])
    responses.get("https://gitlab.example.com/api/v4/projects", json=[{"id": 2}],
                  headers={"X-Next-Page": ""})
    assert [p["id"] for p in _gl().get_all("/projects")] == [1, 2]
    assert responses.calls[0].request.headers["PRIVATE-TOKEN"] == "tok"

@responses.activate
def test_get_404_ok_returns_none_and_raises_otherwise():
    responses.get("https://gitlab.example.com/api/v4/projects/1/protected_branches/main",
                  json={"message": "404"}, status=404)
    assert _gl().get("/projects/1/protected_branches/main", ok404=True) is None
    responses.get("https://gitlab.example.com/api/v4/boom", json={}, status=500)
    import pytest
    from forgeguard.gitlab import GitLabError
    with pytest.raises(GitLabError):
        _gl().get("/boom")

def test_token_override():
    from forgeguard.config import load_config
    from tests.test_config import BASE
    gl = GitLab(load_config(dict(BASE)), token="other")
    assert gl.s.headers["PRIVATE-TOKEN"] == "other"
