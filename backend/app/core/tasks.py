"""Background job queue (research workloads and data collection: backtests, training, reports, ingestion).
Order handling never runs here - it stays in the single trading process.

Two queues keep the workloads apart: `celery` (research, served by the `worker` service) and `ingest`
(data collection, served by the `data-worker` service, which also runs the schedule)."""
from __future__ import annotations

from celery import Celery

from .config import get_settings

celery_app = Celery("qat", broker=get_settings().redis_url, backend=get_settings().redis_url)
celery_app.conf.update(task_serializer="json", accept_content=["json"], result_serializer="json", timezone="UTC",
                       task_acks_late=True, worker_prefetch_multiplier=1,
                       imports=("app.datahub.tasks",), task_routes={"data.*": {"queue": "ingest"}},
                       broker_connection_retry_on_startup=True)


@celery_app.task(name="system.ping")
def ping() -> str:
    return "pong"
