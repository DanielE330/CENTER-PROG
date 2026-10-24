# Адаптировано из проекта «ЦИ1» (ветка gleb, swimshadow): логика сохранена, импорты и доработки под общий бэктест.
"""Models for the irregular part of the cash flow (what is left after recurring
payments and known future events). Each implements `Forecaster`; the registry
lets the backtest compare them and the service pick one by name."""

from datetime import date, timedelta
from typing import Protocol

import numpy as np
import pandas as pd
from ml.lab.workdays import is_working_day


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


class BlockBootstrap:
    """Бутстреп целых рабочих недель (внутринедельная структура сохраняется) без линейного тренда:
    аналог остаточного слоя Rust-ядра, где снятие дрейфа дало основной выигрыш в 30-дневной ошибке."""

    name = "block_bootstrap"

    def __init__(self, weeks: int = 26, shape_keep: float = 0.5):
        self.weeks, self.shape_keep = weeks, shape_keep

    def fit(self, daily: pd.Series) -> None:
        w = _working(daily)
        idx = pd.to_datetime(w.index)
        recent = pd.Series(w.to_numpy(dtype=float), index=idx)
        if len(recent):
            recent = recent[recent.index > recent.index.max() - pd.Timedelta(weeks=self.weeks)]
        blocks: list[np.ndarray] = []
        for _, g in recent.groupby([recent.index.isocalendar().year, recent.index.isocalendar().week]):
            row = np.zeros(5)
            for d, v in g.items():
                if d.weekday() < 5:
                    row[d.weekday()] = v
            blocks.append(row)
        self.blocks = np.array(blocks) if blocks else np.zeros((1, 5))
        sums = self.blocks.sum(axis=1)
        # дрейф: наклон недельных сумм снят, форма недели сохранена наполовину
        slope = np.polyfit(np.arange(len(sums)), sums, 1)[0] if len(sums) >= 4 else 0.0
        trend = slope * (np.arange(len(sums)) - (len(sums) - 1)) / 5.0
        self.blocks = self.blocks - trend[:, None]
        mean_row = self.blocks.mean(axis=0, keepdims=True)
        self.blocks = mean_row + self.shape_keep * (self.blocks - mean_row)

    def sample(self, days, n, rng):
        out = np.zeros((n, len(days)))
        picks = {}
        for j, d in enumerate(days):
            if not is_working_day(d) or d.weekday() > 4:
                continue
            key = (d - timedelta(days=d.weekday()))
            if key not in picks:
                picks[key] = rng.integers(0, len(self.blocks), size=n)
            out[:, j] = self.blocks[picks[key], d.weekday()]
        return out


def _bseries(daily: pd.Series, max_len: int = 520) -> pd.Series:
    """Ряд по рабочим дням пн–пт (праздники — нули): так сезонность 5 для ETS/Theta имеет смысл."""
    s = pd.Series(daily.to_numpy(dtype=float), index=pd.to_datetime(daily.index))
    return s.reindex(pd.bdate_range(s.index.min(), s.index.max()), fill_value=0.0).tail(max_len)


def _map_b(days: list[date], mean: np.ndarray, sigma: np.ndarray, start: pd.Timestamp) -> tuple[np.ndarray, np.ndarray]:
    """Значения по B-календарю -> календарные дни горизонта; выходные и праздники дают 0."""
    bd = pd.bdate_range(start, periods=len(mean))
    lookup = {d.date(): (mean[i], sigma[i]) for i, d in enumerate(bd)}
    m = np.array([lookup.get(d, (0.0, 0.0))[0] if is_working_day(d) else 0.0 for d in days])
    sd = np.array([lookup.get(d, (0.0, 0.0))[1] if is_working_day(d) else 0.0 for d in days])
    return m, sd


class _StatsForecast:
    """Общая часть ETS/Theta из statsforecast: точечный прогноз + интервал 80 % -> нормальное приближение."""

    name = ""
    factory: str = ""

    def fit(self, daily: pd.Series) -> None:
        self.s = _bseries(daily) if len(daily) else pd.Series(dtype=float)

    def sample(self, days, n, rng):
        if len(self.s) < 30:
            return np.zeros((n, len(days)))
        from statsforecast import StatsForecast
        from statsforecast import models as sm

        h = len(pd.bdate_range(days[0], days[-1]))
        model = getattr(sm, self.factory)(season_length=5)
        df = pd.DataFrame({"unique_id": "x", "ds": self.s.index, "y": self.s.to_numpy()})
        fc = StatsForecast(models=[model], freq="B", n_jobs=1).forecast(df=df, h=h, level=[80])
        col = type(model).__name__
        mean = fc[col].to_numpy(dtype=float)
        sigma = (fc[f"{col}-hi-80"].to_numpy(dtype=float) - fc[f"{col}-lo-80"].to_numpy(dtype=float)) / (2 * 1.2816)
        m, sd = _map_b(days, mean, np.maximum(sigma, 0.0), pd.Timestamp(fc["ds"].iloc[0]))
        return m[None, :] + sd[None, :] * rng.standard_normal((n, len(days)))


