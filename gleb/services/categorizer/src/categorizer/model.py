"""ML fallback for operations no rule recognized. Trained per tenant on rule
labels plus manual corrections, so one tenant's texts never shape another's model.
Swap in a stronger model by implementing the same two methods."""

from typing import Protocol

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline

MIN_SAMPLES = 200
MIN_CONFIDENCE = 0.55


class Categorizer(Protocol):
    def fit(self, texts: list[str], labels: list[str]) -> None: ...
    def predict(self, texts: list[str]) -> list[tuple[str, float]]: ...


def features(purpose: str, name: str, amount_kop: int) -> str:
    sign = "IN" if amount_kop >= 0 else "OUT"
    size = int(np.log10(abs(amount_kop) + 1))
    return f"{sign} S{size} {name} | {purpose}".lower()


class TfidfLogReg:
    def __init__(self) -> None:
        self._pipe = None

    def fit(self, texts: list[str], labels: list[str]) -> None:
        if len(texts) < MIN_SAMPLES or len(set(labels)) < 2:
            self._pipe = None
            return
        pipe = make_pipeline(
            TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 5), min_df=2, max_features=30000, sublinear_tf=True),
            LogisticRegression(max_iter=300, C=4.0),
        )
        pipe.fit(texts, labels)
        self._pipe = pipe

    def predict(self, texts: list[str]) -> list[tuple[str, float]]:
        if self._pipe is None or not texts:
            return [("", 0.0)] * len(texts)
        proba = self._pipe.predict_proba(texts)
        classes = self._pipe.classes_
        best = proba.argmax(axis=1)
        return [(str(classes[i]), float(proba[row, i])) for row, i in enumerate(best)]
