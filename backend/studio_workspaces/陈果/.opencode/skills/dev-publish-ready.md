---
description: 开发完成后调用，自动识别项目类型（纯 HTML / Next.js / Node 后端 / Python 后端 / 小程序），按类型准备发布、打包或部署
---

# 完成开发，准备发布

## Overview

开发者在 OpenCode 完成开发后调用本 Skill。自动完成：
1. 检测项目类型
2. 自动修复发布所需配置（无需开发者手动操作）
3. 告知下一步

---

## Step 1：确认项目目录

检查工作区有哪些项目文件夹。只有一个则直接用；有多个则问开发者选哪个。

---

## Step 2：检测项目类型

读取项目根目录文件列表，按以下规则判断：

| 特征 | 类型 |
|------|------|
| `package.json` 依赖含 `"next"` | Next.js 全栈 |
| `package.json` + `server.js/app.js/index.js` | Node.js 后端 |
| `requirements.txt` + `app.py/main.py/server.py` | Python 后端 |
| 只有 `.html` 文件 | 纯静态 HTML |

只读 `package.json` 的 `dependencies`/`devDependencies`，不读其他无关文件。

---

## Step 3：自动修复配置

**Node.js 项目**，读取启动文件（`server.js` 等）前 50 行：
- 如果端口写死（如 `const PORT = 3000`），改成 `const PORT = process.env.PORT || 3000`
- 写回文件

**Python 项目**，读取启动文件前 50 行：
- 如果端口写死（如 `port = 5000`），改成 `port = int(os.environ.get('PORT', 5000))`
- 如果文件头没有 `import os`，加上
- 写回文件

**Next.js 项目**：无需修改任何文件。

**纯静态 HTML**：无需修改任何文件。

---

## Step 4：输出结果

告知开发者：

```
✅ 项目已就绪（[项目类型]）
[如果修改了文件，列出修改内容]

现在可以点底部「发布 Web App」→ 选择 [文件夹名] → 点「检查并发布」。
```

---

## 注意事项

- 不要读 `node_modules/`、`.git/`、`__pycache__/`、`.venv/`、`dist/`、`build/` 目录
- 只读启动文件前 50 行，不要读数据文件、日志、lock 文件
- 修改文件后必须写回（用 file write 工具覆盖原文件）