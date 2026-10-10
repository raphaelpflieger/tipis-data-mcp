"""Verify the exposure committed by Rails and the reader's effective privileges.

analytics._contract is operator-owned; the reader has SELECT only. Rails marks it pending
before rebuilding views and records their definitions atomically on success.
"""

DEFINITIONS_SQL = """
SELECT COALESCE(jsonb_object_agg(c.relname,
  CASE WHEN c.relkind = 'v' THEN md5((SELECT ev_action::text FROM pg_rewrite WHERE ev_class = c.oid AND rulename = '_RETURN') ||
          (SELECT jsonb_agg(jsonb_build_array(a.attname, a.atttypid, a.atttypmod) ORDER BY a.attnum)::text
           FROM pg_attribute a WHERE a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped)) ELSE c.relkind::text END), '{}')
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'analytics' AND c.relname <> '_contract' AND c.relkind IN ('v', 'm', 'r', 'p', 'f')
"""

CHECK_SQL = f"""
SELECT expected = applied AND applied IS NOT NULL AND definitions = ({DEFINITIONS_SQL}),
       NOT (r.rolsuper OR r.rolcreaterole OR r.rolcreatedb OR r.rolbypassrls),
       NOT EXISTS (
         SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
           AND c.relname NOT IN ('spatial_ref_sys', 'geometry_columns', 'geography_columns', 'raster_columns', 'raster_overviews')
           AND (has_table_privilege(current_user, c.oid, 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
                OR has_any_column_privilege(current_user, c.oid, 'SELECT,INSERT,UPDATE,REFERENCES'))
       ),
       NOT EXISTS (
         SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = 'analytics' AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
           AND (NOT has_table_privilege(current_user, c.oid, 'SELECT')
                OR has_table_privilege(current_user, c.oid, 'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
                OR has_any_column_privilege(current_user, c.oid, 'INSERT,UPDATE,REFERENCES'))
       ),
       NOT has_schema_privilege(current_user, 'analytics', 'CREATE'),
       md5(applied || definitions::text)
FROM analytics._contract CROSS JOIN pg_roles r
WHERE id = 1 AND r.rolname = current_user
"""


async def verify(connection) -> str:
    from .db import QueryError

    row = await (await connection.execute(CHECK_SQL)).fetchone()
    if not row or not all(value is True for value in row[:-1]):
        raise QueryError("analytics contract or reader permissions are out of date; operator must sync and verify the views")
    return row[-1]
