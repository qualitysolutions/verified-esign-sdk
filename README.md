# qls-verified-esign

Python SDK for the **Verified (verified.eu) eSign API**: create envelopes, upload PDFs and
attachments, add signers (BankID and the other eID methods), publish, get signing links,
download the signed PDF, and verify the webhooks Verified sends through AWS SNS. Sync and
async clients, typed pydantic v2 models, `mypy --strict` clean.

> **Status: early release.** Verified against the live API on 2026-10-21 for the full
> draft flow (login, envelope, document + upload, recipients, settings, delete). Publishing,
> signing links, the signed-PDF download and webhook delivery follow the official docs and
> OpenAPI spec but are not yet confirmed live. Not an official Verified product. See
> [docs/API_NOTES.md](docs/API_NOTES.md) for where the live API differs from the docs.

## Install

```bash
pip install qls-verified-esign              # API client
pip install "qls-verified-esign[webhooks]"  # + SNS signature verification (cryptography)
```

The distribution is `qls-verified-esign`; the import package is `verified_esign`.
Requires Python 3.10+. Depends on `httpx` and `pydantic>=2`.

## Quick start: send a contract for signing

```python
from verified_esign import EnvelopeSettings, RecipientCreate, SigningDocument, VerifiedClient

with VerifiedClient() as verified:  # reads VERIFIED_* environment variables
    sent = verified.send_for_signing(
        SigningDocument(
            "Avtal AVT-2026-0042.pdf", "avtal.pdf",
            attachments=[("Bilaga 1 - Tjänstebeskrivning.pdf", "bilaga1.pdf")],
        ),
        [
            RecipientCreate(given_name="Anna", family_name="Andersson", email="anna@kund.se", order=1),
            RecipientCreate(given_name="Erik", family_name="Eriksson", email="erik@qls.se", order=2),
        ],
        settings=EnvelopeSettings(greeting="Hej! Här är avtalet för signering.", automatic_reminders=2),
        tags=["naas-avtal"],
    )
    print(sent.envelope_id)
```

`send_for_signing` creates the envelope, applies the settings, creates each document,
uploads the PDF and its attachments, adds the recipients and publishes (which sends the
invitations). Pass `publish=False` to build a draft that notifies nobody. If any step
fails before publishing, the half-built draft is deleted and the error re-raised.

Recipients default to Swedish BankID (`bankid-se`), `sv_SE` and the `signer` role. Other
methods: `bankid-no`, `nets-eident-fi`, `nets-eident-dk`, `eideasy-*`, `email`, `sms`,
`touch-sign` (eIDs must be enabled for your company by Verified). Set `ssn=` to require
that the eID matches a specific personnummer.

### Attachments (bilagor)

Attachments travel with the document and are shown to the signers, **but only the
document PDF is signed**. Anything that must be legally covered by the signature (e.g.
contract terms) belongs inside the PDF; attachments suit reference material. Up to 20 per
document; pdf, docx, xlsx, pptx, zip, png, gif, jpg, rtf, odt. Documents must be PDF and at
most 10 MB (3.5 MB for some NETS methods). Both limits are checked before anything is sent.

## When it is signed: webhook -> signed PDF

```python
from verified_esign import EVENT_SIGNED, WebhookVerifier

verifier = WebhookVerifier(expected_namespace="/companies/<company-id>",
                           allowed_topic_arns={"arn:aws:sns:eu-north-1:...:WebhooksAdmin-..."})

def handle(body: bytes) -> int:
    msg = verifier.verify(body)               # WebhookVerificationError -> answer 403
    if msg.type == "SubscriptionConfirmation":
        verifier.confirm_subscription(msg)    # once, when the webhook is registered
    elif msg.event and msg.event.event_type == EVENT_SIGNED:
        enqueue(msg.event.resource.envelope_id)   # do the work out of band
    return 200

# worker
envelope = verified.envelopes.get(envelope_id)
for doc in envelope.documents:
    pdf = verified.documents.download_signed(envelope.id, doc.id)
```

