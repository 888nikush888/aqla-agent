# Aster configuration recovery before entry

Aster prepares margin mode and leverage before any entry order. Missing replies
to those requests must not hold account reconciliation indefinitely. They also
must never authorize replaying an uncertain entry.

The HTTP error parser must recognize `-4046` as a structured exchange response,
even though margin handling correctly treats it as an already-effective no-op.
Using the no-op-aware rejection predicate to recognize HTTP error bodies had
incorrectly turned HTTP 400 / `-4046` into an uncertain outcome and prevented the
subsequent leverage request.

Configuration preparation first reads the exact native symbol in one-way mode.
It sends only settings that differ from the requested values. An already
effective margin mode or leverage does not require a POST.

For an uncertain configuration, recovery reads its current exact-symbol settings.
If they match, configuration is positively reconciled. Otherwise, recovery waits
for the exchange's fresh server clock to pass the old signature's nonce window
before permitting a corrective setting request. Aster's [official V3 contract](https://github.com/asterdex/api-docs/blob/master/V3%28Recommended%29/EN/aster-finance-futures-api-v3.md#v3-nonce-mechanism)
limits admission of those signatures to sixty seconds from their microsecond
timestamp. This is a cooldown, not proof that a previously accepted request
cannot still be queued. The old operation stays unresolved, with observed
settings and clock evidence. Its original signed requests stay intact.

Only an entry with no entry-operation history, no fills and no staged orders
may make one new configuration attempt. It uses a separate operation identity;
all current source, deadline, ranking, ownership, account and transport checks
still apply. Only this non-exposure correction may pass its exact matching
unresolved configuration parent; unrelated operations stay blocked. Normal entry
admission always sees the full ledger and remains blocked until positive
configuration confirmation. A second uncertain attempt causes no further writes
and remains unresolved. Foreign exposure blocks continuation. Quantity and risk
are not increased; market and price checks remain mandatory before entry.

This rule is specific to configuration. An uncertain order remains uncertain
after nonce expiry or an empty order query and is never resubmitted by this
mechanism. Terminal operation records are preserved on subsequent reconciliation.
Transport failures retain a bounded exception category and HTTP status, without
provider response bodies or signed credentials.
If the exchange explicitly rejects the initial stop, an accepted resting entry
is cancelled immediately through its existing exact identity.

Tests cover lost replies before and after application, partial configuration,
unchanged settings, expired signatures, wrong-market and stale-clock evidence,
bounded retries, manual positions, and uncertain entries. All transports use
fake exchanges and isolated state.
