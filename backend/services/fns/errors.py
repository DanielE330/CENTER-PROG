class SourceUnavailable(Exception):
    """Источник сейчас недоступен или недопустим для автоматического доступа; reason — короткий код."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail or reason
