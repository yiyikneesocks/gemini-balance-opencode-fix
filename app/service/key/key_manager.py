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
        # 成功调用次数（按太平洋日统计）："<pacific_day>::<key>::<model>" -> count
        # 用于监控页展示每个 (key, model) 当天成功了多少次。
        self.key_model_success_count: Dict[str, int] = {}
        # 负载均衡：全局选中序号 + 每个 key 最近一次被选中的序号（用于同次数轮转）
        self._pick_seq: int = 0
        self._key_last_picked_seq: Dict[str, int] = {}
        # 每个模型上一次选中的 key（避免连续命中同一把，规避单 key RPM 突发）
        self._last_picked_key: Dict[str, str] = {}
        # 模型级过载（503 high demand）计数：model -> 连续过载次数
        # 过载是模型全局现象，换 key 无效，因此单独统计并在达到阈值时中断。
        self.model_overload_counts: Dict[str, int] = {}
        # 模型级过载冷却截止时间（monotonic）：model -> until
        self.model_overload_until: Dict[str, float] = {}
        # 在途请求计数："<key>::<model>" -> 正在进行中的请求数。
        # 仅用于分发时的软性均衡（优先挑在途少的 key），**不会阻塞**请求：
        # 只要 key 空闲（未冷却、未失效）就立即分发，不等前一次调用结束。
        self.key_model_inflight: Dict[str, int] = {}
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
        error_log: str = "",
        error_code: Optional[int] = None,
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

            event = self.key_model_error_today.get(km_id, {})
            event.update(
                {
                    "last_error_time": datetime.datetime.now().strftime("%H:%M:%S"),
                    "last_kind": kind,
                    "last_error_log": (error_log or "")[:8000],
                    "last_error_code": error_code,
                }
            )
            event["error_count"] = event.get("error_count", 0) + 1
            self.key_model_error_today[km_id] = event
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
                    # 上游给的 retryDelay 是权威值，尽量遵从；仅设一个宽松上限
                    # （RPD_COOLDOWN_HOURS）防止异常的超大值把 key 长期锁死。
                    max_by_upstream = settings.RPD_COOLDOWN_HOURS * 3600.0
                    cooldown = min(seconds, max_by_upstream)
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
            key,
            model or "_",
            kind,
            cooldown,
            error_code=error_code or 429,
            error_log=error_log,
        )
        return cooldown

    async def note_error(
        self,
        key: str,
        model: str = "",
        kind: str = "network",
        error_log: str = "",
        error_code: Optional[int] = None,
    ) -> None:
        """记录一次"不惩罚 key"的错误（如网络不可达 / 客户端 400）。

        不设置任何冷却、不改失败计数，仅用于监控页展示"今日出错"。
        """
        import datetime

        km_id = self._km_id(key, model or "_")
        async with self.failure_count_lock:
            self._roll_error_day_if_needed()
            event = self.key_model_error_today.get(km_id, {})
            event.update(
                {
                    "last_error_time": datetime.datetime.now().strftime("%H:%M:%S"),
                    "last_kind": kind,
                    "last_error_log": (error_log or "")[:8000],
                    "last_error_code": error_code,
                }
            )
            event["error_count"] = event.get("error_count", 0) + 1
            self.key_model_error_today[km_id] = event
        # 仅记录事件与计数，不写冷却
        await self._persist_key_model_state(
            key,
            model or "_",
            kind,
            0.0,
            error_code=error_code,
            error_log=error_log,
        )

    async def _persist_key_model_state(
        self,
        key: str,
        model: str,
        kind: str,
        cooldown_s: float,
        error_code: int = None,
        error_log: str = "",
    ) -> None:
        """把 (key, model) 状态写入数据库。失败仅记日志，不影响主流程。

        cooldown_s <= 0 表示"仅记录事件，不设冷却"（如网络错误），
        此时不传 cooldown_until，避免覆盖已有的更长冷却。
        """
        try:
            import datetime
            from app.database.services import upsert_key_model_state

            km_id = self._km_id(key, model)
            event = self.key_model_error_today.get(km_id, {})
            cooldown_until = (
                datetime.datetime.now() + datetime.timedelta(seconds=cooldown_s)
                if cooldown_s and cooldown_s > 0
                else None
            )
            await upsert_key_model_state(
                api_key=key,
                model_name=model,
                kind=kind,
                cooldown_until=cooldown_until,
                last_error_time=datetime.datetime.now(),
                consecutive_failures=self.key_model_consecutive_failures.get(
                    km_id, 0
                ),
                error_code=error_code,
                last_error_log=(error_log or "")[:8000],
                error_count=event.get("error_count", 0),
                stat_day=self._pacific_day_str(),
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
                    "last_error_log": row.get("last_error_log") or "",
                    "last_error_code": row.get("error_code"),
                    "error_count": row.get("error_count") or 0,
                }
            # 今日成功计数（按太平洋日；跨日则忽略）
            stat_day = row.get("stat_day")
            if stat_day and stat_day == self._pacific_day_str():
                self.key_model_success_count[f"{stat_day}::{km_id}"] = (
                    row.get("success_count") or 0
                )
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

    @staticmethod
    def _pacific_day_str() -> str:
        """当前太平洋时间（America/Los_Angeles）的日期字符串，作为"一天"的边界。

        配额按太平洋时间重置，因此"今日统计"也按太平洋日切分。
        """
        try:
            from zoneinfo import ZoneInfo
            import datetime

            return datetime.datetime.now(ZoneInfo("America/Los_Angeles")).strftime(
                "%Y-%m-%d"
            )
        except Exception:
            import datetime

            return datetime.datetime.now().strftime("%Y-%m-%d")

    def get_success_count(self, key: str, model: str) -> int:
        """该 (key, model) 在太平洋日内的成功调用次数。"""
        return self.key_model_success_count.get(
            f"{self._pacific_day_str()}::{self._km_id(key, model)}", 0
        )

    # ---------- 在途请求计数（并发公平性） ----------

    def mark_key_inflight(self, key: str, model: str = "") -> None:
        """标记该 (key, model) 有一个请求正在处理中。"""
        km_id = self._km_id(key, model or "_")
        self.key_model_inflight[km_id] = self.key_model_inflight.get(km_id, 0) + 1

    def release_key_inflight(self, key: str, model: str = "") -> None:
        """释放该 (key, model) 的在途计数（请求结束，无论成败）。"""
        km_id = self._km_id(key, model or "_")
        cur = self.key_model_inflight.get(km_id, 0)
        if cur <= 1:
            self.key_model_inflight.pop(km_id, None)
        else:
            self.key_model_inflight[km_id] = cur - 1

    def get_key_inflight(self, key: str, model: str = "") -> int:
        return self.key_model_inflight.get(self._km_id(key, model or "_"), 0)

    # ---------- 模型级过载（503 high demand） ----------

    def record_model_overload(
        self, model: str, key: str = "", error_log: str = "", error_code: int = 503
    ) -> int:
        """记录一次模型过载，返回该模型当前连续过载次数。

        过载是模型全局问题：同时给该模型设置一个短冷却（避免立即再打），
        并累计连续过载次数，供上层判断是否直接中断、建议换模型。
        """
        import datetime

        model = model or "_"
        self.model_overload_counts[model] = (
            self.model_overload_counts.get(model, 0) + 1
        )
        self.model_overload_until[model] = (
            time.monotonic() + settings.OVERLOAD_MODEL_COOLDOWN_S
        )
        count = self.model_overload_counts[model]
        if key:
            km_id = self._km_id(key, model)
            self._roll_error_day_if_needed()
            event = self.key_model_error_today.get(km_id, {})
            event.update(
                {
                    "last_error_time": datetime.datetime.now().strftime("%H:%M:%S"),
                    "last_kind": "overload",
                    "last_error_log": (error_log or "")[:8000],
                    "last_error_code": error_code,
                }
            )
            event["error_count"] = event.get("error_count", 0) + 1
            self.key_model_error_today[km_id] = event
        logger.info(
            f"Model {model} overloaded (503 high demand); consecutive count={count}"
        )
        return count

    def clear_model_overload(self, model: str) -> None:
        """请求成功：清零该模型的连续过载计数。"""
        model = model or "_"
        self.model_overload_counts[model] = 0
        self.model_overload_until[model] = 0.0

    def is_model_overloaded_for_giveup(self, model: str) -> bool:
        """连续过载是否已达"直接中断、建议换模型"的阈值。"""
        return (
            self.model_overload_counts.get(model or "_", 0)
            >= settings.OVERLOAD_KEYS_BEFORE_GIVEUP
        )

    def _is_model_overload_cooling(self, model: str) -> bool:
        return time.monotonic() < self.model_overload_until.get(model or "_", 0.0)


    def pick_rpd_probe_key(self, model: str, exclude: Optional[set] = None) -> Optional[str]:
        """全池 RPD 耗尽时，挑一个最佳"探测"key（其 RPD 冷却剩余最少/最早到期）。

        用于「即使 RPD 耗尽也再试一两轮」——因为配额可能已提前重置，
        或本地判断有误。exclude 用于避免连续探测同一把 key。
        """
        exclude = exclude or set()
        best_key: Optional[str] = None
        best_remaining: Optional[float] = None
        for key in self.api_keys:
            if key in exclude:
                continue
            km_id = self._km_id(key, model or "_")
            remaining = self.key_model_cooldown_until.get(km_id, 0.0) - time.monotonic()
            if remaining <= 0:
                continue  # 未冷却的会被 get_next_working_key 正常处理
            if best_remaining is None or remaining < best_remaining:
                best_remaining = remaining
                best_key = key
        return best_key

    def is_key_rpd_exhausted(self, key: str, model: str = "") -> bool:
        """该 (key, model) 是否处于日配额耗尽（长冷却）状态。"""
        km_id = self._km_id(key, model or "_")
        remaining = self.key_model_cooldown_until.get(km_id, 0.0) - time.monotonic()
        return remaining > settings.RPD_MIN_THRESHOLD_S

    def all_keys_rpd_exhausted(self, model: str = "") -> bool:
        """该模型下是否**所有** key 都处于 RPD 日耗尽状态。

        仅当每一把 key 都被判定为 RPD 长冷却时才返回 True。只要有一把 key
        不是 RPD（例如只是 RPM 短冷却，或根本未冷却），就返回 False——
        此时应作为"稍后重试"处理，而不是误报"RPD 全部耗尽"。
        """
        if not self.api_keys:
            return False
        return all(self.is_key_rpd_exhausted(key, model) for key in self.api_keys)

    async def mark_key_success(self, key: str, model: str = "") -> None:
        """请求成功：累计今日成功次数，清零该 (key, model) 冷却与连续失败计数。

        注意：不清除今日的出错计数（那是按太平洋日累计的），只清冷却。
        """
        km_id = self._km_id(key, model or "_")
        had_cooldown = False
        async with self.failure_count_lock:
            had_cooldown = self.key_model_cooldown_until.get(km_id, 0.0) > 0.0
            self.key_model_cooldown_until[km_id] = 0.0
            self.key_model_consecutive_failures[km_id] = 0
            # 保留 key_model_error_today 的 error_count（今日累计），仅清掉冷却标记
            if km_id in self.key_model_error_today:
                self.key_model_error_today[km_id]["cooling"] = False
            # 累计太平洋日内的成功调用次数
            day = self._pacific_day_str()
            stat_key = f"{day}::{km_id}"
            self.key_model_success_count[stat_key] = (
                self.key_model_success_count.get(stat_key, 0) + 1
            )
            self._roll_success_day_if_needed(day)
        # 清冷却 + 累加成功数（保留今日错误计数）
        try:
            from app.database.services import increment_key_model_success

            await increment_key_model_success(key, model or "_", self._pacific_day_str())
        except Exception as e:
            logger.warning(f"Failed to persist success count: {e}")
        if had_cooldown:
            try:
                from app.database.services import clear_key_model_cooldown

                await clear_key_model_cooldown(key, model or "_")
            except Exception as e:
                logger.warning(f"Failed to clear key model cooldown: {e}")

    def _roll_success_day_if_needed(self, today: Optional[str] = None) -> None:
        """跨太平洋日时清空上一日的成功计数（内存）。在持有锁时调用。"""
        today = today or self._pacific_day_str()
        stale = [k for k in self.key_model_success_count if not k.startswith(f"{today}::")]
        for k in stale:
            self.key_model_success_count.pop(k, None)

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
                    "last_error_log": ev.get("last_error_log", ""),
                    "last_error_code": ev.get("last_error_code"),
                    "error_count": ev.get("error_count", 0),
                    "success_count": self.get_success_count(key, model),
                    "inflight": self.get_key_inflight(key, model),
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

        分发规则（"一轮内不重复，轮完才复用"）：
        1. 先取所有可用 key（有效且未冷却）；
        2. 优先分发给**当前没有在途请求**的 key——保证并发请求各自拿到不同的 key，
           不会连续把同一把 key 分给两个请求；
        3. 只有当所有可用 key 都已在途（这一轮已经轮完）时，才复用某个在途 key
           （此时按今日成功次数最少 + 轮转公平来选）；
        4. 冷却中/失效的 key 一律跳过；若全都不可用则返回 None。

        真正的"限流规避"交给 (key, model) 冷却；本函数只负责把请求尽量均匀地
        分派到不同 key，避免单 key 被连续刷爆。
        """
        # 收集所有当前可用（有效且未冷却）的 key
        available: list = []
        for key in self.api_keys:
            if self.key_failure_counts.get(key, 0) >= self.MAX_FAILURES:
                continue
            if self._is_key_cooling(key, model):
                continue
            available.append(key)

        if not available:
            return None

        # 优先选择"当前没有在途请求"的 key：一个 key 这一轮只分一次
        free = [k for k in available if self.get_key_inflight(k, model) == 0]
        pool = free if free else available

        picked = self._pick_least_used_key(pool, model)
        self._pick_seq += 1
        self._key_last_picked_seq[picked] = self._pick_seq
        self._last_picked_key[model or "_"] = picked
        logger.debug(
            f"Dispatch model={model or '_'}: available={len(available)} "
            f"free={len(free)} -> {redact_key_for_logging(picked)}"
        )
        return picked

    def _pick_least_used_key(self, available: list, model: str) -> str:
        """在候选 key 中按 (在途请求数, 今日成功次数, 最近使用序号) 选最优。

        - **首先比在途请求数**：优先选当前没有其他请求占用的 key，避免多对话
          并发时全部挤在同一把 key 上、导致别的对话长时间拿不到 key（饿死）。
        - 其次比今日成功次数，越少越优先（摊平衡量）；
        - 最后比"最近最少被选中"（recency），保证轮转公平。
        """
        best_key = None
        best_metric = None
        for k in available:
            metric = (
                self.get_key_inflight(k, model),
                self.get_success_count(k, model),
                self._key_last_picked_seq.get(k, 0),
            )
            if best_metric is None or metric < best_metric:
                best_metric = metric
                best_key = k
        return best_key or available[0]

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
