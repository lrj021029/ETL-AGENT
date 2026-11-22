import copy
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "backend"))

from app.approvals import ApprovalEngine, InMemoryApprovalStore  # noqa: E402
from app.broker import InMemoryBrokerStore, ToolBroker  # noqa: E402
from app.capabilities import CapabilityIssuer, CapabilityVerifier, InMemoryFastPath, ReplayGuard  # noqa: E402
from app.capabilities.signing import KeyRing, LocalEd25519Signer  # noqa: E402
from app.evidence import EvidenceLedger  # noqa: E402
from app.policy import PolicyEngine, PolicyStore  # noqa: E402
from app.preparations import PreparationService, StandingScheduleService  # noqa: E402

ORG = "11111111-1111-1111-1111-111111111111"
PROJ = "22222222-2222-2222-2222-222222222222"
H = lambda c: "sha256:" + c * 64  # noqa: E731


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def subject(uid, perms):
    return {"user_id": uid, "org_role": "member", "project_roles": [], "permissions": perms}


class World:
    def __init__(self, ledger_every=50):
        self.clock = Clock()
        self.store = InMemoryBrokerStore()
        self.policy = PolicyEngine(PolicyStore())
        self.preps = StandingScheduleService(self.store.preps, clock=self.clock)
        self.approvals = ApprovalEngine(InMemoryApprovalStore(), self.policy, self.preps, clock=self.clock)
        self.signer = LocalEd25519Signer("etl-agent-capability:v1")
        self.ring = KeyRing()
        self.signer.register(self.ring)
        self.ledger_signer = LocalEd25519Signer("etl-agent-evidence:v1")
        self.ledger_signer.register(self.ring)
        self.ledger = EvidenceLedger(self.store.ledger, self.ledger_signer, checkpoint_every=ledger_every)
        self.issuer = CapabilityIssuer(self.signer, clock=self.clock)
        self.verifier = CapabilityVerifier(self.ring, clock=self.clock)
        self.fast = InMemoryFastPath(clock=self.clock)
        self.replay = ReplayGuard(self.fast, self.store.consumptions)
        self.live = {}
        self.side_effects = []
        self.broker = ToolBroker(
            store=self.store, policy=self.policy, preparations=self.preps, approvals=self.approvals,
            issuer=self.issuer, replay=self.replay, ledger=self.ledger, verifier=self.verifier,
            input_resolver=lambda p: copy.deepcopy(self.live), clock=self.clock,
            executors={t: (lambda ctx, t=t: self.side_effects.append((t, ctx["command"]["command_id"])) or {"job": "j1"})
                       for t in ("seatunnel.submit_batch", "seatunnel.submit_cdc", "doris.atomic_swap",
                                 "rollback.shadow_cleanup", "rollback.swap_restore")},
        )
        self.maker = subject("maker", ["pipeline.prepare"])
        self.dc = subject("dc", ["approval.approve", "approval.data_checker"])
        self.sc = subject("sc", ["approval.approve", "approval.security_checker"])
        self.single = subject("single", ["approval.approve", "approval.single_checker"])
        self.operator = subject("operator", ["execution.commit", "execution.rollback"])
        self.set_live()

    def set_live(self, env="prod", cls="RESTRICTED_PII", **over):
        self.live = {
            "pipeline_version_hash": H("a"), "metadata_snapshot_fingerprint": H("b"), "secret_versions": {"SRC_PWD": 2},
            "policy_version": self.policy.store.active.version, "artifact_hash": H("c"), "environment": env,
            "data_classification": cls, "organization_id": ORG, "project_id": PROJ, "risk_level": None,
            "budget": {"per_run": 1}, "tool": "seatunnel.submit_batch",
        }
        self.live.update(over)

    def prepare(self, risk, slots):
        self.live["risk_level"] = risk
        return self.preps.prepare(maker_id="maker", inputs=copy.deepcopy(self.live), risk_level=risk, required_slots=slots)

    def prepare_p1(self):
        self.set_live("prod", "RESTRICTED_PII")
        return self.prepare("P1", ["DATA_CHECKER", "SECURITY_CHECKER"])

    def approve_p1(self, p):
        self.approvals.approve(p, approver=self.dc, slot="DATA_CHECKER", current_inputs=self.live)
        self.approvals.approve(p, approver=self.sc, slot="SECURITY_CHECKER", current_inputs=self.live)

    def commit(self, p, key="k1", operator=None, tool="seatunnel.submit_batch"):
        return self.broker.commit(organization_id=ORG, project_id=PROJ, preparation_id=p.preparation_id,
                                  operator=operator or self.operator, idempotency_key=key, tool=tool)


@pytest.fixture
def world():
    return World()


@pytest.fixture(scope="session")
def vectors():
    import json

    return json.loads((ROOT / "contracts/crypto/test_vectors.json").read_text(encoding="utf-8"))


def schema(name):
    import json

    return json.loads((ROOT / "contracts/harness" / name).read_text(encoding="utf-8"))
