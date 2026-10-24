"""Журналы без ИНН и содержимого ответов: маска в журнале доступа uvicorn и общий логгер сервиса."""
import logging
import re

log = logging.getLogger("fns")

_INN_Q = re.compile(r"(inn|query)=\d+")
_DIGITS = re.compile(r"\b\d{10,13}\b")


def mask(text: str) -> str:
    return _DIGITS.sub("***", _INN_Q.sub(r"\1=***", text))


class MaskInn(logging.Filter):
    def filter(self, record):
        try:
            if record.args:
                # аргументы бывают не строками (httpx.URL): строку подставляем, только если в ней нашёлся ИНН
                record.args = tuple(m if (m := mask(str(a))) != str(a) else a for a in record.args)
            if isinstance(record.msg, str):
                record.msg = mask(record.msg)
        except Exception:  # noqa: BLE001
            pass
        return True


def install():
    # httpx пишет полный URL запроса (с ИНН) на уровне INFO: маскируем и его
    for name in ("uvicorn.access", "fns", "httpx", "httpcore"):
        lg = logging.getLogger(name)
        if not any(isinstance(f, MaskInn) for f in lg.filters):
            lg.addFilter(MaskInn())
