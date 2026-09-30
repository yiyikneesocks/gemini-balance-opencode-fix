# app/services/chat_service.py

import datetime
import asyncio
import json
import re
import time
from typing import Any, AsyncGenerator, Dict, List

from app.config.config import settings
from app.core.constants import GEMINI_2_FLASH_EXP_SAFETY_SETTINGS
from app.core.error_classifier import ErrorCategory, classify_and_extract
from app.exception.exceptions import (
    AllKeysCoolingError,
    UpstreamNetworkError,
    UpstreamOverloadError,
)
from app.database.services import add_error_log, add_request_log, get_file_api_key
from app.domain.gemini_models import GeminiRequest
from app.handler.response_handler import GeminiResponseHandler
from app.handler.stream_optimizer import gemini_optimizer
from app.log.logger import get_gemini_logger
from app.service.client.api_client import GeminiApiClient
from app.service.key.key_manager import KeyManager
from app.utils.helpers import extract_error_info, redact_key_for_logging

logger = get_gemini_logger()


def _has_image_parts(contents: List[Dict[str, Any]]) -> bool:
    """判断消息是否包含图片部分"""
    for content in contents:
        if "parts" in content:
            for part in content["parts"]:
                if "image_url" in part or "inline_data" in part:
                    return True
    return False


def _extract_file_references(contents: List[Dict[str, Any]]) -> List[str]:
    """從內容中提取文件引用"""
    file_names = []
    for content in contents:
        if "parts" in content:
            for part in content["parts"]:
                if not isinstance(part, dict) or "fileData" not in part:
                    continue
                file_data = part["fileData"]
                if "fileUri" not in file_data:
                    continue
                file_uri = file_data["fileUri"]
                # 從 URI 中提取文件名
                # 1. https://generativelanguage.googleapis.com/v1beta/files/{file_id}
                match = re.match(
                    rf"{re.escape(settings.BASE_URL)}/(files/.*)", file_uri
                )
                if not match:
                    logger.warning(f"Invalid file URI: {file_uri}")
                    continue
                file_id = match.group(1)
                file_names.append(file_id)
                logger.info(f"Found file reference: {file_id}")
    return file_names


def _clean_json_schema_properties(obj: Any) -> Any:
    """清理JSON Schema中Gemini API不支持的字段"""
    if not isinstance(obj, dict):
        return obj

    # Gemini API不支持的JSON Schema字段
    unsupported_fields = {
        "exclusiveMaximum",
        "exclusiveMinimum",
        "const",
        "examples",
        "contentEncoding",
        "contentMediaType",
        "if",
        "then",
        "else",
        "allOf",
        "anyOf",
        "oneOf",
        "not",
        "definitions",
        "$schema",
        "$id",
        "$ref",
        "$comment",
        "readOnly",
        "writeOnly",
    }

    cleaned = {}
    for key, value in obj.items():
        if key in unsupported_fields:
            continue
        if isinstance(value, dict):
            cleaned[key] = _clean_json_schema_properties(value)
        elif isinstance(value, list):
            cleaned[key] = [_clean_json_schema_properties(item) for item in value]
        else:
            cleaned[key] = value

    return cleaned


