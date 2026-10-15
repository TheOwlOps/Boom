"""Structured JSON logging cho Boom agent.

Module này bọc logging stdlib để mọi log record đều có cấu trúc JSON cố định:
    {"timestamp", "level", "name", "message", "request_id", "context"}

- timestamp: ISO8601 UTC (kèm hậu tố Z)
- request_id: gắn qua bind()/contextvar nên an toàn với async task
  (mỗi coroutine giữ contextvar riêng, không rò rỉ giữa các request)
- context: dict tùy ý đi kèm mỗi lần log
- Không bao giờ crash vì object không JSON-serializable (fallback str())

Chỉ dùng stdlib: logging, json, contextvars, datetime, sys.
"""

from __future__ import annotations

import contextvars
import datetime
import json
import logging
import sys

# Biến ngữ cảnh lưu request_id hiện tại.
# Vì sao dùng contextvar thay vì thread-local: trong asyncio, nhiều request
# chạy cùng 1 thread nhưng khác task — thread-local sẽ rò rỉ request_id giữa
# các task, còn contextvar thì mỗi task có bản sao context riêng nên an toàn.
_request_id_var: contextvars.ContextVar = contextvars.ContextVar(
    "boom_request_id", default=None
)

# Context dict mặc định của task hiện tại (mỗi task một bản sao, không chia sẻ).
_context_var: contextvars.ContextVar = contextvars.ContextVar(
    "boom_log_context", default=None
)

# Ghi nhớ setup_logging đã chạy để không gắn handler trùng lặp.
_configured = False


def _safe_json(value):
    """Chuyển mọi giá trị thành JSON-serializable.

    Đệ quy qua dict/list/tuple; object lạ thì fallback str(value).
    Lỗi bất ngờ khi stringify cũng được bắt để logger không bao giờ crash.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(k): _safe_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        # set/frozenset không JSON được trực tiếp: chuyển thành list.
        return [_safe_json(v) for v in value]
    if isinstance(value, (bytes, bytearray)):
        try:
            return bytes(value).decode("utf-8")
        except Exception:  # bytes không decode được thì fallback str
            return str(value)
    try:
        return str(value)
    except Exception:  # noqa: BLE001 - stringify không bao giờ được crash log
        return "<unserializable>"


class JSONFormatter(logging.Formatter):
    """Formatter 1 dòng JSON cho mỗi record.

    Lấy request_id từ contextvar (nếu record chưa gắn riêng), lấy context
    từ contextvar + extra "context" của record rồi gộp lại, safe-json hóa.
    """

    def format(self, record: logging.LogRecord) -> str:
        request_id = getattr(record, "request_id", None) or _request_id_var.get()
        record_ctx = getattr(record, "context", None) or {}
        ctx = dict(_context_var.get() or {})
        if isinstance(record_ctx, dict):
            ctx.update(record_ctx)

        payload = {
            "timestamp": datetime.datetime.now(
                datetime.timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "name": record.name,
            "message": record.getMessage(),
            "request_id": request_id,
            "context": _safe_json(ctx),
        }
        try:
            return json.dumps(payload, ensure_ascii=False)
        except Exception:  # noqa: BLE001 - fallback cuối cùng, không crash
            payload["context"] = "<unserializable>"
            payload["message"] = str(payload["message"])
            return json.dumps(payload, ensure_ascii=False)


class _BoundLogger:
    """Adapter gắn request_id/context mặc định cho logger.

    Mọi phương thức log (debug/info/warning/error/exception) đều chèn
    request_id + context vào extra của record. Không kế thừa logging.Logger
    nên dùng được mọi logger có sẵn; expose thuộc tính qua __getattr__.
    """

    def __init__(self, logger: logging.Logger, request_id=None, context=None):
        self._logger = logger
        self._request_id = request_id
        self._context = dict(context or {})

    def __getattr__(self, item):
        return getattr(self._logger, item)

    def _extra(self, context=None):
        merged = dict(self._context)
        if context:
            merged.update(context)
        return {"request_id": self._request_id, "context": merged}

    def debug(self, msg, *args, context=None, **kwargs):
        self._logger.debug(msg, *args, extra=self._extra(context), **kwargs)

    def info(self, msg, *args, context=None, **kwargs):
        self._logger.info(msg, *args, extra=self._extra(context), **kwargs)

    def warning(self, msg, *args, context=None, **kwargs):
        self._logger.warning(msg, *args, extra=self._extra(context), **kwargs)

    def error(self, msg, *args, context=None, **kwargs):
        self._logger.error(msg, *args, extra=self._extra(context), **kwargs)

    def exception(self, msg, *args, context=None, **kwargs):
        self._logger.exception(msg, *args, extra=self._extra(context), **kwargs)

    def bind(self, request_id=None, **context):
        """Gắn thêm request_id/context, trả về adapter mới (không đổi cũ)."""
        new_ctx = dict(self._context)
        new_ctx.update(context)
        return _BoundLogger(
            self._logger,
            request_id if request_id is not None else self._request_id,
            new_ctx,
        )


def get_logger(name: str) -> logging.Logger:
    """Trả về logger stdlib theo tên module.

    Gọi setup_logging() một lần trước đó để có JSON output; nếu chưa setup,
    logger vẫn hoạt động với handler mặc định (lastResort) của stdlib.
    """
    return logging.getLogger(name)


def bind(request_id=None, _logger=None, **context) -> _BoundLogger:
    """Trả về adapter đã gắn request_id + context.

    Hai cách dùng:
      1. logger = bind(request_id="abc", logger=get_logger(__name__))
      2. logger = bind(request_id="abc")   # dùng root logger làm base

    Cách 2 (không logger) không gắn vào logger cụ thể mà SET contextvar —
    mọi log record phát ra từ task hiện tại (kể cả logger khác nhau) đều
    tự mang request_id/context này, đúng kiểu async-safe.
    """
    if _logger is not None:
        return _BoundLogger(_logger, request_id, context)
    # Vì sao copy rồi set: contextvar là immutable-by-convention, mỗi task
    # thấy bản sao riêng nên set dict mới ở đây không ảnh hưởng task khác.
    base = dict(_context_var.get() or {})
    base.update(context)
    _context_var.set(base)
    if request_id is not None:
        _request_id_var.set(request_id)
    return _BoundLogger(logging.getLogger(), request_id, context)


def reset_bind():
    """Xóa request_id/context đã bind cho task hiện tại (cuối request)."""
    _request_id_var.set(None)
    _context_var.set(None)


def setup_logging(level: str = "INFO", json_output: bool = True) -> None:
    """Cấu hình root logger một lần duy nhất.

    - json_output=True: handler StreamHandler(sys.stdout) + JSONFormatter,
      mỗi record 1 dòng JSON.
    - json_output=False: format text ngắn gọn cho dev.
    - Gọi nhiều lần không gắn handler trùng (guard bằng _configured).
    """
    global _configured
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    if _configured:
        return
    handler = logging.StreamHandler(sys.stdout)
    if json_output:
        handler.setFormatter(JSONFormatter())
    else:
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)-8s %(name)s %(message)s"
            )
        )
    root.addHandler(handler)
    # Không để record lan tới handler mặc định của interpreter (tránh lặp).
    root.propagate = True
    _configured = True
