import tempfile
import unittest
from pathlib import Path

from sales_report_importer import (
    merge_sales_report_rows,
    parse_quantity,
    parse_sales_report_rows,
    read_sales_report,
)


class SalesReportImporterTest(unittest.TestCase):
    def test_parses_csv_with_russian_headers_and_merges_duplicates(self):
        text = (
            "Бренд;Артикул;Название;Количество\n"
            "MANN-FILTER;W 712/95;Фильтр масляный;1\n"
            "MANN FILTER;W-712/95;Фильтр;2\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sales.csv"
            path.write_text(text, encoding="cp1251")

            rows, errors = read_sales_report(path)

        self.assertEqual(errors, [])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].article_key, "W71295")
        self.assertEqual(rows[0].quantity, 3)
        self.assertEqual(rows[0].source_rows, (2, 3))

    def test_parses_rows_without_header_by_default_columns(self):
        rows, errors = parse_sales_report_rows([
            ["BOSCH", "0 986 479 088", "Колодки", "1"],
        ])

        self.assertEqual(errors, [])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].brand, "BOSCH")
        self.assertEqual(rows[0].article_key, "0986479088")

    def test_detects_long_report_headers(self):
        rows, errors = parse_sales_report_rows([
            ["Производитель", "Артикул номенклатуры", "Наименование товара", "Количество продано"],
            ["LUK", "620 3119 00", "Комплект сцепления", "1"],
        ])

        self.assertEqual(errors, [])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].article_key, "620311900")

    def test_quantity_is_rounded_up_and_invalid_rows_reported(self):
        self.assertEqual(parse_quantity("1,2 шт"), 2)

        rows, errors = parse_sales_report_rows([
            ["Бренд", "Артикул", "Количество"],
            ["MANN", "", "1"],
            ["MANN", "W71295", "0"],
        ])

        self.assertEqual(rows, [])
        self.assertEqual(len(errors), 2)

    def test_merge_keeps_separate_empty_brand_and_named_brand(self):
        rows, errors = parse_sales_report_rows([
            ["Бренд", "Артикул", "Количество"],
            ["", "OC90", "1"],
            ["KNECHT", "OC90", "1"],
        ])

        merged = merge_sales_report_rows(rows)

        self.assertEqual(errors, [])
        self.assertEqual(len(merged), 2)


if __name__ == "__main__":
    unittest.main()
