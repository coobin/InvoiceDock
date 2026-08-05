# Security Policy

## Supported versions

Security fixes are provided for the latest released minor version. The project is pre-1.0; deployments should pin a reviewed commit or release rather than track `main` automatically.

## Reporting a vulnerability

Do not open a public issue for vulnerabilities involving authentication bypass, secret disclosure, SSRF, unsafe archive handling, or invoice data exposure. Use the repository's private security advisory feature. Include the affected version, reproduction steps, impact, and a proposed mitigation if available.

Maintainers should acknowledge a complete report within seven days, coordinate a fix and disclosure window, and credit the reporter unless anonymity is requested.

## Deployment threat model

InvoiceDock processes untrusted email, attachments, XML, archives, URLs, and model output. The implementation applies the following controls:

- authenticated file routes; UUID storage names; format, size and archive expansion limits;
- defused XML parsing and bounded OFD/ZIP traversal;
- public-IP validation before each email-link request and redirect;
- CSRF tokens on state-changing browser requests;
- Argon2 local passwords, signed `HttpOnly` sessions, optional secure-cookie mode;
- Fernet encryption for mailbox passwords and API keys using a key derived from `APP_SECRET`;
- no secret values in audit logs; no upload directory mounted as public static content;
- a non-root container with all Linux capabilities dropped and `no-new-privileges`.

These controls do not make public Internet exposure safe by themselves. Put the service behind HTTPS, restrict network access where possible, patch the host and images, and monitor backups.

## Sensitive data

Invoices contain personal and financial data. Before enabling an external LLM, determine whether text or images may leave the organization and whether the provider's retention terms are acceptable. Disable vision input or use an internal OpenAI-compatible endpoint when required.

Back up both `data/` and `APP_SECRET`. Rotating `APP_SECRET` without re-encrypting stored values makes existing mailbox/API credentials unreadable. Treat `.env`, database backups, and invoice files as secrets.

## Known security boundaries

- The current app is a single shared workspace. Roles control administration, not row-level invoice ownership.
- OIDC trust is only as strong as the configured issuer. Restrict domains/groups and protect the client secret.
- Direct email links are fetched only when they resolve to public addresses, but DNS and remote content remain adversarial. Keep egress filtering if the environment requires a stronger boundary.
- Complex browser automation is intentionally not included in v0.1 because it materially expands the attack surface.

