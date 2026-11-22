"""SQL AST 沙箱：负向用例 100% 拒绝（SQL_AST_POLICY_VIOLATION），合法 SELECT 通过。"""
import pytest

from app.sql_analysis import (
    ERROR_CODE,
    SqlAstPolicyViolation,
    analyze_select,
    enforce_select,
    quality_rule_profile,
    transformation_profile,
)

TABLES = {
    "sales.orders": {"id", "customer_name", "amount", "updated_at"},
    "sales.customers": {"id", "name"},
}


def tprof(dialect="mysql", extra=None):
    return transformation_profile(dialect, "sales", TABLES, extra)


BAD = [
    "DROP TABLE sales.orders",
    "DELETE FROM sales.orders",
    "UPDATE sales.orders SET amount = 1",
    "INSERT INTO sales.orders (id) VALUES (1)",
    "SELECT id FROM sales.orders; DROP TABLE sales.orders",
    "SELECT id FROM information_schema.tables",
    "SELECT table_name FROM information_schema.columns",
    "SELECT relname FROM pg_catalog.pg_class",
    "SELECT user FROM mysql.user",
    "SELECT LOAD_FILE('/etc/passwd')",
    "SELECT id FROM sales.orders INTO OUTFILE '/tmp/x'",
    "SELECT id FROM sales.secret_table",
    "SELECT salary FROM sales.orders",
    "SELECT o.salary FROM sales.orders o",
    "SELECT MY_UDF(id) FROM sales.orders",
    "SELECT SLEEP(10) FROM sales.orders",
    "SELECT id FROM other_schema.orders",
    "SELECT id FROM other_db.sales.orders",
    "SELECT * FROM sales.orders",
    "WITH x AS (SELECT id FROM sales.orders) SELECT id FROM x",
    "SELECT id FROM sales.orders UNION SELECT id FROM sales.customers",
    "SELECT id FROM sales.orders FOR UPDATE",
    "SELECT id, ROW_NUMBER() OVER (ORDER BY id) FROM sales.orders",
    "GRANT ALL ON sales.orders TO x",
    "TRUNCATE TABLE sales.orders",
    "CREATE TABLE t AS SELECT id FROM sales.orders",
    "this is not sql at all ((",
    "",
    "SELECT",
    "CALL some_proc()",
    "SET @a = 1",
    "SELECT id FROM sales.orders WHERE id = @x",
]


@pytest.mark.parametrize("sql", BAD)
def test_negative_rejected(sql):
    res = analyze_select(sql, tprof())
    assert not res.ok and res.violations, sql
    with pytest.raises(SqlAstPolicyViolation) as ei:
        enforce_select(sql, tprof())
    assert ei.value.code == ERROR_CODE == "SQL_AST_POLICY_VIOLATION"


def test_dangerous_function_rejected_even_if_allowlisted():
    prof = tprof(extra={"load_file"})
    assert not analyze_select("SELECT LOAD_FILE('/x') FROM sales.orders", prof).ok


def test_unknown_unallowlisted_udf_rejected_but_allowlisted_udf_ok():
    assert not analyze_select("SELECT my_udf(id) FROM sales.orders", tprof()).ok
    assert analyze_select("SELECT my_udf(id) FROM sales.orders", tprof(extra={"my_udf"})).ok


GOOD = [
    "SELECT id, amount FROM sales.orders",
    "SELECT o.id, UPPER(o.customer_name) AS n FROM sales.orders AS o WHERE o.amount > 10 AND o.id IN (1, 2)",
    "SELECT COUNT(*) AS c, SUM(amount) AS s FROM sales.orders GROUP BY id HAVING SUM(amount) > 1 ORDER BY c LIMIT 10",
    "SELECT o.id, c.name FROM sales.orders o JOIN sales.customers c ON o.id = c.id",
    "SELECT CASE WHEN amount > 1 THEN 'a' ELSE 'b' END AS t, COALESCE(amount, 0) FROM orders",
    "SELECT t.id FROM (SELECT id FROM sales.orders) t",
    "SELECT id FROM sales.orders;",
]


@pytest.mark.parametrize("sql", GOOD)
def test_valid_select_passes(sql):
    res = analyze_select(sql, tprof())
    assert res.ok, res.violations


def test_postgres_and_doris_dialects():
    assert analyze_select("SELECT id FROM sales.orders", tprof("postgres")).ok
    assert analyze_select("SELECT id FROM sales.orders", tprof("doris")).ok
    assert analyze_select("SELECT id FROM sales.orders", tprof("seatunnel")).ok
    assert not analyze_select("SELECT id FROM sales.orders", tprof("oracle")).ok


def test_quality_profile_is_stricter():
    q = quality_rule_profile("mysql", "sales", TABLES)
    assert analyze_select("SELECT id FROM sales.orders WHERE amount < 0", q).ok
    assert not analyze_select("SELECT o.id FROM sales.orders o JOIN sales.customers c ON o.id=c.id", q).ok
    assert not analyze_select("SELECT t.id FROM (SELECT id FROM sales.orders) t", q).ok
    assert not analyze_select("SELECT DATE_FORMAT(updated_at, '%Y') FROM sales.orders", q).ok
    # 同样的 SQL 在 Transformation Profile 下合法
    assert analyze_select("SELECT DATE_FORMAT(updated_at, '%Y') FROM sales.orders", tprof()).ok
