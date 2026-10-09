"""Launch state machine; all cloud and clock operations are injected for testing."""

from __future__ import annotations

import copy
import random
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field


class AttentionRequired(RuntimeError):
    """A condition that must not be interpreted as capacity exhaustion."""


class BudgetExpired(RuntimeError):
    pass


@dataclass(frozen=True)
class Profile:
    ocpus: int
    memory_gb: int

    @property
    def label(self):
        return f"{self.ocpus} OCPU / {self.memory_gb} GB"


PREFERRED = Profile(2, 12)
SMALLER = Profile(1, 6)


@dataclass
class Budget:
    seconds: int
    clock: object = time.monotonic
    sleeper: object = time.sleep
    reserve: int = 30
    started: float = field(init=False)

    def __post_init__(self):
        self.started = self.clock()

    @property
    def elapsed(self):
        return self.clock() - self.started

    def can_start(self, cost=90):
        return self.seconds - self.elapsed >= cost + self.reserve

    def require(self, cost=25):
        if not self.can_start(cost):
            raise BudgetExpired("Run budget exhausted; leaving time for reporting.")

    def sleep(self, seconds):
        if not self.can_start(seconds):
            return False
        self.sleeper(seconds)
        return True


@dataclass
class Instance:
    id: str
    state: str
    ad: str = ""


@dataclass
class Plan:
    candidates: list[tuple[str, Profile]]
    existing: Instance | None = None


@dataclass
class Result:
    outcome: str = "search_completed_without_capacity"
    instance: Instance | None = None
    counts: Counter = field(default_factory=Counter)
    details: str = ""


def classify_error(exc):
    """Only explicit capacity/throttle rejections permit a fresh creation request."""
    status = getattr(exc, "status", None)
    code = getattr(exc, "code", "")
    message = str(getattr(exc, "message", "")).lower()
    if code in {
        "LimitExceeded",
        "QuotaExceeded",
        "NotAuthenticated",
        "NotAuthorizedOrNotFound",
    }:
        return "fatal"
    if status == 429:
        return "throttled"
    if status in {400, 500, 503} and (
        "out of host capacity" in message or "out of capacity for shape" in message
    ):
        return "capacity"
    if status is not None and 400 <= status < 500:
        return "fatal"
    # A timeout, connection loss, or unrecognized server failure may follow acceptance.
    return "uncertain"


def error_description(exc):
    return (
        f"status={getattr(exc, 'status', 'transport')} "
        f"code={getattr(exc, 'code', type(exc).__name__)} "
        f"request_id={getattr(exc, 'request_id', '-')}: "
        f"{getattr(exc, 'message', 'The request outcome is uncertain.')}"
    )


