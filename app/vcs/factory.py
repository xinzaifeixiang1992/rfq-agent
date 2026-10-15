from app.db.conn import Database
from app.vcs.base import DataVCS


def create_vcs(db: Database) -> DataVCS:
    if db.dialect == "matrixone":
        from app.vcs.matrixone import MatrixOneVCS
        return MatrixOneVCS(db)
    from app.vcs.sqlite_sim import SQLiteSimVCS
    return SQLiteSimVCS(db)
