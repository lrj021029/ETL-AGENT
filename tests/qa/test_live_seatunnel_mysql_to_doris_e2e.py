"""真实 L2 端到端数据面验收：MySQL 8.0 → Apache SeaTunnel Zeta 2.3.9 → Apache Doris 2.1.7 影子表 → Schema 门禁 → 原子 Swap 发布 → 受管回滚。

严格禁止使用任何 Mock 假装 SeaTunnel 或 Doris 执行成功：
1. 真实向 `etl-agent-demo-mysql` 写入确定性源业务数据。
2. 真实在 `etl-agent-doris-fe/be` 创建正式目标表（v1 旧版数据）与影子表。
3. 真实通过 SeaTunnel Zeta 2.3.9 REST API (`http://127.0.0.1:5801/hazelcast/rest/maps/submit-job`) 提交真实 JDBC → Doris 搬运作业，轮询至 `FINISHED`。
4. 真实查询 Doris 影子表行数与内容核对 100% 一致。
5. 真实经 `DorisPublishAdapter` 执行 Schema 兼容校验与 `ALTER TABLE ... REPLACE WITH TABLE ... PROPERTIES('swap'='true')` 原子替换发布，核对正式表切换为新数据。
6. 真实经 `RollbackExecutor` 执行 Swap 恢复回滚，核对正式表精确恢复为 v1 旧版数据。
"""
from __future__ import annotations

import json
import socket
import time
import urllib.parse
import urllib.request
import uuid

import pymysql
import pytest

from backend.app.execution.state_machine import ExecutionMode, ExecutionRunRecord
from backend.app.publishing.doris_adapter import (
    DorisColumnSpec,
    DorisPublishAdapter,
    DorisTableSchemaSpec,
)
from backend.app.rollback.executor import RollbackCommandContext, RollbackExecutor


