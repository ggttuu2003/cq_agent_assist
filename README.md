# CQ Agent Assist

一个使用 Python 构建的个人 Agent 项目。

当前提供本地个人资料、MySQL 会话保存和多轮模型对话：

```text
FastAPI → LangGraph 对话图 → LangChain 模型客户端 → 阿里百炼
```

RSS、RAG、MCP、插件、多模态和沙箱等功能，等真正开始实现时再逐步加入，不提前安装或配置。

## 本地启动

先启动本机 MySQL，确认项目根目录的 `.env` 已配置。已有配置直接使用，两个终端分别运行前后端。

终端一：启动后端（使用现有 Conda 环境）。

```bash
cd /Users/yizhoucp/IdeaProjects/self-project/CQ-agent-assist
conda activate cq-agent-assist
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

看到 `Application startup complete` 后，再启动前端。后端地址：<http://127.0.0.1:8000/docs>。

终端二：启动前端。

```bash
cd /Users/yizhoucp/IdeaProjects/self-project/CQ-agent-assist/frontend
npm run dev
```

前端地址：<http://127.0.0.1:5173/>。首次安装或依赖变化时，先在前端目录执行 `npm install`；平时直接启动即可。

停止服务：在两个终端分别按 `Ctrl+C`。以上命令在前台运行，日志会直接显示在各自终端中。

可在另一个终端检查后端：

```bash
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/models/default
```

开发环境启用了 React StrictMode。首次加载与刷新时，个人资料、默认模型和会话列表各读取一次，初始化逻辑避免了 StrictMode 检查导致的重复请求。连接失败时可在页面手动重试；HTTP 4xx/5xx 或连接失败需要结合响应和终端日志检查。

## 当前进度

先完成以下内容：

1. 加载环境变量
2. 提供 `/health` 健康检查接口
3. 提供 `/chat` 单轮对话接口，由 LangGraph 执行流程
4. 提供 React 聊天页面，支持 MySQL 会话记录、Markdown、代码复制和深浅主题
5. 设置和编辑个人昵称，保存会话并按当前会话携带历史上下文

MySQL 已用于保存个人资料、会话和消息。Milvus 尚未连接，工具和检索按后续需求增加。当前没有账号密码登录，只适合本机个人使用；同一服务的浏览器访问者共享一份个人资料。

## 环境准备

项目使用 Python 3.11 及以上版本，建议使用已有的 Conda 环境：

```bash
conda activate cq-agent-assist
python --version
python -m pip install -r requirements.txt
```

新环境尚无 `.env` 时复制配置模板；已有文件时保留原配置：

```bash
cp .env.example .env
```

MySQL 连接配置放在 `.env`；Milvus 配置暂不使用：

```env
MYSQL_HOST=127.0.0.1
MYSQL_PORT=3306
MYSQL_USER=root
MYSQL_PASSWORD=你的本地 MySQL 密码
MYSQL_DB=cq_agent_assist

MILVUS_URI=http://127.0.0.1:19530
MILVUS_COLLECTION=cq_agent_assist
```

如果本地数据库还没有创建：

```sql
CREATE DATABASE cq_agent_assist
  DEFAULT CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;
