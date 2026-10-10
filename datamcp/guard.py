"""The SQL guard: what a query may be before it reaches the database.

Defence in depth — the role is already read-only and blind to public.* — but a guard that
understands SQL turns a class of surprises (COPY TO PROGRAM, pg_sleep loops, SET work_mem,
pg_read_file) into a readable refusal before any connection is used. Parsing is done by pglast
(the real PostgreSQL parser), so comments, dollar quoting and semicolons inside strings are
handled by the parser and not by regexes.
"""

from __future__ import annotations

from dataclasses import dataclass

from pglast import ast, parse_sql
from pglast.parser import ParseError
from pglast.visitors import Visitor

ALLOWED_SCHEMAS = {"analytics"}
# function names refused wherever they appear (prefix match on the qualified or bare name)
FORBIDDEN_FUNCTION_PREFIXES = (
    "set_config", "pg_sleep", "pg_terminate_backend", "pg_cancel_backend", "pg_read_", "pg_ls_",
    "pg_stat_file", "lo_", "dblink", "pg_reload_conf", "pg_notify", "query_to_xml", "xmlparse",
    "pg_advisory", "pg_try_advisory", "pg_current_logfile", "pg_rotate_logfile", "pg_file_",
    "pg_backup_", "pg_switch_wal", "pg_create_", "pg_drop_", "pg_replication",
    "brin_summarize", "gin_clean_pending_list", "txid_", "pg_export_snapshot",
)
MAX_SQL_LENGTH = 20_000


class GuardError(ValueError):
    """A refusal, worded for the model that wrote the query."""


@dataclass(frozen=True)
class GuardedQuery:
    kind: str  # "select" | "explain"
    sql: str  # the original statement, trimmed, without a trailing semicolon


class _Inspector(Visitor):
    def __init__(self) -> None:
        self.problems: list[str] = []

    def visit_RangeVar(self, ancestors, node: ast.RangeVar):  # noqa: N802 (pglast naming)
        schema = node.schemaname
        if schema and schema not in ALLOWED_SCHEMAS:
            self.problems.append(
                f"'{schema}.{node.relname}' is not readable: only the analytics schema is exposed "
                f"(write analytics.{node.relname} or just {node.relname})"
            )

    def visit_FuncCall(self, ancestors, node: ast.FuncCall):  # noqa: N802
        name = ".".join(part.sval for part in node.funcname if isinstance(part, ast.String))
        bare = name.split(".")[-1]
        if any(bare.startswith(prefix) for prefix in FORBIDDEN_FUNCTION_PREFIXES):
            self.problems.append(f"the function {name}() is not allowed on this connector")

    def visit_SelectStmt(self, ancestors, node: ast.SelectStmt):  # noqa: N802
        if node.intoClause is not None:
            self.problems.append("SELECT INTO is not allowed: this connector is read-only")
        if node.lockingClause:
            self.problems.append("FOR UPDATE / FOR SHARE are not allowed: this connector is read-only")

    def visit_CopyStmt(self, ancestors, node):  # noqa: N802
        self.problems.append("COPY is not allowed")

    def visit_XmlExpr(self, ancestors, node):  # noqa: N802 — XMLPARSE/XMLSERIALIZE are expressions, not FuncCalls
        self.problems.append("XML expressions (xmlparse, xmlserialize…) are not allowed on this connector")


def guard(sql: str) -> GuardedQuery:
    """Return the statement to run, or raise GuardError with a model-readable reason."""
    text = (sql or "").strip()
    if not text:
        raise GuardError("the query is empty")
    if len(text) > MAX_SQL_LENGTH:
        raise GuardError(f"the query is longer than {MAX_SQL_LENGTH} characters")
    try:
        statements = parse_sql(text)
    except ParseError as error:
        raise GuardError(f"PostgreSQL could not parse the query: {error}") from error
    if len(statements) != 1:
        raise GuardError("send exactly one statement (no semicolon-separated batches)")

    statement = statements[0].stmt
    kind = _kind(statement)
    inspector = _Inspector()
    inspector(statement)
    if inspector.problems:
        raise GuardError("; ".join(dict.fromkeys(inspector.problems)))
    return GuardedQuery(kind=kind, sql=text.rstrip(";").strip())


def _kind(statement: ast.Node) -> str:
    if isinstance(statement, ast.SelectStmt):
        return "select"
    if isinstance(statement, ast.ExplainStmt):
        for option in statement.options or ():
            if isinstance(option, ast.DefElem) and option.defname.lower() == "analyze":
                if _truthy(option.arg):
                    raise GuardError("EXPLAIN ANALYZE runs the query: use EXPLAIN alone")
        if not isinstance(statement.query, ast.SelectStmt):
            raise GuardError("only EXPLAIN of a SELECT is allowed")
        return "explain"
    name = type(statement).__name__.replace("Stmt", "").upper()
    raise GuardError(f"only SELECT (and EXPLAIN of a SELECT) is allowed on this connector, not {name}")


def _truthy(arg: ast.Node | None) -> bool:
    if arg is None:
        return True
    if isinstance(arg, ast.String):
        return arg.sval.lower() not in ("off", "false", "0", "no")
    if isinstance(arg, ast.Integer):
        return arg.ival != 0
    return True