class Hunter:
    def __init__(self, gateway, store, budget, *, rng=None, log=print):
        self.gateway = gateway
        self.store = store
        self.budget = budget
        self.rng = rng or random.SystemRandom()
        self.log = log
        self.result = Result()

    def run(self, profiles=(PREFERRED,), *, preflight_only=False):
        self.gateway.verify_home()
        state = {} if preflight_only else self.store.load()
        if state.get("phase") in {"accepted", "running"}:
            self.result.instance = self.gateway.get_instance(state["instance_id"])
            return self._track(state)
        plan = self.gateway.preflight(profiles)
        if preflight_only:
            self.result.outcome = "preflight_passed"
            self.result.instance = plan.existing
            self.result.details = (
                f"{len(plan.candidates)} eligible domain/size combinations."
            )
            return self.result
        if plan.existing:
            state = {
                "phase": "accepted",
                "instance_id": plan.existing.id,
                "availability_domain": plan.existing.ad,
            }
            self.store.save(state)
            self.result.instance = plan.existing
            return self._track(state)
        pending = state if state.get("phase") == "pending" else None
        pending_uncertain = pending is not None
        if pending:
            self.gateway.validate_pending(pending)
        candidates = []
        throttles = 0
        uncertainties = 0
        while self.budget.can_start():
            if not pending:
                if not candidates:
                    # Preferred size gets one sweep across every AD before fallback sizes.
                    for profile in profiles:
                        batch = [c for c in plan.candidates if c[1] == profile]
                        self.rng.shuffle(batch)
                        candidates.extend(batch)
                if not candidates:
                    raise AttentionRequired(
                        "No domain has quota for an allowed configuration."
                    )
                ad, profile = candidates.pop(0)
                # Check the exact size selected, not whether some smaller size would fit.
                existing = self.gateway.guard_before_launch((profile,))
                if existing:
                    state = {
                        "phase": "accepted",
                        "instance_id": existing.id,
                        "availability_domain": existing.ad,
                    }
                    self.store.save(state)
                    self.result.instance = existing
                    return self._track(state)
                pending = self.gateway.prepare(ad, profile)
                pending.update(
                    phase="pending",
                    retry_token=str(uuid.uuid4()),
                    started_at=time.time(),
                )
                # This durable write must be acknowledged BEFORE any OCI creation call.
                self.store.save(pending)
                pending_uncertain = False
            else:
                existing = self.gateway.find_existing()
                if existing:
                    state = copy.deepcopy(pending)
                    state.update(phase="accepted", instance_id=existing.id)
                    self.store.save(state)
                    self.result.instance = existing
                    return self._track(state)
                self.gateway.validate_pending(pending)
            snapshot = pending["request"]
            key = f"{snapshot['availability_domain']} | {snapshot['ocpus']}/{snapshot['memory_gb']}"
            self.result.counts[f"attempts: {key}"] += 1
            self.log(f"Attempt {key}; explicit retry policy; automatic fault domain.")
            try:
                instance = self.gateway.launch(pending)
            except BudgetExpired:
                break
            except Exception as exc:
                kind = classify_error(exc)
                self.result.counts[kind] += 1
                self.log(f"{kind}: {error_description(exc)}")
                if kind == "capacity" or (
                    kind in {"throttled", "fatal"} and not pending_uncertain
                ):
                    # These are explicit rejections, so advancing to a new token is safe.
                    self.store.save({"phase": "idle"})
                    pending = None
                    uncertainties = 0
                if kind == "fatal":
                    # A rejection of a replay does not resolve an earlier lost response.
                    # Preserve that checkpoint instead of allowing a new token next run.
                    raise AttentionRequired(error_description(exc)) from exc
                if kind == "throttled":
                    throttles += 1
                    delay = self.rng.uniform(60, min(300, 60 * 2**throttles))
                elif kind == "uncertain":
                    pending_uncertain = True
                    uncertainties += 1
                    # Keep the exact request/token, including across subsequent workflow runs.
                    delay = self.rng.uniform(30, min(120, 30 * 2**uncertainties))
                else:
                    throttles = 0
                    delay = self.rng.uniform(30, 60)
                if not self.budget.sleep(delay):
                    break
                continue
            state = copy.deepcopy(pending)
            state.update(phase="accepted", instance_id=instance.id)
            # If this write fails, the durable pending record still permits reconciliation.
            self.store.save(state)
            self.result.instance = instance
            return self._track(state)
        if pending:
            self.result.outcome = "launch_outcome_unresolved"
            self.result.details = (
                "Saved request retained; next run reconciles the same token."
            )
        return self.result

    def _track(self, state):
        while True:
            instance = self.result.instance
            if instance.state == "RUNNING":
                state = copy.deepcopy(state)
                state.update(phase="running", instance_id=instance.id)
                self.store.save(state)
                # Notification failure is visible and retryable without creating another VM.
                self.store.notify(instance)
                self.result.outcome = "vm_running"
                return self.result
            if instance.state not in {"PROVISIONING", "STARTING"}:
                raise AttentionRequired(
                    f"Existing instance is {instance.state}; inspect it in OCI. No new launch."
                )
            self.result.outcome = "launch_accepted_provisioning"
            if not self.budget.can_start(50) or not self.budget.sleep(15):
                return self.result
            self.result.instance = self.gateway.get_instance(instance.id)
