def decode_response_text(response, limit=None):
    raw = getattr(response, "content", b"") or b""
    if isinstance(raw, str):
        text = raw
    else:
        text = _decode_bytes(raw, getattr(response, "encoding", None))
    text = _fix_mojibake(text)
    if limit is not None:
        return text[: int(limit)]
    return text


def _decode_bytes(raw, response_encoding=None):
    declared = _declared_encoding(raw)
    encodings = [
        declared,
        response_encoding,
        "utf-8",
        "utf-8-sig",
        "windows-1251",
        "cp1251",
        "koi8-r",
    ]
    seen = set()
    best = ""
    best_score = None
    for encoding in encodings:
        if not encoding:
            continue
        encoding = str(encoding).strip().lower()
        if encoding in seen:
            continue
        seen.add(encoding)
        try:
            text = raw.decode(encoding)
        except Exception:
            continue
        text = _fix_mojibake(text)
        score = _text_quality_score(text)
        if best_score is None or score < best_score:
            best = text
            best_score = score
    return best or raw.decode("utf-8", errors="replace")


def _declared_encoding(raw):
    sample = raw[:400].decode("ascii", errors="ignore").lower()
    marker = "charset="
    if marker not in sample:
        return ""
    tail = sample.split(marker, 1)[1]
    return tail.split('"', 1)[0].split("'", 1)[0].split(";", 1)[0].split(">", 1)[0].strip()


def _fix_mojibake(text):
    if not isinstance(text, str):
        return text
    markers = ("Р", "С", "Ð", "Ñ", "в„", "В№")
    if not any(marker in text for marker in markers):
        return text
    for source_encoding in ("cp1251", "latin1", "cp1252"):
        try:
            fixed = text.encode(source_encoding).decode("utf-8")
        except Exception:
            continue
        if _mojibake_score(fixed) < _mojibake_score(text):
            return fixed
    try:
        fixed = text.encode("cp1251", errors="ignore").decode("utf-8", errors="ignore")
    except Exception:
        return text
    if fixed and _mojibake_score(fixed) < _mojibake_score(text):
        return fixed
    return text


def _mojibake_score(text):
    bad = sum(text.count(marker) for marker in ("Р", "С", "Ð", "Ñ", "в„", "В№", "�"))
    good = sum(1 for char in text if "А" <= char <= "я" or char in "ёЁ№")
    return bad * 3 - good


def _text_quality_score(text):
    suspicious = sum(
        1
        for char in text
        if ord(char) >= 128 and not ("А" <= char <= "я" or char in "ёЁ№₽«»–—")
    )
    return _mojibake_score(text) + suspicious * 2
