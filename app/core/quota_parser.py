"""
上游 Gemini 配额错误体解析器

从上游 429 响应体中提取结构化配额信息：
- quotaId：区分按天（RPD，日额度彻底耗尽）与按分钟/TPM（RPM，瞬时限流）
- quotaDimensions.model：配额所属模型（天然支持 (key, model) 粒度禁用）
- RetryInfo.retryDelay：上游给出的确切重试等待秒数（如 "24s"）
- message 中的 "limit: N, model: X" / "Please retry in Ns" 作为兜底

真实样本（摘自 t_error_logs）：
{
  "error": {
    "code": 429,
    "message": "... Quota exceeded for metric: ...generate_content_free_tier_requests,
                limit: 20, model: gemini-3.8-flash\\nPlease retry in 24.687026793s.",
    "status": "RESOURCE_EXHAUSTED",
    "details": [
      {"@type": "...QuotaFailure", "violations": [{
          "quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier",
          "quotaDimensions": {"location": "global", "model": "gemini-3.8-flash"},
          "quotaValue": "20"}]},
      {"@type": "...RetryInfo", "retryDelay": "24s"}
    ]
  }
}
"""

import json
import re
from dataclasses import dataclass
from typing import Any, Dict, Optional, Union


@dataclass
class QuotaInfo:
    """上游配额错误的可执行信息。"""

    quota_id: Optional[str] = None
    model: Optional[str] = None
    retry_delay_s: Optional[float] = None
    limit: Optional[int] = None
    is_per_day: bool = False
    raw_message: str = ""


_RETRY_DELAY_RE = re.compile(r"^(\d+(?:\.\d+)?)s$")
_MSG_RETRY_IN_RE = re.compile(r"Please retry in\s+(\d+(?:\.\d+)?)s", re.IGNORECASE)
_MSG_LIMIT_MODEL_RE = re.compile(r"limit:\s*(\d+),\s*model:\s*([\w.\-]+)", re.IGNORECASE)


def _parse_retry_delay(value: Any) -> Optional[float]:
    if not isinstance(value, str):
        return None
    m = _RETRY_DELAY_RE.match(value.strip())
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return None
    return None


def _from_details(details: Any) -> QuotaInfo:
    """从 error.details 的结构化字段提取（权威来源）。"""
    info = QuotaInfo()
    if not isinstance(details, list):
        return info
    for item in details:
        if not isinstance(item, dict):
            continue
        type_url = str(item.get("@type", ""))
        if "QuotaFailure" in type_url:
            violations = item.get("violations") or []
            for v in violations:
                if not isinstance(v, dict):
                    continue
                info.quota_id = v.get("quotaId") or info.quota_id
                dims = v.get("quotaDimensions") or {}
                if isinstance(dims, dict) and dims.get("model"):
                    info.model = dims["model"]
                metric = str(v.get("quotaMetric") or "")
                if metric:
                    m = re.search(r"limit:\s*(\d+)", metric)
                # quotaMetric 通常不含 limit，limit 在 message 里
        elif "RetryInfo" in type_url:
            info.retry_delay_s = _parse_retry_delay(item.get("retryDelay"))
    if info.quota_id:
        info.is_per_day = "perday" in info.quota_id.lower().replace("_", "")
    return info


def _from_message(message: str) -> QuotaInfo:
    """message 文本兜底解析。"""
    info = QuotaInfo(raw_message=message or "")
    if not message:
        return info
    m = _MSG_LIMIT_MODEL_RE.search(message)
    if m:
        try:
            info.limit = int(m.group(1))
        except ValueError:
            pass
        info.model = m.group(2)
    m = _MSG_RETRY_IN_RE.search(message)
    if m:
        try:
            info.retry_delay_s = float(m.group(1))
        except ValueError:
            pass
    # message 中的 metric 名可判断按天
    if re.search(r"PerDay", message, re.IGNORECASE):
        info.is_per_day = True
    return info


def parse_quota_error(error_body: Union[str, Dict[str, Any], None]) -> Optional[QuotaInfo]:
    """解析上游 429 错误体，返回 QuotaInfo；无法识别时返回 None。

    Args:
        error_body: 原始响应体（JSON 字符串）或已反序列化的 dict
    """
    if not error_body:
        return None

    data = None
    if isinstance(error_body, dict):
        data = error_body
    elif isinstance(error_body, str):
        try:
            data = json.loads(error_body)
        except (json.JSONDecodeError, TypeError):
            # 非 JSON：退化为纯文本 message 解析
            return _from_message(error_body)

    if not isinstance(data, dict):
        return None
    error = data.get("error")
    if not isinstance(error, dict):
        return _from_message(str(data)[:500])

    message = str(error.get("message") or "")
    info = _from_details(error.get("details"))
    msg_info = _from_message(message)
    info.raw_message = message

    # details 优先，message 兜底补缺
    if not info.model:
        info.model = msg_info.model
    if info.retry_delay_s is None:
        info.retry_delay_s = msg_info.retry_delay_s
    if info.limit is None:
        info.limit = msg_info.limit
    if not info.is_per_day:
        # quotaId 缺失时用 message 里的 metric 名判断
        info.is_per_day = msg_info.is_per_day or bool(
            info.quota_id and "perday" in info.quota_id.lower().replace("_", "")
        )
    return info
