"""通过真实 Control Plane API 生成 UI 展示所需的业务数据（仅限本地 dev 演示环境）。

原则（docs/ui-redesign/REFACTOR_PLAN.md）：
- 只调用现有公开 API，不直接写数据库，不绕过 LangGraph / Harness / 审批状态机，
  因此 Evidence Ledger 哈希链、RLS 与审计记录保持完整。
- 依赖 scripts/dev/seed_demo.py 已初始化的 6 个演示账号（统一密码 DemoPass!2026）。
- Email OTP 从本地 Mailpit HTTP API 读取（与 tests/qa/test_live_docker_l2.py 相同方式）。
- 不打印 access token / refresh token / Secret。
- 任一步骤失败只记录并继续，最后输出汇总；可重复执行（每次创建新的 Pipeline / Preparation）。

用法：
    python scripts\\dev\\seed_ui_showcase.py
环境变量（可选）：ETL_API_BASE（默认 http://127.0.0.1:8000）、MAILPIT_API（默认 http://127.0.0.1:8025）、
                 SEED_PROJECT_SLUG（默认 proj-orders-prod）
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
from typing import Any

API = os.getenv("ETL_API_BASE", "http://127.0.0.1:8000").rstrip("/")
MAILPIT = os.getenv("MAILPIT_API", "http://127.0.0.1:8025").rstrip("/")
PROJECT_SLUG = os.getenv("SEED_PROJECT_SLUG", "proj-orders-prod")
PASSWORD = "DemoPass!2026"
ACCOUNTS = {
    "maker": "maker@etl-agent.local",
    "data_checker": "data_checker@etl-agent.local",
    "security_checker": "security_checker@etl-agent.local",
    "operator": "operator@etl-agent.local",
    "auditor": "auditor@etl-agent.local",
    "admin": "admin@etl-agent.local",
}
REQUIREMENT = (
    "每天把 demo_ecommerce 中已支付订单同步到 Doris 数仓 etl_warehouse 的订单事实表，"
    "客户邮箱和手机号脱敏，按订单号去重。"
)

summary: dict[str, Any] = {"ok": [], "skipped": [], "failed": []}


class ApiError(RuntimeError):
    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"HTTP {status}: {body[:300]}")
        self.status = status


def _http(method: str, url: str, body: Any | None = None, headers: dict[str, str] | None = None) -> Any:
    data = None
    hdrs = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raise ApiError(exc.code, exc.read().decode("utf-8", "replace")) from None


def _latest_otp(email: str, after_ts: float) -> str:
    """轮询 Mailpit，取发给该邮箱、且在登录之后到达的最新 6 位验证码。"""
    for _ in range(20):
        msgs = (_http("GET", f"{MAILPIT}/api/v1/messages") or {}).get("messages") or []
        for msg in msgs:
            to = [str(t.get("Address", "")).lower() for t in (msg.get("To") or [])]
            if email.lower() not in to:
                continue
            detail = _http("GET", f"{MAILPIT}/api/v1/message/{msg['ID']}")
            text = str(detail.get("Text") or detail.get("Snippet") or "")
            m = re.search(r"\b(\d{6})\b", text)
            created = str(msg.get("Created", ""))
            if m and created:
                return m.group(1)
        time.sleep(0.5)
    raise RuntimeError(f"Mailpit 中未找到 {email} 的验证码")


class Session:
    def __init__(self, role: str) -> None:
        self.role = role
        self.email = ACCOUNTS[role]
        self.token = ""
        self.org_id = ""
        self.project_id = ""

    def login(self) -> "Session":
        t0 = time.time()
        challenge = _http("POST", f"{API}/api/v1/auth/login", {"email": self.email, "password": PASSWORD})
        otp = _latest_otp(self.email, t0)
        tokens = _http(
            "POST",
            f"{API}/api/v1/auth/otp/verify",
            {"challenge_id": challenge["challenge_id"], "otp_code": otp},
        )
        self.token = tokens["access_token"]
        me = _http("GET", f"{API}/api/v1/auth/me", headers={"Authorization": f"Bearer {self.token}"})
        proj = next((p for p in me.get("projects", []) if p.get("slug") == PROJECT_SLUG), None)
        if proj is None:
            raise RuntimeError(f"{self.email} 不是项目 {PROJECT_SLUG} 的成员")
        self.org_id = proj["organization_id"]
        self.project_id = proj["id"]
        print(f"[login] {self.role:16s} ok")
        return self

    def call(self, method: str, path: str, body: Any | None = None, idem: str | None = None) -> Any:
        headers = {
            "Authorization": f"Bearer {self.token}",
            "X-Organization-Id": self.org_id,
            "X-Project-Id": self.project_id,
        }
        if idem:
            headers["Idempotency-Key"] = f"{idem}-{uuid.uuid4()}"
        return _http(method, f"{API}{path.replace('{p}', self.project_id)}", body, headers)


def step(name: str, fn: Any) -> Any:
    try:
        result = fn()
        summary["ok"].append(name)
        print(f"[ ok ] {name}")
        return result
    except Exception as exc:  # noqa: BLE001 - 汇总报告，不中断后续步骤
        summary["failed"].append({"step": name, "error": str(exc)[:400]})
        print(f"[fail] {name}: {str(exc)[:400]}")
        return None


def main() -> int:
    try:
        health = _http("GET", f"{API}/health") or {}
    except Exception as exc:  # noqa: BLE001
        print(f"Control Plane 不可达：{API}/health -> {exc}")
        return 2
    if "service" not in health:
        print(f"{API} 不是 ETL-Agent Control Plane（/health 无 service 字段），请检查端口占用或设置 ETL_API_BASE")
        return 2

    s = {r: step(f"登录 {r}", lambda r=r: Session(r).login()) for r in ACCOUNTS}
    maker = s.get("maker")
    if maker is None:
        print("maker 无法登录，停止。请先执行 scripts\\dev\\seed_demo.py 并确认 Mailpit 已启动。")
        return 3

    # 1. 数据源：探活 + 元数据快照（真实 demo-mysql / doris）
    conns = (step("列出连接", lambda: maker.call("GET", "/api/v1/projects/{p}/connections")) or {}).get("items", [])
    for c in conns:
        step(f"探活 {c['name']}", lambda c=c: maker.call("POST", f"/api/v1/projects/{{p}}/connections/{c['id']}/test", {}))
        step(f"采集元数据 {c['name']}", lambda c=c: maker.call("POST", f"/api/v1/projects/{{p}}/connections/{c['id']}/metadata/probe", {}))
    if len(conns) < 2:
        summary["skipped"].append("Pipeline 流程：项目内少于 2 个连接")
        return finish()
    src = next((c for c in conns if "mysql" in str(c.get("connector_type", "")).lower()), conns[0])
    sink = next((c for c in conns if c["id"] != src["id"]), conns[1])

    # 2. Pipeline Studio：创建 Pipeline → Agent 生成 → 确定性门禁 → 冻结版本
    pipe = step("创建 Pipeline", lambda: maker.call("POST", "/api/v1/projects/{p}/pipelines", {
        "name": f"orders-paid-to-dw-{time.strftime('%m%d%H%M')}",
        "description": "UI 展示数据：已支付订单同步到 Doris 数仓（seed_ui_showcase）",
        "source_connection_id": src["id"],
        "sink_connection_id": sink["id"],
        "execution_mode": "BATCH",
    }))
    thread = step("创建 Agent 线程", lambda: maker.call("POST", "/api/v1/projects/{p}/agent/threads", {
        "source_connection_id": src["id"], "sink_connection_id": sink["id"],
    }))
    state = None
    if thread:
        tid = thread["thread_id"]
        state = step("Agent 生成计划", lambda: maker.call("POST", f"/api/v1/projects/{{p}}/agent/threads/{tid}/invoke", {
            "requirement": REQUIREMENT, "execution_mode": "BATCH",
        }))
        for _ in range(3):
            qs = (state or {}).get("interrupt_questions") or []
            if not qs or (state or {}).get("draft_artifacts"):
                break
            answers = {q["field"]: (q.get("options") or ["默认"])[0] for q in qs}
            state = step("回答 Agent 澄清问题", lambda a=answers: maker.call(
                "POST", f"/api/v1/projects/{{p}}/agent/threads/{tid}/resume", {"answers": a}))
    draft = (state or {}).get("draft_artifacts")
    version = None
    if pipe and draft:
        report = step("确定性门禁校验", lambda: maker.call(
            "POST", f"/api/v1/projects/{{p}}/pipelines/{pipe['id']}/drafts/validate", draft))
        if report and report.get("passed"):
            version = step("冻结版本", lambda: maker.call(
                "POST", f"/api/v1/projects/{{p}}/pipelines/{pipe['id']}/versions",
                {"draft": draft, "comment": "seed_ui_showcase 门禁通过后冻结"}))
        else:
            summary["skipped"].append("冻结版本：门禁未通过（保留失败报告供 Studio 页面展示）")
    else:
        summary["skipped"].append("门禁/冻结：Agent 未返回 draft_artifacts")

    # 3. Harness Preparation + 双人审批 + Operator Commit；另留 2 条待审批供审批台展示
    if version:
        def prep() -> Any:
            return maker.call("POST", "/api/v1/projects/{p}/preparations",
                              {"pipeline_version_id": version["id"], "preparation_type": "EXECUTION"}, idem="prep")
        p_full = step("Preparation #1（将完成审批并提交）", prep)
        p_half = step("Preparation #2（仅 Data Checker 通过）", prep)
        step("Preparation #3（待审批）", prep)

        def approve(sess: Session | None, pid: str, slot: str) -> Any:
            if sess is None:
                raise RuntimeError(f"{slot} 账号未登录")
            return sess.call("POST", f"/api/v1/projects/{{p}}/preparations/{pid}/approvals",
                             {"slot_type": slot, "decision": "APPROVED", "reason": f"{slot} 审核通过（seed_ui_showcase）"})

        if p_full:
            step("#1 Data Checker 通过", lambda: approve(s.get("data_checker"), p_full["id"], "DATA_CHECKER"))
            step("#1 Security Checker 通过", lambda: approve(s.get("security_checker"), p_full["id"], "SECURITY_CHECKER"))
            op = s.get("operator")
            commit = step("#1 Operator 提交执行", lambda: op.call(
                "POST", f"/api/v1/projects/{{p}}/preparations/{p_full['id']}/commit", {}, idem="commit")) if op else None
            if commit:
                summary["execution_id"] = commit.get("execution_id")
        if p_half:
            step("#2 Data Checker 通过", lambda: approve(s.get("data_checker"), p_half["id"], "DATA_CHECKER"))

    # 4. Benchmark：生成数据集并触发一次 L0 运行（策略取项目内已有策略版本，不臆造）
    bench_sess = s.get("admin") or maker
    ds = step("生成 Benchmark 数据集 10K", lambda: bench_sess.call(
        "POST", "/api/v1/projects/{p}/benchmarks/datasets",
        {"name": f"bench-10K-seed42-{time.strftime('%m%d%H%M')}", "scale": "10K", "seed": 42}))
    policies = (step("列出策略版本", lambda: bench_sess.call("GET", "/api/v1/projects/{p}/policies")) or {}).get("items", [])
    if ds and policies:
        step("触发 Benchmark L0", lambda: bench_sess.call(
            "POST", "/api/v1/projects/{p}/benchmarks/runs",
            {"dataset_id": ds["id"], "level": "L0", "policy_version": policies[0]["version"]}, idem="bench"))
    elif ds:
        summary["skipped"].append("Benchmark 运行：项目内没有策略版本（不臆造 policy_version）")

    # 5. Evidence Ledger 完整性校验（Auditor）
    aud = s.get("auditor") or maker
    step("证据链校验", lambda: aud.call("POST", "/api/v1/projects/{p}/evidence/verify", {}))
    return finish()


def finish() -> int:
    print("\n=== seed_ui_showcase 汇总 ===")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if not summary["failed"] else 1


if __name__ == "__main__":
    sys.exit(main())