class AutoETS(_StatsForecast):
    name = "ets"
    factory = "AutoETS"


class Theta(_StatsForecast):
    name = "theta"
    factory = "Theta"


LEVELS = (0.1, 0.25, 0.5, 0.75, 0.9)


def quantiles_to_samples(q: np.ndarray, n: int, rng: np.random.Generator, levels=LEVELS) -> np.ndarray:
    """q: (H, K) предсказанные квантили по возрастанию уровней -> n независимых по дням выборок обратным преобразованием
    (линейная интерполяция между квантилями, за краями — продолжение крайнего отрезка, уровни ограничены [0,02; 0,98])."""
    q = np.sort(q, axis=1)
    lv = np.array(levels)
    u = np.clip(rng.random((n, q.shape[0])), 0.02, 0.98)
    out = np.empty_like(u)
    for j in range(q.shape[0]):
        x = np.interp(u[:, j], lv, q[j])
        lo = u[:, j] < lv[0]
        hi = u[:, j] > lv[-1]
        if lo.any():
            slope = (q[j, 1] - q[j, 0]) / (lv[1] - lv[0])
            x[lo] = q[j, 0] + slope * (u[lo, j] - lv[0])
        if hi.any():
            slope = (q[j, -1] - q[j, -2]) / (lv[-1] - lv[-2])
            x[hi] = q[j, -1] + slope * (u[hi, j] - lv[-1])
        out[:, j] = x
    return out


def _calendar_features(d: date) -> list[float]:
    nxt28 = date(d.year, d.month, 28)
    if d > nxt28:
        nxt28 = date(d.year + (d.month == 12), d.month % 12 + 1, 28)
    month_end = (date(d.year + (d.month == 12), d.month % 12 + 1, 1) - timedelta(days=1)).day
    return [d.weekday(), d.day, d.month, float(is_working_day(d)), (nxt28 - d).days, month_end - d.day]


def _level_features(w: pd.Series, scale: float) -> list[float]:
    """Уровень и разброс недавних рабочих дней, нормированные на масштаб компании."""
    v = w.to_numpy(dtype=float)
    if len(v) == 0:
        return [0.0, 0.0, 0.0, 0.0]
    return [v[-5:].mean() / scale, v[-20:].mean() / scale, v[-60:].mean() / scale, v[-20:].std() / scale]


def company_scale(daily: pd.Series) -> float:
    """Масштаб компании: медианный недельный оборот |операций| (как в оценке Rust-ядра); 1, если данных нет."""
    s = pd.Series(np.abs(daily.to_numpy(dtype=float)), index=pd.to_datetime(daily.index))
    weekly = s.resample("W").sum()
    return float(weekly[weekly > 0].median()) if (weekly > 0).any() else 1.0


