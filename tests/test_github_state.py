import unittest
from unittest.mock import Mock

from hunter.core import AttentionRequired, Instance
from hunter.github_state import GitHubState, MARKER, TITLE, decode_state, encode_state


class StateTests(unittest.TestCase):
    def store(self):
        return GitHubState(repository="owner/repository", token="test-token")

    def test_round_trip_and_no_credentials_in_state(self):
        state = {
            "phase": "pending",
            "retry_token": "public-idempotency-token",
            "request": {"ocpus": 2},
        }
        self.assertEqual(decode_state(encode_state(state)), state)
        self.assertNotIn("test-token", encode_state(state))

    def test_corrupt_unknown_or_missing_instance_state_fails_closed(self):
        for body in (
            "",
            MARKER + "\n```json\ninvalid\n```",
            encode_state({"phase": "unknown"}),
            encode_state({"phase": "accepted"}),
        ):
            with self.assertRaises(AttentionRequired):
                decode_state(body)

    def test_closed_checkpoint_blocks_new_creation(self):
        store = self.store()
        store._issues = Mock(
            return_value=iter([{"number": 1, "title": TITLE, "state": "closed"}])
        )
        with self.assertRaises(AttentionRequired):
            store.load()

    def test_multiple_checkpoints_fail_closed(self):
        store = self.store()
        store._issues = Mock(return_value=iter([{"title": TITLE}, {"title": TITLE}]))
        with self.assertRaises(AttentionRequired):
            store.load()

    def test_save_requires_exact_server_acknowledgment(self):
        store = self.store()
        store.cached = {}
        store._request = Mock(
            return_value={
                "number": 1,
                "state": "open",
                "body": encode_state({"phase": "idle"}),
            }
        )
        with self.assertRaises(AttentionRequired):
            store.save({"phase": "pending"})
        self.assertEqual(store.cached, {})

    def test_notification_is_deduplicated_after_lost_response(self):
        store = self.store()
        store._issues = Mock(
            return_value=iter([{"body": "<!-- oci-a1-hunter-success:vm -->"}])
        )
        store._request = Mock()
        store.notify(Instance("vm", "RUNNING"))
        store._request.assert_not_called()

    def test_repeated_running_state_does_not_write_again(self):
        store = self.store()
        store.cached = {"phase": "running", "instance_id": "vm"}
        store._request = Mock()
        store.save(dict(store.cached))
        store._request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
