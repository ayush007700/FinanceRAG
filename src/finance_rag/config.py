from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")
    openai_chat_model: str = Field(default="gpt-4o", alias="OPENAI_CHAT_MODEL")
    openai_embedding_model: str = Field(
        default="text-embedding-3-large", alias="OPENAI_EMBEDDING_MODEL"
    )
    openai_embedding_dimensions: int = Field(default=3072, alias="OPENAI_EMBEDDING_DIMENSIONS")

    cohere_api_key: str = Field(default="", alias="COHERE_API_KEY")
    use_cohere_rerank: bool = Field(default=False, alias="USE_COHERE_RERANK")

    neo4j_uri: str = Field(default="bolt://localhost:7687", alias="NEO4J_URI")
    neo4j_user: str = Field(default="neo4j", alias="NEO4J_USER")
    neo4j_password: str = Field(default="password", alias="NEO4J_PASSWORD")
    neo4j_database: str = Field(default="neo4j", alias="NEO4J_DATABASE")

    app_env: str = Field(default="development", alias="APP_ENV")
    app_host: str = Field(default="0.0.0.0", alias="APP_HOST")
    app_port: int = Field(default=8000, alias="APP_PORT")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    cors_origins: str = Field(default="*", alias="CORS_ORIGINS")

    chunk_size: int = Field(default=800, alias="CHUNK_SIZE")
    chunk_overlap: int = Field(default=120, alias="CHUNK_OVERLAP")
    retrieval_top_k: int = Field(default=12, alias="RETRIEVAL_TOP_K")
    rerank_top_k: int = Field(default=5, alias="RERANK_TOP_K")
    hybrid_alpha: float = Field(default=0.65, alias="HYBRID_ALPHA")
    max_context_tokens: int = Field(default=6000, alias="MAX_CONTEXT_TOKENS")

    guardrail_max_input_chars: int = Field(default=4000, alias="GUARDRAIL_MAX_INPUT_CHARS")
    guardrail_block_pii: bool = Field(default=True, alias="GUARDRAIL_BLOCK_PII")
    guardrail_require_citations: bool = Field(default=True, alias="GUARDRAIL_REQUIRE_CITATIONS")
    guardrail_refusal_on_low_confidence: bool = Field(
        default=True, alias="GUARDRAIL_REFUSAL_ON_LOW_CONFIDENCE"
    )
    guardrail_min_relevance: float = Field(default=0.35, alias="GUARDRAIL_MIN_RELEVANCE")

    aws_region: str = Field(default="us-east-1", alias="AWS_REGION")
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


@lru_cache
def get_settings() -> Settings:
    return Settings()
