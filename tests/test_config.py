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
    assert cfg.branches == ["main", "master", "prod", "production", "develop", "uat"]
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
