import asyncio
import time
import random
from itertools import cycle
from typing import Any, Dict, Optional, Union

from app.config.config import settings
from app.log.logger import get_key_manager_logger
from app.utils.helpers import redact_key_for_logging

logger = get_key_manager_logger()


def _hash_key(api_key: str) -> str:
    """API key 的 SHA256，用于与数据库存储的 key_hash 匹配（不落明文）。"""
    import hashlib

    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


class KeyManager:
    def __init__(self, api_keys: list, vertex_api_keys: list):
        self.api_keys = api_keys
        self.vertex_api_keys = vertex_api_keys
        self.key_cycle = cycle(api_keys)
        self.vertex_key_cycle = cycle(vertex_api_keys)
        self.key_cycle_lock = asyncio.Lock()
        self.vertex_key_cycle_lock = asyncio.Lock()
        self.failure_count_lock = asyncio.Lock()
        self.vertex_failure_count_lock = asyncio.Lock()
        self.key_failure_counts: Dict[str, int] = {key: 0 for key in api_keys}
        self.vertex_key_failure_counts: Dict[str, int] = {
            key: 0 for key in vertex_api_keys
        }
        # 按 (key, model) 冷却：429/503 时给该 key+model 组合设置冷却截止时间戳。
        # TPM/RPM 配额是按模型计算的，一个模型 429 不代表其他模型不能用。
        self.key_model_cooldown_until: Dict[str, float] = {}
        self.key_model_consecutive_failures: Dict[str, int] = {}
        # 今日出过错的事件记录（与冷却状态分离）：km_id -> {"last_error_time", "last_kind", "last_error_code"}
        # 用于监控页展示"今日已调用出错的模型"，即使冷却已过期也能看到。
        self.key_model_error_today: Dict[str, Dict[str, Any]] = {}
        self.error_today_day: str = ""  # 记录事件属于哪一天，跨天自动清空
        self.MAX_FAILURES = settings.MAX_FAILURES
        self.paid_key = settings.PAID_KEY

    # ---------- 按 (key, model) 冷却（429/503 指数退避） ----------

    @staticmethod
    def _km_id(key: str, model: str) -> str:
        return f"{key}::{model}"

    def _today_str(self) -> str:
        import datetime

        return datetime.datetime.now().strftime("%Y-%m-%d")

    def _roll_error_day_if_needed(self) -> None:
        """跨天时清空今日错误记录（在持有 failure_count_lock 时调用）。"""
        today = self._today_str()
        if self.error_today_day != today:
            self.error_today_day = today
            self.key_model_error_today.clear()

    def _cooldown_seconds(self, km_id: str) -> float:
        """按 (key, model) 连续失败次数计算指数退避秒数：base * 2^n，封顶 COOLDOWN_MAX_S。"""
        base = settings.COOLDOWN_BASE_S
        max_s = settings.COOLDOWN_MAX_S
        n = self.key_model_consecutive_failures.get(km_id, 0)
        # 首次失败冷却 base，之后翻倍
        delay = base * (2 ** max(n - 1, 0))
        # 加入少量抖动，避免同批 key 同步冷却/同步恢复
        jitter = random.uniform(0, base)
        return min(delay + jitter, max_s)

    async def mark_key_cooldown(
        self,
        key: str,
        model: str = "",
        seconds: Optional[float] = None,
        until: Optional[float] = None,
        kind: str = "rpm",
    ) -> float:
        """给 (key, model) 设置冷却（429/503 时调用），返回本次冷却秒数。

        - kind="rpd"  : 按天配额耗尽，长冷却（until 优先，通常为配额重置时刻）
        - kind="rpm"  : 瞬时限流/过载，seconds 通常取上游 retryDelay
        429/503 是瞬时限流或日配额，均不进入永久失败计数。
        """
        km_id = self._km_id(key, model or "_")
        async with self.failure_count_lock:
            self._roll_error_day_if_needed()
            # 记录今日出错事件（监控页用，与冷却是否过期无关）
            import datetime

            self.key_model_error_today[km_id] = {
                "last_error_time": datetime.datetime.now().strftime("%H:%M:%S"),
                "last_kind": kind,
            }
            if kind == "rpd":
                # 日耗尽：连续失败计数不再有意义，冷却时长由 until 决定
                self.key_model_consecutive_failures[km_id] = (
                    self.key_model_consecutive_failures.get(km_id, 0) + 1
                )
                if until is None:
                    until = time.monotonic() + self._rpd_cooldown_seconds()
                self.key_model_cooldown_until[km_id] = until
                cooldown = max(0.0, until - time.monotonic())
            else:
                self.key_model_consecutive_failures[km_id] = (
                    self.key_model_consecutive_failures.get(km_id, 0) + 1
                )
                # 已有更长的冷却（含 RPD 日耗尽）时不得被短冷却覆盖/缩短
                existing = self.key_model_cooldown_until.get(km_id, 0.0)
                if seconds is not None and seconds > 0:
                    cooldown = min(seconds, settings.COOLDOWN_MAX_S * 4)
                else:
                    cooldown = self._cooldown_seconds(km_id)
                new_until = time.monotonic() + cooldown
                if existing > new_until:
                    # 保留更长的冷却：不覆盖内存与数据库中的长冷却记录
                    cooldown = max(0.0, existing - time.monotonic())
                    return cooldown
                self.key_model_cooldown_until[km_id] = new_until
        logger.info(
            f"Key {redact_key_for_logging(key)} model={model or '_'} {kind.upper()} "
            f"cooldown for {cooldown:.1f}s (consecutive failures: "
            f"{self.key_model_consecutive_failures[km_id]})"
        )
        # 持久化到数据库，保证重启后冷却与统计不丢失
        await self._persist_key_model_state(
            key, model or "_", kind, cooldown, error_code=429
        )
        return cooldown

    async def _persist_key_model_state(
        self, key: str, model: str, kind: str, cooldown_s: float, error_code: int = None
    ) -> None:
        """把 (key, model) 冷却状态写入数据库。失败仅记日志，不影响主流程。"""
        try:
            import datetime
            from app.database.services import upsert_key_model_state

            await upsert_key_model_state(
                api_key=key,
                model_name=model,
                kind=kind,
                cooldown_until=datetime.datetime.now()
                + datetime.timedelta(seconds=cooldown_s),
                last_error_time=datetime.datetime.now(),
                consecutive_failures=self.key_model_consecutive_failures.get(
                    self._km_id(key, model), 0
                ),
                error_code=error_code,
            )
        except Exception as e:
            logger.warning(f"Failed to persist key model state: {e}")

    async def restore_from_db(self) -> int:
        """启动时从数据库恢复 (key, model) 冷却状态与今日出错记录。

        cooldown_until 已过期的记录只恢复"今日出错事件"（供监控页展示），
        不再设置冷却；未过期的则同时恢复冷却，避免重启立刻重新打爆配额。
        返回恢复的记录条数。
        """
        try:
            import datetime
            from app.database.services import get_all_key_model_states
        except Exception as e:  # pragma: no cover
            logger.warning(f"Cannot import db services for restore: {e}")
            return 0

        try:
            rows = await get_all_key_model_states()
        except Exception as e:
            logger.warning(f"Failed to load key model states: {e}")
            return 0

        # 建立 key_hash -> 明文 key 映射
        hash_to_key = {_hash_key(k): k for k in self.api_keys}
        now = time.monotonic()
        now_dt = datetime.datetime.now()
        restored = 0
        for row in rows:
            model = row.get("model_name") or "_"
            key_hash = row.get("key_hash")
            key = hash_to_key.get(key_hash)
            if not key:
                continue  # key 已从配置移除
            km_id = self._km_id(key, model)
            kind = row.get("kind") or "rpm"
            cooldown_until = row.get("cooldown_until")
            last_error_time = row.get("last_error_time")
            remaining = 0.0
            if cooldown_until:
                remaining = (cooldown_until - now_dt).total_seconds()
            if remaining > 0:
                self.key_model_cooldown_until[km_id] = now + remaining
                restored += 1
            self.key_model_consecutive_failures[km_id] = (
                row.get("consecutive_failures") or 0
            )
            # 今日出错事件（仅当记录日期为今天）
            if last_error_time and last_error_time.date() == now_dt.date():
                self.key_model_error_today[km_id] = {
                    "last_error_time": last_error_time.strftime("%H:%M:%S"),
                    "last_kind": kind,
                }
        self.error_today_day = now_dt.strftime("%Y-%m-%d")
        if restored:
            logger.info(
                f"Restored {restored} active (key, model) cooldown(s) from database"
            )
        return restored

    @staticmethod
    def _rpd_cooldown_seconds() -> float:
        """日配额耗尽的长冷却秒数 = 距太平洋时间（America/Los_Angeles）次日 00:00 的秒数。

        RPD（Requests Per Day）按太平洋时间午夜重置，所以冷却到下一次重置最准确；
        与固定 24h 不同，它随当前时间自适应该剩余多久。
        无法获取时区信息时回退到 RPD_COOLDOWN_HOURS。
        """
        try:
            from zoneinfo import ZoneInfo
            import datetime

            tz = ZoneInfo("America/Los_Angeles")
            now = datetime.datetime.now(tz)
            next_midnight = (now + datetime.timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            seconds = (next_midnight - now).total_seconds()
            # 至少留 60s，避免边界抖动导致立即解禁
            return max(seconds, 60.0)
        except Exception:
            return settings.RPD_COOLDOWN_HOURS * 3600.0

    def is_key_rpd_exhausted(self, key: str, model: str = "") -> bool:
        """该 (key, model) 是否处于日配额耗尽（长冷却）状态。"""
        km_id = self._km_id(key, model or "_")
        remaining = self.key_model_cooldown_until.get(km_id, 0.0) - time.monotonic()
        return remaining > settings.RPD_MIN_THRESHOLD_S

    async def mark_key_success(self, key: str, model: str = "") -> None:
        """请求成功：清零该 (key, model) 冷却与连续失败计数，并清除数据库记录。"""
        km_id = self._km_id(key, model or "_")
        had_state = False
        async with self.failure_count_lock:
            had_state = (
                self.key_model_cooldown_until.get(km_id, 0.0) > 0.0
                or km_id in self.key_model_error_today
                or self.key_model_consecutive_failures.get(km_id, 0) > 0
            )
            self.key_model_cooldown_until[km_id] = 0.0
            self.key_model_consecutive_failures[km_id] = 0
            # 成功后不再算"今日出错"，从监控列表移除
            self.key_model_error_today.pop(km_id, None)
        # 仅当之前确实有状态时才写库，避免每次成功请求都打一次 DB
        if had_state:
            try:
                from app.database.services import delete_key_model_state

                await delete_key_model_state(key, model or "_")
            except Exception as e:
                logger.warning(f"Failed to clear key model state: {e}")

    def _is_key_cooling(self, key: str, model: str = "") -> bool:
        km_id = self._km_id(key, model or "_")
        return time.monotonic() < self.key_model_cooldown_until.get(km_id, 0.0)

    def _cooldown_remaining(self, key: str, model: str = "") -> float:
        km_id = self._km_id(key, model or "_")
        return max(
            0.0, self.key_model_cooldown_until.get(km_id, 0.0) - time.monotonic()
        )

    async def earliest_cooldown_release(self, model: str = "") -> Optional[float]:
        """返回指定模型下所有冷却中 key 的最早剩余释放秒数；无冷却返回 None。

        model 为空时只统计通用冷却（model='_'）。
        """
        km_prefix_model = model or "_"
        async with self.failure_count_lock:
            remaining = [
                self._cooldown_remaining(k, km_prefix_model)
                for k in self.api_keys
                if self._is_key_cooling(k, km_prefix_model)
            ]
        return min(remaining) if remaining else None

    def get_model_cooling_state(self) -> Dict[str, Dict[str, Any]]:
        """返回今日出现过 429/503 的 (key, model) 概览（按模型聚合）。

        与冷却是否已过期无关——只要今天出过错就在列表里：
        {model: {"cooling_keys", "rpd_keys", "rpm_keys", "error_keys",
                 "earliest_release_s", "keys": [...]}}
        keys 里每项含脱敏 key、最近错误类型（rpd/rpm）、剩余冷却（可为 0）。
        """
        state: Dict[str, Dict[str, Any]] = {}
        now = time.monotonic()
        self._roll_error_day_if_needed()
        tracked = dict(self.key_model_error_today)
        for km_id, ev in tracked.items():
            if "::" not in km_id:
                continue
            key, model = km_id.split("::", 1)
            if model == "_":
                continue
            remaining = max(
                0.0, self.key_model_cooldown_until.get(km_id, 0.0) - now
            )
            kind = ev.get("last_kind", "rpm")
            entry = state.setdefault(
                model,
                {
                    "cooling_keys": 0,
                    "rpd_keys": 0,
                    "rpm_keys": 0,
                    "error_keys": 0,
                    "earliest_release_s": 0.0,
                    "keys": [],
                },
            )
            entry["error_keys"] += 1
            if remaining > 0:
                entry["cooling_keys"] += 1
                if kind == "rpd":
                    entry["rpd_keys"] += 1
                else:
                    entry["rpm_keys"] += 1
                if (
                    entry["earliest_release_s"] == 0.0
                    or remaining < entry["earliest_release_s"]
                ):
                    entry["earliest_release_s"] = remaining
            entry["keys"].append(
                {
                    "key": redact_key_for_logging(key),
                    "kind": kind,
                    "remaining_s": round(remaining, 1),
                    "last_error_time": ev.get("last_error_time", ""),
                }
            )
        # 最早恢复只统计仍在冷却的；全过期则 0
        total_keys = len(self.api_keys)
        for entry in state.values():
            if entry["cooling_keys"] == 0:
                entry["earliest_release_s"] = 0.0
            # 计算"总现状"
            entry["status"], entry["status_label"] = self._model_status(entry, total_keys)
        return state

    @staticmethod
    def _model_status(entry: Dict[str, Any], total_keys: int) -> tuple:
        """根据聚合数据给出模型总现状 (status_code, 中文标签)。

        优先级（从坏到好）：
        - rpd_exhausted : 全部 key 的日配额耗尽（RPD）
        - all_cooling   : 全部 key 都在冷却（RPM）
        - degraded      : 部分 key 冷却中
        - available     : 冷却均已过期，全部可用
        """
        cooling = entry.get("cooling_keys", 0)
        # 同日多次出错可能同一 key 重复，用 keys 去重后计数
        distinct = len({k["key"] for k in entry.get("keys", [])}) or total_keys
        pool = max(total_keys, distinct)
        if cooling == 0:
            return "available", "可用"
        if cooling >= pool and all(
            k["kind"] == "rpd" for k in entry.get("keys", []) if k["remaining_s"] > 0
        ):
            return "rpd_exhausted", "RPD全部耗尽"
        if cooling >= pool:
            return "all_cooling", "全部冷却中"
        return "degraded", f"部分冷却（{cooling}/{pool}）"

    def get_available_models_hint(
        self, exclude_model: str = "", limit: int = 3
    ) -> list:
        """从已跟踪的 (key, model) 中找出当前仍有 key 空闲（未冷却）的模型。

        一个模型只要"有任一 key 未冷却"即可推荐（包括从未出错、未跟踪的 key——
        它们没有配额问题）。用于 429/503 全冷却时给调用方"尝试换模型"的具体建议。
        """
        now = time.monotonic()
        cooling_keys_by_model: Dict[str, int] = {}
        for km_id, until in self.key_model_cooldown_until.items():
            if "::" not in km_id:
                continue
            _, model = km_id.split("::", 1)
            if model == "_" or model == exclude_model:
                continue
            if now < until:
                cooling_keys_by_model[model] = (
                    cooling_keys_by_model.get(model, 0) + 1
                )
        # 任一模型冷却 key 数 < key 池总数 => 至少有一个 key 空闲
        candidates = [
            m for m, c in cooling_keys_by_model.items() if c < len(self.api_keys)
        ]
        return candidates[:limit]

    def all_models_exhausted(self, exclude_model: str = "") -> bool:
        """判断是否所有已跟踪模型的全部 key 都在冷却中（含 RPD 长冷却）。

        用于"全池耗尽"判定：此时进入始终提示换模型阶段。
        """
        now = time.monotonic()
        cooling_by_model: Dict[str, int] = {}
        for km_id, until in self.key_model_cooldown_until.items():
            if "::" not in km_id:
                continue
            _, model = km_id.split("::", 1)
            if model == "_" or model == exclude_model:
                continue
            if now < until:
                cooling_by_model[model] = cooling_by_model.get(model, 0) + 1
        # 没有任何已跟踪模型 => 不算全耗尽（可能是首次使用）
        if not cooling_by_model:
            return False
        # 所有已跟踪模型的冷却 key 数都 >= key 池总数 => 全耗尽
        return all(c >= len(self.api_keys) for c in cooling_by_model.values())

    async def get_paid_key(self) -> str:
        return self.paid_key

    async def get_next_key(self) -> str:
        """获取下一个API key"""
        async with self.key_cycle_lock:
            return next(self.key_cycle)

    async def get_next_vertex_key(self) -> str:
        """获取下一个 Vertex Express API key"""
        async with self.vertex_key_cycle_lock:
            return next(self.vertex_key_cycle)

    async def is_key_valid(self, key: str) -> bool:
        """检查key是否有效"""
        async with self.failure_count_lock:
            return self.key_failure_counts[key] < self.MAX_FAILURES

    async def is_vertex_key_valid(self, key: str) -> bool:
        """检查 Vertex key 是否有效"""
        async with self.vertex_failure_count_lock:
            return self.vertex_key_failure_counts[key] < self.MAX_FAILURES

    async def reset_failure_counts(self):
        """重置所有key的失败计数"""
        async with self.failure_count_lock:
            for key in self.key_failure_counts:
                self.key_failure_counts[key] = 0

    async def reset_vertex_failure_counts(self):
        """重置所有 Vertex key 的失败计数"""
        async with self.vertex_failure_count_lock:
            for key in self.vertex_key_failure_counts:
                self.vertex_key_failure_counts[key] = 0

    async def reset_key_failure_count(self, key: str) -> bool:
        """重置指定key的失败计数，并清除其全部 (key, model) 冷却与数据库记录。"""
        async with self.failure_count_lock:
            if key not in self.key_failure_counts:
                logger.warning(
                    f"Attempt to reset failure count for non-existent key: {key}"
                )
                return False
            self.key_failure_counts[key] = 0
            # 清除该 key 的所有模型冷却与今日出错记录（管理员重置 = 彻底恢复）
            for km_id in list(self.key_model_cooldown_until.keys()):
                if km_id.startswith(f"{key}::"):
                    self.key_model_cooldown_until[km_id] = 0.0
                    self.key_model_consecutive_failures[km_id] = 0
                    self.key_model_error_today.pop(km_id, None)
            logger.info(f"Reset failure count for key: {redact_key_for_logging(key)}")
        # 同步清理数据库
        try:
            from app.database.services import delete_key_model_state

            for model in list(self.key_model_cooldown_until.keys()):
                if model.startswith(f"{key}::"):
                    await delete_key_model_state(key, model.split("::", 1)[1])
        except Exception as e:
            logger.warning(f"Failed to clear key model states on reset: {e}")
        return True

    async def reset_permanent_failure_count(self, key: str) -> None:
        """仅清零永久失败计数（请求成功时调用，不动 (key, model) 冷却）。

        与 reset_key_failure_count（管理员手动重置，清除全部冷却）区分，避免
        某个模型成功时误清其他模型的冷却，破坏模型隔离。
        """
        async with self.failure_count_lock:
            if key in self.key_failure_counts:
                self.key_failure_counts[key] = 0

    async def reset_vertex_key_failure_count(self, key: str) -> bool:
        """重置指定 Vertex key 的失败计数"""
        async with self.vertex_failure_count_lock:
            if key in self.vertex_key_failure_counts:
                self.vertex_key_failure_counts[key] = 0
                logger.info(f"Reset failure count for Vertex key: {redact_key_for_logging(key)}")
                return True
            logger.warning(
                f"Attempt to reset failure count for non-existent Vertex key: {key}"
            )
            return False

    async def get_next_working_key(self, model: str = "") -> Optional[str]:
        """获取指定模型下一可用 key：跳过失效（失败数超限）与冷却中的 (key, model)。

        若所有 key 都在冷却，返回 None，由调用方决定等待最早到期或直接失败。
        """
        async with self.key_cycle_lock:
            initial_key = next(self.key_cycle)
        current_key = initial_key

        while True:
            if (
                await self.is_key_valid(current_key)
                and not self._is_key_cooling(current_key, model)
            ):
                return current_key

            async with self.key_cycle_lock:
                current_key = next(self.key_cycle)
            if current_key == initial_key:
                # 轮完一圈：全都在失效/冷却中
                return None

    async def get_next_working_vertex_key(self) -> str:
        """获取下一可用的 Vertex Express API key"""
        initial_key = await self.get_next_vertex_key()
        current_key = initial_key

        while True:
            if await self.is_vertex_key_valid(current_key):
                return current_key

            current_key = await self.get_next_vertex_key()
            if current_key == initial_key:
                return current_key

    async def handle_api_failure(self, api_key: str, retries: int) -> str:
        """处理API调用失败"""
        async with self.failure_count_lock:
            self.key_failure_counts[api_key] += 1
            if self.key_failure_counts[api_key] >= self.MAX_FAILURES:
                logger.warning(
                    f"API key {redact_key_for_logging(api_key)} has failed {self.MAX_FAILURES} times"
                )
        if retries < settings.MAX_RETRIES:
            return await self.get_next_working_key()
        else:
            return ""

    async def handle_vertex_api_failure(self, api_key: str, retries: int) -> str:
        """处理 Vertex Express API 调用失败"""
        async with self.vertex_failure_count_lock:
            self.vertex_key_failure_counts[api_key] += 1
            if self.vertex_key_failure_counts[api_key] >= self.MAX_FAILURES:
                logger.warning(
                    f"Vertex Express API key {redact_key_for_logging(api_key)} has failed {self.MAX_FAILURES} times"
                )

    def get_fail_count(self, key: str) -> int:
        """获取指定密钥的失败次数"""
        return self.key_failure_counts.get(key, 0)

    def get_vertex_fail_count(self, key: str) -> int:
        """获取指定 Vertex 密钥的失败次数"""
        return self.vertex_key_failure_counts.get(key, 0)

    async def get_all_keys_with_fail_count(self) -> dict:
        """获取所有API key及其失败次数"""
        all_keys = {}
        async with self.failure_count_lock:
            for key in self.api_keys:
                all_keys[key] = self.key_failure_counts.get(key, 0)
        
        valid_keys = {k: v for k, v in all_keys.items() if v < self.MAX_FAILURES}
        invalid_keys = {k: v for k, v in all_keys.items() if v >= self.MAX_FAILURES}
        
        return {"valid_keys": valid_keys, "invalid_keys": invalid_keys, "all_keys": all_keys}

    async def get_keys_by_status(self) -> dict:
        """获取分类后的API key列表，包括失败次数"""
        valid_keys = {}
        invalid_keys = {}

        async with self.failure_count_lock:
            for key in self.api_keys:
                fail_count = self.key_failure_counts[key]
                if fail_count < self.MAX_FAILURES:
                    valid_keys[key] = fail_count
                else:
                    invalid_keys[key] = fail_count

        return {"valid_keys": valid_keys, "invalid_keys": invalid_keys}

    async def get_vertex_keys_by_status(self) -> dict:
        """获取分类后的 Vertex Express API key 列表，包括失败次数"""
        valid_keys = {}
        invalid_keys = {}

        async with self.vertex_failure_count_lock:
            for key in self.vertex_api_keys:
                fail_count = self.vertex_key_failure_counts[key]
                if fail_count < self.MAX_FAILURES:
                    valid_keys[key] = fail_count
                else:
                    invalid_keys[key] = fail_count
        return {"valid_keys": valid_keys, "invalid_keys": invalid_keys}

    async def get_first_valid_key(self) -> str:
        """获取第一个有效的API key"""
        async with self.failure_count_lock:
            for key in self.key_failure_counts:
                if self.key_failure_counts[key] < self.MAX_FAILURES:
                    return key
        if self.api_keys:
            return self.api_keys[0]
        if not self.api_keys:
            logger.warning("API key list is empty, cannot get first valid key.")
            return ""
        return self.api_keys[0]

    async def get_random_valid_key(self) -> str:
        """获取随机的有效API key"""
        valid_keys = []
        async with self.failure_count_lock:
            for key in self.key_failure_counts:
                if self.key_failure_counts[key] < self.MAX_FAILURES:
                    valid_keys.append(key)
        
        if valid_keys:
            return random.choice(valid_keys)
        
        # 如果没有有效的key，返回第一个key作为fallback
        if self.api_keys:
            logger.warning("No valid keys available, returning first key as fallback.")
            return self.api_keys[0]
        
        logger.warning("API key list is empty, cannot get random valid key.")
        return ""


_singleton_instance = None
_singleton_lock = asyncio.Lock()
_preserved_failure_counts: Union[Dict[str, int], None] = None
_preserved_vertex_failure_counts: Union[Dict[str, int], None] = None
_preserved_old_api_keys_for_reset: Union[list, None] = None
_preserved_vertex_old_api_keys_for_reset: Union[list, None] = None
_preserved_next_key_in_cycle: Union[str, None] = None
_preserved_vertex_next_key_in_cycle: Union[str, None] = None


async def get_key_manager_instance(
    api_keys: list = None, vertex_api_keys: list = None
) -> KeyManager:
    """
    获取 KeyManager 单例实例。

    如果尚未创建实例，将使用提供的 api_keys,vertex_api_keys 初始化 KeyManager。
    如果已创建实例，则忽略 api_keys 参数，返回现有单例。
    如果在重置后调用，会尝试恢复之前的状态（失败计数、循环位置）。
    """
    global _singleton_instance, _preserved_failure_counts, _preserved_vertex_failure_counts, _preserved_old_api_keys_for_reset, _preserved_vertex_old_api_keys_for_reset, _preserved_next_key_in_cycle, _preserved_vertex_next_key_in_cycle

    async with _singleton_lock:
        if _singleton_instance is None:
            if api_keys is None:
                raise ValueError(
                    "API keys are required to initialize or re-initialize the KeyManager instance."
                )
            if vertex_api_keys is None:
                raise ValueError(
                    "Vertex Express API keys are required to initialize or re-initialize the KeyManager instance."
                )

            if not api_keys:
                logger.warning(
                    "Initializing KeyManager with an empty list of API keys."
                )
            if not vertex_api_keys:
                logger.warning(
                    "Initializing KeyManager with an empty list of Vertex Express API keys."
                )

            _singleton_instance = KeyManager(api_keys, vertex_api_keys)
            logger.info(
                f"KeyManager instance created/re-created with {len(api_keys)} API keys and {len(vertex_api_keys)} Vertex Express API keys."
            )

            # 0. 从数据库恢复 (key, model) 冷却状态（跨重启保留限流统计）
            try:
                await _singleton_instance.restore_from_db()
            except Exception as e:
                logger.warning(f"Failed to restore key model states from DB: {e}")

            # 1. 恢复失败计数
            if _preserved_failure_counts:
                current_failure_counts = {
                    key: 0 for key in _singleton_instance.api_keys
                }
                for key, count in _preserved_failure_counts.items():
                    if key in current_failure_counts:
                        current_failure_counts[key] = count
                _singleton_instance.key_failure_counts = current_failure_counts
                logger.info("Inherited failure counts for applicable keys.")
            _preserved_failure_counts = None

            if _preserved_vertex_failure_counts:
                current_vertex_failure_counts = {
                    key: 0 for key in _singleton_instance.vertex_api_keys
                }
                for key, count in _preserved_vertex_failure_counts.items():
                    if key in current_vertex_failure_counts:
                        current_vertex_failure_counts[key] = count
                _singleton_instance.vertex_key_failure_counts = (
                    current_vertex_failure_counts
                )
                logger.info("Inherited failure counts for applicable Vertex keys.")
            _preserved_vertex_failure_counts = None

            # 2. 调整 key_cycle 的起始点
            start_key_for_new_cycle = None
            if (
                _preserved_old_api_keys_for_reset
                and _preserved_next_key_in_cycle
                and _singleton_instance.api_keys
            ):
                try:
                    start_idx_in_old = _preserved_old_api_keys_for_reset.index(
                        _preserved_next_key_in_cycle
                    )

                    for i in range(len(_preserved_old_api_keys_for_reset)):
                        current_old_key_idx = (start_idx_in_old + i) % len(
                            _preserved_old_api_keys_for_reset
                        )
                        key_candidate = _preserved_old_api_keys_for_reset[
                            current_old_key_idx
                        ]
                        if key_candidate in _singleton_instance.api_keys:
                            start_key_for_new_cycle = key_candidate
                            break
                except ValueError:
                    logger.warning(
                        f"Preserved next key '{_preserved_next_key_in_cycle}' not found in preserved old API keys. "
                        "New cycle will start from the beginning of the new list."
                    )
                except Exception as e:
                    logger.error(
                        f"Error determining start key for new cycle from preserved state: {e}. "
                        "New cycle will start from the beginning."
                    )

            if start_key_for_new_cycle and _singleton_instance.api_keys:
                try:
                    target_idx = _singleton_instance.api_keys.index(
                        start_key_for_new_cycle
                    )
                    for _ in range(target_idx):
                        next(_singleton_instance.key_cycle)
                    logger.info(
                        f"Key cycle in new instance advanced. Next call to get_next_key() will yield: {start_key_for_new_cycle}"
                    )
                except ValueError:
                    logger.warning(
                        f"Determined start key '{start_key_for_new_cycle}' not found in new API keys during cycle advancement. "
                        "New cycle will start from the beginning."
                    )
                except StopIteration:
                    logger.error(
                        "StopIteration while advancing key cycle, implies empty new API key list previously missed."
                    )
                except Exception as e:
                    logger.error(
                        f"Error advancing new key cycle: {e}. Cycle will start from beginning."
                    )
            else:
                if _singleton_instance.api_keys:
                    logger.info(
                        "New key cycle will start from the beginning of the new API key list (no specific start key determined or needed)."
                    )
                else:
                    logger.info(
                        "New key cycle not applicable as the new API key list is empty."
                    )

            # 清理所有保存的状态
            _preserved_old_api_keys_for_reset = None
            _preserved_next_key_in_cycle = None

            # 3. 调整 vertex_key_cycle 的起始点
            start_key_for_new_vertex_cycle = None
            if (
                _preserved_vertex_old_api_keys_for_reset
                and _preserved_vertex_next_key_in_cycle
                and _singleton_instance.vertex_api_keys
            ):
                try:
                    start_idx_in_old = _preserved_vertex_old_api_keys_for_reset.index(
                        _preserved_vertex_next_key_in_cycle
                    )

                    for i in range(len(_preserved_vertex_old_api_keys_for_reset)):
                        current_old_key_idx = (start_idx_in_old + i) % len(
                            _preserved_vertex_old_api_keys_for_reset
                        )
                        key_candidate = _preserved_vertex_old_api_keys_for_reset[
                            current_old_key_idx
                        ]
                        if key_candidate in _singleton_instance.vertex_api_keys:
                            start_key_for_new_vertex_cycle = key_candidate
                            break
                except ValueError:
                    logger.warning(
                        f"Preserved next key '{_preserved_vertex_next_key_in_cycle}' not found in preserved old Vertex Express API keys. "
                        "New cycle will start from the beginning of the new list."
                    )
                except Exception as e:
                    logger.error(
                        f"Error determining start key for new Vertex key cycle from preserved state: {e}. "
                        "New cycle will start from the beginning."
                    )

            if start_key_for_new_vertex_cycle and _singleton_instance.vertex_api_keys:
                try:
                    target_idx = _singleton_instance.vertex_api_keys.index(
                        start_key_for_new_vertex_cycle
                    )
                    for _ in range(target_idx):
                        next(_singleton_instance.vertex_key_cycle)
                    logger.info(
                        f"Vertex key cycle in new instance advanced. Next call to get_next_vertex_key() will yield: {start_key_for_new_vertex_cycle}"
                    )
                except ValueError:
                    logger.warning(
                        f"Determined start key '{start_key_for_new_vertex_cycle}' not found in new Vertex Express API keys during cycle advancement. "
                        "New cycle will start from the beginning."
                    )
                except StopIteration:
                    logger.error(
                        "StopIteration while advancing Vertex key cycle, implies empty new Vertex Express API key list previously missed."
                    )
                except Exception as e:
                    logger.error(
                        f"Error advancing new Vertex key cycle: {e}. Cycle will start from beginning."
                    )
            else:
                if _singleton_instance.vertex_api_keys:
                    logger.info(
                        "New Vertex key cycle will start from the beginning of the new Vertex Express API key list (no specific start key determined or needed)."
                    )
                else:
                    logger.info(
                        "New Vertex key cycle not applicable as the new Vertex Express API key list is empty."
                    )

            # 清理所有保存的状态
            _preserved_vertex_old_api_keys_for_reset = None
            _preserved_vertex_next_key_in_cycle = None

        return _singleton_instance


async def reset_key_manager_instance():
    """
    重置 KeyManager 单例实例。
    将保存当前实例的状态（失败计数、旧 API keys、下一个 key 提示）
    以供下一次 get_key_manager_instance 调用时恢复。
    """
    global _singleton_instance, _preserved_failure_counts, _preserved_vertex_failure_counts, _preserved_old_api_keys_for_reset, _preserved_vertex_old_api_keys_for_reset, _preserved_next_key_in_cycle, _preserved_vertex_next_key_in_cycle
    async with _singleton_lock:
        if _singleton_instance:
            # 1. 保存失败计数
            _preserved_failure_counts = _singleton_instance.key_failure_counts.copy()
            _preserved_vertex_failure_counts = (
                _singleton_instance.vertex_key_failure_counts.copy()
            )

            # 2. 保存旧的 API keys 列表
            _preserved_old_api_keys_for_reset = _singleton_instance.api_keys.copy()
            _preserved_vertex_old_api_keys_for_reset = (
                _singleton_instance.vertex_api_keys.copy()
            )

            # 3. 保存 key_cycle 的下一个 key 提示
            try:
                if _singleton_instance.api_keys:
                    _preserved_next_key_in_cycle = (
                        await _singleton_instance.get_next_key()
                    )
                else:
                    _preserved_next_key_in_cycle = None
            except StopIteration:
                logger.warning(
                    "Could not preserve next key hint: key cycle was empty or exhausted in old instance."
                )
                _preserved_next_key_in_cycle = None
            except Exception as e:
                logger.error(f"Error preserving next key hint during reset: {e}")
                _preserved_next_key_in_cycle = None

            # 4. 保存 vertex_key_cycle 的下一个 key 提示
            try:
                if _singleton_instance.vertex_api_keys:
                    _preserved_vertex_next_key_in_cycle = (
                        await _singleton_instance.get_next_vertex_key()
                    )
                else:
                    _preserved_vertex_next_key_in_cycle = None
            except StopIteration:
                logger.warning(
                    "Could not preserve next key hint: Vertex key cycle was empty or exhausted in old instance."
                )
                _preserved_vertex_next_key_in_cycle = None
            except Exception as e:
                logger.error(f"Error preserving next key hint during reset: {e}")
                _preserved_vertex_next_key_in_cycle = None

            _singleton_instance = None
            logger.info(
                "KeyManager instance has been reset. State (failure counts, old keys, next key hint) preserved for next instantiation."
            )
        else:
            logger.info(
                "KeyManager instance was not set (or already reset), no reset action performed."
            )
