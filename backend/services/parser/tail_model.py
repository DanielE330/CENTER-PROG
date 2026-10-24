"""Лёгкая дообучаемая модель для «хвоста» — операций, которых не поймали правила.

Идея: проект gleb (swimshadow) (gleb/services/categorizer/src/categorizer/model.py: TF-IDF +
логрегрессия, обучается на метках правил плюс ручных правках). У нас та же схема без sklearn/numpy:
мультиномиальный Naive Bayes на чистом Python, отдельно для поступлений и списаний.

Признаки: знак и порядок суммы, слова и целиком нормализованное имя контрагента («память
контрагента»: если правила уже узнали этого контрагента в этой выписке, пустое назначение
не помешает), основы слов назначения и их биграммы. Обучение — на самой же выписке: операции,
которым правила поставили категорию, плюс ручные правки (corrections, вес выше).
Модель не хранится и ничего не пишет на диск; для небольших выписок не включается.

Интерфейс как у gleb: fit(samples) / predict(samples) -> [(категория, уверенность)].
"""
import math
import re
from collections import Counter, defaultdict

MIN_SAMPLES = 100      # меньше размеченных операций в направлении — модель не строим
MIN_CONFIDENCE = 0.6   # ниже — оставляем запасную категорию
MAX_REPORTED_CONFIDENCE = 0.7  # ниже, чем у точных правил, чтобы UI мог подсветить «угадано»
ALPHA = 0.3            # сглаживание Лапласа
CORRECTION_WEIGHT = 5  # ручная правка весит как столько обычных меток

_WORD = re.compile(r"[a-zа-яё]{3,}", re.I)
_VAT_TAIL = re.compile(r"(?:в том числе|в т\.\s?ч\.|сумма)\s*\S*\s*ндс.*$|ндс.*$", re.I)
_LEGAL = {"ооо", "оао", "пао", "зао", "акционерное", "общество"}
STEM = 6


def _name_norm(name: str) -> str:
    n = re.sub(r"[\d«»\"'№()\-_.,]+", " ", (name or "").lower())
    return re.sub(r"\s+", " ", n).strip()


def features(purpose: str, name: str, amount_kop: int) -> list[tuple[str, float]]:
    """Список (признак, вес)."""
    f: list[tuple[str, float]] = [("size:%d" % len(str(abs(int(amount_kop)))), 0.5)]
    nn = _name_norm(name)
    if nn:
        f.append(("N:" + nn, 3.0))
        for w in _WORD.findall(nn):
            if w not in _LEGAL:
                f.append(("n:" + w[:STEM], 1.0))
    text = _VAT_TAIL.sub(" ", (purpose or "").lower())
    text = re.sub(r"\d+", " ", text)
    stems = [w[:STEM] for w in _WORD.findall(text)]
    for w in stems:
        f.append(("p:" + w, 1.0))
    for a, b in zip(stems, stems[1:]):
        f.append(("b:%s %s" % (a, b), 1.0))
    return f


class _NB:
    def __init__(self) -> None:
        self.prior: Counter = Counter()
        self.counts: dict[str, Counter] = defaultdict(Counter)
        self.totals: Counter = Counter()
        self.vocab: set[str] = set()

    def add(self, feats: list[tuple[str, float]], label: str, weight: float) -> None:
        self.prior[label] += weight
        for k, w in feats:
            self.counts[label][k] += w * weight
            self.totals[label] += w * weight
            self.vocab.add(k)

    def predict(self, feats: list[tuple[str, float]]) -> tuple[str, float]:
        n = sum(self.prior.values())
        v = len(self.vocab) or 1
        scores = {}
        for c, pc in self.prior.items():
            s = math.log(pc / n)
            cc, tot = self.counts[c], self.totals[c]
            for k, w in feats:
                if k in self.vocab:  # неизвестные признаки не штрафуют ни один класс
                    s += w * math.log((cc.get(k, 0.0) + ALPHA) / (tot + ALPHA * v))
            scores[c] = s
        m = max(scores.values())
        z = sum(math.exp(x - m) for x in scores.values())
        best = max(scores, key=scores.get)
        return best, 1.0 / z


class TailModel:
    """Fit на размеченных операциях, predict на хвосте. Операция — dict с ключами
    purpose, counterparty, amount_kop, direction (+ category для обучения)."""

    def __init__(self) -> None:
        self._m: dict[str, _NB] = {}

    def fit(self, samples: list[dict], corrections: list[dict] | None = None) -> "TailModel":
        self._m = {}
        by_dir: dict[str, list[tuple[dict, float]]] = defaultdict(list)
        for s in samples:
            by_dir[s["direction"]].append((s, 1.0))
        for s in corrections or []:
            by_dir[s["direction"]].append((s, CORRECTION_WEIGHT))
        for d, items in by_dir.items():
            if len(items) < MIN_SAMPLES or len({s["category"] for s, _ in items}) < 2:
                continue
            nb = _NB()
            for s, w in items:
                nb.add(features(s["purpose"], s["counterparty"], int(s["amount_kop"])), s["category"], w)
            self._m[d] = nb
        return self

    @property
    def ready(self) -> bool:
        return bool(self._m)

    def predict(self, rows: list[dict]) -> list[tuple[str, float]]:
        out = []
        for r in rows:
            nb = self._m.get(r["direction"])
            if nb is None:
                out.append(("", 0.0))
                continue
            cat, p = nb.predict(features(r["purpose"], r["counterparty"], int(r["amount_kop"])))
            out.append((cat, p))
        return out


def apply_tail_model(txs: list[dict], corrections: list[dict] | None = None) -> int:
    """Переразмечает in-place операции с category_method == 'fallback'. Возвращает, сколько изменено."""
    train = [t for t in txs if t.get("category_method") in ("rule", "rule_gleb")]
    tail = [t for t in txs if t.get("category_method") == "fallback"]
    if not tail or not train:
        return 0
    model = TailModel().fit(train, corrections)
    if not model.ready:
        return 0
    changed = 0
    for t, (cat, p) in zip(tail, model.predict(tail)):
        if cat and p >= MIN_CONFIDENCE:
            t["category"] = cat
            t["category_confidence"] = round(min(p, MAX_REPORTED_CONFIDENCE), 2)
            t["category_method"] = "ml"
            changed += 1
    return changed
