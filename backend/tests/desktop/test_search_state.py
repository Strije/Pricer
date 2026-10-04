import unittest

from search_state import SearchState


class SearchStateTests(unittest.TestCase):
    def test_new_search_invalidates_old_search(self):
        state = SearchState()
        first_id, first_started = state.start("OC90")
        second_id, second_started = state.start("LF1005")

        self.assertTrue(first_started)
        self.assertTrue(second_started)
        self.assertFalse(state.is_current(first_id))
        self.assertTrue(state.is_current(second_id))

    def test_cancel_invalidates_current_search(self):
        state = SearchState()
        search_id, started = state.start("OC90")
        state.cancel(search_id)

        self.assertTrue(started)
        self.assertFalse(state.is_current(search_id))

    def test_same_active_search_is_single_flight(self):
        state = SearchState()
        first_id, first_started = state.start("OC90")
        second_id, second_started = state.start("oc90")

        self.assertTrue(first_started)
        self.assertFalse(second_started)
        self.assertEqual(first_id, second_id)

    def test_cancelled_search_can_be_started_again(self):
        state = SearchState()
        first_id, _ = state.start("OC90")
        state.cancel(first_id)
        second_id, started = state.start("OC90")

        self.assertTrue(started)
        self.assertNotEqual(first_id, second_id)
        self.assertTrue(state.is_current(second_id))


if __name__ == "__main__":
    unittest.main()