class GBPool:
    """Глобальная модель: один бустинг с квантильной потерей на все компании (нормировка на масштаб компании).
    Обучается только на данных до `train_end`; для компании берётся модель, не видевшая её фолд (leave-company-out).
    LightGBM, если доступен (Linux/Docker), иначе HistGradientBoosting из scikit-learn — та же квантильная потеря."""

    def __init__(self, series: dict[str, pd.Series], train_end: date, folds: int = 5, origin_step: int = 14, horizon: int = 30, max_iter: int = 100):
        self.series, self.train_end, self.folds, self.step, self.horizon, self.max_iter = series, train_end, folds, origin_step, horizon, max_iter
        self.cids = sorted(series)
        self.fold_of = {c: i % folds for i, c in enumerate(self.cids)}
        self._models: dict[int, list] = {}
        self.backend = "sklearn.HistGradientBoosting"

    def _rows(self, cid: str) -> tuple[np.ndarray, np.ndarray]:
        s = self.series[cid]
        s = s[s.index <= self.train_end]
        if len(s) < 150:
            return np.empty((0, 11)), np.empty(0)
        scale = company_scale(s)
        wk = s[[is_working_day(d) for d in s.index]]
        X, y = [], []
        first, last = s.index.min() + timedelta(days=90), self.train_end - timedelta(days=self.horizon)
        o = first
        while o <= last:
            lvl = _level_features(wk[wk.index <= o], scale)
            for h in range(1, self.horizon + 1):
                t = o + timedelta(days=h)
                if is_working_day(t) and t in s.index:
                    X.append(_calendar_features(t) + lvl + [h])
                    y.append(float(s[t]) / scale)
            o += timedelta(days=self.step)
        return np.array(X, dtype=float).reshape(-1, 11), np.array(y, dtype=float)

    def _fit_fold(self, fold: int) -> list:
        parts = [self._rows(c) for c in self.cids if self.fold_of[c] != fold]
        X = np.vstack([p[0] for p in parts if len(p[1])])
        y = np.concatenate([p[1] for p in parts if len(p[1])])
        models = []
        for q in LEVELS:
            try:
                import lightgbm as lgb

                m = lgb.LGBMRegressor(objective="quantile", alpha=q, n_estimators=self.max_iter, learning_rate=0.08, num_leaves=31, min_child_samples=40, verbose=-1)
                self.backend = "lightgbm"
            except (ImportError, OSError):
                from sklearn.ensemble import HistGradientBoostingRegressor

                m = HistGradientBoostingRegressor(loss="quantile", quantile=q, max_iter=self.max_iter, learning_rate=0.08, min_samples_leaf=40)
            models.append(m.fit(X, y))
        return models

    def models_for(self, cid: str) -> list:
        fold = self.fold_of.get(cid, 0)
        if fold not in self._models:
            self._models[fold] = self._fit_fold(fold)
        return self._models[fold]


POOL: dict = {"pool": None, "cid": None}  # задаётся бэктестом: пул и компания, для которой строится прогноз


class GBQuantile:
    name = "gb_quantile"

    def fit(self, daily: pd.Series) -> None:
        self.cid = POOL["cid"]
        self.scale = company_scale(daily) if len(daily) else 1.0
        wk = daily[[is_working_day(d) for d in daily.index]] if len(daily) else daily
        self.level = _level_features(wk, self.scale)
        self.as_of = daily.index.max() if len(daily) else None

    def sample(self, days, n, rng):
        pool: GBPool | None = POOL["pool"]
        if pool is None or self.as_of is None:
            return np.zeros((n, len(days)))
        models = pool.models_for(self.cid)
        X = np.array([_calendar_features(d) + self.level + [(d - self.as_of).days] for d in days], dtype=float)
        q = np.column_stack([m.predict(X) for m in models]) * self.scale
        out = quantiles_to_samples(q, n, rng)
        mask = np.array([is_working_day(d) for d in days])
        out[:, ~mask] = 0.0
        return out


class Chronos:
    """Chronos-Bolt zero-shot (эксперимент, не для продакшена): нужен `chronos-forecasting` и torch. Без них возвращает нули."""

    name = "chronos"

    def fit(self, daily: pd.Series) -> None:
        self.s = _bseries(daily, 512) if len(daily) else pd.Series(dtype=float)

    def sample(self, days, n, rng):
        if len(self.s) < 30:
            return np.zeros((n, len(days)))
        import os

        import torch
        from chronos import BaseChronosPipeline

        if not hasattr(Chronos, "_pipe"):
            Chronos._pipe = BaseChronosPipeline.from_pretrained(os.environ.get("CHRONOS_MODEL", "amazon/chronos-bolt-tiny"), device_map="cpu")
        h = len(pd.bdate_range(days[0], days[-1]))
        scale = float(np.abs(self.s.to_numpy()).mean()) or 1.0
        ctx = torch.tensor(self.s.to_numpy() / scale, dtype=torch.float32)
        q, _ = Chronos._pipe.predict_quantiles(inputs=ctx, prediction_length=h, quantile_levels=list(LEVELS))
        q = np.sort(q[0].numpy(), axis=1) * scale
        start = self.s.index.max() + pd.offsets.BDay(1)
        bd = pd.bdate_range(start, periods=h)
        look = {d.date(): q[i] for i, d in enumerate(bd)}
        full = np.array([look.get(d, np.zeros(len(LEVELS))) if is_working_day(d) else np.zeros(len(LEVELS)) for d in days])
        return quantiles_to_samples(full, n, rng)


REGISTRY: dict[str, type] = {m.name: m for m in (NaiveLastWeek, WeekdayBootstrap, WeekdayMean, BlockBootstrap, AutoETS, Theta, GBQuantile, Chronos)}


def make(name: str) -> Forecaster:
    return REGISTRY[name]()
