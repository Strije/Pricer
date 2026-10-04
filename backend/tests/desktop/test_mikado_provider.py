import unittest
import xml.etree.ElementTree as ET

from mikado import MikadoProvider, _encode_form_cp1251, _parse_xml, _sanitize_xml


class MikadoProviderTests(unittest.TestCase):
    def test_get_prices_with_brand_uses_code_brand_stock_info(self):
        provider = MikadoProvider("client", "password")
        calls = []

        def fake_post(url, action, params):
            calls.append((action, params))
            return ET.fromstring("""
                <CodeBrandResult>
                  <List>
                    <CodeBrandLine>
                      <OrderCode>xbs-OC90</OrderCode>
                      <PriceRUR>528.99</PriceRUR>
                      <Brand>KNECHT</Brand>
                      <Name>Фильтр масляный</Name>
                      <StokID>1</StokID>
                      <StokName>Основной</StokName>
                      <StockQTY>6</StockQTY>
                      <MinZakazQTY>1</MinZakazQTY>
                      <DeliveryDelay>0</DeliveryDelay>
                    </CodeBrandLine>
                  </List>
                </CodeBrandResult>
            """)

        provider._post = fake_post
        result = provider.get_prices("OC90", brand="KNECHT")

        self.assertEqual(calls[0][0], "CodeBrandStockInfo")
        self.assertEqual(calls[0][1]["Brand"], "KNECHT")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["zakaz_code"], "xbs-OC90")
        self.assertEqual(result[0]["quantity"], "6")
        self.assertFalse(result[0]["is_cross"])

    def test_brand_candidates_skip_analogs_and_other_articles(self):
        provider = MikadoProvider("client", "password")
        provider._post = lambda url, action, params: ET.fromstring("""
            <Code_SearchResult xmlns="http://mikado-parts.ru/service">
              <List>
                <Code_List_Row>
                  <ProducerBrand>MANN-FILTER</ProducerBrand>
                  <ProducerCode>W71275</ProducerCode>
                  <Name>Фильтр масляный</Name>
                  <CodeType>Original</CodeType>
                </Code_List_Row>
                <Code_List_Row>
                  <ProducerBrand>BOSCH</ProducerBrand>
                  <ProducerCode>W71275</ProducerCode>
                  <Name>Аналог</Name>
                  <CodeType>Analog</CodeType>
                </Code_List_Row>
                <Code_List_Row>
                  <ProducerBrand>LYNXAUTO</ProducerBrand>
                  <ProducerCode>LF1005</ProducerCode>
                  <Name>Другой артикул</Name>
                  <CodeType>Original</CodeType>
                </Code_List_Row>
              </List>
            </Code_SearchResult>
        """)

        result = provider.get_brand_candidates("W71275")

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["brand"], "MANN-FILTER")

    def test_form_encoding_uses_cp1251_for_cyrillic_notes(self):
        encoded = _encode_form_cp1251({"Notes": "склд", "ClientID": "123"})

        self.assertIn(b"Notes=%F1%EA%EB%E4", encoded)
        self.assertNotIn(b"%D1%81%D0%BA", encoded)


class MikadoXmlSanitizeTests(unittest.TestCase):
    def test_invalid_char_reference_no_longer_breaks_parsing(self):
        payload = b"<r><Name>Ss\xd0\xb5nsor&#4; ABS</Name></r>"

        with self.assertRaises(ET.ParseError):
            ET.fromstring(payload)

        root = _parse_xml(payload)
        self.assertIsNotNone(root)
        self.assertEqual(root.find("Name").text, "Ssеnsor  ABS")

    def test_raw_control_byte_is_stripped(self):
        root = _parse_xml("<r><Name>\x04Ролик</Name></r>".encode("utf-8"))

        self.assertIsNotNone(root)
        self.assertEqual(root.find("Name").text, " Ролик")

    def test_valid_references_are_preserved(self):
        sanitized = _sanitize_xml(b"<r><Name>caf&#233;&#10;&#x41;</Name></r>")

        self.assertEqual(sanitized, b"<r><Name>caf&#233;&#10;&#x41;</Name></r>")
        self.assertEqual(_parse_xml(sanitized).find("Name").text, "café\nA")

    def test_unrecoverable_xml_returns_none_instead_of_raising(self):
        self.assertIsNone(_parse_xml(b"<r><Name>no closing tag"))

    def test_search_rows_survive_invalid_char_reference(self):
        provider = MikadoProvider("client", "password")
        provider._post = lambda url, action, params: _parse_xml(
            """
            <Code_SearchResult xmlns="http://mikado-parts.ru/service">
              <List>
                <Code_List_Row>
                  <ZakazCode>xzk-ss-1359</ZakazCode>
                  <ProducerBrand>Zekkert</ProducerBrand>
                  <ProducerCode>ss1359</ProducerCode>
                  <Name>&#4;Стойка стабилизатора</Name>
                  <PriceRUR>394.61</PriceRUR>
                  <CodeType>Analog</CodeType>
                </Code_List_Row>
              </List>
            </Code_SearchResult>
            """.encode("utf-8")
        )

        result = provider.get_prices("0323FDRR")

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["zakaz_code"], "xzk-ss-1359")
        self.assertTrue(result[0]["is_cross"])


if __name__ == "__main__":
    unittest.main()
