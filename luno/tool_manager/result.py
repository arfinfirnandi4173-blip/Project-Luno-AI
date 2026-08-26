"""
result.py
=========

`ToolResult` - what every handler execution ultimately produces, matching
the spec's exact shape:

    {"success": true, "tool": "...", "action": "...", "message": "...",
     "data": {}, "execution_time_ms": 35}

plus a few EXTENSION fields (`status`, `error_type`, `retryable`) needed
to cover the spec's own "Error Handling" list (unknown tool, unknown
action, validation failure, timeout, handler crash, retryable/non-
retryable failure) - `to_dict()` still includes them, so nothing is
hidden, but the first six keys are always exactly the spec's shape for
any consumer that only cares about those.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class ResultStatus(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"


@dataclass
class ToolResult:
    success: bool
    tool: str
    action: str
    message: str = ""
    data: Dict[str, Any] = field(default_factory=dict)
    execution_time_ms: float = 0.0

    status: ResultStatus = ResultStatus.SUCCESS
    error_type: Optional[str] = None
    retryable: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "tool": self.tool,
            "action": self.action,
            "message": self.message,
            "data": dict(self.data),
            "execution_time_ms": round(self.execution_time_ms, 2),
            "status": self.status.value,
            "error_type": self.error_type,
            "retryable": self.retryable,
        }

    @classmethod
    def ok(cls, tool: str, action: str, message: str = "", data: Optional[Dict[str, Any]] = None,
           execution_time_ms: float = 0.0) -> "ToolResult":
        return cls(success=True, tool=tool, action=action, message=message, data=data or {},
                    execution_time_ms=execution_time_ms, status=ResultStatus.SUCCESS)

    @classmethod
    def fail(cls, tool: str, action: str, message: str, error_type: Optional[str] = None,
             retryable: bool = False, execution_time_ms: float = 0.0,
             status: ResultStatus = ResultStatus.FAILED, data: Optional[Dict[str, Any]] = None) -> "ToolResult":
        return cls(success=False, tool=tool, action=action, message=message, data=data or {},
                    execution_time_ms=execution_time_ms, status=status, error_type=error_type, retryable=retryable)

    @classmethod
    def coerce(cls, value: Any, tool: str, action: str) -> "ToolResult":
        """Lets a handler return a plain dict instead of constructing a
        `ToolResult` itself, for convenience - `manager.py` calls this on
        whatever a handler's `execute()` returns. Raises `TypeError` for
        anything else, which `manager.py` catches and reports as a
        `handler_crash` (a handler returning garbage is a bug in that
        handler, same severity as it raising)."""
        if isinstance(value, cls):
            return value
        if isinstance(value, dict):
            payload = dict(value)
            payload.setdefault("tool", tool)
            payload.setdefault("action", action)
            status = payload.get("status")
            if isinstance(status, str):
                payload["status"] = ResultStatus(status)
            return cls(**payload)
        raise TypeError(f"Handler for '{tool}.{action}' returned {type(value).__name__}, expected ToolResult or dict")
