# Fresh entry observations and bounded budget recovery

Hyperliquid entry admission retains the two-second price proof and ten-second
complete account proof. Mark context and order book are read concurrently. An
expired pair gets one complete new read, bounded by the same two-second window;
an invalid market or malformed response is not converted into permission.

Shared accounts with multiple DEX scopes use the existing complete WebSocket
inventory, including every expected position and order scope. Nonempty order
scopes still require REST details proving reduce-only status. Missing scopes,
wrong accounts, stale timestamps and unsupported account modes continue to block
entry. Financial balances and exposure are never replaced by a cached budget.

The old small-inventory REST branch scaled badly: eleven DEX scopes required
302 request-weight units for each account observation, before signer, market and
price checks. The official [Hyperliquid rate-limit contract](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/rate-limits-and-user-limits)
sets a shared REST budget of 1,200 units per minute per IP. The stream removes
the per-DEX REST fan-out without removing any execution guard.

An entry already admitted locally may reuse the existing single budget refresh
only while it is preparing, has no fills, no entry orders or entry-operation
history, and any existing leverage operation is positively accepted. The
refresh preserves the immutable original plan and links one smaller successor
atomically. Quantity, notional, risk and original deadline cannot increase.
Current source, configuration, ownership, account and final transport checks
still apply. A second refresh is forbidden. A manually placed position, an
unknown send or a terminal trade cannot be reopened by this mechanism.

Express admission repeats original evidence checks directly over the current
source instead of rebuilding neighbouring context. Delayed notifications use
the current target/trade outcome; terminal outcomes end outstanding retry hints.
Recovery also repairs older terminal hints without changing fills or sending
orders. Parser coverage, source interpretation, ranking and risk settings are
unchanged.

Regression checks use MockTransport, fake WebSocket frames and isolated SQLite
state. No real test orders or paid model calls are required for this change.
