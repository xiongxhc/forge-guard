import pytest
from forgeguard.config import load_config

BASE = {
    "FORGEGUARD_GITLAB_URL": "https://gitlab.example.com",
    "FORGEGUARD_GITLAB_TOKEN": "tok",
    "FORGEGUARD_FEISHU_APP_ID": "cli_x",
    "FORGEGUARD_FEISHU_APP_SECRET": "sec",
    "FORGEGUARD_FEISHU_CHAT_ID": "oc_x",
}

def test_defaults_and_required():
    cfg = load_config(BASE)
    assert cfg.gitlab_url == "https://gitlab.example.com"
    assert cfg.branches == ["main", "master", "prod", "production", "develop", "dev", "uat"]
    assert cfg.exclude == []
    assert cfg.diff_cap_bytes == 300_000
    assert cfg.state_path.endswith("forge-guard/state.json")

def test_trailing_slash_stripped_and_overrides():
    env = dict(BASE, FORGEGUARD_GITLAB_URL="https://gitlab.example.com/",
               FORGEGUARD_BRANCHES="main, uat", FORGEGUARD_EXCLUDE="sandbox/x")
    cfg = load_config(env)
    assert cfg.gitlab_url == "https://gitlab.example.com"
    assert cfg.branches == ["main", "uat"]
    assert cfg.exclude == ["sandbox/x"]

def test_missing_token_exits():
    env = {k: v for k, v in BASE.items() if k != "FORGEGUARD_GITLAB_TOKEN"}
    with pytest.raises(SystemExit):
        load_config(env)

def test_extra_tokens_parsed():
    cfg = load_config(dict(BASE, FORGEGUARD_GITLAB_EXTRA_TOKENS="tok2, tok3"))
    assert cfg.extra_tokens == ["tok2", "tok3"]

def test_extra_tokens_default_empty():
    assert load_config(dict(BASE)).extra_tokens == []

def test_branch_match_exact_and_glob():
    cfg = load_config(dict(BASE, FORGEGUARD_BRANCHES="dev,uat,*/dev,*/uat"))
    assert cfg.branch_match("dev")
    assert cfg.branch_match("adaa/dev")
    assert cfg.branch_match("uaeaa/uat")
    assert not cfg.branch_match("dev-tooling")       # plain entries stay exact
    assert not cfg.branch_match("redevelop")
    assert not cfg.branch_match("feature/devtools")  # glob is tail-anchored
    cfg = load_config(BASE)                          # no glob entries -> pure exact
    assert not cfg.branch_match("adaa/dev")

def test_review_branches_review_only():
    cfg = load_config(dict(BASE, FORGEGUARD_REVIEW_BRANCHES="release/*,uat-*"))
    assert cfg.review_match("release/2.1.0")
    assert cfg.review_match("uat-2.1.0")
    assert cfg.review_match("dev")                 # main list included
    assert not cfg.branch_match("release/2.1.0")   # protection untouched
    cfg = load_config(BASE)
    assert not cfg.review_match("release/2.1.0")   # default empty

def test_review_mode_default_and_override():
    assert load_config(BASE).review_mode == "files"
    assert load_config(BASE).context_cap_bytes == 600_000
    assert load_config(dict(BASE, FORGEGUARD_REVIEW_MODE="diff")).review_mode == "diff"

def test_review_mode_invalid_exits():
    with pytest.raises(SystemExit):
        load_config(dict(BASE, FORGEGUARD_REVIEW_MODE="checkout"))
