# Changelog

All notable changes to `qls-verified-esign` are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions are
CalVer `YYYY.M.D[.N]`, like the other QLS SDKs.

## [2026.10.21] - 2026-10-21

First release.

### Added
- `VerifiedClient` / `AsyncVerifiedClient`: email + password login, JWT cached until
  1 h before `exp`, re-login on 401, optional `withoutIpLock`, company namespace from
  `company_id` or resolved from `/auth/userinfo`.
- `envelopes`: `create`, `get`, `update` (settings), `publish`, `abort`, `trash`, `delete`,
  `query` / `iter` (`/query-new/envelopes`).
- `documents`: `create`, `get`, `list`, `upload` (document or attachment, validated
  locally), `list_files`, `file_url`, `download`, `download_signed`.
- `recipients`: `add`, `get`, `list`, `update`, `sign_link`, `remind`.
- `send_for_signing(...)`: one call from PDF(s) + attachments + recipients to a published
  (or draft) envelope; deletes the half-built draft if a step fails.
- `WebhookVerifier`: AWS SNS signature verification (v1/v2), certificate-URL pinning to
  `sns.*.amazonaws.com`, topic allowlist, company check, replay window, subscription
  confirmation; typed `WebhookEvent` (extra `webhooks` installs `cryptography`).
- Read models accept both envelope shapes: `GET /envelopes/{id}` embeds objects, while
  `/query-new/envelopes` returns bare ids (`descriptor: "default"`, string `role` /
  `signedFile`) and notifications keyed by id (normalised to a list). Found by the live
  integration test.
- Verified read/write against the live API (draft flow incl. attachments); sanitized recordings in
  `tests/fixtures/live/`. See `docs/API_NOTES.md` for live-vs-docs differences.
