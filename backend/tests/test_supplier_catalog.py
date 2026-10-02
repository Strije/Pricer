import json
import os

from app import supplier_catalog as catalog
from app.security import SecretBox, hash_password, verify_password

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "recordings", "engine_settings.json")


def test_split_and_compose_roundtrip():
    with open(FIXTURE, encoding="utf-8") as file:
        settings = json.load(file)
    org, accounts = catalog.split_settings(settings)
    assert org["markup_rules"] and "hide_no_return" in org
    composed = catalog.compose_settings(org, accounts)
    for section in catalog.CATALOG:
        if section in settings:
            assert composed[section] == settings[section], section


def test_secrets_are_separated():
    _, accounts = catalog.split_settings({
        "armtek": {"login": "u", "password": "p", "vkorg": "4000", "incoterms_label": "Личное имя"},
        "url_csv": {"urls": ["ftp://u:p@host/x.csv"], "cache_hours": 24},
        "abcp_suppliers": [{"name": "A", "host": "h", "login": "l", "password": "p"}],
    })
    by_section = {section: (config, secrets) for section, config, secrets in accounts}
    config, secrets = by_section["armtek"]
    assert secrets == {"login": "u", "password": "p"} and config == {"vkorg": "4000"}  # подпись отброшена
    config, secrets = by_section["url_csv"]
    assert "urls" in secrets and config == {"cache_hours": 24}
    config, secrets = by_section["abcp_suppliers"]
    assert config == {"name": "A", "host": "h"} and set(secrets) == {"login", "password"}


def test_secret_box_and_passwords():
    from cryptography.fernet import Fernet

    box = SecretBox(Fernet.generate_key())
    sealed = box.seal({"password": "секрет"})
    assert "секрет" not in sealed and box.open(sealed) == {"password": "секрет"}
    stored = hash_password("correct horse")
    assert verify_password("correct horse", stored) and not verify_password("wrong", stored)
