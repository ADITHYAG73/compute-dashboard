"""Read-only provider adapters, with injected clients for offline tests."""
from __future__ import annotations

from datetime import datetime, timedelta
import json
import logging
import time
from typing import Any, Callable
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .core import Json, identity, merge_resource, number, project, timestamp

LOG = logging.getLogger(__name__)


class RunPod:
    BASE = "https://api.runpod.io/v2"

    def __init__(self, key: str, get: Callable[..., Json] | None = None) -> None:
        self.key = key
        self.get = get or self._get

    def _get(self, path: str, params: Json) -> Json:
        request = Request(self.BASE + path + "?" + urlencode(params), headers={"Authorization": f"Bearer {self.key}"})
        for attempt in range(4):
            try:
                with urlopen(request, timeout=30) as response:
                    return json.load(response)
            except HTTPError as exc:
                if exc.code not in (429, 500, 502, 503, 504) or attempt == 3:
                    raise RuntimeError(f"RunPod HTTP {exc.code}") from None
                time.sleep(2 ** attempt)
        raise RuntimeError("RunPod retries exhausted")

    def pages(self, path: str, field: str) -> list[Json]:
        result: list[Json] = []
        params: Json = {"limit": 200}
        seen: set[str] = set()
        while True:
            page = self.get(path, params)
            result.extend(page[field])
            pagination = page["pagination"]
            if not pagination["hasNextPage"]:
                return result
            cursor = pagination["nextCursor"]
            if not cursor or cursor in seen:
                raise ValueError("Invalid pagination cursor")
            seen.add(cursor)
            params = {**params, "cursor": cursor}

    def collect(self, state: Json, config: Json, now: datetime, backfill: Json) -> None:
        params = {"startTime": config["billing_start"], "endTime": now.isoformat(), "bucketSize": "month"}
        billing = self.get("/billing", params)["records"]
        pod_billing = self.get("/billing/pods", params)["records"]
        pods = self.pages("/pods", "pods")
        audits = self.pages("/audit-logs", "auditLogs")
        centers = {r["id"] for r in self.get("/catalog/datacenters", {})["dataCenters"]}
        state["billing"] = {r["startTime"][:7]: number(r["totalAmount"]) for r in billing}
        for r in state.get("resources", {}).values():
            if r["provider"] == "runpod":
                r["running"] = False
        ids = {r["podId"] for r in pod_billing} | {r["id"] for r in pods}
        ids |= {r["resourceId"] for r in audits if r.get("resourceType") == "pod"}
        live = {r["id"]: r for r in pods}
        for raw_id in ids:
            key = identity("runpod", raw_id)
            historical = backfill.get(key, {})
            p = live.get(raw_id, {})
            gpu = p.get("gpu") or {}
            # Explicit allowlists prevent arbitrary names/tags from becoming public data.
            gpu_name = config["gpu_aliases"].get(gpu.get("id"), historical.get("gpu"))
            region = p.get("dataCenterId") or historical.get("region")
            if region and region not in centers and region not in config["regions"]:
                region = None
            meta = {"provider": "runpod", "gpu": gpu_name,
                    "gpu_count": gpu.get("count", historical.get("gpu_count")),
                    "region": region,
                    "running": p.get("status") == "RUNNING",
                    "gpu_spend": sum(number(r["gpuAmount"]) for r in pod_billing if r["podId"] == raw_id),
                    "historical_gpu_rate": historical.get("historical_gpu_rate")}
            if p or historical:
                meta["project"] = project(p.get("name", ""), config["projects"], historical.get("project"))
            if meta["running"]:
                meta["last_seen_running"] = now.isoformat()
            events = []
            for a in audits:
                if a.get("resourceType") != "pod" or a.get("resourceId") != raw_id:
                    continue
                action = {"create": "start", "start": "start", "resume": "start", "stop": "stop", "delete": "stop", "terminate": "stop"}.get(a["action"])
                if action:
                    events.append({"at": a["timestamp"], "action": action})
            if p.get("startedAt") and p.get("status") == "RUNNING":
                events.append({"at": p["startedAt"], "action": "start"})
            merge_resource(state, key, meta, events)
        state.setdefault("sources", {})["runpod"] = {"status": "ok", "as_of": now.isoformat(), "basis": "[sourced] REST v2; lifecycle hours are [inference]"}
        LOG.info("runpod collected billing_buckets=%d resources=%d audit_events=%d", len(billing), len(ids), len(audits))


