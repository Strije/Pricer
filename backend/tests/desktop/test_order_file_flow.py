import re
import unittest
import datetime

import engine as main
from ._compat import SkitchenApp
from provider_health import ProviderCircuitBreaker
from provider_result_cache import ProviderResultCache


class _Aliases:
    def for_provider(self, brand, _provider):
        return brand

    def key(self, value):
        return re.sub(r"[^A-Z0-9]+", "", str(value or "").upper())

    def same(self, left, right):
        return self.key(left).replace("FILTER", "") == self.key(right).replace("FILTER", "")


class _FileOrderProvider:
    timeout = 3
    last_message = ""

    def get_prices(self, article):
        return [
            {
                "provider": "Test Supplier",
                "article": article,
                "brand": "MANN FILTER",
                "price": 520,
                "quantity": 5,
                "delivery_hours": 24,
                "warehouse": "Main",
                "goods_id": "g1",
                "warehouse_id": "w1",
            },
            {
                "provider": "Test Supplier",
                "article": article,
                "brand": "KNECHT",
                "price": 400,
                "quantity": 5,
                "delivery_hours": 12,
            },
        ]


class _BulkPackProvider:
    timeout = 3
    last_message = ""

    def get_prices(self, article):
        return [
            {
                "provider": "Test Supplier",
                "article": article,
                "brand": "MANN FILTER",
                "price": 100,
                "quantity": 100,
                "minimum_quantity": 24,
                "multiplicity": 1,
                "delivery_hours": 24,
            },
            {
                "provider": "Test Supplier",
                "article": article,
                "brand": "MANN FILTER",
                "price": 180,
                "quantity": 5,
                "minimum_quantity": 1,
                "multiplicity": 1,
                "delivery_hours": 72,
            },
        ]


class MikadoProvider:
    """Двойник поставщика, запоминающий бренд запроса.

    Имя класса важно: _query_provider выбирает способ вызова по нему.
    """

    timeout = 3
    last_message = ""

    def __init__(self):
        self.asked_brands = []

    def get_prices(self, article, brand=""):
        self.asked_brands.append(brand)
        return [
            {
                "provider": "Mikado",
                "article": article,
                "brand": "MANN FILTER",
                "price": 500,
                "quantity": 5,
                "delivery_hours": 24,
                "warehouse": "Main",
                "zakaz_code": "z1",
            }
        ]