- Webhooks are registered per company in the Verified web app (Company settings >
  Webhooks), not via the API. Each company needs its own endpoint URL.
- Verified delivers through **AWS SNS** with no shared secret: `WebhookVerifier` checks
  the RSA signature against the certificate at `SigningCertURL`, which must be an
  `https://sns.<region>.amazonaws.com/...pem` URL. It also checks the company, the topic
  (when pinned) and a replay window.
- The first message is a `SubscriptionConfirmation`. Until `confirm_subscription` runs the
  webhook stays pending, and if it never runs Verified disables it (delete and re-create).
- Events carry no data, may be late and may arrive out of order: treat them as "go look"
  hints and read the envelope through the API. `sealedDocument.attached` means every
  recipient has signed and the sealed PDF exists.

## Authentication & configuration

Verified has no API keys. The SDK logs in as a Verified user (`POST /auth`, Basic auth),
receives a JWT valid 7 days, reuses it until an hour before `exp`, and logs in again on a
`401`. **Use a dedicated API user** and keep its password in a secret store.

| Setting      | Env var                | Notes |
| ------------ | ---------------------- | ----- |
| `email`      | `VERIFIED_EMAIL`       | required |
| `password`   | `VERIFIED_PASSWORD`    | required |
| `company_id` | `VERIFIED_COMPANY_ID`  | optional if the user belongs to exactly one company |
| `ip_lock`    | `VERIFIED_IP_LOCK`     | `1` (default) or `0` |
| `base_url`   | `VERIFIED_BASE_URL`    | default `https://app.verified.eu/api` |

**IP lock**: by default a token only works from the IP that logged in. On hosts whose
egress IP can change (Kubernetes without a static NAT IP, laptops on a VPN or Cisco Secure
Access) set `ip_lock=False`.

## API map

| Namespace | Operations |
| --- | --- |
| `send_for_signing(...)` | documents + attachments + recipients + publish in one call |
| `envelopes` | `create`, `get`, `update`, `publish`, `abort`, `trash`, `delete`, `query`, `iter` |
| `documents` | `create`, `get`, `list`, `upload`, `list_files`, `file_url`, `download`, `download_signed` |
| `recipients` | `add`, `get`, `list`, `update`, `sign_link`, `remind` |
| `userinfo()`, `namespace`, `renew_token()` | account helpers |
| `request(...)` | escape hatch for any path |

Writes are asynchronous at Verified (CQRS): settings and recipient updates answer
`202 Accepted` and apply shortly after, so read back with `get` when you need the new
state. Verified has no idempotency keys, so the SDK never retries a POST that may have
reached the server.

Signing links (`recipients.sign_link`) embed an access token. Treat them as secrets.

## Errors

Every error derives from `VerifiedError`. HTTP errors raise `APIStatusError` subclasses
(`BadRequestError`, `UnauthorizedError`, `ForbiddenError`, `NotFoundError`,
`ConflictError`, `UnprocessableEntityError`, `RateLimitError`, `ServerError`) with
`status_code`, `body`, `method` and `path`. Login failures raise `AuthenticationError`
(never echoing the response). Webhook problems raise `WebhookVerificationError`.

## Logging

The SDK logs to the `verified_esign` logger (silent by default): DEBUG for method, path,
status and timing, WARNING for retries. It never logs credentials, tokens, signing links,
headers or bodies.

## Development

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/mypy src/verified_esign
.venv/bin/pytest -q                    # unit + sanitized live fixtures
.venv/bin/pytest -m integration        # live draft round trip; needs VERIFIED_* in the env
```

`scripts/sanitize_fixtures.py` turns a raw recording of live responses into
`tests/fixtures/live/` and refuses to write a file if any personal value survives.

## License

MIT, see [LICENSE](LICENSE).
