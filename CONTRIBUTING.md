# Contributing

## Development setup

Clone `radar-core` and `vigia` into the same parent directory, then run:

```bash
cd vigia
make install
make check
```

`make vendor-core` refreshes the ignored `vendor/radar-core` snapshot. Do not
commit that generated directory.

## Where changes belong

Keep provider adapters, travel contracts, route scheduling, calendar policies,
deal rules and alert rendering in Vigía. Generic lifecycle, persistence,
resilience and transport behavior belongs in `radar-core` only after multiple
consumers prove the abstraction.

## Pull request checklist

1. Add or update tests for observable behavior.
2. Run `make check`.
3. Keep all fixtures synthetic and free of account identifiers.
4. Document new environment variables in `.env.example`.
5. Update `CHANGELOG.md` for consumer-visible changes.
6. Never include real credentials, bookings, travel plans or chat IDs.

Security vulnerabilities must follow [`SECURITY.md`](SECURITY.md) and must not
be reported in a public issue.
