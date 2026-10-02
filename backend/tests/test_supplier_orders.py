"""Заказы в личных кабинетах поставщиков: разбор ответов (поля — как в 1С-обработке) и сопоставление
строк поставщика с позициями журнала «Заказы поставщикам»."""
import datetime
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import supplier_orders as so
from app import db
from app.supplier_lines import apply_rows, fetcher_for, match_rows, normalize_status

SINCE = datetime.date(2026, 9, 1)


class Response:
    def __init__(self, body, status=200):
        self.status_code = status
        self.text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
        self.content = self.text.encode("utf-8")

    def json(self):
        return json.loads(self.text)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeHttp:
    """Подменяет so._session(): отдаёт ответы по очереди и запоминает запросы."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self):
        return self

    def _next(self, method, url, kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)

    def get(self, url, **kwargs):
        return self._next("GET", url, kwargs)

    def post(self, url, **kwargs):
        return self._next("POST", url, kwargs)


@pytest.fixture
def http(monkeypatch):
    def install(*responses):
        fake = FakeHttp(*[r if isinstance(r, Response) else Response(r) for r in responses])
        monkeypatch.setattr(so, "_session", fake)
        return fake
    return install


class P:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def test_armtek_partial_rejection_is_split(http):
    fake = http({"STATUS": 200, "RESP": {"DATA": [
        {"ORDER": "0123", "ORDER_DATE": "20260915101500", "PIN": "OC90", "BRAND": "KNECHT", "NAME": "Фильтр",
         "KWMENG": "3", "REJECTED": "1", "STATUS": "В работе", "ABGRU_TXT": "нет у поставщика", "NOTE": "ORD-1"},
        {"ORDER": "0124", "ORDER_DATE": "20260916", "PIN": "W712", "BRAND": "MANN", "NAME": "Фильтр",
         "ZZKWMENG": "2", "REJECTED": "2", "STATUS": "Закрыт", "ABGRU_TXT": "снят"}]}})
    rows = so.armtek_orders(P(vkorg="4000", kunnr="43", headers={"Authorization": "Basic x"}), SINCE)
    assert fake.calls[0][1] == "https://ws.armtek.ru/api/ws_reports/getOrderPositionsReportByDate2"
    assert fake.calls[0][2]["data"]["SCRDATE"] == "20260901" and fake.calls[0][2]["data"]["format"] == "json"
    assert [(r["quantity"], r["refused"], r["status"]) for r in rows] == [
        (1, True, "отклонено: нет у поставщика"), (2, False, "В работе"), (2, True, "отклонено: снят")]
    assert rows[0]["date"] == "2026-09-15T10:15:00" and rows[2]["date"] == "2026-09-16T00:00:00"
    assert rows[1]["comment"] == "ORD-1"


ROSSKO = """<?xml version="1.0" encoding="utf-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/" xmlns:ns1="https://api.rossko.ru/">
<SOAP-ENV:Body><ns1:GetOrdersResponse><ns1:OrdersResult><ns1:success>true</ns1:success>
<ns1:OrdersList><ns1:Order><ns1:id>R-77</ns1:id><ns1:created_date>15.09.2026 12:00:00</ns1:created_date>
<ns1:parts><ns1:part><ns1:partnumber>GDB1330</ns1:partnumber><ns1:brand>TRW</ns1:brand><ns1:name>Колодки</ns1:name>
<ns1:count>1</ns1:count><ns1:status>2</ns1:status></ns1:part>
<ns1:part><ns1:partnumber>15208-AA130</ns1:partnumber><ns1:brand>SUBARU</ns1:brand><ns1:count>2</ns1:count>
<ns1:status>7</ns1:status></ns1:part></ns1:parts></ns1:Order></ns1:OrdersList></ns1:OrdersResult>
</ns1:GetOrdersResponse></SOAP-ENV:Body></SOAP-ENV:Envelope>"""


def test_rossko_status_codes(http):
    fake = http(ROSSKO)
    rows = so.rossko_orders(P(key1="k1", key2="k2"), SINCE)
    assert b"<ns0:start_date>2026-09-01</ns0:start_date>" in fake.calls[0][2]["data"]
    assert [(r["order"], r["article"], r["status"], r["refused"]) for r in rows] == [
        ("R-77", "GDB1330", "отгружено", False), ("R-77", "15208-AA130", "нет в наличии", True)]
    assert rows[0]["date"] == "2026-09-15T12:00:00"


def test_favorit_cancelled_order_and_paging(http):
    page = [{"id": str(i), "number": f"F{i}", "date": "2026-09-10T09:00:00", "status": {"id": "1", "name": "В работе"},
             "goodsList": [{"goods": {"number": "PART", "brand": "LYNX", "name": "x"}, "count": 2, "countCancel": 1}]}
            for i in range(40)]
    last = [{"id": "99", "number": "F99", "status": {"id": "8", "name": "Отменён"},
             "goodsList": [{"goods": {"number": "X1", "brand": "B"}, "count": 3}]}]
    fake = http(page, last)
    rows = so.favorit_orders(P(api_key="c", developer_key="d"), SINCE)
    assert [c[2]["params"]["count"] for c in fake.calls] == ["0", "40"]
    assert fake.calls[0][2]["headers"]["X-Favorit-ClientKey"] == "c"
    assert rows[0]["refused"] and rows[0]["quantity"] == 1 and rows[1]["status"] == "В работе"
    assert rows[-1] == {**rows[-1], "order": "F99", "quantity": 3, "refused": True}


def test_favorit_5xx_retries_with_keys_in_params(http):
    fake = http(Response("oops", 502), [])
    assert so.favorit_orders(P(api_key="c", developer_key="d"), SINCE) == []
    assert fake.calls[1][2]["params"]["key"] == "c" and "X-Favorit-ClientKey" not in fake.calls[1][2]["headers"]


def test_forum_unconfirmed_after_status_50_is_refusal(http):
    http([{"nr": "5001", "statusId": 60, "status": "Отгружен", "dtc": "12.09.2026 10:00:00",
           "arTovs": [{"nr": "OC90", "brand": "KNECHT", "tovname": "Ф", "k1": 3, "k3": 2}]},
          {"nr": "5002", "statusId": 10, "status": "Принят", "arTovs": [{"nr": "A", "brand": "B", "k1": 1, "k3": 0}]}])
    rows = so.forum_orders(P(base_url="https://api.forum-auto.ru/v2", login="l", password="p"), SINCE)
    assert [(r["order"], r["quantity"], r["refused"]) for r in rows] == [("5001", 1, True), ("5001", 2, False),
                                                                       ("5002", 1, False)]


def test_forum_no_active_orders_and_errors(http):
    http({"errors": {"FaultCode": "26", "FaultString": "нет"}})
    assert so.forum_orders(P(base_url="u", login="l", password="p"), SINCE) == []
    http({"errors": {"FaultCode": "1", "FaultString": "bad"}})
    with pytest.raises(RuntimeError, match="bad"):
        so.forum_orders(P(base_url="u", login="l", password="p"), SINCE)


def test_avtoto_shipping_list(http):
    fake = http({"Shippings": [{"Id": "S1", "Date": "20-09-2026 08:00:00", "Type": "Доставка",
                                "Orders": [{"OrderId": "77", "Code": "OC90", "Manuf": "KNECHT", "Count": "1"}]}],
                 "Pagination": {"CountPages": 1}})
    provider = P(base_url="https://www.avtoto.ru/?soap_server=json_mode")
    provider._user_payload = lambda: {"user_id": "1"}
    rows = so.avtoto_orders(provider, SINCE)
    assert fake.calls[0][2]["data"]["action"] == "GetShippingList"
    assert json.loads(fake.calls[0][2]["data"]["data"])["from"] == "01.09.2026"
    assert rows[0]["status"].startswith("отгружено") and rows[0]["date"] == "2026-09-20T08:00:00"
    assert normalize_status(rows[0]["status"]) == "in_transit"


def test_abstd_statuses_and_login_fallback(http):
    fake = http(Response("forbidden", 403), {"status": "OK", "orders": [
        {"order_id": "A1", "create_date": "11.09.2026 10:00:00", "order_products": [
            {"article": "OC90", "brand": "KNECHT", "quantity": 3, "product_statuses": [
                {"status_id": "5", "status_name": "В пути", "quantity": 2},
                {"status_id": "-3", "status_name": "Отказ", "quantity": 1}]}]}]})
    provider = P(base_url="https://abstd.ru")
    provider._auth_logins = lambda: ["007", "7"]
    provider._calc_auth = lambda login: "h-" + login
    rows = so.abstd_orders(provider, SINCE)
    assert [c[2]["params"]["auth"] for c in fake.calls] == ["h-007", "h-7"]
    assert [(r["quantity"], r["status"], r["refused"]) for r in rows] == [(1, "Отказ", True), (2, "В пути", False)]


def test_abcp_partial_delivery(http):
    provider = P()
    provider.get_orders = lambda limit=100, skip=0: [
        {"number": "900", "date": "2026-09-20 10:00:00", "positions": [
            {"positionId": "P1", "supplierCode": "S", "brand": "VAG", "number": "04E115561H", "numberFix": "04E115561H",
             "quantityOrdered": 4, "quantity": 3, "status": "В пути"}]},
        {"number": "100", "date": "2026-01-01 10:00:00", "positions": [{"brand": "X", "number": "Y", "quantity": 1}]}]
    rows = so.abcp_orders(provider, SINCE)
    assert [(r["order"], r["quantity"], r["refused"], r["position_id"]) for r in rows] == [
        ("900", 1, True, "P1"), ("900", 3, False, "P1")]


def test_prlg_and_mikado(http):
    http({"pages": 1, "data": [{"order_id": "L1", "datetime": "2026-09-05 10:00:00", "products": [
        {"article": "OC90", "brand": "KNECHT", "quantity": 1, "status": "Отказ", "status_id": "6"}]}]})
    rows = so.prlg_orders(P(api_key="s"), SINCE)
    assert rows[0]["refused"] and rows[0]["order"] == "L1"
    http("""<?xml version="1.0"?><ArrayOfBasketItem xmlns="http://mikado-parts.ru/service">
