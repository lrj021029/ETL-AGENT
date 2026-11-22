"""Tool Broker: the sole side-effect gateway. Commit / Rollback / Schedule all use ONE atomic
transaction: capability_issuances + capability_consumptions(jti) + execution_runs +
execution_commands + outbox_events + outbox_deliveries + evidence_events (ADR-003)."""
from __future__ import annotations

import copy
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from app.approvals import AUTO_APPROVER, ApprovalEngine
from app.capabilities import CapabilityIssuer, CapabilityVerifier, ReplayGuard, TOOL_AUDIENCE
from app.evidence import EvidenceLedger, InMemoryLedgerStore
from app.policy import PolicyEngine
from app.preparations import InMemoryPreparationStore, Preparation, PreparationService, StandingSchedulePreparation
from app.security.canonical import canonical_digest
from app.security.errors import HarnessError

from app.capabilities import InMemoryConsumptionStore

TOOL_EVENT = {
    "seatunnel.submit_batch": "EXECUTION_SUBMIT_BATCH", "seatunnel.submit_cdc": "EXECUTION_SUBMIT_CDC",
    "seatunnel.cancel_job": "EXECUTION_CANCEL", "doris.atomic_swap": "EXECUTION_PUBLISH_SWAP",
    "rollback.shadow_cleanup": "ROLLBACK_SHADOW_CLEANUP", "rollback.swap_restore": "ROLLBACK_SWAP_RESTORE",
    "rollback.cdc_recover": "ROLLBACK_CDC_RECOVER",
}
SUBMIT_TOOLS = {"seatunnel.submit_batch", "seatunnel.submit_cdc", "seatunnel.cancel_job", "doris.atomic_swap"}
ROLLBACK_TOOLS = {"rollback.shadow_cleanup", "rollback.swap_restore", "rollback.cdc_recover"}
TERMINAL = {"SUCCEEDED", "CANCELLED", "INTERRUPTED", "FAILED", "ROLLED_BACK", "STOPPED"}
SCHEDULER = "system:scheduler"
_DENY_CODES = ("PDP_DENIED_P0", "APPROVAL_SELF_APPROVE_FORBIDDEN", "APPROVAL_SLOT_CONFLICT", "SOD_VIOLATION", "PERMISSION_DENIED")


class InMemoryBrokerStore:
    """Transactional in-memory stand-in for the PostgreSQL tables. `transaction()` is
    all-or-nothing and serialized (models SERIALIZABLE / row locks). Bind a SQLAlchemy
    implementation with the same methods in integration."""

    def __init__(self) -> None:
        self.preps = InMemoryPreparationStore()
        self.consumptions = InMemoryConsumptionStore()
        self.ledger = InMemoryLedgerStore()
        self.issuances: dict[str, dict] = {}
        self.runs: dict[str, dict] = {}
        self.commands: dict[str, dict] = {}
        self.outbox_events: dict[str, dict] = {}
        self.outbox_deliveries: dict[str, dict] = {}
        self.idempotency: dict[tuple, dict] = {}
        self._lock = threading.RLock()

    def _state(self) -> dict:
        return {
            "preps": self.preps._d, "cons": self.consumptions.rows, "chains": self.ledger.chains,
            "cps": self.ledger.cps, "iss": self.issuances, "runs": self.runs, "cmds": self.commands,
            "ob": self.outbox_events, "obd": self.outbox_deliveries, "idem": self.idempotency,
        }

    @contextmanager
    def transaction(self):
        with self._lock:
            snap = copy.deepcopy(self._state())
            try:
                yield self
            except BaseException:
                self.preps._d = snap["preps"]
                self.consumptions.rows = snap["cons"]
                self.ledger.chains, self.ledger.cps = snap["chains"], snap["cps"]
                self.issuances, self.runs, self.commands = snap["iss"], snap["runs"], snap["cmds"]
                self.outbox_events, self.outbox_deliveries, self.idempotency = snap["ob"], snap["obd"], snap["idem"]
                raise

    def claim_command(self, command_id: str) -> bool:
        with self._lock:
            c = self.commands[command_id]
            if c["state"] != "PENDING":
                return False
            c["state"] = "DISPATCHING"
            return True


