"""Git for Data 抽象：快照、分支、差异、合并、挑选、回滚。"""
from app.vcs.base import DataVCS, DiffResult, VCSConflict
from app.vcs.factory import create_vcs

__all__ = ["DataVCS", "DiffResult", "VCSConflict", "create_vcs"]
