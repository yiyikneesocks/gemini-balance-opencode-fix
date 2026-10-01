# ARCHITECTURE-dispatch.md — 分发与请求两侧的职责边界

> **状态：当前设计（2026-09-30 定稿）**
> 本文是 gemini-balance（opencode fork）分发体系的**权威说明**。
> 改动任何一侧之前，先读这份文档确认没有越界。

---

## 0. 一句话总结

**分发侧**（`KeyManager` + 错误分类/重试）：只管「下一把发哪把 key、冷却怎么触发、
错误怎么处理、要不要重试」。它**不知道、也不关心**是谁领的 key。

**请求侧**（FastAPI 依赖 + service 重试循环里的领 key 调用）：只管「按顺序来领 key」。
它**不知道、也不关心**领到的是哪一把——领到就用，领不到（全不可用）才等。

两侧唯一的交互点是一个纯函数式的接口：
`get_next_working_key(model) -> key | None`。
没有共享的请求状态，没有按 key 的锁，没有名额池。

```
┌────────────────────────┐          ┌──────────────────────────────┐
│ 请求侧（N 个并发请求）  │          │ 分发侧（KeyManager，全局一份）│
│                        │          │                              │
│ 请求A ─┐               │  领 key  │  可用集合 = 未失效 ∧ 未冷却    │
│ 请求B ─┼─ 按顺序调用 ──┼─────────▶│  ① 排除最近发过的 KEY_REPEAT_GAP│
│ 请求C ─┘  get_next_   │◀─ key ───┤  ② 其中选今日成功次数最少      │
│            working_key│          │  ③ 同次数按 recency 轮转      │
│ 领到→立刻调用上游      │          │  ④ 冷却/失效一律跳过          │
│ 领不到(全不可用)→等待  │          │  维护：冷却/过载/统计/报错     │
└────────────────────────┘          └──────────────────────────────┘
```

---

## 1. 分发侧：只负责三件事

分发侧的实现全部在 `app/service/key/key_manager.py` 的 `KeyManager`（单例），
加上两个纯模块 `app/core/error_classifier.py` 与 `app/core/quota_parser.py`。

### 1.1 决定「下一把发哪把 key」

`KeyManager.get_next_working_key(model)` — `key_manager.py:742`

对一个模型的 key 池，按如下**优先级顺序**挑出下一把：

| 优先级 | 规则 | 说明 |
|-------|------|------|
| 0 | 可用集合 | `未失效（永久失败数 < MAX_FAILURES）∧ 该 (key, model) 未冷却`；集合为空 → 返回 `None` |
| 1 | **重复间隔**（最高优先级） | 排除「最近 `KEY_REPEAT_GAP` 次（默认 2）已被选中」的 key → 同一把 key 两次被选中之间至少隔 2 次其他选择，**绝不背靠背重复**。若可用 key 数 ≤ gap 导致排除后为空，退化为「至少排除上一次那把」 |
| 2 | **今日成功次数最少** | 在满足间隔的候选里，选太平洋日内成功次数最少的一把，把当日用量摊平 |
| 3 | recency 轮转 | 同次数时选「最近最少被选中」的（`_key_last_picked_seq`），保证同批 key 公平轮转 |

辅助方法：
- `_pick_least_used_key()` — `key_manager.py:793`：按 `(今日成功次数, recency)` 选最优
- `_recent_picks[model]` — 最近选择序列（只保留 gap+1 个），支撑间隔约束
- 实测行为：3 keys → `K1,K2,K3,K1,…`（间隔 3）；不均衡用量（K1=10）仍保持 `K2,K3,K1,…` 间隔 3，**间隔优先于"最少优先"**

### 1.2 维护冷却逻辑

冷却一律是 **(key, model) 粒度**——某模型触发限流不影响该 key 的其他模型。

