"""Evaluation run history.

Until now each eval overwrote data/processed/eval_report.json and comparing runs
meant copying the file by hand -- data/processed/ still holds four such copies.
That loses the two things that matter: which configuration produced a number,
and which individual case moved.

Two tables because both questions get asked. ``eval_runs`` answers "did quality
change between these commits"; ``eval_cases`` answers "which question broke",
which is the one that actually leads to a fix.

Headline metrics are real columns rather than JSONB keys so run-over-run
comparison is a plain SQL join. The full summary is kept alongside so nothing is
lost when a metric is added later.

Revision ID: 0003
Revises: 0002
"""

from __future__ import annotations

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE eval_runs (
            run_id          BIGSERIAL PRIMARY KEY,
            label           TEXT,
            git_sha         TEXT,
            git_dirty       BOOLEAN NOT NULL DEFAULT false,
            -- Snapshot of the knobs that change retrieval or generation. A
            -- metric without the configuration that produced it is not a
            -- measurement, it is a number.
            config          JSONB NOT NULL DEFAULT '{}'::jsonb,
            summary         JSONB NOT NULL DEFAULT '{}'::jsonb,

            n_cases         INTEGER NOT NULL DEFAULT 0,
            n_answerable    INTEGER NOT NULL DEFAULT 0,
            n_refusal_cases INTEGER NOT NULL DEFAULT 0,

            hit_rate            DOUBLE PRECISION,
            mrr                 DOUBLE PRECISION,
            ndcg                DOUBLE PRECISION,
            precision_at_k      DOUBLE PRECISION,
            recall_at_k         DOUBLE PRECISION,
            abstention_accuracy DOUBLE PRECISION,
            refusal_recall      DOUBLE PRECISION,
            faithfulness        DOUBLE PRECISION,
            latency_ms_p50      DOUBLE PRECISION,
            hallucinated_citations INTEGER NOT NULL DEFAULT 0,

            -- Regression gate outcome, so a run records its own verdict.
            baseline_run_id BIGINT REFERENCES eval_runs (run_id) ON DELETE SET NULL,
            passed          BOOLEAN,
            failures        TEXT[] NOT NULL DEFAULT '{}',

            created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX eval_runs_created_idx ON eval_runs (created_at DESC)")
    op.execute("CREATE INDEX eval_runs_sha_idx ON eval_runs (git_sha)")

    op.execute(
        """
        CREATE TABLE eval_cases (
            run_id          BIGINT NOT NULL
                                REFERENCES eval_runs (run_id) ON DELETE CASCADE,
            case_id         TEXT NOT NULL,
            query           TEXT NOT NULL,
            service_line    TEXT,
            expect_refusal  BOOLEAN NOT NULL DEFAULT false,
            refused         BOOLEAN NOT NULL DEFAULT false,
            abstention_correct BOOLEAN,

            hit_rate        DOUBLE PRECISION,
            mrr             DOUBLE PRECISION,
            ndcg            DOUBLE PRECISION,
            precision_at_k  DOUBLE PRECISION,
            recall_at_k     DOUBLE PRECISION,
            faithfulness    DOUBLE PRECISION,

            top_cosine      DOUBLE PRECISION,
            latency_ms      DOUBLE PRECISION,
            hallucinated_ids TEXT[] NOT NULL DEFAULT '{}',

            PRIMARY KEY (run_id, case_id)
        )
        """
    )
    # Case-level history across runs: "when did rd-003 start failing?"
    op.execute("CREATE INDEX eval_cases_case_idx ON eval_cases (case_id, run_id DESC)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS eval_cases")
    op.execute("DROP TABLE IF EXISTS eval_runs")
