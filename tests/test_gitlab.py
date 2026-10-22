import responses
from forgeguard.config import load_config
from forgeguard.gitlab import GitLab, rebase_url
from tests.test_config import BASE

def _gl():
    return GitLab(load_config(BASE))

def test_rebase_url_forces_configured_host():
    assert rebase_url("https://gitlab.internal.example/g/p/-/commit/abc",
                      "https://gitlab.example.com") == "https://gitlab.example.com/g/p/-/commit/abc"

def test_review_text_rewrites_only_exact_configured_origins():
    from forgeguard.gitlab import rewrite_gitlab_links
    cfg = load_config(dict(BASE, FORGEGUARD_GITLAB_LINK_ORIGINS="https://old.example"))
    text = (
        "[MR](https://old.example/g/p/-/merge_requests/1?q=%2F#note_2) "
        "<HTTPS://OLD.EXAMPLE/g/p> https://old.example. "
        "https://old.example.evil/path https://old.example:8443/path "
        "https://old.example@evil.example/path http://old.example/path "
        "https://docs.example/?ref=https://old.example/path"
    )
    expected = text.replace("https://old.example/g/p", cfg.gitlab_url + "/g/p")
    expected = expected.replace("HTTPS://OLD.EXAMPLE/g/p", cfg.gitlab_url + "/g/p")
    expected = expected.replace("https://old.example. ", cfg.gitlab_url + ". ")
    assert rewrite_gitlab_links(text, cfg) == expected
    assert rewrite_gitlab_links(text, load_config(BASE)) == text

def test_review_text_handles_adjacent_links_tables_and_ipv6_without_rewriting_nested_queries():
    from forgeguard.gitlab import rewrite_gitlab_links
    cfg = load_config(dict(BASE, FORGEGUARD_GITLAB_LINK_ORIGINS=
                          "https://old.example,https://[2001:db8::1]:8443"))
    text = (
        "[one](https://old.example/a)[two](https://old.example/b)\n"
        "|https://old.example/a|https://old.example/b|\n"
        "https://[2001:db8::1]:8443/g/p?q=1#note\n"
        "https://old.example/a?next=https://old.example/b#note\n"
        "https://docs.example/a?next=https://old.example/b"
    )
    assert rewrite_gitlab_links(text, cfg) == (
        "[one](https://gitlab.example.com/a)[two](https://gitlab.example.com/b)\n"
        "|https://gitlab.example.com/a|https://gitlab.example.com/b|\n"
        "https://gitlab.example.com/g/p?q=1#note\n"
        "https://gitlab.example.com/a?next=https://old.example/b#note\n"
        "https://docs.example/a?next=https://old.example/b"
    )

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
