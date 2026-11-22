# Security / Harness Kernel (M6) - implementation notes

Packages (all under `backend/app/`, import root = `backend/`):

| Package | Purpose |
|---|---|
| `security` | RFC8785-subset `canonical_bytes/canonical_digest/event_hash`, `redaction` (text/dict/logging filter), `HarnessError(code)` |
| `secrets` | `SecretProvider` protocol, `VaultKV2SecretProvider` (hvac-shaped client), `EncryptedPostgresSecretProvider` (Fernet/MultiFernet, ref-bound, rotation) over a `SecretRowStore`, masking |
| `policy` | Versioned `PolicyStore` (publish/diff/rollback-as-new-version, invariants: SoD enforced, P0 list kept) + `PolicyEngine.evaluate` (PdpRequest -> PdpResponse), custom-role SoD validation |
| `preparations` | `PreparationService.prepare` (no side effects), `fingerprint`, `classify_change`, `assert_valid`, `StandingSchedulePreparation` |
| `approvals` | `ApprovalEngine` (slots, maker self-approve block, dual-slot block, `approval_digest`, P3 auto slot) |
| `capabilities` | Ed25519 JWS issuer/verifier (15 claims, TTL<=300s), `LocalEd25519Signer`, `VaultTransitEd25519Signer` (Vault std-b64 -> Base64URL), `KeyRing`, `ReplayGuard` (Redis `SET NX EX 360` + PG unique `jti`) |
| `broker` | `ToolBroker.commit / authorize_rollback / commit_scheduled / handle_outbox / execute_command`; single atomic transaction |
| `evidence` | `EvidenceLedger` (per org+project chain), signed checkpoints every N, `verify_chain` |

## Integration bindings (Lead)
- `InMemoryBrokerStore` is the reference for the PG-backed store: `transaction()` must be one SERIALIZABLE/RLS-scoped PG txn covering
  `capability_issuances`, `capability_consumptions(jti PK)`, `execution_runs`, `execution_commands(command_id unique)`, `outbox_events`,
  `outbox_deliveries`, `evidence_events`, plus CAS `preparations.status PREPARED->COMMITTED` (UPDATE ... WHERE status='PREPARED').
  `claim_command` = `UPDATE execution_commands SET state='DISPATCHING' WHERE command_id=:id AND state='PENDING'` (rowcount==1).
- `SqlAlchemyConsumptionStore` joins a caller's Connection; `RedisFastPath(redis_client)`; `HvacTransitClient(hvac_client)` for Vault Transit.
- Vault public key for verification: `KeyRing.add_vault_public_key("etl-agent-capability:v1", keys["1"]["public_key"])`.
- Ledger `LedgerStore` protocol maps to `evidence_events` / `evidence_checkpoints`; appends must be unique on (org, project, sequence_no).

## Behavioural decisions worth knowing
- Capability is issued **and consumed** (jti inserted) in the commit transaction; the worker re-verifies signature+binding against DB rows
  (leeway `worker_grace_seconds`, default 3600 for outbox delay), requires the consumption row, and uses an atomic command claim for redelivery idempotency.
- A failed executor leaves the command `UNKNOWN` (state machine reconcile path); it is never blindly re-run.
- Concurrent/second commit of one Preparation with a different Idempotency-Key -> `CAPABILITY_REPLAY_DETECTED`; same key -> original result.
- Claim-binding mismatches (aud/tool/project/env/hash/digest) use `CAPABILITY_INVALID_SIGNATURE` (no dedicated contract code exists).
- Budget exhaustion on a standing schedule -> `PREPARATION_INVALIDATED` (reason `budget_exceeded`).
- PDP risk: environment/classification derived; caller-supplied `risk_level` can only escalate.
