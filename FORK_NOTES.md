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

## Notes

- No upstream files were removed. The patches are intentionally small and
  individually revertible.
- Secrets (API keys, DB passwords, `AUTH_TOKEN`) live only in a local `.env`,
  which is git-ignored and never committed.

## License

This fork keeps the upstream license (CC BY-NC 4.0, non-commercial). See
[LICENSE](./LICENSE).
