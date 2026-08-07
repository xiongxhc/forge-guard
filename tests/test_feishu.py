import json, responses
from forgeguard.config import load_config
from forgeguard.feishu import Feishu
from tests.test_config import BASE

FA = "https://open.feishu.cn/open-apis"

def _feishu(tmp_path, usermap):
    p = tmp_path / "usermap.json"
    p.write_text(json.dumps(usermap))
    cfg = load_config(dict(BASE, FORGEGUARD_USERMAP=str(p)))
    return Feishu(cfg)

@responses.activate
def test_notify_with_mention(tmp_path):
    responses.post(f"{FA}/auth/v3/tenant_access_token/internal",
                   json={"code": 0, "tenant_access_token": "t-x"})
    responses.post(f"{FA}/im/v1/messages?receive_id_type=chat_id",
                   json={"code": 0})
    _feishu(tmp_path, {"alice": "ou_123"}).notify("MR needs tests", at_gitlab_user="alice")
    body = json.loads(responses.calls[1].request.body)
    assert body["receive_id"] == "oc_x"
    assert '<at user_id="ou_123"></at>' in json.loads(body["content"])["text"]
    assert responses.calls[1].request.headers["Authorization"] == "Bearer t-x"

@responses.activate
def test_notify_unmapped_user_sends_without_at(tmp_path):
    responses.post(f"{FA}/auth/v3/tenant_access_token/internal",
                   json={"code": 0, "tenant_access_token": "t-x"})
    responses.post(f"{FA}/im/v1/messages?receive_id_type=chat_id", json={"code": 0})
    _feishu(tmp_path, {}).notify("hello", at_gitlab_user="ghost")
    assert "<at" not in json.loads(
        json.loads(responses.calls[1].request.body)["content"])["text"]
