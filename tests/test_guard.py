"""The SQL guard: what gets through, what is refused, and that bypass attempts are refused."""

from __future__ import annotations

import pytest

from datamcp.guard import GuardError, guard

ALLOWED = [
    "SELECT 1",
    "select count(*) from listings",
    "SELECT * FROM analytics.listings WHERE address_city = 'Lyon' LIMIT 10",
    "SELECT kind, count(*) FROM things GROUP BY 1 ORDER BY 2 DESC",
    "WITH t AS (SELECT * FROM things) SELECT * FROM t",
    "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r WHERE n < 10) SELECT * FROM r",
    "SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY amount) FROM things",
    "SELECT * FROM things t JOIN monthly_things m ON m.kind = t.kind",
    "SELECT * FROM things WHERE kind = 'a; DROP TABLE things; --'",  # semicolon inside a string
    "SELECT $$x; y$$ AS s FROM things",  # dollar quoting
    "SELECT 1 -- trailing comment\n",
    "/* leading comment */ SELECT 1",
    "SELECT 1;",  # one trailing semicolon is fine
    "SELECT generate_series(1, 3)",
    "SELECT now(), current_date, date_trunc('month', day) FROM things",
    "SELECT jsonb_object_keys('{\"a\": 1}'::jsonb)",
    "SELECT * FROM things TABLESAMPLE SYSTEM (10)",
    "SELECT unnest(ARRAY[1, 2])",
    "EXPLAIN SELECT * FROM things",
    "EXPLAIN (FORMAT TEXT, COSTS OFF) SELECT * FROM things",
    "EXPLAIN (ANALYZE false) SELECT 1",
    "SELECT ç, 'ünïcode' FROM (SELECT 1 AS ç) q",
    "SELECT 1 UNION SELECT 2",
    "SELECT * FROM things ORDER BY id LIMIT 5 OFFSET 2",
    "SELECT string_agg(kind, ',') FROM things",
]

REFUSED = [
    ("UPDATE things SET amount = 0", "only SELECT"),
    ("DELETE FROM things", "only SELECT"),
    ("INSERT INTO things VALUES (1)", "only SELECT"),
    ("DROP TABLE things", "only SELECT"),
    ("CREATE TABLE x (id int)", "only SELECT"),
    ("ALTER TABLE things ADD COLUMN x int", "only SELECT"),
    ("TRUNCATE things", "only SELECT"),
    ("SET work_mem = '1GB'", "only SELECT"),
    ("RESET statement_timeout", "only SELECT"),
    ("SET statement_timeout = 0", "only SELECT"),
    ("BEGIN", "only SELECT"),
    ("COMMIT", "only SELECT"),
    ("COPY things TO '/tmp/x'", "only SELECT"),
    ("COPY (SELECT 1) TO PROGRAM 'id'", "only SELECT"),
    ("DO $$ BEGIN END $$", "only SELECT"),
    ("CALL something()", "only SELECT"),
    ("LOCK TABLE things", "only SELECT"),
    ("VACUUM", "only SELECT"),
    ("GRANT SELECT ON things TO public", "only SELECT"),
    ("SELECT 1; SELECT 2", "exactly one statement"),
    ("SELECT 1;\n-- x\nSELECT 2;", "exactly one statement"),
    ("SELECT * FROM public.secret", "not readable"),
    ("SELECT * FROM public.users LIMIT 1", "not readable"),
    ("SELECT * FROM pg_catalog.pg_roles", "not readable"),
    ("SELECT * FROM information_schema.tables", "not readable"),
    ("SELECT rolname FROM pg_catalog.pg_authid", "not readable"),
    ("SELECT pg_sleep(10)", "not allowed"),
    ("SELECT * FROM things WHERE amount > (SELECT pg_sleep(1))", "not allowed"),
    ("SELECT set_config('statement_timeout', '0', false)", "not allowed"),
    ("SELECT 1 WHERE set_config('a', 'b', true) IS NOT NULL", "not allowed"),
    ("SELECT pg_terminate_backend(1)", "not allowed"),
    ("SELECT pg_cancel_backend(1)", "not allowed"),
    ("SELECT pg_read_file('/etc/passwd')", "not allowed"),
    ("SELECT pg_read_binary_file('/etc/passwd')", "not allowed"),
    ("SELECT pg_ls_dir('/')", "not allowed"),
    ("SELECT pg_stat_file('/etc/passwd')", "not allowed"),
    ("SELECT lo_import('/etc/passwd')", "not allowed"),
    ("SELECT * FROM dblink('x', 'select 1') AS t(a int)", "not allowed"),
    ("SELECT pg_reload_conf()", "not allowed"),
    ("SELECT pg_notify('c', 'p')", "not allowed"),
    ("SELECT query_to_xml('select 1', true, false, '')", "not allowed"),
    ("SELECT xmlparse(document '<a/>')", "not allowed"),
    ("SELECT pg_advisory_lock(1)", "not allowed"),
    ("SELECT pg_catalog.pg_sleep(1)", "not allowed"),
    ("SELECT PG_SLEEP(1)", "not allowed"),  # case does not help
    ("SELECT * INTO new_table FROM things", "read-only"),
    ("SELECT * FROM things FOR UPDATE", "read-only"),
    ("SELECT * FROM things FOR SHARE", "read-only"),
    ("EXPLAIN ANALYZE SELECT * FROM things", "EXPLAIN ANALYZE"),
    ("EXPLAIN (ANALYZE) SELECT * FROM things", "EXPLAIN ANALYZE"),
    ("EXPLAIN (ANALYZE true, BUFFERS) SELECT 1", "EXPLAIN ANALYZE"),
    ("EXPLAIN UPDATE things SET amount = 1", "only EXPLAIN of a SELECT"),
    ("", "empty"),
    ("   ", "empty"),
    ("SELEC 1", "could not parse"),
    ("SELECT * FROM things WHERE", "could not parse"),
    ("SELECT 1 " + "+ 1 " * 8000, "longer than"),
]


@pytest.mark.parametrize("sql", ALLOWED)
def test_allowed(sql: str):
    guarded = guard(sql)
    assert guarded.kind in ("select", "explain")
    assert not guarded.sql.endswith(";")


@pytest.mark.parametrize("sql,reason", REFUSED)
def test_refused(sql: str, reason: str):
    with pytest.raises(GuardError) as raised:
        guard(sql)
    assert reason.lower() in str(raised.value).lower(), str(raised.value)




def test_analytics_prefix_is_fine():
    assert guard("SELECT * FROM analytics.things").kind == "select"
