# Verified API: what was verified live, and where it differs from the docs

Docs: https://docs.verified.eu/docs/api-documentation/d6a8ba3f3c186-introduction
OpenAPI (Swagger 2.0, Stoplight export):
`https://stoplight.io/api/v1/projects/verified/api-documentation/nodes/reference/openapi.json?fromExportButton=true&snapshotType=http_service`

## Verified against the live API (2026-10-21)

Draft flow end to end with a real company account; recordings sanitized into
`tests/fixtures/live/` by `scripts/sanitize_fixtures.py`.

| Call | Live result | Docs say |
| --- | --- | --- |
| `POST /auth` (Basic) | 200 `{"token"}`, JWT **RS256**, 7 days | HS256 example |
| `GET /auth/userinfo` | 200; company id in `roles[].name` `/roles/admin/companies/<id>` | same |
| `GET /auth/token` | 200 `{"token"}` | same |
| `POST /envelope-descriptors/default/envelopes` `{}` | **201**, body `{"uid"}`, `Location: /api/envelopes/<id>` | same |
| `PUT /envelopes/{id}` (settings) | **202, empty body**, applied async (read back = applied) | 202 |
| `POST /envelopes/{id}/documents` | **200, empty body**, id ONLY in `Location` | 201 implied |
| `POST .../files/document` (multipart `file` + `name`) | 200 `{"id"}`; no S3 PUT step | guide hints at an S3 URL |
| `GET .../files` | 200 array with `fileType`, `storagePath`, `owners` | |
| `POST /envelopes/{id}/recipients` | **200, empty body**, id ONLY in `Location` | 200 `{}` |
| `GET /envelopes/{id}` | 200, embeds `documents[]` and `recipients[]` | |
| `DELETE /envelopes/{id}` (draft) | 200 | |
| `POST .../files/attachment` | 200 `{"id"}`; listed with `fileType: "attachment"` | |
| `GET /query-new/envelopes` | 200 array; **`descriptor` is a bare id string** (`"default"`), not the object `GET /envelopes/{id}` embeds | spec types it as string |

## Not yet verified live (needs a published envelope)

- `PUT /publish-status`, `jobs/get.sign.link`, `jobs/send.notification`, `abort-status`, `trash-status`
- The `signedFile` field on a completed document and `files/{id}/url?asObject=true`
- `GET /query-new/envelopes` filter/paging semantics (the call itself works; see above)
- Webhook delivery (needs the webhook registered in the web app)

## Sender on create (verified live 2026-10-21, drafts)

`POST /envelope-descriptors/{id}/envelopes` takes `{"sender": {email, givenName, familyName}}`
(OpenAPI spec, not the guides). Without it the envelope's `sender` is the API user. With it,
Verified records the sender **exactly as given, without validation**: a non-user and an
arbitrary address were both accepted (`test_sender_override`). So the API user can stay a
service account while the seller is the sender. Not yet seen: what a published envelope's
e-mails show and where completion notices go - always pass the seller's real e-mail.

## Behaviour the SDK encodes

- **IP lock**: tokens only work from the IP that logged in, unless `POST /auth?withoutIpLock=1`.
- **No idempotency keys**: the SDK never retries a POST once it may have reached the server.
- **Namespace**: `x-namespace: /companies/<id>`; envelopes are bound to the namespace they were created in.
- **Recipients cannot be deleted** (no endpoint). `PUT` takes no `telephone`/`notificationMethod`.
- **Sign links carry an `access_token`**: treat them as secrets, never log them.
- **Attachments are not signed**: only the document PDF is. Legally binding content must be inside the PDF.
- Documents: PDF only, at most 10 MB (3.5 MB for some NETS methods); up to 10 documents.

## Webhooks

- Registered per company in the web app (Company settings > Webhooks); no API. One URL per company.
- Delivered by **AWS SNS**: verify the RSA signature against `SigningCertURL`
  (`sns.<region>.amazonaws.com`); confirm the subscription by GETting `SubscribeURL`, or
  Verified disables the webhook (a disabled webhook must be deleted and re-created).
- `Message` is JSON-in-JSON: `{"Created","EventType","resource":{"type","id","uid","Version","Namespace","flowId","flowConfigId"}}` (mixed key case).
- Events: `envelope.published`, `envelope.trashed`, `envelope.aborted`, `envelope.expiration.set`,
  `document.aborted`, `document.rejected`, `notification.bounced`, `sealedDocument.attached` (= fully signed).
- No event data, no ordering guarantee, built-in latency: always re-read the envelope via the API.
