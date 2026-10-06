import hashlib
import hmac
import json
import logging
from contextvars import ContextVar
from datetime import UTC, datetime

request_id: ContextVar[str] = ContextVar("request_id", default="-")
_FIELDS = frozenset(
    {
        "update_id",
        "user_hash",
        "workflow",
        "role",
        "model",
        "latency_ms",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "remaining_tokens",
        "status",
        "error_type",
        "cost",
        "prompt_version",
    }
)


class SafeJSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        # Never format arbitrary exception bodies or third-party HTTP URLs (bot token).
        data = {
            "time": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "event": record.msg if isinstance(record.msg, str) else "event",
            "request_id": request_id.get(),
        }
        data.update({k: getattr(record, k) for k in _FIELDS if hasattr(record, k)})
        return json.dumps(data, default=str)


def setup_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(SafeJSONFormatter())
    logger = logging.getLogger("finance")
    logger.handlers = [handler]
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for name in ("httpx", "httpcore", "sqlalchemy", "asyncpg"):
        logging.getLogger(name).setLevel(logging.CRITICAL)


def user_hash(user_id: int, key: str) -> str:
    return hmac.new(key.encode(), str(user_id).encode(), hashlib.sha256).hexdigest()[:16]
