import json
import os
import sqlite3
import uuid
import time

import psycopg
from psycopg.rows import dict_row

DB_PATH = os.getenv("DB_PATH", "brandguard.db")


class PostgresCompat:
    """Compatibility wrapper for the existing SQLite-style database calls."""

    def __init__(self, connection):
        self.connection = connection

    def execute(self, sql, params=()):
        return self.connection.execute(sql.replace("?", "%s"), params)

    def executescript(self, script):
        for statement in script.split(";"):
            if statement.strip():
                self.execute(statement)

    def __enter__(self):
        self.connection.__enter__()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return self.connection.__exit__(exc_type, exc_value, traceback)


def conn():
    database_url = os.getenv("DATABASE_URL")

    if database_url:
        connection = psycopg.connect(database_url, row_factory=dict_row)
        return PostgresCompat(connection)

    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


# --------------------------------------------------
# DATABASE INITIALIZATION
# --------------------------------------------------

def init():
    with conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS reviews (
            id TEXT PRIMARY KEY,
            text TEXT,
            summary TEXT,
            mode TEXT,
            created_at REAL
        );

        CREATE TABLE IF NOT EXISTS findings (
            id TEXT PRIMARY KEY,
            review_id TEXT,
            rule_id TEXT,
            category TEXT,
            severity TEXT,
            evidence TEXT,
            explanation TEXT,
            suggested_fix TEXT,
            status TEXT DEFAULT 'pending',
            edited_fix TEXT
        );

        CREATE TABLE IF NOT EXISTS approval_workflow (
            review_id TEXT PRIMARY KEY,
            status TEXT DEFAULT 'draft',
            submitted_by TEXT,
            manager TEXT,
            decision_comment TEXT,
            created_at REAL,
            updated_at REAL,
            FOREIGN KEY(review_id) REFERENCES reviews(id)
        );

        CREATE TABLE IF NOT EXISTS workflow_history (
            id TEXT PRIMARY KEY,
            review_id TEXT,
            action TEXT,
            actor TEXT,
            comment TEXT,
            created_at REAL,
            FOREIGN KEY(review_id) REFERENCES reviews(id)
        );

        CREATE TABLE IF NOT EXISTS comments (
            id TEXT PRIMARY KEY,
            review_id TEXT,
            actor TEXT,
            message TEXT,
            created_at REAL,
            FOREIGN KEY(review_id) REFERENCES reviews(id)
        );

        CREATE TABLE IF NOT EXISTS revision_history (
            id TEXT PRIMARY KEY,
            review_id TEXT,
            actor TEXT,
            action TEXT,
            details TEXT,
            created_at REAL,
            FOREIGN KEY(review_id) REFERENCES reviews(id)
        );
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            email TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'Creator',
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at REAL NOT NULL
        );
        """)


# --------------------------------------------------
# REVIEW FUNCTIONS
# --------------------------------------------------

def save_review(text, findings, summary, mode):
    rid = uuid.uuid4().hex[:12]

    with conn() as c:
        c.execute(
            "INSERT INTO reviews VALUES (?, ?, ?, ?, ?)",
            (rid, text, summary, mode, time.time())
        )

        for f in findings:
            f["id"] = uuid.uuid4().hex[:10]
            f["status"] = "pending"

            c.execute("""
                INSERT INTO findings (
                    id, review_id, rule_id, category,
                    severity, evidence, explanation, suggested_fix
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                f["id"],
                rid,
                f["rule_id"],
                f["category"],
                f["severity"],
                f["evidence"],
                f["explanation"],
                f["suggested_fix"]
            ))

    add_revision(
        rid,
        "Creator",
        "draft_created",
        f"Created draft with {len(findings)} findings"
    )

    return rid