class OrderFileFlowTest(unittest.TestCase):
    def _app(self):
        app = SkitchenApp.__new__(SkitchenApp)
        app.providers = [_FileOrderProvider()]
        app.brand_aliases = _Aliases()
        app.provider_circuit = ProviderCircuitBreaker(threshold=3, cooldown_seconds=120)
        app.provider_result_cache = ProviderResultCache(ttl_seconds=300, max_entries=20)
        app._selected_brand_variants = {}
        app.provider_filters = {}
        app.hide_no_return = False
        app._detail_log = lambda *args, **kwargs: None
        app._format_seconds = lambda value: str(value)
        app._provider_cart_state = lambda provider: ("order", "") if provider == "Test Supplier" else ("disabled", "")
        app._missing_cart_fields = lambda provider, item: []
        app._filter_provider_item = lambda provider, item, **_kwargs: (True, "")
        app._apply_warehouse_extra_days = lambda provider, item: None
        app.apply_markup = lambda price: (round(float(price) * 1.2, 2), 20, None)
        return app

    def test_order_file_row_uses_own_brand_not_brand_from_previous_search(self):
        """Регресс: бренд из обычного поиска подставлялся во все строки файла.

        После поиска ADDINOL заказ из 45 строк ушёл поставщикам под этим брендом,
        и Mikado, Armtek, ABSTD и Profit-League не нашли вообще ничего.
        """
        app = self._app()
        provider = MikadoProvider()
        app.providers = [provider]
        # остаток от предыдущего интерактивного поиска
        app._selected_brand_variants = {"MikadoProvider": "ADDINOL"}

        app._search_order_file_row(
            {
                "brand": "MANN-FILTER",
                "article": "W 712/95",
                "quantity": 1,
                "article_key": "W71295",
            },
            {"strategy": "price", "max_days": None},
        )

        self.assertTrue(provider.asked_brands)
        self.assertNotIn("ADDINOL", provider.asked_brands)
        self.assertEqual(provider.asked_brands[0], "MANN-FILTER")

    def test_all_warehouses_option_bypasses_warehouse_filters(self):
        """Галочка «Все склады» нужна для массовой закупки на витрину."""
        app = self._app()
        seen = []

        def fake_filter(provider, item, *, include_no_return=False, ignore_warehouse_filters=False):
            seen.append(ignore_warehouse_filters)
            # настройки складов прячут всё, кроме случая с обходом
            return (True, "") if ignore_warehouse_filters else (False, "склад")

        app._filter_provider_item = fake_filter
        row = {"brand": "MANN-FILTER", "article": "W 712/95", "quantity": 1, "article_key": "W71295"}

        without = app._search_order_file_row(row, {"strategy": "price", "max_days": None})
        self.assertEqual(without["status_code"], "not_found")
        self.assertEqual(seen[-1], False)

        with_bypass = app._search_order_file_row(
            row, {"strategy": "price", "max_days": None, "ignore_warehouse_filters": True}
        )
        self.assertEqual(with_bypass["status_code"], "ready")
        self.assertEqual(seen[-1], True)

    def test_recheck_drops_foreign_offers_before_normalising(self):
        """Перепроверка не должна нормализовать всё подряд.

        185 позиций прогоняли через нормализацию 12 190 предложений и занимали
        около 200 секунд из пятиминутного окна годности проверки.
        """
        app = self._app()
        item = {"article": "W 712/95", "brand": "MANN-FILTER"}
        keep = app._order_item_identity_filter(item)

        self.assertTrue(keep({"article": "W-712/95", "brand": "MANN FILTER"}))
        self.assertFalse(keep({"article": "OP570", "brand": "MANN FILTER"}))
        self.assertFalse(keep({"article": "W71295", "brand": "KNECHT"}))

    def test_recheck_identity_filter_is_skipped_without_article_and_brand(self):
        app = self._app()
        self.assertIsNone(app._order_item_identity_filter({}))
        self.assertIsNone(app._order_item_identity_filter(None))

    def test_search_order_file_row_selects_exact_orderable_offer(self):
        app = self._app()

        result = app._search_order_file_row(
            {
                "brand": "MANN-FILTER",
                "article": "W 712/95",
                "quantity": 2,
                "article_key": "W71295",
            },
            {"strategy": "price", "max_days": None},
        )

        self.assertEqual(result["status_code"], "ready")
        self.assertEqual(result["quantity"], 2)
        self.assertEqual(result["offer"]["brand"], "MANN FILTER")
        self.assertEqual(result["offer"]["sale_price"], 624.0)
        self.assertEqual(result["offer"]["verification_status"], "valid")
        self.assertTrue(result["offer"]["last_checked_at"])
        self.assertEqual(len(result["alternatives"]), 1)

    def test_search_order_file_row_can_allow_non_exact_options(self):
        app = self._app()

        result = app._search_order_file_row(
            {
                "brand": "MANN-FILTER",
                "article": "W 712/95",
                "quantity": 2,
                "article_key": "W71295",
            },
            {"strategy": "price", "max_days": None, "exact_match": False},
        )

        self.assertEqual(result["status_code"], "ready")
        self.assertEqual(result["status"], "Подобран вариант")
        self.assertEqual(result["offer"]["brand"], "KNECHT")
        self.assertEqual(result["offer"]["sale_price"], 480.0)

    def test_search_order_file_row_does_not_choose_bulk_pack_for_fastest_strategy(self):
        app = self._app()
        app.providers = [_BulkPackProvider()]

        result = app._search_order_file_row(
            {
                "brand": "MANN-FILTER",
                "article": "W 712/95",
                "quantity": 2,
                "article_key": "W71295",
            },
            {"strategy": "fastest", "max_days": None},
        )

        self.assertEqual(result["status_code"], "ready")
        self.assertEqual(result["offer"]["price"], 180.0)
        self.assertEqual(result["offer"]["actual_order_quantity"], 2)
        self.assertEqual(result["quantity"], 2)
        self.assertEqual([offer["actual_order_quantity"] for offer in result["alternatives"]], [2, 24])

    def test_prepared_order_file_entry_keeps_requested_quantity_separate_from_actual(self):
        app = self._app()
        row = {
            "source": {
                "brand": "MANN-FILTER",
                "article": "W 712/95",
                "quantity": 2,
            },
            "quantity": 24,
            "offer": {
                "internal_offer_id": "bulk-1",
                "provider": "Test Supplier",
                "brand": "MANN FILTER",
                "article": "W71295",
                "price": 100,
                "quantity": 100,
                "minimum_quantity": 24,
                "quantity_step": 1,
                "actual_order_quantity": 24,
                "requested_quantity": 2,
            },
        }

        entry, error = app._prepared_order_file_entry(row)

        self.assertEqual(error, "")
        self.assertEqual(entry["qty"], 24)
        self.assertEqual(entry["item"]["requested_quantity"], 2)
        self.assertEqual(entry["item"]["actual_order_quantity"], 24)

    def test_order_file_reason_does_not_prefer_local_reject_when_api_offer_exists(self):
        app = self._app()

        reason = app._order_file_reason(
            {"exact_count": 8, "accepted_count": 8, "raw_count": 15},
            {"local": 7, "disabled": 0, "missing_fields": 0, "quantity": 0},
            [],
            False,
            exact_match=True,
            selectable_count=1,
        )

        self.assertEqual(reason, "точных предложений: 8")

    def test_order_file_reason_explains_quantity_rejects(self):
        app = self._app()

        reason = app._order_file_reason(
            {"exact_count": 56, "accepted_count": 56, "raw_count": 339},
            {
                "local": 0,
                "disabled": 0,
                "missing_fields": 0,
                "quantity": 56,
                "requested_quantity": 2,
                "max_available_quantity": 1,
            },
            [],
            False,
            exact_match=True,
            selectable_count=0,
        )

        self.assertIn("точных предложений: 56", reason)
        self.assertIn("нужно 2", reason)
        self.assertIn("максимум 1", reason)

    def test_order_file_reason_explains_avtoto_missing_part_id(self):
        app = self._app()

        reason = app._order_file_reason(
            {"exact_count": 1, "accepted_count": 1, "raw_count": 3},
            {
                "local": 0,
                "disabled": 0,
                "missing_fields": 1,
                "missing_fields_map": {"part_id": 1},
                "quantity": 0,
            },
            [],
            False,
            exact_match=True,
            selectable_count=0,
        )

        self.assertIn("Avtoto", reason)
        self.assertIn("part_id", reason)
        self.assertIn("API-заказа", reason)

    def test_search_order_file_row_requires_brand(self):
        app = self._app()

        result = app._search_order_file_row(
            {"brand": "", "article": "W 712/95", "quantity": 1, "article_key": "W71295"},
            {"strategy": "price", "max_days": None},
        )

        self.assertEqual(result["status_code"], "needs_review")

    def test_recheck_prefers_avtoto_part_id_over_ambiguous_supplier_offer_id(self):
        app = self._app()
        order_item = {
            "provider": "Avtoto",
            "brand": "FEBI",
            "article": "101171",
            "supplier_offer_id": "1",
            "search": {
                "requested_article": "101171",
                "selected_brand": "FEBI",
                "exact_match": True,
            },
            "snapshot": {
                "part_id": "77",
                "brand": "FEBI",
                "article": "101171",
            },
        }
        offers = [
            {
                "provider": "Avtoto",
                "brand": "BOSCH",
                "article": "1457429249",
                "supplier_offer_id": "1",
                "part_id": "bad",
                "is_cross": True,
                "price": 391,
                "available_quantity": 10,
                "delivery_hours": 0,
            },
            {
                "provider": "Avtoto",
                "brand": "FEBI",
                "article": "101171",
                "supplier_offer_id": "77",
                "part_id": "77",
                "is_cross": False,
                "price": 2502,
                "available_quantity": 5,
                "delivery_hours": 72,
            },
        ]

        selected = app._match_order_offer(order_item, offers)

        self.assertEqual(selected["part_id"], "77")

    def test_recheck_rejects_cross_for_exact_file_order(self):
        app = self._app()
        order_item = {
            "provider": "Avtoto",
            "brand": "FEBI",
            "article": "101171",
            "supplier_offer_id": "1",
            "search": {
                "requested_article": "101171",
                "selected_brand": "FEBI",
                "exact_match": True,
            },
        }
        offers = [
            {
                "provider": "Avtoto",
                "brand": "FEBI",
                "article": "101171",
                "supplier_offer_id": "1",
                "part_id": "bad",
                "is_cross": True,
                "price": 2000,
                "available_quantity": 5,
                "delivery_hours": 24,
            },
        ]

        self.assertIsNone(app._match_order_offer(order_item, offers))

    def test_recheck_does_not_match_reused_supplier_id_to_different_article(self):
        app = self._app()
        order_item = {
            "provider": "Avtoto",
            "brand": "BIG FILTER",
            "article": "GB1219",
            "supplier_offer_id": "1103",
            "snapshot": {
                "brand": "BIG FILTER",
                "article": "GB1219",
                "part_id": "1103",
            },
        }
        offers = [
            {
                "provider": "Avtoto",
                "brand": "BOSCH",
                "article": "1457429249",
                "supplier_offer_id": "1103",
                "part_id": "1103",
                "is_cross": False,
                "price": 391,
                "available_quantity": 60,
                "delivery_hours": 120,
            },
        ]

        self.assertIsNone(app._match_order_offer(order_item, offers))

    def test_recheck_rejects_cross_when_original_item_was_exact(self):
        app = self._app()
        order_item = {
            "provider": "Avtoto",
            "brand": "BOSCH",
            "article": "1987301012",
            "supplier_offer_id": "420",
            "snapshot": {
                "brand": "BOSCH",
                "article": "1987301012",
                "part_id": "420",
                "is_cross": False,
            },
        }
        offers = [
            {
                "provider": "Avtoto",
                "brand": "BOSCH",
                "article": "1987301012",
                "supplier_offer_id": "420",
                "part_id": "420",
                "is_cross": True,
                "offer_relation": "cross",
                "price": 242,
                "available_quantity": 100,
                "delivery_hours": 96,
            },
        ]

        self.assertIsNone(app._match_order_offer(order_item, offers))
        self.assertEqual(
            app._order_recheck_failure_reason(order_item, offers),
            "поставщик вернул аналог/кросс вместо выбранного товара",
        )

    def test_submit_preflight_rejects_snapshot_for_different_article(self):
        app = self._app()
        order_item = {
            "provider": "Avtoto",
            "brand": "FEBI",
            "article": "101171",
            "quantity": 2,
            "verification_status": "valid",
            "search": {"requested_article": "101171", "selected_brand": "FEBI", "exact_match": True},
            "snapshot": {
                "part_id": "wrong-id",
                "brand": "FEBI",
                "article": "999999",
                "available_quantity": 5,
            },
        }

        send_item = app._order_item_for_submit(order_item)
        allowed, reason = app._order_submit_preflight(order_item, send_item, 2)

        self.assertFalse(allowed)
        self.assertEqual(reason, "сохранённый API-id относится к другому артикулу")

    def test_submit_preflight_rejects_cross_for_exact_file_order(self):
        app = self._app()
        order_item = {
            "provider": "Avtoto",
            "brand": "FEBI",
            "article": "101171",
            "quantity": 1,
            "verification_status": "valid",
            "search": {"requested_article": "101171", "selected_brand": "FEBI", "exact_match": True},
            "snapshot": {
                "part_id": "cross-id",
                "brand": "FEBI",
                "article": "101171",
                "is_cross": True,
                "available_quantity": 5,
            },
        }

        send_item = app._order_item_for_submit(order_item)
        allowed, reason = app._order_submit_preflight(order_item, send_item, 1)

        self.assertFalse(allowed)
        self.assertEqual(reason, "сохранённый API-id относится к аналогу/кроссу")

    def test_submit_preflight_rejects_quantity_changed_after_check(self):
        app = self._app()
        order_item = {
            "provider": "Test Supplier",
            "brand": "MANN FILTER",
            "article": "W71295",
            "quantity": 2,
            "requested_quantity": 2,
            "verification_status": "valid",
            "search": {"requested_article": "W71295", "selected_brand": "MANN FILTER", "exact_match": True},
            "snapshot": {
                "brand": "MANN FILTER",
                "article": "W71295",
                "available_quantity": 50,
                "minimum_quantity": 24,
                "quantity_step": 1,
            },
        }

        send_item = app._order_item_for_submit(order_item)
        allowed, reason = app._order_submit_preflight(order_item, send_item, 2)

        self.assertFalse(allowed)
        self.assertIn("количество изменилось после проверки", reason)

    def test_recheck_allows_same_cross_when_original_item_was_cross(self):
        app = self._app()
        order_item = {
            "provider": "Avtoto",
            "brand": "BOSCH",
            "article": "1987301012",
            "supplier_offer_id": "420",
            "snapshot": {
                "brand": "BOSCH",
                "article": "1987301012",
                "part_id": "420",
                "is_cross": True,
                "offer_relation": "cross",
            },
        }
        offers = [
            {
                "provider": "Avtoto",
                "brand": "BOSCH",
                "article": "1987301012",
                "supplier_offer_id": "420",
                "part_id": "420",
                "is_cross": True,
                "offer_relation": "cross",
                "price": 242,
                "available_quantity": 100,
                "delivery_hours": 96,
            },
        ]

        selected = app._match_order_offer(order_item, offers)

        self.assertIsNotNone(selected)
        self.assertEqual(selected["supplier_offer_id"], "420")

    @staticmethod
    def _old_order(seconds):
        old_time = (
            datetime.datetime.now() - datetime.timedelta(seconds=seconds)
        ).isoformat(timespec="seconds")
        return {
            "status": "draft",
            "verification_status": "valid",
            "verified_at": old_time,
            "items": [
                {
                    "submit_status": "not_submitted",
                    "verification_status": "valid",
                }
            ],
        }

    def test_old_verified_order_stays_valid_while_limit_disabled(self):
        # Лимит по времени выключен (ORDER_VERIFICATION_TTL_SECONDS = 0):
        # большой заказ не успевал пройти просмотр за окно TTL и уходил в цикл
        # перепроверок. Расхождение по цене и остатку теперь ловим по ответу
        # поставщика, а не запретом на отправку.
        app = self._app()
        order = self._old_order(24 * 60 * 60)

        result = app._mark_order_stale_if_expired(order)

        self.assertEqual(result["verification_status"], "valid")
        self.assertEqual(result["items"][0]["verification_status"], "valid")

    def test_verified_order_becomes_stale_after_ttl(self):
        # Сам механизм протухания оставлен рабочим: если лимит снова включат
        # настройкой, старый снимок должен уводить заказ в перепроверку.
        app = self._app()
        original_ttl = main.ORDER_VERIFICATION_TTL_SECONDS
        main.ORDER_VERIFICATION_TTL_SECONDS = 10 * 60
        try:
            order = self._old_order(main.ORDER_VERIFICATION_TTL_SECONDS + 1)
            result = app._mark_order_stale_if_expired(order)
        finally:
            main.ORDER_VERIFICATION_TTL_SECONDS = original_ttl

        self.assertEqual(result["verification_status"], "stale")
        self.assertEqual(result["items"][0]["verification_status"], "stale")

    def test_entries_verified_at_requires_valid_items(self):
        app = self._app()
        fresh_time = datetime.datetime.now().isoformat(timespec="seconds")
        old_time = (
            datetime.datetime.now() - datetime.timedelta(hours=5)
        ).isoformat(timespec="seconds")

        self.assertEqual(
            app._entries_verified_at([
                {"item": {"verification_status": "valid", "last_checked_at": fresh_time}},
            ]),
            fresh_time,
        )
        # Пока лимит выключен, возраст снимка сам по себе проверку не отменяет.
        self.assertEqual(
            app._entries_verified_at([
                {"item": {"verification_status": "valid", "last_checked_at": old_time}},
            ]),
            old_time,
        )
        self.assertEqual(
            app._entries_verified_at([
                {"item": {"verification_status": "valid", "last_checked_at": ""}},
            ]),
            "",
        )
        self.assertEqual(
            app._entries_verified_at([
                {"item": {"verification_status": "stale", "last_checked_at": fresh_time}},
            ]),
            "",
        )

    def test_entries_verified_at_drops_expired_items_when_limit_enabled(self):
        app = self._app()
        original_ttl = main.ORDER_VERIFICATION_TTL_SECONDS
        main.ORDER_VERIFICATION_TTL_SECONDS = 10 * 60
        try:
            old_time = (
                datetime.datetime.now()
                - datetime.timedelta(seconds=main.ORDER_VERIFICATION_TTL_SECONDS + 1)
            ).isoformat(timespec="seconds")
            result = app._entries_verified_at([
                {"item": {"verification_status": "valid", "last_checked_at": old_time}},
            ])
        finally:
            main.ORDER_VERIFICATION_TTL_SECONDS = original_ttl

        self.assertEqual(result, "")



