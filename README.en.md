# InvoiceDock · 票舱

A self-hosted web workspace for collecting Chinese invoices from IMAP mailboxes or manual uploads, prioritizing Tax Invoice Cloud verification, falling back to local OCR/document text plus an OpenAI-compatible LLM for dual-source consistency checks, and producing A4 print sheets and Excel ledgers.

The application clearly distinguishes provider-backed verification from OCR/LLM agreement. Model agreement reduces data-entry errors; it is not proof of tax authenticity.

## Highlights

- Multiple IMAP mailboxes, scheduled read-only scans, supported attachments, bounded ZIP extraction, and safe direct-download links.
- PDF/XML/OFD/image ingestion with owner-scoped SHA-256 and business deduplication.
- Tax app/access token flow and `recognitionCheck` integration.
- Local PDF text extraction, XML/OFD parsing, Tesseract OCR, and configurable OpenAI-compatible models.
- Field-by-field evidence comparison, manual review, business-key duplicate alerts, and audit logs.
- Searchable invoice ledger, previews, Excel export, and A4 1/2/4-up PDF generation.
- Local admin plus optional OIDC, CSRF protection, encrypted integration/mail credentials, and owner-scoped invoices, files, mailboxes, logs, and verification caches.
- One-container Docker Compose deployment with SQLite WAL persistence, a non-root read-only container, resource limits, and log rotation.

## Start

```bash
cp .env.example .env
# Set APP_SECRET, ADMIN_PASSWORD and APP_BASE_URL
docker compose up -d --build
```

Open `http://localhost:8765`. See the [Chinese README](README.md), [deployment guide](docs/DEPLOYMENT.md), [architecture](docs/ARCHITECTURE.md), and [security policy](SECURITY.md) for complete details.

Compose binds the application to `127.0.0.1` by default. Keep it behind an HTTPS reverse proxy, configure `FORWARDED_ALLOW_IPS` and `TRUSTED_PROXY_IPS` with controlled direct proxy peers (never `*`), and use `APP_BIND_IP=0.0.0.0` only together with a firewall or a private Docker network. Environment-supplied integration values take precedence over database values.

Self-registration requires a 12-character password by default. OIDC identities bind only by `issuer + sub`; a verified email collision never auto-links an existing local account. Member data is isolated at the application/database-row level, while administrators retain global access. This is not a separate database or encryption domain per tenant.

## License

Apache-2.0. The implementation is original; research references are documented in [NOTICE](NOTICE) and [docs/RESEARCH.md](docs/RESEARCH.md).
