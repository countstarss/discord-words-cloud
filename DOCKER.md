# Docker 操作说明

本文档说明如何在本地使用 Docker / Docker Compose 运行本项目的 3 个服务：

- `web`: Flask Web UI + API
- `bot`: Discord 消息采集进程
- `worker`: 日报生成进程

## 1. 前置要求

本机需要安装：

- Docker Desktop 或 Docker Engine
- Docker Compose
- Python 3

可先检查：

```bash
docker --version
docker compose version
python3 --version
```

## 2. 环境变量准备

项目使用 [`.env`](/Users/luke/discord-words-cloud/.env) 保存运行配置。

至少需要这些变量：

```env
DATABASE_URL=postgresql://user:password@host:5432/dbname
DISCORD_BOT_TOKEN=your_discord_bot_token
LLM_BASE_URL=https://your-llm-endpoint
LLM_MODEL=your-model
LLM_API_KEY=your-api-key
```

注意：

- `DATABASE_URL` 不要加引号。
- 如果数据库跑在宿主机，而不是 Docker 容器里，连接地址不要写成 `localhost`。
- 对 Docker 来说，宿主机数据库通常应写为 `host.docker.internal`。

例如：

```env
DATABASE_URL=postgresql://postgres:password@host.docker.internal:5432/rubii_words
```

## 3. 为什么要生成 `.docker.env`

你的 [`.env`](/Users/luke/discord-words-cloud/.env) 里可能包含多行 JSON，例如 `DISCORD_REGION_CHANNELS`。

`docker compose` 不能直接解析这种多行格式，所以项目里提供了转换脚本：

[`scripts/generate_docker_env.py`](/Users/luke/discord-words-cloud/scripts/generate_docker_env.py)

它会把 [`.env`](/Users/luke/discord-words-cloud/.env) 转成 Docker Compose 可读取的 [`.docker.env`](/Users/luke/discord-words-cloud/.docker.env)。

## 4. 首次启动

在项目根目录执行：

```bash
cd /Users/luke/discord-words-cloud
python3 scripts/generate_docker_env.py
docker compose --env-file .docker.env up -d
```

这会启动：

- `discord-words-cloud-web-local`
- `discord-words-cloud-bot-local`
- `discord-words-cloud-worker-local`

启动后可访问：

- Web 页面: [http://127.0.0.1:8080](http://127.0.0.1:8080)
- 健康检查: [http://127.0.0.1:8080/api/health](http://127.0.0.1:8080/api/health)

## 5. 日常命令

查看服务状态：

```bash
docker compose --env-file .docker.env ps
```

启动服务：

```bash
docker compose --env-file .docker.env up -d
```

停止并删除服务：

```bash
docker compose --env-file .docker.env down
```

重启全部服务：

```bash
docker compose --env-file .docker.env restart
```

只重启某一个服务：

```bash
docker compose --env-file .docker.env restart web
docker compose --env-file .docker.env restart bot
docker compose --env-file .docker.env restart worker
```

## 6. 查看日志

查看全部日志：

```bash
docker compose --env-file .docker.env logs -f
```

只看 Web：

```bash
docker compose --env-file .docker.env logs -f web
```

只看 Bot：

```bash
docker compose --env-file .docker.env logs -f bot
```

只看 Worker：

```bash
docker compose --env-file .docker.env logs -f worker
```

只看最近 100 行：

```bash
docker compose --env-file .docker.env logs --tail 100 web
```

## 7. 修改代码后如何更新

如果你改了 Python 代码、模板或 Dockerfile，需要重新构建并重启：

```bash
python3 scripts/generate_docker_env.py
docker compose --env-file .docker.env up -d --build
```

如果只是改了 [`.env`](/Users/luke/discord-words-cloud/.env)，通常这样就够：

```bash
python3 scripts/generate_docker_env.py
docker compose --env-file .docker.env up -d
```

如果你想强制重建单个服务：

```bash
docker compose --env-file .docker.env build web
docker compose --env-file .docker.env up -d web
```

## 8. 单独操作某个服务

只启动 Web：

```bash
docker compose --env-file .docker.env up -d web
```

只启动 Bot：

```bash
docker compose --env-file .docker.env up -d bot
```

只启动 Worker：

```bash
docker compose --env-file .docker.env up -d worker
```

停止某个服务：

```bash
docker compose --env-file .docker.env stop web
docker compose --env-file .docker.env stop bot
docker compose --env-file .docker.env stop worker
```

## 9. 常见排查

### 9.1 `docker compose` 读取 `.env` 失败

如果报错类似：

```bash
unexpected character "{" in variable name
```

说明 [`.env`](/Users/luke/discord-words-cloud/.env) 里包含多行 JSON，记得先重新生成 [`.docker.env`](/Users/luke/discord-words-cloud/.docker.env)：

```bash
python3 scripts/generate_docker_env.py
```

然后使用：

```bash
docker compose --env-file .docker.env up -d
```

### 9.2 `web` 启动失败，数据库连不上

先检查：

```bash
docker compose --env-file .docker.env logs --tail 100 web
```

重点看：

- `DATABASE_URL` 是否正确
- 数据库是否允许当前 IP 访问
- 如果数据库在宿主机，是否使用了 `host.docker.internal`

### 9.3 `bot` 没有工作

检查日志：

```bash
docker compose --env-file .docker.env logs -f bot
```

重点看：

- `DISCORD_BOT_TOKEN` 是否正确
- `DISCORD_REGION_CHANNELS` 是否是有效 JSON
- Bot 是否有目标频道的访问权限

### 9.4 `worker` 没有生成报告

检查日志：

```bash
docker compose --env-file .docker.env logs -f worker
```

重点看：

- `LLM_BASE_URL`
- `LLM_MODEL`
- `LLM_API_KEY`
- 数据库连接是否正常

## 10. 文件说明

相关文件如下：

- [`Dockerfile`](/Users/luke/discord-words-cloud/Dockerfile): 定义 `web` / `bot` / `worker` 三个镜像目标
- [`docker-compose.yml`](/Users/luke/discord-words-cloud/docker-compose.yml): 本地 Compose 编排文件
- [`.env`](/Users/luke/discord-words-cloud/.env): 你平时维护的原始环境变量文件
- [`.docker.env`](/Users/luke/discord-words-cloud/.docker.env): 给 Docker Compose 使用的转换结果
- [`scripts/generate_docker_env.py`](/Users/luke/discord-words-cloud/scripts/generate_docker_env.py): 生成 `.docker.env` 的脚本

## 11. 推荐工作流

平时最推荐使用下面这套流程：

```bash
cd /Users/luke/discord-words-cloud
python3 scripts/generate_docker_env.py
docker compose --env-file .docker.env up -d --build
docker compose --env-file .docker.env ps
docker compose --env-file .docker.env logs -f web
```
