import unittest

from provider_health import ProviderCircuitBreaker


class ProviderCircuitBreakerTests(unittest.TestCase):
    def test_does_not_count_plain_no_results_as_operational_failure(self):
        self.assertFalse(ProviderCircuitBreaker.should_count_failure("товары не найдены"))
        self.assertFalse(ProviderCircuitBreaker.should_count_failure("бренды не найдены"))

    def test_counts_network_and_http_failures(self):
        self.assertTrue(ProviderCircuitBreaker.should_count_failure("таймаут после 10с"))
        self.assertTrue(ProviderCircuitBreaker.should_count_failure("HTTP 500: server error"))
        self.assertTrue(ProviderCircuitBreaker.should_count_failure("ошибка соединения"))

    def test_opens_after_threshold_and_allows_after_cooldown(self):
        now = [100.0]
        breaker = ProviderCircuitBreaker(threshold=2, cooldown_seconds=30, clock=lambda: now[0])

        self.assertEqual(breaker.allow("Forum-Auto"), (True, 0))
        self.assertEqual(breaker.record_failure("Forum-Auto", "HTTP 500"), 0)
        self.assertEqual(breaker.allow("Forum-Auto"), (True, 0))
        self.assertEqual(breaker.record_failure("Forum-Auto", "HTTP 500"), 30)

        allowed, remaining = breaker.allow("Forum-Auto")
        self.assertFalse(allowed)
        self.assertEqual(remaining, 30)

        now[0] = 131.0
        self.assertEqual(breaker.allow("Forum-Auto"), (True, 0))

    def test_cooldown_start_resets_failures(self):
        # После паузы поставщик возвращается с чистым счётчиком: одиночный
        # хвостовой таймаут не должен снова закрывать его на полную паузу.
        now = [100.0]
        breaker = ProviderCircuitBreaker(threshold=2, cooldown_seconds=30, clock=lambda: now[0])

        breaker.record_failure("Avtoto", "таймаут")
        self.assertEqual(breaker.record_failure("Avtoto", "таймаут"), 30)

        now[0] = 131.0
        self.assertEqual(breaker.allow("Avtoto"), (True, 0))
        self.assertEqual(breaker.record_failure("Avtoto", "таймаут"), 0)
        self.assertEqual(breaker.allow("Avtoto"), (True, 0))

    def test_default_thresholds_are_forgiving(self):
        breaker = ProviderCircuitBreaker()
        self.assertEqual(breaker.threshold, 5)
        self.assertEqual(breaker.cooldown_seconds, 45)

    def test_success_resets_failures(self):
        breaker = ProviderCircuitBreaker(threshold=2, cooldown_seconds=30, clock=lambda: 10.0)

        breaker.record_failure("ABSTD", "HTTP 502")
        breaker.record_success("ABSTD")
        self.assertEqual(breaker.record_failure("ABSTD", "HTTP 502"), 0)


if __name__ == "__main__":
    unittest.main()
