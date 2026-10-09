import copy
import unittest
from unittest.mock import Mock

from hunter.core import (
    AttentionRequired,
    Budget,
    Hunter,
    Instance,
    Plan,
    PREFERRED,
    SMALLER,
    classify_error,
)


class ServiceError(Exception):
    def __init__(
        self, status=500, code="InternalError", message="Out of host capacity."
    ):
        self.status, self.code, self.message = status, code, message
        self.request_id = "test-request"


class Clock:
    def __init__(self):
        self.now = 0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class PredictableRandom:
    @staticmethod
    def shuffle(items):
        pass

    @staticmethod
    def uniform(low, high):
        return low


class MemoryStore:
    def __init__(self, state=None):
        self.state = copy.deepcopy(state or {})
        self.history = []
        self.notifications = []

    def load(self):
        return copy.deepcopy(self.state)

    def save(self, state):
        self.state = copy.deepcopy(state)
        self.history.append(copy.deepcopy(state))

    def notify(self, instance):
        if instance.id not in self.notifications:
            self.notifications.append(instance.id)


class Gateway:
    def __init__(self, store, *, answers=None, existing=None, clock=None):
        self.store, self.clock = store, clock
        self.answers = list(answers or [])
        self.existing = existing
        self.launches = []
        self.guard_profiles = []
        self.get_states = []
        self.verify_home = Mock()
        self.validate_pending = Mock()

    def preflight(self, profiles):
        return Plan(
            [(ad, p) for p in profiles for ad in ("AD-1", "AD-2", "AD-3")],
            self.existing,
        )

    def guard_before_launch(self, profiles):
        self.guard_profiles.append(profiles)
        return self.existing

    def find_existing(self):
        return self.existing

    def prepare(self, ad, profile):
        return {
            "fingerprint": "test-fingerprint",
            "request": {
                "availability_domain": ad,
                "ocpus": profile.ocpus,
                "memory_gb": profile.memory_gb,
                "image_id": "initial-image",
                "subnet_id": "initial-subnet",
            },
        }

    def launch(self, record):
        if self.store.state != record:
            raise AssertionError("Launch occurred before an acknowledged checkpoint")
        self.launches.append(copy.deepcopy(record))
        answer = self.answers.pop(0) if self.answers else ServiceError()
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def get_instance(self, instance_id):
        if self.get_states:
            return Instance(instance_id, self.get_states.pop(0), "AD-1")
        return self.existing or Instance(instance_id, "RUNNING", "AD-1")