def _build_tools(model: str, payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """构建工具"""

    def _has_function_call(contents: List[Dict[str, Any]]) -> bool:
        """检查内容中是否包含 functionCall"""
        if not contents or not isinstance(contents, list):
            return False
        for content in contents:
            if not content or not isinstance(content, dict) or "parts" not in content:
                continue
            parts = content.get("parts", [])
            if not parts or not isinstance(parts, list):
                continue
            for part in parts:
                if isinstance(part, dict) and "functionCall" in part:
                    return True
        return False

    def _merge_tools(tools: List[Dict[str, Any]]) -> Dict[str, Any]:
        record = dict()
        for item in tools:
            if not item or not isinstance(item, dict):
                continue

            for k, v in item.items():
                if k == "functionDeclarations" and v and isinstance(v, list):
                    functions = record.get("functionDeclarations", [])
                    # 清理每个函数声明中的不支持字段
                    cleaned_functions = []
                    for func in v:
                        if isinstance(func, dict):
                            cleaned_func = _clean_json_schema_properties(func)
                            cleaned_functions.append(cleaned_func)
                        else:
                            cleaned_functions.append(func)
                    functions.extend(cleaned_functions)
                    record["functionDeclarations"] = functions
                else:
                    record[k] = v
        return record

    def _is_structured_output_request(payload: Dict[str, Any]) -> bool:
        """检查请求是否要求结构化JSON输出"""
        try:
            generation_config = payload.get("generationConfig", {})
            return generation_config.get("responseMimeType") == "application/json"
        except (AttributeError, TypeError):
            return False

    tool = dict()
    if payload and isinstance(payload, dict) and "tools" in payload:
        if payload.get("tools") and isinstance(payload.get("tools"), dict):
            payload["tools"] = [payload.get("tools")]
        items = payload.get("tools", [])
        if items and isinstance(items, list):
            tool.update(_merge_tools(items))

    # "Tool use with a response mime type: 'application/json' is unsupported"
    # Gemini API限制：不支持同时使用tools和结构化输出(response_mime_type='application/json')
    # 当请求指定了JSON响应格式时，跳过所有工具的添加以避免API错误
    has_structured_output = _is_structured_output_request(payload)
    if not has_structured_output:
        if (
            settings.TOOLS_CODE_EXECUTION_ENABLED
            and not (model.endswith("-search") or "-thinking" in model)
            and not _has_image_parts(payload.get("contents", []))
        ):
            tool["codeExecution"] = {}

        if model.endswith("-search"):
            tool["googleSearch"] = {}

        real_model = _get_real_model(model)
        if real_model in settings.URL_CONTEXT_MODELS and settings.URL_CONTEXT_ENABLED:
            tool["urlContext"] = {}

    # 解决 "Tool use with function calling is unsupported" 问题
    if tool.get("functionDeclarations") or _has_function_call(
        payload.get("contents", [])
    ):
        tool.pop("googleSearch", None)
        tool.pop("codeExecution", None)
        tool.pop("urlContext", None)

    return [tool] if tool else []


def _get_real_model(model: str) -> str:
    if model.endswith("-search"):
        model = model[:-7]
    if model.endswith("-image"):
        model = model[:-6]
    if model.endswith("-non-thinking"):
        model = model[:-13]
    if "-search" in model and "-non-thinking" in model:
        model = model[:-20]
    return model


def _get_safety_settings(model: str) -> List[Dict[str, str]]:
    """获取安全设置"""
    if model == "gemini-2.0-flash-exp":
        return GEMINI_2_FLASH_EXP_SAFETY_SETTINGS
    return settings.SAFETY_SETTINGS


def _filter_empty_parts(contents: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Filters out contents with empty or invalid parts."""
    if not contents:
        return []

    filtered_contents = []
    for content in contents:
        if (
            not content
            or "parts" not in content
            or not isinstance(content.get("parts"), list)
        ):
            continue

        valid_parts = [
            part for part in content["parts"] if isinstance(part, dict) and part
        ]

        if valid_parts:
            new_content = content.copy()
            new_content["parts"] = valid_parts
            filtered_contents.append(new_content)

    return filtered_contents


def _ensure_valid_ending_turn(contents: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Ensure contents do not end with a model turn / empty parts.

    Gemini rejects requests whose last content has role "model" (400
    "Requests ending with a model turn are not supported"). The upstream
    @ai-sdk/google SDK may emit a trailing empty model turn, and this
    proxy's _filter_empty_parts can additionally drop a trailing empty
    user turn, exposing a model turn. Append a minimal user turn to make
    the payload valid.

    A trailing user turn whose parts are structurally non-empty but carry
    no usable text (e.g. ``[{"text": ""}]``) is treated the same way, since
    upstream strips empty text and would again end on a model turn.
    """

    def _has_usable_part(parts: Any) -> bool:
        if not isinstance(parts, list):
            return False
        for part in parts:
            if not isinstance(part, dict) or not part:
                continue
            text = part.get("text")
            if text is not None:
                if isinstance(text, str) and text.strip():
                    return True
                continue
            # Non-text parts (inlineData / fileData / functionCall / ...)
            return True
        return False

    if not contents:
        contents = []
    last = contents[-1] if contents else None
    last_role = (last or {}).get("role")
    if last is None or last_role == "model" or not _has_usable_part(
        (last or {}).get("parts")
    ):
        contents = list(contents)
        contents.append({"role": "user", "parts": [{"text": "continue"}]})
    return contents


def _build_payload(model: str, request: GeminiRequest) -> Dict[str, Any]:
    """构建请求payload"""
    request_dict = request.model_dump(exclude_none=False)
    if request.generationConfig:
        if request.generationConfig.maxOutputTokens is None:
            # 如果未指定最大输出长度，则不传递该字段，解决截断的问题
            if "maxOutputTokens" in request_dict["generationConfig"]:
                request_dict["generationConfig"].pop("maxOutputTokens")

    # 检查是否为TTS模型
    is_tts_model = "tts" in model.lower()

    if is_tts_model:
        # TTS模型使用简化的payload，不包含tools和safetySettings
        payload = {
            "contents": _ensure_valid_ending_turn(
                _filter_empty_parts(request_dict.get("contents", []))
            ),
            "generationConfig": request_dict.get("generationConfig"),
        }

        # 只在有systemInstruction时才添加
        if request_dict.get("systemInstruction"):
            payload["systemInstruction"] = request_dict.get("systemInstruction")
    else:
        # 非TTS模型使用完整的payload
        payload = {
            "contents": _ensure_valid_ending_turn(
                _filter_empty_parts(request_dict.get("contents", []))
            ),
            "tools": _build_tools(model, request_dict),
            "safetySettings": _get_safety_settings(model),
            "generationConfig": request_dict.get("generationConfig"),
            "systemInstruction": request_dict.get("systemInstruction"),
        }

    # 确保 generationConfig 不为 None
    if payload["generationConfig"] is None:
        payload["generationConfig"] = {}

    if model.endswith("-image") or model.endswith("-image-generation"):
        payload.pop("systemInstruction")
        payload["generationConfig"]["responseModalities"] = ["Text", "Image"]

    # 处理思考配置：优先使用客户端提供的配置，否则使用默认配置
    client_thinking_config = None
    if request.generationConfig and request.generationConfig.thinkingConfig:
        client_thinking_config = request.generationConfig.thinkingConfig

    if client_thinking_config is not None:
        # 客户端提供了思考配置，直接使用
        payload["generationConfig"]["thinkingConfig"] = client_thinking_config
    else:
        # 客户端没有提供思考配置，使用默认配置
        if model.endswith("-non-thinking"):
            if "gemini-2.5-pro" in model:
                payload["generationConfig"]["thinkingConfig"] = {"thinkingBudget": 128}
            else:
                payload["generationConfig"]["thinkingConfig"] = {"thinkingBudget": 0}
        elif _get_real_model(model) in settings.THINKING_BUDGET_MAP:
            if settings.SHOW_THINKING_PROCESS:
                payload["generationConfig"]["thinkingConfig"] = {
                    "thinkingBudget": settings.THINKING_BUDGET_MAP.get(model, 1000),
                    "includeThoughts": True,
                }
            else:
                payload["generationConfig"]["thinkingConfig"] = {
                    "thinkingBudget": settings.THINKING_BUDGET_MAP.get(model, 1000)
                }

    return payload


class GeminiChatService:
    """聊天服务"""

    def __init__(self, base_url: str, key_manager: KeyManager):
        self.api_client = GeminiApiClient(base_url, settings.TIME_OUT)
        self.key_manager = key_manager
        self.response_handler = GeminiResponseHandler()

    def _extract_text_from_response(self, response: Dict[str, Any]) -> str:
        """从响应中提取文本内容"""
        if not response.get("candidates"):
            return ""

        candidate = response["candidates"][0]
        content = candidate.get("content", {})
        parts = content.get("parts", [])

        if parts and "text" in parts[0]:
            return parts[0].get("text", "")
        return ""

    def _create_char_response(
        self, original_response: Dict[str, Any], text: str
    ) -> Dict[str, Any]:
        """创建包含指定文本的响应"""
        response_copy = json.loads(json.dumps(original_response))
        if response_copy.get("candidates") and response_copy["candidates"][0].get(
            "content", {}
        ).get("parts"):
            response_copy["candidates"][0]["content"]["parts"][0]["text"] = text
        return response_copy

    async def _call_with_retry(self, call, api_key: str, model: str = ""):
        """带分类的重试执行器（Gemini 原生路径）。

        策略：
        - network   : 同 key 指数退避重试 NETWORK_RETRY_ATTEMPTS 次，不计失败数；
                      仍失败抛出明确网络错误。
        - client    : 立即抛出（换 key 必然复现），不计数。
        - auth      : 永久失败数 +2 快速拉黑，立即换 key。
        - rate_limit / overload : 仅给 (key, model) 设置指数冷却（TPM/RPM 配额
                      按模型计，一个模型 429 不代表其他模型不能用），不进永久
                      失败数；换 key 时自动跳过该 (key, model) 组合。
        - unknown   : 保守按永久失败 +1 处理，换 key。
        全部 key 冷却中时等待最早到期（封顶配置）。
        """
        current_key = api_key
        network_attempt = 0
        max_network_retries = settings.NETWORK_RETRY_ATTEMPTS
        switched_keys = 0
        rpd_probe = 0
        self._rpd_probed_keys: set = set()

        # 熔断窗口内：直接返回与"首个触发者"相同的错误，快速失败，不空转
        active = self.key_manager.get_active_breaker_error(model)
        if active is not None:
            raise UpstreamOverloadError(model=model, detail=active)

        while True:
            try:
                return await call(current_key)
            except Exception as e:
                category, status_code, message = classify_and_extract(e)
                logger.warning(
                    f"Upstream call failed [{category.value}] status={status_code}: "
                    f"{message[:200]}"
                )

                if category == ErrorCategory.NETWORK:
                    # 记录到监控（不计惩罚、不设冷却），便于看到今日网络错误
                    await self.key_manager.note_error(
                        current_key,
                        model,
                        kind="network",
                        error_log=message,
                        error_code=status_code,
                    )
                    network_attempt += 1
                    if network_attempt >= max_network_retries:
                        logger.error(
                            f"Network unreachable after {network_attempt} attempts "
                            f"on same key; giving up (key not penalized)"
                        )
                        # 网络不可达：触发全局熔断，窗口内所有模型快速失败
                        err = UpstreamNetworkError(
                            f"{type(e).__name__}: {message}".strip(": ")
                        )
                        self.key_manager.trip_global_breaker(err.detail)
                        raise err from e
                    backoff = settings.NETWORK_BACKOFF_BASE_S * (
                        2 ** (network_attempt - 1)
                    )
                    logger.warning(
                        f"Network error, retrying same key in {backoff:.1f}s "
                        f"(attempt {network_attempt}/{max_network_retries})"
                    )
                    await asyncio.sleep(backoff)
                    continue

                # 非 network：按类别处理 key
                if category == ErrorCategory.CLIENT:
                    # 请求体问题，换 key 无意义也不记账；仅记录到监控
                    await self.key_manager.note_error(
                        current_key,
                        model,
                        kind="client",
                        error_log=message,
                        error_code=status_code,
                    )
                    raise
                elif category == ErrorCategory.AUTH:
                    async with self.key_manager.failure_count_lock:
                        if current_key in self.key_manager.key_failure_counts:
                            self.key_manager.key_failure_counts[current_key] += 2
                elif category == ErrorCategory.OVERLOAD:
                    # 上游 503 过载（high demand）是"模型全局"问题，换 key 无效：
                    # 记录模型级过载计数；达到阈值则直接中断，建议换模型。
                    overload_count = self.key_manager.record_model_overload(
                        model,
                        key=current_key,
                        error_log=message,
                        error_code=status_code,
                    )
                    if self.key_manager.is_model_overloaded_for_giveup(model):
                        hints = self.key_manager.get_available_models_hint(
                            exclude_model=model
                        )
                        logger.error(
                            f"Model {model} overloaded {overload_count} times in a "
                            f"row; returning non-retryable error, suggest switching "
                            f"model: {hints}"
                        )
                        err = UpstreamOverloadError(model=model, model_hints=hints)
                        self.key_manager.trip_model_breaker(model, err.detail)
                        raise err from e
                elif category in (
                    ErrorCategory.RATE_LIMIT_RPD,
                    ErrorCategory.RATE_LIMIT_RPM,
                    ErrorCategory.RATE_LIMIT,
                ):
                    # 瞬时限流：只按 (key, model) 冷却，不进永久失败数。
                    # 上游给了 retryDelay 就以它为准；仅在无 retryDelay 时，RPD 才用长冷却。
                    from app.core.quota_parser import parse_quota_error

                    body = e.args[1] if len(e.args) >= 2 else message
                    quota = parse_quota_error(body if isinstance(body, str) else None)
                    if quota and quota.retry_delay_s:
                        # 上游给了确切重试时间就以它为准（免费层的 PerDay 实为滚动额度，
                        # 上游常说 "retry in 12s"，此时按 RPD 冷却到午夜会过度惩罚）。
                        await self.key_manager.mark_key_cooldown(
                            current_key,
                            model,
                            seconds=quota.retry_delay_s,
                            error_log=message,
                            error_code=status_code,
                        )
                    elif category == ErrorCategory.RATE_LIMIT_RPD:
                        # 无 retryDelay 的 RPD：真正的日额度耗尽，长冷却到太平洋午夜
                        await self.key_manager.mark_key_cooldown(
                            current_key,
                            model,
                            kind="rpd",
                            error_log=message,
                            error_code=status_code,
                        )
                    else:
                        await self.key_manager.mark_key_cooldown(
                            current_key,
                            model,
                            error_log=message,
                            error_code=status_code,
                        )
                else:  # unknown：保守记账
                    async with self.key_manager.failure_count_lock:
                        if current_key in self.key_manager.key_failure_counts:
                            self.key_manager.key_failure_counts[current_key] += 1

                switched_keys += 1
                next_key = await self.key_manager.get_next_working_key(model)
                if next_key:
                    logger.info(
                        f"Switched to new API key: {redact_key_for_logging(next_key)} "
                        f"after {category.value} error"
                    )
                    current_key = next_key
                    network_attempt = 0  # 换 key 后重新计算网络退避
                    continue

                # 无可用 key（全部冷却/失效）：等最早到期（封顶配置）后重取
                wait_s = await self.key_manager.earliest_cooldown_release(model)
                # 仅当"每一把 key 都是 RPD 日耗尽"时才算 RPD 全耗尽；
                # 部分 RPD、部分 RPM 的情况按"稍后可重试"处理，不误报 RPD。
                rpd_exhausted = self.key_manager.all_keys_rpd_exhausted(model)
                if wait_s and not rpd_exhausted:
                    wait_s = min(wait_s, settings.ALL_COOLING_MAX_WAIT_S)
                    logger.warning(
                        f"All keys cooling down for model={model or '_'} "
                        f"(not all RPD-exhausted); waiting {wait_s:.1f}s "
                        f"for earliest release"
                    )
                    await asyncio.sleep(wait_s)
                    next_key = await self.key_manager.get_next_working_key(model)
                    if next_key:
                        current_key = next_key
                        network_attempt = 0
                        continue
                    # 等待后仍无 key：重新评估是否已变成全 RPD 耗尽
                    rpd_exhausted = self.key_manager.all_keys_rpd_exhausted(model)
                # 全池 RPD 耗尽：配额可能已重置，再探测重试 RPD_PROBE_ATTEMPTS 轮
                if rpd_exhausted and rpd_probe < settings.RPD_PROBE_ATTEMPTS:
                    probe_key = self.key_manager.pick_rpd_probe_key(
                        model, exclude=self._rpd_probed_keys
                    )
                    if probe_key:
                        rpd_probe += 1
                        self._rpd_probed_keys.add(probe_key)
                        logger.warning(
                            f"All keys RPD-exhausted for model={model or '_'}, but "
                            f"probing key {redact_key_for_logging(probe_key)} anyway "
                            f"(attempt {rpd_probe}/{settings.RPD_PROBE_ATTEMPTS}); "
                            f"quota may have reset"
                        )
                        current_key = probe_key
                        network_attempt = 0
                        continue
                logger.error(
                    f"No available API key after {switched_keys} switches; failing"
                )
                hints = self.key_manager.get_available_models_hint(
                    exclude_model=model
                )
                if rpd_exhausted:
                    logger.error(
                        f"All keys RPD-exhausted for model={model or '_'}; "
                        f"suggesting model switch: {hints}"
                    )
                err = AllKeysCoolingError(
                    model=model, model_hints=hints, rpd_exhausted=rpd_exhausted
                )
                if rpd_exhausted:
                    # 全 RPD 耗尽：按模型熔断，窗口内该模型快速失败
                    self.key_manager.trip_model_breaker(model, err.detail)
                raise err from e

    async def generate_content(
        self, model: str, request: GeminiRequest, api_key: str
    ) -> Dict[str, Any]:
        """生成内容"""
        # 檢查並獲取文件專用的 API key（如果有文件）
        file_names = _extract_file_references(request.model_dump().get("contents", []))
        if file_names:
            logger.info(f"Request contains file references: {file_names}")
            file_api_key = await get_file_api_key(file_names[0])
            if file_api_key:
                logger.info(
                    f"Found API key for file {file_names[0]}: {redact_key_for_logging(file_api_key)}"
                )
                api_key = file_api_key  # 使用文件的 API key
            else:
                logger.warning(
                    f"No API key found for file {file_names[0]}, using default key: {redact_key_for_logging(api_key)}"
                )

        payload = _build_payload(model, request)
        start_time = time.perf_counter()
        request_datetime = datetime.datetime.now()
        is_success = False
        status_code = None
        response = None

        try:
            response = await self._call_with_retry(
                lambda key: self.api_client.generate_content(payload, model, key),
                api_key,
                model,
            )
            is_success = True
            status_code = 200
            await self.key_manager.mark_key_success(api_key, model)
            await self.key_manager.reset_permanent_failure_count(api_key)
            self.key_manager.clear_model_overload(model)
            self.key_manager.clear_breaker_on_success(model)
            return self.response_handler.handle_response(response, model, stream=False)
        except Exception as e:
            is_success = False
            status_code, error_log_msg = extract_error_info(e)
            logger.error(f"Normal API call failed with error: {error_log_msg}")

            await add_error_log(
                gemini_key=api_key,
                model_name=model,
                error_type="gemini-chat-non-stream",
                error_log=error_log_msg,
                error_code=status_code,
                request_msg=payload if settings.ERROR_LOG_RECORD_REQUEST_BODY else None,
                request_datetime=request_datetime,
            )
            raise e
        finally:
            end_time = time.perf_counter()
            latency_ms = int((end_time - start_time) * 1000)
            await add_request_log(
                model_name=model,
                api_key=api_key,
                is_success=is_success,
                status_code=status_code,
                latency_ms=latency_ms,
                request_time=request_datetime,
            )

    async def count_tokens(
        self, model: str, request: GeminiRequest, api_key: str
    ) -> Dict[str, Any]:
        """计算token数量"""
        # countTokens API只需要contents
        payload = {
            "contents": _filter_empty_parts(request.model_dump().get("contents", []))
        }
        start_time = time.perf_counter()
        request_datetime = datetime.datetime.now()
        is_success = False
        status_code = None
        response = None

        try:
            response = await self.api_client.count_tokens(payload, model, api_key)
            is_success = True
            status_code = 200
            return response
        except Exception as e:
            is_success = False
            status_code, error_log_msg = extract_error_info(e)
            logger.error(f"Count tokens API call failed with error: {error_log_msg}")

            await add_error_log(
                gemini_key=api_key,
                model_name=model,
                error_type="gemini-count-tokens",
                error_log=error_log_msg,
                error_code=status_code,
                request_msg=payload if settings.ERROR_LOG_RECORD_REQUEST_BODY else None,
            )
            raise e
        finally:
            end_time = time.perf_counter()
            latency_ms = int((end_time - start_time) * 1000)
            await add_request_log(
                model_name=model,
                api_key=api_key,
                is_success=is_success,
                status_code=status_code,
                latency_ms=latency_ms,
                request_time=request_datetime,
            )

    async def stream_generate_content(
        self, model: str, request: GeminiRequest, api_key: str
    ) -> AsyncGenerator[str, None]:
        """流式生成内容"""
        # 檢查並獲取文件專用的 API key（如果有文件）
        file_names = _extract_file_references(request.model_dump().get("contents", []))
        if file_names:
            logger.info(f"Request contains file references: {file_names}")
            file_api_key = await get_file_api_key(file_names[0])
            if file_api_key:
                logger.info(
                    f"Found API key for file {file_names[0]}: {redact_key_for_logging(file_api_key)}"
                )
                api_key = file_api_key  # 使用文件的 API key
            else:
                logger.warning(
                    f"No API key found for file {file_names[0]}, using default key: {redact_key_for_logging(api_key)}"
                )

        retries = 0
        max_retries = max(settings.MAX_RETRIES, 3)  # 换 key 轮次上限
        payload = _build_payload(model, request)
        is_success = False
        status_code = None
        final_api_key = api_key

        current_key = api_key
        network_attempt = 0
        max_network_retries = settings.NETWORK_RETRY_ATTEMPTS
        first_chunk_sent = False
        rpd_probe = 0
        self._rpd_probed_keys: set = set()

        # 熔断窗口内：直接返回与"首个触发者"相同的错误，快速失败，不空转
        active = self.key_manager.get_active_breaker_error(model)
        if active is not None:
            raise UpstreamOverloadError(model=model, detail=active)

        while True:
            request_datetime = datetime.datetime.now()
            start_time = time.perf_counter()
            final_api_key = current_key
            try:
                async for line in self.api_client.stream_generate_content(
                    payload, model, current_key
                ):
                    # print(line)
                    if line.startswith("data:"):
                        first_chunk_sent = True
                        line = line[6:]
                        response_data = self.response_handler.handle_response(
                            json.loads(line), model, stream=True
                        )
                        text = self._extract_text_from_response(response_data)
                        # 如果有文本内容，且开启了流式输出优化器，则使用流式输出优化器处理
                        if text and settings.STREAM_OPTIMIZER_ENABLED:
                            # 使用流式输出优化器处理文本输出
                            async for (
                                optimized_chunk
                            ) in gemini_optimizer.optimize_stream_output(
                                text,
                                lambda t: self._create_char_response(response_data, t),
                                lambda c: "data: " + json.dumps(c) + "\n\n",
                            ):
                                yield optimized_chunk
                        else:
                            # 如果没有文本内容（如工具调用等），整块输出
                            yield "data: " + json.dumps(response_data) + "\n\n"
                logger.info("Streaming completed successfully")
                is_success = True
                status_code = 200
                await self.key_manager.mark_key_success(current_key, model)
                await self.key_manager.reset_permanent_failure_count(current_key)
                self.key_manager.clear_model_overload(model)
                self.key_manager.clear_breaker_on_success(model)
                break
            except Exception as e:
                is_success = False
                category, status_code, error_log_msg = classify_and_extract(e)
                logger.warning(
                    f"Streaming API call failed [{category.value}] "
                    f"status={status_code}: {error_log_msg[:200]}"
                )

                # 流已经开始输出：无法重试（会把两段流拼进同一个 SSE），直接抛
                if first_chunk_sent:
                    logger.error(
                        "Stream already emitted chunks; aborting without retry "
                        "to avoid corrupting the SSE stream"
                    )
                    raise

                if category == ErrorCategory.NETWORK:
                    # 记录到监控（不计惩罚、不设冷却），便于看到今日网络错误
                    await self.key_manager.note_error(
                        current_key,
                        model,
                        kind="network",
                        error_log=error_log_msg,
                        error_code=status_code,
                    )
                    network_attempt += 1
                    if network_attempt >= max_network_retries:
                        logger.error(
                            f"Network unreachable after {network_attempt} attempts "
                            f"on same key; giving up (key not penalized)"
                        )
                        err = UpstreamNetworkError(
                            f"{type(e).__name__}: {message}".strip(": ")
                        )
                        self.key_manager.trip_global_breaker(err.detail)
                        raise err from e
                    backoff = settings.NETWORK_BACKOFF_BASE_S * (
                        2 ** (network_attempt - 1)
                    )
                    logger.warning(
                        f"Network error, retrying same key in {backoff:.1f}s "
                        f"(attempt {network_attempt}/{max_network_retries})"
                    )
                    await asyncio.sleep(backoff)
                    continue

                await add_error_log(
                    gemini_key=current_key,
                    model_name=model,
                    error_type="gemini-chat-stream",
                    error_log=error_log_msg,
                    error_code=status_code,
                    request_msg=(
                        payload if settings.ERROR_LOG_RECORD_REQUEST_BODY else None
                    ),
                    request_datetime=request_datetime,
                )

                if category == ErrorCategory.CLIENT:
                    await self.key_manager.note_error(
                        current_key,
                        model,
                        kind="client",
                        error_log=error_log_msg,
                        error_code=status_code,
                    )
                    raise
                elif category == ErrorCategory.AUTH:
                    async with self.key_manager.failure_count_lock:
                        if current_key in self.key_manager.key_failure_counts:
                            self.key_manager.key_failure_counts[current_key] += 2
                elif category == ErrorCategory.OVERLOAD:
                    # 上游 503 过载（high demand）是"模型全局"问题，换 key 无效：
                    # 记录模型级过载计数；达到阈值则直接中断，建议换模型。
                    overload_count = self.key_manager.record_model_overload(
                        model,
                        key=current_key,
                        error_log=error_log_msg,
                        error_code=status_code,
                    )
                    if self.key_manager.is_model_overloaded_for_giveup(model):
                        hints = self.key_manager.get_available_models_hint(
                            exclude_model=model
                        )
                        logger.error(
                            f"Model {model} overloaded {overload_count} times in a "
                            f"row; returning non-retryable error, suggest switching "
                            f"model: {hints}"
                        )
                        err = UpstreamOverloadError(model=model, model_hints=hints)
                        self.key_manager.trip_model_breaker(model, err.detail)
                        raise err from e
                elif category in (
                    ErrorCategory.RATE_LIMIT_RPD,
                    ErrorCategory.RATE_LIMIT_RPM,
                    ErrorCategory.RATE_LIMIT,
                ):
                    # 瞬时限流：只按 (key, model) 冷却，不进永久失败数。
                    # 上游给了 retryDelay 就以它为准；仅在无 retryDelay 时，RPD 才用长冷却。
                    from app.core.quota_parser import parse_quota_error

                    body = e.args[1] if len(e.args) >= 2 else error_log_msg
                    quota = parse_quota_error(body if isinstance(body, str) else None)
                    if quota and quota.retry_delay_s:
                        # 上游给了确切重试时间就以它为准（免费层的 PerDay 实为滚动额度，
                        # 上游常说 "retry in 12s"，此时按 RPD 冷却到午夜会过度惩罚）。
                        await self.key_manager.mark_key_cooldown(
                            current_key,
                            model,
                            seconds=quota.retry_delay_s,
                            error_log=error_log_msg,
                            error_code=status_code,
                        )
                    elif category == ErrorCategory.RATE_LIMIT_RPD:
                        # 无 retryDelay 的 RPD：真正的日额度耗尽，长冷却到太平洋午夜
                        await self.key_manager.mark_key_cooldown(
                            current_key,
                            model,
                            kind="rpd",
                            error_log=error_log_msg,
                            error_code=status_code,
                        )
                    else:
                        await self.key_manager.mark_key_cooldown(
                            current_key,
                            model,
                            error_log=error_log_msg,
                            error_code=status_code,
                        )
                else:
                    async with self.key_manager.failure_count_lock:
                        if current_key in self.key_manager.key_failure_counts:
                            self.key_manager.key_failure_counts[current_key] += 1

                retries += 1
                if retries >= max_retries:
                    logger.error(
                        f"Max key switches ({max_retries}) reached for streaming."
                    )
                    raise
                next_key = await self.key_manager.get_next_working_key(model)
                if next_key:
                    logger.info(
                        f"Switched to new API key: {redact_key_for_logging(next_key)} "
                        f"after {category.value} error"
                    )
                    current_key = next_key
                    network_attempt = 0
                    continue

                wait_s = await self.key_manager.earliest_cooldown_release(model)
                # 仅当每一把 key 都是 RPD 日耗尽时才算 RPD 全耗尽；
                # 部分 RPD、部分 RPM 的情况按"稍后可重试"处理，不误报 RPD。
                rpd_exhausted = self.key_manager.all_keys_rpd_exhausted(model)
                if wait_s and not rpd_exhausted:
                    wait_s = min(wait_s, settings.ALL_COOLING_MAX_WAIT_S)
                    logger.warning(
                        f"All keys cooling down for model={model or '_'} "
                        f"(not all RPD-exhausted); waiting {wait_s:.1f}s "
                        f"for earliest release"
                    )
                    await asyncio.sleep(wait_s)
                    next_key = await self.key_manager.get_next_working_key(model)
                    if next_key:
                        current_key = next_key
                        network_attempt = 0
                        continue
                    # 等待后仍无 key：重新评估是否已变成全 RPD 耗尽
                    rpd_exhausted = self.key_manager.all_keys_rpd_exhausted(model)
                # 全池 RPD 耗尽：配额可能已重置，再探测重试 RPD_PROBE_ATTEMPTS 轮
                if rpd_exhausted and rpd_probe < settings.RPD_PROBE_ATTEMPTS:
                    probe_key = self.key_manager.pick_rpd_probe_key(
                        model, exclude=self._rpd_probed_keys
                    )
                    if probe_key:
                        rpd_probe += 1
                        self._rpd_probed_keys.add(probe_key)
                        logger.warning(
                            f"All keys RPD-exhausted for model={model or '_'}, but "
                            f"probing key {redact_key_for_logging(probe_key)} anyway "
                            f"(attempt {rpd_probe}/{settings.RPD_PROBE_ATTEMPTS}); "
                            f"quota may have reset"
                        )
                        current_key = probe_key
                        network_attempt = 0
                        continue

                logger.error(f"No valid API key available after {retries} retries.")
                hints = self.key_manager.get_available_models_hint(
                    exclude_model=model
                )
                if rpd_exhausted:
                    logger.error(
                        f"All keys RPD-exhausted for model={model or '_'}; "
                        f"suggesting model switch: {hints}"
                    )
                err = AllKeysCoolingError(
                    model=model, model_hints=hints, rpd_exhausted=rpd_exhausted
                )
                if rpd_exhausted:
                    # 全 RPD 耗尽：按模型熔断，窗口内该模型快速失败
                    self.key_manager.trip_model_breaker(model, err.detail)
                raise err from e
            finally:
                end_time = time.perf_counter()
                latency_ms = int((end_time - start_time) * 1000)
                await add_request_log(
                    model_name=model,
                    api_key=final_api_key,
                    is_success=is_success,
                    status_code=status_code,
                    latency_ms=latency_ms,
                    request_time=request_datetime,
                )
