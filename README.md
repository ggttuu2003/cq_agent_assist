# CQ Agent Assist

一个使用 Python 构建的个人 Agent 项目。

当前只搭建最小基础版本：

```text
FastAPI + MySQL + Milvus
```

RSS、RAG、MCP、插件、多模态和沙箱等功能，等真正开始实现时再逐步加入，不提前安装或配置。

## 当前第一步

先完成以下内容：

1. 加载环境变量
2. 连接 MySQL
3. 连接 Milvus
4. 提供 `/health` 健康检查接口

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

`.env` 当前只保留 MySQL 和 Milvus 配置：

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

## 下一步

先创建三个文件：

```text
app/settings.py
app/main.py
app/core/protocols.py
```

只完成配置加载、健康检查和基础扩展协议，确认服务可以启动后，再实现第一个具体功能。
