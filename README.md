# AI 工作台（AI Workbench）

> 本地优先（local-first）的开源个人运营中枢：把**定时任务、记忆、Skill、每日亮点、对话记忆、本地 Git 仓库**六类个人资产，收敛为一个可检索、可编辑、可沉淀、可管理远程仓库的结构化工作台。

纯 Python 标准库实现，**零外部依赖、零账号、零云端**。数据全部留在你自己的机器上。

---

## ✨ 特性

- **六类资产一屏管理**：定时任务（自动化）、记忆（Markdown 笔记）、Skill（技能说明）、每日亮点、对话记忆、本地 Git 仓库。
- **内容分类体系**：六类资产共享统一分类标记（`[分类=xxx]` + `categories.json` 索引），可逐条打标签并在前端按分类筛选；旧数据无分类显示「未分类」仍可被检索。
- **Skill 详情查看**：直接在工作台内查看任意 Skill 的 `SKILL.md` 全文（带路径穿越防护）。
- **对话记忆**：第 5 类资产，沉淀跨会话的关键结论（格式复用亮点，以 `CM-` 前缀区分）。
- **本地 Git 仓库管理 + 平台令牌托管**：扫描本机 Git 仓库（分支 / 状态 / 远程），并对 GitHub、Gitee 做 `fetch` / `pull` / `checkout` / `clone` / **推送**；令牌由工作台脱敏托管，直接驱动远程操作。
- **真·本地编辑**：在网页里编辑 / 新增 / 删除，会**真实改写本地文件与本地 SQLite**，不是影子副本。
- **写前自动备份 + 误删恢复**：每次写操作前自动生成 `.bak`，写坏可在「备份管理」里一键还原。
- **数据导出 / 主题 / 跨平台**：一键导出全部数据为 JSON；明暗主题（记忆到 localStorage）；附 `start-workbench.sh` 跨平台启动脚本。
- **定时任务多字段编辑**：在界面内解析中文频率（如「每周五 10:30」）并改回自动化调度。
- **飞书表格风格 UI**：粘性表头、筛选、搜索、行内编辑、批量更新。
- **人人可装**：`git clone` + 一条命令即可在任意机器本地运行，无需安装数据库、无需联网。

---

## 🚀 快速开始

要求 **Python 3.8+**（仅用到标准库，无需 `pip install` 任何东西）。

```bash
# 1. 克隆（GitHub）
git clone https://github.com/hjunrun02-sys/ai-workbench.git
# 或国内镜像（Gitee）：
# git clone https://gitee.com/msjr123/ai-workbench.git
cd ai-workbench

# 2. 启动
#    macOS / Linux：
./start-workbench.sh
#    Windows（直接双击 start-workbench.bat，或命令行）：
python server.py
```

首次启动会自动在 `data/` 下建库并写入示例数据，界面直接有内容。

> 默认绑定 `127.0.0.1:8765`；端口被占用时 `server.py` 会自动顺延（8765~8814）。启动后浏览器自动打开，若没打开请手动访问终端打印的地址。用完点页面里的「关闭工作台」即可。

### 用浏览器访问
启动后浏览器会自动打开；若没打开，手动访问终端里打印的地址（如 `http://127.0.0.1:8765/`）。

---

## 📦 六类资产一览

| 分类 | 数据存储 | 说明 |
| --- | --- | --- |
| 定时任务 | `data/automations.db`（SQLite） | 调度自动化，界面内多字段编辑 |
| 记忆 | `data/memory/MEMORY.md` | 长期项目笔记 / 偏好 |
| Skill | `data/skills/<名称>/SKILL.md` | 技能说明，可查看全文 |
| 每日亮点 | `data/highlights.md` | 当日要点，支持分类 |
| 对话记忆 | `data/conversation_memory.md` | 跨会话关键结论（`CM-` 前缀），支持分类 |
| 本地 Git 仓库 | 扫描本机（深度 `GIT_DEPTH=4`）+ `data/git_roots.txt` | 分支 / 状态 / 远程；令牌驱动远程操作 |

---

## 🔑 平台令牌管理（GitHub / Gitee）

工作台可以托管你的 GitHub Personal Access Token 与 Gitee 令牌，并据此**直接对仓库做远程操作**（推送 / 拉取 / 克隆），无需每次手动输密码，也无需依赖系统凭据管理器的弹窗。

### 配置方式
1. 打开工作台，点工具栏 **「平台令牌」**。
2. 分别填写：
   - **GitHub**：PAT（形如 `ghp_xxx`）。
   - **Gitee**：登录用户名 + 私人令牌（Gitee 必须用 `用户名:令牌` 形式）。
