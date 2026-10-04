import os
import tempfile
import unittest

from bulk_pricing import (
    brand_candidates,
    guess_layout,
    pretty_name,
    read_any,
    safe_save,
)

# Так выглядит выгрузка «Гиперсклад»: расширение .xls, внутри HTML в cp1251,
# заголовка нет, порядок колонок свой.
HTML_EXPORT = (
    "<html><head><meta http-equiv='Content-Type' content='text/html; charset=windows-1251'>"
    "</head><body><TABLE border=1>"
    "<TR><TD>STELLOX</TD><TD>0010098SX</TD><TD align=center>2</TD>"
    "<TD align=right>159,3&nbsp;</TD><TD class=ct>RUR</TD>"
    "<TD>ДАТЧИК ИЗНОСА КОЛОДОК</td><TD>ГС-2-2-К1&nbsp;</TD></TR>"
    "<TR><TD>MAHLE</TD><TD>01473N0</TD><TD align=center>1</TD>"
    "<TD align=right>2500&nbsp;</TD><TD class=ct>RUR</TD>"
    "<TD>КОЛЬЦА ПОРШНЕВЫЕ, КОМПЛЕКТ</td><TD>ГС-4-2-К2&nbsp;</TD></TR>"
    "</TABLE></body></html>"
)


class PrettyNameTests(unittest.TestCase):
    def test_lowers_caps_but_keeps_latin_and_indexes(self):
        self.assertEqual(pretty_name("ДАТЧИК ИЗНОСА КОЛОДОК"), "Датчик износа колодок")
        self.assertEqual(
            pretty_name("БЛОК РОЗЖИГА LEDO OXT6S D3"),
            "Блок розжига LEDO OXT6S D3",
        )

    def test_repairs_latin_homoglyphs_from_1c(self):
        # первая буква латинская P — с ней поиск по названию промахивается
        self.assertEqual(pretty_name("PОЛИКОПОДШИПНИК КОНИЧЕСКИЙ"), "Роликоподшипник конический")

    def test_keeps_punctuation_and_empty(self):
        self.assertEqual(pretty_name("КОЛЬЦА ПОРШНЕВЫЕ,  КОМПЛЕКТ"), "Кольца поршневые, комплект")
        self.assertEqual(pretty_name(None), "")


class BrandCandidatesTests(unittest.TestCase):
    def test_expands_truncated_brand(self):
        self.assertEqual(brand_candidates("GENERAL"), ["GENERAL", "GENERAL MOTORS", "GM"])

    def test_keeps_unknown_brand_as_is(self):
        self.assertEqual(brand_candidates("STELLOX"), ["STELLOX"])

    def test_does_not_duplicate_full_brand(self):
        self.assertEqual(brand_candidates("MERCEDES-BENZ"), ["MERCEDES-BENZ"])

    def test_empty_brand(self):
        self.assertEqual(brand_candidates(""), [])


class ReadAnyTests(unittest.TestCase):
    def _write(self, name, data, encoding="cp1251"):
        path = os.path.join(self.tmp.name, name)
        with open(path, "wb") as file:
            file.write(data.encode(encoding))
        return path

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_reads_html_disguised_as_xls(self):
        rows, header = read_any(self._write("sklad.xls", HTML_EXPORT))
        self.assertEqual(header, [])
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["brand"], "STELLOX")
        self.assertEqual(rows[0]["article"], "0010098SX")
        self.assertEqual(rows[0]["name"], "Датчик износа колодок")
        self.assertEqual(rows[0]["quantity"], 2)
        # исходные колонки сохраняются целиком, чтобы попасть в отчёт
        self.assertIn("ГС-2-2-К1", rows[0]["extras"])

    def test_manual_columns_override_guess(self):
        rows, _ = read_any(
            self._write("sklad.xls", HTML_EXPORT),
            columns="brand,article,quantity,-,-,name,-",
        )
        self.assertEqual(rows[1]["article"], "01473N0")
        self.assertEqual(rows[1]["quantity"], 1)

    def test_reads_csv_with_header(self):
        csv_text = "Бренд;Артикул;Наименование;Количество\nSTELLOX;0010098SX;ДАТЧИК;3\n"
        rows, header = read_any(self._write("list.csv", csv_text, encoding="utf-8"))
        self.assertEqual(header[0], "Бренд")
        self.assertEqual(rows[0]["quantity"], 3)
        self.assertEqual(rows[0]["name"], "Датчик")

    def test_skips_rows_without_article(self):
        broken = HTML_EXPORT.replace("<TD>01473N0</TD>", "<TD>   </TD>")
        rows, _ = read_any(self._write("broken.xls", broken))
        self.assertEqual(len(rows), 1)

    def test_raises_when_article_column_not_found(self):
        with self.assertRaises(ValueError):
            read_any(self._write("bad.csv", "a;b\n;\n", encoding="utf-8"), columns="name")


class GuessLayoutTests(unittest.TestCase):
    def test_finds_columns_without_header(self):
        rows = [
            ["STELLOX", "0010098SX", "2", "159,3", "RUR", "ДАТЧИК ИЗНОСА КОЛОДОК", "ГС-2-2-К1"],
            ["MAHLE", "01473N0", "1", "2500", "RUR", "КОЛЬЦА ПОРШНЕВЫЕ КОМПЛЕКТ", "ГС-4-2-К2"],
        ]
        mapping = guess_layout(rows)
        self.assertEqual(mapping["article"], 1)
        self.assertEqual(mapping["name"], 5)
        self.assertEqual(mapping["quantity"], 2)


class SafeSaveTests(unittest.TestCase):
    def test_falls_back_when_file_is_locked(self):
        from openpyxl import Workbook

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        target = os.path.join(tmp.name, "out.xlsx")

        class LockedWorkbook(Workbook):
            def save(self, path):
                if path == target:  # как Excel, держащий файл открытым
                    raise PermissionError(path)
                return super().save(path)

        saved = safe_save(LockedWorkbook(), target)
        self.assertEqual(saved, os.path.join(tmp.name, "out_2.xlsx"))
        self.assertTrue(os.path.exists(saved))


if __name__ == "__main__":
    unittest.main()