```

## 当前依赖

现在只有一个依赖文件：

```text
requirements.txt
```

后续开始实现具体功能时，再按实际需要添加对应依赖。例如实现 RSS 时再添加 RSS 解析库，实现 MCP 时再添加 MCP SDK，实现图片检索时再添加多模态模型依赖。

应用启动时自动创建缺失的 `cq_user`、`cq_conversation` 和 `cq_message` 表。旧版本会话表会补充 `model_name` 字段，优先按历史回答中的模型回填，没有记录时使用当前默认模型。

## 模型配置和对话

在本地 `.env` 中配置：

```env
LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
LLM_API_KEY=你的百炼密钥
LLM_MODEL=qwen-plus
```

启动后打开 [接口文档](http://127.0.0.1:8000/docs)，展开 `POST /chat`，点击 `Try it out` 并发送：

```json
{"message": "用一句话解释什么是 Agent"}
```

返回格式：

```json
{"reply": "模型生成的回答"}
```

切换到其他支持 OpenAI 兼容协议的服务时，一起修改地址、密钥、模型名，然后重启服务。对话代码不需要修改。只切换同一家服务的模型时，修改 `LLM_MODEL` 即可。

`OpenAI 兼容协议` 是请求格式，使用百炼地址时请求发往百炼。模型客户端由 `langchain-openai` 提供，项目不自行拼装模型 HTTP 请求。接入其他原生协议时，在 `app/llm/factory.py` 中创建对应的 LangChain 模型客户端。

当前 LangGraph 流程为 `START → model → END`。`MessagesState` 保存本次调用的消息，模型节点通过 `ainvoke` 生成回答。图本身不保存跨请求状态；会话接口从 MySQL 读取最近 20 轮、最多约 6 万字符的历史，再交给图。不同会话的上下文互不混用。原 `/chat` 仍是单轮调试接口，不保存历史。工具节点、检索节点和条件分支后续按需增加。

在 IDEA 中打开 `tests/test_chat.py`，点击测试类或方法左侧的绿色三角即可运行。测试使用模拟服务，不会调用百炼。

## 前端页面

前端位于 `frontend`，使用 React、TypeScript 和 Vite。视觉参考 [prompt-kit](https://www.prompt-kit.com/c/full-chat-app) 的中性色主题，欢迎页、会话侧栏和聊天布局做了独立设计，样式集中写在一个 CSS 文件中。

使用 Node.js 22.12 或更新版本，按本文开头的“本地启动”运行前后端后，打开 <http://127.0.0.1:5173> 即可聊天。

开发时，页面请求 `/api/*`，Vite 将其转发给 `http://127.0.0.1:8000/*`。模型地址和密钥继续使用后端 `.env`，前端不需要配置密钥。

输入框左下角显示当前模型名称。新会话通过 `GET /api/models/default` 读取后端的 `LLM_MODEL` 作为默认模型；已有会话使用会话自身保存的模型名称。默认模型接口只读取配置，不会调用模型服务。创建会话时保存当时的默认模型；后续发送使用会话绑定的模型，修改 `LLM_MODEL` 并重启后只影响之后创建的会话。每条回答也记录使用的模型，当前页面暂不提供模型切换。

打开页面立即显示聊天主界面，后端在后台准备随机默认昵称，不需要手动填写。加载或连接失败时仍停留在主界面，可输入内容并重试连接，连接完成后即可发送。侧栏底部显示个人资料，点击资料按钮可修改。昵称没有登录认证功能。会话和成功完成的问答保存在 MySQL，刷新或换浏览器后仍可查看；旧版 localStorage 匿名历史原样保留，本次不自动导入。

打开会话后，地址中的 `?conversation=会话ID` 记录当前页面位置，刷新会恢复同一会话及其模型，不会新建记录。点击“新对话”后刷新仍是空白页，发送第一条问题时才创建会话。无效或已删除的会话地址会回到空白页，网络失败时可重试恢复。各窗口和设备分别维护打开位置，正文统一由服务端保存；未来 Windows、手机端可使用相同会话接口。

聊天回答通过 SSE 逐段显示，完整回答保存成功后才标记完成。点击“停止生成”会关闭流连接，服务端取消本次生成并释放占用；未完成片段只留在当前页面。失败后可以重试，重试复用请求编号；停止或断开时会查询保存结果，已经提交成功的完整问答可以恢复。同一会话占用释放后才能再次发送。删除操作会同时删除该会话的消息，需要确认。

输入框支持 Enter 发送、Shift+Enter 换行，中文输入法选词时不会触发发送。搜索按钮或 `⌘/Ctrl + K` 可以搜索已加载的会话标题和正文。页面适配手机屏幕，左下角可切换深浅主题。

桌面端可拖动侧栏右边缘调整宽度，双击恢复默认宽度，刷新后会保留设置。分隔条聚焦后也可以用左右方向键微调。手机端保持抽屉式侧栏。

检查类型和构建：

```bash
cd frontend
npm run build
```

构建产物位于 `frontend/dist`。部署时需由静态服务器托管，并把 `/api/*` 转发到 FastAPI；Vite 的开发代理不会包含在构建产物中。

## 代码目录

```text
app/
├── main.py                # 应用入口、生命周期和路由注册
├── api/                   # 只放 HTTP 路由接口
│   ├── chat.py            # 模型信息与单轮调试接口
│   ├── conversations.py   # 会话管理与多轮对话接口
│   ├── health.py          # 健康检查
│   └── profile.py         # 本地昵称资料接口
├── dependencies.py        # 数据库、个人资料和 Agent 的接口依赖
├── schemas/               # 接口数据结构和校验
├── agent/                 # LangGraph 编排
├── llm/                   # 模型服务
├── core/                  # 基础配置和通用协议
├── db/                    # MySQL 连接和表模型
├── services/              # 个人资料、会话等业务逻辑
└── sources/               # 数据来源
```

```text
frontend/
├── src/
│   ├── App.tsx            # 聊天页面、会话状态和交互
│   ├── Profile.tsx        # 昵称设置与资料编辑
│   ├── api.ts             # 请求 Python 后端
│   ├── Markdown.tsx       # Markdown 展示和代码复制
│   ├── styles.css         # 页面布局、深浅主题和手机适配
│   └── main.tsx           # React 入口
├── public/favicon.svg     # 页面图标
├── index.html             # 网页入口
├── vite.config.ts         # 本地开发代理
├── tsconfig.json          # TypeScript 配置
├── package.json           # 前端依赖和运行命令
└── package-lock.json      # 锁定具体依赖版本
```


## 个人资料与会话接口

- `GET /models/default`：读取新会话的默认模型。
- `GET /profile`：读取当前个人资料；第一次访问时自动生成随机默认昵称。
- `PUT /profile`：修改昵称，例如 `{"display_name": "长青"}`。
- `GET /conversations`：加载会话。
- `POST /conversations`：发送 `{"first_message": "首次问题"}` 创建会话，后端立即保存问题前 28 个字符作为标题；不调用模型，模型失败后标题仍保留。前端直接使用返回的标题，发送消息接口不再修改标题。
- `GET /conversations/{id}`、`DELETE /conversations/{id}`：详情或删除。
- `POST /conversations/{id}/messages`：使用会话绑定的模型发送 `{"message": "你好", "request_id": "本轮UUID"}`；返回保存后的完整会话。重试使用相同请求编号。
- `POST /conversations/{id}/messages/stream`：前端发送与重试使用此接口，请求同上；返回 `delta` 文字片段、`done` 已保存问答或 `error` 事件。浏览器请求路径加 `/api` 前缀。

方案见 [002-个人资料与会话管理需求-2026-10-30.md](doc/002-个人资料与会话管理需求-2026-10-30.md) 和 [003-流式回答需求-2026-10-30.md](doc/003-流式回答需求-2026-10-30.md)。

普通测试不依赖 MySQL，也不调用真实模型：

```bash
python -m unittest discover -s tests -v
```

完整会话测试使用 `.env` 中的 MySQL 连接，在随机命名的 `cq_test_*` 临时库验证保存、上下文、去重、删除和并发，结束后只清理自己创建的临时库，不操作应用数据库。测试账号需要创建/删除临时库的权限：

```bash
CQ_TEST_MYSQL=1 python -m unittest discover -s tests -v
```

在 IDEA 单独运行 `tests/test_conversations.py` 时，也可以在运行配置的环境变量填写 `CQ_TEST_MYSQL=1`。
