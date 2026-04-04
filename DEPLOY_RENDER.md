# Render 线上部署说明

这套部署方案直接复用你本地 Docker 拆分出来的三个进程：

- `web` -> Render `Web Service`
- `bot` -> Render `Background Worker`
- `worker` -> Render `Background Worker`

重点区别只有一个：

- 本地是 `docker compose` 帮你同时拉起 3 个容器
- Render 不直接运行整个 `docker-compose.yml`
- 所以线上要把这 3 个服务分别创建出来

仓库根目录已经补了一份 [`render.yaml`](/Users/luke/discord-words-cloud/render.yaml)，可以直接用 Blueprint 导入。

## 1. 为什么本地 Docker 能跑，线上不能直接照搬

你本地成功的关键点有两个：

1. [docker-compose.yml](/Users/luke/discord-words-cloud/docker-compose.yml) 把 3 个服务一起拉起来。
2. [scripts/generate_docker_env.py](/Users/luke/discord-words-cloud/scripts/generate_docker_env.py) 把多行 JSON 压成单行，让 `DISCORD_REGION_CHANNELS` 能被容器读取。

在线上平台里：

- `docker-compose.yml` 通常不会被当作“整套编排”直接运行
- 平台更常见的做法是“一个服务对应一个长期运行进程”
- 所以你要保留“3 个进程”的结构，但换成平台自己的服务编排方式

## 2. 推荐平台

推荐先用 `Render`。

原因：

- 很适合 `1 个 web + 多个常驻 worker` 这种架构
- 不需要你自己维护服务器
- 支持 Python 原生 runtime，避免你现在这个多阶段 [Dockerfile](/Users/luke/discord-words-cloud/Dockerfile) 在线上选错 `build target`
- 支持 Blueprint，一次性创建 3 个服务

## 3. 线上服务和本地服务的对应关系

本地 Docker Compose 里的服务：

- `web`
- `bot`
- `worker`

线上对应：

- `rubii-words-web`
  - 类型：`Web Service`
  - 启动命令：
    `gunicorn --bind 0.0.0.0:$PORT --workers 1 --threads 8 --timeout 120 --access-logfile - --error-logfile - src.web.wsgi:app`
- `rubii-words-bot`
  - 类型：`Background Worker`
  - 启动命令：
    `python -m src.main bot`
- `rubii-words-worker`
  - 类型：`Background Worker`
  - 启动命令：
    `python -m src.main daily-report-worker`

数据库迁移只放在 `web` 的 pre-deploy 里执行：

```bash
python -m src.main migrate-db
```

不要把 migration 同时挂到 `bot` 和 `worker`，这样最稳。

## 4. 部署前准备

先把仓库推到 GitHub。

然后准备线上环境变量。你本地已经有 [`.env`](/Users/luke/discord-words-cloud/.env) 和 [`.docker.env`](/Users/luke/discord-words-cloud/.docker.env) 的思路，线上直接复用同一批值即可。

至少需要这些值：

```env
DATABASE_URL=postgresql://...
DISCORD_BOT_TOKEN=...
DISCORD_REGION_CHANNELS=[{"key":"cn","name":"中文","guild_id":123,"channels":[{"id":456,"name":"聊天室"}]}]
LLM_BASE_URL=https://...
LLM_MODEL=...
LLM_API_KEY=...
```

注意：

- `DATABASE_URL` 在线上不能写 `localhost`
- 如果你已经有现成的 Neon / Supabase / Render Postgres，直接填外网连接串
- `DISCORD_REGION_CHANNELS` 必须是单行 JSON
- `DISCORD_BOT_TOKEN`、`LLM_API_KEY` 这类普通字符串，不要带首尾引号

## 5. 怎么从本地 `.env` 生成线上可粘贴的值

你仓库里的 [`scripts/generate_docker_env.py`](/Users/luke/discord-words-cloud/scripts/generate_docker_env.py) 在线上准备阶段仍然有用。

执行：

```bash
cd /Users/luke/discord-words-cloud
python3 scripts/generate_docker_env.py
```

然后查看：

```bash
rg '^DISCORD_REGION_CHANNELS=' .docker.env
```

把等号右边那一整段复制到 Render。

如果你要检查其它值：

```bash
rg '^(DATABASE_URL|DISCORD_BOT_TOKEN|LLM_BASE_URL|LLM_MODEL|LLM_API_KEY)=' .docker.env
```

注意：

- 如果某个值在 `.docker.env` 里长成 `"xxx"`，粘贴到 Render 时建议去掉最外层引号
- `DISCORD_REGION_CHANNELS` 这种 JSON 值本身不要改结构，只需要保持单行

