from enum import StrEnum


class Role(StrEnum):
    ADMIN = "admin"  # everything, incl. credentials, users, risk limits
    TRADER = "trader"  # trading operations, strategies, backtests
    VIEWER = "viewer"  # read-only

    @property
    def rank(self) -> int:
        return {"viewer": 0, "trader": 1, "admin": 2}[self.value]
