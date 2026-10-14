# Execution outcome views

Source event `status="executed"` is a consumed source binding. It is retained for
compatibility and immutability; it never confirms an entry order or fill.
Readers must use the additive `execution_outcome` projection instead.

- `not_sent`: a rejected/cancelled zero-fill trade has an explicit unsent
  operation, or no entry operation and no admitted orders. A preview rejected
  before trade admission also has no entry.
- `planned`, `waiting_entry`, `pending_entry`: saved work, with no confirmed
  entry fill. A plan ID or trade ID alone is never fill evidence.
- `pending_observation`: a durable technical read retry is pending. Its original
  deadline is retained. Preview retries do not authorize execution.
- `filled`: retained own entry-fill receipts confirm entry history. Current
  ownership is reported separately, only if receipt quantities reconcile.
- `closed`: receipt-backed own entry and exit quantities reconcile to zero
  and the workflow is terminal. It remains a historically filled trade.
- `no_entry_fill`: a terminal order has no retained own entry fill; this does not
  say it was never sent.
- `unknown`, `conflicted`: send outcome, historical receipts or current
  ownership cannot be confirmed. A legacy scalar `filled_quantity` without
  receipts does not repair this. A manual position conflict hides the current
  own remaining quantity without erasing confirmed historical entry fills.
  Any prepared, sending or unknown associated entry attempt keeps ownership
  unconfirmed, even if a newer attempt is explicitly unsent. Historical unsent
  attempts do not obscure a subsequently confirmed entry. Associations use
  exact `entry:<trade_id>` identities and colon-delimited attempt suffixes;
  unrelated SL, close, leverage and other-trade operations are excluded.
- `source_revision_unconfirmed`, `update_only`: a source revision cannot use
  another revision's entry fills as its own execution proof, and an SL/TP update
  does not prove a new entry. Existing fills appear under `bound_trade_history`.

`confirmed_entry_quantity` is historical and `known_own_remaining_quantity` is
current. Different exchange/network/account/pipeline/currency targets remain
separate. Neither quantity comes from a fresh exchange inventory read.
`source_revision_matches` identifies the exact immutable revision used by a
target; a later source edit is not silently treated as that original signal.

Status summaries expose `execution_counts.known_own_open`, `pending_entries`
and `unsettled_ownership`. `open_trade_count` counts only `known_own_open`;
`active_trade_count` includes pending entries and unresolved ownership, while
`conflicted_trade_count` identifies the conflict cases separately. The Telegram
status labels this distinction explicitly; these are not exchange-wide counts.

Full/decision/batch source reads, classification acknowledgments, source lookup
and selected diagnostics expose the same projection without state mutation.
Compact readers retain their byte budget and versioned continuation contract.
Notification IDs, immutable facts, versions, cursors and ACK semantics remain
unchanged. Retry cards use only bounded diagnostic categories and timestamps;
they neither expose provider traces nor claim final rejection while a retry is
pending. Historical stale-price errors do not imply reuse of a preview quote.

`entry_diagnostic` exposes only a bounded code, stage, category, exception class
and boolean retryability. It can come from a failure saved on the trade before
an operation existed or from the current operation's result. Provider messages,
traces and extra diagnostic fields stay out of public projections; stored
receipts are unchanged. Legacy failures without structured proof remain null.
