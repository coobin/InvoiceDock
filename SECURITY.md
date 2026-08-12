# Security Policy

## Supported versions

Security fixes are provided for the latest released minor version. The project is pre-1.0; deployments should pin a reviewed commit or release rather than track `main` automatically.

## Reporting a vulnerability

Do not open a public issue for vulnerabilities involving authentication bypass, secret disclosure, SSRF, unsafe archive handling, or invoice data exposure. Use the repository's private security advisory feature. Include the affected version, reproduction steps, impact, and a proposed mitigation if available.

Maintainers should acknowledge a complete report within seven days, coordinate a fix and disclosure window, and credit the reporter unless anonymity is requested.

## Deployment threat model

InvoiceDock processes untrusted email, attachments, XML, archives, URLs, and model output. The implementation applies the following controls:

- authenticated file routes; UUID storage names; format, size and archive expansion limits;
- defused XML parsing and separately bounded OFD/ZIP entry counts and total expansion sizes;
- public-IP validation before each email-link request and redirect;
- outbound destination validation for user-configured IMAP and LLM endpoints, with an administrator-only private-host allowlist;
- CSRF tokens on state-changing browser requests;
- Argon2 local passwords (12-character minimum by default), signed `HttpOnly` sessions, optional secure-cookie mode;
- OIDC binding by `issuer + sub`, mandatory verified-email semantics, and no automatic email-based linking to local accounts;
- Fernet encryption for mailbox passwords and API keys using a key derived from `APP_SECRET`;
- no secret values in audit logs; no upload directory mounted as public static content;
- owner-scoped invoices, source files, mailboxes, job logs, file hashes, business deduplication, and verification caches;
- per-user storage, upload, OCR/LLM, tax-provider, and task-concurrency limits plus a global processing limit;
- spreadsheet text neutralization to prevent formula execution in exported workbooks;
- CSP, `X-Content-Type-Options`, frame protection, a strict referrer policy, and `no-store` on sensitive responses;
- a non-root, read-only-root container with all Linux capabilities dropped, `no-new-privileges`, PID/CPU/memory limits, a bounded temporary filesystem, and log rotation;
- weekly dependency update checks and a strict `pip-audit` CI job.

These controls do not make public Internet exposure safe by themselves. Put the service behind HTTPS, keep the application port private, restrict egress and ingress where possible, patch the host and images, and monitor backups. `FORWARDED_ALLOW_IPS` and `TRUSTED_PROXY_IPS` must list controlled direct proxy peers; never configure either as `*`.

## Sensitive data

Invoices contain personal and financial data. Before enabling an external LLM, determine whether text or images may leave the organization and whether the provider's retention terms are acceptable. Disable vision input or use an internal OpenAI-compatible endpoint when required.

Back up both `data/` and `APP_SECRET`. Rotating `APP_SECRET` without re-encrypting stored values makes existing mailbox/API credentials unreadable. Treat `.env`, database backups, and invoice files as secrets. Encrypt backups, grant the backup identity read-only access where practical, and perform periodic restore drills that include SQLite `quick_check` and referenced-file validation.

## Known security boundaries

- Member data is isolated by application/database ownership checks; administrators intentionally have global access. All users still share one SQLite database, process, and filesystem, so this is not a cryptographic tenant boundary. Deploy separate instances when that boundary is required.
- OIDC trust is only as strong as the configured issuer. Restrict domains/groups, require verified emails, protect the client secret, and test role mappings before enabling it publicly.
- Direct email links are fetched only when they resolve to public addresses, but DNS and remote content remain adversarial. Keep egress filtering if the environment requires a stronger boundary.
- Private IMAP/LLM destinations require an explicit administrator allowlist. An allowlist is not a replacement for egress firewall rules, DNS controls, or authentication on internal services.
- In-process rate and concurrency controls assume one application replica. Run only one replica with SQLite; use a shared rate limiter, durable task queue, and PostgreSQL before scaling horizontally.
- Resource limits mitigate abuse but do not guarantee availability against every parser, OCR, image, or decompression attack. Size the limits below host capacity and monitor disk usage, OOM events, and job latency.
- Complex browser automation is intentionally not included in v0.1 because it materially expands the attack surface.
