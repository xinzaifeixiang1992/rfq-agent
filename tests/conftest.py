import sys
from functools import partial
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings  # noqa: E402
from app.memory import memoria  # noqa: E402
from app.seed import seed, seed_memories  # noqa: E402
from app.services import build_services  # noqa: E402
from tests.fake_memoria import FakeMemoria  # noqa: E402


class _FakeHttpx:  # MemoriaStore 内部的 httpx.Client 指向按官方 api-reference 实现的假服务
    def __init__(self) -> None:
        self.Client = partial(httpx.Client, transport=FakeMemoria())


@pytest.fixture(params=["local", "git4data", "memoria"])
def settings(request, tmp_path, monkeypatch) -> Settings:
    if request.param == "memoria":
        monkeypatch.setattr(memoria, "httpx", _FakeHttpx())
    return Settings(db_backend="sqlite", sqlite_path=str(tmp_path / "t.db"), memory_backend=request.param,
                    memoria_url="http://memoria.test", memoria_api_key="test-key",
                    llm_base_url="", llm_api_key="", upload_dir=str(tmp_path / "up"), output_dir=str(tmp_path / "out"))


@pytest.fixture()
def svc(settings):
    s = build_services(settings)
    seed(s.db, history=600)
    seed_memories(s.memory)
    return s
