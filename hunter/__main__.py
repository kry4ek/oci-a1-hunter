"""Entrypoint used by Actions and local runs with environment-provided credentials."""

import argparse
import os
import sys
import traceback

from .core import (
    AttentionRequired,
    Budget,
    BudgetExpired,
    Hunter,
    PREFERRED,
    Result,
    SMALLER,
)
from .github_state import GitHubState
from .oracle import OracleGateway


def write_summary(result, budget, log):
    lines = [
        "## Oracle A1 search",
        "",
        f"Outcome: **{result.outcome}**",
        f"Elapsed: {budget.elapsed:.1f} seconds",
        "",
        result.details,
        "",
        "| Counter | Count |",
        "| --- | ---: |",
    ]
    lines.extend(
        f"| {key.replace('|', '/')} | {value} |"
        for key, value in sorted(result.counts.items())
    )
    summary = "\n".join(lines) + "\n"
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(summary)
    log(f"Outcome: {result.outcome}; elapsed={budget.elapsed:.1f}s; {result.details}")


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("preflight", "hunt"), default="preflight")
    parser.add_argument("--duration-seconds", type=int, default=720)
    parser.add_argument("--allow-smaller", action="store_true")
    args = parser.parse_args(argv)
    if not 120 <= args.duration_seconds <= 720:
        parser.error("duration must be between 120 and 720 seconds")
    budget = Budget(args.duration_seconds)
    sensitive = [
        os.environ.get(name, "")
        for name in (
            "OCI_CLI_KEY_CONTENT",
            "GH_TOKEN",
            "SSH_PUBLIC_KEY",
            "OCI_CLI_USER",
            "OCI_CLI_TENANCY",
            "OCI_CLI_FINGERPRINT",
        )
    ]

    def log(message):
        for value in sensitive:
            if value:
                message = message.replace(value, "[redacted]")
        print(message.replace("\r", " ").replace("\n", " "), flush=True)

    result = Result()
    runner = None
    exit_code = 0
    try:
        gateway = OracleGateway(budget)
        store = None if args.mode == "preflight" else GitHubState(budget=budget)
        runner = Hunter(gateway, store, budget, log=log)
        profiles = (PREFERRED, SMALLER) if args.allow_smaller else (PREFERRED,)
        result = runner.run(profiles, preflight_only=args.mode == "preflight")
    except BudgetExpired:
        result = runner.result if runner else result
        result.outcome = "run_budget_exhausted"
        result.details = (
            "Budget ended cleanly; any pending launch checkpoint is retained."
        )
        exit_code = 1 if args.mode == "preflight" else 0
    except Exception as exc:
        result = runner.result if runner else result
        result.outcome = "attention_required"
        if isinstance(exc, AttentionRequired):
            log(str(exc))
        elif hasattr(exc, "status"):
            from .core import error_description

            log(error_description(exc))
        else:
            log(
                f"Preflight/runtime failure ({type(exc).__name__}); no further creation requests."
            )
            for frame in traceback.extract_tb(exc.__traceback__)[-4:]:
                log(f"Location: {os.path.basename(frame.filename)}:{frame.lineno} in {frame.name}")
        result.details = "Inspect the sanitized error above before resuming."
        exit_code = 1
    finally:
        write_summary(result, budget, log)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
