---
description: 根据 OpenAPI/Swagger JSON 文件自动生成 Python 自动化测试脚本，并输出专业的测试报告。
---

# API 测试脚本生成器

根据 OpenAPI/Swagger JSON 文件自动生成 Python 自动化测试脚本，并输出专业的测试报告。

## 触发条件

用户要求根据 API 文档生成自动化测试脚本时使用此 Skill。

## 输入要求

用户提供以下信息：

### 必要信息
1. **API 文档文件** - OpenAPI/Swagger JSON 文件路径
2. **API 基础地址** - 如 http://127.0.0.1:7060/eye/backend

### 可选信息
- **测试账号** - 登录所需的账号密码
- **公司ID** - 如果API需要company header
- **认证接口** - 登录接口路径和参数格式
- **输出目录** - 报告保存路径

## 执行流程

### 第一步：解析 API 文档

读取 OpenAPI/Swagger JSON 文件，提取：
- 所有接口路径 (paths)
- 请求方法 (get/post/put/delete)
- 请求参数 (parameters)
- 请求体 (requestBody)
- 响应结构 (responses)
- 认证方式 (security schemes)

### 第二步：生成测试脚本

生成 Python + pytest 测试脚本，包含：

1. **配置部分**：
   - API 基础地址
   - 认证信息
   - 请求头

2. **API 客户端类**：
   - 封装登录/登出
   - 封装各类请求方法
   - 自动处理 Token

3. **测试用例**：
   - 每个接口对应一个测试方法
   - 包含请求参数和预期响应
   - 自动记录请求响应日志

4. **报告生成**：
   - 将每个请求转换为 Curl 格式
   - 记录完整请求和响应
   - 生成 Markdown 格式测试报告

### 第三步：运行测试

执行生成的测试脚本，收集结果。

### 第四步：生成报告

生成专业的 Markdown 测试报告，包含：

1. **测试概览**
   - 测试时间、API地址、账号信息
   - 接口总数、通过数、失败数、通过率

2. **测试结果汇总表**
   - 序号、接口路径、请求方法、HTTP状态码、业务响应码、测试结果

3. **详细请求响应日志**
   - 每个接口的 Curl 命令（可直接复制使用）
   - 完整请求参数
   - 响应内容

## 输出格式

### 测试脚本模板

```python
"""
{API名称} API 自动化测试脚本
生成时间: {生成时间}
"""

import requests
import json
import datetime
import os
from typing import Dict, Any, Optional

# ==================== 配置 ====================
BASE_URL = "{api_base_url}"
COMPANY_ID = "{company_id}"

TEST_USER = {{
    "phone": "{phone}",
    "password": "{password}"
}}

REPORT_DIR = r"{report_dir}"
os.makedirs(REPORT_DIR, exist_ok=True)

# ==================== 测试日志 ====================
test_logs = []

def log_test(method, path, params, json_data, response, status):
    """记录测试日志"""
    try:
        resp_json = response.json()
    except:
        resp_json = {{"error": "无法解析响应"}}
    
    test_logs.append({{
        "method": method,
        "path": path,
        "params": params,
        "json": json_data,
        "status_code": response.status_code,
        "response": resp_json,
        "test_status": status
    }})

# ==================== API 客户端 ====================

class APIClient:
    """API 客户端"""
    
    def __init__(self, base_url: str = BASE_URL):
        self.base_url = base_url
        self.session = requests.Session()
        self.session.headers.update({{
            "Content-Type": "application/json",
            "Accept": "application/json"
        }})
        self.token = None
    
    def set_token(self, token: str):
        self.token = token
        self.session.headers["Authorization"] = f"Bearer {{token}}"
    
    def login(self, phone: str, password: str) -> Dict[str, Any]:
        """登录获取 Token"""
        response = self.post("{login_path}", json={{
            "phone": phone,
            "password": password
        }})
        
        data = response.json()
        if data.get("code") == 0:
            token = data.get("data", {{}}).get("token")
            self.set_token(token)
            
            # 获取公司ID
            companies = data.get("data", {{}}).get("companies", [])
            if companies:
                company_id = companies[0].get("companyId")
                if company_id:
                    self.session.headers["company"] = company_id
        
        return data

# ==================== 测试用例 ====================
# 自动生成的测试用例...

# ==================== 主函数 ====================

def generate_report():
    """生成测试报告"""
    # 生成 Markdown 报告，包含 curl 格式
    ...
```

### 测试报告模板

```markdown
# API 自动化测试报告

## 测试概览

| 项目 | 内容 |
|------|------|
| **测试时间** | 2026-01-01 10:00:00 |
| **API地址** | http://api.example.com |
| **接口总数** | 50 |

---

## 测试结果汇总

| 序号 | 接口路径 | 请求方法 | HTTP状态码 | 业务响应码 | 测试结果 |
|------|----------|----------|------------|------------|----------|
| 1 | /api/login | POST | 200 | 0 | ✅ PASS |
| 2 | /api/user/info | GET | 200 | 0 | ✅ PASS |
... |

---

## 详细请求响应日志

### 1. POST /api/login

**Curl请求**:
```bash
curl \
  'http://api.example.com/api/login' \
  -H 'Authorization: Bearer xxx' \
  -H 'Content-Type: application/json' \
  --data-raw '{"phone":"13800138000","password":"123456"}' \
  --insecure
```

**响应状态码**: `200`

**响应内容**:
```json
{{
  "code": 0,
  "message": "成功",
  "data": {{...}}
}}
```

---
```

## 使用示例

### 用户输入

```
帮我生成测试脚本：
- API文档: ./api/openapi.json
- API地址: http://127.0.0.1:7060/eye/backend
- 账号: 17700100001 / LM123456
- 公司ID: 248
```

### AI 执行

1. 读取 `./api/openapi.json` 文件
2. 解析所有接口定义
3. 生成测试脚本 `test_api.py`
4. 运行测试
5. 生成报告 `test-report/api-test-report.md`

### 输出文件

- **测试脚本**: `{output_dir}/test_api.py`
- **测试报告**: `{output_dir}/test-report/api-test-report.md`

## 注意事项

- 确保 API 文档为有效的 OpenAPI/Swagger JSON 格式
- 测试脚本需要安装依赖: `pip install requests pytest`
- 报告中包含的真实 Token 请勿泄露
- 部分接口可能需要真实的业务数据支持