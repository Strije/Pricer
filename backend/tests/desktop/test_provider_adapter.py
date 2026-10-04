import unittest
from requests import exceptions as request_exceptions

from provider_adapter import run_with_retries


class ProviderAdapterTests(unittest.TestCase):
    def test_retries_transient_errors(self):
        attempts = {"count": 0}

        def flaky_request():
            attempts["count"] += 1
            if attempts["count"] < 3:
                raise request_exceptions.Timeout("temporary")
            return {"ok": True}

        result = run_with_retries(
            flaky_request,
            retries=3,
            backoff=0,
            retryable_exceptions=(request_exceptions.Timeout,),
        )

        self.assertEqual(result, {"ok": True})
        self.assertEqual(attempts["count"], 3)

    def test_does_not_retry_non_retryable_errors(self):
        attempts = {"count": 0}

        def failing_request():
            attempts["count"] += 1
            raise ValueError("fatal")

        with self.assertRaises(ValueError):
            run_with_retries(
                failing_request,
                retries=2,
                backoff=0,
                retryable_exceptions=(request_exceptions.Timeout,),
            )

        self.assertEqual(attempts["count"], 1)


if __name__ == "__main__":
    unittest.main()
