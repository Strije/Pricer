import re
import requests
import xml.etree.ElementTree as ET
from urllib.parse import urlencode

NS = {"ns": "http://mikado-parts.ru/service"}
NS_BASKET = {"ns": "http://mikado-parts.ru/ws1/"}

class MikadoProvider:
    def __init__(self, client_id, password, timeout=10):
        self.client_id = client_id
        self.password = password
        self.timeout = int(timeout)
        self.service_url = "http://www.mikado-parts.ru/ws1/service.asmx"
        self.basket_url = "http://www.mikado-parts.ru/ws1/basket.asmx"

    def _post(self, url, action, params):
        session = requests.Session()
        session.trust_env = False
        resp = session.post(
            f"{url}/{action}",
            data=_encode_form_cp1251(params),
            headers={"Content-Type": "application/x-www-form-urlencoded; charset=windows-1251"},
            timeout=self.timeout,
            proxies={"http": None, "https": None},
        )
        if resp.status_code != 200:
            return None
        return _parse_xml(resp.content)

    def get_prices(self, article, brand=""):
        if brand:
            root = self._post(self.service_url, "CodeBrandStockInfo", {
                "Code": article,
                "Brand": brand,
                "ClientID": self.client_id,
                "Password": self.password,
            })
            if root is None:
                return []
            return self._parse_brand_stock(root, article, brand)

        root = self._post(self.service_url, "Code_Search", {
            "Search_Code": article,
            "ClientID": self.client_id,
            "Password": self.password,
            "FromStockOnly": "FromStockAndByOrder",
        })
        if root is None:
            return []
        return self._parse_search(root)

    def get_brand_candidates(self, article):
        root = self._post(self.service_url, "Code_Search", {
            "Search_Code": article,
            "ClientID": self.client_id,
            "Password": self.password,
            "FromStockOnly": "FromStockAndByOrder",
        })
        if root is None:
            return []
        result = []
        seen = set()
        list_el = root.find(".//ns:List", NS)
        if list_el is None:
            return result
        requested_key = _clean_code(article)
        for row in list_el.findall("ns:Code_List_Row", NS):
            brand = _txt(row.find("ns:ProducerBrand", NS)) or _txt(row.find("ns:Brand", NS))
            if not brand:
                continue
            producer_code = _txt(row.find("ns:ProducerCode", NS)) or article
            name = _txt(row.find("ns:Name", NS))
            code_type = _txt(row.find("ns:CodeType", NS))
            if code_type in ("Analog", "AnalogOEM"):
                continue
            if requested_key and _clean_code(producer_code) != requested_key:
                continue
            zakaz = _txt(row.find("ns:ZakazCode", NS))
            marker = (brand.upper(), _clean_code(producer_code), name.upper(), code_type)
            if marker in seen:
                continue
            seen.add(marker)
            result.append({
                "brand": brand,
                "article": producer_code,
                "name": name,
                "source": "Code_Search",
                "order_code": zakaz,
                "CodeType": code_type,
                "is_cross": code_type in ("Analog", "AnalogOEM"),
            })
        return result

    def get_brands(self, article):
        brands = []
        for item in self.get_brand_candidates(article):
            brand = item.get("brand")
            if brand and brand not in brands:
                brands.append(brand)
        return brands

    def _parse_search(self, root):
        results = []
        list_el = root.find(".//ns:List", NS)
        if list_el is None:
            return results
        for row in list_el.findall("ns:Code_List_Row", NS):
            zakaz = _txt(row.find("ns:ZakazCode", NS))
            brand = _txt(row.find("ns:ProducerBrand", NS)) or _txt(row.find("ns:Brand", NS))
            producer_code = _txt(row.find("ns:ProducerCode", NS))
            article_val = producer_code or ""
            name = _txt(row.find("ns:Name", NS))
            price = _float(row.find("ns:PriceRUR", NS))
            code_type = _txt(row.find("ns:CodeType", NS))
            source_el = row.find("ns:Source", NS)
            source_brand = _txt(source_el.find("ns:SourceProducer", NS)) if source_el is not None else ""
            source_code = _txt(source_el.find("ns:SourceCode", NS)) if source_el is not None else ""
            min_qty_str = _txt(row.find("ns:MinZakazQTY", NS)) or "1"
            srock = _txt(row.find("ns:Srock", NS))
            row_days = _parse_days(srock)
            stocks_el = row.find("ns:OnStocks", NS)
            stock_lines = []
            if stocks_el is not None:
                for sl in stocks_el.findall("ns:StockLine", NS):
                    qty_str = _txt(sl.find("ns:StockQTY", NS)) or "0"
                    wh = _txt(sl.find("ns:StokName", NS)) or "-"
                    stock_id = _txt(sl.find("ns:StokID", NS))
                    delivery_delay = _txt(sl.find("ns:DeliveryDelay", NS))
                    stock_lines.append((qty_str, wh, stock_id, delivery_delay))
            if not stock_lines:
                stock_lines = [("0", "-", "1", "")]

            for qty_str, warehouse, stock_id, delivery_delay in stock_lines:
                days = _parse_days(delivery_delay)
                if days == 0:
                    days = row_days
                results.append({
                    "provider": "Mikado",
                    "brand": brand,
                    "article": article_val,
                    "price": price,
                    "days": days,
                    "quantity": qty_str,
                    "logo": warehouse,
                    "warehouse": warehouse,
                    "stock_id": stock_id or "1",
                    "delivery_delay": delivery_delay,
                    "name": name,
                    "zakaz_code": zakaz,
                    "multiplicity": _int(min_qty_str) if min_qty_str.isdigit() else 1,
                    "code_type": code_type,
                    "is_cross": code_type in ("Analog", "AnalogOEM"),
                    "source_brand": source_brand,
                    "source_code": source_code,
                })
        return results

    def _parse_brand_stock(self, root, requested_article, requested_brand):
        results = []
        list_el = _find_descendant(root, "List")
        if list_el is None:
            return results
        rows = [child for child in list(list_el) if _local_name(child.tag) in ("CodeBrandLine", "Code_List_Row")]
        if not rows:
            rows = list(list_el)
        for row in rows:
            zakaz = _child_text(row, "OrderCode", "ZakazCode")
            brand = _child_text(row, "Brand", "ProducerBrand") or requested_brand
            name = _child_text(row, "Name")
            price = _float_text(_child_text(row, "PriceRUR"))
            qty_str = _child_text(row, "StockQTY") or "0"
            warehouse = _child_text(row, "StokName", "StockName") or "-"
            stock_id = _child_text(row, "StokID", "StockID") or "1"
            delivery_delay = _child_text(row, "DeliveryDelay")
            min_qty_str = _child_text(row, "MinZakazQTY") or "1"
            results.append({
                "provider": "Mikado",
                "brand": brand,
                "article": requested_article,
                "price": price,
                "days": _parse_days(delivery_delay),
                "quantity": qty_str,
                "logo": warehouse,
                "warehouse": warehouse,
                "stock_id": stock_id,
                "delivery_delay": delivery_delay,
                "name": name,
                "zakaz_code": zakaz,
                "multiplicity": max(1, _int(min_qty_str)),
                "code_type": "Exact",
                "is_cross": False,
                "source_brand": "",
                "source_code": "",
            })
        return results

    def add_to_basket(self, item, quantity=1, comment=""):
        zakaz_code = item.get("zakaz_code", "")
        if not zakaz_code:
            return {"success": False, "error": "Нет zakaz_code"}
        root = self._post(self.basket_url, "Basket_Add", {
            "ZakazCode": zakaz_code,
            "QTY": str(quantity),
            "DeliveryType": "0",
            "Notes": comment,
            "ClientID": self.client_id,
            "Password": self.password,
            "ExpressID": "0",
            "StockID": str(item.get("stock_id", "1") or "1"),
        })
        if root is None:
            return {"success": False, "error": "HTTP error"}
        msg = _txt(root.find(".//ns:Message", NS_BASKET))
        if msg == "OK":
            id_el = root.find(".//ns:ID", NS_BASKET)
            return {"success": True, "data": {"id": _txt(id_el) if id_el is not None else ""}}
        return {"success": False, "error": msg or "Unknown error"}


