"""Sanitized accounting model. Provider payloads never cross the public boundary."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from hashlib import sha256
import math
from typing import Any

Json = dict[str, Any]


def timestamp(value: str | datetime) -> datetime:
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("Timezone required")
    return dt.astimezone(timezone.utc)


def number(value: Any) -> float:
    n = float(value)
    if not math.isfinite(n) or n < 0:
        raise ValueError("Expected finite nonnegative number")
    return n


def identity(provider: str, resource_id: str) -> str:
    return sha256(f"{provider}:{resource_id}".encode()).hexdigest()[:24]


def project(name: str, approved: list[str], tag: str | None = None) -> str:
    candidate = tag if tag is not None else name.split("-", 1)[0]
    return candidate if candidate in approved else "unknown"


def merge_resource(state: Json, key: str, metadata: Json, events: list[Json]) -> None:
    old = state.setdefault("resources", {}).setdefault(key, {"events": []})
    old.update({k: v for k, v in metadata.items() if v is not None})
    unique = {(e["at"], e["action"]) for e in old["events"] + events}
    old["events"] = [{"at": at, "action": action} for at, action in sorted(unique)]


def intervals(resource: Json, now: datetime) -> list[tuple[datetime, datetime]]:
    """Union lifecycle intervals; never count stopped time or invent missing starts.

    Lifecycle actions approximate allocated time, not utilization. Only a resource
    observed running in this successful snapshot may have an open interval.
    """
    result: list[tuple[datetime, datetime]] = []
    start: datetime | None = None
    for event in resource.get("events", []):
        at = timestamp(event["at"])
        if at > now:
            continue
        if event["action"] == "start" and start is None:
            start = at
        elif event["action"] == "stop" and start is not None:
            if at > start:
                result.append((start, at))
            start = None
    end = timestamp(resource["last_seen_running"]) if resource.get("last_seen_running") else now
    if start is not None and (resource.get("running") or resource.get("last_seen_running")) and end > start:
        result.append((start, min(now, end)))
    return result


def summarize(state: Json, config: Json, constants: Json, now: datetime, mode: str) -> Json:
    groups: dict[str, dict[str, float]] = {k: defaultdict(float) for k in ("gpu", "project", "region")}
    hours = carbon = covered = 0.0
    missing_hours = 0
    longest: list[Json] = []
    running = 0
    for r in state.get("resources", {}).values():
        running += int(bool(r.get("running")) and state.get("sources", {}).get(r["provider"], {}).get("status") == "ok")
        spans = intervals(r, now)
        duration = sum((b - a).total_seconds() / 3600 for a, b in spans)
        count = number(r["gpu_count"]) if r.get("gpu_count") is not None else None
        gpu_hours: float | None = duration * count if count is not None and spans else None
        basis = "lifecycle estimate"
        # Historical rates are explicitly entered per resource, never guessed from a live price.
        if gpu_hours is None and r.get("historical_gpu_rate") and count:
            gpu_hours = number(r.get("gpu_spend", 0)) / number(r["historical_gpu_rate"]) * count
            basis = "cost / historical pod GPU rate estimate"
        if gpu_hours is None:
            missing_hours += 1
            continue
        hours += gpu_hours
        for key in groups:
            groups[key][r.get(key, "unknown")] += gpu_hours
        gpu = constants["gpus"].get(r.get("gpu"))
        grid_key = config.get("grid_regions", {}).get(r.get("region"))
        grid = constants["grids"].get(grid_key)
        if gpu and grid:
            carbon += gpu_hours * gpu["watts"] / 1000 * constants["pue"]["value"] * grid["kg_per_kwh"]
            covered += gpu_hours
        for a, b in spans:
            longest.append({"provider": r["provider"], "project": r.get("project", "unknown"),
                            "gpu": r.get("gpu", "unknown"), "region": r.get("region", "unknown"),
                            "hours": round((b-a).total_seconds()/3600, 3), "basis": basis})
    monthly = [{"month": k, "usd": round(v, 4)} for k, v in sorted(state.get("billing", {}).items())]
    return {
        "schema_version": 1, "generated_at": now.isoformat(), "mode": mode,
        "totals": {"gpu_hours_estimate": round(hours, 3) if hours else None,
                   "spend_usd_known": round(sum(x["usd"] for x in monthly), 2) if monthly else None,
                   "runpod_pods_observed": sum(r.get("provider") == "runpod" for r in state.get("resources", {}).values()),
                   "kgco2e_estimate": round(carbon, 4) if covered else None,
                   "carbon_covered_gpu_hours": round(covered, 3),
                   "resources_without_hours": missing_hours, "running": running if any(state.get("sources", {}).get(p, {}).get("status") == "ok" for p in ("runpod", "aws")) else None},
        "monthly_spend": monthly,
        "breakdowns": {k: [{"label": name, "hours": round(value, 3)} for name, value in sorted(v.items(), key=lambda x: -x[1])] for k, v in groups.items()},
        "longest_runs": sorted(longest, key=lambda x: -x["hours"])[:10],
        "sources": state.get("sources", {}), "kaggle": state.get("kaggle"),
        "constants": constants,
        "limitations": ["Spend covers RunPod only; AWS billing is not collected.",
                         "GPU-hours are partial allocation estimates, not measured utilization.",
                         "Unknown hardware, GPU count, runtime or grid factors are never filled with zero.",
                         "Carbon covers only matched GPU-hours; CPU, RAM, storage, manufacturing and offsets are excluded.",
                         "Create/start/stop requests can differ from actual runtime; missing events and retention gaps reduce coverage.",
                         "Kaggle quota-hours are separate from physical GPU-hours and are not added to lifetime totals."]}
