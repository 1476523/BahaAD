"""唯一的 SQLite 連線工廠。

設計理由（見 docs/decisions/0000-architecture-overview.md）：舊專案裡不同模組各自用不同的
timeout 設定直連同一個資料庫檔案，高併發下容易搶鎖失敗。這裡收斂成一個地方：固定 timeout、
開啟 WAL 模式（多個讀取者不會互相卡住，寫入也不會卡住讀取），全專案只有這裡呼叫
sqlite3.connect()。
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

_DEFAULT_TIMEOUT_SECONDS = 10.0


class Database:
    """包一層 WAL 模式的 SQLite 連線工廠，供 store/ 底下其他模組共用。"""

    def __init__(self, db_path: Path, timeout: float = _DEFAULT_TIMEOUT_SECONDS) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._timeout = timeout
        # 每個 Database 實例對應同一支資料庫檔案的所有寫入操作序列化在這把鎖底下，
        # 避免多執行緒同時進行「讀取-修改-寫回」時互相插隊(WAL 模式本身允許併發讀取，
        # 但複合操作的原子性還是要靠應用層的鎖，SQLite 的交易只保證單一陳述式層級)
        self.write_lock = threading.Lock()
        self._init_pragmas()

    def _init_pragmas(self) -> None:
        # 刻意不用 `with self.connect() as conn:`——sqlite3.Connection 當 context manager
        # 只會在離開時 commit/rollback 目前的交易，不會關閉連線，這支連線就會一直開著等
        # GC 回收，Windows 上這代表資料庫檔案在那之前一直被鎖住（實測發現：Phase 3
        # 「重置 BahaAD」要刪除資料庫檔案時因為這個殘留連線被 PermissionError 擋下）。
        conn = self.connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.commit()
        finally:
            conn.close()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path), timeout=self._timeout)
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """開一個連線、成功時 commit、例外時 rollback、離開時關閉連線。"""
        conn = self.connect()
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    @property
    def path(self) -> Path:
        return self._db_path
