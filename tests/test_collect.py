from copy import deepcopy
from datetime import timedelta
import json
from pathlib import Path
from types import SimpleNamespace
import pytest

from dashboard.collect import update_provider
from dashboard.core import identity, intervals, merge_resource, number, summarize, timestamp
from dashboard.providers import AWS, Kaggle, RunPod

ROOT = Path(__file__).resolve().parents[1]
NOW = timestamp("2026-10-29T12:00:00Z")
CONFIG = json.loads((ROOT / "config/settings.json").read_text())
CONSTANTS = json.loads((ROOT / "config/constants.json").read_text())


def event(at, action):
    return {"at": f"2026-10-29T{at}:00:00Z", "action": action}


def test_intervals_restart_dedup_missing_start_and_open():
    state = {}
    events = [event("01", "stop"), event("02", "start"), event("03", "start"),
              event("04", "stop"), event("05", "start"), event("06", "stop"), event("10", "start")]
    merge_resource(state, "x", {"running": True}, events + events)
    spans = intervals(state["resources"]["x"], NOW)
    assert [int((b-a).total_seconds()/3600) for a,b in spans] == [2,1,2]
    state["resources"]["x"]["running"] = False
    assert len(intervals(state["resources"]["x"], NOW)) == 2


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1])
def test_invalid_numbers(value):
    with pytest.raises(ValueError):
        number(value)


def runpod_payloads():
    return {
        "/billing": {"records": [{"startTime": "2026-10-01T00:00:00Z", "totalAmount": 10}]},
        "/billing/pods": {"records": [{"podId": "secret-id", "gpuAmount": 8}]},
        "/pods": {"pods": [{"id": "secret-id", "name": "gemma-PRIVATE_TASK", "gpu": {"id": "NVIDIA L4", "count": 4}, "status": "RUNNING", "dataCenterId": "US-MO-2", "startedAt": "2026-10-29T10:00:00Z", "env": {"TOKEN": "SECRET_VALUE"}, "ssh": {"host": "private-host"}}], "pagination": {"hasNextPage": False}},
        "/audit-logs": {"auditLogs": [{"resourceType": "pod", "resourceId": "secret-id", "action": "create", "timestamp": "2026-10-29T10:00:00Z", "email": "private@example.com", "value": "TASK_TEXT"}], "pagination": {"hasNextPage": False}},
        "/catalog/datacenters": {"dataCenters": [{"id": "US-MO-2"}]}}


def test_runpod_sanitizes_idempotent_and_multigpu():
    payloads = runpod_payloads()
    calls = []
    def get(path, params):
        calls.append((path, deepcopy(params)))
        return payloads[path]
    state = {}
    collector = RunPod("DO_NOT_LEAK", get)
    collector.collect(state, CONFIG, NOW, {})
    first = deepcopy(state)
    collector.collect(state, CONFIG, NOW, {})
    assert first == state
    result = summarize(state, {**CONFIG, "grid_regions": {}}, CONSTANTS, NOW, "test")
    assert result["totals"]["gpu_hours_estimate"] == 8
    assert result["totals"]["spend_usd_known"] == 10
    assert result["totals"]["kgco2e_estimate"] is None
    serialized = json.dumps([state, result])
    for secret in ["secret-id", "PRIVATE_TASK", "SECRET_VALUE", "TASK_TEXT", "private@example.com", "private-host", "DO_NOT_LEAK"]:
        assert secret not in serialized
    assert calls[0][1]["startTime"] == CONFIG["billing_start"]


def test_pagination_and_loop_guard():
    responses = [ {"pods": [1], "pagination": {"hasNextPage": True, "nextCursor": "a"}},
                  {"pods": [2], "pagination": {"hasNextPage": False}} ]
    assert RunPod("", lambda *_: responses.pop(0)).pages("/pods", "pods") == [1,2]
    with pytest.raises(ValueError):
        RunPod("", lambda *_: {"pods": [], "pagination": {"hasNextPage": True, "nextCursor": "a"}}).pages("/pods", "pods")


def test_carbon_units_and_partial_coverage():
    state = {"resources": {"x": {"provider": "runpod", "gpu": "L4", "gpu_count": 4, "region": "test-fr", "running": False, "events": [event("02","start"),event("04","stop")]}, "y": {"provider": "runpod", "events": []}}}
    config = {**CONFIG, "grid_regions": {"test-fr": "FR"}}
    t = summarize(state, config, CONSTANTS, NOW, "test")["totals"]
    assert t["gpu_hours_estimate"] == 8
    assert t["kgco2e_estimate"] == round(8 * .072 * .020,4)
    assert t["resources_without_hours"] == 1


