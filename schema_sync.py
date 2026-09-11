"""Bring the live database in step with models.py, every time the app boots.

Adding a field to a model used to mean hand-writing a matching ALTER in
app.py's _migrate(). This walks the model metadata instead and adds whatever
the database turns out to be missing, so a new column is just a new column.

Deliberately additive only. It creates missing tables, columns and indexes and
nothing else: it never drops a column, never retypes one, and never touches
constraints. Those are the changes that can lose a day of billing, so they stay
manual, where a person can look at the data first. Anything it cannot do
safely is reported rather than forced.

Every statement runs in its own transaction, and a failure is logged and
stepped over -- a schema mismatch must never stop the till from opening.
"""

from sqlalchemy import inspect, text
from sqlalchemy.schema import CreateIndex


def _quote(name, dialect):
    return dialect.identifier_preparer.quote(name)


def _render_literal(value, dialect):
    """Render a Python default as a DDL literal, or None if we should not try."""
    if value is None:
        return 'NULL'
    # bool before int: bool is an int subclass, and TRUE/1 are not interchangeable.
    if isinstance(value, bool):
        if dialect.name == 'sqlite':
            return '1' if value else '0'
        return 'TRUE' if value else 'FALSE'
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    return None


def _default_literal(col, dialect):
    """The DEFAULT clause for a new column, or None if it has no usable one.

    Callable defaults (utcnow and friends) are applied by the ORM on insert,
    not by the database, so there is nothing to put in the DDL for them.
    """
    if col.server_default is not None:
        arg = getattr(col.server_default, 'arg', None)
        if arg is None:
            return None
        return str(getattr(arg, 'text', arg))
    default = col.default
    if default is None or not getattr(default, 'is_scalar', False):
        return None
    return _render_literal(default.arg, dialect)


def _add_column_sql(table, col, dialect):
    """(sql, relaxed) for adding col. `relaxed` means NOT NULL had to be dropped."""
    sql = (
        f'ALTER TABLE {_quote(table.name, dialect)} '
        f'ADD COLUMN {_quote(col.name, dialect)} {col.type.compile(dialect=dialect)}'
    )
    default = _default_literal(col, dialect)
    if default is not None:
        sql += f' DEFAULT {default}'
    if not col.nullable:
        if default is None:
            # NOT NULL with nothing to backfill fails the moment the table has
            # a single row. Add it nullable and say so; the model still treats
            # it as required, and a human can tighten it once it is populated.
            return sql, True
        sql += ' NOT NULL'
    return sql, False


def sync_schema(db, logger=None):
    """Add every table, column and index the models declare and the DB lacks.

    Returns a dict summarising what happened, so the caller can log or assert
    on it. Safe to run on every boot: it is a no-op once the schema matches.
    """
    engine = db.engine
    dialect = engine.dialect
    result = {'columns': [], 'indexes': [], 'relaxed': [], 'failed': []}

    def _run(sql_or_ddl, label):
        try:
            with engine.begin() as conn:
                if dialect.name == 'postgresql':
                    # An ALTER or CREATE INDEX waiting on another connection's
                    # lock would otherwise hang the boot indefinitely. Better to
                    # give up on the statement than to leave the till closed.
                    conn.execute(text("SET LOCAL lock_timeout = '5s'"))
                conn.execute(text(sql_or_ddl) if isinstance(sql_or_ddl, str) else sql_or_ddl)
            return True
        except Exception as exc:
            result['failed'].append((label, str(exc).strip().splitlines()[0]))
            if logger:
                logger.warning(f'schema sync could not apply {label}: {exc}')
            return False

    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    for table in db.metadata.sorted_tables:
        if table.name not in existing_tables:
            # create_all() has just built it in full, indexes included.
            continue

        have_columns = {c['name'] for c in inspector.get_columns(table.name)}
        for col in table.columns:
            if col.name in have_columns:
                continue
            sql, relaxed = _add_column_sql(table, col, dialect)
            label = f'{table.name}.{col.name}'
            if _run(sql, label):
                result['columns'].append(label)
                if relaxed:
                    result['relaxed'].append(label)
                    if logger:
                        logger.warning(
                            f'schema sync added {label} as NULL-able: the model marks it '
                            f'NOT NULL but gives no default to backfill existing rows'
                        )

        # create_all() only builds indexes for tables it creates, so an index
        # added to an existing model would otherwise never appear.
        have_indexes = {i['name'] for i in inspector.get_indexes(table.name)}
        for index in table.indexes:
            if index.name in have_indexes:
                continue
            if _run(CreateIndex(index, if_not_exists=True), f'index {index.name}'):
                result['indexes'].append(index.name)

    if logger:
        if result['columns'] or result['indexes']:
            logger.info(
                'schema sync: added '
                f'{len(result["columns"])} column(s) {result["columns"] or ""} and '
                f'{len(result["indexes"])} index(es) {result["indexes"] or ""}'
            )
        else:
            logger.debug('schema sync: database already matches the models')
    return result