<BasketItem><ID>11</ID><ZakazCode>KN-OC90</ZakazCode><Name>Фильтр</Name><QTY>2</QTY><Status>Zakaz</Status>
<Srok>3 дня</Srok></BasketItem><BasketItem><ID>12</ID><ZakazCode>XX-1</ZakazCode><Status>Basket</Status></BasketItem>
<BasketItem><ID>13</ID><ZakazCode>MN-W712</ZakazCode><QTY>1</QTY><Status>Otkaz</Status></BasketItem></ArrayOfBasketItem>""")
    rows = so.mikado_orders(P(client_id="1", password="p"), SINCE)
    assert [(r["article"], r["refused"]) for r in rows] == [("OC90", False), ("W712", True)]


def test_fetchers_cover_desktop_providers():
    from abcp_supplier import abcp_supplier_class

    import armtek
    import avtoto
    import tradesoft

    assert so.fetcher_for(armtek.ArmtekProvider("l", "p", "4000", "43")) is so.armtek_orders
    assert so.fetcher_for(avtoto.AvtotoProvider("1", "l", "p")) is so.avtoto_orders
    assert so.fetcher_for(abcp_supplier_class("Элит")("h", "l", "p")) is so.abcp_orders
    assert fetcher_for(tradesoft.TradesoftProvider("l", "p")).__name__ == "refresh_tradesoft"
    assert fetcher_for(armtek.ArmtekProvider("l", "p", "4000", "43")) is not None


# ---------- сопоставление ----------

@pytest.fixture
def org_session():
    engine = create_engine("sqlite://")
    db.Base.metadata.create_all(engine)
    with sessionmaker(engine)() as session:
        org = db.Organization(name="o", settings={})
        session.add(org)
        session.flush()
        yield session, org.id


def _line(session, org_id, index, **kw):
    base = dict(organization_id=org_id, order_id="ORD-1", item_index=index, provider="Армтек", brand="KNECHT",
                article="OC90", quantity=1, status="submitted", submitted_at=datetime.datetime(2026, 9, 15, 10, 0))
    line = db.SupplierLine(**{**base, **kw})
    session.add(line)
    session.flush()
    return line


def _row(order, article, quantity, status, refused=False, date="2026-09-15T10:05:00", **kw):
    return {"order": order, "date": date, "article": article, "brand": kw.pop("brand", "KNECHT"), "name": "",
            "quantity": quantity, "status": status, "refused": refused, "comment": kw.pop("comment", ""), **kw}


def test_partial_refusal_status(org_session):
    session, org = org_session
    line = _line(session, org, 0, quantity=3)
    rows = [_row("A", "OC90", 1, "отклонено: нет", True), _row("A", "OC90", 2, "В пути")]
    assert apply_rows(session, org, "Армтек", rows) == 1
    assert line.status == "in_transit" and line.status_text == "В пути; отказ 1 шт."
    assert apply_rows(session, org, "Армтек", rows) == 0  # повтор без изменений — без событий


def test_full_refusal_and_leading_zeros(org_session):
    session, org = org_session
    line = _line(session, org, 0, article="0986452041", brand="BOSCH")
    apply_rows(session, org, "Армтек", [_row("A", "986452041", 1, "снят", True, brand="Bosch")])
    assert line.status == "refused" and line.closed


def test_label_in_comment_and_date_pick_right_order(org_session):
    session, org = org_session
    mine = _line(session, org, 0, order_id="ORD-7")
    other = _line(session, org, 1, order_id="ORD-8", submitted_at=datetime.datetime(2026, 9, 18, 10, 0))
    rows = [_row("B", "OC90", 1, "Отгружен", date="2026-09-18T10:01:00"),
            _row("A", "OC90", 1, "Принят", date="2026-09-15T10:01:00", comment="заказ ORD-7")]
    matched = match_rows([mine, other], rows)
    assert matched[mine.id][0][0][0]["order"] == "A" and matched[other.id][0][0][0]["order"] == "B"


def test_older_supplier_orders_are_not_ours(org_session):
    session, org = org_session
    line = _line(session, org, 0)
    assert apply_rows(session, org, "Армтек", [_row("OLD", "OC90", 1, "Выдан", date="2026-08-01T10:00:00")]) == 0
    assert line.status == "submitted"


def test_known_supplier_ref_is_strict(org_session):
    session, org = org_session
    line = _line(session, org, 0, supplier_ref="A")
    rows = [_row("B", "OC90", 1, "Отказ", True), _row("A", "OC90", 1, "Подтверждён")]
    apply_rows(session, org, "Армтек", rows)
    assert line.status == "confirmed"


def test_supplier_does_not_roll_back_operator(org_session):
    session, org = org_session
    line = _line(session, org, 0, status="arrived")
    assert apply_rows(session, org, "Армтек", [_row("A", "OC90", 1, "отгружено")]) == 0
    assert line.status == "arrived"
    assert apply_rows(session, org, "Армтек", [_row("A", "OC90", 1, "отказ", True)]) == 1  # отказ — всегда


def test_unknown_text_means_at_least_confirmed(org_session):
    session, org = org_session
    line = _line(session, org, 0, status="unknown")
    apply_rows(session, org, "Армтек", [_row("A", "OC90", 1, "комплектуется")])
    assert line.status == "confirmed" and line.status_text == "комплектуется"


@pytest.mark.parametrize("text,code", [("ждет подтверждения", "submitted"), ("ожидаем поступление", "confirmed"),
                                       ("ожидаем товар на складе", "confirmed"), ("на складе филиала", "arrived"),
                                       ("отменен клиентом", "refused"), ("товар возвращен", "returned")])
def test_rossko_texts(text, code):
    assert normalize_status(text) == code


# Тексты статусов из реальной записи личных кабинетов (02.10.2026).
@pytest.mark.parametrize("text,code", [
    ("Получен клиентом", "arrived"), ("Возвращен", "returned"), ("Отменен", "refused"),
    ("Перемещение резерва (наличие)", "confirmed"), ("Зарезервирован к отгрузке", "confirmed"),
    ("Готов к отгрузке", "confirmed"), ("Отгружен", "in_transit"), ("Собран", "confirmed"),
    ("В сборке", "confirmed"), ("Обработан", "confirmed"), ("Не выполнен", "refused"),
    ("Позиция полностью поставлена", "arrived"), ("В работе", "confirmed"), ("На комплектации", "confirmed"),
    ("Подтвержден", "confirmed"), ("В пути", "in_transit"), ("Готов к транзиту", "confirmed"),
    ("Отменен (или отмена позиции)", "refused"), ("Заказан у поставщика", "confirmed"),
    ("Поступил в работу", "confirmed"), ("Упакован на складе заказа", "confirmed"),
    ("Собран на складе заказа", "confirmed"), ("Выкуплен", "confirmed"), ("Собирается", "confirmed"),
    ("отгружено", "in_transit"), ("комплектуется", "confirmed"), ("отклонено: Снятие резервирования", "refused"),
    ("Выдано", "arrived"), ("Готово к выдаче", "arrived"), ("Задерживается", "in_transit"),
])
def test_real_supplier_texts(text, code):
    assert normalize_status(text) == code


def test_returned_rows_are_returned(org_session):
    session, org = org_session
    line = _line(session, org, 0)
    apply_rows(session, org, "Армтек", [_row("A", "OC90", 1, "товар возвращен", True)])
    assert line.status == "returned"