class HunterTests(unittest.TestCase):
    def setup_hunter(self, *, state=None, answers=None, existing=None, seconds=360):
        self.clock = Clock()
        self.budget = Budget(seconds, clock=self.clock, sleeper=self.clock.sleep)
        self.store = MemoryStore(state)
        self.gateway = Gateway(
            self.store, answers=answers, existing=existing, clock=self.clock
        )
        self.runner = Hunter(
            self.gateway,
            self.store,
            self.budget,
            rng=PredictableRandom(),
            log=lambda message: None,
        )
        return self.runner

    def test_rotates_every_domain_before_repeating(self):
        self.setup_hunter(
            answers=[ServiceError()] * 3 + [Instance("vm", "RUNNING", "AD-1")]
        ).run()
        self.assertEqual(
            [x["request"]["availability_domain"] for x in self.gateway.launches],
            ["AD-1", "AD-2", "AD-3", "AD-1"],
        )
        self.assertEqual(len({x["retry_token"] for x in self.gateway.launches}), 4)

    def test_smaller_fallback_only_after_preferred_sweep(self):
        self.setup_hunter(
            answers=[ServiceError()] * 3 + [Instance("vm", "RUNNING")]
        ).run((PREFERRED, SMALLER))
        self.assertEqual(
            [x["request"]["ocpus"] for x in self.gateway.launches], [2, 2, 2, 1]
        )
        self.assertEqual(self.gateway.guard_profiles[-1], (SMALLER,))

    def test_default_never_requests_smaller_or_paid_shape(self):
        result = self.setup_hunter(seconds=240).run()
        self.assertEqual(result.outcome, "search_completed_without_capacity")
        self.assertTrue(all(x["request"]["ocpus"] == 2 for x in self.gateway.launches))
        self.assertLessEqual(self.clock.now, 240 - self.budget.reserve)
        self.assertEqual(self.store.state, {"phase": "idle"})

    def test_uncertain_result_retries_exact_request_and_token(self):
        self.setup_hunter(answers=[TimeoutError(), Instance("vm", "RUNNING")]).run()
        self.assertEqual(self.gateway.launches[0], self.gateway.launches[1])
        self.assertEqual(self.store.notifications, ["vm"])

    def test_pending_checkpoint_survives_between_workflow_runs(self):
        result = self.setup_hunter(seconds=149, answers=[TimeoutError()]).run()
        self.assertEqual(result.outcome, "launch_outcome_unresolved")
        pending = copy.deepcopy(self.store.state)
        self.setup_hunter(state=pending, answers=[Instance("vm", "RUNNING")]).run()
        self.assertEqual(self.gateway.launches, [pending])

    def test_checkpoint_failure_prevents_creation(self):
        self.setup_hunter()
        self.store.save = Mock(side_effect=AttentionRequired("checkpoint unavailable"))
        with self.assertRaises(AttentionRequired):
            self.runner.run()
        self.assertEqual(self.gateway.launches, [])

    def test_process_killed_after_request_preserves_pending_checkpoint(self):
        self.setup_hunter(answers=[SystemExit("runner killed")])
        with self.assertRaises(SystemExit):
            self.runner.run()
        self.assertEqual(self.store.state["phase"], "pending")

    def test_lost_acceptance_checkpoint_is_reconciled_on_next_run(self):
        self.setup_hunter(answers=[Instance("vm", "PROVISIONING")])
        original_save = self.store.save

        def save(state):
            if state["phase"] == "accepted":
                raise AttentionRequired("lost GitHub response")
            original_save(state)

        self.store.save = save
        with self.assertRaises(AttentionRequired):
            self.runner.run()
        pending = copy.deepcopy(self.store.state)
        self.setup_hunter(state=pending, existing=Instance("vm", "RUNNING")).run()
        self.assertEqual(self.gateway.launches, [])

    def test_existing_stopped_instance_requires_attention(self):
        self.setup_hunter(existing=Instance("vm", "STOPPED"))
        with self.assertRaisesRegex(AttentionRequired, "STOPPED"):
            self.runner.run()
        self.assertEqual(self.gateway.launches, [])

    def test_existing_provisioning_instance_is_tracked(self):
        result = self.setup_hunter(
            existing=Instance("vm", "PROVISIONING"), seconds=150
        ).run()
        self.assertEqual(self.gateway.launches, [])
        self.assertEqual(result.outcome, "launch_accepted_provisioning")

    def test_saved_accepted_instance_is_tracked_without_new_preflight_or_launch(self):
        self.setup_hunter(state={"phase": "accepted", "instance_id": "vm"})
        self.gateway.preflight = Mock(
            side_effect=AssertionError("must not look for another VM")
        )
        result = self.runner.run()
        self.assertEqual(result.outcome, "vm_running")
        self.assertEqual(self.gateway.launches, [])

    def test_saved_terminated_instance_does_not_restart_search(self):
        self.setup_hunter(state={"phase": "running", "instance_id": "vm"})
        self.gateway.get_states = ["TERMINATED"]
        with self.assertRaises(AttentionRequired):
            self.runner.run()
        self.assertEqual(self.gateway.launches, [])

    def test_lookup_failure_is_not_interpreted_as_zero_instances(self):
        self.setup_hunter()
        self.gateway.guard_before_launch = Mock(
            side_effect=ServiceError(403, "NotAuthorizedOrNotFound")
        )
        with self.assertRaises(ServiceError):
            self.runner.run()
        self.assertEqual(self.gateway.launches, [])

    def test_fatal_error_stops_after_one_attempt(self):
        self.setup_hunter(
            answers=[ServiceError(400, "LimitExceeded", "capacity limit exceeded")]
        )
        with self.assertRaises(AttentionRequired):
            self.runner.run()
        self.assertEqual(len(self.gateway.launches), 1)
        self.assertEqual(self.store.state, {"phase": "idle"})

    def test_throttling_backs_off_before_new_request(self):
        self.setup_hunter(
            answers=[ServiceError(429, "TooManyRequests"), Instance("vm", "RUNNING")]
        ).run()
        self.assertGreaterEqual(self.clock.sleeps[0], 60)
        self.assertNotEqual(
            self.gateway.launches[0]["retry_token"],
            self.gateway.launches[1]["retry_token"],
        )

    def test_unknown_server_error_cannot_advance_domain(self):
        self.setup_hunter(
            answers=[
                ServiceError(500, "InternalError", "Unexpected backend failure"),
                Instance("vm", "RUNNING"),
            ]
        ).run()
        self.assertEqual(self.gateway.launches[0], self.gateway.launches[1])

    def test_fatal_rejection_of_uncertain_replay_preserves_original_checkpoint(self):
        self.setup_hunter(
            answers=[TimeoutError(), ServiceError(409, "InvalidatedRetryToken")]
        )
        with self.assertRaises(AttentionRequired):
            self.runner.run()
        self.assertEqual(self.store.state["phase"], "pending")
        self.assertEqual(self.gateway.launches[0], self.gateway.launches[1])

    def test_throttled_uncertain_replay_keeps_same_token(self):
        self.setup_hunter(
            answers=[
                TimeoutError(),
                ServiceError(429, "TooManyRequests"),
                Instance("vm", "RUNNING"),
            ]
        ).run()
        self.assertEqual(self.gateway.launches[0], self.gateway.launches[2])

    def test_expired_pending_request_never_launches(self):
        self.setup_hunter(state={"phase": "pending"})
        self.gateway.validate_pending.side_effect = AttentionRequired("expired")
        with self.assertRaises(AttentionRequired):
            self.runner.run()
        self.assertEqual(self.gateway.launches, [])

    def test_preflight_never_writes_state_or_launches(self):
        self.setup_hunter()
        self.runner.store = None
        result = self.runner.run(preflight_only=True)
        self.assertEqual(result.outcome, "preflight_passed")
        self.assertEqual(self.gateway.launches, [])
        self.assertEqual(self.store.history, [])

    def test_insufficient_remaining_time_does_not_start_attempt(self):
        self.setup_hunter(seconds=119).run()
        self.assertEqual(self.gateway.launches, [])

    def test_capacity_classification_requires_explicit_host_capacity_message(self):
        self.assertEqual(classify_error(ServiceError()), "capacity")
        self.assertEqual(
            classify_error(ServiceError(message="Invalid capacity reservation")),
            "uncertain",
        )
        self.assertEqual(classify_error(ServiceError(401, "NotAuthenticated")), "fatal")


if __name__ == "__main__":
    unittest.main()
