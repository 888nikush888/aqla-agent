# Durable journal synchronization

Replacing an entry attempt or a protection order can remove its client ID from
the current trade. Its last complete public `Orders` row now remains in
`journal_order_archive`, including its original account, network, values and
fingerprint. It remains exportable after restart and after acknowledgement.
Current orders take precedence if the same ID becomes current again. Archived
rows are journal evidence only; they never authorize an order or change exposure.
Operation records retain the definitive sent/not-sent outcome.

The state also saves the fingerprints of the rows it has queued in
`journal_projection`. This is a projection checkpoint, not a delivery receipt.
An unchanged restart preserves the existing queue. Changed renderings or changes
made while export was disabled are queued on startup. A changed spreadsheet,
namespace or execution-account destination requires a full refresh. Existing
states without a checkpoint receive one complete refresh on upgrade.

Delivery still requires the current row and pending generation to match the
fingerprint accepted by Sheets. A lost reply is recovered by the existing
ID/readback transaction. No missing row is silently acknowledged or discarded;
a pending queue with no renderable records produces an explicit warning.

For legacy rows lost before archiving existed, recover the exact original row
from a private state backup or a verified audit-journal row. Validate its schema,
ID, execution scope and fingerprint against the durable pending marker before
adding it to the archive through a stopped service's state transaction. Keep the
proof private and let normal synchronization acknowledge it. Do not reconstruct
prices from current market data, invent order outcomes or reset financial state.

Regression coverage uses isolated SQLite and fake Sheets/exchanges: replaced
entries, TPs and exits; stale acknowledgements; lost replies; repeated restarts;
changed projections and destinations; disabled export; and network cutover.
