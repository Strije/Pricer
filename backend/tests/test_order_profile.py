"""Профиль заказа поставщика: варианты из справочников поставщика (как проверка в настройках десктопа)."""
import pytest

from app import order_profile
from app import supplier_catalog as catalog
from test_server import H, app_factory, register  # noqa: F401


class _Zeep:
    """Объект ответа SOAP (zeep): поля — атрибуты, а не ключи словаря."""

    def __init__(self, **fields):
        self.__dict__.update(fields)


def test_options_read_dicts_and_soap_objects_without_duplicates():
    items = [{"id": "1", "name": "Курьер"}, _Zeep(Id="2", Name="Самовывоз"), {"id": "1", "name": "дубль"},
             {"id": "", "name": "без кода"}, {"ID": "3"}]
    assert order_profile.options(items, ("id", "Id", "ID"), ("name", "Name")) == [
        {"value": "1", "label": "Курьер"}, {"value": "2", "label": "Самовывоз"}, {"value": "3", "label": "3"}]
    assert order_profile.as_items({"items": [1, 2]}) == [1, 2]
    assert order_profile.as_items({"a": {"id": 1}}) == [{"id": 1}]


def test_catalog_has_order_profile_fields():
    public = catalog.public_catalog()
    rossko = {f["name"]: f for f in public["rossko"]["fields"]}
    assert public["rossko"]["order_options"] and not public["favorit"]["order_options"]
    assert rossko["address_id"] == {"name": "address_id", "label": "Адрес доставки", "type": "choice", "group": "order"}
    assert "group" not in rossko["key1"]
    # контакт и телефон для заказа — личные данные: шифруются, как при импорте settings.json
    assert {"contact_name", "contact_phone"} <= catalog.secret_fields("rossko")
    assert "phone_number" in catalog.secret_fields("tiss_tmparts")
    _, accounts = catalog.split_settings({"rossko": {"key1": "k1", "key2": "k2", "address_id": "77",
                                                     "contact_name": "Иван", "contact_phone": "79780000000"}})
    _, config, secrets = accounts[0]
    assert config == {"address_id": "77"} and set(secrets) == {"key1", "key2", "contact_name", "contact_phone"}


class _FakeRossko:
    created = []

    def __init__(self, key1, key2, timeout=15, **kwargs):
        self.key1, self.key2 = key1, key2
        _FakeRossko.created.append((key1, key2, timeout))

    def get_checkout_details(self):
        return {"success": True,
                "deliveries": [_Zeep(id="000000001", name="Доставка"), _Zeep(id="000000002", name="Самовывоз")],
                "payments": [{"id": "1", "name": "Наличные"}],
                "addresses": [{"id": "77", "address": "Севастополь, ул. Тестовая, 1"}],
                "companies": [{"id": "5", "company_name": "ООО Тест"}]}

    def checkout_defaults(self, details):
        return {"delivery_id": "000000001", "payment_id": "1", "address_id": "77", "requisite_id": ""}

    def checkout_details_message(self, details):
        return f"OK: доставок {len(details['deliveries'])}, ключ {self.key2}"


def test_rossko_options_and_defaults(monkeypatch):
    import rossko

    monkeypatch.setattr(rossko, "RosskoProvider", _FakeRossko)
    result = order_profile.load_options("rossko", {"key1": "k1", "key2": "k2", "timeout": "20"})
    assert result["ok"]
    assert result["options"]["delivery_id"] == [{"value": "000000001", "label": "Доставка"},
                                                {"value": "000000002", "label": "Самовывоз"}]
    assert result["options"]["requisite_id"] == [{"value": "5", "label": "ООО Тест"}]
    assert result["defaults"] == {"delivery_id": "000000001", "payment_id": "1", "address_id": "77"}
    assert _FakeRossko.created[-1] == ("k1", "k2", 20)
    assert order_profile.load_options("rossko", {"key1": "k1"})["ok"] is False


def test_armtek_lists_are_built_for_the_chosen_vkorg(monkeypatch):
    import armtek

    asked = []

    class FakeArmtek(armtek.ArmtekProvider):
        def get_user_vkorg_list(self):
            return [{"VKORG": "4000", "PROGRAM_NAME": "Армтек Юг"}, {"VKORG": "5000", "PROGRAM_NAME": "Армтек Центр"}]

        def get_user_info(self, vkorg):
            asked.append(vkorg)
            return {"STRUCTURE": {
                "RG_TAB": [{"KUNNR": "43001", "SNAME": "ООО Ромашка"}],
                "WE_TAB": [{"KUNNR": "43002", "SNAME": "Склад Ромашки"}],
                "ZA_TAB": [{"KUNNR": "50001", "ADDRESS": "Севастополь, ул. Тестовая, 1"}],
                "CONTACT_TAB": [{"PARNR": "777", "NAME": "Иван"}],
                "DOGOVOR_TAB": [{"VBELN": "D-1", "NAME": "Договор поставки"}],
            }}

    monkeypatch.setattr(armtek, "ArmtekProvider", FakeArmtek)
    result = order_profile.load_options("armtek", {"login": "u", "password": "p", "vkorg": "5000"})
    assert result["ok"] and asked == ["5000"]  # сохранённый VKORG поставщик знает — списки для него
    assert result["options"]["kunnr"] == [{"value": "43001", "label": "ООО Ромашка"}]
    assert result["options"]["kunnr_za"] == [{"value": "50001", "label": "Севастополь, ул. Тестовая, 1"}]
    assert result["options"]["vbeln"] == [{"value": "D-1", "label": "Договор поставки"}]
    assert result["defaults"]["vkorg"] == "5000" and result["defaults"]["kunnr"] == "43001"
    result = order_profile.load_options("armtek", {"login": "u", "password": "p", "vkorg": "9999"})
    assert asked[-1] == "4000" and result["defaults"]["vkorg"] == "4000"  # неизвестный — первый из списка


