# Security policy

## Supported versions

Security fixes are applied to the latest tagged release. Before `1.0`, pin an
exact tag or reviewed commit for production use.

## Reporting a vulnerability

Do not open a public issue with exploit details, credentials, production data,
booking references, chat identifiers or personal travel plans.

Use GitHub's **Security → Report a vulnerability** flow when available. If
private vulnerability reporting is not enabled, contact the maintainer through
the GitHub profile and share only enough information to establish a private
reporting channel.

Please include the affected version, a minimal reproduction using synthetic
data, likely impact and any mitigation already tested.

## Sensitive areas

- provider tokens and OAuth credentials;
- Telegram authorization and chat allowlists;
- SMTP URLs containing credentials;
- request URLs, logs and exception traces;
- deduplication behavior that could create alert floods;
- unbounded retry or scan behavior;
- SQLite files containing historical searches.
