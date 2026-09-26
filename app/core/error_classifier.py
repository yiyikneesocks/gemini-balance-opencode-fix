"""
上游错误分类器

把上游 Gemini API / 本代理抛出的异常归类为有限的几种失败类别，
供重试循环决定「同 key 退避 / 换 key / fail fast」与「是否计入 key 失败数」。

类别语义：
- network     : 本机/链路不可达（连不上上游）。key 无辜，不得计入 key 失败数。
- client      : 请求体本身非法（400 非 key 原因）。换 key 必然复现，fail fast。
- auth        : key 无效/无权限（400 API key not valid / 401 / 403）。key 的错，快速拉黑。
- rate_limit  : 429（TPM/RPM 超限）。按 key 冷却退避后可恢复。
- overload    : 上游 500/503 过载。按 key 冷却退避后可恢复。
- unknown     : 无法识别。保守处理：视为 key 相关失败。
"""

import json
from enum import Enum
from typing import Optional, Tuple

import httpx

from app.exception.exceptions import APIError, UpstreamNetworkError


class ErrorCategory(str, Enum):
    NETWORK = "network"
    CLIENT = "client"
    AUTH = "auth"
    RATE_LIMIT = "rate_limit"
    RATE_LIMIT_RPD = "rate_limit_rpd"  # 按天配额彻底耗尽（该 key 该模型当日不可用）
    RATE_LIMIT_RPM = "rate_limit_rpm"  # 按分钟/TPM 瞬时限流（冷却后可恢复）
    OVERLOAD = "overload"
    UNKNOWN = "unknown"


# 明确属于 key 问题的 400 错误关键字（Google 对无效 key 返回 400 而非 401）
_AUTH_400_MARKERS = (
    "API key not valid",
    "API_KEY_INVALID",
    "API key expired",
    "API key service restriction",
)


def _error_message(e: Exception) -> str:
    parts = [str(e)]
    parts.extend(str(a) for a in e.args)
    return " | ".join(p for p in parts if p)


def _parse_upstream_status(e: Exception) -> Optional[int]:
    """从 Exception(status, body) 双参数约定中取状态码。"""
    if len(e.args) >= 2:
        try:
            return int(e.args[0])
        except (TypeError, ValueError):
            return None
    return None


def _is_upstream_network_error(e: Exception) -> bool:
    """按类型识别真网络错误（而非状态码）。"""
    if isinstance(e, UpstreamNetworkError):
        return True
    if isinstance(e, httpx.HTTPError):
        return True
    # 代理自抛的 503 网络 error（从 api_client 转出后可能只剩 detail 文本）
    if isinstance(e, APIError) and getattr(e, "error_code", "") == "network_error":
        return True
    return False


def classify_error(e: Exception) -> ErrorCategory:
    """把异常归类为 ErrorCategory。"""
    if _is_upstream_network_error(e):
        return ErrorCategory.NETWORK

    status = _parse_upstream_status(e)
    message = _error_message(e)

    if status == 429:
        return ErrorCategory.RATE_LIMIT
    if status in (500, 503):
        return ErrorCategory.OVERLOAD
    if status in (401, 403):
        return ErrorCategory.AUTH
    if status == 400:
        if any(marker in message for marker in _AUTH_400_MARKERS):
            return ErrorCategory.AUTH
        return ErrorCategory.CLIENT
    if status == 404:
        # 模型不存在等，换 key 无意义
        return ErrorCategory.CLIENT

    if "API key not valid" in message or "API_KEY_INVALID" in message:
        return ErrorCategory.AUTH

    return ErrorCategory.UNKNOWN


def classify_and_extract(e: Exception) -> Tuple[ErrorCategory, int, str]:
    """
    返回 (类别, 状态码, 错误消息)。

    状态码约定：network→503, client→400, auth→401, rate_limit(_rpd/_rpm)→429,
    overload→503, unknown→502；若能从异常里解析出真实状态码则优先用真实的。
    429 会进一步用 quota_parser 细分 RPD（日耗尽）/ RPM（瞬时限流）。
    """
    category = classify_error(e)
    from app.utils.helpers import extract_error_info

    status_code, message = extract_error_info(e)

    if category == ErrorCategory.RATE_LIMIT:
        # 细分：解析上游配额体，判断按天耗尽还是瞬时限流
        from app.core.quota_parser import parse_quota_error

        body = None
        if len(e.args) >= 2 and isinstance(e.args[1], str):
            body = e.args[1]
        elif isinstance(message, str):
            body = message
        quota = parse_quota_error(body)
        if quota and quota.is_per_day:
            category = ErrorCategory.RATE_LIMIT_RPD
        else:
            category = ErrorCategory.RATE_LIMIT_RPM

    # extract_error_info 对 2 参数异常会给出真实状态码；其余按类别给默认
    if category == ErrorCategory.NETWORK:
        status_code = 503
    elif category == ErrorCategory.CLIENT:
        status_code = status_code if (isinstance(status_code, int) and 400 <= status_code < 500) else 400
    elif category == ErrorCategory.AUTH:
        status_code = status_code if (isinstance(status_code, int) and 400 <= status_code < 500) else 401
    elif category in (ErrorCategory.RATE_LIMIT, ErrorCategory.RATE_LIMIT_RPD, ErrorCategory.RATE_LIMIT_RPM):
        status_code = 429
    elif category == ErrorCategory.OVERLOAD:
        status_code = status_code if (isinstance(status_code, int) and 500 <= status_code < 600) else 503
    elif category == ErrorCategory.UNKNOWN:
        status_code = status_code if isinstance(status_code, int) else 502

    return category, status_code, message
