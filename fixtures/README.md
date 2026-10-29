Fixtures captured 2026-10-29 by local Claude from the RunPod MCP (read-only calls), for offline development.
- runpod_billing_monthly.json: list-billing bucketSize=month lastN=12 (account total $158.20 since 2026-04).
- runpod_audit_sample.json: list-audit-logs (pod create/delete/stop events carry podId + timestamps; create.value has machineId but NOT gpu type or data center).
Known gap: GPU type / data center of DELETED pods is not in billing or audit records; for 2026-09..10 it can be backfilled from ~/Desktop/PythonProjects/runpod/ag_gemma4devagentcomp/experiments/*.yaml and journal/claude/*.md (read-only).
