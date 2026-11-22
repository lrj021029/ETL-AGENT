"""严格 SELECT-only 的 SQL AST 安全沙箱（基于 sqlglot）。

安全模型：
* 全部判定基于 AST 节点类型 / 表 / 列 / 函数的**显式白名单**，绝不依赖正则黑名单；
* 未知节点、解析失败、多语句、非 SELECT 根节点一律拒绝（fail-closed）；
* 另设一份函数危险名单仅作为纵深防御（白名单仍是唯一放行依据）；
* `SQL Transformation` 与 `SQL Quality Rule` 使用独立的策略 Profile。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

ERROR_CODE = "SQL_AST_POLICY_VIOLATION"

# 方言映射：SeaTunnel SQL 以 MySQL 语法作为解析基准
_DIALECT_MAP = {"mysql": "mysql", "postgres": "postgres", "doris": "doris", "seatunnel": "mysql"}

# 系统目录 / 系统库（纵深防御，白名单本身已足以拒绝）
SYSTEM_SCHEMAS = frozenset(
    {"information_schema", "pg_catalog", "mysql", "sys", "performance_schema", "pg_toast", "__internal_schema"}
)

# 纵深防御：明确危险的函数名（即便被误加入白名单也会被拒绝）
DANGEROUS_FUNCTIONS = frozenset(
    {
        "load_file", "sleep", "benchmark", "pg_sleep", "pg_read_file", "pg_read_binary_file",
        "pg_ls_dir", "lo_import", "lo_export", "dblink", "copy", "sys_exec", "sys_eval",
        "xp_cmdshell", "current_setting", "set_config", "version", "user", "current_user",
        "database", "session_user", "get_lock", "release_lock", "extractvalue", "updatexml",
    }
)

# 允许出现的非函数 AST 节点（函数节点单独按名称白名单判定）
BASE_NODES: frozenset[type] = frozenset(
    {
        exp.Select, exp.Table, exp.TableAlias, exp.Column, exp.Identifier, exp.Star, exp.Alias,
        exp.Literal, exp.Boolean, exp.Null, exp.Where, exp.From, exp.Group, exp.Order, exp.Ordered,
        exp.Having, exp.Limit, exp.Offset, exp.And, exp.Or, exp.Not, exp.EQ, exp.NEQ, exp.GT,
        exp.GTE, exp.LT, exp.LTE, exp.Add, exp.Sub, exp.Mul, exp.Div, exp.Mod, exp.Neg, exp.Paren,
        exp.In, exp.Between, exp.Like, exp.ILike, exp.Is, exp.DataType, exp.DataTypeParam,
        exp.Distinct, exp.Escape, exp.Tuple, exp.Dot, exp.DPipe, exp.IntDiv,
    }
)
JOIN_NODES: frozenset[type] = frozenset({exp.Join})
SUBQUERY_NODES: frozenset[type] = frozenset({exp.Subquery})

SAFE_FUNCTIONS = frozenset(
    {
        "count", "sum", "avg", "min", "max", "abs", "round", "floor", "ceil", "ceiling", "coalesce",
        "nullif", "ifnull", "upper", "lower", "trim", "ltrim", "rtrim", "length", "substring",
        "substr", "concat", "replace", "cast", "case", "if", "date_format", "date", "year", "month",
        "day", "left", "right", "lpad", "rpad", "regexp_like", "md5", "sha2", "time_to_str", "ts_or_ds_to_timestamp",
    }
)


@dataclass(frozen=True)
class SqlPolicyProfile:
    """一个 SQL 策略 Profile：方言、白名单与结构限制。"""

    name: str
    dialect: str
    default_schema: str
    # 允许的表：{"schema.table": {允许的列}}
    allowed_tables: dict[str, frozenset[str]]
    allowed_functions: frozenset[str] = SAFE_FUNCTIONS
    allow_join: bool = True
    allow_subquery: bool = True
    allow_star: bool = False
    single_table_only: bool = False


def transformation_profile(
    dialect: str,
    default_schema: str,
    allowed_tables: dict[str, set[str]],
    extra_functions: set[str] | None = None,
) -> SqlPolicyProfile:
    """SQL Transformation：允许 JOIN / 子查询。"""
    return SqlPolicyProfile(
        name="SQL_TRANSFORMATION",
        dialect=dialect,
        default_schema=default_schema,
        allowed_tables={k.lower(): frozenset(c.lower() for c in v) for k, v in allowed_tables.items()},
        allowed_functions=SAFE_FUNCTIONS | frozenset(f.lower() for f in (extra_functions or set())),
    )


def quality_rule_profile(
    dialect: str, default_schema: str, allowed_tables: dict[str, set[str]]
) -> SqlPolicyProfile:
    """SQL Quality Rule：更严格——单表、无 JOIN、无子查询、仅基础函数，不允许 UDF。"""
    return SqlPolicyProfile(
        name="SQL_QUALITY_RULE",
        dialect=dialect,
        default_schema=default_schema,
        allowed_tables={k.lower(): frozenset(c.lower() for c in v) for k, v in allowed_tables.items()},
        allowed_functions=SAFE_FUNCTIONS - {"date_format", "time_to_str", "ts_or_ds_to_timestamp"},
        allow_join=False,
        allow_subquery=False,
        single_table_only=True,
    )


@dataclass(frozen=True)
class Violation:
    code: str  # 细分原因，如 DDL_DML_REJECTED / TABLE_NOT_ALLOWED
    detail: str


@dataclass
class SqlAnalysisResult:
    ok: bool
    violations: list[Violation] = field(default_factory=list)
    tables: list[str] = field(default_factory=list)  # 'schema.table'
    has_join: bool = False
    has_aggregate: bool = False
    functions: list[str] = field(default_factory=list)
    normalized_sql: str | None = None


class SqlAstPolicyViolation(Exception):
    code = ERROR_CODE

    def __init__(self, violations: list[Violation]):
        self.violations = violations
        super().__init__("; ".join(f"{v.code}: {v.detail}" for v in violations))


def _func_name(node: exp.Func) -> str:
    if isinstance(node, exp.Anonymous):
        return str(node.name).lower()
    return node.sql_name().lower()


def analyze_select(sql: str, profile: SqlPolicyProfile) -> SqlAnalysisResult:
    """分析 SQL；任何违规都会收集到 violations 中，ok=False。"""
    res = SqlAnalysisResult(ok=False)
    bad = res.violations

    def reject(code: str, detail: str) -> SqlAnalysisResult:
        bad.append(Violation(code, detail))
        return res

    dialect = _DIALECT_MAP.get(profile.dialect)
    if dialect is None:
        return reject("DIALECT_NOT_ALLOWED", profile.dialect)
    if not sql or not sql.strip():
        return reject("EMPTY_SQL", "empty")
    try:
        statements = sqlglot.parse(sql, read=dialect)
    except SqlglotError as e:  # 未知语法 / 解析失败 -> fail-closed
        return reject("PARSE_FAILED", str(e)[:200])
    except Exception as e:  # noqa: BLE001
        return reject("PARSE_FAILED", type(e).__name__)
    statements = [s for s in statements if s is not None]
    if len(statements) != 1:
        return reject("MULTI_STATEMENT_REJECTED", f"{len(statements)} statements")
    root = statements[0]
    if not isinstance(root, exp.Select):
        return reject("NON_SELECT_ROOT", type(root).__name__)
    if not root.expressions or not any(isinstance(root.find(exp.Table), exp.Table) for _ in (0,)):
        return reject("INCOMPLETE_SELECT", "SELECT requires projections and table source")

    allowed_nodes = set(BASE_NODES)
    if profile.allow_join:
        allowed_nodes |= JOIN_NODES
    if profile.allow_subquery:
        allowed_nodes |= SUBQUERY_NODES

    table_aliases: dict[str, str] = {}  # alias/表名 -> 'schema.table'
    real_tables: list[str] = []
    derived_names: set[str] = set()
    select_aliases: set[str] = set()

    for node in root.walk():
        if isinstance(node, exp.Func) and type(node) not in allowed_nodes:
            name = _func_name(node)
            res.functions.append(name)
            if name in DANGEROUS_FUNCTIONS:
                bad.append(Violation("DANGEROUS_FUNCTION", name))
            elif name not in profile.allowed_functions:
                bad.append(Violation("FUNCTION_NOT_ALLOWED", name))
            if isinstance(node, exp.AggFunc):
                res.has_aggregate = True
            continue
        if type(node) not in allowed_nodes:
            bad.append(Violation("AST_NODE_NOT_ALLOWED", type(node).__name__))
            continue
        if isinstance(node, exp.Join):
            res.has_join = True
        if isinstance(node, exp.Alias) and node.alias:
            select_aliases.add(node.alias.lower())
        if isinstance(node, exp.Subquery) and node.alias:
            table_aliases.setdefault(node.alias.lower(), "")
            for p in node.this.expressions if isinstance(node.this, exp.Select) else []:
                derived_names.add(p.alias_or_name.lower())
        if isinstance(node, exp.Table):
            if not isinstance(node.this, exp.Identifier):
                bad.append(Violation("TABLE_NOT_ALLOWED", "non-identifier table source"))
                continue
            if node.args.get("catalog"):
                bad.append(Violation("CROSS_CATALOG_REJECTED", node.sql()))
                continue
            schema = (node.db or profile.default_schema).lower()
            qualified = f"{schema}.{node.name.lower()}"
            if schema in SYSTEM_SCHEMAS:
                bad.append(Violation("SYSTEM_CATALOG_ACCESS", qualified))
            elif schema != profile.default_schema.lower() and not any(
                k.startswith(schema + ".") for k in profile.allowed_tables
            ):
                bad.append(Violation("CROSS_SCHEMA_REJECTED", qualified))
            elif qualified not in profile.allowed_tables:
                bad.append(Violation("TABLE_NOT_ALLOWED", qualified))
            else:
                real_tables.append(qualified)
                table_aliases[(node.alias or node.name).lower()] = qualified
    res.tables = sorted(set(real_tables))
    if profile.single_table_only and len(set(real_tables)) > 1:
        bad.append(Violation("MULTI_TABLE_REJECTED", ",".join(res.tables)))

    union_cols: set[str] = set()
    for t in set(real_tables):
        union_cols |= profile.allowed_tables[t]

    for col in root.find_all(exp.Column):
        if isinstance(col.this, exp.Star):
            if not profile.allow_star:
                bad.append(Violation("STAR_NOT_ALLOWED", col.sql()))
            continue
        name = col.name.lower()
        qual = col.table.lower() if col.table else ""
        if col.args.get("db") or col.args.get("catalog"):
            bad.append(Violation("COLUMN_NOT_ALLOWED", col.sql()))
        elif qual:
            tbl = table_aliases.get(qual)
            if tbl is None:
                bad.append(Violation("COLUMN_NOT_ALLOWED", f"unknown qualifier {qual}"))
            elif tbl == "":  # 子查询别名，列须来自其投影
                if name not in derived_names:
                    bad.append(Violation("COLUMN_NOT_ALLOWED", col.sql()))
            elif name not in profile.allowed_tables[tbl]:
                bad.append(Violation("COLUMN_NOT_ALLOWED", f"{tbl}.{name}"))
        elif name not in union_cols and name not in derived_names and name not in select_aliases:
            bad.append(Violation("COLUMN_NOT_ALLOWED", name))

    for star in root.find_all(exp.Star):
        if not isinstance(star.parent, (exp.Count, exp.Column)) and not profile.allow_star:
            bad.append(Violation("STAR_NOT_ALLOWED", "SELECT *"))

    if bad:
        return res
    res.ok = True
    res.normalized_sql = root.sql(dialect=dialect)
    return res


def enforce_select(sql: str, profile: SqlPolicyProfile) -> SqlAnalysisResult:
    res = analyze_select(sql, profile)
    if not res.ok:
        raise SqlAstPolicyViolation(res.violations)
    return res