class RecheckStrategyTests(unittest.TestCase):
    """Перепроверка обязана выбирать по той же стратегии, что и подбор.

    Живой случай 18.08.2026: заказ собирали по минимальной цене, а перепроверка
    ранжировала по сроку и подменила 17 позиций на более быстрые и дорогие —
    плюс 3142 рубля к закупке.
    """

    def _app(self):
        app = SkitchenApp.__new__(SkitchenApp)
        return app

    def _offers(self):
        return [
            {"provider": "Avtoto", "brand": "MANN", "article": "W7015",
             "price": 475, "purchase_price": 475, "delivery_hours": 24, "available_quantity": 5,
             "supplier_offer_id": "184"},
            {"provider": "Avtoto", "brand": "MANN", "article": "W7015",
             "price": 380, "purchase_price": 380, "delivery_hours": 48, "available_quantity": 5,
             "supplier_offer_id": "38"},
        ]

    def test_price_strategy_keeps_the_cheapest_offer(self):
        app = self._app()

        best = app._best_recheck_offer(self._offers(), "price")

        self.assertEqual(best["supplier_offer_id"], "38")
        self.assertEqual(best["price"], 380)

    def test_fastest_strategy_still_prefers_the_shorter_term(self):
        app = self._app()

        best = app._best_recheck_offer(self._offers(), "fastest")

        self.assertEqual(best["supplier_offer_id"], "184")

    def test_default_without_strategy_is_the_cheapest(self):
        app = self._app()

        self.assertEqual(app._best_recheck_offer(self._offers())["price"], 380)

    def test_delivery_limit_from_the_search_is_respected(self):
        # Лимит срока был жёстким условием при подборе: выбор по цене
        # не должен его нарушать.
        app = self._app()
        offers = self._offers() + [{
            "provider": "Avtoto", "brand": "MANN", "article": "W7015",
            "price": 200, "purchase_price": 200, "delivery_hours": 240,
            "available_quantity": 5, "supplier_offer_id": "999",
        }]

        best = app._best_recheck_offer(offers, "price", max_hours=72)

        self.assertEqual(best["supplier_offer_id"], "38")

    def test_limit_is_ignored_when_nothing_fits_it(self):
        app = self._app()
        offers = [{
            "provider": "Avtoto", "brand": "MANN", "article": "W7015",
            "price": 200, "purchase_price": 200, "delivery_hours": 240,
            "available_quantity": 5, "supplier_offer_id": "999",
        }]

        best = app._best_recheck_offer(offers, "price", max_hours=72)

        self.assertEqual(best["supplier_offer_id"], "999")

    def test_limit_is_read_from_the_order_item(self):
        app = self._app()

        self.assertEqual(app._order_item_max_hours({"search": {"max_days": 3}}), 72)
        self.assertEqual(app._order_item_max_hours({"snapshot": {"selection_max_days": 2}}), 48)
        self.assertIsNone(app._order_item_max_hours({"search": {"max_days": None}}))
        self.assertIsNone(app._order_item_max_hours({}))

    def test_strategy_is_read_from_the_order_item(self):
        app = self._app()

        self.assertEqual(app._order_item_strategy({"search": {"strategy": "fastest"}}), "fastest")
        self.assertEqual(
            app._order_item_strategy({"snapshot": {"selection_strategy": "fastest"}}),
            "fastest",
        )
        # Старые заказы стратегию не хранят — там минимальная цена, как в подборе.
        self.assertEqual(app._order_item_strategy({"search": {}}), "price")
        self.assertEqual(app._order_item_strategy({}), "price")

    def test_order_item_records_the_strategy_it_was_picked_with(self):
        from order_store import OrderStore

        item = OrderStore._item_from_entry({
            "item": {
                "provider": "Avtoto", "brand": "MANN", "article": "W7015",
                "price": 380, "quantity": "5", "selection_strategy": "fastest",
            },
            "qty": 1,
        })

        self.assertEqual(item["search"]["strategy"], "fastest")
        self.assertEqual(OrderStore._item_summary(item)["search"]["strategy"], "fastest")


if __name__ == "__main__":
    unittest.main()
