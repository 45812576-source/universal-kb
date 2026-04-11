from pydantic import ConfigDict
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = ConfigDict(env_file=".env", extra="ignore")

    DATABASE_URL: str = "mysql+pymysql://root@localhost:3306/universal_kb"
    JWT_SECRET: str = "change-me-in-production"
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_MINUTES: int = 480
    MILVUS_HOST: str = "localhost"
    MILVUS_PORT: int = 19530
    REDIS_URL: str = "redis://localhost:6379"
    UPLOAD_DIR: str = "./uploads"
    # 每用户 opencode 工作目录根路径（持久化，重启不丢失）
    STUDIO_WORKSPACE_ROOT: str = "~/studio_workspaces"

    # ── 阿里云 OSS 存储 ──────────────────────────────────────────────────────
    OSS_ENDPOINT: str = "oss-cn-guangzhou.aliyuncs.com"
    OSS_BUCKET: str = "le-desk"
    OSS_ACCESS_KEY_ID: str = ""
    OSS_ACCESS_KEY_SECRET: str = ""

    # OnlyOffice Document Server
    ONLYOFFICE_URL: str = "http://localhost:8443"
    ONLYOFFICE_JWT_SECRET: str = "change-me-onlyoffice"

    # Feishu (Lark) integration
    LARK_APP_ID: str = ""
    LARK_APP_SECRET: str = ""
    LARK_VERIFICATION_TOKEN: str = ""
    LARK_ENCRYPT_KEY: str = ""
    LARK_OAUTH_REDIRECT_URI: str = ""  # 飞书 OAuth 回调地址，如 https://xxx/api/lark/oauth/callback

    # LLM API Keys (forwarded to opencode web process)
    KIMI_API_KEY: str = ""
    DEEPSEEK_API_KEY: str = ""
    BAILIAN_API_KEY: str = ""
    ARK_API_KEY: str = ""  # 火山引擎 ARK，备用 provider
    LEMONDATA_API_KEY: str = ""  # LemonData，受限模型（需管理员授权）

    # ── 治理自动化 ───────────────────────────────────────────────────────
    GOVERNANCE_AUTO_APPLY_THRESHOLD: int = 85  # 置信度 >= 此值自动生效
    GOVERNANCE_LLM_ENABLED: bool = True  # 关键词规则不足时是否走 LLM fallback

    # 百炼用量超限后自动切换到 ARK（由外部监控脚本或管理接口写入）
    BAILIAN_FALLBACK_TO_ARK: bool = False

    # ── Skill Studio 结构化模式 ──────────────────────────────────────────────
    STUDIO_STRUCTURED_MODE: str = "on"  # "off" | "on"

    # 百炼 Coding Plan 三窗口额度，达到任一窗口 90% 自动切换 ARK
    BAILIAN_QUOTA_5H: int = 6000    # 每 5 小时最多请求数
    BAILIAN_QUOTA_7D: int = 45000   # 每 7 天最多请求数
    BAILIAN_QUOTA_30D: int = 90000  # 每订阅月最多请求数

settings = Settings()
