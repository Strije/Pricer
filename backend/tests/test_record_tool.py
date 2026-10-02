"""Проверка скрипта записи ответов поставщиков (tools/record_provider_responses.py)."""
import base64
import importlib.util
import json
import os

import requests
from requests.adapters import HTTPAdapter

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location(
    "record_tool", os.path.join(ROOT, "tools", "record_provider_responses.py")
)
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)

SETTINGS = {
    "armtek": {"login": "user@mail.example", "password": "Pa$$/word1", "vkorg": "4000"},
    "rossko": {"key1": "abcdef0123456789", "contact_phone": "+7(900)000-00-00"},
    "url_csv": {"urls": ["ftp://ftpuser:ftpsecret@ftp.example.com/price.csv"]},
    "timeout": 12,
}


def test_collects_secret_values_and_encoded_variants():
    secrets = tool.collect_secrets(SETTINGS)
    for value in ("Pa$$/word1", "abcdef0123456789", "+7(900)000-00-00", "ftpuser", "ftpsecret"):
        assert value in secrets
    assert "4000" not in secrets  # vkorg — не секрет
    assert "Pa%24%24%2Fword1" in secrets  # URL-кодированный пароль
    basic = base64.b64encode(b"user@mail.example:Pa$$/word1").decode()
    assert basic in secrets


def test_records_http_and_masks_everything(tmp_path, monkeypatch):
    secrets = tool.collect_secrets(SETTINGS)
    out = tmp_path / "rec.jsonl"
    recorder = tool.Recorder(str(out), secrets)

    def fake_send(self, request, *args, **kwargs):
        response = requests.Response()
        response.status_code = 200
        response._content = '{"token": "abcdef0123456789", "price": 700}'.encode()
        response.headers["Set-Cookie"] = "session=1"
        response.request = request
        return response

    monkeypatch.setattr(HTTPAdapter, "send", fake_send)
    original = tool.install_http_recorder(recorder)
    try:
        requests.post(
            "https://api.example.com/search?key=abcdef0123456789&pwd=Pa%24%24%2Fword1",
            data={"login": "user@mail.example", "article": "W71295"},
            auth=("user@mail.example", "Pa$$/word1"),
        )
    finally:
        HTTPAdapter.send = original
    recorder.close()

    text = out.read_text(encoding="utf-8")
    assert tool.verify_no_secrets(str(out), secrets) == []
    record = json.loads(text.splitlines()[0])
    assert record["type"] == "http" and record["status_code"] == 200
    assert "W71295" in record["request_body"]
    assert '"price": 700' in record["response_body"]
    assert "Authorization" not in record["request_headers"]
    assert "Set-Cookie" not in record["response_headers"]


def test_ordering_is_blocked():
    class Provider:
        def add_to_basket(self, *a):
            return "sent"

    provider = Provider()
    tool.block_ordering([provider])
    try:
        provider.add_to_basket({}, 1)
    except RuntimeError:
        pass
    else:
        raise AssertionError("корзина должна быть заблокирована")


def test_masks_derived_credentials_by_parameter_name():
    url = ("https://abstd.example/api-search?article=162622&agreement_id=31663&auth=e61a1090abcdef"
           "&brand=ZIC")
    assert tool.mask_params(url).endswith("agreement_id=31663&auth=***&brand=ZIC")
    url = "https://x.example/search/articles?userlogin=api@id1&userpsw=e9dffc24&number=162622"
    assert tool.mask_params(url) == "https://x.example/search/articles?userlogin=***&userpsw=***&number=162622"
    assert tool.mask_params("ClientID=1&Password=abc&Code=W71295") == "ClientID=***&Password=***&Code=W71295"
    assert tool.mask_params('{"user_login": "u", "user_password": "p", "brand": "ZIC"}') == (
        '{"user_login": "***", "user_password": "***", "brand": "ZIC"}'
    )
    assert tool.mask_params("<KEY1>abc</KEY1><text>W71295</text>") == "<KEY1>***</KEY1><text>W71295</text>"
    # Обычные поля с похожими именами не трогаем
    assert tool.mask_params("?brand_key=MANN&article=1") == "?brand_key=MANN&article=1"


def test_md5_of_password_is_treated_as_secret():
    import hashlib

    secrets = tool.collect_secrets(SETTINGS)
    assert hashlib.md5(b"Pa$$/word1").hexdigest() in secrets
