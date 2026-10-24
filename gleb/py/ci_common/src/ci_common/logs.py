"""JSON logs with a last line of defence: identifiers are masked in every record
even if someone logs a raw value by mistake. Free text (payment purposes,
names) cannot be masked by pattern; the e2e log-canary test catches it."""

import json
import logging
import re
import sys
from datetime import UTC, datetime

_PATTERNS = [
    (re.compile(r"\b\d{20}\b"), "[account]"),
    (re.compile(r"\b\d{12}\b|\b\d{10}\b"), "[inn]"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[email]"),
    (re.compile(r"(?:\+7|\b8)[\s(-]*\d{3}[\s)-]*\d{3}[\s-]*\d{2}[\s-]*\d{2}\b"), "[phone]"),
    (re.compile(r"(?i)bearer\s+[\w.-]+"), "Bearer [token]"),
]


def redact(text: str) -> str:
    for pattern, label in _PATTERNS:
        text = pattern.sub(label, text)
    return text


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": redact(record.getMessage()),
        }
        for key, value in getattr(record, "fields", {}).items():
            payload[key] = redact(str(value)) if isinstance(value, str) else value
        if record.exc_info:
            payload["exception"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False)


def init(service: str) -> logging.Logger:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)
    # Access logs would print query strings; request logging is ours and path-only.
    logging.getLogger("uvicorn.access").disabled = True
    log = logging.getLogger(service)
    log.info("starting", extra={"fields": {"service": service}})
    return log
