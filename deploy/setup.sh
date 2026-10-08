#!/usr/bin/env bash
# Установка Pricer на VPS (Ubuntu/Debian) рядом с другими сервисами — их не трогаем:
# свой пользователь, своя база PostgreSQL, свой сайт nginx, свой порт 8095.
# Запускать от root:
#
#   curl -fsSLo pricer-setup.sh https://raw.githubusercontent.com/Strije/Pricer/claude/price-web-migration-plan-fmsod0/deploy/setup.sh
#   bash pricer-setup.sh install [адрес] [email]                   # 1) всё поставить и включить HTTPS
#        адрес — свой домен (pricer.avtodrug92.ru); без него — технический адрес сервера
#        у хостинга или бесплатное имя вида 109-73-199-217.sslip.io
#   bash pricer-setup.sh create-org "Автодруг" you@mail.ru          # 2) организация и администратор
#   bash pricer-setup.sh update                                     # потом: свежий код из git
#   bash pricer-setup.sh backup | restore ФАЙЛ | status
#   bash pricer-setup.sh import-brands report.xls                  # справочник брендов ABCP (выгрузка из админки)
#   bash pricer-setup.sh doctor                                     # что с сервером: вывод можно прислать
#
# Свой домен должен уже указывать на этот сервер (DNS, запись A), иначе сертификат не выпустится.
set -euo pipefail

REPO="${REPO:-https://github.com/Strije/Pricer.git}"
BRANCH="${BRANCH:-claude/price-web-migration-plan-fmsod0}"
DIR=/opt/pricer
DATA=/var/lib/pricer
BACKUPS=/var/backups/pricer
ENVF=/etc/pricer.env
SITE=/etc/nginx/sites-available/pricer

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
ok() { printf '   \033[32m✔ %s\033[0m\n' "$*"; }
bad() { printf '   \033[31m✘ %s\033[0m\n' "$*"; }
die() { bad "$*"; exit 1; }
env_get() { [ -f "$ENVF" ] && sed -n "s/^$1=//p" "$ENVF" | tail -1 || true; }
env_set() {
    if grep -q "^$1=" "$ENVF"; then sed -i "s|^$1=.*|$1=$2|" "$ENVF"; else echo "$1=$2" >> "$ENVF"; fi
}
health() {
    local i
    for i in $(seq 1 30); do
        if curl -fsS -m 5 http://127.0.0.1:8095/health >/dev/null 2>&1; then ok "сервис отвечает"; return 0; fi
        sleep 1
    done
    bad "сервис не отвечает — journalctl -u pricer -n 50"; return 1
}

pick_python() {  # Python 3.11+ (на Ubuntu 22.04 по умолчанию 3.10 — ставим 3.11 из PPA deadsnakes)
    local py
    for py in python3.13 python3.12 python3.11 python3; do
        if command -v "$py" >/dev/null && "$py" -c 'import sys; sys.exit(sys.version_info < (3, 11))'; then echo "$py"; return; fi
    done
    if command -v add-apt-repository >/dev/null; then
        add-apt-repository -y ppa:deadsnakes/ppa >/dev/null && apt-get update -qq \
            && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3.11 python3.11-venv >/dev/null && echo python3.11 && return
    fi
    die "нужен Python 3.11 или новее"
}

ensure_postgres() {  # база включена при загрузке, запущена и защищена от OOM-killer
    local dropin=/etc/systemd/system/postgresql@.service.d
    mkdir -p "$dropin"
    # при нехватке памяти ядро пусть лучше остановит Pricer (он перезапустится сам), чем базу
    printf '[Service]\nOOMScoreAdjust=-800\n' > "$dropin/pricer-oom.conf"
    systemctl daemon-reload
    systemctl enable -q postgresql 2>/dev/null || true
    pg_lsclusters --no-header 2>/dev/null | awk '$4 !~ /online/ {print $1, $2}' | while read -r ver name; do
        echo "   … PostgreSQL $ver $name была остановлена — запускаю"
        pg_ctlcluster "$ver" "$name" start || bad "PostgreSQL $ver не запускается: tail -n 30 /var/log/postgresql/postgresql-$ver-$name.log"
    done || true
}

