import os
import sys

APP_NAME = "Проценка"


CONFIG_ENV_VAR = "PROCENKA_CONFIG_DIR"


def get_config_dir():
    """Возвращает путь к папке конфигурации.
    Приоритет:
      1. переменная окружения PROCENKA_CONFIG_DIR;
      2. для .exe: папка config\\ рядом с .exe, если она уже есть (портативный режим);
      3. для .exe: %APPDATA%\\Проценка\\
      4. для скрипта: папка проекта\\config\\
    """
    override = os.environ.get(CONFIG_ENV_VAR, "").strip()
    if override:
        path = os.path.abspath(os.path.expanduser(override))
    elif getattr(sys, 'frozen', False):
        portable = os.path.join(os.path.dirname(os.path.abspath(sys.executable)), "config")
        if os.path.isdir(portable):
            path = portable
        else:
            # Запуск из .exe — используем AppData
            base = os.environ.get('APPDATA', os.path.expanduser('~'))
            path = os.path.join(base, APP_NAME)
    else:
        # Запуск из скрипта — используем config/ рядом с проектом
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config")
    os.makedirs(path, exist_ok=True)
    return path


def get_settings_path():
    return os.path.join(get_config_dir(), "settings.json")


def get_history_path():
    return os.path.join(get_config_dir(), "history.json")


def get_order_history_path():
    return os.path.join(get_config_dir(), "order_history.json")


def get_cross_db_path():
    return os.path.join(get_config_dir(), "cross_pairs.sqlite3")


def get_orders_dir():
    path = os.path.join(get_config_dir(), "orders")
    os.makedirs(path, exist_ok=True)
    return path
