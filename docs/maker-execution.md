# Maker execution

Each trading profile can set `entry_execution` and `take_profit_execution` to
`standard` or `maker_only`. Both default to `standard`. The saved trade plan
freezes both modes together with its policy. Changes apply to future plans;
existing admission, source, revision, binding and account risk checks still apply.
Default fields are omitted from serialization so old standard plan IDs and JSON
remain compatible.

Maker entries and normal targets use the native post-only limit instruction:
Hyperliquid `Alo`, Aster `GTX`, and Lighter `POST_ONLY` (`2`). Unsupported native
instructions fail closed. Stops, triggers and emergency/authorized position
reductions retain their existing trigger or IOC instruction.

Source limits and ranges remain binding. A market entry intent selects the fresh
passive bid for a long, or ask for a short, rounded away from crossing on the native
tick grid. Its reference, sizing, reserve and targets are calculated from that
price within the existing source slippage and risk limits. A later quote must
still be fresh and passive before the single send. The saved price never chases
the book. Crossing rejections do not create a replacement send or taker fallback.
Resting entries retain the original deadline. Partial fills require confirmed
remainder cancellation, reconciled owned quantity, and a stop for the actual
remaining quantity. An uncertain cancellation preserves existing protection.

Maker targets retain the calculated target price and native reduce-only flag.
Rejected targets remain unfilled, are visible in status/Telegram messages, and
retain the confirmed stop. A new source-authorized target update remains possible
through the existing management path; there is no implicit resubmission.
The active rejection identifies the current TP operation and its own target
identities. Retired generations cannot reassert it during recovery. A replacement
ACK alone does not clear it: every current TP needs a positive own order/market
observation with an accepted status. Rejected operation records and historical
notifications remain intact. Closed/closing cards do not present a TP rejection
as current or claim a live stop; active cards claim retained stop protection only
when the latest own stop observation covers the remaining quantity and price.

Actual fill role is native evidence: strict Hyperliquid `crossed`, Aster `maker`,
or Lighter `is_maker_ask` combined with the owning account side. Planned mode and
fee amount do not prove role. The additive `trade.fill_execution_roles` map uses
the existing stable `fill_events` identity, with `maker`, `taker`, or `unknown`.
Missing later role evidence preserves a known role. Contradictory known evidence
sets `unknown` permanently and records `fill_execution_role_conflicts`.
Financial fill records, entry/funding fingerprints and immutable lifecycle facts
remain unchanged. Read views may join the role sidecar as `execution_role`.
Missing fee evidence remains incomplete.

Regression evidence includes 72 complete frozen standard plans from release
`4253019` and 72 complete maker-mode regression snapshots. The maker snapshots
freeze all plan JSON, IDs and Decimal representations; independent wire,
side/tick, slippage, partial quantity and risk assertions check their execution
meaning. All verification is offline using generated test signers and local mock
transports, including the installed Lighter native signer.