## 6. 在 Render 上实际部署

### 方案 A：直接用 Blueprint

1. 进入 Render 控制台。
2. 选择 `New +` -> `Blueprint`。
3. 连接这个 GitHub 仓库。
4. 让 Render 读取根目录的 [`render.yaml`](/Users/luke/discord-words-cloud/render.yaml)。
5. 首次创建时，Render 会要求你填写 `sync: false` 的环境变量。
6. 按服务填写：

- `rubii-words-web`
  - `DATABASE_URL`
  - `DISCORD_REGION_CHANNELS`
- `rubii-words-bot`
  - `DATABASE_URL`
  - `DISCORD_BOT_TOKEN`
  - `DISCORD_REGION_CHANNELS`
- `rubii-words-worker`
  - `DATABASE_URL`
  - `DISCORD_REGION_CHANNELS`
  - `LLM_BASE_URL`
  - `LLM_MODEL`
  - `LLM_API_KEY`

7. 点击创建并等待部署完成。

### 方案 B：不用 Blueprint，手动建 3 个服务

如果你不想一次性导入，也可以在 Render 里手动创建：

1. 新建一个 `Web Service`
   - Runtime: `Python`
   - Build Command:
     `pip install -r requirements.txt`
   - Start Command:
     `gunicorn --bind 0.0.0.0:$PORT --workers 1 --threads 8 --timeout 120 --access-logfile - --error-logfile - src.web.wsgi:app`
   - Pre-deploy Command:
     `python -m src.main migrate-db`
   - Health Check Path:
     `/api/health`
2. 新建一个 `Background Worker`
   - Build Command:
     `pip install -r requirements.txt`
   - Start Command:
     `python -m src.main bot`
3. 再新建一个 `Background Worker`
   - Build Command:
     `pip install -r requirements.txt`
   - Start Command:
     `python -m src.main daily-report-worker`

三个服务都使用同一个仓库。

## 7. 部署完成后怎么验收

### Web

打开：

- 首页：`https://你的域名/`
- 健康检查：`https://你的域名/api/health`

期望健康检查返回：

```json
{"ok": true, "timestamp": "..."}
```

### Bot

查看 Render 日志，应该能看到类似：

```text
[bot] ready ...
```

如果没有连上，优先检查：

- `DISCORD_BOT_TOKEN`
- Bot 是否已被邀请进目标服务器
- 目标频道权限是否正确

### Worker

查看日志，应该能看到类似：

```text
[worker] ready tz=Asia/Shanghai interval=2h@05 daily=00:20 ...
```

如果启动失败，优先检查：

- `LLM_BASE_URL`
- `LLM_MODEL`
- `LLM_API_KEY`
- `DISCORD_REGION_CHANNELS`

## 8. 以后更新代码怎么发版

如果你用了 Blueprint 或 GitHub 自动部署：

- 直接 push 到绑定分支
- Render 会自动重新 build / deploy

这等价于你本地的：

```bash
docker compose --env-file .docker.env up -d --build
```

只是触发方式从“本地重建容器”变成了“平台自动重建服务”。

## 9. 最容易踩的坑

### 9.1 把 `docker-compose.yml` 当成线上编排文件

Render 不会像你本地那样直接帮你跑整套 Compose。

正确做法是把：

- `web`
- `bot`
- `worker`

拆成 3 个 Render 服务。

### 9.2 `DISCORD_REGION_CHANNELS` 仍然是多行

线上环境变量里必须放单行 JSON。

正确做法：

```bash
python3 scripts/generate_docker_env.py
rg '^DISCORD_REGION_CHANNELS=' .docker.env
```

### 9.3 把引号一起粘贴上去

像下面这种值：

```env
DISCORD_BOT_TOKEN="abc"
```

粘贴到 Render 时应该只填：

```text
abc
```

不要把外层引号也带上。

### 9.4 `DATABASE_URL` 还写着 `localhost`

线上服务里的 `localhost` 指向的是 Render 自己的容器，不是你电脑。

### 9.5 把 migration 放在所有服务上

只让 `web` 跑 pre-deploy migration，避免重复执行。

## 10. 安全提醒

本地 [`.docker.env`](/Users/luke/discord-words-cloud/.docker.env) 和 [`.env`](/Users/luke/discord-words-cloud/.env) 里承载的是高敏感配置。

建议：

- 确认这两个文件没有被提交到 Git
- 不要截图或发给第三方
- 如果昨天排查时曾把 token / API key 发到外部平台，尽快轮换对应密钥
