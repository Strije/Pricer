"""Сторож Pricer: раз в 5 минут (systemd timer) проверяет, что сервис жив, и пишет в Telegram только
при смене состояния («сломалось» / «починилось»). Раз в сутки — копия базы и ключа шифрования.

Проверки: /health (сервис и база), место на диске, срок HTTPS-сертификата по PRICER_PUBLIC_URL.
Копия: pg_dump базы + /var/lib/pricer (ключ шифрования secret.key, справочник брендов) в
/var/backups/pricer/pricer-ГГГГММДД.tar.gz, хранится 14 последних. Без ключа копия базы бесполезна:
пароли поставщиков в ней зашифрованы — поэтому они лежат вместе, а папка доступна только root.
Только стандартная библиотека Python: сторож работает и при сломанном окружении сервиса.
"""
import datetime
import io
import json
import os
import shutil
import socket
import ssl
import subprocess
import tarfile
import time
import urllib.parse
import urllib.request

STATE = "/var/lib/pricer-watchdog/state.json"
DATA = "/var/lib/pricer"
BACKUPS = "/var/backups/pricer"
KEEP = 14


def check_api():
    try:
        with urllib.request.urlopen("http://127.0.0.1:8095/health", timeout=10) as response:
            return None if response.status == 200 else f"Pricer отвечает {response.status}"
    except Exception as exc:
        return f"Pricer не отвечает ({type(exc).__name__})"


def check_disk():
    usage = shutil.disk_usage("/")
    used = usage.used / usage.total * 100
    return f"Диск заполнен на {used:.0f}%" if used > 90 else None


def check_cert():
    url = urllib.parse.urlsplit(os.environ.get("PRICER_PUBLIC_URL", ""))
    if url.scheme != "https" or not url.hostname:
        return None
    try:
        with socket.create_connection((url.hostname, url.port or 443), timeout=15) as raw, \
                ssl.create_default_context().wrap_socket(raw, server_hostname=url.hostname) as tls:
            end = ssl.cert_time_to_seconds(tls.getpeercert()["notAfter"])
    except (OSError, KeyError, ValueError) as exc:
        return f"HTTPS снаружи не работает ({type(exc).__name__})"
    days = int((end - time.time()) // 86400)
    return f"Сертификат HTTPS истекает через {days} дн." if days < 14 else None


def database_name():
    url = os.environ.get("PRICER_DATABASE_URL", "")
    return urllib.parse.urlsplit(url).path.lstrip("/") or "pricer"


def backup(today):
    os.makedirs(BACKUPS, mode=0o700, exist_ok=True)
    target = os.path.join(BACKUPS, f"pricer-{today}.tar.gz")
    if os.path.exists(target):
        return None
    dump = subprocess.run(["runuser", "-u", "postgres", "--", "pg_dump", "--no-owner", database_name()],
                          capture_output=True, timeout=600)
    if dump.returncode != 0:
        return "Копия базы не сделана: " + dump.stderr.decode(errors="replace")[:200]
    tmp = target + ".part"
    with tarfile.open(tmp, "w:gz") as tar:
        info = tarfile.TarInfo("pricer.sql")
        info.size, info.mtime = len(dump.stdout), time.time()
        tar.addfile(info, io.BytesIO(dump.stdout))
        if os.path.isdir(DATA):
            tar.add(DATA, arcname="var", filter=lambda t: None if "csv_cache" in t.name else t)
    os.chmod(tmp, 0o600)
    os.replace(tmp, target)
    for old in sorted(f for f in os.listdir(BACKUPS) if f.startswith("pricer-") and f.endswith(".tar.gz"))[:-KEEP]:
        os.remove(os.path.join(BACKUPS, old))
    return None


def telegram(text):
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return
    api = (os.environ.get("TELEGRAM_API") or "https://api.telegram.org").rstrip("/")
    data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
    try:
        urllib.request.urlopen(f"{api}/bot{token}/sendMessage", data=data, timeout=15)
    except Exception:
        pass


def main():
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    try:
        with open(STATE, encoding="utf-8") as file:
            state = json.load(file)
    except (OSError, ValueError):
        state = {}
    problems = {}
    for name, check in (("api", check_api), ("disk", check_disk), ("cert", check_cert)):
        message = check()
        if message:
            problems[name] = message
    today = datetime.date.today().strftime("%Y%m%d")
    if state.get("backup_day") != today:
        message = backup(today)
        if message:
            problems["backup"] = message
        else:
            state["backup_day"] = today
    before = state.get("problems", {})
    for name, message in problems.items():
        if before.get(name) != message:
            telegram(f"⚠️ Pricer: {message}")
    for name in before:
        if name not in problems:
            telegram(f"✅ Pricer: снова в порядке ({name})")
    state["problems"] = problems
    with open(STATE, "w", encoding="utf-8") as file:
        json.dump(state, file, ensure_ascii=False)


if __name__ == "__main__":
    main()
