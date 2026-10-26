"""Runtime configuration. Everything comes from environment variables at call time
so tests can point the app at a temporary data directory."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURE_CORPUS = ROOT / "tests" / "fixtures" / "corpus"

# Settings that may only change through the server CLI (scripts/tenant_admin.py)
# or the server environment. The settings API refuses them with 403.
BOX_ONLY = {
    "live_meta_enabled",
    "meta_allowlist",
    "live_calendar_enabled",
    "live_channels_enabled",
    "ai_token_budget",
    "critique_token_budget",
}


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


@dataclass
class Config:
    data_dir: Path
    corpus_dir: Path
    brand: str
    owner_display: str
    port: int
    llm_provider: str
    llm_model: str
    disable_llm: bool
    ai_token_budget_default: int
    critique_token_budget_default: int
    builder_ai: bool
    builder_daily_cap: int
    cookie_secure: bool
    default_tenant: str
    session_hours: int
    embeddings: str
    workers: int
    third_party_kb: bool

    @property
    def tenants_dir(self) -> Path:
        return self.data_dir / "tenants"

    @property
    def registry_path(self) -> Path:
        return self.tenants_dir / "registry.db"

    @property
    def demo_db_path(self) -> Path:
        return self.data_dir / "demo.db"

    @property
    def index_dir(self) -> Path:
        return Path(os.environ.get("OPERATOR_INDEX_DIR", str(self.data_dir / "index")))

    @property
    def effective_corpus_dir(self) -> Path:
        """inputs/ when it holds material, else the fictional fixture corpus."""
        d = self.corpus_dir
        if d.exists() and any(p.suffix in (".txt", ".md") for p in d.rglob("*") if p.is_file()):
            return d
        return FIXTURE_CORPUS

    @property
    def using_fixture_corpus(self) -> bool:
        return self.effective_corpus_dir == FIXTURE_CORPUS


def get() -> Config:
    return Config(
        data_dir=Path(_env("OPERATOR_DATA_DIR", str(ROOT / "data"))),
        corpus_dir=Path(_env("OPERATOR_CORPUS_DIR", str(ROOT / "inputs"))),
        brand=_env("OPERATOR_BRAND", "Operator AI"),
        owner_display=_env("OPERATOR_OWNER_DISPLAY", "Programme owner"),
        port=_int("OPERATOR_PORT", 8850),
        llm_provider=_env("OPERATOR_LLM_PROVIDER", "anthropic"),
        llm_model=_env("OPERATOR_LLM_MODEL", "claude-sonnet-5-5"),
        disable_llm=_env("OPERATOR_DISABLE_LLM", "0") == "1",
        ai_token_budget_default=_int("OPERATOR_AI_TOKEN_BUDGET", 0),
        critique_token_budget_default=_int("OPERATOR_CRITIQUE_TOKEN_BUDGET", 0),
        builder_ai=_env("OPERATOR_BUILDER_AI", "1") != "0",
        builder_daily_cap=_int("OPERATOR_BUILDER_DAILY_CAP", 200),
        cookie_secure=_env("OPERATOR_COOKIE_SECURE", "0") == "1",
        default_tenant=_env("OPERATOR_DEFAULT_TENANT", "owner"),
        session_hours=_int("OPERATOR_SESSION_HOURS", 12),
        embeddings=_env("OPERATOR_EMBEDDINGS", "auto"),
        workers=_int("OPERATOR_WORKERS", 4),
        third_party_kb=_env("OPERATOR_THIRD_PARTY_KB", "0") == "1",
    )


def llm_key() -> str:
    """Model API key, read at call time and never stored anywhere."""
    cfg = get()
    names = {
        "anthropic": "ANTHROPIC_API_KEY",
        "openai": "OPENAI_API_KEY",
    }
    return os.environ.get(names.get(cfg.llm_provider, "OPERATOR_LLM_API_KEY"), "") or os.environ.get(
        "OPERATOR_LLM_API_KEY", ""
    )
