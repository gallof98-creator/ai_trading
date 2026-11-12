"""Background job queue (research workloads only: backtests, training, optimisation, reports).
Order handling never runs here - it stays in the single trading process."""
from __future__ import annotations

from celery import Celery

from .config import get_settings

celery_app = Celery("qat", broker=get_settings().redis_url, backend=get_settings().redis_url)
celery_app.conf.update(task_serializer="json", accept_content=["json"], result_serializer="json", timezone="UTC",
                       task_acks_late=True, worker_prefetch_multiplier=1)


@celery_app.task(name="system.ping")
def ping() -> str:
    return "pong"