def _txt(el):
    return el.text.strip() if el is not None and el.text else ""

def _encode_form_cp1251(params):
    return urlencode(params or {}, encoding="cp1251", errors="replace").encode("ascii")

# Микадо иногда отдаёт в наименованиях управляющие символы: и ссылками (&#4;),
# и сырыми байтами. Для XML 1.0 это недопустимо, и весь ответ переставал разбираться.
_CHAR_REF = re.compile(rb"&#(?:[xX][0-9a-fA-F]+|[0-9]+);")
_RAW_CONTROL = re.compile(rb"[\x00-\x08\x0b\x0c\x0e-\x1f]")

def _is_xml_char(code):
    return (
        code in (0x09, 0x0A, 0x0D)
        or 0x20 <= code <= 0xD7FF
        or 0xE000 <= code <= 0xFFFD
        or 0x10000 <= code <= 0x10FFFF
    )

def _sanitize_xml(payload):
    """Вырезает символы, недопустимые в XML, оставляя остальной ответ нетронутым."""
    def replace_ref(match):
        body = match.group(0)[2:-1].decode("ascii", "ignore")
        try:
            code = int(body[1:], 16) if body[:1] in ("x", "X") else int(body)
        except ValueError:
            return b" "
        return match.group(0) if _is_xml_char(code) else b" "

    return _RAW_CONTROL.sub(b" ", _CHAR_REF.sub(replace_ref, payload))

def _parse_xml(payload):
    """Разбор ответа: одна битая строка не должна ронять весь поиск."""
    try:
        return ET.fromstring(payload)
    except ET.ParseError:
        pass
    try:
        return ET.fromstring(_sanitize_xml(payload))
    except ET.ParseError:
        return None

def _float(el):
    if el is not None and el.text:
        try:
            return float(el.text)
        except:
            return 0.0
    return 0.0

def _float_text(value):
    try:
        return float(str(value or "").replace(",", "."))
    except:
        return 0.0

def _int(s):
    try:
        return int(s)
    except:
        return 0

def _parse_days(value):
    value = str(value or "").strip()
    if not value or value == "?":
        return 0
    if "-" in value:
        parts = [part.strip() for part in value.split("-") if part.strip()]
        parsed = [_int(part) for part in parts]
        parsed = [part for part in parsed if part >= 0]
        return max(parsed) if parsed else 0
    return _int(value)

def _parse_qty(qty_str):
    qty_str = qty_str.strip()
    if qty_str.endswith("+"):
        try:
            return int(qty_str.rstrip("+")) * 2
        except:
            return 10
    try:
        return int(qty_str)
    except:
        return 0

def _clean_code(value):
    return "".join(char for char in str(value or "").upper() if char.isalnum())

def _local_name(tag):
    return str(tag or "").rsplit("}", 1)[-1]

def _find_descendant(root, name):
    if root is None:
        return None
    for element in root.iter():
        if _local_name(element.tag) == name:
            return element
    return None

def _child_text(element, *names):
    if element is None:
        return ""
    wanted = set(names)
    for child in list(element):
        if _local_name(child.tag) in wanted:
            return _txt(child)
    return ""