deploy_code() {
    local src
    src=$(mktemp -d)
    echo "   … скачиваю код ($BRANCH)"
    git clone -q --depth 1 -b "$BRANCH" "$REPO" "$src"
    mkdir -p "$DIR"
    # тесты и демо-записи на сервере не нужны
    rsync -a --delete --exclude venv --exclude tests --exclude __pycache__ --exclude var "$src/backend/" "$DIR/backend/"
    rsync -a --delete "$src/deploy/" "$DIR/deploy/"
    (cd "$src" && git rev-parse --short HEAD) > "$DIR/VERSION"
    rm -rf "$src"
    if [ ! -x "$DIR/venv/bin/python" ]; then
        local py
        py=$(pick_python)
        # на Ubuntu/Debian модуль venv — отдельный пакет python3.X-venv
        "$py" -m venv "$DIR/venv" 2>/dev/null || {
            DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "$("$py" -c 'import sys; print("python%d.%d-venv" % sys.version_info[:2])')" >/dev/null
            rm -rf "$DIR/venv"; "$py" -m venv "$DIR/venv"; }
    fi
    echo "   … ставлю зависимости (2–5 минут, без вывода)"
    "$DIR/venv/bin/pip" install -q --upgrade pip
    "$DIR/venv/bin/pip" install -q -r "$DIR/backend/requirements.txt"
    chown -R root:root "$DIR"
    cp "$DIR/deploy/pricer.service" "$DIR/deploy/pricer-watchdog.service" "$DIR/deploy/pricer-watchdog.timer" /etc/systemd/system/
    systemctl daemon-reload
    ok "код $(cat "$DIR/VERSION") выложен в $DIR"
}