def get_review(rid):
    with conn() as c:
        r = c.execute(
            "SELECT * FROM reviews WHERE id = ?",
            (rid,)
        ).fetchone()

        if not r:
            return None

        findings = [
            dict(x) for x in c.execute(
                "SELECT * FROM findings WHERE review_id = ?",
                (rid,)
            )
        ]

    return {
        **dict(r),
        "findings": findings
    }


def list_reviews(limit=50):
    with conn() as c:
        rows = c.execute("""
            SELECT
                r.id,
                r.summary,
                r.mode,
                r.created_at,
                (
                    SELECT COUNT(*)
                    FROM findings f
                    WHERE f.review_id = r.id
                ) AS n_findings
            FROM reviews r
            ORDER BY r.created_at DESC
            LIMIT ?
        """, (limit,)).fetchall()

    return [dict(r) for r in rows]


# --------------------------------------------------
# FINDING DECISION
# --------------------------------------------------

def set_decision(
    rid,
    fid,
    status,
    edited_fix=None,
    actor="User"
):
    with conn() as c:
        cur = c.execute("""
            UPDATE findings
            SET status = ?, edited_fix = ?
            WHERE id = ? AND review_id = ?
        """, (status, edited_fix, fid, rid))

    if cur.rowcount > 0:
        add_revision(
            rid,
            actor,
            f"finding_{status}",
            f"Finding {fid} marked as {status}"
        )

    return cur.rowcount > 0


# --------------------------------------------------
# APPROVAL WORKFLOW
# --------------------------------------------------

def submit_for_approval(rid, submitted_by, manager=None):
    now = time.time()

    with conn() as c:
        exists = c.execute(
            "SELECT id FROM reviews WHERE id = ?",
            (rid,)
        ).fetchone()

        if not exists:
            return False

        c.execute("""
            INSERT INTO approval_workflow (
                review_id,
                status,
                submitted_by,
                manager,
                created_at,
                updated_at
            )
            VALUES (?, 'pending_approval', ?, ?, ?, ?)

            ON CONFLICT(review_id) DO UPDATE SET
                status = 'pending_approval',
                submitted_by = excluded.submitted_by,
                manager = excluded.manager,
                decision_comment = NULL,
                updated_at = excluded.updated_at
        """, (rid, submitted_by, manager, now, now))

        c.execute("""
            INSERT INTO workflow_history
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            uuid.uuid4().hex[:12],
            rid,
            "submitted",
            submitted_by,
            "Submitted for approval",
            now
        ))

    add_revision(
        rid,
        submitted_by,
        "submitted_for_approval",
        "Submitted for approval"
    )

    return True


def update_approval(rid, status, actor, comment=None):
    if status not in ("approved", "changes_requested"):
        return False

    now = time.time()

    with conn() as c:
        exists = c.execute(
            "SELECT review_id FROM approval_workflow WHERE review_id = ?",
            (rid,)
        ).fetchone()

        if not exists:
            return False

        c.execute("""
            UPDATE approval_workflow
            SET status = ?,
                decision_comment = ?,
                updated_at = ?
            WHERE review_id = ?
        """, (status, comment, now, rid))

        c.execute("""
            INSERT INTO workflow_history
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            uuid.uuid4().hex[:12],
            rid,
            status,
            actor,
            comment,
            now
        ))

    add_revision(
        rid,
        actor,
        status,
        comment
    )

    return True


def get_workflow(rid):
    with conn() as c:
        row = c.execute(
            "SELECT * FROM approval_workflow WHERE review_id = ?",
            (rid,)
        ).fetchone()

        history = c.execute("""
            SELECT *
            FROM workflow_history
            WHERE review_id = ?
            ORDER BY created_at ASC
        """, (rid,)).fetchall()

    if not row:
        return None

    return {
        **dict(row),
        "history": [dict(x) for x in history]
    }


# --------------------------------------------------
# COMMENTS
# --------------------------------------------------

