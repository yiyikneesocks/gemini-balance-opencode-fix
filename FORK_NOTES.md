# Fork Notes — gemini-balance (opencode fix edition)

This repository is a fork of [snailyp/gemini-balance](https://github.com/snailyp/gemini-balance).

Upstream project: **snailyp/gemini-balance** — https://github.com/snailyp/gemini-balance
(All original code, design and documentation belong to the upstream author.
This fork only adds a small set of local patches, listed below.)

## Purpose of this fork

A self-hosted Gemini proxy used together with **opencode**. The patches below
fix issues observed when opencode talks to the proxy using the **native Gemini
API format** (`POST /v1beta/models/{model}:streamGenerateContent`).

## Local changes vs. upstream

| File | Change | Reason |
|------|--------|--------|
| `app/service/chat/gemini_chat_service.py` | Added `_ensure_valid_ending_turn()` and applied it in `_build_payload()` | Fix intermittent `400 Requests ending with a model turn are not supported`. The upstream `@ai-sdk/google` SDK may emit a trailing empty `{role:"model"}` content, and empty-part filtering can expose a model turn at the end. The proxy now appends a minimal user turn when needed. |
| `app/templates/base.html` | Font Awesome CDN `cdnjs.cloudflare.com` → `cdn.jsdelivr.net` | cdnjs was slow/unavailable on the deployment host, breaking the UI icons. |
| `constraints.txt` (new) | `starlette<1.0` | Starlette ≥ 1.0 changed the `TemplateResponse` signature and caused the web UI to return 500. Install with `pip install -r requirements.txt -c constraints.txt`. |
| `deploy/gemini-balance.service` (new) | systemd unit | Long-running non-Docker deployment. Adjust `User`/`WorkingDirectory` to your environment. |
| `scripts/ghpush.sh` (new) | Per-repository GitHub HTTPS push helper | Credential isolation per repository. Contains no secrets. |

### Error handling: classification, per-model cooldown, persistence

Upstream treats **every** non-200 response identically: rotate to the next key,
retry, and increment a permanent failure counter for that key. Under real traffic
this blacks out the whole key pool during network outages, wastes retries on 400
payload errors, and misjudges rate-limited keys as "invalid". This fork replaces
that with error classification plus per-`(key, model)` cooldowns.

| File | Change | Reason |
|------|--------|--------|
| `app/core/error_classifier.py` (new) | Classify upstream errors into `network / client / auth / rate_limit_rpd / rate_limit_rpm / overload / unknown` | Each class needs a different retry strategy. |
| `app/core/quota_parser.py` (new) | Parse the upstream 429 body: `quotaId` (per-day vs per-minute), `quotaDimensions.model`, `RetryInfo.retryDelay`, plus message fallbacks | Distinguish daily quota exhaustion (RPD) from momentary rate limits (RPM), and use Google's own `retryDelay` instead of guessing. |
| `app/utils/helpers.py` | Added `extract_error_info()` | Safe extraction of `(status, message)`; fixes `tuple index out of range` on httpx exceptions that carry only one arg. |
| `app/service/key/key_manager.py` | Per-`(key, model)` cooldown; RPM uses upstream `retryDelay`; RPD cools until Pacific midnight; longer cooldowns are never shortened by shorter ones | A model hitting its quota must not affect other models on the same key. |
| `app/service/chat/gemini_chat_service.py` | Unified retry loop for stream and non-stream: network → same-key exponential backoff (1s/2s/4s) then fail; `client` (400) → fail fast; `auth` → penalize + switch; `rate_limit`/`overload` → cooldown + switch | Removes the double retry layer (service loop × route `RetryHandler`) that produced up to 9 upstream calls per request. |
| `app/exception/exceptions.py` | `UpstreamNetworkError` (503, explicit message), `AllKeysCoolingError` (429 + model-switch suggestion) | Network problems are reported as network problems, not as a generic 500. |
| `app/service/client/api_client.py` | Wrap httpx calls; raise `UpstreamNetworkError` on `httpx.HTTPError` | Same reason. |
| `app/database/models.py` + `app/database/services.py` | New table `t_key_model_state` (key stored as SHA256, never plaintext); restore on startup; hourly cleanup | Cooldown state survives restarts instead of being lost from memory. |
| `app/router/key_routes.py`, `app/router/routes.py`, `app/templates/model_cooldown.html` (new) | `/model-cooldown` page and `/api/keys/model-cooldown` endpoint | Observe which `(key, model)` pairs hit 429/503 today and when they recover. |
| `app/handler/error_handler.py`, routers | Pass `APIError` through instead of collapsing into 500; routers use `extract_error_info()` | Preserve the real status code and message for the client. |

Behaviour summary:

- `network` → retry the **same** key with backoff, never penalize the key.
- `client` (400 payload) → fail immediately; switching keys cannot help.
- `auth` (400 bad key / 401 / 403) → penalize the key, switch immediately.
- `rate_limit_rpm` / `overload` → cooldown that `(key, model)` and switch key.
- `rate_limit_rpd` → long cooldown for that `(key, model)` until Pacific midnight;
  if every key is exhausted for that model, return 429 immediately with a
  "switch model" hint listing models that still have quota.
- 429 / 503 / network errors **never** count toward the permanent failure counter —
  only auth and unknown errors do. Keys are no longer misjudged as invalid.

All new settings (`NETWORK_RETRY_ATTEMPTS`, `NETWORK_BACKOFF_BASE_S`,
`COOLDOWN_BASE_S`, `COOLDOWN_MAX_S`, `ALL_COOLING_MAX_WAIT_S`,
`RPD_COOLDOWN_HOURS`, `RPD_MIN_THRESHOLD_S`) have defaults in `app/config/config.py`,
so no `.env` change is required.

## Notes

- No upstream files were removed. The patches are intentionally small and
  individually revertible.
- Secrets (API keys, DB passwords, `AUTH_TOKEN`) live only in a local `.env`,
  which is git-ignored and never committed.

## License

This fork keeps the upstream license (CC BY-NC 4.0, non-commercial). See
[LICENSE](./LICENSE).
