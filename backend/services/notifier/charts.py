"""Графики для Telegram (PNG). matplotlib без GUI; шрифт DejaVu поддерживает кириллицу."""
import io
from datetime import timedelta

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.dates import DateFormatter  # noqa: E402
from matplotlib.ticker import FuncFormatter, MaxNLocator  # noqa: E402

from . import analytics as an, fmt  # noqa: E402
from .service import Bundle  # noqa: E402

BLUE, GREEN, RED, GRAY, AMBER = "#2563eb", "#16a34a", "#dc2626", "#6b7280", "#d97706"


def _mln(x, _):
    a = abs(x) / 100
    if a >= 1_000_000:
        return f"{x / 100_000_000:.1f} млн"
    if a >= 1_000:
        return f"{x / 100_000:.0f} тыс"
    return f"{x / 100:.0f}"


def _png(fig) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()


def _style(ax):
    ax.grid(alpha=.25)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.yaxis.set_major_formatter(FuncFormatter(_mln))


def risk_chart(b: Bundle, past_days: int = 14, future_days: int = 30) -> bytes:
    fig, ax = plt.subplots(figsize=(8, 4))
    t = b.today
    hist = an.daily_balances(b.txs, t - timedelta(days=past_days), t - timedelta(days=1))
    if hist:
        ax.plot([d for d, _ in hist], [v for _, v in hist], color=GRAY, lw=2, label="Факт")
    fut = [d for d in b.fc.days if t <= d.date <= t + timedelta(days=future_days)]
    if fut:
        x = [d.date for d in fut]
        ax.fill_between(x, [d.p10 for d in fut], [d.p90 for d in fut], color=BLUE, alpha=.15, label="Диапазон p10–p90")
        ax.plot(x, [d.p50 for d in fut], color=BLUE, lw=2, label="Прогноз (медиана)")
    ax.axhline(0, color=RED, lw=1)
    ax.axvline(t, color=GRAY, ls=":", lw=1)
    bd = b.fc.break_date
    if bd and t <= bd <= t + timedelta(days=future_days):
        ax.axvline(bd, color=RED, ls="--", lw=1.5, label=f"Возможный разрыв {fmt.day(bd)}")
    ax.set_title(f"Остаток на счёте: факт и прогноз · {b.company}".strip(" ·"), fontsize=11)
    ax.legend(fontsize=8, loc="best")
    ax.xaxis.set_major_formatter(DateFormatter("%d.%m"))
    ax.tick_params(axis="x", labelsize=8)
    _style(ax)
    return _png(fig)


def week_chart(b: Bundle) -> bytes:
    fig, ax = plt.subplots(figsize=(8, 4))
    w, (ai, ae) = b.week, b.week_avg
    labels = ["Поступления", "Расходы", "Чистый поток"]
    fact = [w["income"], w["expense"], w["net"]]
    avg = [ai, ae, ai - ae]
    xs = range(3)
    ax.bar([x - .2 for x in xs], avg, width=.4, color="#cbd5e1", label="Средняя неделя (4 нед.)")
    ax.bar([x + .2 for x in xs], fact, width=.4, color=[GREEN, RED, BLUE], label="Прошлая неделя")
    ax.set_xticks(list(xs), labels)
    ax.axhline(0, color=GRAY, lw=.8)
    ax.set_title(f"Неделя {fmt.period(w['start'], w['end'])}: факт против нормы", fontsize=11)
    ax.legend(fontsize=8)
    _style(ax)
    return _png(fig)


def month_chart(b: Bundle) -> bytes:
    m = b.month_last
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(10, 4), gridspec_kw={"width_ratios": [1.2, 1]})
    flows = an.weekly_flows(b.txs, m["start"], m["end"])
    xs = range(len(flows))
    a1.bar([x - .2 for x in xs], [f["income"] for f in flows], width=.4, color=GREEN, label="Поступления")
    a1.bar([x + .2 for x in xs], [f["expense"] for f in flows], width=.4, color=RED, label="Расходы")
    a1.set_xticks(list(xs), [f"{f['start'].day}–{f['end'].day}" for f in flows], fontsize=8)
    a1.set_title("По неделям месяца", fontsize=10)
    a1.legend(fontsize=8, loc="upper left", ncol=2)
    a1.margins(y=.15)
    _style(a1)
    cats = list(m["by_cat_out"].items())[:6][::-1]
    a2.barh([fmt.category(c) for c, _ in cats], [v for _, v in cats], color=BLUE)
    a2.set_title("Куда ушли деньги", fontsize=10)
    a2.xaxis.set_major_formatter(FuncFormatter(_mln))
    a2.xaxis.set_major_locator(MaxNLocator(4))
    a2.grid(axis="x", alpha=.25)
    for s in ("top", "right"):
        a2.spines[s].set_visible(False)
    fig.suptitle(f"{fmt.month_title(m['start'])} · {b.company}".strip(" ·"), fontsize=11)
    return _png(fig)