| 触发源 | 冷却时长 | 实现位置 |
|-------|---------|---------|
| 429 带上游 `retryDelay`（含 message 里 `Please retry in Ns`）| **以上游为准**（免费层 PerDay 实为滚动额度，常说 retry in 12s）| `key_manager.py:90 mark_key_cooldown(seconds=…)` |
| 429 RPD 且**无** retryDelay | 长冷却到**太平洋午夜**（`_rpd_cooldown_seconds`，`key_manager.py:314`）| 同上 `kind="rpd"` |
| 429 RPM 无 retryDelay / 503 过载 | 指数退避：`COOLDOWN_BASE_S × 2ⁿ`，封顶 `COOLDOWN_MAX_S`（2s 起、64s 封顶，带抖动）| `_cooldown_seconds` |
| 503 过载（模型级） | 额外给**整个模型**一个短冷却 `OVERLOAD_MODEL_COOLDOWN_S` | `record_model_overload`（`key_manager.py:362`）|

关键约束：
- **更长的冷却永不被短冷却覆盖**：已处于 RPD 长冷却的 key，不会被随后的 RPM 短冷却缩短（`mark_key_cooldown` 内比较 `existing > new_until`）。
- **上游 retryDelay 是权威**：结构化 `RetryInfo.retryDelay` 优先，message 里的 `Please retry in Ns` 兜底（`app/core/quota_parser.py:116`）。显式给值时的上限放宽到 `RPD_COOLDOWN_HOURS`（防异常超大值）。
- **冷却/过载/网络错误都不进永久失败数**——只有 `auth`（+2，快速拉黑）与 `unknown`（+1）才计 `key_failure_counts`，达到 `MAX_FAILURES`（10）才判失效。
- 冷却状态 **MySQL 持久化**（表 `t_key_model_state`，key 只存 SHA256），重启由 `restore_from_db()`（`key_manager.py:247`）恢复；成功调用 `mark_key_success()`（`key_manager.py:454`）只清冷却、保留当日统计。
- 每小时定时任务清理"冷却已过期且非今日"的历史行。

### 1.3 不同情形下的重试与报错逻辑

错误先经 `classify_and_extract()`（`error_classifier.py:101`）归类，
再按类别走不同分支（两条路径共用同一套语义）：

| 类别 | 判定 | 对 key 的处置 | 重试行为 | 给客户端的错误 |
|------|------|--------------|---------|---------------|
| `network` | **按类型**（httpx 异常 / `UpstreamNetworkError`），非状态码 | **不惩罚、不计数、不冷却**（只记监控 `note_error`）| **同一把 key** 退避 1s→2s→4s（`NETWORK_RETRY_ATTEMPTS=3`）后放弃 | `UpstreamNetworkError`：状态 `NETWORK_ERROR_STATUS_CODE`（默认 **424**，**不可重试**）|
| `client` | 400 非 key 原因（payload/turn/thought_signature/location）| 不计数；仅记监控 | **立即失败**（换 key 必然复现）| 上游原文透传（4xx）|
| `auth` | 400 含 `API key not valid` / 401 / 403 | 永久失败 **+2**（快速拉黑）| 立即换 key | 4xx |
| `rate_limit_rpm` | 429，quotaId 为 PerMinute/TPM 或无结构化体 | (key,model) 冷却（用上游 retryDelay 或指数退避），**不进永久计数** | 换 key | `AllKeysCoolingError` 429（可重试）|
| `rate_limit_rpd` | 429，quotaId 含 PerDay **且无** retryDelay | (key,model) 长冷却到太平洋午夜 | 换 key；全池 RPD 耗尽 → 探测重试 `RPD_PROBE_ATTEMPTS` 轮后仍无 → 中断 | 全 RPD 耗尽：**424 不可重试** + 换模型建议 |
| `overload` | 503 high demand | **模型级**计数（换 key 无效），不进永久计数 | 连续 `OVERLOAD_KEYS_BEFORE_GIVEUP`（3）次 → 中断 | `UpstreamOverloadError`：**424 不可重试** + 换模型建议 |
| `unknown` | 以上都不是 | 永久失败 +1（保守）| 换 key | 5xx |

