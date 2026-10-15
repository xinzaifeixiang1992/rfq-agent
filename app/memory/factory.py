from app.config import Settings
from app.db.conn import Database
from app.memory.base import MemoryStore


def create_memory(s: Settings, db: Database) -> MemoryStore:
    """local：带 branch 列的单表实现；git4data：记忆表交给 Git4Data（MatrixOne 原生 / SQLite 仿真）；
    memoria：MatrixOrigin Memoria 云服务。"""
    if s.memory_backend == "memoria":
        from app.memory.memoria import MemoriaStore
        return MemoriaStore(s.memoria_url, s.memoria_api_key)
    if s.memory_backend == "git4data":
        from app.memory.git4data import Git4DataMemoryStore
        from app.vcs import create_vcs
        return Git4DataMemoryStore(db, create_vcs(db))
    from app.memory.local import LocalMemoryStore
    return LocalMemoryStore(db)
