"""
异常处理模块，定义应用程序中使用的自定义异常和异常处理器
"""

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.log.logger import get_exceptions_logger

logger = get_exceptions_logger()


class APIError(Exception):
    """API错误基类

    retry_after: 可选的建议重试秒数；设置后会在响应里带 `Retry-After` 头。
    opencode/AI SDK 会优先遵守该头（不被 30s 上限 clamp），实现"我们说多久就等多久"。
    """

    def __init__(
        self,
        status_code: int,
        detail: str,
        error_code: str = None,
        retry_after: float = None,
    ):
        self.status_code = status_code
        self.detail = detail
        self.error_code = error_code or "api_error"
        self.retry_after = retry_after
        super().__init__(self.detail)


class AuthenticationError(APIError):
    """认证错误"""

    def __init__(self, detail: str = "Authentication failed"):
        super().__init__(
            status_code=401, detail=detail, error_code="authentication_error"
        )


class AuthorizationError(APIError):
    """授权错误"""

    def __init__(self, detail: str = "Not authorized to access this resource"):
        super().__init__(
            status_code=403, detail=detail, error_code="authorization_error"
        )


class ResourceNotFoundError(APIError):
    """资源未找到错误"""

    def __init__(self, detail: str = "Resource not found"):
        super().__init__(
            status_code=404, detail=detail, error_code="resource_not_found"
        )


class ModelNotSupportedError(APIError):
    """模型不支持错误"""

    def __init__(self, model: str):
        super().__init__(
            status_code=400,
            detail=f"Model {model} is not supported",
            error_code="model_not_supported",
        )


class APIKeyError(APIError):
    """API密钥错误"""

    def __init__(self, detail: str = "Invalid or expired API key"):
        super().__init__(status_code=401, detail=detail, error_code="api_key_error")


class ServiceUnavailableError(APIError):
    """服务不可用错误"""

    def __init__(self, detail: str = "Service temporarily unavailable"):
        super().__init__(
            status_code=503, detail=detail, error_code="service_unavailable"
        )


class UpstreamNetworkError(APIError):
    """上游 Gemini API 网络不可达错误。

    默认返回 NETWORK_ERROR_STATUS_CODE（默认 424）——AI SDK / opencode 只把
    408/409/429/5xx 视为可重试，424 属于"不可重试"，据此让 opencode 立刻停止
    重试、直接报错，避免网络真断时全池空转、日志刷屏。

    ⚠️ 文案必须避开 opencode 的"可重试"正则（见 opencode-config/RETRY-POLICY.md
    §3）：message/body 命中 `network error`/`connection error`/`timeout`/`5xx数字`
    等字样，即使状态码是 424 也会被重试。因此**不要回显原始异常文本**。
    """

    # 面向客户端的固定文案：刻意不含任何可重试触发词
    SAFE_DETAIL = (
        "The proxy cannot reach the upstream provider right now. "
        "Please check your link, then pick a different model to continue."
    )

    def __init__(self, detail: str = ""):
        try:
            from app.config.config import settings

            status = settings.NETWORK_ERROR_STATUS_CODE
        except Exception:
            status = 424
        # 原始 detail 只用于日志（见 error_log），不放入客户端文案
        self.raw_detail = detail
        super().__init__(
            status_code=status,
            detail=self.SAFE_DETAIL,
            error_code="network_error",
        )


class UpstreamOverloadError(APIError):
    """上游模型持续过载（high demand），换 key 无效，应换模型。

    返回不可重试状态码，让客户端立即停止重试并改用其他模型。
    也用于熔断窗口内复用"首个触发者"的错误文案（detail 显式传入时原样使用）。

    ⚠️ 文案避开 opencode 可重试正则：不含 `503`/`overloaded`/`at capacity`
    等字样（否则 424 仍会被重试，见 opencode-config/RETRY-POLICY.md §3）。
    """

    def __init__(self, model: str = "", model_hints=None, detail: str = ""):
        self.model_hints = list(model_hints or [])
        try:
            from app.config.config import settings

            status = settings.NETWORK_ERROR_STATUS_CODE
        except Exception:
            status = 424
        if not detail:
            detail = (
                f"The upstream model '{model or 'requested model'}' is busy on the "
                f"provider side, and other keys will not help. "
                f"Please select another model to continue."
            )
            if self.model_hints:
                detail += (
                    f" Models you can try: {', '.join(self.model_hints)}."
                )
        super().__init__(status_code=status, detail=detail, error_code="upstream_overload")