@dataclass
class CommitResult:
    execution_id: str
    command_id: str
    capability_jti: str
    capability_jws: str
    outbox_event_id: str
    idempotent_replay: bool = False


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class ToolBroker:
    def __init__(self, *, store: InMemoryBrokerStore, policy: PolicyEngine, preparations: PreparationService,
                 approvals: ApprovalEngine, issuer: CapabilityIssuer, replay: ReplayGuard, ledger: EvidenceLedger,
                 verifier: CapabilityVerifier, input_resolver: Callable[[Preparation], dict],
                 executors: dict[str, Callable[[dict], Any]] | None = None,
                 clock: Callable[[], float] = time.time, worker_grace_seconds: int = 3600):
        self.store, self.policy, self.preps, self.approvals = store, policy, preparations, approvals
        self.issuer, self.replay, self.ledger, self.resolve = issuer, replay, ledger, input_resolver
        self.executors = executors or {}
        self._clock = clock
        self._worker_verifier = CapabilityVerifier(verifier.keyring, clock=clock, leeway=worker_grace_seconds)
        self.verifier = verifier

    # ------------------------------------------------------------------ public API
    def commit(self, *, organization_id: str, project_id: str, preparation_id: str, operator: dict,
               idempotency_key: str, tool: str = "seatunnel.submit_batch", trace_id: str | None = None) -> CommitResult:
        if tool not in SUBMIT_TOOLS:
            raise HarnessError("AGENT_TOOL_FORBIDDEN", f"tool {tool} is not a commit tool")
        prep = self._load(preparation_id, organization_id, project_id)
        return self._core(prep, operator, "execution.commit", tool, idempotency_key, trace_id, one_shot=True)

    def authorize_rollback(self, *, organization_id: str, project_id: str, rollback_preparation_id: str,
                           execution_id: str, operator: dict, idempotency_key: str, tool: str,
                           trace_id: str | None = None) -> CommitResult:
        if tool not in ROLLBACK_TOOLS:
            raise HarnessError("AGENT_TOOL_FORBIDDEN", f"tool {tool} is not a rollback tool")
        run = self.store.runs.get(execution_id)
        if not run or (run["organization_id"], run["project_id"]) != (organization_id, project_id):
            raise HarnessError("TENANT_ACCESS_DENIED", "execution not found in tenant")
        prep = self._load(rollback_preparation_id, organization_id, project_id)
        if prep.inputs.get("pipeline_version_hash") != run["pipeline_version_hash"]:
            raise HarnessError("PREPARATION_INVALIDATED", "rollback preparation not bound to the execution's pipeline version")
        return self._core(prep, operator, "execution.rollback", tool, idempotency_key, trace_id, one_shot=True,
                          rollback_of=execution_id)

    def commit_scheduled(self, sp: StandingSchedulePreparation, fire_time: float, trace_id: str | None = None) -> CommitResult | None:
        """Celery Beat entry. Runs as `system:scheduler`; full re-verification and the same atomic txn."""
        now = self._clock()
        if sp.kind != "STANDING_SCHEDULE":
            raise HarnessError("PREPARATION_INVALIDATED", "not a standing schedule preparation")
        operator = {"user_id": SCHEDULER, "org_role": "system", "project_roles": [], "permissions": ["schedule.manage"],
                    "is_system_principal": True}
        key = f"schedule:{sp.preparation_id}:{int(fire_time)}"
        prior = self.store.idempotency.get((sp.organization_id, sp.project_id, SCHEDULER, key))
        if prior:
            return CommitResult(**{**prior["result"], "idempotent_replay": True})
        if sp.misfire_policy == "SKIP" and now - fire_time > sp.misfire_grace_seconds:
            self._evidence(sp, SCHEDULER, "SCHEDULE_FIRE_SKIPPED", {"reason": "MISFIRE_SKIP", "fire_time": int(fire_time)})
            return None
        if sp.concurrency_policy == "FORBID" and any(
                r["preparation_id"] == sp.preparation_id and r["status"] not in TERMINAL for r in self.store.runs.values()):
            self._evidence(sp, SCHEDULER, "SCHEDULE_FIRE_SKIPPED", {"reason": "CONCURRENCY_FORBID", "fire_time": int(fire_time)})
            return None
        if sp.spent_budget + sp.max_budget_per_run > sp.total_budget:
            raise HarnessError("PREPARATION_INVALIDATED", "standing schedule budget exhausted", {"reason": "budget_exceeded"})
        res = self._core(sp, operator, "schedule.commit", sp.inputs.get("tool", "seatunnel.submit_batch"), key,
                         trace_id, one_shot=False)
        sp.spent_budget += sp.max_budget_per_run
        return res

    # ------------------------------------------------------------------ worker side
    def handle_outbox(self, payload: dict) -> dict:
        """Celery worker entry (at-least-once). Context comes from the DB row, never the payload."""
        cmd = self.store.commands.get(payload.get("command_id"))
        if not cmd:
            raise HarnessError("TENANT_ACCESS_DENIED", "unknown command")
        if cmd["capability_jti"] != payload.get("capability_jti") or cmd["execution_id"] != payload.get("execution_id"):
            raise HarnessError("CAPABILITY_INVALID_SIGNATURE", "payload does not match command row")
        if payload.get("capability_jws") != self.store.issuances[cmd["capability_jti"]]["capability_jws"]:
            raise HarnessError("CAPABILITY_INVALID_SIGNATURE", "payload capability differs from issued capability")
        return self.execute_command(cmd["command_id"])

    def execute_command(self, command_id: str) -> dict:
        cmd = self.store.commands[command_id]
        run = self.store.runs[cmd["execution_id"]]
        iss = self.store.issuances[cmd["capability_jti"]]
        claims = self._worker_verifier.verify(iss["capability_jws"], {
            "organization_id": run["organization_id"], "project_id": run["project_id"],
            "environment": run["environment"], "tool": cmd["tool"], "preparation_id": run["preparation_id"],
            "pipeline_version_hash": run["pipeline_version_hash"], "aud": TOOL_AUDIENCE[cmd["tool"]],
        })
        if not self.store.consumptions.exists(claims["jti"]):
            raise HarnessError("CAPABILITY_INVALID_SIGNATURE", "capability was never consumed at commit")
        if not self.store.claim_command(command_id):
            # redelivery: idempotent, no second consumption and no second side effect
            return {"command_id": command_id, "state": cmd["state"], "idempotent": True, "result": cmd.get("result")}
        executor = self.executors.get(cmd["tool"])
        try:
            if executor is None:
                raise HarnessError("AGENT_TOOL_FORBIDDEN", f"no executor registered for {cmd['tool']}")
            result = executor({"command": dict(cmd), "run": dict(run), "claims": claims})
        except BaseException:
            cmd["state"] = "UNKNOWN"  # must be reconciled, never blindly re-run
            raise
        cmd["state"], cmd["result"] = "SUBMITTED", result
        run["status"] = "SUBMITTING"
        return {"command_id": command_id, "state": "SUBMITTED", "idempotent": False, "result": result}

    # ------------------------------------------------------------------ internals
    def _load(self, preparation_id: str, org: str, project: str) -> Preparation:
        prep = self.store.preps.get(preparation_id)
        if prep is None:
            raise HarnessError("PREPARATION_INVALIDATED", "preparation not found")
        if (prep.organization_id, prep.project_id) != (org, project):
            raise HarnessError("TENANT_ACCESS_DENIED", "preparation belongs to another tenant")
        return prep

    def _evidence(self, prep: Preparation, actor: str, event_type: str, payload: dict, resource_ref: str | None = None):
        return self.ledger.append(
            organization_id=prep.organization_id, project_id=prep.project_id, actor=actor, event_type=event_type,
            resource_ref=resource_ref or f"preparation:{prep.preparation_id}",
            artifact_hash=prep.inputs.get("artifact_hash"), payload=payload)

    def _core(self, prep: Preparation, operator: dict, action: str, tool: str, idem_key: str, trace_id: str | None,
              *, one_shot: bool, rollback_of: str | None = None) -> CommitResult:
        scope = (prep.organization_id, prep.project_id, operator["user_id"], idem_key)
        req_hash = canonical_digest({"prep": prep.preparation_id, "tool": tool, "action": action, "rb": rollback_of})
        prior = self.store.idempotency.get(scope)
        if prior:
            if prior["request_hash"] != req_hash:
                raise HarnessError("IDEMPOTENCY_CONFLICT", "Idempotency-Key reused with a different request")
            return CommitResult(**{**prior["result"], "idempotent_replay": True})
        if prep.status == "COMMITTED" and one_shot:
            raise HarnessError("CAPABILITY_REPLAY_DETECTED", "preparation already committed")
        if prep.status == "INVALIDATED":
            raise HarnessError("PREPARATION_INVALIDATED", "preparation invalidated")

        # 1. re-verify: fingerprint (pipeline hash, metadata, secret versions, policy), expiry
        current = self.resolve(prep)
        self.preps.assert_valid(prep, current)
        active_policy = self.policy.store.active.version
        if prep.policy_version != active_policy or current.get("policy_version") != active_policy:
            self._invalidate(prep)
            raise HarnessError("PREPARATION_INVALIDATED", "policy version changed")

        # 2. PDP re-evaluation at commit time
        req = {
            "subject": operator, "organization_id": prep.organization_id, "project_id": prep.project_id, "action": action,
            "resource": {"resource_type": "preparation", "resource_id": prep.preparation_id},
            "environment": prep.inputs.get("environment", "prod"),
            "data_classification": prep.inputs.get("data_classification", "RESTRICTED_PII"),
            "risk_level": prep.risk_level, "artifact_hash": prep.inputs.get("artifact_hash"),
            "approval_context": {"maker_id": prep.maker_id, "preparation_id": prep.preparation_id, "existing_approvers": {}},
        }
        pdp = self.policy.evaluate(req, policy_version=active_policy)
        if pdp.decision == "DENY":
            code = next((r for r in pdp.reason_codes if r in _DENY_CODES), "PERMISSION_DENIED")
            raise HarnessError(code, "commit denied by policy", {"reasons": pdp.reason_codes})
        if pdp.required_approval_slots != prep.required_slots:
            self._invalidate(prep)
            raise HarnessError("PREPARATION_INVALIDATED", "required approval slots changed under current policy")
        if pdp.decision == "ALLOW":  # P3: Broker enforces the SAME path; only the approval slot is automatic
            self.approvals.auto_approve_p3(prep, pdp, current)
        digest = self.approvals.digest(prep)

        # 3. sign capability + Redis fast-path replay guard
        jws, claims = self.issuer.issue(
            sub=operator["user_id"], tool=tool, organization_id=prep.organization_id, project_id=prep.project_id,
            environment=prep.inputs["environment"], preparation_id=prep.preparation_id,
            pipeline_version_hash=prep.inputs["pipeline_version_hash"], approval_digest=digest,
            policy_version=active_policy)
        self.replay.reserve(claims["jti"])

        # 4. ONE atomic transaction
        execution_id, command_id, event_id = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
        trace = trace_id or uuid.uuid4().hex
        now = self._clock()
        event_type = TOOL_EVENT[tool]
        payload = {
            "schema_version": "1.0.0", "event_type": event_type, "command_id": command_id, "execution_id": execution_id,
            "rollback_run_id": str(uuid.uuid4()) if rollback_of else None, "capability_jti": claims["jti"],
            "capability_jws": jws, "trace_id": trace, "created_at": _iso(now),
        }
        with self.store.transaction() as tx:
            if one_shot and not tx.preps.compare_and_set_status(prep.preparation_id, "PREPARED", "COMMITTED"):
                raise HarnessError("CAPABILITY_REPLAY_DETECTED", "preparation already committed (concurrent commit lost)")
            tx.issuances[claims["jti"]] = {"jti": claims["jti"], "capability_jws": jws, "claims": claims, "issued_at": now}
            self.replay.consume(claims["jti"], preparation_id=prep.preparation_id, command_id=command_id)
            tx.runs[execution_id] = {
                "execution_id": execution_id, "organization_id": prep.organization_id, "project_id": prep.project_id,
                "environment": prep.inputs["environment"], "preparation_id": prep.preparation_id,
                "pipeline_version_hash": prep.inputs["pipeline_version_hash"], "tool": tool, "status": "QUEUED",
                "rollback_of": rollback_of, "created_by": operator["user_id"],
            }
            tx.commands[command_id] = {
                "command_id": command_id, "execution_id": execution_id, "tool": tool, "capability_jti": claims["jti"],
                "state": "PENDING", "idempotency_key": idem_key,
            }
            tx.outbox_events[event_id] = {"event_id": event_id, "payload": payload, "created_at": now}
            tx.outbox_deliveries[event_id] = {"event_id": event_id, "status": "PENDING", "attempts": 0, "lease_until": None}
            self.ledger.append(
                organization_id=prep.organization_id, project_id=prep.project_id, actor=operator["user_id"],
                event_type="EXECUTION_COMMITTED" if not rollback_of else "ROLLBACK_AUTHORIZED",
                resource_ref=f"execution:{execution_id}", artifact_hash=prep.inputs.get("artifact_hash"),
                payload={"command_id": command_id, "jti": claims["jti"], "tool": tool, "approval_digest": digest,
                         "policy_version": active_policy, "preparation_id": prep.preparation_id})
            result = CommitResult(execution_id, command_id, claims["jti"], jws, event_id)
            tx.idempotency[scope] = {"request_hash": req_hash, "result": dict(result.__dict__)}
        return result

    def _invalidate(self, prep: Preparation) -> None:
        prep.status = "INVALIDATED"
        self.store.preps.save(prep)
