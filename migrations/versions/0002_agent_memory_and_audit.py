"""Agent memory and audit trail.

Four memory tiers all land in this one database, so the multi-agent system needs
no additional infrastructure:

  working    -- LangGraph AgentState, in process, one request
  short-term -- LangGraph PostgresSaver checkpointer, keyed by thread_id
                (creates and owns its own tables via .setup())
  long-term  -- agent_memories, namespaced per user/org, embedded for recall
  episodic   -- query_audit, append-only

query_audit is the compliance requirement and the eval feedback source at once:
it records what was asked, what was retrieved, what was cited and which model
answered, so an answer can be reconstructed months later.

Revision ID: 0002
Revises: 0001
"""

from __future__ import annotations

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

EMBEDDING_DIM = 1536


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE query_audit (
            audit_id        BIGSERIAL PRIMARY KEY,
            trace_id        TEXT NOT NULL,
            thread_id       TEXT,
            user_id         TEXT,
            org_id          TEXT,
            query           TEXT NOT NULL,
            rewritten_query TEXT,
            service_line    TEXT,
            answer          TEXT,
            refused         BOOLEAN NOT NULL DEFAULT false,
            refusal_reason  TEXT,
            retrieved_ids   TEXT[] NOT NULL DEFAULT '{}',
            cited_ids       TEXT[] NOT NULL DEFAULT '{}',
            hallucinated_ids TEXT[] NOT NULL DEFAULT '{}',
            -- Which models produced this answer. Floating aliases move under
            -- you, so the answer cannot be reconstructed without recording them.
            models          JSONB NOT NULL DEFAULT '{}'::jsonb,
            route           TEXT,
            critic_attempts INTEGER NOT NULL DEFAULT 0,
            metrics         JSONB NOT NULL DEFAULT '{}'::jsonb,
            guardrails      TEXT[] NOT NULL DEFAULT '{}',
            latency_ms      DOUBLE PRECISION,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX query_audit_trace_idx ON query_audit (trace_id)")
    op.execute("CREATE INDEX query_audit_thread_idx ON query_audit (thread_id, created_at)")
    op.execute("CREATE INDEX query_audit_created_idx ON query_audit (created_at DESC)")
    op.execute("CREATE INDEX query_audit_refused_idx ON query_audit (refused) WHERE refused")

    op.execute(
        f"""
        CREATE TABLE agent_memories (
            memory_id   BIGSERIAL PRIMARY KEY,
            namespace   TEXT NOT NULL,
            key         TEXT NOT NULL,
            content     TEXT NOT NULL,
            kind        TEXT NOT NULL DEFAULT 'fact',
            metadata    JSONB NOT NULL DEFAULT '{{}}'::jsonb,
            embedding   vector({EMBEDDING_DIM}),
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (namespace, key)
        )
        """
    )
    # Recall is by similarity within a namespace, so the index mirrors the
    # chunks table: HNSW on cosine distance.
    op.execute(
        """
        CREATE INDEX agent_memories_embedding_idx ON agent_memories
        USING hnsw (embedding vector_cosine_ops)
        """
    )
    op.execute("CREATE INDEX agent_memories_namespace_idx ON agent_memories (namespace)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS agent_memories")
    op.execute("DROP TABLE IF EXISTS query_audit")
