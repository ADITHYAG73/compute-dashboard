"""Run `python -m dashboard.collect --offline` without any credentials."""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import sys
from typing import Callable

import yaml

from .core import Json, number, summarize, timestamp
from .providers import AWS, Kaggle, RunPod

LOG = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[1]


def read(path: Path) -> Json:
    return json.loads(path.read_text())


def atomic_json(path: Path, data: Json) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True, allow_nan=False) + "\n")
    tmp.replace(path)


def update_provider(state: Json, provider: str, collect: Callable[[Json], None], now: datetime) -> bool:
    candidate = deepcopy(state)
    try:
        collect(candidate)
    except Exception as exc:
        # Only exception TYPE is logged: API error bodies can include private data.
        LOG.error("provider=%s failed error_type=%s; preserving previous data", provider, type(exc).__name__)
        old = state.setdefault("sources", {}).get(provider, {})
        state["sources"][provider] = {**old, "status": "error", "attempted_at": now.isoformat()}
        return False
    state.clear()
    state.update(candidate)
    return True


def manual_quota(state: Json, path: Path) -> None:
    data = yaml.safe_load(path.read_text())
    if data.get("as_of") is None:
        return
    as_of = timestamp(str(data["as_of"]))
    used, total = number(data["used_hours"]), number(data["total_hours"])
    state["kaggle"] = {"used_hours": used, "total_hours": total,
                       "remaining_hours": max(0, total-used), "as_of": as_of.isoformat(),
                       "refresh_at": str(data.get("refresh_at") or ""), "basis": "[sourced] manual weekly entry"}
    state.setdefault("sources", {})["kaggle"] = {"status": "manual", "as_of": as_of.isoformat()}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--offline", action="store_true")
    mode.add_argument("--live", action="store_true")
    parser.add_argument("--output", type=Path, default=ROOT / "site/data.json")
    parser.add_argument("--state", type=Path, default=ROOT / "data/state.json")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    config = read(ROOT / "config/settings.json")
    constants = read(ROOT / "config/constants.json")
    now = datetime.now(timezone.utc)
    if args.offline:
        state = read(ROOT / "fixtures/handoff_summary.json")
        now = timestamp("2026-10-29T00:00:00Z")
        mode_name = "offline handoff summary — not an API capture"
    else:
        state = read(args.state) if args.state.exists() else {"resources": {}, "sources": {}}
        mode_name = "live collection"
        configured = False
        if os.environ.get("RUNPOD_API_KEY"):
            configured = True
            backfill = read(ROOT / "data/backfill.json")
            update_provider(state, "runpod", lambda s: RunPod(os.environ["RUNPOD_API_KEY"]).collect(s, config, now, backfill), now)
        else:
            state.setdefault("sources", {})["runpod"] = {"status": "not configured"}
        if config["aws_regions"] and (os.environ.get("AWS_ACCESS_KEY_ID") or os.environ.get("AWS_WEB_IDENTITY_TOKEN_FILE")):
            import boto3
            configured = True
            update_provider(state, "aws", lambda s: AWS(boto3.Session()).collect(s, config, now), now)
        else:
            state.setdefault("sources", {})["aws"] = {"status": "not configured"}
        if os.environ.get("KAGGLE_API_TOKEN") or (os.environ.get("KAGGLE_USERNAME") and os.environ.get("KAGGLE_KEY")):
            configured = True
            def kaggle_collect(s: Json) -> None:
                from kaggle.api.kaggle_api_extended import KaggleApi
                client = KaggleApi()
                client.authenticate()
                Kaggle(client).collect(s, now)
            update_provider(state, "kaggle", kaggle_collect, now)
        else:
            state.setdefault("sources", {})["kaggle"] = {"status": "not configured"}
            manual_quota(state, ROOT / "config/kaggle.yaml")
        if not configured and not state.get("kaggle"):
            LOG.error("No providers configured; existing output preserved")
            return 1
        atomic_json(args.state, state)
    atomic_json(args.output, summarize(state, config, constants, now, mode_name))
    LOG.info("sanitized dashboard written; mode=%s", mode_name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
