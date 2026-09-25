"""共享基础定义：业务错误、时间工具与常量。"""
from datetime import datetime, timezone

MAX_ARM_LENGTH = 40
MIN_REASON_LENGTH = 8


class BusinessError(Exception):
    def __init__(self, message, status=400, code="bad_request"):
        super().__init__(message)
        self.message, self.status, self.code = message, status, code


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
