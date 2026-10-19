"""Copy the three SQLite files and verify the ledgers replay to the snapshot."""

from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path

from app.db.repository import load
from app.db.session import make_engine, make_session_factory


def sqlite_paths(database_url: str) -> list[Path]:
    if not database_url.startswith("sqlite:///"):
        raise ValueError("backup is implemented for sqlite:/// URLs")
    shared = Path(database_url[len("sqlite:///") :]).expanduser().resolve()
    stem = shared.with_suffix("")
    return [shared, Path(f"{stem}.baseline.db"), Path(f"{stem}.ai.db")]


def backup_sqlite(database_url: str, dest_dir: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    folder = dest_dir / stamp
    folder.mkdir(parents=True, exist_ok=True)
    copied = 0
    for path in sqlite_paths(database_url):
        if not path.is_file():
            continue
        shutil.copy2(path, folder / path.name)
        copied += 1
    if copied == 0:
        raise FileNotFoundError("no sqlite files to back up")
    (folder / "OK").write_text("copied\n", encoding="utf-8")
    return folder


def verify_backup(folder: Path) -> dict[str, str]:
    """Replay each arm ledger from the copies. Raises if the snapshot does not match."""

    from app.db.session import init_db

    report: dict[str, str] = {}
    for path in sorted(folder.glob("*.db")):
        name = path.name
        if name.endswith(".baseline.db"):
            key = "baseline"
        elif name.endswith(".ai.db"):
            key = "ai"
        else:
            key = "shared"
        engine = make_engine(f"sqlite:///{path}")
        init_db(engine)
        with make_session_factory(engine)() as session:
            ledger = load(session)
        if ledger.trading_blocked and ledger.block_reason == "balance_snapshot_mismatch":
            raise ValueError(f"{path.name}: snapshot does not match replay")
        report[key] = (
            f"opened={ledger.opened} free={ledger.free} reserved={ledger.reserved} "
            f"reserve={ledger.profit_reserve} pnl={ledger.realized_pnl}"
        )
    if not report:
        raise FileNotFoundError("backup folder has no sqlite files")
    return report