3. 保存后，令牌写入本机 **`.git/config`** 的 `insteadOf` 规则（见下），界面只以 `****wVAy` 形式脱敏展示，编辑时才要求重填。

### 存储位置与安全性
- 令牌以 `insteadOf` 形式写入**本仓库的 `.git/config`**：
  - GitHub：`url.https://<PAT>@github.com/.insteadof https://github.com/`
  - Gitee：`url.https://<用户名>:<令牌>@gitee.com/.insteadof https://gitee.com/`
- `.git/config` 是 git 元数据，**不会被 `git push` 推到远程、不在公开仓库里**。
- 远程操作通过 `git -c url...insteadOf=...` **临时注入**令牌，对任意仓库生效，且不污染你的全局 git 配置。
- 在「Git 仓库」标签页点 **「推送」** 即可推送到该仓库的全部远程。

> ⚠️ 安全提示：令牌以明文存于本机 `.git/config`。若机器有被攻破风险，请去 GitHub / Gitee 撤销（revoke）该令牌；撤销后工作台远程操作会失败，届时重新填入新令牌即可。

---

## 📁 数据与目录

```
ai-workbench/
├── server.py              # 本地 HTTP 服务（仅绑定 127.0.0.1）
├── workbench_app.html     # 飞书表格风格前端（由 server.py 提供）
├── start-workbench.sh     # macOS / Linux 启动脚本
├── start-workbench.bat    # Windows 启动脚本（如需要）
├── docs/                  # 项目结构图与活动图（SVG）
│   ├── v1.5-structure.svg
│   └── v1.5-activity.svg
├── data/                  # ★ 你的全部个人数据都在这里（不上云）
│   ├── automations.db     # 定时任务（SQLite，首次启动自动建表 + 示例）
│   ├── highlights.md      # 每日亮点（Markdown，支持 [分类=xxx]）
│   ├── conversation_memory.md # 对话记忆（Markdown，支持 [分类=xxx]，首次写入时生成）
│   ├── categories.json    # 分类索引（首次打分类时生成）
│   ├── git_roots.txt      # 允许的 Git 仓库根目录（clone 目标限定在此内）
│   ├── memory/MEMORY.md   # 记忆（Markdown 小节）
│   ├── skills/<名称>/     # Skill 目录，每个含 SKILL.md
│   └── skill_override.json# Skill 功能说明的本地覆盖
├── AI工作台_PRD.md         # 完整产品定义（PRD）
├── README.md
├── LICENSE
├── requirements.txt        # 标准库实现，本文件仅作声明（无需 pip install）
└── .gitignore
```

### 自定义数据目录
默认数据放在程序同级的 `data/`。可通过环境变量覆盖：

```bash
export AI_WORKBENCH_DATA=/path/to/your/data
python server.py
```

---

## 🔒 隐私与安全

- 服务**仅绑定 `127.0.0.1`**，不暴露到外网，仅本机可访问。
- 任何数据**不上传云端**，无第三方请求（除你主动配置的远程 Git 仓库令牌外）。
- 所有"删除"均为软删除或本地文件改写，并保留 `.bak` 备份。
- 平台令牌明文存于本机 `.git/config`，请注意机器安全（见上方「平台令牌管理」安全提示）。

---

## 🧩 路线图（摘要）

| 阶段 | 内容 | 状态 |
| --- | --- | --- |
| MVP | 六类资产查看 / 搜索 / 筛选 / 编辑 / 删除 / 新增 + 本地写回 | ✅ 已交付 |
| v1.1 | 定时任务界面内新建、亮点按日期排序与筛选、空状态引导 | ✅ 已交付 |
| v1.5（收口版） | 内容分类体系 + 分类筛选；Skill 详情查看；对话记忆；本地 Git 仓库管理（扫描/状态/分支/远程 + fetch/pull/checkout/clone）；定时任务多字段编辑；误删恢复 UI；数据导出 JSON / 明暗主题 / 跨平台启动脚本；平台令牌管理（GitHub/Gitee 直接管理） | ✅ 已发布（v1.5.0） |
| v2.0 | **已取消**（用户决策：2.0+ 不做） | ❌ 取消 |
| v3.0 | **已取消**（同上：技能市场 / 模板编排 / 一键打包 / 多语言主题） | ❌ 取消 |

> 注：v1.5 原计划中的「AI 自动亮点捕获 / 会话结束记忆沉淀」在零依赖开源版不可行（无 AI 内核、不联网），已降级为 **agent 端沉淀 + 手动速记入口**。

完整产品定义见 [`AI工作台_PRD.md`](./AI工作台_PRD.md)。

---

## 📄 许可证

[MIT](./LICENSE) —— 可自由使用、修改、分发。
