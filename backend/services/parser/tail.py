"""ML на хвост: операции, которые не распознали правила. Модель обучается отдельно на каждого тенанта по меткам правил
и ручным исправлениям, поэтому тексты одной компании не влияют на другую. scikit-learn подключается лениво: без него
парсер работает на правилах (как раньше)."""
import math

MIN_SAMPLES = 200
MIN_CONFIDENCE = 0.55


def features(purpose: str, name: str, amount_kop: int) -> str:
    sign = "IN" if amount_kop >= 0 else "OUT"
    size = int(math.log10(abs(amount_kop) + 1))
    return f"{sign} S{size} {name} | {purpose}".lower()


class TfidfLogReg:
    """TF-IDF по символьным n-граммам (3–5) + логистическая регрессия. Быстрая, на CPU, объяснимая."""

    def __init__(self) -> None:
        self._pipe = None

    @property
    def ready(self) -> bool:
        return self._pipe is not None

    def fit(self, texts: list[str], labels: list[str]) -> "TfidfLogReg":
        if len(texts) < MIN_SAMPLES or len(set(labels)) < 2:
            self._pipe = None
            return self
        try:
            from sklearn.feature_extraction.text import TfidfVectorizer
            from sklearn.linear_model import LogisticRegression
            from sklearn.pipeline import make_pipeline
        except ImportError:
            self._pipe = None
            return self
        self._pipe = make_pipeline(
            TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 5), min_df=2, max_features=30000, sublinear_tf=True),
            LogisticRegression(max_iter=300, C=4.0),
        ).fit(texts, labels)
        return self

    def predict(self, texts: list[str]) -> list[tuple[str, float]]:
        """(подкатегория, уверенность); ("", 0) — модель не обучена. Решает вызывающий по MIN_CONFIDENCE."""
        if self._pipe is None or not texts:
            return [("", 0.0)] * len(texts)
        proba = self._pipe.predict_proba(texts)
        classes = self._pipe.classes_
        best = proba.argmax(axis=1)
        return [(str(classes[i]), float(proba[row, i])) for row, i in enumerate(best)]
