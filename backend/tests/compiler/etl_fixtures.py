"""编译器 / Agent 测试共用的构造器（元数据、Connector 注册表、绑定、默认 Plan）。"""
from __future__ import annotations

SRC_CONN = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
SINK_CONN = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
SNAP = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
SECRET_SRC = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
SECRET_SINK = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"


def metadata():
    from app.pipeline.tools import MetadataSnapshot, TableMeta

    return MetadataSnapshot(
        snapshot_id=SNAP,
        tables={
            "sales.orders": TableMeta(
                columns={"id": "BIGINT", "customer_name": "VARCHAR", "amount": "DECIMAL", "updated_at": "DATETIME"},
                primary_keys=["id"],
                sensitive_columns=["customer_name"],
            )
        },
    )


def _entry(cid, roles, **caps):
    base = dict(
        metadata_probe=True, full_load=True, incremental=True, cdc=False, delete_events=False,
        upsert=True, checkpoint_restore=False, schema_evolution=False, quality_split_compatible=True,
    )
    base.update(caps)
    return {
        "schema_version": "1.0.0", "connector_id": cid, "connector_name": cid, "connector_version": "1",
        "seatunnel_version": "2.3.9", "roles": roles, "auth_modes": ["PASSWORD"],
        "support_status": "CONFIGURED", "capabilities": base,
        "publish_rollback_limits": {"atomic_table_replace": True, "shadow_table_supported": True, "notes": ""},
    }


def registry_entries():
    return [
        _entry("mysql", ["SOURCE", "SINK"]),
        _entry("doris", ["SINK"], full_load=False),
        {**_entry("oracle", ["SOURCE"]), "support_status": "PLANNED"},
    ]


def registry():
    from app.hocon.registry import ConnectorRegistry

    return ConnectorRegistry(registry_entries())


def bindings():
    return {
        "source": {
            "connection_id": SRC_CONN, "connection_version": 1, "connector_id": "mysql",
            "metadata_snapshot_id": SNAP, "secret_ref_id": SECRET_SRC, "secret_version": 1,
        },
        "sink": {
            "connection_id": SINK_CONN, "connection_version": 1, "connector_id": "doris",
            "secret_ref_id": SECRET_SINK, "secret_version": 1,
        },
    }


def plan_dict(sql="SELECT id, amount FROM sales.orders"):
    return {
        "schema_version": "1.0.0",
        "plan_name": "orders_sync",
        "sync_mode": "FULL_LOAD",
        "source": {
            "connection_id": SRC_CONN, "connection_version": 1, "connector_id": "mysql",
            "metadata_snapshot_id": SNAP,
            "objects": [{"schema_name": "sales", "table_name": "orders", "primary_keys": ["id"]}],
        },
        "sink": {
            "connection_id": SINK_CONN, "connection_version": 1, "connector_id": "doris",
            "target_schema": "dw", "target_table": "orders", "write_mode": "SHADOW_SWAP", "primary_keys": ["id"],
        },
        "field_mappings": [
            {"source_field": "id", "target_field": "id", "target_type": "BIGINT", "nullable": False},
            {"source_field": "amount", "target_field": "amount", "target_type": "DECIMAL"},
        ],
        "transformations": [
            {"step_id": "s1", "kind": "SELECT_SQL", "sql_dialect": "mysql", "sql_text": sql}
        ],
        "publish_strategy": {"adapter": "DORIS_ATOMIC_SWAP", "require_schema_compat_gate": True, "retain_previous_version": True},
        "secret_refs": [],
    }


def quality_dict():
    return {
        "schema_version": "1.0.0",
        "error_table_name": "orders_dq_errors",
        "raw_value_storage_policy": "REDACTED_BY_CLASSIFICATION",
        "rules": [
            {
                "rule_id": "nn_id", "rule_type": "NOT_NULL", "stage": "STREAMING_ROW_SPLIT", "column_name": "id",
                "error_code": "DQ_NOT_NULL", "severity": "REJECT_ROW", "params": {},
            }
        ],
    }