cmd_install() {
    local domain="" email="" arg
    for arg in "$@"; do  # порядок любой: адрес и/или email (письма Let's Encrypt о сертификате)
        case "$arg" in *@*) email="$arg" ;; *) domain="$arg" ;; esac
    done
    [ "$(id -u)" = 0 ] || die "запускать от root"

    say "Пакеты"
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3 python3-venv git rsync curl nginx certbot \
        python3-certbot-nginx postgresql >/dev/null
    ok "Python для сервиса: $(pick_python), PostgreSQL, nginx, certbot"
    ensure_postgres

    say "Адрес"
    local here there
    here=$(curl -fsS -4 -m 10 https://ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')
    if [ -z "$domain" ]; then
        # своего домена нет: технический адрес сервера у хостинга (обратная запись DNS, как у API
        # Автодруга), а если его нет — бесплатное имя sslip.io, которое само указывает на этот IP
        domain=$(getent hosts "$here" | awk '{print $2}' | grep '\.' | grep -v '^localhost' | head -1 || true)
        if [ -n "$domain" ] && [ "$(getent ahostsv4 "$domain" | awk 'NR==1{print $1}')" = "$here" ]; then
            ok "технический адрес сервера: $domain"
        else
            domain="${here//./-}.sslip.io"
            ok "свой адрес не задан — используем $domain (бесплатное имя для IP $here)"
        fi
    fi
    there=$(getent ahostsv4 "$domain" | awk 'NR==1{print $1}' || true)
    if [ "${SKIP_DNS_CHECK:-0}" = 1 ]; then ok "проверка DNS пропущена (SKIP_DNS_CHECK=1)"
    elif [ -n "$there" ] && [ "$here" = "$there" ]; then ok "$domain → $there (этот сервер)"
    else bad "$domain → ${there:-не найден}, а этот сервер — ${here:-?}. Добавьте в DNS запись A: $domain → $here и повторите"; exit 1; fi

    say "Пользователь и папки"
    id pricer >/dev/null 2>&1 || useradd --system --home "$DATA" --shell /usr/sbin/nologin pricer
    install -d -m 700 -o pricer -g pricer "$DATA"
    install -d -m 700 -o root -g root "$BACKUPS"
    ok "пользователь pricer, данные в $DATA, копии в $BACKUPS"

    say "База PostgreSQL"
    local pass
    pass=$(env_get PRICER_DATABASE_URL | sed -n 's|.*://pricer:\([^@]*\)@.*|\1|p')
    [ -n "$pass" ] || pass=$(openssl rand -hex 24)
    runuser -u postgres -- psql -qtAc "SELECT 1 FROM pg_roles WHERE rolname='pricer'" | grep -q 1 \
        || runuser -u postgres -- psql -qc "CREATE ROLE pricer LOGIN PASSWORD '$pass'"
    runuser -u postgres -- psql -qc "ALTER ROLE pricer PASSWORD '$pass'"
    runuser -u postgres -- psql -qtAc "SELECT 1 FROM pg_database WHERE datname='pricer'" | grep -q 1 \
        || runuser -u postgres -- createdb -O pricer pricer
    ok "база pricer (пользователь pricer, только с этого сервера)"

    say "Настройки $ENVF"
    [ -f "$ENVF" ] || { install -m 600 /dev/null "$ENVF"; }
    env_set PRICER_DATABASE_URL "postgresql+psycopg://pricer:$pass@127.0.0.1/pricer"
    env_set PRICER_VAR_DIR "$DATA"
    env_set PRICER_PUBLIC_URL "https://$domain"
    env_set PRICER_SECURE_COOKIES 1
    [ -n "$(env_get PRICER_ALLOW_SIGNUP)" ] || env_set PRICER_ALLOW_SIGNUP 0
    [ -n "$(env_get PRICER_STATUS_REFRESH_MINUTES)" ] || env_set PRICER_STATUS_REFRESH_MINUTES 30
    if [ -z "$(env_get TELEGRAM_BOT_TOKEN)" ] && [ -f /etc/avtodrug-api.env ]; then
        # бот сторожа Автодруга на этом же сервере — сообщения о сбоях Pricer придут в тот же чат
        for key in TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID TELEGRAM_API; do
            env_set "$key" "$(sed -n "s/^$key=//p" /etc/avtodrug-api.env | tail -1)"
        done
        ok "Telegram сторожа — тот же бот, что у Автодруга"
    fi
    chmod 600 "$ENVF"
    ok "готово (права 600, в git не попадает)"

    say "Код"
    deploy_code

    say "nginx и HTTPS"
    sed "s/__DOMAIN__/$domain/" "$DIR/deploy/nginx-pricer.conf" > "$SITE"
    ln -sf "$SITE" /etc/nginx/sites-enabled/pricer
    nginx -t -q && systemctl reload nginx
    if [ -n "$email" ]; then certbot --nginx -n --agree-tos -m "$email" -d "$domain" --redirect >/dev/null
    else certbot --nginx -n --agree-tos --register-unsafely-without-email -d "$domain" --redirect >/dev/null; fi
    ok "https://$domain (сертификат Let's Encrypt продлевается сам)"

    say "Запуск"
    systemctl enable -q --now pricer pricer-watchdog.timer
    health
    say "Готово: https://$domain — дальше: bash $0 create-org \"Название\" ВАШ@EMAIL"
}

cmd_update() {
    say "Обновление"
    ensure_postgres
    deploy_code
    systemctl restart pricer
    health
}

manage() {  # команда app.manage от имени сервиса: нужны только адрес базы и папка данных
    cd "$DIR/backend" && runuser -u pricer -- env PRICER_DATABASE_URL="$(env_get PRICER_DATABASE_URL)" \
        PRICER_VAR_DIR="$DATA" "$DIR/venv/bin/python" -m app.manage "$@"
}

