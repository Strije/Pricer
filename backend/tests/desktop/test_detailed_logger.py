import datetime
import json
import os
import shutil
import unittest

from detailed_logger import DetailedLogger



def _days_ago(days):
    return (datetime.date.today() - datetime.timedelta(days=days)).isoformat()


class DetailedLoggerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = os.path.join(os.getcwd(), "tmp_test_detailed_logger")
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        os.makedirs(self.temp_dir, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_writes_jsonl_and_redacts_configured_secrets(self):
        logger = DetailedLogger(log_dir=self.temp_dir)
        logger.set_secrets_from_settings(
            {
                "profit_league": {"api_key": "SECRET-12345"},
                "forum_auto": {"password": "forum-password"},
            }
        )

        logger.log(
            "provider_error",
            provider="Forum-Auto",
            message="bad SECRET-12345 forum-password",
            url="https://example.test/api?pass=forum-password&secret=SECRET-12345",
            payload={"password": "forum-password", "article": "OC90"},
        )

        # provider_error попадает и в подробный лог, и в поток ошибок
        files = sorted(os.listdir(self.temp_dir))
        self.assertEqual(len(files), 2)
        self.assertEqual(
            files,
            sorted([os.path.basename(logger.current_path()), os.path.basename(logger.errors_path())]),
        )
        for name in files:
            with open(os.path.join(self.temp_dir, name), encoding="utf-8") as file:
                dumped = json.dumps(json.loads(file.readline()), ensure_ascii=False)
            self.assertNotIn("SECRET-12345", dumped)
            self.assertNotIn("forum-password", dumped)
        with open(logger.current_path(), encoding="utf-8") as file:
            record = json.loads(file.readline())

        self.assertEqual(record["event"], "provider_error")
        self.assertEqual(record["provider"], "Forum-Auto")
        self.assertNotIn("SECRET-12345", json.dumps(record, ensure_ascii=False))
        self.assertNotIn("forum-password", json.dumps(record, ensure_ascii=False))
        self.assertEqual(record["payload"]["password"], "***")
        self.assertEqual(record["payload"]["article"], "OC90")

    def test_redacts_auth_headers(self):
        logger = DetailedLogger(log_dir=self.temp_dir)
        redacted = logger._redact_text("Authorization: Bearer abc.def.ghi")

        self.assertIn("Bearer ***", redacted)
        self.assertNotIn("abc.def.ghi", redacted)

    def test_rotates_current_log_by_size(self):
        logger = DetailedLogger(log_dir=self.temp_dir, max_bytes=256 * 1024)
        current = logger.current_path()
        with open(current, "w", encoding="utf-8") as file:
            file.write("x" * (256 * 1024))

        logger.log("event", message="after rotate")

        files = sorted(os.listdir(self.temp_dir))
        self.assertEqual(len(files), 2)
        self.assertTrue(any(name.endswith("-001.jsonl") for name in files))
        with open(current, encoding="utf-8") as file:
            record = json.loads(file.readline())
        self.assertEqual(record["message"], "after rotate")

    def test_drops_empty_fields_but_keeps_false_and_zero(self):
        logger = DetailedLogger(log_dir=self.temp_dir)

        logger.log(
            "order_file_row_finish",
            status="ready",
            raw_count=0,
            exact_match=False,
            warehouse="",
            part_id=None,
            samples={"quantity": [], "accepted": [{"price": 100, "gid": None}]},
        )

        with open(logger.current_path(), encoding="utf-8") as file:
            record = json.loads(file.readline())

        self.assertNotIn("warehouse", record)
        self.assertNotIn("part_id", record)
        self.assertNotIn("provider", record)
        self.assertNotIn("quantity", record["samples"])
        self.assertNotIn("gid", record["samples"]["accepted"][0])
        # ноль и False несут смысл и обязаны остаться
        self.assertEqual(record["raw_count"], 0)
        self.assertIs(record["exact_match"], False)
        self.assertEqual(record["samples"]["accepted"][0]["price"], 100)

    def test_failures_go_to_the_error_stream_without_heavy_samples(self):
        logger = DetailedLogger(log_dir=self.temp_dir)

        logger.log(
            "order_submit_item_finish",
            provider="Avtoto",
            article="1987301001",
            success=False,
            response={"success": False, "error": "Avtoto: товар недоступен для заказа"},
            provider_stats=[{"provider": "Avtoto", "sample": ["x" * 500]}],
            filter_samples={"accepted": ["y" * 500]},
        )

        with open(logger.errors_path(), encoding="utf-8") as file:
            record = json.loads(file.readline())

        self.assertEqual(record["article"], "1987301001")
        self.assertIn("недоступен", record["response"]["error"])
        self.assertNotIn("provider_stats", record)
        self.assertNotIn("filter_samples", record)

    def test_successful_records_stay_out_of_the_error_stream(self):
        logger = DetailedLogger(log_dir=self.temp_dir)

        logger.log("order_submit_item_finish", provider="Avtoto", success=True)
        logger.log("order_file_row_finish", status="ready")
        logger.log("provider_cache_store", provider="Avtoto", result_count=6)

        self.assertFalse(os.path.exists(logger.errors_path()))

    def test_error_stream_catches_failed_rows_and_provider_timeouts(self):
        logger = DetailedLogger(log_dir=self.temp_dir)

        logger.log("order_file_row_finish", status="not_found", article="GB1107")
        logger.log("provider_debug", provider="Avtoto", step="_call:timeout", payload="{}")
        logger.log("order_submit_finish", status="partial", failed_count=1)

        with open(logger.errors_path(), encoding="utf-8") as file:
            events = [json.loads(line)["event"] for line in file]

        self.assertEqual(
            events,
            ["order_file_row_finish", "provider_debug", "order_submit_finish"],
        )

    def test_keeps_logs_by_date_and_drops_only_what_is_older(self):
        logger = DetailedLogger(log_dir=self.temp_dir, max_days=90)
        # Даты от сегодняшнего дня: с вшитыми датами тест сам устаревал через 90 дней.
        fresh = os.path.join(self.temp_dir, f"procenka-details-{_days_ago(60)}.jsonl")
        stale = os.path.join(self.temp_dir, f"procenka-details-{_days_ago(200)}.jsonl")
        stale_errors = os.path.join(self.temp_dir, f"procenka-errors-{_days_ago(200)}.jsonl")
        for path in (fresh, stale, stale_errors):
            with open(path, "w", encoding="utf-8") as file:
                file.write("{}\n")

        logger.log("event", message="today")

        self.assertTrue(os.path.exists(fresh))
        self.assertFalse(os.path.exists(stale))
        self.assertFalse(os.path.exists(stale_errors))

    def test_many_rotations_in_one_day_no_longer_evict_history(self):
        logger = DetailedLogger(log_dir=self.temp_dir, max_days=90)
        old_day = os.path.join(self.temp_dir, f"procenka-details-{_days_ago(60)}.jsonl")
        with open(old_day, "w", encoding="utf-8") as file:
            file.write("{}\n")
        for index in range(1, 21):
            path = os.path.join(self.temp_dir, f"procenka-details-{_days_ago(1)}-{index:03d}.jsonl")
            with open(path, "w", encoding="utf-8") as file:
                file.write("{}\n")

        logger.log("event", message="after many rotations")

        # старое правило (14 файлов) стёрло бы историю июля этими ротациями
        self.assertTrue(os.path.exists(old_day))


if __name__ == "__main__":
    unittest.main()
