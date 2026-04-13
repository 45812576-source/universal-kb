import pymysql
pymysql.install_as_MySQLdb()

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase
from app.config import settings

engine = create_engine(
    settings.DATABASE_URL,
    pool_pre_ping=True,       # 每次取连接前 ping 一下，剔除已断开的连接
    pool_size=10,             # 常驻连接数（默认 5 太小，定时任务多容易耗尽）
    max_overflow=20,          # 突发可额外创建的连接数
    pool_recycle=1800,        # 30 分钟回收连接，避免被 MySQL wait_timeout 断开
    pool_timeout=10,          # 等待连接的超时秒数（默认 30 太长，快速失败更好）
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
