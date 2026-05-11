# Tm2Do

像上一份课程作业那样，搭一个**团队协作进度小站**：单文件、少魔法、改样式就搜模板字符串。

Build a tiny **team progress** site the way you'd hand in a class project — single file, little magic, tweak UI by searching template strings.

---

中文 · English

---

## 中文文档

### 为什么要再造一个协作工具

「企业级项目管理」往往意味着：权限矩阵、泳道、自动化、插件市场……对**小团队或课设组队**来说，先把**谁在做什么、做到哪一步**说清楚，往往就够了。

`Tm2Do` 面向这种需求：**Web 1.5**（服务端渲染 + 表单）、多用户登录、项目 / 里程碑 / 任务 / 评论 / 活动流水，以及基于流水的**进度回退**。

### 核心理念：像 py-blog 一样「一个文件就是一个站点」

- **`tm2do.py`** 承载路由、模板片段、SQLite 迁移与业务逻辑，没有层层分包。
- 页面以 **服务端预处理 + HTML 拼接** 为主，layout 使用 **Jinja2（DictLoader）**，与 [py-blog](https://github.com/WHC2006/py-blog) 同一套路。
- 部署：**装依赖 → 跑脚本**（或 `uvicorn`），适合挂在 Nginx/Caddy 后面。

### 功能一览

- **单文件**：FastAPI + SQLite；可选 `requirements.txt` 管理依赖。
- **多用户**：站长（全站唯一）+ 协作者；首次访问走 **`/setup`** 创建站长。
- **项目**：站长可见全部项目；协作者仅可见 **`project_members`**；**协作者新建项目**时自动加入成员。
- **项目列表页（登录后主页）**：顶部展示 **进行中里程碑** 仪表盘（剩余天数 / 今日截止 / 逾期），点击跳转至对应项目页的 `#ms-{id}` 锚点。
- **里程碑 / 任务 / 评论**：表单 CRUD；任务支持负责人与截止日期。
- **里程碑负责人**：里程碑可勾选 **多位** 活跃用户为负责人；数据保存在关联表 `milestone_assignees`（迁移自动从旧的单列 `assigned_user_id` 导入）；「指派给我的里程碑」在您被列为负责人之一时展示；与任务负责人相互独立。
- **项目详情页布局**：自上而下为 **时间线** → **指派给我的里程碑**（任务表 + 跳转至下方完整卡片 `#ms-{id}`）→ **全部里程碑**（完整编辑表单）→ **未归类任务** → **新建里程碑 / 新建任务**；**≥900px 宽屏**左侧粘性 **页面索引**，窄屏为顶部 `<details>` 折叠索引。
- **活动流水**：项目页展示；**任务 / 里程碑字段变更**带 `before`/`after` 快照。
- **进度回退**：协作者只可回退 **自己作为操作者（`actor_id`）** 的可逆流水；**站长可回退任意**可逆流水；若他人之后已改动同一实体则**悲观拒绝**。
- **安全**：登录 Session、变更类 POST **CSRF**。
- **备案号**：`TM2DO_ICP` / `TM2DO_ICP_URL` 控制页脚文案与链接。
- **反代友好**：`TM2DO_TRUST_PROXY` 识别 `X-Forwarded-Proto`；`TM2DO_ROOT_PATH` 支持子路径挂载；默认 **`127.0.0.1:8766`**；提供 **`GET /health`**。

### 30 秒上手

```bash
cd py-Tm2Do
pip install -r requirements.txt
python tm2do.py
```

浏览器打开 **http://127.0.0.1:8766/** ，按向导创建站长后登录。

等价启动（便于生产参数）：

```bash
python -m uvicorn tm2do:app --host 127.0.0.1 --port 8766
```

### 环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `TM2DO_HOST` | `127.0.0.1` | `python tm2do.py` 时的监听地址 |
| `TM2DO_PORT` | `8766` | `python tm2do.py` 时的监听端口 |
| `TM2DO_DB_PATH` | `./tm2do.sqlite3`（当前工作目录） | SQLite 文件路径 |
| `TM2DO_ROOT_PATH` | （空） | 子路径挂载前缀，例如 `/tm2do` |
| `TM2DO_TRUST_PROXY` | （关） | 设为 `1`/`true`/`yes`/`on` 时信任 `X-Forwarded-Proto`（Cookie `Secure`、HTTPS 判定） |
| `TM2DO_ICP` | （空） | 页脚备案文案，例如 `粤ICP备xxxxxxxx号` |
| `TM2DO_ICP_URL` | `https://beian.miit.gov.cn/` | 备案链接 |

> 仅在**可信反向代理之后**开启 `TM2DO_TRUST_PROXY`；直连公网且未反代时不要开，避免伪造 HTTPS。

### Nginx 反代示例（站点根路径）

```nginx
server {
    listen 443 ssl http2;
    server_name your.domain.com;

    # ssl_certificate ...;
    # ssl_certificate_key ...;

    large_client_header_buffers 8 16k;

    location / {
        proxy_pass http://127.0.0.1:8766;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

部署时建议设置环境变量：`TM2DO_TRUST_PROXY=1`。

### 子路径挂载示例（`/tm2do/`）

应用侧：`TM2DO_ROOT_PATH=/tm2do`。

```nginx
location /tm2do/ {
    proxy_pass http://127.0.0.1:8766/tm2do/;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-Prefix /tm2do;
}
```

（按实际上游路径微调 `proxy_pass` 与尾部斜杠。）

### 不打算做的事

- 不做 SPA / 富文本编辑器：需要时在本地写好描述再粘贴。
- 不做站内邮件与复杂审批流。
- 不做「第二位站长」：全站仅一名站长；换人请 README 自行 SQL 或后续加转让功能。

### 协议

MIT。随便用，写得不好别骂得太狠。

---

## English

### Why another tiny PM tool

Enterprise PM stacks are often overkill for **small teams or class projects**.
Sometimes you only need: **who owns what**, **what stage it's in**, and an audit trail.

`Tm2Do` is that: **Web 1.5** SSR + forms, multi-user login, projects / milestones /
tasks / comments / activity log, and **rollback** tied to reversible audit entries.

### Core idea: one file like py-blog

- **`tm2do.py`** is the whole app (routes, templates-in-code, SQLite migrations).
- Pages are mostly built by **server-side string assembly**; the shell layout uses
 **Jinja2 DictLoader**, same spirit as [py-blog](https://github.com/WHC2006/py-blog).
- Deploy: **install deps → run** (or `uvicorn`), typically behind Nginx/Caddy.

### Features

- **Single-file** FastAPI + SQLite + optional `requirements.txt`.
- **Multi-user**: one **owner** + collaborators; bootstrap via **`/setup`**.
- **Projects**: owner sees all; collaborators see **membership** projects only;
 creators auto-join **project_members** when they create a project.
- **Projects home (`/projects`)**: **in-progress milestones** strip with days-until-due / due-today / overdue;
 click jumps to `#ms-{id}` on the project page.
- **Milestones / tasks / comments** with form POST workflows.
- **Milestone owners**: each milestone can designate **multiple** active users via checkboxes;
 stored in **`milestone_assignees`** (migration imports legacy single `assigned_user_id`);
 the **Mine** section appears when you are among those owners (distinct from per-task assignees).
- **Project detail layout**: **timeline** first, then **milestones assigned to me**
 (task table + jump link to the full card `#ms-{id}`), then **all milestones** (full edit forms),
 **ungrouped tasks**, and **create forms** at the bottom; **sticky TOC** at ≥900px,
 collapsible **details** TOC on small screens.
- **Activity log** with `before`/`after` snapshots for task / milestone field updates.
- **Rollback**: collaborators only on events where they are **`actor_id`**;
 owners may rollback any reversible event; **pessimistic** reject if newer edits exist.
- **CSRF** on mutating POSTs; cookie sessions.
- **ICP footer** via `TM2DO_ICP` / `TM2DO_ICP_URL`.
- **Reverse-proxy friendly** (`TM2DO_TRUST_PROXY`, `TM2DO_ROOT_PATH`), **`GET /health`**,
 default bind **`127.0.0.1:8766`**.

### Quick start

```bash
cd py-Tm2Do
pip install -r requirements.txt
python tm2do.py
```

Open **http://127.0.0.1:8766/** and finish `/setup`, then sign in.

Alternative:

```bash
python -m uvicorn tm2do:app --host 127.0.0.1 --port 8766
```

### Environment variables

| Variable | Default | Description |
| --- | --- | --- |
| `TM2DO_HOST` | `127.0.0.1` | Bind host when running `python tm2do.py` |
| `TM2DO_PORT` | `8766` | Bind port when running `python tm2do.py` |
| `TM2DO_DB_PATH` | `./tm2do.sqlite3` | SQLite file path |
| `TM2DO_ROOT_PATH` | empty | URL prefix when mounted under a subpath |
| `TM2DO_TRUST_PROXY` | off | Set `1`/`true`/`yes`/`on` to honor `X-Forwarded-Proto` |
| `TM2DO_ICP` | empty | Footer filing text (China hosting) |
| `TM2DO_ICP_URL` | MIIT URL | Footer filing link |

Only enable **`TM2DO_TRUST_PROXY`** behind a **trusted** reverse proxy.

### Nginx reverse proxy (site root)

```nginx
server {
    listen 443 ssl http2;
    server_name your.domain.com;

    large_client_header_buffers 8 16k;

    location / {
        proxy_pass http://127.0.0.1:8766;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

Set `TM2DO_TRUST_PROXY=1` in production behind HTTPS terminators.

### Non-goals

- No SPA / WYSIWYG editor.
- No email workflows or enterprise RBAC beyond owner/collaborator + membership.
- No second owner account — transfer requires SQL or a future feature.

### License

MIT. Use it freely; be gentle when judging the code.
