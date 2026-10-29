# CQ Agent Assist

一个使用 Python 构建的个人 Agent 项目。

当前提供健康检查和单轮模型对话：

```text
FastAPI → LangGraph 对话图 → LangChain 模型客户端 → 阿里百炼
```

RSS、RAG、MCP、插件、多模态和沙箱等功能，等真正开始实现时再逐步加入，不提前安装或配置。

## 当前进度

先完成以下内容：

1. 加载环境变量
2. 提供 `/health` 健康检查接口
3. 提供 `/chat` 单轮对话接口，由 LangGraph 执行流程
4. 提供 React 聊天页面，支持本地会话记录、Markdown、代码复制和深浅主题

MySQL 和 Milvus 尚未连接。当前对话不依赖数据库，也还没有工具调用和会话记忆。

## 环境准备

项目使用 Python 3.11 及以上版本，建议使用已有的 Conda 环境：

```bash
conda activate 你的环境名
python --version
python -m pip install -r requirements.txt
```

复制配置模板：

```bash
cp .env.example .env
```

MySQL 和 Milvus 的配置保留在 `.env`，后续实现持久化和检索时使用：

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

## 启动服务

```text
app/core/settings.py
app/main.py
```

当前已经完成配置加载和 `/health` 健康检查。启动服务：

```bash
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

另开一个终端验证：

```bash
curl http://127.0.0.1:8000/health
```

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

当前 LangGraph 流程为 `START → model → END`。`MessagesState` 保存本次调用的消息，模型节点通过 `ainvoke` 生成回答。图会复用，但尚未启用跨请求记忆，不同请求不会共享聊天内容。工具节点、检索节点和条件分支后续按需增加。

在 IDEA 中打开 `tests/test_chat.py`，点击测试类或方法左侧的绿色三角即可运行。测试使用模拟服务，不会调用百炼。

## 前端页面

前端位于 `frontend`，使用 React、TypeScript 和 Vite。视觉参考 [prompt-kit](https://www.prompt-kit.com/c/full-chat-app) 的中性色主题，欢迎页、会话侧栏和聊天布局做了独立设计，样式集中写在一个 CSS 文件中。

使用 Node.js 22.12 或更新版本。保持后端运行，在另一个终端执行：

```bash
cd frontend
npm install
npm run dev
```

打开 <http://127.0.0.1:5173> 即可聊天。若依赖已经安装，后续直接运行 `npm run dev`。

开发时，页面请求 `/api/chat`，Vite 会将其转发给 `http://127.0.0.1:8000/chat`。模型地址和密钥继续使用后端 `.env`，前端不需要配置密钥。

左侧会话记录保存在当前浏览器，刷新后仍可查看；清除浏览器数据后会丢失。它只是界面历史，当前模型每次只接收最新的一条问题，还没有多轮记忆。点击停止只取消前端等待，不保证后端模型调用随之停止。

输入框支持 Enter 发送、Shift+Enter 换行，中文输入法选词时不会触发发送。搜索按钮或 `⌘/Ctrl + K` 可以搜索本地会话。页面适配手机屏幕，左下角可切换深浅主题。

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
├── api/                   # HTTP 接口
│   ├── chat.py            # 对话路由
│   ├── dependencies.py    # 接口依赖的创建
│   └── health.py          # 健康检查
├── schemas/               # 接口数据结构和校验
│   └── chat.py            # 对话请求和响应
├── agent/                 # LangGraph 编排
│   └── graph.py           # 消息状态、模型节点和执行顺序
├── llm/                   # 模型服务
│   ├── factory.py         # 根据配置创建 LangChain 模型客户端
│   └── errors.py          # 模型配置和调用异常
├── core/                  # 基础配置和通用协议
│   ├── settings.py        # 环境配置
│   └── protocols.py       # 文档、数据源和检索协议
└── sources/               # 数据来源
    └── memory.py          # 内存数据源
```

```text
frontend/
├── src/
│   ├── App.tsx            # 聊天页面、会话状态和交互
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
