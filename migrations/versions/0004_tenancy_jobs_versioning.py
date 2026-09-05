"""Tenancy, async indexing jobs, and document effective dating.

Three gaps that block real deployment:

*Tenancy.* Every row was globally visible. One org could retrieve another's
documents, which for a firm serving competing clients is the failure that ends
the engagement. ``org_id`` is added to documents and chunks and threaded through
retrieval.

*Async indexing.* ``/v1/index`` ran inside the request, so a real corpus timed
out the connection with no way to observe progress or retry. ``index_jobs``
makes it a tracked background job.

*Effective dating.* ``documents.effective_date`` existed but nothing read it.
Tax guidance is superseded by legislative cycle, so answering a 2024 question
from a 2019 document is a correctness bug, not a ranking one. A supersession
window makes "as of" answerable.

The default org exists so existing rows stay reachable: adding tenancy must not
orphan an already-indexed corpus.

Revision ID: 0004
Revises: 0003
"""

from __future__ import annotations

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

DEFAULT_ORG = "default"


def upgrade() -> None:
    # ------------------------------------------------------------ tenancy
    for table in ("documents", "chunks"):
        op.execute(
            f"ALTER TABLE {table} ADD COLUMN org_id TEXT NOT NULL DEFAULT '{DEFAULT_ORG}'"
        )
    op.execute("CREATE INDEX documents_org_idx ON documents (org_id)")
    # Partial vector index per tenant is impractical, so org_id joins the
    # existing filter index instead and is applied as a predicate.
    op.execute("CREATE INDEX chunks_org_level_idx ON chunks (org_id, level)")

    # ------------------------------------------------- effective dating
    op.execute("ALTER TABLE documents ADD COLUMN superseded_date DATE")
    op.execute("ALTER TABLE chunks ADD COLUMN effective_date DATE")
    op.execute("ALTER TABLE chunks ADD COLUMN superseded_date DATE")
    # Range predicate on every dated query: index both bounds.
    op.execute(
        "CREATE INDEX chunks_effective_window_idx ON chunks (effective_date, superseded_date)"
    )

    # --------------------------------------------------------------- jobs
    op.execute(
        """
        CREATE TABLE index_jobs (
            job_id       BIGSERIAL PRIMARY KEY,
            org_id       TEXT NOT NULL DEFAULT 'default',
            status       TEXT NOT NULL DEFAULT 'queued',
            paths        TEXT[] NOT NULL DEFAULT '{}',
            source_uri   TEXT,
            documents    INTEGER NOT NULL DEFAULT 0,
            chunks       INTEGER NOT NULL DEFAULT 0,
            image_chunks INTEGER NOT NULL DEFAULT 0,
            entity_links INTEGER NOT NULL DEFAULT 0,
            error        TEXT,
            -- Retry bookkeeping: a job that dies mid-run must be visibly
            -- retryable rather than silently stuck in "running".
            attempts     INTEGER NOT NULL DEFAULT 0,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
            started_at   TIMESTAMPTZ,
            finished_at  TIMESTAMPTZ,
            CONSTRAINT index_jobs_status_check
                CHECK (status IN ('queued', 'running', 'succeeded', 'failed'))
        )
        """
    )
    op.execute("CREATE INDEX index_jobs_status_idx ON index_jobs (status, created_at)")
    op.execute("CREATE INDEX index_jobs_org_idx ON index_jobs (org_id, created_at DESC)")

    # Audit gains the tenant so the compliance record is per-org too.
    op.execute("CREATE INDEX query_audit_org_idx ON query_audit (org_id, created_at DESC)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS index_jobs")
    op.execute("DROP INDEX IF EXISTS query_audit_org_idx")
    op.execute("DROP INDEX IF EXISTS chunks_effective_window_idx")
    op.execute("DROP INDEX IF EXISTS chunks_org_level_idx")
    op.execute("DROP INDEX IF EXISTS documents_org_idx")
    for table in ("documents", "chunks"):
        op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS org_id")
    op.execute("ALTER TABLE documents DROP COLUMN IF EXISTS superseded_date")
    op.execute("ALTER TABLE chunks DROP COLUMN IF EXISTS effective_date")
    op.execute("ALTER TABLE chunks DROP COLUMN IF EXISTS superseded_date")
