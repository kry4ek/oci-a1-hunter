# OCI A1 hunter

Searches for **one** `VM.Standard.A1.Flex` instance named `tott-web` in Frankfurt,
using the account's existing `tott-vcn`, public subnet(s), and SSH public key.
The preferred configuration is **2 OCPUs / 12 GB RAM**, Ubuntu 22.04, and a 50 GB
balanced boot volume. A **1 OCPU / 6 GB** fallback is optional.

## What changed

- A 12-minute total budget, including preflight, scheduled at minutes 7, 22, 37,
  and 52. A separate 16-minute Actions timeout leaves room for installation and
  reporting. GitHub schedules can still be delayed or dropped.
- Explicit requests with SDK retries and circuit breaking disabled. The hunter
  visits each eligible AD before repeating it, waits 30–60 seconds after capacity
  rejection, and backs off further for throttling and uncertain responses.
- Home-region, tenancy-wide compute/storage inventory, effective quota,
  platform-image, and public-subnet checks. Read/permission failures stop the run.
- A checkpoint acknowledged by GitHub **before** each creation request, so a
  process kill or lost response can be reconciled in a subsequent run.
- Provisioning is tracked separately from RUNNING. Stopped or failed instances
  require attention instead of causing another creation attempt.
- Per-run summaries and a deduplicated issue when RUNNING is verified.

## Existing secrets

Keep the repository's existing Actions secrets:

| Secret | Value |
| --- | --- |
| `OCI_CLI_USER` | OCI API user OCID |
| `OCI_CLI_TENANCY` | Tenancy OCID |
| `OCI_CLI_FINGERPRINT` | API key fingerprint |
| `OCI_CLI_KEY_CONTENT` | PEM private API key, with actual newlines |
| `OCI_CLI_REGION` | `eu-frankfurt-1` |
| `SSH_PUBLIC_KEY` | One valid OpenSSH public key |

`GH_TOKEN` uses the workflow's built-in token. No extra GitHub token or new OCI
storage bucket is needed. The workflow needs `contents: read` and `issues: write`.

The OCI user needs read access to region subscriptions, compartments (including
children), instances, A1 container instances, boot/block volumes, images, shapes,
VCNs/subnets, and effective compute quotas. It also needs the existing permissions
to launch Compute instances, create boot volumes, and attach VNICs. An existing
administrator API user normally has these permissions. A narrowly scoped user
may need its IAM policy updated; failed reads are never treated as empty usage.

The root compartment placement and existing `tott-vcn` are preserved. Prefer one
regional public subnet. If using AD-specific subnets, provide one compatible
public subnet in each AD. Multiple equally suitable public subnets cause a clear
configuration error rather than an arbitrary choice.

## Validate and start

1. Run the `Validate hunter` checks on the pull request. Owner-created PRs within
   this repository also perform a **read-only OCI preflight** using the secrets.
   Fork PRs do not get secrets.
2. In Actions → `hunt-a1` → Run workflow, select the implementation branch,
   `mode=preflight`, and a 360-second budget. Preflight creates no OCI resources
   and writes no checkpoint or notification issue.
3. Run that branch with `mode=hunt` and a 180- or 360-second budget for a bounded
   live search. The `hunt-a1` concurrency group also serializes this with the
   original main-branch workflow. A queued run may wait for an existing run.
4. After review and merge, scheduled runs use `mode=hunt` and a 720-second budget.
   Compare actual start times and summaries over the next day against the old
   gaps; a cron change alone does not establish better coverage.
5. Once the success issue appears, obtain the public IP in OCI and disable the
   `hunt-a1` workflow. Subsequent runs otherwise track the recorded instance.

The implementation is not activated on the default branch until the PR is merged.

## Optional smaller VM

The fallback is disabled by default because the intended application may need
12 GB. Enable `allow_smaller` for a manual run, or set repository Actions variable
`HUNTER_ALLOW_SMALLER=true` for scheduled runs. The hunter tries a preferred-size
sweep before a smaller-size sweep and stops after one acceptable instance.
It does not resize the instance automatically; expansion needs available capacity.

## Durable state and recovery

The single issue **OCI A1 hunter state (automated)** is a journal, not a support
ticket. Its body contains an immutable request snapshot, a request fingerprint,
an idempotency token, timestamp, and (after acceptance) the instance ID. In this
public repository these resource identifiers are public metadata. It never
stores the API private key, GitHub token, or SSH public key.

- An explicit capacity/throttle rejection clears the pending checkpoint before
  moving to another domain or size.
- A timeout or unknown server error retains the exact request and token. The
  next run checks existing instances and then retries that same request.
- A changed configuration, malformed checkpoint, or pending token older than
  23 hours stops safely (Oracle retry tokens normally expire after 24 hours).
- A missing or inaccessible recorded instance does not restart creation.
- Closed, duplicated, or corrupted state issues stop the hunter.

For manual recovery, first disable the workflow and let any active run finish.
Inspect OCI instances (including other compartments and provisioning/stopped
states) and volumes to determine the original request's outcome. If no instance
was created and a fresh search is appropriate, keep the issue open and replace
only its JSON checkpoint with `{"schema": 1, "phase": "idle"}`. Preserve the
marker and fenced JSON format. Do not delete the state issue to bypass uncertainty.

The concurrency guard covers this GitHub workflow, not unrelated console users
or external scripts. Avoid launching competing instances during a hunt.

## Free-tier boundaries

The code permits Frankfurt only and verifies that it is the tenancy's home
region. It caps total observed A1 Compute allocation at 2 OCPUs / 12 GB and total
observed boot/block storage at 200 GB including the new 50 GB boot volume. It
checks all listed active compartments and blocks when A1 container allocations
need review. It never upgrades the account, changes regions, creates other
shapes, resizes, restarts, or deletes resources.

These conservative guards do not replace Oracle's billing and account rules.
Other workloads, historical monthly usage, concurrent provisioning, and later
manual changes can affect eligibility. Oracle capacity is not guaranteed.

Sources: [Always Free resources](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm),
[OCI retries](https://docs.oracle.com/en-us/iaas/tools/python/latest/sdk_behaviors/retries.html),
[GitHub schedules](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule).

## Local checks

Use Python 3.12:

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python -m compileall -q hunter tests
```

The unit suite simulates cloud responses and never contacts OCI. Running
`python -m hunter` defaults to read-only preflight; live search requires `--mode hunt`
and the same environment variables used by Actions.