class AllKeysCoolingError(APIError):
    """指定模型的所有 key 都在冷却中（429/503 累积）。

    - rpd_exhausted=False：瞬时限流，稍后重试即可，或尝试其他模型。
    - rpd_exhausted=True ：该模型所有 key 的日配额已耗尽（RPD），重试无意义，
      应当换模型（提示中会列出当前仍有配额的模型）。
    """

    def __init__(
        self,
        model: str = "",
        retry_after_s: float = 0,
        model_hints=None,
        rpd_exhausted: bool = False,
    ):
        self.model_hints = list(model_hints or [])
        self.retry_after_s = retry_after_s
        self.rpd_exhausted = rpd_exhausted
        model_part = f" for model '{model}'" if model else ""
        if rpd_exhausted:
            detail = (
                f"The daily allowance for model '{model or 'requested model'}' is "
                f"spent on every key. It refreshes at the provider's midnight, so "
                f"re-sending now will not help. Please choose another model."
            )
        else:
            detail = (
                f"All API keys are rate-limited or cooling down{model_part}. "
                f"Retry after ~{int(retry_after_s)}s if provided."
                if retry_after_s
                else f"All API keys are rate-limited or cooling down{model_part}."
            )
        if self.model_hints:
            detail += (
                f" Models with available quota: {', '.join(self.model_hints)}."
            )
        # RPD 日耗尽：重试无意义 → 返回不可重试状态码（默认 424），让 opencode
        # 立即停止重试并提示换模型；普通瞬时限流仍用 429（可重试），并带上
        # Retry-After，让 opencode 按我们给的时间等待（opencode 会优先遵守该头）。
        status = 429
        retry_after = None
        if rpd_exhausted:
            try:
                from app.config.config import settings

                status = settings.NETWORK_ERROR_STATUS_CODE
            except Exception:
                status = 424
        elif retry_after_s and retry_after_s > 0:
            retry_after = retry_after_s
        super().__init__(
            status_code=status,
            detail=detail,
            error_code="all_keys_cooling",
            retry_after=retry_after,
        )


def setup_exception_handlers(app: FastAPI) -> None:
    """
    设置应用程序的异常处理器

    Args:
        app: FastAPI应用程序实例
    """

    @app.exception_handler(APIError)
    async def api_error_handler(request: Request, exc: APIError):
        """处理API错误"""
        logger.error(f"API Error: {exc.detail} (Code: {exc.error_code})")
        headers = {}
        retry_after = getattr(exc, "retry_after", None)
        if retry_after and retry_after > 0:
            headers["Retry-After"] = str(int(retry_after))
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.error_code, "message": exc.detail}},
            headers=headers or None,
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException):
        """处理HTTP异常"""
        logger.error(f"HTTP Exception: {exc.detail} (Status: {exc.status_code})")
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": "http_error", "message": exc.detail}},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request, exc: RequestValidationError
    ):
        """处理请求验证错误"""
        error_details = []
        for error in exc.errors():
            error_details.append(
                {"loc": error["loc"], "msg": error["msg"], "type": error["type"]}
            )

        logger.error(f"Validation Error: {error_details}")
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "validation_error",
                    "message": "Request validation failed",
                    "details": error_details,
                }
            },
        )

    @app.exception_handler(Exception)
    async def general_exception_handler(request: Request, exc: Exception):
        """处理通用异常"""
        logger.exception(f"Unhandled Exception: {str(exc)}")
        return JSONResponse(
            status_code=500,
            content=str(exc),
        )
