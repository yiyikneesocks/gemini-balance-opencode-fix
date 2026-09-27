"""
数据库初始化模块
"""
from dotenv import dotenv_values

from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from app.database.connection import engine, Base
from app.database.models import Settings
from app.log.logger import get_database_logger

logger = get_database_logger()


# 轻量自动迁移：给已存在的表补充后续版本新增的列。
# 说明：Base.metadata.create_all 不会为已存在的表新增列，因此这里显式补齐。
_AUTO_MIGRATIONS = {
    "t_key_model_state": {
        "last_error_log": "TEXT NULL COMMENT '最近一次错误详情（用于前端弹窗）'",
        "error_count": "INT NULL DEFAULT 0 COMMENT '今日出错次数'",
        "success_count": "INT NULL DEFAULT 0 COMMENT '今日成功调用次数（太平洋日）'",
        "stat_day": "VARCHAR(10) NULL COMMENT '统计所属太平洋日 YYYY-MM-DD'",
    },
}


def _apply_auto_migrations():
    """为已存在的表补充缺失的列（幂等）。"""
    try:
        inspector = inspect(engine)
        existing_tables = set(inspector.get_table_names())
        with engine.begin() as conn:
            for table, columns in _AUTO_MIGRATIONS.items():
                if table not in existing_tables:
                    continue
                existing_cols = {c["name"] for c in inspector.get_columns(table)}
                for col, ddl in columns.items():
                    if col in existing_cols:
                        continue
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}"))
                    logger.info(f"Auto-migration: added column {table}.{col}")
    except Exception as e:
        logger.error(f"Failed auto-migration: {str(e)}")
        raise


def create_tables():
    """
    创建数据库表
    """
    try:
        # 创建所有表
        Base.metadata.create_all(engine)
        # 为已有表补齐新增列
        _apply_auto_migrations()
        logger.info("Database tables created successfully")
    except Exception as e:
        logger.error(f"Failed to create database tables: {str(e)}")
        raise


def import_env_to_settings():
    """
    将.env文件中的配置项导入到t_settings表中
    """
    try:
        # 获取.env文件中的所有配置项
        env_values = dotenv_values(".env")
        
        # 获取检查器
        inspector = inspect(engine)
        
         # 检查t_settings表是否存在
        if "t_settings" in inspector.get_table_names():
            # 使用Session进行数据库操作
            with Session(engine) as session:
                # 获取所有现有的配置项
                current_settings = {setting.key: setting for setting in session.query(Settings).all()}
                
                # 遍历所有配置项
                for key, value in env_values.items():
                    # 检查配置项是否已存在
                    if key not in current_settings:
                        # 插入配置项
                        new_setting = Settings(key=key, value=value)
                        session.add(new_setting)
                        logger.info(f"Inserted setting: {key}")
                
                # 提交事务
                session.commit()
                
        logger.info("Environment variables imported to settings table successfully")
    except Exception as e:
        logger.error(f"Failed to import environment variables to settings table: {str(e)}")
        raise


def initialize_database():
    """
    初始化数据库
    """
    try:
        # 创建表
        create_tables()
        
        # 导入环境变量
        import_env_to_settings()
    except Exception as e:
        logger.error(f"Failed to initialize database: {str(e)}")
        raise
