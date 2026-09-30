# Changelog — fork additions

All changes below are local patches on top of
[snailyp/gemini-balance](https://github.com/snailyp/gemini-balance).
Upstream code is otherwise unchanged.

## 2026-09-27

### Dispatch: request-agnostic key rotation

- The backend's dispatch is intentionally simple and **request-agnostic**: it only
  (1) hands out a key, (2) decides which key comes next, (3) maintains cooldown /
  error logic. It does not track requests or queues.
- Key selection gives **top priority to not repeating a key too soon**: the same key
  is not reused until at least `KEY_REPEAT_GAP` (default 2) other selections have
  happened. Among keys satisfying that gap, it picks the available one with the
  fewest successful calls today (ties broken by least-recently-picked). Cooling /
  invalid keys are skipped; selecting a key never waits for a previous call on it.

### Concurrency fairness: no more starvation between simultaneous conversations

- Previously there was no notion of a key being **in use**: two conversations could
  be handed the same key, and once it cooled, both raced for the same single key —
  one conversation could be starved for a long time.
- Added **in-flight tracking per `(key, model)`** (`mark_key_inflight` /
  `release_key_inflight`). Key selection now prefers the key with the **fewest
  in-flight requests**, then the fewest successful calls today, then least-recently
  picked. Concurrent conversations are now spread across distinct keys.
- New "in-flight" column on the `/model-cooldown` dashboard.

### Upstream overload (503 high demand) is no longer treated as a rate limit

- Upstream `503 high demand` ("This model is currently experiencing high demand")
  is a **model-wide** condition — switching API keys does not help. It used to be
  handled like an RPM rate limit (per-key cooldown + key switch), which just spun
  through the whole pool.
- It is now tracked at the **model level**: consecutive overloads are counted and a
  short model-wide cooldown is applied. After
  `OVERLOAD_KEYS_BEFORE_GIVEUP` (default 3) consecutive overloads, the proxy returns
  a **non-retryable** error (`UpstreamOverloadError`, status
  `NETWORK_ERROR_STATUS_CODE`, default 424) telling the client to **switch model**,
  and lists models that may still be available. A successful call clears the counter.
- New settings: `OVERLOAD_KEYS_BEFORE_GIVEUP`, `OVERLOAD_MODEL_COOLDOWN_S`.

### Robust upstream error handling

- **Error classification** (`app/core/error_classifier.py`): classify upstream
  failures into `network` / `client` / `auth` / `rate_limit_rpd` /
  `rate_limit_rpm` / `overload` / `unknown`, each with its own retry strategy.
- **Quota parsing** (`app/core/quota_parser.py`): read the upstream 429 body for
  `quotaId` (per-day RPD vs per-minute RPM), `quotaDimensions.model`, and
  `RetryInfo.retryDelay`.
- **Per-`(key, model)` cooldown** (`app/service/key/key_manager.py`): RPM
  cooldowns use the upstream `retryDelay`; RPD cools until Pacific midnight;
  a longer cooldown is never shortened by a shorter one. A model hitting its
  quota does not affect other models on the same key.
- **Unified retry loop** (`app/service/chat/gemini_chat_service.py`): a single
  loop for stream and non-stream. The route-level `RetryHandler` is removed from
  the chat endpoints (previously up to 3×3 = 9 upstream calls per request).
  - `network`: retry the **same** key with backoff (1s/2s/4s), then fail; the key
    is never penalized.
  - `client` (400): fail fast; switching keys cannot help.
  - `auth` (bad key / 401 / 403): penalize and switch immediately.
  - `rate_limit_rpm` / `overload`: cooldown `(key, model)` and switch.
  - `rate_limit_rpd`: long cooldown; if every key is exhausted for a model,
    return immediately with a "switch model" hint.
- **429 / 503 / network errors no longer count toward the permanent failure
  counter** — only `auth` / `unknown` do. Keys are no longer misjudged as invalid.
- **Network errors are explicit** (`UpstreamNetworkError`). The returned status
  code is configurable via `NETWORK_ERROR_STATUS_CODE` (default **424**), which is
  outside the AI SDK retry set (408/409/429/≥500), so opencode stops retrying
  immediately instead of spinning.
- **Log write fix**: normalize empty/invalid `status_code` before writing request
  / error logs (fixes MySQL error 1366).

### Persistence

- New table **`t_key_model_state`** storing per-`(key, model)` cooldown and daily
  statistics; the key is stored as a SHA256 hash, never plaintext.
- State is restored on startup (cooldowns and today's statistics survive restarts);
  an hourly job cleans stale rows.
- Lightweight auto-migration adds new columns to the existing table idempotently.

### Dashboard

- New **`/model-cooldown`** page and `/api/keys/model-cooldown` endpoint.
- Shows today's `(key, model)` pairs that hit 429/503: masked key, status badge
  (`RPD 日耗尽` / `冷却中` / `可用`), last error type, remaining cooldown, last
  error time, **today's success / error counts**, with per-model collapsible cards
  and an overall model status (`RPD全部耗尽` / `全部冷却中` / `部分冷却` / `可用`).
- Clicking the error-type badge opens the full upstream error body.
- Navigation button added to the config / dashboard / logs pages.

### Load balancing

- `BALANCE_BY_SUCCESS_COUNT` (default on): pick the available key with the fewest
  successful calls today, skipping the key picked last (avoids bursting a single
  key into its RPM limit). Rate-limit cooldown remains the hard backstop.

### Also included

- Fix intermittent `400 Requests ending with a model turn are not supported`
  (`_ensure_valid_ending_turn`, see `DIAGNOSIS-turn-400.md`).
- Pin `starlette<1.0` (`constraints.txt`) — Starlette ≥ 1.0 broke the web UI.
- Font Awesome CDN switched to jsDelivr.
- systemd unit template under `deploy/`.

## Earlier

- `fix(gemini): append trailing user turn to avoid model-turn 400`.
