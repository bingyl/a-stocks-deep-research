from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

# app/core/config.py -> 项目根目录
BASE_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = BASE_DIR / "data"
load_dotenv(BASE_DIR / ".env")


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


class Settings:
    llm_model: str
    llm_api_key: str
    llm_base_url: str
    bocha_api_key: str
    bocha_base_url: str
    agent_max_tool_rounds: int
    followup_tool_run_limit: int
    followup_model_run_limit: int
    database_url: str
    db_pool_size: int
    db_max_overflow: int
    db_pool_recycle: int
    # 向量库（见 app.persistence.vectorstore）：chroma 本地 / milvus 生产
    vector_backend: str
    vector_chroma_dir: str
    vector_milvus_uri: str
    vector_milvus_token: str
    vector_milvus_db: str
    # debug / embedding / RAG
    debug: bool
    embedding_api_key: str
    embedding_base_url: str
    embedding_model: str
    embedding_dim: int
    embedding_batch_size: int
    rag_enabled: bool
    rag_followup_tool: bool
    rag_collection: str
    rag_parent_chunk_size: int
    rag_child_chunk_size: int
    rag_child_chunk_overlap: int
    rag_json_parent_chunk_size: int
    rag_json_child_chunk_size: int
    rag_json_child_chunk_overlap: int
    rag_max_output_chars: int
    rag_stale_hours: int

    def __init__(self) -> None:
        self.llm_model = os.getenv("LLM_MODEL", "deepseek-v4-flash").strip()
        self.llm_api_key = os.getenv("LLM_API_KEY", "").strip()
        self.llm_base_url = os.getenv("LLM_BASE_URL", "https://api.deepseek.com").strip()
        self.bocha_api_key = os.getenv("BOCHA_API_KEY", "").strip()
        self.bocha_base_url = os.getenv(
            "BOCHA_BASE_URL", "https://api.bochaai.com"
        ).strip().rstrip("/")
        self.agent_max_tool_rounds = int(os.getenv("AGENT_MAX_TOOL_ROUNDS", "16"))
        self.followup_tool_run_limit = int(os.getenv("FOLLOWUP_TOOL_RUN_LIMIT", "16"))
        self.followup_model_run_limit = int(os.getenv("FOLLOWUP_MODEL_RUN_LIMIT", "24"))
        default_sqlite = (
            f"sqlite+aiosqlite:///{(BASE_DIR / 'data' / 'app_local_sqlite.db').as_posix()}"
        )
        self.database_url = os.getenv("DATABASE_URL", default_sqlite).strip()
        self.db_pool_size = int(os.getenv("DB_POOL_SIZE", "5"))
        self.db_max_overflow = int(os.getenv("DB_MAX_OVERFLOW", "10"))
        self.db_pool_recycle = int(os.getenv("DB_POOL_RECYCLE", "1800"))
        self.vector_backend = os.getenv("VECTOR_BACKEND", "chroma").strip().lower()
        self.vector_chroma_dir = os.getenv(
            "VECTOR_CHROMA_DIR", "./data/vector_chroma"
        ).strip()
        self.vector_milvus_uri = os.getenv(
            "VECTOR_MILVUS_URI", "http://127.0.0.1:19530"
        ).strip()
        self.vector_milvus_token = os.getenv("VECTOR_MILVUS_TOKEN", "").strip()
        self.vector_milvus_db = os.getenv("VECTOR_MILVUS_DB", "default").strip()

        self.debug = _env_bool("DEBUG", False)
        self.embedding_api_key = os.getenv("EMBEDDING_API_KEY", "").strip()
        self.embedding_base_url = os.getenv(
            "EMBEDDING_BASE_URL",
            "https://dashscope.aliyuncs.com/compatible-mode/v1",
        ).strip().rstrip("/")
        self.embedding_model = os.getenv("EMBEDDING_MODEL", "text-embedding-v4").strip()
        self.embedding_dim = int(os.getenv("EMBEDDING_DIM", "1024"))
        # DashScope text-embedding-v4 单请求上限 10
        self.embedding_batch_size = max(
            1, min(int(os.getenv("EMBEDDING_BATCH_SIZE", "10")), 10)
        )
        # 总开关：分析/追问过程是否把工具产出写入向量库
        self.rag_enabled = _env_bool("RAG_ENABLED", True)
        # 追问是否挂载 rag_search（需同时 RAG_ENABLED=true 才生效）
        self.rag_followup_tool = _env_bool("RAG_FOLLOWUP_TOOL", True)
        self.rag_collection = os.getenv("RAG_COLLECTION", "a_share_rag").strip()
        # 文本 / 网页切分
        self.rag_parent_chunk_size = int(os.getenv("RAG_PARENT_CHUNK_SIZE", "1200"))
        self.rag_child_chunk_size = int(os.getenv("RAG_CHILD_CHUNK_SIZE", "400"))
        self.rag_child_chunk_overlap = int(os.getenv("RAG_CHILD_CHUNK_OVERLAP", "80"))
        # JSON（财务工具等）单独放宽，避免指标块被切太碎
        self.rag_json_parent_chunk_size = int(
            os.getenv("RAG_JSON_PARENT_CHUNK_SIZE", "2400")
        )
        self.rag_json_child_chunk_size = int(
            os.getenv("RAG_JSON_CHILD_CHUNK_SIZE", "560")
        )
        self.rag_json_child_chunk_overlap = int(
            os.getenv("RAG_JSON_CHILD_CHUNK_OVERLAP", "100")
        )
        self.rag_max_output_chars = int(os.getenv("RAG_MAX_OUTPUT_CHARS", "80000"))
        # 知识库材料超过该小时数视为过期，追问应提示重新 web_search
        self.rag_stale_hours = int(os.getenv("RAG_STALE_HOURS", "24"))

        # JWT 用户认证（关闭时行为与旧版一致：报告全局可见、同 code 全局互斥）
        self.auth_enabled = _env_bool("AUTH_ENABLED", False)
        self.jwt_secret = os.getenv("JWT_SECRET", "").strip()
        self.jwt_expire_minutes = int(os.getenv("JWT_EXPIRE_MINUTES", "10080"))
        self.auth_allow_register = _env_bool("AUTH_ALLOW_REGISTER", False)
        self.auth_bootstrap_username = os.getenv("AUTH_BOOTSTRAP_USERNAME", "").strip()
        self.auth_bootstrap_password = os.getenv("AUTH_BOOTSTRAP_PASSWORD", "").strip()
        # true：启动时把引导账号密码同步为 AUTH_BOOTSTRAP_PASSWORD（改密后一次性打开即可）
        self.auth_bootstrap_sync_password = _env_bool("AUTH_BOOTSTRAP_SYNC_PASSWORD", False)

    def rag_ingest_enabled(self) -> bool:
        """深研/追问工具产出是否入库向量库。"""
        return bool(self.rag_enabled)

    def rag_followup_enabled(self) -> bool:
        """追问 Agent 是否提供 rag_search 工具。"""
        return bool(self.rag_enabled and self.rag_followup_tool)

    def require_llm(self) -> None:
        if not self.llm_api_key:
            raise RuntimeError("未配置 LLM_API_KEY，请在 .env 中填写")

    def require_bocha(self) -> None:
        if not self.bocha_api_key:
            raise RuntimeError("未配置 BOCHA_API_KEY，请在 .env 中填写")

    def require_embedding(self) -> None:
        if not self.embedding_api_key:
            raise RuntimeError("未配置 EMBEDDING_API_KEY，请在 .env 中填写")

    def require_auth_secret(self) -> None:
        if self.auth_enabled and (
            not self.jwt_secret or self.jwt_secret in {"change-me", "changeme"}
        ):
            raise RuntimeError(
                "已开启 AUTH_ENABLED，请在 .env 中设置足够强度的 JWT_SECRET"
            )


@lru_cache
def get_settings() -> Settings:
    return Settings()


setting = get_settings()
