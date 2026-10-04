import unittest

from forum_auto import ForumAutoProvider


class ForumAutoProviderTests(unittest.TestCase):
    def test_requests_and_marks_crosses_when_enabled(self):
        provider = ForumAutoProvider("login", "password", include_crosses=True)
        calls = []

        def fake_get(method, params, encoding="utf-8"):
            calls.append((method, params))
            return [
                {"art": "A100", "price": 100, "num": 1},
                {"nr": "B200", "price": "110,5", "num": 1, "h_deliv": 6},
            ]

        provider._get = fake_get
        result = provider.get_prices("A-100")

        self.assertEqual(calls[0][1]["cross"], 1)
        self.assertFalse(result[0]["is_cross"])
        self.assertTrue(result[1]["is_cross"])

    def test_can_disable_crosses(self):
        provider = ForumAutoProvider("login", "password", include_crosses=False)
        provider._get = lambda method, params, encoding="utf-8": (
            self.assertEqual(params["cross"], 0) or []
        )
        provider.get_prices("A100")

    def test_uses_documented_rest_endpoint_case(self):
        provider = ForumAutoProvider("login", "password")

        class Response:
            status_code = 200
            text = "[]"

            def json(self):
                return []

        class Session:
            trust_env = True

            def get(self, url, **kwargs):
                self.url = url
                return Response()

        session = Session()
        import forum_auto
        old_session = forum_auto.requests.Session
        forum_auto.requests.Session = lambda: session
        try:
            provider._get("listGoods", {"art": "A100"})
        finally:
            forum_auto.requests.Session = old_session
        self.assertTrue(session.url.endswith("/listGoods"))

    def test_fault_error_is_exposed(self):
        provider = ForumAutoProvider("login", "password")
        provider._get = lambda method, params, encoding="utf-8": None
        provider.last_message = "Неверный логин/пароль"
        result = provider.add_to_basket({"gid": "143431W20"}, 1)
        self.assertFalse(result["success"])
        self.assertIn("Неверный", result["error"])

    def test_extracts_fault_code_message(self):
        provider = ForumAutoProvider("login", "password")
        self.assertIn("устарело", provider._extract_error({"FaultCode": 31, "FaultString": "old"}))

    def test_debug_summary_masks_credentials(self):
        provider = ForumAutoProvider("login", "password")
        provider._debug(f"login={provider.login}&pass={provider.password}")
        summary = provider.debug_summary()
        self.assertNotIn("password", summary)
        self.assertNotIn("login", summary)
        self.assertIn("***", summary)

    def test_retries_without_crosses_after_http_500(self):
        provider = ForumAutoProvider("login", "password", include_crosses=True)
        calls = []

        def fake_get(method, params, encoding="utf-8"):
            calls.append(params["cross"])
            if params["cross"] == 1:
                provider.last_http_status = 500
                provider.last_message = "HTTP 500"
                return None
            return [{"art": "A100", "price": 100, "num": 1}]

        provider._get = fake_get
        result = provider.get_prices("A100")

        self.assertEqual(calls, [1, 0])
        self.assertEqual(len(result), 1)

    def test_get_brand_candidates_uses_list_brands(self):
        provider = ForumAutoProvider("login", "password")
        calls = []

        def fake_get(method, params, encoding="utf-8"):
            calls.append((method, params))
            return {"data": [
                {"art": "OC90", "brand": "KNECHT", "name": "Фильтр"},
                {"art": "OC 90", "brand": "MAHLE", "name": "Фильтр"},
            ]}

        provider._get = fake_get
        result = provider.get_brand_candidates("OC90")

        self.assertEqual(calls[0][0], "listBrands")
        self.assertEqual(result[0]["brand"], "KNECHT")
        self.assertEqual(result[1]["article"], "OC 90")


if __name__ == "__main__":
    unittest.main()
