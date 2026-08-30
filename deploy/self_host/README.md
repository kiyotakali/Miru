# Miru Self-Host / Provisioning Lifecycle

这套脚本是 Miru 私有服务器的生命周期接口：官方开服后端可以非交互式调用，技术用户未来也可以手动使用同一套脚本自部署。

第一版官方主线不是让普通用户 SSH 服务器；普通用户只在官网付款，然后拿到长邀请码。这里的脚本是给官方 provisioning backend 或技术用户用的。

## Official Provisioning

官方 provisioning backend 在云服务器创建完成、拿到公网 IPv4、放行端口后，通过 SSH 执行：

```bash
SERVER_IP=<public-ipv4> \
SERVER_PORT=5001 \
MIRU_IMAGE=miru/server:0.2.0 \
AI_VISION_HOST=openrouter.ai \
AI_VISION_KEY=<secret> \
AI_VISION_MODEL=google/gemini-2.5-flash-lite \
AI_CHAT_HOST=openrouter.ai \
AI_CHAT_KEY=<secret> \
AI_CHAT_MODEL=openai/gpt-4.1-mini \
AI_MEMORY_HOST=openrouter.ai \
AI_MEMORY_KEY=<secret> \
AI_MEMORY_MODEL=qwen/qwen3-235b-a22b-2507 \
bash install.sh --non-interactive
```

如果使用 GitHub Releases 下载的镜像包，推荐传入 `--image-tar`，避免目标
服务器在线拉取镜像：

```bash
SERVER_IP=<public-ipv4> \
SERVER_PORT=5001 \
MIRU_IMAGE=miru/server:0.2.0 \
bash install.sh --non-interactive --image-tar /path/to/miru-server-v0.2.0-linux-amd64.tar.gz
```

安装成功后输出：

```text
SERVER_URL=http://<public-ipv4>:5001
MIRU_INVITATION_CODE=MIRU-XXXXXXXXXX-YYYYYY
MIRU_HOME=/opt/miru
```

Provisioning backend 只需要保存邀请码、服务器 IP、实例 id、镜像版本和健康状态；不读取用户聊天、截图、记忆。

## Technical Self-Deploy

技术用户可以手动运行：

```bash
bash install.sh
```

交互式模式会尝试探测公网 IPv4，并提示输入模型 API key。

自动安装 Docker 目前只承诺 Ubuntu/Debian。其他 Linux 用户如果已经自己装好 Docker/Compose，可以继续使用同一套脚本；否则请先完成 Docker/Compose 安装。

## Runtime Layout

```text
/opt/miru/
  .env          sensitive runtime config and API keys
  compose.yaml  Docker Compose stack
  data/         persistent user/admin data
  logs/         persistent logs
  backups/      local sensitive backups
```

`.env` 和备份文件都包含敏感信息，权限必须保持 `600`。

## Commands

```bash
bash status.sh --json
bash backup.sh
bash restore.sh /opt/miru/backups/miru-backup-YYYYmmdd_HHMMSS.tar.gz --yes
bash reset.sh --user-data --yes
bash reset.sh --factory --yes
bash update.sh --image miru/server:0.1.1 --image-tar /path/to/miru-server-0.1.1-linux-amd64.tar.gz
```

`reset.sh` 默认是 `--user-data`：清空用户、记忆、聊天、截图和旧邀请码，但保留 `.env`、compose 和 AI 配置。`--factory` 才会清空整个 data 目录。

`backup.sh` 默认包含 `.env`，因为官方恢复需要完整运行配置；备份文件因此包含 API key，不能公开分发。
