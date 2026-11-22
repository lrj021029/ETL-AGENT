"""Backend 单元测试共享 Pytest Fixtures。

配置 `backend/` 为 Python 导入根目录，构建内存级 SQLAlchemy 2.0 完整 71 表数据库环境，
预置 6 类标准演示账号与多组织/多项目拓扑，并提供 FastAPI TestClient。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.auth.service import seed_demo_iam_data  # noqa: E402
from app.main import create_app  # noqa: E402
from app.shared.models import Base  # noqa: E402


@pytest.fixture()
def db_session_factory() -> Generator[sessionmaker[Session], None, None]:
    """创建包含全量 6 大领域 71 张表的独立内存测试数据库会话工厂。"""
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    try:
        yield factory
    finally:
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


@pytest.fixture()
def db_session(db_session_factory: sessionmaker[Session]) -> Generator[Session, None, None]:
    """提供单个测试函数的数据库会话。"""
    session = db_session_factory()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def seeded_env(db_session: Session) -> dict[str, Any]:
    """在测试数据库中初始化固定系统角色、权限、两个组织、三个项目与 6 类演示用户。"""
    seeded = seed_demo_iam_data(db_session)
    db_session.commit()
    return seeded


@pytest.fixture()
def api_client(
    db_session_factory: sessionmaker[Session],
    seeded_env: dict[str, Any],
    tmp_path: Path,
) -> Generator[TestClient, None, None]:
    """提供绑定测试数据库与受控临时上传目录的 FastAPI TestClient。"""
    app = create_app(session_factory=db_session_factory)
    from app.connections.service import ConnectionService

    conn_svc = ConnectionService()
    conn_svc.settings.allowed_upload_root = tmp_path / "uploads"
    app.state.connection_service = conn_svc

    with TestClient(app) as client:
        yield client
