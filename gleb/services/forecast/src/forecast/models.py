"""Models for the irregular part of the cash flow (what is left after recurring
payments and known future events). Each implements `Forecaster`; the registry
lets the backtest compare them and the service pick one by name."""

from datetime import date, timedelta
from typing import Protocol

import numpy as np
import pandas as pd
from forecast.calendar import is_working_day


class Forecaster(Protocol):
    name: str

    def fit(self, daily: pd.Series) -> None:
        """daily: net irregular flow per calendar day (kopecks), index = dates up to as_of."""

    def sample(self, days: list[date], n: int, rng: np.random.Generator) -> np.ndarray:
        """Returns n × len(days) simulated daily flows."""


def _working(daily: pd.Series) -> pd.Series:
    return daily[[is_working_day(d) for d in daily.index]]


class NaiveLastWeek:
    """Repeats the last observed week. The floor every other model must beat."""

    name = "naive_last_week"

    def fit(self, daily: pd.Series) -> None:
        w = _working(daily)
        self.by_wd = {d.weekday(): float(v) for d, v in w.tail(5).items()}

    def sample(self, days, n, rng):
        row = np.array([self.by_wd.get(d.weekday(), 0.0) if is_working_day(d) else 0.0 for d in days])
        return np.tile(row, (n, 1))


class WeekdayBootstrap:
    """Bootstraps past days of the same weekday (last `weeks` weeks) and scales them
    by yearly seasonality when more than a year of history exists."""

    name = "weekday_bootstrap"

    def __init__(self, weeks: int = 12):
        self.weeks = weeks

    def fit(self, daily: pd.Series) -> None:
        w = _working(daily)
        recent = w[w.index > (w.index.max() - timedelta(weeks=self.weeks))] if len(w) else w
        self.pool = {wd: recent[[d.weekday() == wd for d in recent.index]].to_numpy(dtype=float) for wd in range(5)}
        self.all = recent.to_numpy(dtype=float) if len(recent) else np.zeros(1)
        self.season = self._seasonality(daily)
        self.recent_months = {d.month for d in recent.index}

    @staticmethod
    def _seasonality(daily: pd.Series) -> dict[int, float]:
        if len(daily) == 0 or (daily.index.max() - daily.index.min()).days < 395:
            return {}
        s = pd.Series(daily.to_numpy(dtype=float), index=pd.to_datetime(daily.index))
        monthly = s.resample("MS").sum()
        by_month = monthly.groupby(monthly.index.month).mean()
        level = by_month.abs().mean() or 1.0
        # Ratio of the month's mean flow to the average absolute level; flows can be negative.
        return {int(m): float(v / level) for m, v in by_month.items()}

    def _scale(self, month: int) -> float:
        if not self.season:
            return 1.0
        recent = np.mean([self.season.get(m, 1.0) for m in self.recent_months]) if self.recent_months else 1.0
        target = self.season.get(month, recent)
        if abs(recent) < 0.2:
            return 1.0
        return float(np.clip(target / recent, 0.3, 3.0))

    def sample(self, days, n, rng):
        out = np.zeros((n, len(days)))
        for j, d in enumerate(days):
            if not is_working_day(d):
                continue
            pool = self.pool.get(d.weekday())
            if pool is None or len(pool) == 0:
                pool = self.all
            out[:, j] = rng.choice(pool, size=n) * self._scale(d.month)
        return out


class WeekdayMean(WeekdayBootstrap):
    """Same as the bootstrap without noise: a point forecast for comparison."""

    name = "weekday_mean"

    def sample(self, days, n, rng):
        row = np.array(
            [
                (
                    np.mean(self.pool.get(d.weekday(), self.all))
                    if len(self.pool.get(d.weekday(), []))
                    else np.mean(self.all)
                )
                * self._scale(d.month)
                if is_working_day(d)
                else 0.0
                for d in days
            ]
        )
        return np.tile(row, (n, 1))


REGISTRY: dict[str, type] = {m.name: m for m in (NaiveLastWeek, WeekdayBootstrap, WeekdayMean)}


def make(name: str) -> Forecaster:
    return REGISTRY[name]()
