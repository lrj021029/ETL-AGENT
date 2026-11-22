"""确定性基准测试数据集生成器（支持 10K / 100K / 1M 三档流式生成与 SHA256 清单）。

覆盖两条核心业务与异常场景：
- Scenario A (orders_users): Orders + Users 宽表关联数据集（干净数据，用于端到端吞吐与映射评测）。
- Scenario B (logs_anomaly): 日志与异常流数据集，支持可控错误注入率（默认 >20%），
  覆盖 NOT_NULL / DATA_TYPE / REGEX / RANGE / ENUM / UNIQUE 重复主键与 Schema Drift 载荷，
  并精确统计各 DQ_* 错误码分布与生成 SHA256 清单。
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

# 三档标准行数规模（PRD §7.5 & TECHNICAL_SPEC §17.4）
TIER_ROW_COUNTS: dict[str, int] = {
    "10K": 10_000,
    "100K": 100_000,
    "1M": 1_000_000,
}

# 固定基准时间起点，确保跨运行字节级确定性
_BASE_TS = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)

# Scenario B 支持注入的异常类型与契约错误码映射
ANOMALY_TYPES: tuple[tuple[str, str], ...] = (
    ("NOT_NULL", "DQ_NOT_NULL_VIOLATION"),
    ("DATA_TYPE", "DQ_DATA_TYPE_VIOLATION"),
    ("REGEX", "DQ_REGEX_VIOLATION"),
    ("RANGE", "DQ_RANGE_VIOLATION"),
    ("ENUM", "DQ_ENUM_VIOLATION"),
    ("UNIQUE", "DQ_UNIQUE_VIOLATION"),
    ("SCHEMA_DRIFT", "PUBLISH_SCHEMA_INCOMPATIBLE"),
)

_ORDER_STATUSES = ("CREATED", "PAID", "SHIPPED", "COMPLETED", "CANCELLED")
_CURRENCIES = ("USD", "CNY", "EUR", "JPY")
_LOG_LEVELS = ("INFO", "WARN", "ERROR", "DEBUG")
_SERVICE_NAMES = ("api-gateway", "etl-worker", "seatunnel-adapter", "pdp-engine")


@dataclass(frozen=True)
class GeneratedDatasetManifest:
    """数据集生成清单（包含确定性 SHA256 校验与异常统计）。"""

    scenario: str
    tier: str
    seed: int
    total_rows: int
    error_rate_target: float
    actual_error_rows: int
    actual_error_rate: float
    anomaly_breakdown: dict[str, int]
    files: dict[str, dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "manifest_version": "1.0.0",
            "scenario": self.scenario,
            "tier": self.tier,
            "seed": self.seed,
            "total_rows": self.total_rows,
            "error_rate_target": self.error_rate_target,
            "actual_error_rows": self.actual_error_rows,
            "actual_error_rate": self.actual_error_rate,
            "anomaly_breakdown": self.anomaly_breakdown,
            "files": self.files,
        }


def _sha256_file(path: Path) -> str:
    """流式计算文件 SHA256 摘要，避免大文件占满内存。"""
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _sql_escape(val: Any) -> str:
    """将 Python 值转义为标准 SQL 字面量。"""
    if val is None:
        return "NULL"
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        return str(val)
    text = str(val).replace("'", "''")
    return f"'{text}'"


def _iter_orders_users_rows(total_rows: int, seed: int) -> Iterator[dict[str, Any]]:
    """流式生成 Scenario A (Orders + Users 宽表) 确定性记录。"""
    rng = random.Random(seed)
    user_pool_size = max(100, min(total_rows // 10, 50_000))
    countries = ("CN", "US", "DE", "JP", "SG")

    for idx in range(total_rows):
        user_idx = rng.randint(1, user_pool_size)
        ts = _BASE_TS + timedelta(seconds=idx % 86400, days=idx // 86400)
        amount = round(rng.uniform(5.0, 5000.0), 2)
        yield {
            "order_id": f"ORD-{idx + 1:09d}",
            "user_id": f"USR-{user_idx:07d}",
            "user_tier": "VIP" if user_idx % 5 == 0 else "STANDARD",
            "country": countries[user_idx % len(countries)],
            "order_status": _ORDER_STATUSES[rng.randrange(len(_ORDER_STATUSES))],
            "amount": amount,
            "currency": _CURRENCIES[rng.randrange(len(_CURRENCIES))],
            "created_at": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }


def _iter_logs_anomaly_rows(
    total_rows: int,
    seed: int,
    error_rate: float,
) -> Iterator[tuple[dict[str, Any], str | None]]:
    """流式生成 Scenario B (Logs/Anomaly 流) 确定性记录及对应预期错误码。"""
    if not (0.0 <= error_rate <= 1.0):
        raise ValueError(f"error_rate 必须在 [0.0, 1.0] 范围内，实际为: {error_rate}")

    rng = random.Random(seed)
    target_error_count = int(round(total_rows * error_rate))
    # 使用固定步长 + 确定性抖动选择错误行索引，既 O(1) 内存又能精确控制错误总数
    # 当 error_rate > 0 且 target_error_count == 0 时至少保证 0 行；通常 total_rows >= 100
    error_stride = (total_rows / target_error_count) if target_error_count > 0 else float("inf")
    next_error_pos = 0.0
    injected_count = 0

    for idx in range(total_rows):
        ts = _BASE_TS + timedelta(milliseconds=idx * 10)
        event_id = f"EVT-{idx + 1:09d}"
        row: dict[str, Any] = {
            "event_id": event_id,
            "service_name": _SERVICE_NAMES[idx % len(_SERVICE_NAMES)],
            "severity": _LOG_LEVELS[rng.randrange(len(_LOG_LEVELS))],
            "client_ip": f"10.{(idx >> 16) & 255}.{(idx >> 8) & 255}.{(idx % 250) + 1}",
            "status_code": 200 if idx % 10 != 0 else 201,
            "latency_ms": rng.randint(1, 500),
            "event_time": ts.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        }

        is_error_row = False
        if injected_count < target_error_count and idx >= int(next_error_pos):
            is_error_row = True
            injected_count += 1
            next_error_pos += error_stride

        if not is_error_row:
            yield row, None
            continue

        # 按固定轮转分配 7 类异常，确保所有 DQ_* 错误码及 Schema Drift 均被覆盖
        anomaly_kind, error_code = ANOMALY_TYPES[(injected_count - 1) % len(ANOMALY_TYPES)]
        if anomaly_kind == "NOT_NULL":
            row["service_name"] = None
        elif anomaly_kind == "DATA_TYPE":
            row["latency_ms"] = "NaN_TYPE_ERR"
        elif anomaly_kind == "REGEX":
            row["client_ip"] = "invalid-ip-format"
        elif anomaly_kind == "RANGE":
            row["latency_ms"] = -999
        elif anomaly_kind == "ENUM":
            row["severity"] = "INVALID_SEVERITY"
        elif anomaly_kind == "UNIQUE":
            # 与前序确定性主键冲突（避免 idx == 0 时无前序主键）
            dup_idx = max(1, idx)
            row["event_id"] = f"EVT-{dup_idx:09d}"
        elif anomaly_kind == "SCHEMA_DRIFT":
            row["schema_drift_extra_col"] = {"unexpected_nested": True, "v": 2}

        yield row, error_code


def generate_dataset(
    output_dir: str | Path,
    scenario: str = "logs_anomaly",
    tier: str = "10K",
    seed: int = 42,
    error_rate: float = 0.25,
    formats: tuple[str, ...] = ("csv", "jsonl", "sql"),
) -> GeneratedDatasetManifest:
    """生成指定场景、规模档位与随机种子的确定性数据集文件及 SHA256 清单。

    参数:
        output_dir: 输出目录路径
        scenario: 'orders_users' (Scenario A) 或 'logs_anomaly' (Scenario B)
        tier: '10K' | '100K' | '1M'
        seed: 固定随机种子
        error_rate: Scenario B 的目标错误注入比例（默认 0.25 > 0.20 阈值）
        formats: 需要生成的格式元组，支持 'csv', 'jsonl', 'sql'
    """
    if tier not in TIER_ROW_COUNTS:
        raise ValueError(f"不支持的数据集档位 '{tier}'，可选值: {list(TIER_ROW_COUNTS.keys())}")
    if scenario not in ("orders_users", "logs_anomaly"):
        raise ValueError(f"不支持的场景 '{scenario}'，可选值: 'orders_users', 'logs_anomaly'")

    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    total_rows = TIER_ROW_COUNTS[tier]
    effective_error_rate = error_rate if scenario == "logs_anomaly" else 0.0

    # 定义固定列顺序，保证 CSV 与 SQL 写入确定性
    if scenario == "orders_users":
        fieldnames = [
            "order_id",
            "user_id",
            "user_tier",
            "country",
            "order_status",
            "amount",
            "currency",
            "created_at",
        ]
        table_name = "demo_orders_users"
    else:
        fieldnames = [
            "event_id",
            "service_name",
            "severity",
            "client_ip",
            "status_code",
            "latency_ms",
            "event_time",
        ]
        table_name = "demo_logs_stream"

    prefix = f"{scenario}_{tier.lower()}_seed{seed}"
    file_paths: dict[str, Path] = {}
    handles: dict[str, Any] = {}
    csv_writer = None

    try:
        if "csv" in formats:
            p = out_path / f"{prefix}.csv"
            f = p.open("w", encoding="utf-8", newline="", buffering=1024 * 1024)
            handles["csv"] = f
            file_paths["csv"] = p
            csv_writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            csv_writer.writeheader()

        if "jsonl" in formats:
            p = out_path / f"{prefix}.jsonl"
            handles["jsonl"] = p.open("w", encoding="utf-8", newline="\n", buffering=1024 * 1024)
            file_paths["jsonl"] = p

        if "sql" in formats:
            p = out_path / f"{prefix}.sql"
            f = p.open("w", encoding="utf-8", newline="\n", buffering=1024 * 1024)
            handles["sql"] = f
            file_paths["sql"] = p
            f.write(f"-- ETL-Agent Deterministic Seed SQL ({scenario}, tier={tier}, seed={seed})\n")
            cols_sql = ", ".join(fieldnames)
            f.write(f"-- Target table: {table_name} ({cols_sql})\n")

        anomaly_breakdown: dict[str, int] = {}
        actual_error_rows = 0

        if scenario == "orders_users":
            row_iter = ((r, None) for r in _iter_orders_users_rows(total_rows, seed))
        else:
            row_iter = _iter_logs_anomaly_rows(total_rows, seed, effective_error_rate)

        sql_batch: list[str] = []
        cols_sql = ", ".join(fieldnames)

        for row, error_code in row_iter:
            if error_code is not None:
                actual_error_rows += 1
                anomaly_breakdown[error_code] = anomaly_breakdown.get(error_code, 0) + 1

            if csv_writer is not None:
                csv_writer.writerow(row)

            if "jsonl" in handles:
                handles["jsonl"].write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")

            if "sql" in handles:
                vals = ", ".join(_sql_escape(row.get(col)) for col in fieldnames)
                sql_batch.append(f"({vals})")
                if len(sql_batch) >= 500:
                    handles["sql"].write(
                        f"INSERT INTO {table_name} ({cols_sql}) VALUES\n  "
                        + ",\n  ".join(sql_batch)
                        + ";\n"
                    )
                    sql_batch.clear()

        if "sql" in handles and sql_batch:
            handles["sql"].write(
                f"INSERT INTO {table_name} ({cols_sql}) VALUES\n  "
                + ",\n  ".join(sql_batch)
                + ";\n"
            )
            sql_batch.clear()
    finally:
        for h in handles.values():
            h.close()

    files_meta: dict[str, dict[str, Any]] = {}
    for fmt, p in file_paths.items():
        files_meta[fmt] = {
            "filename": p.name,
            "size_bytes": p.stat().st_size,
            "sha256": _sha256_file(p),
        }

    actual_rate = (actual_error_rows / total_rows) if total_rows > 0 else 0.0
    manifest = GeneratedDatasetManifest(
        scenario=scenario,
        tier=tier,
        seed=seed,
        total_rows=total_rows,
        error_rate_target=effective_error_rate,
        actual_error_rows=actual_error_rows,
        actual_error_rate=round(actual_rate, 6),
        anomaly_breakdown=anomaly_breakdown,
        files=files_meta,
    )

    manifest_path = out_path / f"{prefix}_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest.to_dict(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return manifest


def main() -> None:
    """命令行入口：生成指定档位的确定性测试数据集。"""
    parser = argparse.ArgumentParser(description="ETL-Agent 确定性基准数据集生成器")
    parser.add_argument("--output-dir", required=True, help="输出目录")
    parser.add_argument("--scenario", choices=["orders_users", "logs_anomaly"], default="logs_anomaly")
    parser.add_argument("--tier", choices=list(TIER_ROW_COUNTS.keys()), default="10K")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--error-rate", type=float, default=0.25)
    parser.add_argument("--formats", nargs="+", default=["csv", "jsonl", "sql"])
    args = parser.parse_args()

    manifest = generate_dataset(
        output_dir=args.output_dir,
        scenario=args.scenario,
        tier=args.tier,
        seed=args.seed,
        error_rate=args.error_rate,
        formats=tuple(args.formats),
    )
    print(json.dumps(manifest.to_dict(), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
