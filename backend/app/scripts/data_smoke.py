"""Check every data source that is ready with one small real request each.

    docker compose exec api python -m app.scripts.data_smoke            # all sources that are ready
    docker compose exec api python -m app.scripts.data_smoke fred sec   # only these

Sends at most one or two requests per source (well inside every free limit), stores the few rows received
in the lake like any other ingestion, and prints what came back. Sources that are switched off or still
waiting for a key are listed as such and skipped. Exit code 1 if a ready source failed.
"""
from __future__ import annotations

import sys

from ..core.config import get_settings
from ..database.base import Database
from ..datahub.http import SourceDisabled
from ..datahub.providers import PROBES
from ..datahub.service import DataHub


def main(names: list[str]) -> int:
    settings = get_settings()
    hub = DataHub(Database(settings.database_url), settings)
    failed = 0
    for name, probe in PROBES.items():
        if names and name not in names:
            continue
        try:
            out = hub.ingest(name, probe["dataset"], probe["keys"], probe.get("timeframe", ""), actor="smoke-test", **probe.get("options", {}))
        except SourceDisabled as exc:
            print(f"{name:10s} SKIPPED  {exc}")
            continue
        result = out["results"][0]
        ok = result["status"] in ("ok", "empty")
        failed += not ok
        detail = result["message"] or f"{result['rows_in']} rows received, {result['rows_new']} new"
        print(f"{name:10s} {'OK      ' if ok else 'FAILED  '} {probe['dataset']} {probe['keys'][0]}: {detail} ({out['requests']} requests, {out['seconds']} s)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