def test_billing_rate_fallback_does_not_use_disk_or_double_count():
    state = {"resources": {"x": {"provider": "runpod", "gpu_count": 2, "gpu_spend": 6.36, "historical_gpu_rate": 3.18, "events": []}}}
    assert summarize(state, CONFIG, CONSTANTS, NOW, "test")["totals"]["gpu_hours_estimate"] == 4
    state["resources"]["x"]["events"] = [event("02","start"),event("03","stop")]
    assert summarize(state, CONFIG, CONSTANTS, NOW, "test")["totals"]["gpu_hours_estimate"] == 2


def test_failed_source_preserves_data_and_redacts_logs(caplog):
    state = {"billing": {"2026-10": 10}, "sources": {"runpod": {"status": "ok", "as_of": "old"}}}
    def fail(s):
        s["billing"] = {}
        raise RuntimeError("SECRET_RESPONSE")
    assert not update_provider(state, "runpod", fail, NOW)
    assert state["billing"] == {"2026-10": 10}
    assert state["sources"]["runpod"]["as_of"] == "old"
    assert "SECRET_RESPONSE" not in caplog.text


def test_kaggle_quota_not_physical_gpu_hours():
    q = SimpleNamespace(time_used=timedelta(hours=4), total_time_allowed=timedelta(hours=30))
    client = SimpleNamespace(quota_view=lambda: SimpleNamespace(gpu_quota=q, quota_refresh_time=NOW))
    state = {}
    Kaggle(client).collect(state, NOW)
    assert state["kaggle"]["remaining_hours"] == 26
    assert summarize(state, CONFIG, CONSTANTS, NOW, "test")["totals"]["gpu_hours_estimate"] is None


class Paginator:
    def __init__(self, pages): self.pages = pages
    def paginate(self, **kwargs): return iter(self.pages)


def test_aws_deleted_instance_and_failed_event():
    def cloud_event(name, hour, failed=False):
        e = {"eventName": name, "eventTime": f"2026-10-29T{hour}:00:00Z", "responseElements": {"instancesSet": {"items": [{"instanceId": "i-private", "instanceType": "g6.xlarge"}]}}}
        if failed: e["errorCode"] = "AccessDenied"
        return {"CloudTrailEvent": json.dumps(e)}
    pages = {"describe_instances": [{"Reservations": []}], "describe_instance_types": [{"InstanceTypes": [{"InstanceType": "g6.xlarge", "GpuInfo": {"Gpus": [{"Name": "L4", "Count": 1}]}}]}], "lookup_events": [{"Events": [cloud_event("RunInstances", "02"), cloud_event("StopInstances", "03", True), cloud_event("TerminateInstances", "04")]}]}
    client = SimpleNamespace(get_paginator=lambda name: Paginator(pages[name]))
    session = SimpleNamespace(client=lambda *a, **kw: client)
    state = {}
    AWS(session).collect(state, {**CONFIG, "aws_regions": ["us-east-1"]}, NOW)
    assert summarize(state, CONFIG, CONSTANTS, NOW, "test")["totals"]["gpu_hours_estimate"] == 2
    assert "i-private" not in json.dumps(state)


def test_handoff_discrepancy_is_not_silently_corrected():
    state = json.loads((ROOT / "fixtures/handoff_summary.json").read_text())
    assert summarize(state, CONFIG, CONSTANTS, NOW, "offline")["totals"]["spend_usd_known"] == 158.21


def test_failed_or_disappeared_running_resource_freezes_at_last_evidence():
    r = {"provider": "runpod", "gpu_count": 1, "running": True,
         "last_seen_running": "2026-10-29T04:00:00Z", "events": [event("02", "start")]}
    state = {"resources": {"x": r}, "sources": {"runpod": {"status": "error"}}}
    totals = summarize(state, CONFIG, CONSTANTS, NOW, "test")["totals"]
    assert totals["gpu_hours_estimate"] == 2
    assert totals["running"] is None
    r["running"] = False
    assert summarize(state, CONFIG, CONSTANTS, NOW, "test")["totals"]["gpu_hours_estimate"] == 2


def test_grid_unit_conversions():
    assert CONSTANTS["grids"]["US-ERCT"]["kg_per_kwh"] == pytest.approx(736.629 * .45359237 / 1000)
    assert CONSTANTS["grids"]["US-CAMX"]["kg_per_kwh"] == pytest.approx(429.983 * .45359237 / 1000)
