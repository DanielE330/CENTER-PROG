# Адаптировано из проекта «ЦИ1» (ветка gleb, swimshadow): логика сохранена, импорты и доработки под общий бэктест.
"""When will a customer pay an invoice: Kaplan–Meier survival curves.

A curve per counterparty, shrunk towards the global curve when its history is
short (weight n / (n + k)). Invoices still unpaid at the cut-off are censored
observations; without them long delays would disappear and the model would be
optimistic."""

from dataclasses import dataclass, field

import numpy as np

MAX_DAYS = 365
SHRINK_K = 5.0


def km_survival(durations: np.ndarray, events: np.ndarray) -> np.ndarray:
    """S[t] = P(not paid after t days), t = 0..MAX_DAYS."""
    surv = np.ones(MAX_DAYS + 1)
    if len(durations) == 0:
        return surv
    d = np.clip(durations.astype(int), 0, MAX_DAYS)
    deaths = np.bincount(d[events == 1], minlength=MAX_DAYS + 1).astype(float)
    exits = np.bincount(d, minlength=MAX_DAYS + 1).astype(float)
    at_risk = len(d) - np.concatenate([[0.0], np.cumsum(exits)[:-1]])
    with np.errstate(divide="ignore", invalid="ignore"):
        hazard = np.where(at_risk > 0, deaths / at_risk, 0.0)
    return np.cumprod(1.0 - hazard)


@dataclass
class DelayModel:
    global_curve: np.ndarray
    curves: dict[str, tuple[np.ndarray, int]] = field(default_factory=dict)

    @staticmethod
    def fit(records: list[tuple[str | None, int, bool]]) -> "DelayModel":
        """records: (counterparty_ref, days from document to payment or to cut-off, paid?)."""
        if not records:
            # No history at all: a generic curve, median about three weeks.
            t = np.arange(MAX_DAYS + 1)
            return DelayModel(global_curve=np.exp(-t / 30.0))
        dur = np.array([r[1] for r in records])
        ev = np.array([1 if r[2] else 0 for r in records])
        model = DelayModel(global_curve=km_survival(dur, ev))
        by_cp: dict[str, list[int]] = {}
        for i, (cp, _, _) in enumerate(records):
            if cp:
                by_cp.setdefault(cp, []).append(i)
        for cp, idx in by_cp.items():
            ix = np.array(idx)
            model.curves[cp] = (km_survival(dur[ix], ev[ix]), len(ix))
        return model

    def curve(self, cp: str | None) -> np.ndarray:
        if cp in self.curves:
            own, n = self.curves[cp]
            w = n / (n + SHRINK_K)
            return w * own + (1 - w) * self.global_curve
        return self.global_curve

    def median_delay(self, cp: str | None) -> int:
        s = self.curve(cp)
        below = np.nonzero(s <= 0.5)[0]
        return int(below[0]) if len(below) else MAX_DAYS

    def sample(
        self, cp: str | None, elapsed: int, n: int, rng: np.random.Generator, p_never: float = 0.0
    ) -> np.ndarray:
        """Total delay in days given it is unpaid after `elapsed` days; -1 means "does not pay"."""
        s = self.curve(cp)
        elapsed = int(np.clip(elapsed, 0, MAX_DAYS))
        base = s[elapsed]
        out = np.full(n, -1, dtype=int)
        if base <= 1e-9:
            # The curve says it should have been paid already: pay soon, at a random near day.
            out[:] = elapsed + rng.integers(1, 8, size=n)
        else:
            cond = s[elapsed:] / base
            u = rng.random(n)
            # First t where conditional survival drops below u.
            idx = np.searchsorted(-cond, -u, side="left")
            ok = idx < len(cond)
            out[ok] = elapsed + idx[ok]
        if p_never > 0:
            out[rng.random(n) < p_never] = -1
        return out
