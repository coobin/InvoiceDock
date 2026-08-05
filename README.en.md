# InvoiceDock · 票舱

A self-hosted web workspace for collecting Chinese invoices from IMAP mailboxes or manual uploads, prioritizing Kingdee Invoice Cloud verification, falling back to local OCR/document text plus an OpenAI-compatible LLM for dual-source consistency checks, and producing A4 print sheets and Excel ledgers.

The application clearly distinguishes provider-backed verification from OCR/LLM agreement. Model agreement reduces data-entry errors; it is not proof of tax authenticity.

## Highlights

- Multiple IMAP mailboxes, scheduled read-only scans, supported attachments, bounded ZIP extraction, and safe direct-download links.
- PDF/XML/OFD/image ingestion with SHA-256 file deduplication.
- Kingdee app/access token flow and `recognitionCheck` integration.
- Local PDF text extraction, XML/OFD parsing, Tesseract OCR, and configurable OpenAI-compatible models.
- Field-by-field evidence comparison, manual review, business-key duplicate alerts, and audit logs.
- Searchable invoice ledger, previews, Excel export, and A4 1/2/4-up PDF generation.
- Local admin plus optional OIDC, CSRF protection, encrypted integration/mail credentials.
- One-container Docker Compose deployment with SQLite WAL persistence.

## Start

```bash
cp .env.example .env
# Set APP_SECRET, ADMIN_PASSWORD and APP_BASE_URL
docker compose up -d --build
```

Open `http://localhost:8765`. See the [Chinese README](README.md), [deployment guide](docs/DEPLOYMENT.md), [architecture](docs/ARCHITECTURE.md), and [security policy](SECURITY.md) for complete details.

## License

Apache-2.0. The implementation is original; research references are documented in [NOTICE](NOTICE) and [docs/RESEARCH.md](docs/RESEARCH.md).
