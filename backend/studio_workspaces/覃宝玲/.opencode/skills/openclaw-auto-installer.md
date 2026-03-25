---
description: 自动安装 OpenClaw 及其依赖。适用于首次安装 OpenClaw 或修复损坏的安装环境。触发条件：(1) 用户要求安装 OpenClaw，(2) 检测到 OpenClaw 缺失或损坏，(3) 需要在新机器上部署 OpenClaw。会自动检查并安装 Node.js 22+、pnpm，然后安装 OpenClaw，并验证安装成功。
---

# OpenClaw 自动安装器

此 skill 自动完成 OpenClaw 的完整安装流程，包括依赖检查、环境配置和安装验证。

## 核心流程

```
1. 检查前置依赖 (Node.js 22+, pnpm)
   ↓
2. 如缺失，自动安装缺失依赖并配置环境变量
   ↓
3. 使用 pnpm 安装 OpenClaw
   ↓
4. 验证安装成功
   ↓
5. 成功 → 提示用户 | 失败 → 返回步骤1重试
```

## 步骤 1: 检查前置依赖

执行以下命令检查系统环境：

```powershell
# 检查 Node.js 版本
node --version

# 检查 pnpm 版本
pnpm --version

# 检查 OpenClaw 是否已安装
openclaw --version
```

### Windows 依赖清单

| 依赖 | 最低版本 | 检查命令 | 缺失时的处理 |
|------|----------|----------|--------------|
| Node.js | 22.16.0 | `node --version` | 自动下载安装 |
| pnpm | 10.23.0 | `pnpm --version` | 通过 npm 安装 |
| Git | 任意 | `git --version` | 可选，建议安装 |

## 步骤 2: 安装缺失依赖

### 自动安装 Node.js (Windows)

使用 [nvm-windows](https://github.com/coreybutler/nvm-windows) 或直接下载安装包：

```powershell
# 使用 winget 安装
winget install OpenJS.NodeJS

# 或手动下载: https://nodejs.org/dist/v22.20.0/node-v22.20.0-x64.msi
```

### 自动安装 pnpm

```powershell
# 通过 npm 安装
npm install -g pnpm

# 验证安装
pnpm --version
```

### 配置环境变量

如果手动安装 Node.js，需要确保：
- `PATH` 包含 `C:\Program Files\nodejs\`
- 可执行 `node --version` 和 `npm --version`

## 步骤 3: 安装 OpenClaw

```powershell
# 全局安装
pnpm add -g openclaw

# 或使用 npx 直接运行
npx openclaw --version
```

## 步骤 4: 验证安装

```powershell
# 检查 OpenClaw 版本
openclaw --version

# 检查网关状态
openclaw gateway status
```

验证成功标准：
- `openclaw --version` 返回版本号
- `openclaw gateway status` 显示网关运行状态

## 步骤 5: 失败重试机制

如果安装失败，执行以下诊断：

```powershell
# 诊断 1: 检查 Node.js 路径
where node

# 诊断 2: 检查 pnpm 配置
pnpm config list

# 诊断 3: 清除缓存重试
pnpm store clean
pnpm add -g openclaw --force
```

重新执行步骤 1-4，直到安装成功。

## 使用此 skill

当用户要求安装 OpenClaw 或安装失败时，使用此 skill 自动完成整个安装流程。

### 示例对话

- "帮我安装 OpenClaw"
- "OpenClaw 安装不上，帮我看看"
- "在新电脑上部署 OpenClaw"
- "安装失败，帮我修复"

### 执行命令

直接运行安装脚本：

```powershell
# 完整安装流程（包含所有检查和重试）
powershell -ExecutionPolicy Bypass -File D:\openclaw\skills\openclaw-auto-installer\scripts\install.ps1
```

## 故障排除

### 常见问题

1. **node-pty 安装失败**
   - 需要 C++ 编译工具
   - Windows: 安装 [Build Tools for Visual Studio](https://visualstudio.microsoft.com/visual-cpp-build-tools/)

2. **sharp 安装失败**
   - 可能需要管理员权限
   - 尝试: `pnpm add -g openclaw --ignore-scripts` 后手动编译

3. **权限错误**
   - 使用管理员权限运行 PowerShell
   - 或配置 pnpm 的 `prefix` 到用户目录