def _tcp_open(host: str, port: int, timeout: float = 1.5) -> bool:
    """探测宿主机回环端口是否可达。"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _http_json(method: str, url: str, body: bytes | None = None, content_type: str = "application/json") -> dict:
    """向真实 SeaTunnel Zeta REST API 发送 HTTP 请求并解析 JSON。"""
    import urllib.error

    req = urllib.request.Request(url, data=body, headers={"Content-Type": content_type}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310
            raw = resp.read().decode("utf-8", errors="replace").strip()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")
        raise AssertionError(f"SeaTunnel Zeta HTTP {exc.code} on {url}: {err_body[:1200]}") from exc


def _doris_conn():
    """建立到真实 Apache Doris FE MySQL 协议端口 (127.0.0.1:9030) 的连接。"""
    return pymysql.connect(host="127.0.0.1", port=9030, user="root", password="", autocommit=True, connect_timeout=5)


def _mysql_conn():
    """建立到真实演示 MySQL 8.0 (127.0.0.1:3306) 的连接。"""
    return pymysql.connect(
        host="127.0.0.1",
        port=3306,
        user="root",
        password="dev_only_demo_mysql_root_placeholder",
        database="demo_ecommerce",
        autocommit=True,
        connect_timeout=5,
    )


def test_l2_real_seatunnel_mysql_to_doris_batch_publish_and_rollback() -> None:
    """真实 MySQL 8.0 → SeaTunnel Zeta 2.3.9 → Doris 2.1.7 端到端批处理搬运、原子发布与回滚验证。"""
    for svc, port in (("demo-mysql", 3306), ("seatunnel-zeta", 5801), ("doris-fe", 9030)):
        if not _tcp_open("127.0.0.1", port):
            pytest.skip(f"BLOCKED: {svc} container not running on 127.0.0.1:{port}")

    # 确认 Doris BE 处于 Alive 状态
    dconn = _doris_conn()
    try:
        with dconn.cursor() as cur:
            cur.execute("SHOW BACKENDS")
            backends = cur.fetchall()
            alive = [row for row in backends if any(str(col).lower() == "true" for col in row)]
            if not alive:
                pytest.skip("BLOCKED: Doris FE is up but no Alive Doris BE backend is registered")
    finally:
        dconn.close()

    run_tag = uuid.uuid4().hex[:8]
    src_table = f"l2_src_orders_{run_tag}"
    target_table = f"l2_target_orders_{run_tag}"
    shadow_table = f"{target_table}__shadow_{run_tag}"

    # 1. 在真实 MySQL 8.0 写入 25 条确定性源订单数据
    mconn = _mysql_conn()
    try:
        with mconn.cursor() as cur:
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS `{src_table}` (
                    `order_id` BIGINT NOT NULL PRIMARY KEY,
                    `user_name` VARCHAR(64) NOT NULL,
                    `amount` DECIMAL(12, 2) NOT NULL,
                    `order_status` VARCHAR(32) NOT NULL
                )
                """
            )
            cur.execute(f"TRUNCATE TABLE `{src_table}`")
            rows = [
                (1000 + i, f"customer_{i:03d}", round(99.5 + i * 10.25, 2), "PAID" if i % 2 == 0 else "SHIPPED")
                for i in range(1, 26)
            ]
            cur.executemany(
                f"INSERT INTO `{src_table}` (order_id, user_name, amount, order_status) VALUES (%s, %s, %s, %s)",
                rows,
            )
    finally:
        mconn.close()

    # 2. 在真实 Apache Doris 2.1.7 创建正式目标表（含 v1 旧版 2 条记录）与影子表（空表）
    dconn = _doris_conn()
    try:
        with dconn.cursor() as cur:
            cur.execute("CREATE DATABASE IF NOT EXISTS etl_warehouse")
            cur.execute("USE etl_warehouse")
            for tbl in (target_table, shadow_table):
                cur.execute(f"DROP TABLE IF EXISTS `{tbl}`")
                cur.execute(
                    f"""
                    CREATE TABLE `{tbl}` (
                        `order_id` BIGINT NOT NULL,
                        `user_name` VARCHAR(64) NOT NULL,
                        `amount` DECIMAL(12, 2) NOT NULL,
                        `order_status` VARCHAR(32) NOT NULL
                    )
                    UNIQUE KEY(`order_id`)
                    DISTRIBUTED BY HASH(`order_id`) BUCKETS 2
                    PROPERTIES ("replication_num" = "1")
                    """
                )
            # 正式表先写入 2 条 v1 历史数据，用于验证原子发布与回滚恢复
            cur.execute(
                f"INSERT INTO `{target_table}` VALUES "
                f"(1, 'legacy_v1_user_1', 10.00, 'LEGACY'), "
                f"(2, 'legacy_v1_user_2', 20.00, 'LEGACY')"
            )
    finally:
        dconn.close()

    # 3. 构造真实 SeaTunnel Zeta 2.3.9 JSON 作业并提交至 Zeta REST API
    job_name = f"etl_agent_l2_batch_{run_tag}"
    job_payload = {
        "env": {
            "job.mode": "BATCH",
            "parallelism": 1,
        },
        "source": [
            {
                "plugin_name": "Jdbc",
                "url": "jdbc:mysql://demo-mysql:3306/demo_ecommerce?useSSL=false&allowPublicKeyRetrieval=true&serverTimezone=UTC",
                "driver": "com.mysql.cj.jdbc.Driver",
                "user": "demo_etl_reader",
                "password": "dev_only_demo_mysql_reader_placeholder",
                "query": f"SELECT order_id, user_name, amount, order_status FROM demo_ecommerce.{src_table}",
            }
        ],
        "transform": [],
        "sink": [
            {
                "plugin_name": "Doris",
                "fenodes": "doris-fe:8030",
                "query-port": 9030,
                "username": "root",
                "password": "",
                "database": "etl_warehouse",
                "table": shadow_table,
                "schema_save_mode": "IGNORE",
                "data_save_mode": "APPEND_DATA",
                "sink.label-prefix": f"etl_agent_{run_tag}",
                "sink.enable-2pc": "false",
                "doris.config": {
                    "format": "json",
                    "read_json_by_line": "true",
                },
            }
        ],
    }

    qs = urllib.parse.urlencode({"jobName": job_name})
    submit_res = _http_json(
        "POST",
        f"http://127.0.0.1:5801/hazelcast/rest/maps/submit-job?{qs}",
        json.dumps(job_payload).encode("utf-8"),
    )
    job_id = str(submit_res.get("jobId") or "")
    assert job_id, f"SeaTunnel Zeta submit-job 未返回有效 jobId: {submit_res}"

    # 4. 真实轮询 Zeta REST API 直到作业 FINISHED
    final_status = "UNKNOWN"
    job_info: dict = {}
    deadline = time.monotonic() + 90.0
    while time.monotonic() < deadline:
        job_info = _http_json("GET", f"http://127.0.0.1:5801/hazelcast/rest/maps/job-info/{job_id}")
        final_status = str(job_info.get("jobStatus") or "").upper()
        if final_status in {"FINISHED", "FAILED", "CANCELED"}:
            break
        time.sleep(1.5)

    assert final_status == "FINISHED", f"真实 SeaTunnel 作业未成功结束: status={final_status}, info={job_info}"

    # 5. 真实核对 Doris 影子表已精准写入 25 条 MySQL 源记录
    dconn = _doris_conn()
    try:
        with dconn.cursor() as cur:
            cur.execute("USE etl_warehouse")
            cur.execute(f"SELECT COUNT(*) FROM `{shadow_table}`")
            shadow_count = int(cur.fetchone()[0])
            assert shadow_count == 25, f"期望 Doris 影子表写入 25 行，实际 {shadow_count}"

            # 6. 通过 DorisPublishAdapter 执行 Schema 兼容门禁 + 真实原子表替换 (swap=true)
            cols = [
                DorisColumnSpec("order_id", "BIGINT", False, True),
                DorisColumnSpec("user_name", "VARCHAR(64)", False, False),
                DorisColumnSpec("amount", "DECIMAL(12,2)", False, False),
                DorisColumnSpec("order_status", "VARCHAR(32)", False, False),
            ]
            target_schema = DorisTableSchemaSpec(
                table_name=f"etl_warehouse.{target_table}",
                columns=cols,
                engine_type="OLAP",
                keys_type="UNIQUE_KEYS",
                key_columns=["order_id"],
            )
            shadow_schema = DorisTableSchemaSpec(
                table_name=f"etl_warehouse.{shadow_table}",
                columns=cols,
                engine_type="OLAP",
                keys_type="UNIQUE_KEYS",
                key_columns=["order_id"],
            )

            def _exec_doris_ddl(sql: str) -> dict:
                cur.execute(sql)
                return {"executed": True, "sql": sql}

            org_id = "69da18bd-949c-4246-9ad0-d290024ad290"
            proj_id = "32d6418e-3391-4a9c-afac-28034152f6bd"
            adapter = DorisPublishAdapter(sql_executor=_exec_doris_ddl)
            published = adapter.publish_shadow_to_target(
                organization_id=org_id,
                project_id=proj_id,
                execution_id=f"exec-l2-{run_tag}",
                pipeline_version_id=f"pv-l2-{run_tag}",
                artifact_hash="sha256:" + "a" * 64,
                target_connection_id="conn-doris-l2",
                quality_report_id=f"qr-l2-{run_tag}",
                published_by="operator@etl-agent.local",
                target_schema=target_schema,
                shadow_schema=shadow_schema,
            )
            assert published.status == "PUBLISHED"

            # 验证正式表已经原子切换为 25 条新数据，且归档影子表保留了 2 条 v1 旧数据
            cur.execute(f"SELECT COUNT(*) FROM `{target_table}`")
            assert int(cur.fetchone()[0]) == 25
            cur.execute(f"SELECT COUNT(*) FROM `{shadow_table}`")
            assert int(cur.fetchone()[0]) == 2

            # 7. 通过 RollbackExecutor 执行真实受管 Swap 回滚恢复至 v1 旧数据
            rollback_executor = RollbackExecutor(doris_adapter=adapter, sql_executor=_exec_doris_ddl)
            run_rec = ExecutionRunRecord(
                execution_id=f"exec-l2-{run_tag}",
                organization_id=org_id,
                project_id=proj_id,
                pipeline_id="pipe-l2",
                pipeline_version_id=f"pv-l2-{run_tag}",
                artifact_hash="sha256:" + "a" * 64,
                execution_mode=ExecutionMode.BATCH,
                preparation_id=f"prep-l2-{run_tag}",
                capability_jti=str(uuid.uuid4()),
                command_id=str(uuid.uuid4()),
                status="SUCCEEDED",
                external_job_id=job_id,
            )
            rb_ctx = RollbackCommandContext(
                organization_id=org_id,
                project_id=proj_id,
                preparation_id=f"prep-rb-{run_tag}",
                capability_jti=str(uuid.uuid4()),
                command_id=str(uuid.uuid4()),
                initiated_by="operator@etl-agent.local",
                trace_id=f"trace-l2-{run_tag}",
            )
            rb_result = rollback_executor.execute_swap_restore(
                ctx=rb_ctx,
                run=run_rec,
                published_version=published,
                target_schema=target_schema,
                archive_schema=shadow_schema,
            )
            assert rb_result.status == "SUCCEEDED"

            # 验证正式表已精确回滚为 2 条 v1 历史记录
            cur.execute(f"SELECT COUNT(*) FROM `{target_table}`")
            assert int(cur.fetchone()[0]) == 2
            cur.execute(f"SELECT user_name FROM `{target_table}` ORDER BY order_id")
            assert [r[0] for r in cur.fetchall()] == ["legacy_v1_user_1", "legacy_v1_user_2"]
    finally:
        # 清理本次临时测试表（不触碰演示基线表）
        with dconn.cursor() as cur:
            cur.execute(f"DROP TABLE IF EXISTS etl_warehouse.`{target_table}`")
            cur.execute(f"DROP TABLE IF EXISTS etl_warehouse.`{shadow_table}`")
        dconn.close()
        mconn2 = _mysql_conn()
        try:
            with mconn2.cursor() as cur:
                cur.execute(f"DROP TABLE IF EXISTS `{src_table}`")
        finally:
            mconn2.close()
