"""SQLite access helpers and schema."""

import json
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from . import domain

SCHEMA = """
CREATE TABLE IF NOT EXISTS contracts (
    downstream    TEXT PRIMARY KEY,
    columns_json  TEXT NOT NULL,
    fingerprint   TEXT NOT NULL,
    version       INTEGER NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS days (
    day          TEXT PRIMARY KEY,
    current_rev  INTEGER NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS publications (
    day                    TEXT NOT NULL,
    rev                    INTEGER NOT NULL,
    original_name          TEXT NOT NULL,
    original_size          INTEGER NOT NULL,
    original_sha256        TEXT NOT NULL,
    settlement_sha256      TEXT NOT NULL,
    settlement_size        INTEGER NOT NULL,
    ridership_sha256       TEXT NOT NULL,
    ridership_size         INTEGER NOT NULL,
    settlement_fingerprint TEXT NOT NULL,
    ridership_fingerprint  TEXT NOT NULL,
    settlement_version     INTEGER NOT NULL,
    ridership_version      INTEGER NOT NULL,
    settlement_columns     TEXT NOT NULL,
    ridership_columns      TEXT NOT NULL,
    row_count              INTEGER NOT NULL,
    created_at             TEXT NOT NULL,
    has_sale_day           INTEGER NOT NULL DEFAULT 0,
    audit_eligible         INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, rev)
);

CREATE TABLE IF NOT EXISTS previews (
    id                     TEXT PRIMARY KEY,
    day                    TEXT NOT NULL,
    base_rev               INTEGER NOT NULL,
    settlement_version     INTEGER NOT NULL,
    ridership_version      INTEGER NOT NULL,
    original_name          TEXT NOT NULL,
    original_size          INTEGER NOT NULL,
    original_sha256        TEXT NOT NULL,
    result_json            TEXT NOT NULL,
    convertible            INTEGER NOT NULL,
    created_at             TEXT NOT NULL,
    expires_at             TEXT NOT NULL,
    consumed_at            TEXT
);

CREATE TABLE IF NOT EXISTS app_settings (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_rows (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    day          TEXT NOT NULL,
    rev          INTEGER NOT NULL,
    row_index    INTEGER NOT NULL,
    source_line  INTEGER NOT NULL,
    ticket_id    TEXT NOT NULL,
    amount       INTEGER NOT NULL,
    sale_day     TEXT
);

CREATE INDEX IF NOT EXISTS idx_publications_day ON publications(day, rev);
CREATE INDEX IF NOT EXISTS idx_previews_expiry ON previews(expires_at);
CREATE INDEX IF NOT EXISTS idx_audit_day_rev ON audit_rows(day, rev);
CREATE INDEX IF NOT EXISTS idx_audit_ticket ON audit_rows(sale_day, ticket_id);
"""

AUDIT_ENABLED_KEY = "audit_enabled"
AUDIT_ENABLED_AT_KEY = "audit_enabled_at"

# Lightweight migration for databases created before the audit feature.
_MIGRATIONS = (
    (
        "has_sale_day",
        "ALTER TABLE publications ADD COLUMN has_sale_day INTEGER NOT NULL DEFAULT 0",
    ),
    (
        "audit_eligible",
        "ALTER TABLE publications ADD COLUMN audit_eligible INTEGER NOT NULL DEFAULT 0",
    ),
)

PREVIEW_TTL_SECONDS = 2 * 60 * 60


def utcnow():
    # Keep microseconds: enable/publish ordering within one second must be
    # distinguishable for the audit inclusion check.
    return datetime.now(timezone.utc)


def iso(moment):
    return moment.isoformat().replace("+00:00", "Z")


def connect(path):
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = FULL")
    return conn


@contextmanager
def transaction(conn, *, immediate=False):
    conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def init_db(conn):
    conn.executescript(SCHEMA)
    _migrate(conn)
    now = iso(utcnow())
    for downstream in domain.DOWNSTREAM_ORDER:
        columns = domain.DEFAULT_CONTRACTS[downstream]
        conn.execute(
            "INSERT OR IGNORE INTO contracts VALUES (?, ?, ?, ?, ?)",
            (
                downstream,
                json.dumps(columns, ensure_ascii=False),
                domain.fingerprint(columns),
                1,
                now,
            ),
        )
    # The audit feature ships disabled; the first explicit enable flips it.
    conn.execute(
        "INSERT OR IGNORE INTO app_settings (key, value) VALUES (?, ?)",
        (AUDIT_ENABLED_KEY, "0"),
    )


def _migrate(conn):
    existing = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(publications)").fetchall()
    }
    for column, statement in _MIGRATIONS:
        if column not in existing:
            conn.execute(statement)


def get_setting(conn, key, default=None):
    row = conn.execute(
        "SELECT value FROM app_settings WHERE key = ?", (key,)
    ).fetchone()
    return row["value"] if row else default


def set_setting(conn, key, value):
    conn.execute(
        """INSERT INTO app_settings (key, value) VALUES (?, ?)
           ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
        (key, str(value)),
    )


def get_audit_settings(conn):
    return {
        "enabled": get_setting(conn, AUDIT_ENABLED_KEY, "0") == "1",
        "enabled_at": get_setting(conn, AUDIT_ENABLED_AT_KEY),
    }


def set_audit_enabled(conn, enabled):
    """Toggle the audit feature.

    Enabling (including re-enabling after a disable) stamps the current UTC
    time: only revisions published at or after that point can enter audits.
    Disabling keeps the stored timestamp for history.
    """
    current = get_audit_settings(conn)
    now = iso(utcnow())
    set_setting(conn, AUDIT_ENABLED_KEY, "1" if enabled else "0")
    if enabled and not current["enabled"]:
        set_setting(conn, AUDIT_ENABLED_AT_KEY, now)
    return get_audit_settings(conn) | {"updated_at": now}


def insert_audit_rows(conn, day, rev, rows):
    conn.executemany(
        """INSERT INTO audit_rows
           (day, rev, row_index, source_line, ticket_id, amount, sale_day)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        [
            (
                day,
                rev,
                row["row_index"],
                row["source_line"],
                row["ticket_id"],
                row["amount"],
                row["sale_day"],
            )
            for row in rows
        ],
    )


