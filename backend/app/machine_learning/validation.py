"""Purged, embargoed walk-forward cross-validation for overlapping forward-looking labels."""
from __future__ import annotations

from collections.abc import Iterator

import numpy as np


class PurgedWalkForwardSplit:
    """Expanding-window splits in time order.

    A training sample is kept only if its label is decided BEFORE the test window starts
    (t1 < first test bar), which removes label overlap. `embargo` additionally drops that many
    bars just before the test window to cover serial correlation in features.
    """

    def __init__(self, n_splits: int = 5, embargo: int = 0, min_train: int = 200) -> None:
        if n_splits < 2:
            raise ValueError("n_splits must be at least 2")
        self.n_splits, self.embargo, self.min_train = n_splits, embargo, min_train

    def split(self, t0: np.ndarray, t1: np.ndarray) -> Iterator[tuple[np.ndarray, np.ndarray]]:
        n = len(t0)
        fold = n // (self.n_splits + 1)
        if fold < 20:
            raise ValueError("Not enough samples for the requested number of splits")
        for k in range(1, self.n_splits + 1):
            start, stop = k * fold, (k + 1) * fold if k < self.n_splits else n
            test = np.arange(start, stop)
            first_test_bar = t0[start]
            train = np.where((t1[:start] < first_test_bar - self.embargo))[0]
            if len(train) >= self.min_train:
                yield train, test
