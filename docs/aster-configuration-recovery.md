# Aster configuration recovery before entry

Aster prepares margin mode and leverage before any entry order. Missing replies
to those requests must not hold account reconciliation indefinitely. They also
must never authorize replaying an uncertain entry.

Configuration preparation first reads the exact native symbol in one-way mode.
It sends only settings that differ from the requested values. An already
effective margin mode or leverage does not require a POST.

For an uncertain configuration, recovery reads its current exact-symbol settings.
If they match, configuration is positively reconciled. Otherwise, recovery waits
until the exchange's fresh server clock proves every old signature's nonce
window has ended. Aster's [official V3 contract](https://github.com/asterdex/api-docs/blob/master/V3%28Recommended%29/EN/aster-finance-futures-api-v3.md#v3-nonce-mechanism)
limits those signatures to sixty seconds from their microsecond timestamp.
The old operation is then recorded as `configuration_not_effective`, with the
observed settings and clock evidence. Its original signed requests stay intact.

Only an entry with no entry-operation history, no fills and no staged orders
may make one new configuration attempt. It uses a separate operation identity;
all current source, deadline, ranking, market, ownership, account and transport
checks still apply. A second ineffective attempt terminates the entry. Foreign
exposure blocks continuation. The original quantity and risk are not increased.

This rule is specific to configuration. An uncertain order remains uncertain
after nonce expiry or an empty order query and is never resubmitted by this
mechanism. Terminal operation records are preserved on subsequent reconciliation.
Transport failures retain a bounded exception category and HTTP status, without
provider response bodies or signed credentials.

Tests cover lost replies before and after application, partial configuration,
unchanged settings, expired signatures, wrong-market and stale-clock evidence,
bounded retries, manual positions, and uncertain entries. All transports use
fake exchanges and isolated state.
