"""Trading sessions and market calendars.

Regular sessions only. Exchange holidays are supplied per calendar via `holidays`
(none are bundled - load them from your data vendor).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class SessionCalendar:
    name: str
    tz: str = "UTC"
    open_time: time | None = None  # None => continuous
    close_time: time | None = None
    weekdays: frozenset[int] = frozenset(range(7))
    holidays: frozenset[date] = field(default_factory=frozenset)

    def is_trading_day(self, day: date) -> bool:
        """Whether any regular session takes place on this local calendar date."""
        if day in self.holidays:
            return False
        if self.name in ("FX", "CME"):
            return day.weekday() < 5
        return day.weekday() in self.weekdays

    def is_open(self, ts: datetime) -> bool:
        if ts.tzinfo is None:
            raise ValueError("Timestamp must be timezone-aware")
        local = ts.astimezone(ZoneInfo(self.tz))
        if local.date() in self.holidays:
            return False
        if self.name == "FX":  # Sunday 17:00 to Friday 17:00 New York
            wd, t = local.weekday(), local.time()
            return not (wd == 5 or (wd == 4 and t >= time(17)) or (wd == 6 and t < time(17)))
        if self.name == "CME":  # Sunday 17:00 to Friday 16:00 Chicago, daily 16:00-17:00 break
            wd, t = local.weekday(), local.time()
            if wd == 5 or (wd == 6 and t < time(17)) or (wd == 4 and t >= time(16)):
                return False
            return not (time(16) <= t < time(17))
        if local.weekday() not in self.weekdays:
            return False
        if self.open_time is None or self.close_time is None:
            return True
        return self.open_time <= local.time() < self.close_time


_WEEK = frozenset(range(5))
CALENDARS: dict[str, SessionCalendar] = {
    "24x7": SessionCalendar("24x7"),
    "FX": SessionCalendar("FX", tz="America/New_York"),
    "CME": SessionCalendar("CME", tz="America/Chicago"),
    "XNYS": SessionCalendar("XNYS", tz="America/New_York", open_time=time(9, 30), close_time=time(16), weekdays=_WEEK),
    "XETR": SessionCalendar("XETR", tz="Europe/Berlin", open_time=time(9), close_time=time(17, 30), weekdays=_WEEK),
    "XLON": SessionCalendar("XLON", tz="Europe/London", open_time=time(8), close_time=time(16, 30), weekdays=_WEEK),
    "XMIL": SessionCalendar("XMIL", tz="Europe/Rome", open_time=time(9), close_time=time(17, 30), weekdays=_WEEK),
    "XPAR": SessionCalendar("XPAR", tz="Europe/Paris", open_time=time(9), close_time=time(17, 30), weekdays=_WEEK),
    "XAMS": SessionCalendar("XAMS", tz="Europe/Amsterdam", open_time=time(9), close_time=time(17, 30), weekdays=_WEEK),
    "XMAD": SessionCalendar("XMAD", tz="Europe/Madrid", open_time=time(9), close_time=time(17, 30), weekdays=_WEEK),
    "XSWX": SessionCalendar("XSWX", tz="Europe/Zurich", open_time=time(9), close_time=time(17, 30), weekdays=_WEEK),
}


def get_calendar(name: str) -> SessionCalendar:
    try:
        return CALENDARS[name]
    except KeyError:
        raise ValueError(f"Unknown calendar {name!r}") from None
