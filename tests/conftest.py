"""Each test gets an isolated workspace: its own config copy, metadata DB and fake estates."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _clear_caches() -> None:
    from alertbot import config, secrets, settings
    from alertbot.connectors import feeds, sql_source
    from alertbot.db import session
    from alertbot.engine import cycle

    for fn in (settings.get_settings, config.load_projects, config.load_ai_config, session.get_engine,
               session._factory, sql_source.engine_for, secrets._from_key_vault, feeds.feed_engine):
        fn.cache_clear()
    cycle._CONNECTORS.clear()


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """Slipstream only (fast). Returns the tmp dir; the cwd is set to it."""
    cfg = tmp_path / "config"
    (cfg / "projects").mkdir(parents=True)
    shutil.copy(ROOT / "config" / "projects" / "slipstream.yaml", cfg / "projects")
    shutil.copy(ROOT / "config" / "ai.yaml", cfg)
    shutil.copy(ROOT / "config" / "members.yaml", cfg)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ALERTBOT_DB_URL", f"sqlite:///{(tmp_path / '.data' / 'alertbot.db').as_posix()}")
    monkeypatch.setenv("ALERTBOT_CONFIG_DIR", str(cfg))
    monkeypatch.setenv("ALERTBOT_AUTH_MODE", "dev")
    monkeypatch.setenv("PA_SLIPSTREAM_URL", "")
    _clear_caches()
    yield tmp_path
    from alertbot.db.session import get_engine

    get_engine().dispose()
    _clear_caches()


@pytest.fixture
def estate(workspace):
    """Fake Slipstream estate with 5 weeks of history, baselines learned, first cycle run."""
    from alertbot.config import load_projects
    from alertbot.db import create_all, session_scope
    from alertbot.engine.cycle import backfill, run_cycle
    from alertbot.seed import seed
    from alertbot.sim.estate import Estate

    create_all()
    cfg = load_projects()["slipstream"]
    est = Estate(cfg)
    est.reset()
    est.generate_history(days=35)
    with session_scope() as s:
        seed(s)
    backfill(cfg, days=37)
    report = run_cycle(cfg)
    assert report.status == "ok", report.error
    return cfg


def freeze_now(monkeypatch, at) -> None:  # noqa: ANN001
    """Pin the app's clock: every alertbot module's `utcnow` returns `at` (tests whose history is built at a fixed date)."""
    import sys

    from alertbot import timeutil

    real = timeutil.utcnow
    for name, mod in list(sys.modules.items()):
        if name.startswith("alertbot") and getattr(mod, "utcnow", None) is real:
            monkeypatch.setattr(mod, "utcnow", lambda: at)
