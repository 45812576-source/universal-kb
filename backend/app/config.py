from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    DATABASE_URL: str = "mysql+pymysql://root:kb_root_pass@localhost:3306/universal_kb"
    JWT_SECRET: str = "change-me-in-production"
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_MINUTES: int = 480
    MILVUS_HOST: str = "localhost"
    MILVUS_PORT: int = 19530
    REDIS_URL: str = "redis://localhost:6379"
    UPLOAD_DIR: str = "./uploads"

    class Config:
        env_file = ".env"


settings = Settings()