class AWS:
    def __init__(self, session: Any) -> None:
        self.session = session

    @staticmethod
    def lifecycle_pages(trail: Any, start: datetime, now: datetime) -> list[Json]:
        pages = []
        for name in ("RunInstances", "StartInstances", "StopInstances", "TerminateInstances"):
            for page in trail.get_paginator("lookup_events").paginate(
                LookupAttributes=[{"AttributeKey": "EventName", "AttributeValue": name}],
                StartTime=start, EndTime=now):
                pages.append(page)
                time.sleep(0.55)  # LookupEvents limit: two requests/second per region.
        return pages

    def collect(self, state: Json, config: Json, now: datetime) -> None:
        from botocore.config import Config
        options = Config(retries={"mode": "standard", "total_max_attempts": 4}, connect_timeout=10, read_timeout=30)
        for r in state.get("resources", {}).values():
            if r["provider"] == "aws":
                r["running"] = False
        for region in config["aws_regions"]:
            ec2 = self.session.client("ec2", region_name=region, config=options)
            trail = self.session.client("cloudtrail", region_name=region, config=options)
            instances = [i for page in ec2.get_paginator("describe_instances").paginate() for reservation in page["Reservations"] for i in reservation["Instances"]]
            events: dict[str, list[Json]] = {}
            types: dict[str, str] = {i["InstanceId"]: i["InstanceType"] for i in instances}
            previous = state.get("sources", {}).get("aws", {}).get("as_of")
            start = max(now - timedelta(days=89), timestamp(previous) - timedelta(days=1)) if previous else now - timedelta(days=89)
            for page in self.lifecycle_pages(trail, start, now):
                for item in page["Events"]:
                    event = json.loads(item["CloudTrailEvent"])
                    action = {"RunInstances": "start", "StartInstances": "start", "StopInstances": "stop", "TerminateInstances": "stop"}.get(event.get("eventName"))
                    if not action or event.get("errorCode"):
                        continue
                    response = (event.get("responseElements") or {}).get("instancesSet", {}).get("items", [])
                    requested = (event.get("requestParameters") or {}).get("instancesSet", {}).get("items", [])
                    for instance in response or requested:
                        rid = instance.get("instanceId")
                        if not rid:
                            continue
                        events.setdefault(rid, []).append({"at": event["eventTime"], "action": action})
                        if instance.get("instanceType"):
                            types[rid] = instance["instanceType"]
            specs: Json = {}
            if types:
                for page in ec2.get_paginator("describe_instance_types").paginate(InstanceTypes=sorted(set(types.values()))):
                    for spec in page["InstanceTypes"]:
                        specs[spec["InstanceType"]] = spec.get("GpuInfo", {}).get("Gpus", [])
            current = {i["InstanceId"]: i for i in instances}
            for rid in set(events) | set(current):
                instance = current.get(rid, {})
                tags = {t["Key"]: t["Value"] for t in instance.get("Tags", [])}
                gpus = specs.get(types.get(rid, ""))
                meta: Json = {"provider": "aws", "region": region,
                              "running": instance.get("State", {}).get("Name") == "running"}
                if instance:
                    meta["project"] = project(tags.get("Name", ""), config["projects"], tags.get("Project"))
                if meta["running"]:
                    meta["last_seen_running"] = now.isoformat()
                if gpus is not None:
                    meta["gpu_count"] = sum(g["Count"] for g in gpus)
                    names = {config["gpu_aliases"].get(g["Name"], "unknown") for g in gpus}
                    meta["gpu"] = names.pop() if len(names) == 1 else "unknown"
                # LaunchTime may change on restart; use only as current-session evidence.
                evs = events.get(rid, [])
                if meta["running"] and instance.get("LaunchTime"):
                    evs.append({"at": instance["LaunchTime"].isoformat(), "action": "start"})
                merge_resource(state, identity("aws", rid), meta, evs)
            LOG.info("aws collected region=%s instances=%d event_resources=%d", region, len(instances), len(events))
        state.setdefault("sources", {})["aws"] = {"status": "ok", "as_of": now.isoformat(), "basis": "[sourced] EC2 + 89-day CloudTrail lookback; runtime [inference]; spend unavailable"}


class Kaggle:
    def __init__(self, client: Any) -> None:
        self.client = client

    def collect(self, state: Json, now: datetime) -> None:
        result = self.client.quota_view()
        if result.gpu_quota is None:
            raise ValueError("GPU quota unavailable")
        used = number(result.gpu_quota.time_used.total_seconds()) / 3600
        total = number(result.gpu_quota.total_time_allowed.total_seconds()) / 3600
        state["kaggle"] = {"used_hours": used, "total_hours": total,
                           "remaining_hours": max(0, total-used),
                           "refresh_at": result.quota_refresh_time.isoformat() if result.quota_refresh_time else None,
                           "as_of": now.isoformat(), "basis": "[sourced] Kaggle weekly quota; not physical GPU-hours"}
        state.setdefault("sources", {})["kaggle"] = {"status": "ok", "as_of": now.isoformat()}
