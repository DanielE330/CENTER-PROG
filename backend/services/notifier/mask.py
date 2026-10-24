"""Маскирование перед отправкой в облачную модель. Идея — из llm-gateway проекта «ЦИ1» (ветка gleb).

Названия контрагентов заменяются на «Контрагент A/B/…», а ИНН, номера счетов, телефоны и email вырезаются.
Таблица замен живёт только в памяти одного запроса; в ответе модели псевдонимы возвращаются на место,
поэтому пользователь читает настоящие названия, а модель их не видела. Отключить маскирование нельзя.
"""
import re
import string

FORMS = r"(?:ООО|ОАО|ПАО|ЗАО|АО|НКО|ИП)"
# название в кавычках до закрывающей, иначе одно–два слова после организационной формы
ORG = re.compile(rf"\b{FORMS}\s*(?:[«\"“][^«»\"“”\n]{{1,80}}[»\"”]|[\w-]+(?:\s[\w-]+)?)", re.IGNORECASE)
ACCOUNT = re.compile(r"(?<!\d)\d{20}(?!\d)")
PHONE = re.compile(r"(?<!\d)\+?\d[\d\s()\-]{9,16}\d(?!\d)")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
INN = re.compile(r"(?<!\d)(?:\d{12}|\d{10})(?!\d)")
ALIAS = re.compile(r"Контрагент ([A-Z](?:[A-Z])?\d*)")


class Masker:
    def __init__(self) -> None:
        self._alias: dict[str, str] = {}   # исходное название -> псевдоним
        self._back: dict[str, str] = {}    # псевдоним -> исходное название

    def _name(self, original: str) -> str:
        key = " ".join(original.split()).lower()
        if key not in self._alias:
            n = len(self._alias)
            letters = string.ascii_uppercase
            tag = letters[n % 26] + (str(n // 26) if n >= 26 else "")
            self._alias[key] = f"Контрагент {tag}"
            self._back[self._alias[key]] = original
        return self._alias[key]

    def mask(self, text: str) -> str:
        text = ACCOUNT.sub("[счёт]", text)
        text = EMAIL.sub("[email]", text)
        text = PHONE.sub("[телефон]", text)
        text = INN.sub("[ИНН]", text)
        return ORG.sub(lambda m: self._name(m.group(0)), text)

    def mask_obj(self, obj):
        """Маскирует все строки во вложенных dict/list (ключи остаются как есть)."""
        if isinstance(obj, str):
            return self.mask(obj)
        if isinstance(obj, dict):
            return {k: self.mask_obj(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self.mask_obj(v) for v in obj]
        return obj

    def unmask(self, text: str) -> str:
        return ALIAS.sub(lambda m: self._back.get(m.group(0), m.group(0)), text)

    def unmask_obj(self, obj):
        if isinstance(obj, str):
            return self.unmask(obj)
        if isinstance(obj, dict):
            return {k: self.unmask_obj(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self.unmask_obj(v) for v in obj]
        return obj
