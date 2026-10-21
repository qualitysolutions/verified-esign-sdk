# Changelog

All notable changes to `qls-verified-esign` are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions are
CalVer `YYYY.M.D[.N]`, like the other QLS SDKs.

## [2026.10.21.1] - 2026-10-21

### Added
- `make_tag(value)`: turns a display value (e.g. the customer company name) into a valid
  Verified tag - lowercase, ASCII-folded (å/ä -> a, ö -> o), runs of other characters ->
  `-`. `validate_tag(tag)` checks one.
- `DocumentCreate.tags` and `send_for_signing(tags=...)` are validated locally (Verified:
  lowercase, no spaces, >= 3 chars) and de-duplicated; `send_for_signing` rejects a bad tag
  before creating anything.
- Live integration test tags its draft, finds it again via `documents.tags` and checks
  that every hit carries the tag (the filter really filters).

### Fixed
- `envelopes.query`/`iter`: a bare `{"field","op","value"}` filter is wrapped in
  `{"and": [...]}`, the only form Verified documents. Live, a bare tag filter returned
  untagged envelopes (looked ignored, no error).
- `Recipient.order` is a float when read: live envelopes hold fractional orders (`100.5`)
  and one such envelope failed `envelopes.query` for the whole page. Found live.

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
