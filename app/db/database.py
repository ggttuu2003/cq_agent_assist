"""创建 MySQL 连接池，并为每个请求提供独立数据库会话。"""

from fastapi import Request
from sqlalchemy import URL, inspect, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.settings import Settings
from app.db.models import Base
from app.db import knowledge, traces  # 注册知识资料和调用记录表，交给同一份 metadata 创建。


def create_database(settings: Settings):
    """使用 URL 对象处理密码中的特殊字符，不拼接含密码的连接字符串。"""
    url = URL.create(
        "mysql+aiomysql", username=settings.mysql_user, password=settings.mysql_password,
        host=settings.mysql_host, port=settings.mysql_port, database=settings.mysql_db,
        query={"charset": "utf8mb4"},
    )
    engine = create_async_engine(url, pool_pre_ping=True, pool_recycle=1800)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def initialize_database(engine, default_model: str = "") -> None:
    """创建缺失的表，并为旧会话补上模型字段。"""
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        # 调用记录保留微秒，避免同一秒内失败重试的执行顺序不确定。
        for table_name in ("cq_ai_trace", "cq_ai_call"):
            trace_columns = await connection.run_sync(
                lambda sync_connection, name=table_name: inspect(sync_connection).get_columns(name)
            )
            created = next(column for column in trace_columns if column["name"] == "created_at")
            if getattr(created["type"], "fsp", 0) != 6:
                changes = ["MODIFY created_at DATETIME(6) NOT NULL COMMENT '开始时间UTC'",
                           "MODIFY finished_at DATETIME(6) NULL COMMENT '结束时间UTC'"]
                if table_name == "cq_ai_trace":
                    changes.append("MODIFY expires_at DATETIME(6) NOT NULL COMMENT '预计最晚结束时间UTC'")
                await connection.execute(text(f"ALTER TABLE {table_name} " + ", ".join(changes)))
        message_columns = await connection.run_sync(
            lambda sync_connection: [column["name"] for column in inspect(sync_connection).get_columns("cq_message")]
        )
        if "citations" not in message_columns:
            await connection.execute(text("ALTER TABLE cq_message ADD COLUMN citations JSON NULL COMMENT '知识资料引用快照'"))
        attachment_columns = await connection.run_sync(
            lambda sync_connection: [column["name"] for column in inspect(sync_connection)
                                     .get_columns("cq_message_attachment")]
        )
        if "position" not in attachment_columns:
            await connection.execute(text(
                "ALTER TABLE cq_message_attachment ADD COLUMN position INT NOT NULL DEFAULT 0 COMMENT '消息内的附件顺序'"
            ))
        columns = await connection.run_sync(
            lambda sync_connection: [column["name"] for column in inspect(
                sync_connection,
            ).get_columns("cq_conversation")]
        )
        if "model_name" not in columns:
            await connection.execute(text(
                "ALTER TABLE cq_conversation ADD COLUMN model_name VARCHAR(200) NOT NULL DEFAULT '' COMMENT '本会话使用的模型'"
            ))
        if "knowledge_enabled" not in columns:
            await connection.execute(text(
                "ALTER TABLE cq_conversation ADD COLUMN knowledge_enabled BOOLEAN NOT NULL DEFAULT 0 COMMENT '是否每轮先检索知识库'"
            ))
        # 旧会话优先沿用历史回答中的模型，不能全部覆盖成新的默认模型。
        await connection.execute(text("""
            UPDATE cq_conversation AS conversation
            JOIN (
                SELECT conversation_id, MAX(sequence) AS last_sequence
                FROM cq_message
                WHERE role = 'assistant' AND model IS NOT NULL AND TRIM(model) <> ''
                GROUP BY conversation_id
            ) AS latest ON latest.conversation_id = conversation.id
            JOIN cq_message AS message
                ON message.conversation_id = latest.conversation_id
                AND message.sequence = latest.last_sequence
            SET conversation.model_name = TRIM(message.model)
            WHERE conversation.model_name = ''
        """))
        if default_model.strip():
            await connection.execute(
                text("UPDATE cq_conversation SET model_name = :model WHERE model_name = ''"),
                {"model": default_model.strip()},
            )


async def get_db(request: Request):
    """请求结束时释放连接，异常导致未提交的事务自动回滚。"""
    async with request.app.state.db_sessions() as session:
        yield session
