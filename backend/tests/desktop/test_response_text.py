import unittest

from response_text import decode_response_text


class FakeResponse:
    def __init__(self, content, encoding=None):
        self.content = content
        self.encoding = encoding


class ResponseTextTests(unittest.TestCase):
    def test_fixes_utf8_read_as_cp1251(self):
        response = FakeResponse("Заказ №273785023".encode("utf-8"), encoding="cp1251")

        self.assertEqual(decode_response_text(response), "Заказ №273785023")

    def test_decodes_cp1251_response(self):
        response = FakeResponse("Ошибка авторизации".encode("cp1251"), encoding="ISO-8859-1")

        self.assertEqual(decode_response_text(response), "Ошибка авторизации")


if __name__ == "__main__":
    unittest.main()
