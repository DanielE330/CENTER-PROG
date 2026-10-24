"""Маскирование перед отправкой облачному провайдеру и обратная подстановка в ответе.

Заменяются: названия контрагентов (организационно-правовая форма + название, ИП ФИО, явный список `protect`),
ФИО, ИНН, номера счетов и карт, email и телефоны. Вместо значения подставляется токен вида `[КОНТРАГЕНТ_1]`.
Одинаковые значения получают один токен; ответ модели возвращается с исходными значениями."""
import re

_ORG_FORM = r"(?:ООО|АО|ПАО|ЗАО|ОАО|НКО|АНО|МУП|ГУП|ИП)"
_QUOTED = r"[«\"“„][^»\"”“]{1,80}[»\"”“]"
_NAME = r"[А-ЯЁ][а-яё]+"

# порядок важен: сначала более специфичные шаблоны
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("КОНТРАГЕНТ", re.compile(rf"{_ORG_FORM}\s*{_QUOTED}")),
    ("КОНТРАГЕНТ", re.compile(rf"ИП\s+{_NAME}(?:\s+{_NAME}\s+{_NAME}|\s+[А-ЯЁ]\.\s?[А-ЯЁ]\.)")),
    ("ФИО", re.compile(rf"{_NAME}\s+{_NAME}\s+{_NAME}(?:ович|евич|ьич|ич|овна|евна|ична|инична)\b")),
    ("ФИО", re.compile(rf"{_NAME}\s+[А-ЯЁ]\.\s?[А-ЯЁ]\.")),
    ("EMAIL", re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")),
    ("ТЕЛЕФОН", re.compile(r"(?<!\d)(?:\+7|8)[\s(-]*\d{3}[\s)-]*\d{3}[\s-]*\d{2}[\s-]*\d{2}(?!\d)")),
    ("СЧЁТ", re.compile(r"(?<![\d.,])\d{20}(?![\d])")),
    ("КАРТА", re.compile(r"(?<![\d.,])\d{4}[ -]\d{4}[ -]\d{4}[ -]\d{4}(?![\d])|(?<![\d.,])\d{16}(?![\d])")),
    ("ИНН", re.compile(r"(?<![\d.,])\d{12}(?![\d]|[.,]\d)|(?<![\d.,])\d{10}(?![\d]|[.,]\d)")),
]
_TOKEN = re.compile(r"\[[А-ЯЁA-Z]+_\d+\]")


class Masker:
    """Один экземпляр на запрос: токены стабильны внутри запроса, ответ раскрывается тем же экземпляром."""

    def __init__(self, protect: list[str] | None = None) -> None:
        self._by_value: dict[str, str] = {}
        self._by_token: dict[str, str] = {}
        self._counters: dict[str, int] = {}
        self.replaced = 0
        terms = sorted({p.strip() for p in (protect or []) if p and len(p.strip()) >= 3}, key=len, reverse=True)
        self._protect = [re.compile(re.escape(t), re.IGNORECASE) for t in terms]

    def _token(self, kind: str, value: str) -> str:
        key = f"{kind}:{value.lower()}"
        tok = self._by_value.get(key)
        if tok is None:
            n = self._counters.get(kind, 0) + 1
            self._counters[kind] = n
            tok = f"[{kind}_{n}]"
            self._by_value[key] = tok
            self._by_token[tok] = value
        self.replaced += 1
        return tok

    def mask(self, text: str) -> str:
        if not text:
            return text
        for rx in self._protect:
            text = rx.sub(lambda m: self._token("КОНТРАГЕНТ", m.group(0)), text)
        for kind, rx in _PATTERNS:
            text = _sub_outside_tokens(rx, text, lambda m, k=kind: self._token(k, m.group(0)))
        return text

    def unmask(self, text: str) -> str:
        if not text or not self._by_token:
            return text
        return _TOKEN.sub(lambda m: self._by_token.get(m.group(0), m.group(0)), text)

    def mask_json(self, value):
        """Маскирует все строки внутри JSON-структуры (аргументы вызовов инструментов и т.п.)."""
        if isinstance(value, str):
            return self.mask(value)
        if isinstance(value, list):
            return [self.mask_json(v) for v in value]
        if isinstance(value, dict):
            return {k: self.mask_json(v) for k, v in value.items()}
        return value

    def unmask_json(self, value):
        if isinstance(value, str):
            return self.unmask(value)
        if isinstance(value, list):
            return [self.unmask_json(v) for v in value]
        if isinstance(value, dict):
            return {k: self.unmask_json(v) for k, v in value.items()}
        return value


def _sub_outside_tokens(rx: re.Pattern[str], text: str, repl) -> str:
    """Не трогаем уже подставленные токены (в них есть цифры)."""
    out, pos = [], 0
    for tm in _TOKEN.finditer(text):
        out.append(rx.sub(repl, text[pos:tm.start()]))
        out.append(tm.group(0))
        pos = tm.end()
    out.append(rx.sub(repl, text[pos:]))
    return "".join(out)