def add_comment(rid, actor, message):
    now = time.time()

    with conn() as c:
        exists = c.execute(
            "SELECT id FROM reviews WHERE id = ?",
            (rid,)
        ).fetchone()

        if not exists:
            return None

        comment_id = uuid.uuid4().hex[:12]

        c.execute("""
            INSERT INTO comments (
                id, review_id, actor, message, created_at
            )
            VALUES (?, ?, ?, ?, ?)
        """, (
            comment_id,
            rid,
            actor,
            message,
            now
        ))

    return comment_id


def get_comments(rid):
    with conn() as c:
        rows = c.execute("""
            SELECT *
            FROM comments
            WHERE review_id = ?
            ORDER BY created_at ASC
        """, (rid,)).fetchall()

    return [dict(row) for row in rows]


# --------------------------------------------------
# REVISION HISTORY
# --------------------------------------------------

def add_revision(rid, actor, action, details=None):
    now = time.time()

    with conn() as c:
        exists = c.execute(
            "SELECT id FROM reviews WHERE id = ?",
            (rid,)
        ).fetchone()

        if not exists:
            return False

        c.execute("""
            INSERT INTO revision_history (
                id, review_id, actor, action, details, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            uuid.uuid4().hex[:12],
            rid,
            actor,
            action,
            details,
            now
        ))

    return True


def get_revisions(rid):
    with conn() as c:
        rows = c.execute("""
            SELECT *
            FROM revision_history
            WHERE review_id = ?
            ORDER BY created_at ASC
        """, (rid,)).fetchall()

    return [dict(row) for row in rows]
# --------------------------------------------------
# ANALYTICS
# --------------------------------------------------

def get_analytics():
    with conn() as c:

        # Review statistics
        review_stats = c.execute("""
            SELECT
                COUNT(*) AS total_reviews,
                SUM(CASE WHEN aw.status = 'approved' THEN 1 ELSE 0 END) AS approved,
                SUM(CASE WHEN aw.status = 'pending_approval' THEN 1 ELSE 0 END) AS pending,
                SUM(CASE WHEN aw.status = 'changes_requested' THEN 1 ELSE 0 END) AS changes_requested,
                SUM(CASE WHEN aw.review_id IS NULL THEN 1 ELSE 0 END) AS drafts
            FROM reviews r
            LEFT JOIN approval_workflow aw ON r.id = aw.review_id
        """).fetchone()

        # Finding severity
        severity_rows = c.execute("""
            SELECT severity, COUNT(*) AS total
            FROM findings
            GROUP BY severity
        """).fetchall()

        # Finding categories
        category_rows = c.execute("""
            SELECT category, COUNT(*) AS total
            FROM findings
            GROUP BY category
            ORDER BY total DESC
        """).fetchall()

        # Finding decisions
        decision_rows = c.execute("""
            SELECT status, COUNT(*) AS total
            FROM findings
            GROUP BY status
        """).fetchall()

        # Activity by role
        actor_rows = c.execute("""
            SELECT actor, COUNT(*) AS total
            FROM revision_history
            GROUP BY actor
            ORDER BY total DESC
        """).fetchall()

        # Recent activity
        activity_rows = c.execute("""
            SELECT
                rh.review_id,
                rh.actor,
                rh.action,
                rh.details,
                rh.created_at,
                r.summary
            FROM revision_history rh
            LEFT JOIN reviews r ON rh.review_id = r.id
            ORDER BY rh.created_at DESC
            LIMIT 10
        """).fetchall()

    return {
        "reviews": dict(review_stats),
        "severity": {
            row["severity"]: row["total"]
            for row in severity_rows
        },
        "categories": [
            {"category": row["category"], "total": row["total"]}
            for row in category_rows
        ],
        "decisions": {
            row["status"]: row["total"]
            for row in decision_rows
        },
        "actor_activity": [
            {"actor": row["actor"], "total": row["total"]}
            for row in actor_rows
        ],
        "recent_activity": [dict(row) for row in activity_rows]
    }