import re


PRICE_TOKEN_NAMES = {
    "abs": "ABS",
    "abstd": "ABSTD",
    "armtek": "Армтек",
    "avtoto": "Avtoto",
    "auto": "Auto",
    "auto24": "Auto24",
    "csv": "CSV",
    "domodedovo": "Домодедово",
    "favorit": "Фаворит",
    "freno": "Freno",
    "help": "Help",
    "krasnodar": "Краснодар",
    "krd": "Краснодар",
    "krd2": "Краснодар 2",
    "krs": "Краснодар",
    "liga": "Лига",
    "league": "League",
    "mikado": "Mikado",
    "moscow": "Москва",
    "msk": "Москва",
    "por": "ПОР",
    "price": "Прайс",
    "profit": "Profit",
    "rostov": "Ростов",
    "rossko": "Rossko",
    "rst": "Ростов",
    "simf": "Симферополь",
    "spb": "Санкт-Петербург",
    "tiss": "TISS",
}


def humanize_price_name(value, force_prefix=False):
    raw = str(value or "").strip()
    if not raw:
        return raw
    body = raw
    has_prefix = raw.lower().startswith("прайс ")
    if has_prefix:
        body = raw[6:].strip()
    elif not force_prefix and not _looks_machine_name(raw):
        return raw
    body = _humanize_price_body(body)
    if not body:
        return raw
    return f"Прайс {body}" if (has_prefix or force_prefix or _looks_machine_name(raw)) else body


def looks_like_price_source_name(value):
    raw = str(value or "").strip()
    return raw.lower().startswith("прайс ") or _looks_machine_name(raw)


def _looks_machine_name(value):
    text = str(value or "").strip()
    if not text:
        return False
    if not re.fullmatch(r"[A-Za-z0-9_.\-\s]+", text):
        return False
    return any(mark in text for mark in ("-", "_", ".")) or text == text.lower()


def _humanize_price_body(value):
    text = str(value or "").strip()
    if not text:
        return ""
    parts = [part for part in re.split(r"[-_\s.]+", text) if part]
    if len(parts) <= 1:
        return _humanize_token(text)
    return " ".join(_humanize_token(part) for part in parts)


def _humanize_token(token):
    token = str(token or "").strip()
    if not token:
        return ""
    mapped = PRICE_TOKEN_NAMES.get(token.lower())
    if mapped:
        return mapped
    if token.isupper():
        return token
    if token.isdigit():
        return token
    return token[:1].upper() + token[1:]
