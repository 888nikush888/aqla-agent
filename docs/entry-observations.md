# Entry observations and bounded recovery

Entries keep the existing 2 second price, 10 second account and 9 second large-DEX stream limits. Original source deadlines, risk/margin limits, source/rank/configuration bindings, maker policy, position ownership and protective order rules remain mandatory.

`TemporaryObservation` represents a verified freshness or read-only transport failure. Wrong accounts, malformed scopes, invalid market/SL/TP structure, risk/margin/admission changes and unclassified exceptions remain closed. Diagnostics retain stage, code, category and a bounded exception type; transport/provider messages are discarded.

## Final transport boundary

Signing and expensive local preparation precede another observation. A committed `sending` operation still precedes the only transport attempt. After that durable write, the live guard rechecks account admission and price; its final synchronous guard rechecks current state and quote age after snapshot copying, immediately before transport. A slow audit write can cause one bounded price re-observation without weakening the 2 second limit. Account reuse is limited to a still-fresh snapshot inside the current admission phase; signer and prepared boundaries force fresh account reads.

A locally blocked transport is recorded as `rejected / not_sent`. A crash after `sending`, or any error after the actual send starts, remains ambiguous and requires exact reconciliation. A signed Hyperliquid packet expiring before any transport is a separate temporary failure from the original signal deadline.

Large-DEX observation consumes one followup aggregate on the existing subscription when its initial state is stale or unclocked. Every required order scope must already be complete inside the same 9 second observation. Nonempty REST details are checked again against the exchange state age. Wrong users or invalid scopes never qualify for a followup or retry.

## Authority and retry records

A preview read failure remains source-ready with a target `observation_retry` whose authority is `preview`. A successful saved preview alone never grants background execution. The original source time and profile deadline remain binding. The current configuration/ranking revisions fence preview retries; a durable in-flight preview lease prevents concurrent calls bypassing the backoff.

Only an explicitly requested `trade_execute` can create retry authority `execute`. Its plan stays immutable. The retry metadata lives beside the saved plan and on its owned, zero-fill trade. Risk and market capacity remain held while waiting. Failed observations have at most five attempts and wait 30, 60, 120 and 300 seconds, capped by the original deadline. Repeated calls cannot advance that clock or count. The backend's normal account recovery resumes eligible work without another model decision. Its separate recovery lock and atomic durable retry claim fence concurrent recoveries and interrupted attempts.

Previously rejected/unmarked records are never selected for retry. An accepted/partially accepted/unknown entry or unresolved operation cannot be rearmed. A known `not_sent` request with no accepted legs may use a new attempt identity, client order ID and nonce; the exact old request, wire and outcome stay unchanged in history. Accepted leverage configuration is reused. Once transport may have started, retry authority is completed and reconciliation takes over.

Expiration, retry exhaustion and changed source/rank/profile/pipeline/account permissions terminate unsent work conservatively. Paused entries cannot send. Protection and reduce-only legs after an accepted entry remain independent of new-entry guards, deadlines and maker restrictions.

## Offline verification

The focused regression suite covers delayed signing/audit/sending writes, changes at the final dispatch boundary, SQLite restart and concurrent recovery, accepted leverage reuse, immutable prior requests, lost responses, retry exhaustion, original deadlines, preview-only authority and safe diagnostic markers. Native payload tests cover Hyperliquid, Aster and Lighter, including Aster protection after a pause immediately following entry acceptance. Inventory tests cover stale/unclocked-to-fresh followups without duplicate subscriptions and stale state after REST details. Tests use synthetic transport only.
