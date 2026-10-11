import json

import pytest
from conftest import account

from chatgpt_gateway.storage import Store


def test_host_and_key_survive_restart(tmp_path):
    original, later = Store(tmp_path), Store(tmp_path)
    assert original.api_key == later.api_key and original.host_id == later.host_id
    assert (tmp_path / "host.json").stat().st_mode & 0o777 == 0o600
    assert tmp_path.stat().st_mode & 0o777 == 0o700


def test_same_email_different_registrations_remain_separate(tmp_path):
    store = Store(tmp_path)
    a = store.save(account(client_id="oaiapp_one"), active=True)
    b = store.save(account(client_id="oaiapp_two"), active=True)
    assert a["id"] != b["id"] and len(store.accounts()) == 2
    assert store.select(a["id"])
    assert store.active()["client_id"] == "oaiapp_one"
    assert store.get("../../host.json") is None


def test_journal_rejects_content_and_credentials(tmp_path):
    store = Store(tmp_path)
    for field in ("prompt", "access_token", "response", "authorization_url"):
        with pytest.raises(ValueError):
            store.journal({field: "secret"})
    store.journal({"request_id": "one", "event": "completed", "usage": {"input_tokens": 5}})
    assert store.recent()[0]["usage"]["input_tokens"] == 5
    assert (tmp_path / "requests.jsonl").stat().st_mode & 0o777 == 0o600
    assert json.loads((tmp_path / "requests.jsonl").read_text())["request_id"] == "one"
