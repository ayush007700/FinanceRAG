from functools import lru_cache

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")
    openai_chat_model: str = Field(default="gpt-4o", alias="OPENAI_CHAT_MODEL")
    # Cheap model for classification-style steps (answerability, routing)
    # where a full reasoning model buys nothing.
    openai_fast_model: str = Field(default="gpt-4o-mini", alias="OPENAI_FAST_MODEL")
    openai_vision_model: str = Field(default="gpt-4o", alias="OPENAI_VISION_MODEL")
    openai_embedding_model: str = Field(
        default="text-embedding-3-large", alias="OPENAI_EMBEDDING_MODEL"
    )
    openai_embedding_dimensions: int = Field(default=1536, alias="OPENAI_EMBEDDING_DIMENSIONS")

    cohere_api_key: str = Field(default="", alias="COHERE_API_KEY")
    # "cohere" -> hosted cross-encoder (fast, cheap, purpose-built)
    # "llm"    -> chat model scoring passages; ~3x the cost and seconds of added
    #             latency on the critical path. Retained for A/B comparison only.
    # "none"   -> trust RRF order and skip reranking entirely (free)
    rerank_provider: str = Field(default="cohere", alias="RERANK_PROVIDER")
    cohere_rerank_model: str = Field(default="rerank-v3.5", alias="COHERE_RERANK_MODEL")
    rerank_timeout_seconds: float = Field(default=10.0, alias="RERANK_TIMEOUT_SECONDS")
    rerank_max_retries: int = Field(default=2, alias="RERANK_MAX_RETRIES")

    database_url: str = Field(
        # 127.0.0.1 over localhost: localhost resolves to both ::1 and
        # 127.0.0.1, so a server bound to only one stack can silently take
        # the connection. Compose publishes Postgres on 5434 to stay clear
        # of a native PostgreSQL service on 5432.
        default="postgresql+psycopg://finrag:finrag@127.0.0.1:5434/finrag",
        alias="DATABASE_URL",
    )
    db_pool_size: int = Field(default=5, alias="DB_POOL_SIZE")
    db_max_overflow: int = Field(default=10, alias="DB_MAX_OVERFLOW")
    db_pool_timeout: int = Field(default=30, alias="DB_POOL_TIMEOUT")
    db_echo: bool = Field(default=False, alias="DB_ECHO")

    redis_url: str = Field(default="redis://localhost:6379/0", alias="REDIS_URL")
    cache_enabled: bool = Field(default=True, alias="CACHE_ENABLED")
    cache_ttl_seconds: int = Field(default=3600, alias="CACHE_TTL_SECONDS")
    cache_semantic_threshold: float = Field(default=0.92, alias="CACHE_SEMANTIC_THRESHOLD")
    cache_semantic_max_scan: int = Field(default=200, alias="CACHE_SEMANTIC_MAX_SCAN")

    langsmith_tracing: bool = Field(default=False, alias="LANGSMITH_TRACING")
    langsmith_api_key: str = Field(default="", alias="LANGSMITH_API_KEY")
    langsmith_project: str = Field(
        default="source-advisors-finance-rag", alias="LANGSMITH_PROJECT"
    )
    langsmith_endpoint: str = Field(
        default="https://api.smith.langchain.com", alias="LANGSMITH_ENDPOINT"
    )

    # Self-hosted Langfuse. Runs in-VPC, so traces of tax queries stay inside
    # the deployment -- the reason to prefer it over a SaaS tracer here.
    # Coexists with LangSmith rather than replacing it: LangSmith is the hosted
    # convenience for development, Langfuse is what can point at production.
    langfuse_enabled: bool = Field(default=False, alias="LANGFUSE_ENABLED")
    langfuse_public_key: str = Field(default="", alias="LANGFUSE_PUBLIC_KEY")
    langfuse_secret_key: str = Field(default="", alias="LANGFUSE_SECRET_KEY")
    langfuse_host: str = Field(default="http://localhost:3001", alias="LANGFUSE_HOST")
    langfuse_timeout_seconds: int = Field(default=10, alias="LANGFUSE_TIMEOUT_SECONDS")

    multimodal_enabled: bool = Field(default=True, alias="MULTIMODAL_ENABLED")
    media_dir: str = Field(default="data/media", alias="MEDIA_DIR")
    max_images_per_doc: int = Field(default=20, alias="MAX_IMAGES_PER_DOC")

    # --- indexing execution -----------------------------------------------
    # "ecs" runs indexing as its own task; "inline" runs it in-process, which is
    # only viable in development. The API service is sized for IO-bound request
    # serving (0.5 vCPU / 1 GiB) and cannot parse a large PDF corpus without
    # being OOM-killed, so inline is not a production option.
    index_runner: str = Field(default="inline", alias="INDEX_RUNNER")
    ecs_cluster: str = Field(default="", alias="ECS_CLUSTER")
    index_task_definition: str = Field(default="", alias="INDEX_TASK_DEFINITION")
    index_task_container: str = Field(default="index", alias="INDEX_TASK_CONTAINER")
    index_task_subnets: str = Field(default="", alias="INDEX_TASK_SUBNETS")
    index_task_security_groups: str = Field(default="", alias="INDEX_TASK_SECURITY_GROUPS")
    index_task_assign_public_ip: bool = Field(
        default=True, alias="INDEX_TASK_ASSIGN_PUBLIC_IP"
    )
    # Jobs still "running" past this are treated as abandoned by a killed worker.
    stale_job_minutes: int = Field(default=60, alias="STALE_JOB_MINUTES")

    # --- storage & tenancy -----------------------------------------------
    # Uploads must outlive a task. Without a bucket they land on the container
    # filesystem, which on Fargate disappears on redeploy and is invisible to
    # every other task.
    s3_bucket: str = Field(default="", alias="S3_BUCKET")
    s3_prefix: str = Field(default="", alias="S3_PREFIX")
    upload_dir: str = Field(default="data/uploads", alias="UPLOAD_DIR")

    # Single-tenant deployments keep the default; the column exists either way
    # so enabling tenancy later is a config change, not a migration.
    default_org_id: str = Field(default="default", alias="DEFAULT_ORG_ID")
    enforce_tenancy: bool = Field(default=True, alias="ENFORCE_TENANCY")

    # Effective dating. Off by default: a corpus without dates would retrieve
    # nothing if every document were treated as not-yet-effective.
    filter_by_effective_date: bool = Field(
        default=False, alias="FILTER_BY_EFFECTIVE_DATE"
    )

    app_env: str = Field(default="development", alias="APP_ENV")
    app_host: str = Field(default="0.0.0.0", alias="APP_HOST")
    app_port: int = Field(default=8000, alias="APP_PORT")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    cors_origins: str = Field(default="*", alias="CORS_ORIGINS")

    chunk_size: int = Field(default=800, alias="CHUNK_SIZE")
    chunk_overlap: int = Field(default=120, alias="CHUNK_OVERLAP")
    retrieval_top_k: int = Field(default=12, alias="RETRIEVAL_TOP_K")
    rerank_top_k: int = Field(default=5, alias="RERANK_TOP_K")
    max_context_tokens: int = Field(default=6000, alias="MAX_CONTEXT_TOKENS")

    # Reciprocal Rank Fusion. k dampens top-rank dominance so that agreement
    # across rankers outweighs being #1 in any single ranker (Cormack 2009).
    rrf_k: int = Field(default=60, alias="RRF_K")
    rrf_candidates: int = Field(default=50, alias="RRF_CANDIDATES")
    graph_expand_limit: int = Field(default=8, alias="GRAPH_EXPAND_LIMIT")
    graph_expand_weight: float = Field(default=0.5, alias="GRAPH_EXPAND_WEIGHT")

    # Absolute (un-normalised) cosine floor. RRF scores are rank-derived and
    # meaningless in absolute terms, so this reads raw similarity instead.
    #
    # This is only a cheap first filter for wholly off-domain questions. It
    # cannot decide answerability: measured on the golden set, answerable
    # questions score 0.591-0.799 and unanswerable ones 0.554-0.735, so the
    # distributions overlap almost completely and no threshold separates them.
    # Cosine measures topical similarity, not whether the passage contains the
    # requested fact. The answerability gate below does that job.
    min_absolute_cosine: float = Field(default=0.20, alias="MIN_ABSOLUTE_COSINE")

    # Explicit answerability gate: one cheap model call between retrieval and
    # generation. Refusing here also skips the expensive generation call, so on
    # unanswerable questions it costs less than it saves.
    answerability_check_enabled: bool = Field(
        default=True, alias="ANSWERABILITY_CHECK_ENABLED"
    )
    answerability_max_passages: int = Field(default=8, alias="ANSWERABILITY_MAX_PASSAGES")
    answerability_passage_chars: int = Field(default=1200, alias="ANSWERABILITY_PASSAGE_CHARS")

    # --- multi-agent orchestration -------------------------------------
    conversation_memory_enabled: bool = Field(
        default=True, alias="CONVERSATION_MEMORY_ENABLED"
    )
    long_term_memory_enabled: bool = Field(default=True, alias="LONG_TERM_MEMORY_ENABLED")
    audit_enabled: bool = Field(default=True, alias="AUDIT_ENABLED")

    # The Critic may send the Researcher back at most this many times. Bounded
    # because an unbounded self-correction loop is an unbounded bill.
    critic_max_retries: int = Field(default=1, alias="CRITIC_MAX_RETRIES")
    critic_enabled: bool = Field(default=True, alias="CRITIC_ENABLED")

    web_search_enabled: bool = Field(default=False, alias="WEB_SEARCH_ENABLED")
    # Accepts either name: TAVILY_API_KEY is Tavily's own convention,
    # TAVILY_WEB_SEARCH is what this deployment's .env uses.
    tavily_api_key: str = Field(
        default="",
        validation_alias=AliasChoices("TAVILY_API_KEY", "TAVILY_WEB_SEARCH"),
    )
    web_search_max_results: int = Field(default=5, alias="WEB_SEARCH_MAX_RESULTS")
    # Recency window for the news topic. Tax guidance changes by legislative
    # cycle, so a stale "current rate" is worse than no answer.
    web_search_days: int = Field(default=30, alias="WEB_SEARCH_DAYS")
    web_search_depth: str = Field(default="advanced", alias="WEB_SEARCH_DEPTH")
    # Cap per page: full articles blow the context budget, snippets are
    # too thin to verify a claim against.
    web_search_content_chars: int = Field(default=4000, alias="WEB_SEARCH_CONTENT_CHARS")
    # Comma-separated. Empty means no restriction. Preferring primary sources
    # matters more here than breadth: a tax figure from a content farm is a
    # liability even when it happens to be right.
    web_search_include_domains: str = Field(
        default="irs.gov,gov.uk,treasury.gov,federalregister.gov",
        alias="WEB_SEARCH_INCLUDE_DOMAINS",
    )
    web_search_timeout_seconds: float = Field(default=10.0, alias="WEB_SEARCH_TIMEOUT_SECONDS")

    guardrail_max_input_chars: int = Field(default=4000, alias="GUARDRAIL_MAX_INPUT_CHARS")
    guardrail_block_pii: bool = Field(default=True, alias="GUARDRAIL_BLOCK_PII")
    guardrail_require_citations: bool = Field(default=True, alias="GUARDRAIL_REQUIRE_CITATIONS")
    guardrail_refusal_on_low_confidence: bool = Field(
        default=True, alias="GUARDRAIL_REFUSAL_ON_LOW_CONFIDENCE"
    )

    aws_region: str = Field(default="ap-south-1", alias="AWS_REGION")
    aws_cloudwatch_namespace: str = Field(
        default="FinanceRAG/SourceAdvisors", alias="AWS_CLOUDWATCH_NAMESPACE"
    )
    otel_exporter_otlp_endpoint: str = Field(default="", alias="OTEL_EXPORTER_OTLP_ENDPOINT")
    enable_prometheus: bool = Field(default=True, alias="ENABLE_PROMETHEUS")

    company_name: str = "Source Advisors"
    company_tagline: str = (
        "Specialized tax consulting: R&D Tax Credit, Cost Segregation, "
        "Energy Efficiency (§179D & §45L), Sales & Use Tax, ITC/PTC, "
        "Commercial Property Tax, and LIFO inventory solutions."
    )


    @field_validator("index_runner")
    @classmethod
    def _validate_index_runner(cls, value: str) -> str:
        allowed = {"ecs", "inline"}
        normalised = value.strip().lower()
        if normalised not in allowed:
            raise ValueError(f"INDEX_RUNNER must be one of {sorted(allowed)}, got {value!r}")
        return normalised

    @field_validator("rerank_provider")
    @classmethod
    def _validate_rerank_provider(cls, value: str) -> str:
        allowed = {"cohere", "llm", "none"}
        normalised = value.strip().lower()
        if normalised not in allowed:
            raise ValueError(
                f"RERANK_PROVIDER must be one of {sorted(allowed)}, got {value!r}"
            )
        return normalised


@lru_cache
def get_settings() -> Settings:
    return Settings()
