"""A durable pre-launch journal in one GitHub issue, using the workflow token."""

import copy
import json
import os
import re
import urllib.error
import urllib.request

from .core import AttentionRequired

TITLE = "OCI A1 hunter state (automated)"
MARKER = "<!-- oci-a1-hunter-state:v1 -->"


def encode_state(state):
    return (
        f"{MARKER}\nThis issue is the hunter's durable launch checkpoint. "
        "Do not close or edit it while the hunter is enabled. "
        "It contains launch metadata and an idempotency token, never API credentials.\n\n"
        f"```json\n{json.dumps(dict(schema=1, **state), indent=2, sort_keys=True)}\n```\n"
    )


def decode_state(body):
    match = re.search(r"```json\n(.*?)\n```", body, re.S)
    if MARKER not in body or not match:
        raise AttentionRequired(
            "Hunter state issue is malformed; inspect it before resuming."
        )
    try:
        state = json.loads(match.group(1))
    except (ValueError, TypeError) as exc:
        raise AttentionRequired("Hunter state JSON is invalid.") from exc
    if not isinstance(state, dict) or state.pop("schema", None) != 1:
        raise AttentionRequired("Unknown hunter state schema.")
    if state.get("phase") not in {"idle", "pending", "accepted", "running"}:
        raise AttentionRequired("Unknown hunter state phase.")
    if state["phase"] in {"accepted", "running"} and not state.get("instance_id"):
        raise AttentionRequired("Saved instance ID is missing.")
    return state


class GitHubState:
    def __init__(self, repository=None, token=None, budget=None):
        self.repository = repository or os.environ.get("GITHUB_REPOSITORY", "")
        self.token = token or os.environ.get("GH_TOKEN", "")
        if (
            not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.repository)
            or not self.token
        ):
            raise AttentionRequired(
                "GITHUB_REPOSITORY and GH_TOKEN are required for live hunting."
            )
        self.budget = budget
        self.issue_number = None
        self.cached = None

    def _request(self, method, suffix, payload=None):
        if self.budget:
            self.budget.require(12)
        request = urllib.request.Request(
            f"https://api.github.com/repos/{self.repository}/{suffix}",
            data=None if payload is None else json.dumps(payload).encode(),
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "Content-Type": "application/json",
                "User-Agent": "oci-a1-hunter",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            raise AttentionRequired(
                f"GitHub checkpoint/notification API returned HTTP {exc.code}; "
                "no further OCI creation requests will be sent."
            ) from exc
        except (OSError, ValueError) as exc:
            raise AttentionRequired(
                "GitHub checkpoint/notification could not be confirmed; stopping safely."
            ) from exc

    def _issues(self):
        page = 1
        while True:
            issues = self._request("GET", f"issues?state=all&per_page=100&page={page}")
            yield from (issue for issue in issues if "pull_request" not in issue)
            if len(issues) < 100:
                break
            page += 1

    def load(self):
        matches = [
            issue
            for issue in self._issues()
            if issue["title"] == TITLE or MARKER in (issue.get("body") or "")
        ]
        if len(matches) > 1:
            raise AttentionRequired(
                "Multiple hunter state issues exist; reconcile them before resuming."
            )
        if not matches:
            self.cached = {}
            return {}
        issue = matches[0]
        if issue["state"] != "open":
            raise AttentionRequired(
                "Hunter state issue is closed; inspect and reopen it to resume."
            )
        self.issue_number = issue["number"]
        self.cached = decode_state(issue.get("body") or "")
        return copy.deepcopy(self.cached)

    def save(self, state):
        if self.cached is None:
            raise AttentionRequired("Checkpoint must be loaded before saving.")
        if state == self.cached:
            return
        body = encode_state(state)
        if self.issue_number is None:
            result = self._request("POST", "issues", {"title": TITLE, "body": body})
            self.issue_number = result["number"]
        else:
            result = self._request(
                "PATCH", f"issues/{self.issue_number}", {"body": body}
            )
        if (
            result.get("state") != "open"
            or decode_state(result.get("body") or "") != state
        ):
            raise AttentionRequired(
                "GitHub did not confirm the exact checkpoint; stopping safely."
            )
        self.cached = copy.deepcopy(state)

    def notify(self, instance):
        # Explicit repository scope, with a marker to recover a lost notification response.
        marker = f"<!-- oci-a1-hunter-success:{instance.id} -->"
        if any(marker in (issue.get("body") or "") for issue in self._issues()):
            return
        self._request(
            "POST",
            "issues",
            {
                "title": "✅ tott-web A1 is RUNNING",
                "body": f"{marker}\nThe hunter verified that tott-web is RUNNING in "
                f"{instance.ad}. Find its public IP in OCI Compute → Instances. "
                "Disable the hunt-a1 workflow when ready. The durable checkpoint "
                "prevents subsequent runs from launching another VM.",
            },
        )