换 key / 等待的公共规则（`gemini_chat_service.py:384 _call_with_retry` 与流式循环同构）：
- 请求成功 → `mark_key_success`（清冷却）+ `clear_model_overload`（清过载计数）
- 换 key 上限：流式 `max(MAX_RETRIES,3)` 轮；轮完抛 `AllKeysCoolingError`
- **全部 key 不可用时才等待**：`earliest_cooldown_release(model)`（`key_manager.py:507`）取最早到期，封顶 `ALL_COOLING_MAX_WAIT_S`（10s）；等待后重取一次，仍无才失败
- 「全 RPD 耗尽」的判定必须**每一把 key 都是 RPD**（`all_keys_rpd_exhausted`，`key_manager.py:443`）——部分 RPD/部分 RPM 时按「可重试限流」处理，不误报
- 全 RPD 时仍允许 `pick_rpd_probe_key`（`key_manager.py:416`）探测重试（配额可能已提前重置）

**为什么网络/RPD/过载返回 424**：AI SDK（opencode）只把 `408/409/429/≥500` 视为可重试
（`@ai-sdk/provider/.../api-call-error.ts:28-32`）。这三类重试无意义（网络断/日配额尽/模型全局过载），
返回 424 让客户端**立即停止重试**；瞬时限流保持 429 让客户端正常退避重试。

> ⚠️ **关键坑：状态码不是唯一判定依据。** opencode 还有第二道"可重试"判定——
> 对**错误 message 与 responseBody 做正则匹配**（见 `~/CodingProgram/opencode-config/RETRY-POLICY.md`
> §3）：命中 `network error` / `connection error` / `timeout` / `overloaded` /
> `at capacity` / 数字 `429|500|502|503|504|524` 等，**即使状态码是 424 也会被重试**。
>
> 因此 424 的**文案必须避开这些触发词**：
> - `UpstreamNetworkError`：**不回显原始异常文本**（`ConnectError`/`timed out` 等会命中正则），
>   原始文本只进日志（`raw_detail`），客户端只看到固定安全文案。
> - `UpstreamOverloadError`：不得出现 `503` / `overloaded` 字样。
> - `AllKeysCoolingError`（RPD 424）：不得出现 "quota exceeded"/"resource exhausted" 等。
>
> 相反，**429 瞬时限流**我们**希望** opencode 重试，所以其文案可含触发词，且会带
> `Retry-After` 头——opencode 会优先遵守该头（不被 30s 上限 clamp），实现"我们说多久就等多久"。

### 1.4 熔断器（424 快速失败窗口）

一旦真的返回 424，说明重试无意义。为避免"每个新请求都再走一遍重试流程然后同样 424"的空转，
分发侧维护一个**熔断窗口**（`BREAKER_WINDOW_S`，默认 15s）：

| 触发源 | 熔断范围 | 触发点 |
|-------|---------|-------|
| 网络不可达（同一 key 退避 3 次仍失败）| **全局**（所有模型）| `trip_global_breaker` |
| 某模型全 RPD 耗尽 | **该模型** | `trip_model_breaker(model, …)` |
| 某模型持续 503 过载达阈值 | **该模型** | `trip_model_breaker(model, …)` |

窗口内：
- 新的请求在**入口就短路**（路由依赖 `get_next_working_key` 与 service 两条路径均先查
  `get_active_breaker_error(model)`）→ 直接返回 424，且**复用"第一个触发者"的错误文案**
  （`UpstreamOverloadError(model=…, detail=active)`）。
- 全局熔断优先于模型熔断。
- **任一请求成功** → `clear_breaker_on_success(model)` 立即解除（链路/模型已恢复）。
- 窗口到期自动失效。
- 只保留"第一个触发者"的文案（`trip_*` 在已有错误时不覆盖）。

