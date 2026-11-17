"""JSON-lines logging. request_id / job_id ride along via contextvars, so every log line
emitted while handling a request or a job carries them without passing them around."""

import json
import logging
import time
from contextvars import ContextVar

request_id: ContextVar[str | None] = ContextVar("request_id", default=None)
job_id: ContextVar[int | None] = ContextVar("job_id", default=None)

_STANDARD = set(vars(logging.makeLogRecord({}))) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        line = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)),
            "level": record.levelname.lower(),
            "msg": record.getMessage(),
            "request_id": request_id.get(),
            "job_id": job_id.get(),
        }
        line |= {k: v for k, v in vars(record).items() if k not in _STANDARD}  # extra=...
        if record.exc_info:
            line["error"] = self.formatException(record.exc_info)
        return json.dumps(line, default=str)


def setup_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=level, handlers=[handler], force=True)
