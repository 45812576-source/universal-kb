# Universal-KB 后端本地环境初始化指南

## 环境要求

- Python 3.13（conda base 环境）
- MySQL 8.x（Homebrew 安装）
- Node.js 22+（前端依赖）

---

## 一、安装 Python 依赖

`requirements.txt` 中存在两个有问题的包，需要跳过单独处理：

```bash
# 跳过 lark-oapi（版本 1.3.8 不存在）、pymilvus、grpcio，先装其余依赖
grep -v -E "lark-oapi|pymilvus|grpcio" requirements.txt | pip install --prefer-binary -r /dev/stdin

# 单独安装 pymilvus（使用新版，旧版与 Python 3.13 不兼容）
pip install "pymilvus>=2.5.0"
```

> **注意事项：**
> - `lark-oapi==1.3.8` 版本不存在，直接跳过，飞书集成功能暂不可用
> - `pymilvus 2.4.4` 依赖的 `grpcio<=1.63.0` 在 macOS 26 + Python 3.13 上编译失败，改用 `pymilvus>=2.5.0`
> - `grpcio 1.78.0` 已通过 conda 预装，无需额外安装

---

## 二、启动 MySQL 并创建数据库

本地 MySQL 通过 Homebrew 安装，默认 root 无密码。

```bash
# 确认 MySQL 已启动
brew services list | grep mysql

# 如未启动，手动启动
brew services start mysql

# 创建数据库
mysql -u root -e "CREATE DATABASE IF NOT EXISTS universal_kb CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
```

> **注意：** `docker-compose.yml` 中配置的 MySQL root 密码为 `kb_root_pass`，但本地 Homebrew MySQL 无密码，`app/config.py` 默认连接串 `mysql+pymysql://root@localhost:3306/universal_kb` 正好匹配，无需修改。

---

## 三、修复 models/__init__.py（补全缺失导入）

原始 `__init__.py` 遗漏了多个 model 导入，导致数据库表创建失败。需要在文件末尾追加：

```python
from app.models.draft import Draft  # noqa: F401
from app.models.workspace import Workspace, WorkspaceSkill, WorkspaceTool, WorkspaceDataTable, WorkspaceStatus  # noqa: F401
from app.models.feedback_item import FeedbackItem  # noqa: F401
from app.models.opportunity import Opportunity  # noqa: F401
from app.models.raw_input import RawInput  # noqa: F401
```

---

## 四、初始化数据库表

```bash
cd /Users/xia/project/universal-kb/backend

python -c "
from app.database import engine, Base
from app.models import *
Base.metadata.create_all(bind=engine)
print('表创建完成')
"
```

---

## 五、修复 passlib 兼容性问题

`passlib` 与新版 `bcrypt`（4.x+）不兼容，会导致登录接口 500 报错。

**修改 `app/services/auth_service.py`**，将 passlib 替换为直接使用 bcrypt：

```python
# 原代码（有问题）
from passlib.context import CryptContext
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)

def hash_password(plain: str) -> str:
    return pwd_context.hash(plain)

# 修改后
import bcrypt

def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode(), hashed.encode())

def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()
```

---

## 六、创建初始管理员账号

`scripts/seed.py` 因同样的 passlib 问题无法直接运行，手动创建：

```bash
python -c "
import bcrypt
pwd = bcrypt.hashpw(b'admin123', bcrypt.gensalt()).decode()

from app.database import SessionLocal
from app.models.user import User, Role, Department
db = SessionLocal()

dept = Department(name='管理部')
db.add(dept)
db.flush()

admin = User(
    username='admin',
    display_name='管理员',
    password_hash=pwd,
    role=Role.SUPER_ADMIN,
    department_id=dept.id,
    is_active=True
)
db.add(admin)
db.commit()
db.close()
print('管理员创建完成：admin / admin123')
"
```

> **User 模型字段注意：**
> - 密码字段为 `password_hash`（不是 `hashed_password`）
> - 姓名字段为 `display_name`（不是 `full_name`）
> - 没有 `email` 字段
> - 角色枚举值为 `Role.SUPER_ADMIN`（不是 `Role.admin`）

---

## 七、启动后端服务

```bash
cd /Users/xia/project/universal-kb/backend
uvicorn app.main:app --reload --port 8000
```

启动成功后访问：
- API 文档：http://localhost:8000/docs
- 登录接口：`POST http://localhost:8000/api/auth/login`

---

## 默认账号

| 用户名 | 密码 | 角色 |
|--------|------|------|
| admin | admin123 | SUPER_ADMIN |

---

## 前端配置

前端（le-desk）通过 `/api/proxy/[...path]` 代理所有后端请求，后端地址在 `BACKEND_URL` 环境变量中配置，默认 `http://localhost:8000`。

```bash
cd /Users/xia/project/le-desk
npm run dev   # 启动在 http://localhost:5023
```