def test_tiss_chain_and_partial_answer(monkeypatch):
    import tiss_tmparts

    class FakeTiss:
        def __init__(self, api_key, warehouse_mode=0, timeout=6, **kwargs):
            self.last_message = ""
            self.legal_organization_id = ""

        def get_legal_organizations(self):
            return [{"id": "L1", "name": "ООО Один"}, {"id": "L2", "name": "ООО Два"}]

        def get_contracts(self, legal_id):
            return [{"id": f"{legal_id}-C1", "name": "Договор"}] if legal_id == "L2" else []

        def get_outlets(self, contract_id):
            return [{"id": "O1", "address": "Симферополь"}]

        def get_warehouses(self, outlet_id):
            return [{"id": "W1"}, {"id": "W2"}]

    monkeypatch.setattr(tiss_tmparts, "TissTmpartsProvider", FakeTiss)
    result = order_profile.load_options("tiss_tmparts", {"api_key": "k", "legal_organization_id": "L2"})
    assert result["ok"]
    assert result["defaults"] == {"legal_organization_id": "L2", "contract_id": "L2-C1", "outlet_id": "O1",
                                  "allowed_warehouses": "W1,W2"}
    # у первого юрлица договоров нет: юрлица показываем, причину — сообщением
    result = order_profile.load_options("tiss_tmparts", {"api_key": "k"})
    assert result["ok"] is False and result["message"] == "договоры не найдены"
    assert [o["value"] for o in result["options"]["legal_organization_id"]] == ["L1", "L2"]


def test_profit_league_picks_first_variants(monkeypatch):
    import pr_lg

    class FakePl:
        def __init__(self, api_key, timeout=12, **kwargs):
            self.last_message = ""

        def check_connection(self):
            return True, "складов: 3"

        def get_order_params(self):
            return {"methods": {"items": [{"id": "m1", "name": "Доставка"}]}, "payments": [{"id": "p1", "name": "Счёт"}],
                    "points": [{"code": "T1", "name": "Точка", "address": "Ялта, ул. Набережная"}], "pickup_points": []}

    monkeypatch.setattr(pr_lg, "PrLgProvider", FakePl)
    result = order_profile.load_options("profit_league", {"api_key": "k"})
    assert result["ok"] and result["message"].startswith("складов: 3; доставок: 1")
    assert result["options"]["order_address"] == [{"value": "Ялта, ул. Набережная", "label": "Ялта, ул. Набережная"}]
    assert result["defaults"] == {"order_method": "m1", "order_payment": "p1", "order_point": "T1",
                                  "order_address": "Ялта, ул. Набережная"}


def test_order_options_endpoint(app_factory, monkeypatch):  # noqa: F811
    import rossko

    monkeypatch.setattr(rossko, "RosskoProvider", _FakeRossko)
    make_client, _ = app_factory
    owner = register(make_client(), "profile-owner@example.com", org="Профиль")
    r = owner.post("/api/suppliers", headers=H, json={"section": "rossko", "config": {"enabled": True},
                                                      "secrets": {"key1": "rossko-key-one", "key2": "rossko-key-two"}})
    assert r.status_code == 200, r.text
    account_id = r.json()["id"]
    # секретные поля из формы не подменяются: ключи берутся из базы
    r = owner.post(f"/api/suppliers/{account_id}/order-options", headers=H,
                   json={"values": {"key1": "подмена", "address_id": "77"}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] and body["options"]["address_id"][0]["value"] == "77"
    assert _FakeRossko.created[-1][:2] == ("rossko-key-one", "rossko-key-two")
    assert "rossko-key-two" not in r.text  # сообщение поставщика с ключом очищено
    stranger = register(make_client(), "profile-stranger@example.com", org="Чужая")
    assert stranger.post(f"/api/suppliers/{account_id}/order-options", headers=H, json={}).status_code == 404
    r = owner.post("/api/suppliers", headers=H, json={"section": "mikado", "secrets": {"login": "l", "password": "p"}})
    assert owner.post(f"/api/suppliers/{r.json()['id']}/order-options", headers=H).status_code == 400