cmd_create_org() {
    [ $# -eq 2 ] || die "использование: create-org \"Название организации\" email"
    manage create-org "$1" "$2"
}

cmd_reset_password() {
    [ $# -eq 1 ] || die "использование: reset-password email"
    manage reset-password "$1"
}

cmd_import_brands() {  # синонимы брендов ABCP -> $DATA/brands; без них веб узнаёт ~10% написаний брендов
    local file="${1:-}" tmp
    [ -f "$file" ] || die "использование: import-brands отчёт.xls (выгрузка справочника брендов из админки ABCP)"
    tmp=$(mktemp -d)
    cp "$file" "$tmp/" && chown -R pricer:pricer "$tmp"
    manage import-brands "$tmp/$(basename "$file")"
    rm -rf "$tmp"
    systemctl restart pricer
    health
}

cmd_backup() {
    say "Копия сейчас"
    rm -f "$BACKUPS/pricer-$(date +%Y%m%d).tar.gz"
    rm -f /var/lib/pricer-watchdog/state.json
    systemctl start pricer-watchdog.service
    ls -lh "$BACKUPS" | tail -3
}

cmd_restore() {
    local file="${1:-}" tmp
    [ -f "$file" ] || die "использование: restore $BACKUPS/pricer-ГГГГММДД.tar.gz"
    say "Восстановление из $file (текущие данные будут заменены)"
    read -r -p "   Продолжить? Напишите ДА: " answer
    [ "$answer" = "ДА" ] || die "отменено"
    tmp=$(mktemp -d)
    tar -xzf "$file" -C "$tmp"
    systemctl stop pricer
    runuser -u postgres -- dropdb --if-exists pricer
    runuser -u postgres -- createdb -O pricer pricer
    # от имени pricer: таблицы должны принадлежать сервису (он сам добавляет новые столбцы при обновлении)
    psql -q "$(env_get PRICER_DATABASE_URL | sed 's|+psycopg||')" -f "$tmp/pricer.sql" >/dev/null
    rsync -a --delete "$tmp/var/" "$DATA/" && chown -R pricer:pricer "$DATA"
    rm -rf "$tmp"
    systemctl start pricer
    health
}

cmd_status() {
    systemctl --no-pager --lines=0 status pricer || true
    curl -fsS -m 5 http://127.0.0.1:8095/health && echo
    echo "версия: $(cat "$DIR/VERSION" 2>/dev/null)"
    echo "копии:"; ls -lh "$BACKUPS" 2>/dev/null | tail -5
}

cmd_doctor() {  # сводка для разбора сбоев; пароли из адресов вида ://user:pass@ скрываются
    {
        set +e  # сводка собирается целиком, даже если какой-то команды нет или grep ничего не нашёл
        say "Pricer"
        echo "версия: $(cat "$DIR/VERSION" 2>/dev/null)  сервис: $(systemctl is-active pricer)  с $(systemctl show -p ActiveEnterTimestamp --value pricer)"
        echo "перезапусков сервиса: $(systemctl show -p NRestarts --value pricer)"
        curl -sS -m 5 http://127.0.0.1:8095/health; echo
        say "PostgreSQL"
        pg_lsclusters 2>&1
        pg_lsclusters --no-header 2>/dev/null | while read -r ver name _ _ _ _ log; do
            echo "-- $log (последние ошибки)"
            grep -E "FATAL|PANIC|ERROR|database system" "$log" 2>/dev/null | grep -vi password | tail -n 12
        done
        say "Память и диск"
        free -m; swapon --show 2>/dev/null || true; [ -n "$(swapon --show 2>/dev/null)" ] || echo "swap: нет"
        df -h / | tail -1
        say "Нехватка памяти за 3 суток (ядро)"
        journalctl -k --since "-3 days" --no-pager -q 2>/dev/null | grep -iE "out of memory|killed process|oom-kill" | tail -n 10 || true
        say "Загрузки сервера"
        journalctl --list-boots --no-pager 2>/dev/null | tail -n 4
        say "Pricer: ошибки за сутки"
        journalctl -u pricer --since "-1 day" --no-pager -q 2>/dev/null | grep -E "\[pricer\]|Error|error|Killed|Main process exited" | tail -n 25
        say "Сторож"
        cat /var/lib/pricer-watchdog/state.json 2>/dev/null; echo
        systemctl list-timers pricer-watchdog.timer --no-pager 2>/dev/null | head -2
    } 2>&1 | sed -E 's#(://[^:/@ ]+):[^@ ]+@#\1:***@#g'
}

case "${1:-}" in
    install) shift; cmd_install "$@" ;;
    update) cmd_update ;;
    create-org) shift; cmd_create_org "$@" ;;
    reset-password) shift; cmd_reset_password "$@" ;;
    import-brands) shift; cmd_import_brands "$@" ;;
    backup) cmd_backup ;;
    restore) shift; cmd_restore "$@" ;;
    status) cmd_status ;;
    doctor) cmd_doctor ;;
    *) sed -n '2,15p' "$0"; exit 2 ;;
esac
