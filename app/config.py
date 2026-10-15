from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")

    db_backend: str = "sqlite"  # sqlite | matrixone
    sqlite_path: str = "data/rfq.db"
    mo_host: str = ""
    mo_port: int = 6001
    mo_user: str = ""
    mo_password: str = ""
    mo_database: str = "rfq_agent_v2"
    mo_connect_timeout: int = 10
    mo_read_timeout: int = 120
    mo_write_timeout: int = 60
    mo_diff_timeout: int = 12  # DATA BRANCH DIFF 专用短超时；失败时应用层有界降级
    mo_init_command: str = ""  # 工作区库连接串中的 --init-command，如 SET ROLE `moi_role_1`;SET enable_remap_hint = 1

    memory_backend: str = "local"  # local | git4data | memoria
    memoria_url: str = ""
    memoria_api_key: str = ""

    llm_base_url: str = ""
    llm_api_key: str = ""
    model_fast: str = "deepseek-v4.1-flash"
    model_strong: str = "deepseek-v4-pro"
    model_vision: str = "qwen3.8-max"

    api_base: str = "http://127.0.0.1:8000"
    upload_dir: str = "data/uploads"
    output_dir: str = "outputs"

    # 公网演示：管理接口须同时打开开关并携带 X-Admin-Token；写接口按客户端 IP 限流（0 关闭）
    demo_mode: bool = False
    admin_token: str = ""
    allow_admin_reset: bool = False
    rate_limit_writes: int = 30
    rate_limit_window: int = 60

    @property
    def llm_enabled(self) -> bool:
        return bool(self.llm_base_url and self.llm_api_key)

    def path(self, rel: str) -> Path:
        p = Path(rel)
        p = p if p.is_absolute() else ROOT / p
        p.parent.mkdir(parents=True, exist_ok=True)
        return p


@lru_cache
def get_settings() -> Settings:
    return Settings()
