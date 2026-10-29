# Compute / field notes

A public, static compute ledger for RunPod, AWS EC2 and Kaggle. **Local v1; not published.** No provider credentials have been used during development. All collector tests use synthetic responses.

## Preview locally

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pytest -q
.venv/bin/python -m mypy dashboard
.venv/bin/python -m dashboard.collect --offline
.venv/bin/python -m http.server 8879 --bind 127.0.0.1 --directory site
```

Open http://127.0.0.1:8879. The page uses local HTML/CSS/JS and inline SVG, no CDN or build server. The Python collector requires the repository checkout (editable installation).

**Current evidence:** [sourced] `fixtures/README.md` names billing/audit captures, but those files were absent on 2026-10-29. `fixtures/handoff_summary.json` explicitly records the user's handoff rather than pretending to be an API response. Its monthly figures total **$158.21**, versus the stated aggregate **$158.20**. July was absent and is not treated as zero. The reported 49 Sep–Oct pods cannot yet be reconciled. The preview therefore shows unavailable GPU-hours/carbon and no invented runs.

## What is collected

| Provider | Evidence and limits |
| --- | --- |
| RunPod | [sourced] Official live [OpenAPI](https://api.runpod.io/v2/openapi.json), checked 2026-10-29: base `https://api.runpod.io/v2`; GET `/billing`, `/billing/pods`, `/audit-logs`, `/pods`, `/catalog/datacenters`. Billing uses a fixed start date; pod/audit cursor pages are exhausted. No calls to task/container endpoints. |
| AWS | [sourced] boto3 EC2 `DescribeInstances`, `DescribeInstanceTypes`, CloudTrail `LookupEvents`. [AWS documents free 90-day event history](https://aws.amazon.com/cloudtrail/pricing/). First scan uses 89 days; subsequent scans overlap the last success by one day. Failed operations are ignored. Runtime is an [inference] from lifecycle request timestamps. Automatic or lost state transitions can leave gaps; no metered runtime claim. |
| Kaggle | [sourced] Official [client at the reviewed commit](https://github.com/Kaggle/kaggle-cli/blob/a636fc70230c9e8feeb33a525dce1a07df62fbea/src/kaggle/api/kaggle_api_extended.py) exposes `quota_view()` with `gpu_quota.time_used`, `total_time_allowed`, and `quota_refresh_time`. The SDK metadata for `kernels_list()` exposes `last_run_time`, `enable_gpu` and `machine_shape` (last-session accelerator hints), but no duration field or complete GPU accounting ledger. Quota is collected directly with Kaggle 2.2.5; manual `config/kaggle.yaml` is available without credentials. Quota-hours never become physical GPU-hours. |

AWS spend is **unavailable**, not zero: no Cost Explorer calls, paid trail, CloudTrail Lake, S3, or CloudWatch services are provisioned. RunPod billing includes non-GPU account charges; GPU-hour rate estimates use only per-pod GPU cost.

## Configuration the user must review

Edit `config/settings.json`:

- `projects`: public project allowlist. Pod `<project>-<purpose>` and EC2 `Project` tag (preferred over Name prefix) resolve to that list; other names become `unknown`. A hyphen inside a project slug requires the EC2 tag or explicit backfill.
- `aws_regions`: explicitly list every AWS region to scan, e.g. `["us-east-1"]`. Empty disables AWS. Match the IAM policy's allowed regions.
- `gpu_aliases`: exact provider hardware labels to verified models; ambiguous A100/H100 labels stay unknown. Add model-specific NVIDIA power sources before using new cards for carbon.
- `grid_regions`: explicit data center/region to factor mapping, e.g. `{"eu-west-3": "FR"}` after confirming location. Three journal-identified U.S. facilities use the cited EPA national average as an explicitly coarse [inference]; all other locations are unmapped. Do not infer an electricity subregion from a state alone.
- `billing_start`: earliest accounting month, initially April 2026. Collection replaces returned monthly totals, so current-month revisions do not double count.

`data/backfill.json` contains seven verified metadata associations from dated local operational journals: hardware, count, region when known and historical pod GPU rate. Keys are truncated SHA-256 identifiers, not raw pod IDs. No task text, traces, notebook content, addresses, credentials or competition files were copied. These entries only enrich matching billing/audit evidence; they do not create invented runs. [inference] A historical rate may not hold throughout a resource's life; fallback hours are always labelled estimates. H200 records were left unknown because the specific form factor was not established.

## User-created secrets and IAM

The user creates credentials; this project never creates or rotates them. Add these under **Settings → Secrets and variables → Actions → Repository secrets** after approving publication:

| Secret | Required for |
| --- | --- |
| `RUNPOD_API_KEY` | RunPod key restricted to `billing:read`, `audit:read`, `pods:read`, `catalog:read` per the v2 OpenAPI. No write scopes. Verify the account includes audit access; a 403 is reported as source failure, never as zero activity. |
| `AWS_ACCESS_KEY_ID` | AWS read-only principal with the policy below. |
| `AWS_SECRET_ACCESS_KEY` | Matching secret access key. |
| `AWS_SESSION_TOKEN` | Only if using temporary credentials; those expire and must be refreshed by the user. Omit for an IAM access key. |
| `KAGGLE_API_TOKEN` | Preferred Kaggle token from the user's Kaggle account. |
| `KAGGLE_USERNAME` + `KAGGLE_KEY` | Alternative legacy Kaggle credentials; use instead of `KAGGLE_API_TOKEN`, not in addition. No Kaggle credentials needed for the manual YAML fallback. |

The proposed policy is [docs/aws-read-only-policy.json](docs/aws-read-only-policy.json):

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Sid": "ComputeDashboardReadOnly",
    "Effect": "Allow",
    "Action": ["ec2:DescribeInstances", "ec2:DescribeInstanceTypes", "cloudtrail:LookupEvents"],
    "Resource": "*",
    "Condition": {"StringEquals": {"aws:RequestedRegion": ["ap-south-1"]}}
  }]
}
```

[inference] This is the smallest action set used by this implementation; account-wide describe/lookup operations use `Resource: "*"`. Replace the example region list before attaching it to a dedicated principal. No billing or mutation permission is required. A future OIDC role can avoid long-lived AWS credentials, but is not created here.

## Publication — only after user approval

1. Resolve the missing fixtures/one-cent discrepancy and review the public project names, region disclosure, sanitized ledger, and page.
2. **The user must approve before any GitHub repository is created or anything is pushed.** No remote is configured by this implementation.
3. After approval, create a public repository, push the reviewed files, and set its default branch. Keep `.env`, `private/`, captures and local artifacts excluded. The entire repository will be public, not just `site/`.
4. Select **Settings → Pages → Build and deployment → Source: GitHub Actions**. Permit Actions to write repository contents; branch protection must permit the snapshot commit or the workflow fails safely without force-pushing.
5. Add the user-created secrets. Add repository variable **`DASHBOARD_ENABLED=true`** to activate the workflow, then run **Refresh compute dashboard** manually once.
6. The workflow runs at minutes 7 and 37 each hour, serializes overlapping runs, commits only `data/state.json` and `site/data.json`, and uploads only `site/`. It does not expose collector state as part of the Pages artifact, though the sanitized state remains visible in the public repository.

[sourced] [Public standard GitHub-hosted runners are free](https://docs.github.com/en/billing/concepts/product-billing/github-actions); [Pages supports public repositories on GitHub Free](https://docs.github.com/en/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages). The workflow refuses private repositories, uses standard Ubuntu, no cache, and retains a small Pages artifact for one day. No billed infrastructure is created. [sourced] [Scheduled workflows may be delayed and are disabled after 60 days without repository activity](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule); “every 30 minutes” is a target, not a freshness guarantee. The page marks snapshots older than 90 minutes stale.

## Accounting, privacy and resilience

- The public JSON is constructed from an allowlist: aggregate spend, time, approved project labels, GPU/region breakdowns, anonymous longest runs, source freshness and cited constants. HTML uses `textContent`, not interpolated HTML.
- The repository ledger stores hashed resource identities, sanitized hardware/project/region, lifecycle timestamps and costs so deletion/retention does not erase already collected history. Hashing is pseudonymization, not encryption; anyone already knowing a raw ID can match it. No audit actor, email, event payload, EC2 tag dump, instance name, environment, IP, SSH or task content is persisted.
- Duplicate lifecycle events are merged; starts while running do not add overlapping intervals. Stops close a run; stopped time is excluded. Open runs end at the last successful running observation. On failure, the provider's prior dataset and last-success timestamp remain, with an error badge; other providers can refresh. API bodies and exception messages are never logged. Debug logs contain only counts, source/region and exception class.
- [inference] Lifecycle-based GPU-hours are allocated-time estimates. Create/stop requests, missing events, hardware changes on a reused resource, and spot interruptions limit accuracy. Historical hardware is assumed constant per resource; changing configuration requires separate accounting or review. Unmatched count/model/time remains unknown. The carbon number is only the covered subset, never an account-wide footprint.
- [sourced] The requested [Lacoste et al. paper](https://arxiv.org/pdf/1910.09700) and [calculator](https://mlco2.github.io/impact/) motivate hardware × runtime × grid intensity. The calculator instructs users to apply PUE separately. Every implemented constant is dated and linked in `config/constants.json` and on the page. PUE 1.0 is an explicit [inference] for a GPU-only baseline; no facility overhead, CPU, RAM, storage, embodied emissions or offsets are claimed.

## Remaining questions

- Where are the billing, per-pod billing and audit captures described in the handoff? Which total resolves the one-cent difference?
- Which AWS regions and public project slugs should be enabled?
- Which facilities have verified grid mapping and PUE, and what are the exact H200/other unmatched GPU variants?
- Does the RunPod account's read-only key include audit access? Live integration remains untested by instruction.
- Does the user approve publication, including sanitized resource history and regional information?
