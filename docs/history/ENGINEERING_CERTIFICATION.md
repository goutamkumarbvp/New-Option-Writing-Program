# V2.0.1 Engineering Conformance Certificate

## Scope
This certificate is limited to the defined internal engineering acceptance criteria for the Institutional Options Risk Terminal. It is **not** a guarantee of trading profit and it is **not** a certification of external broker/exchange behavior.

## Mandatory criteria
1. Source compilation succeeds.
2. Regression and certification tests pass.
3. No synthetic market data or fabricated broker success is generated.
4. Missing/stale/crossed/out-of-order market data fails closed.
5. Strategy cannot approve without live evidence and a validated order.
6. Manual/Auto approval policy is enforced.
7. Two independent risk evidence domains can veto execution.
8. Soft/hard portfolio stops and exposure/margin/order limits are enforced.
9. Kill switch blocks execution.
10. Persistent orders/fills/positions/events/reconciliation state exists.
11. Fill recording is idempotent.
12. Submitted and filled are distinct states.
13. Broker reconciliation detects local/remote order mismatches.
14. Secrets are runtime-only and excluded from the artifact.
15. Artifact is reproducible from source and passes packaging checks.

## Certification status
The certificate may be issued only if every criterion above and all automated tests pass in the release environment.

## Absolute boundary
No engineering certificate can guarantee future trading profits. Real-money certification additionally requires controlled authenticated tests against each user's broker accounts, exchange data, network conditions, and operational environment.