def list_current_publications(conn, days):
    """Return the current-revision publication for each given day."""
    if not days:
        return []
    rows = conn.execute(
        """SELECT p.* FROM publications p
           JOIN days d ON d.day = p.day AND d.current_rev = p.rev
           WHERE p.day IN (%s)""" % ",".join("?" for _ in days),
        list(days),
    ).fetchall()
    return [dict(r) for r in rows]


def list_audit_rows(conn, day_revs):
    """Return audit rows for a list of (day, rev) pairs, ordered for display."""
    if not day_revs:
        return []
    clauses = " OR ".join("(day = ? AND rev = ?)" for _ in day_revs)
    params = [value for pair in day_revs for value in pair]
    rows = conn.execute(
        f"SELECT * FROM audit_rows WHERE {clauses} "
        "ORDER BY day, row_index, id",
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def get_contract(conn, downstream):
    row = conn.execute(
        """SELECT downstream, columns_json, fingerprint, version, updated_at
           FROM contracts WHERE downstream = ?""",
        (downstream,),
    ).fetchone()
    if row is None:
        return None
    return {
        "downstream": row["downstream"],
        "columns": json.loads(row["columns_json"]),
        "fingerprint": row["fingerprint"],
        "version": int(row["version"]),
        "updated_at": row["updated_at"],
    }


def get_contracts(conn):
    return {name: get_contract(conn, name) for name in domain.DOWNSTREAM_ORDER}


def update_contract(conn, downstream, columns):
    """Replace a contract and bump its version.  Never touches publications.

    A no-op edit (identical canonical columns) does not bump the version, so
    it cannot invalidate previews that are still pending confirmation.
    """
    fp = domain.fingerprint(columns)
    current = get_contract(conn, downstream)
    if current is not None and current["fingerprint"] == fp:
        return current
    conn.execute(
        """UPDATE contracts
           SET columns_json = ?, fingerprint = ?, version = version + 1, updated_at = ?
           WHERE downstream = ?""",
        (json.dumps(columns, ensure_ascii=False), fp, iso(utcnow()), downstream),
    )
    return get_contract(conn, downstream)


def day_revision(conn, day):
    row = conn.execute("SELECT current_rev FROM days WHERE day = ?", (day,)).fetchone()
    return int(row["current_rev"]) if row else 0


def get_publication(conn, day, rev):
    row = conn.execute(
        "SELECT * FROM publications WHERE day = ? AND rev = ?", (day, rev)
    ).fetchone()
    return dict(row) if row else None


def list_publications(conn, day, limit=200):
    rows = conn.execute(
        "SELECT * FROM publications WHERE day = ? ORDER BY rev DESC LIMIT ?", (day, limit)
    ).fetchall()
    return [dict(r) for r in rows]


def list_days(conn, limit=200):
    rows = conn.execute(
        """SELECT d.day, d.current_rev, d.updated_at,
                  (SELECT count(*) FROM publications p WHERE p.day = d.day) AS revision_count
           FROM days d ORDER BY d.day DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def create_preview(conn, *, preview_id, day, base_rev, contract_versions,
                   original_name, original_size, original_sha256, result,
                   convertible):
    now = utcnow()
    expires = now + timedelta(seconds=PREVIEW_TTL_SECONDS)
    conn.execute(
        """INSERT INTO previews (id, day, base_rev, settlement_version,
                                 ridership_version, original_name, original_size,
                                 original_sha256, result_json, convertible,
                                 created_at, expires_at, consumed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)""",
        (
            preview_id,
            day,
            base_rev,
            contract_versions["settlement"],
            contract_versions["ridership"],
            original_name,
            original_size,
            original_sha256,
            json.dumps(result, ensure_ascii=False),
            1 if convertible else 0,
            iso(now),
            iso(expires),
        ),
    )
    return iso(expires)


def get_preview(conn, preview_id):
    row = conn.execute("SELECT * FROM previews WHERE id = ?", (preview_id,)).fetchone()
    if row is None:
        return None
    item = dict(row)
    item["result"] = json.loads(item.pop("result_json"))
    return item


def delete_preview(conn, preview_id):
    conn.execute("DELETE FROM previews WHERE id = ?", (preview_id,))


def purge_expired_previews(conn, now=None):
    now = now or utcnow()
    rows = conn.execute(
        "SELECT id FROM previews WHERE expires_at < ?", (iso(now),)
    ).fetchall()
    ids = [r["id"] for r in rows]
    if ids:
        conn.executemany("DELETE FROM previews WHERE id = ?", [(i,) for i in ids])
    return ids


def wait_for_write_lock(conn, attempts=200, delay=0.05):
    """Best-effort helper used by tests to serialise around a busy database."""
    for _ in range(attempts):
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("ROLLBACK")
            return
        except sqlite3.OperationalError:
            time.sleep(delay)
    raise sqlite3.OperationalError("database is locked")