监控：`/api/keys/model-cooldown` 返回 `breaker` 字段（全局 + 各模型的剩余秒数与错误文案）。

### 1.5 分发侧**不做**的事（边界）

- ❌ 不追踪请求、不记录请求者（谁领的 key、还剩几个在途）
- ❌ 不按 key 加锁 / 不做名额池 / 不限制同一 key 的并发数
- ❌ 不等待某把 key 的上一次调用结束——可用就发
- ❌ 不感知 HTTP 层（不直接抛 HTTPException；自定义异常统一由 `app/exception/exceptions.py` 定义，路由层转译）

---

## 2. 请求侧：只管排队领 key

请求侧没有独立的排队组件——"排队"就是**每个请求按到达顺序，独立地调用分发接口领 key**。
asyncio 单 worker 下天然按事件循环顺序串行领取。

### 2.1 领 key 的两个入口

1. **入口（路由依赖）**：`gemini_routes.py:41 get_next_working_key`
   - 从请求体解析 `model`（冷却按模型隔离）
   - 调 `key_manager.get_next_working_key(model)` 领 key
   - **领到 → 直接进入 service**，全程不关心是哪一把、是否被别人在用
   - **领不到（全不可用）→ 这里是唯一的"等待点"**：非全 RPD 时等最早到期（≤10s）再领一次；仍无 → 返回 429（全 RPD 时 424）+ 换模型建议
2. **重试循环内**：`gemini_chat_service.py:384 _call_with_retry`（非流式）与
   `stream_generate_content`（流式）——失败后**再次领下一把**（`get_next_working_key`），
   全不可用时同样"等最早到期 → 再领一次 → 仍无才失败"。

### 2.2 请求侧**不做**的事（边界）

- ❌ 不挑 key（不知道也不选哪一把）
- ❌ 不维护冷却/失败计数（那是分发侧的事）
- ❌ 不实现任何按 key 的互斥（同一把 key 可同时服务多个请求——**这是设计，不是 bug**）
- ❌ 不区分请求来自谁（两个对话、两个用户，处理完全一致）

---

## 3. 两侧交互协议（唯一接口）

```
key = await key_manager.get_next_working_key(model)
  - 返回 str  : 领到一把可用 key（请求侧立即用它调上游，无任何附加等待）
  - 返回 None : 该模型当前没有任何可用 key（全部冷却/失效）
```

`None` 的语义是**"现在没有"**，不是"这把被占了"。请求侧收到 `None` 后：
1. 有限等待最早到期的冷却（≤ `ALL_COOLING_MAX_WAIT_S`）；
2. 再领一次；
3. 仍无 → 按情形返回 429（可重试限流）或 424（网络断/全 RPD/持续过载）。

**反例（已踩过的坑，勿重复）**：
- ❌ 按 `(key, model)` 加信号量/名额池（`acquire_key_slot`，已删除，commit a8b2e68）——
  让请求等待"同一把 key 的上一次调用结束"，违背"可用就发"，白白降低吞吐；
- ❌ 用在途计数（in-flight）挑选/排除 key（已删除，commit 2a64daf）——
  把"请求状态"泄漏进分发算法，违背两侧解耦；
- ❌ 严格 round-robin / "一轮内不重复"（曾被否决）——最终规则是**间隔 ≥ KEY_REPEAT_GAP
  的最少优先**，不是严格轮转。

---

## 4. 配置项速查（`app/config/config.py`）

