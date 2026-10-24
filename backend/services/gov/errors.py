class GovError(Exception):
    """Базовая ошибка модуля."""
    code = "gov_error"

    def __init__(self, message: str, source: str = ""):
        super().__init__(message)
        self.source = source


class FetchError(GovError):
    """Источник недоступен, запрещён robots.txt или вернул не 200."""
    code = "gov_unavailable"


class ParseError(GovError):
    """Разметка источника изменилась и не соответствует ожидаемой."""
    code = "gov_parse_error"
