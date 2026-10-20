# Missing source fields are separate from execution permission

A source can describe an unambiguous entry without specifying its stop, targets
or leverage. The interpretation preserves absent values as `null` or `[]`.
`trade_preview` alone applies the configured Backup profile or rejects an
unexecutable plan. An absent source stop does not require a new operator rule
when Backup already supplies it.

Explicit new source targets such as `50% / 100%` remain two `roe_percent`
targets under `source-roe-v1`. Their prices use effective margin leverage and
the confirmed own entry VWAP. Missing absolute target prices do not erase the
targets or replace them with Backup targets.

The shared classification check rejects `ambiguous`/`incomplete` drafts whose
stated cause is only missing execution inputs or the already defined TP
percentage basis. The checker returns a concrete repair; single and batch
classification writes and preview recheck the same policy before committing.
The check never converts a classification to an order or invents a source value.
Independent missing identity, contradictory values, leverage intervals,
unsupported entry/management structures and binding TP intervals remain blocked.

Read receipts, exact evidence, source revisions and original deadlines still
apply. Backup configuration, effective leverage, exchange minimums, ownership,
account limits and risk budgets are unchanged. A corrected interpretation is
neither an executable plan nor an exchange acknowledgement.

Regression tests use synthetic originals, isolated state and fake exchanges.
They cover configured/unconfigured Backup, retained ROE targets, non-actional
write paths, real independent ambiguity, post-fill protection and repeated
recovery without duplicate sends. Private originals and model observations stay
outside the repository.

Final entry admission retains the two-second price-proof limit. If local audit
writes or the final admission check age a previously fresh quote, the guard may
read it once more and repeat all admission and price checks. Persistent delay,
a changed source/configuration, expired deadlines or exhausted retries still
block entry. Reading current account limits uses only their existing state roots;
it does not copy source bodies or financial history and never caches the limits.