| 配置 | 默认 | 归属 | 作用 |
|------|------|------|------|
| `KEY_REPEAT_GAP` | 2 | 分发·顺序 | 同一把 key 两次被选中之间至少隔开的选择次数（最高优先级）|
| `MAX_FAILURES` | 10 | 分发·失效 | 永久失败数达到即判 key 失效（仅 auth/unknown 会累加）|
| `COOLDOWN_BASE_S` / `COOLDOWN_MAX_S` | 2 / 64 | 分发·冷却 | 无上游 retryDelay 时的指数退避基数/封顶 |
| `RPD_COOLDOWN_HOURS` | 24 | 分发·冷却 | 无 retryDelay 的 RPD 长冷却（正常按太平洋午夜计算，此值是兜底）|
| `RPD_MIN_THRESHOLD_S` | 300 | 分发·冷却 | 剩余冷却超过此值视为"长冷却/RPD" |
| `RPD_PROBE_ATTEMPTS` | 2 | 分发·重试 | 全池 RPD 耗尽时的额外探测轮数 |
| `NETWORK_RETRY_ATTEMPTS` / `NETWORK_BACKOFF_BASE_S` | 3 / 1.0 | 分发·重试 | 网络错误同 key 退避次数/基数（1s→2s→4s）|
| `ALL_COOLING_MAX_WAIT_S` | 10 | 请求·等待 | 全部 key 不可用时，最多等最早到期的秒数 |
| `OVERLOAD_KEYS_BEFORE_GIVEUP` | 3 | 分发·过载 | 连续多少次 503 过载后中断并建议换模型 |
| `OVERLOAD_MODEL_COOLDOWN_S` | 5 | 分发·过载 | 模型级过载短冷却 |
| `NETWORK_ERROR_STATUS_CODE` | 424 | 报错 | 网络/RPD 全尽/持续过载返回的状态码（424 = AI SDK 不可重试）|
| `BREAKER_WINDOW_S` | 15 | 分发·熔断 | 返回 424 后的快速失败窗口（全局或按模型）|
| `MAX_RETRIES` | 3 | 分发·重试 | 换 key 轮次上限基数 |

---

## 5. 监控与持久化（分发侧的自证）

- **页面** `/model-cooldown`（`app/templates/model_cooldown.html`）+ 端点
  `/api/keys/model-cooldown`（`app/router/key_routes.py`）：
  展示今日出过错的 `(key, model)`：脱敏 key、现状徽章（RPD日耗尽/冷却中/可用）、
  最近错误类型、剩余冷却、最近错误时间、今日成功/出错次数；
  点击错误类型标签弹出**完整上游错误原文**；每模型可折叠 + 总现状徽章。
- **持久化** `t_key_model_state`：`key_hash`(SHA256)/`key_masked`/`model_name`/`kind`/
  `cooldown_until`/`last_error_time`/`last_error_log`/`error_count`/`success_count`/
  `stat_day`(太平洋日)。启动恢复（`restore_from_db`），成功只清冷却保留统计，
  每小时清理非今日且已过期的行。新增列由 `_apply_auto_migrations()` 幂等补齐。
- 今日成功/出错计数按**太平洋日**切分（与 Google 配额重置边界一致），跨日自动清零。

---

## 6. 快速对照：改代码前先问自己

> **我要改的行为属于哪一侧？**

| 想做的事 | 属于 | 改哪里 |
|---------|------|--------|
| 调整"下一把发谁"的顺序（间隔/最少优先/轮转）| 分发 | `get_next_working_key` / `_pick_least_used_key` |
| 调整冷却时长、触发条件 | 分发 | `mark_key_cooldown` / `quota_parser` / config |
| 调整某类错误的重试次数、退避 | 分发 | `_call_with_retry` / 流式循环 / config |
| 调整返回给客户端的状态码/文案 | 分发（异常定义）| `app/exception/exceptions.py` |
| 调整"全不可用时等多久" | 请求侧 | 路由 `get_next_working_key` 与 service 循环里的 `ALL_COOLING_MAX_WAIT_S` 逻辑 |
| 让某个请求插队/优先 | ❌ 不支持 | 设计上不存在请求优先级；两侧均无请求身份 |
| 限制同一 key 的并发数 | ❌ 不做 | 已两次否决（名额池/在途计数），违背"可用就发" |
