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

@responses.activate
def test_notify_post_with_mention(tmp_path):
    responses.post(f"{FA}/auth/v3/tenant_access_token/internal",
                   json={"code": 0, "tenant_access_token": "t-x"})
    responses.post(f"{FA}/im/v1/messages?receive_id_type=chat_id", json={"code": 0})
    _feishu(tmp_path, {"alice": "ou_123"}).notify_post(
        "📝 Review: x",
        [[{"tag": "text", "text": "sum"}],
         [{"tag": "text", "text": "MR: "},
          {"tag": "a", "text": "!5", "href": "https://g/mr/5"}]],
        at_gitlab_user="alice")
    body = json.loads(responses.calls[1].request.body)
    assert body["msg_type"] == "post"
    content = json.loads(body["content"])["zh_cn"]
    assert content["title"] == "📝 Review: x"
    assert content["content"][0] == [{"tag": "at", "user_id": "ou_123"}]
    assert content["content"][1] == [{"tag": "text", "text": "sum"}]
    assert {"tag": "a", "text": "!5", "href": "https://g/mr/5"} in content["content"][2]

@responses.activate
def test_notify_post_unmapped_user_named_in_text(tmp_path):
    responses.post(f"{FA}/auth/v3/tenant_access_token/internal",
                   json={"code": 0, "tenant_access_token": "t-x"})
    responses.post(f"{FA}/im/v1/messages?receive_id_type=chat_id", json={"code": 0})
    _feishu(tmp_path, {}).notify_post(
        "t", [[{"tag": "text", "text": "s"}]], at_gitlab_user="ghost")
    content = json.loads(json.loads(responses.calls[1].request.body)["content"])["zh_cn"]
    assert content["content"][0] == [{"tag": "text", "text": "@ghost"}]
    assert not any(s["tag"] == "at" for line in content["content"] for s in line)

def test_open_id_falls_back_to_normalized_name(tmp_path):
    fs = _feishu(tmp_path, {"lintianhua": "ou_1", "Zihan.Guo": "ou_2", "alice": "ou_3"})
    assert fs.open_id("lintianhua") == "ou_1"        # exact
    assert fs.open_id("lin tianhua") == "ou_1"       # git author name vs gitlab username
    assert fs.open_id("Lin Tianhua") == "ou_1"
    assert fs.open_id("zihan guo") == "ou_2"         # dot-separated username
    assert fs.open_id("nobody") is None
    assert fs.open_id(None) is None

@responses.activate
def test_notify_post_mentions_via_normalized_name(tmp_path):
    responses.post(f"{FA}/auth/v3/tenant_access_token/internal",
                   json={"code": 0, "tenant_access_token": "t-x"})
    responses.post(f"{FA}/im/v1/messages?receive_id_type=chat_id", json={"code": 0})
    _feishu(tmp_path, {"lintianhua": "ou_1"}).notify_post(
        "t", [[{"tag": "text", "text": "s"}]], at_gitlab_user="lin tianhua")
    content = json.loads(json.loads(responses.calls[1].request.body)["content"])["zh_cn"]
    assert content["content"][0] == [{"tag": "at", "user_id": "ou_1"}]
