"""Run walk-forward validations side by side on several processor cores.

A validation is pure computation on data that is already in memory, so it can be handed to another process
unchanged; reading the data and recording the verdict stay in the main process. Worker processes are started
fresh ("spawn"), never forked from the running server, and are kept between cycles. If the pool cannot be
used for any reason, the same work is done one after the other: slower, with identical results.
"""
from __future__ import annotations

import multiprocessing
import os
import time
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor, as_completed
from concurrent.futures.process import BrokenProcessPool

from ..backtesting.walk_forward import WalkForwardSpec, walk_forward
from ..core.logging import get_logger

log = get_logger(__name__)
MAX_AUTO_WORKERS = 8


def worker_count(configured: int = 0) -> int:
    """`configured` processes, or half the cores (at most eight) when it is 0: the rest stay free for the pages, the
    live feeds and the trading engine, which share the machine with the validations."""
    if configured > 0:
        return configured
    return max(1, min(MAX_AUTO_WORKERS, (os.cpu_count() or 2) // 2))


class ValidationPool:
    def __init__(self) -> None:
        self._executor: ProcessPoolExecutor | None = None
        self._workers = 0
        self.last: dict = {}

    def _pool(self, workers: int) -> ProcessPoolExecutor:
        if self._executor is None or self._workers != workers:
            self.close()
            self._executor = ProcessPoolExecutor(max_workers=workers, mp_context=multiprocessing.get_context("spawn"))
            self._workers = workers
        return self._executor

    def close(self) -> None:
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._executor = None

    def run(self, specs: list[WalkForwardSpec], workers: int = 0, timeout: float | None = None,
            progress: Callable[[int, int], None] | None = None) -> list[dict | Exception | None]:
        """One entry per spec, in the same order: the walk-forward result, the exception it raised, or None when it
        did not finish within `timeout` seconds (it is simply done again later)."""
        workers = min(worker_count(workers), max(1, len(specs)))
        started = time.perf_counter()
        results: list[dict | Exception | None] = [None] * len(specs)
        mode = "parallel"
        if workers <= 1 or len(specs) < 2:
            mode = "sequential"
            self._sequential(specs, results, range(len(specs)), started, timeout, progress)
        else:
            try:
                futures = {self._pool(workers).submit(walk_forward, spec): i for i, spec in enumerate(specs)}
                try:
                    for future in as_completed(futures, timeout=timeout):
                        try:
                            results[futures[future]] = future.result()
                        except BrokenProcessPool:
                            raise
                        except Exception as exc:  # this validation failed (for example too little history); the others carry on
                            results[futures[future]] = exc
                        if progress:
                            progress(sum(r is not None for r in results), len(specs))
                except TimeoutError:
                    for future in futures:
                        future.cancel()
            except (BrokenProcessPool, OSError, RuntimeError) as exc:
                # A worker died or processes cannot be started here: finish what is left in this process.
                log.warning("validation_pool_unavailable", error=type(exc).__name__)
                self.close()
                mode = "sequential (process pool unavailable)"
                self._sequential(specs, results, [i for i, r in enumerate(results) if r is None], started, timeout, progress)
        self.last = {"mode": mode, "workers": workers if mode == "parallel" else 1, "validations": len(specs),
                     "finished": sum(r is not None for r in results), "seconds": round(time.perf_counter() - started, 2)}
        return results

    @staticmethod
    def _sequential(specs, results, indexes, started: float, timeout: float | None, progress=None) -> None:
        for i in indexes:
            if timeout is not None and time.perf_counter() - started > timeout:
                return
            try:
                results[i] = walk_forward(specs[i])
            except Exception as exc:
                results[i] = exc
            if progress:
                progress(sum(r is not None for r in results), len(specs))
