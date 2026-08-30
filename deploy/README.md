# Miru Private Server Docker

Milestone B 的目标是先把当前 Miru server 收敛成一个可重复构建、可本地启动、数据外挂不丢的单用户 Docker 镜像。干净云服务器部署、官方自动开服和自部署 install 脚本放到后续里程碑。

Milestone C 的 lifecycle 脚本在 `deploy/self_host/`。它们不是给普通用户学习 Docker 的主流程，而是给官方 provisioning backend 调用，同时兼容技术用户未来自部署。Milestone B 的 `docker_run_local.sh` / `docker_smoke_test.sh` 仍然只用于本地开发验收。

## Build

```bash
bash deploy/docker_build.sh 0.1.0
```

生成镜像：

```text
miru/server:0.1.0
```

发布给普通用户的第一版推荐使用镜像包，而不是要求目标服务器在线
`docker pull`。构建 linux/amd64 镜像后导出 GitHub Releases 文件：

```bash
MIRU_DOCKER_PLATFORM=linux/amd64 bash deploy/docker_build.sh 0.1.0
bash deploy/docker_export_image_tar.sh 0.1.0
```

输出：

```text
dist/miru-server-0.1.0-linux-amd64.tar.gz
dist/miru-server-0.1.0-linux-amd64.tar.gz.sha256
```

DMG 自有服务器向导会让用户 browse 选择这个镜像包，然后上传到
用户自己的云服务器并通过 `docker load` 导入。`deploy/docker_push_tcr.sh`
仅作为可选 registry 发布工具保留，不再是默认用户路径。

## Local Run

浏览器 / DMG 在同一台 Mac 上测试时，可以使用 `127.0.0.1`：

```bash
MIRU_DOCKER_HOST_PORT=5081 \
SERVER_IP=127.0.0.1 \
bash deploy/docker_run_local.sh 0.1.0
```

脚本会：

- 启动容器。
- 挂载数据到 `.docker/miru-data`。
- 挂载日志到 `.docker/miru-logs`。
- 打印当前长邀请码 `MIRU-XXXXXXXXXX-YYYYYY`。

手机真机局域网测试时，`SERVER_IP` 必须换成 Mac 的局域网 IPv4，例如：

```bash
MIRU_DOCKER_HOST_PORT=5081 \
SERVER_IP=192.168.1.23 \
bash deploy/docker_run_local.sh 0.1.0
```

不要给手机使用 `127.0.0.1`，那会指向手机自己。

同一个 data volume 从 `127.0.0.1` 切到局域网 IPv4 时，`docker_init.py` 会复用本地 invite，只重组 server 段；`docker_run_local.sh` 会等待 `docker_bootstrap.json` 里的 `server_ip/server_port` 与当前环境一致后再打印邀请码，避免拿到旧 server 段。

## Full Local Smoke

完整 smoke test 需要真实三层 API 配置。默认读取项目根目录 `.env`，也可以用 `MIRU_DOCKER_ENV_FILE` 指向其他 env 文件。

```bash
MIRU_DOCKER_ENV_FILE=.env \
bash deploy/docker_smoke_test.sh 0.1.0
```

它会使用临时 data/logs，不会污染当前项目数据。检查内容：

- 容器启动与 `/api/health`。
- Docker init 生成长邀请码。
- `/api/auth/login` 通过长码创建用户并返回 token。
- SSE 连接。
- 真实主 agent 聊天回复。
- 测试截图上传到 `/api/device/screenshot`。
- `llm_usage.jsonl` 中出现 `agent_chat_iter`、`ScreenObservationVLM`、`AttentionEngineEvaluate`，以及 `ScreenSemanticGate` / `ScreenSlotWriterV3` / `Pass4Append:screenshot` 之一。
- 容器重启后邀请码和用户不变。

如果只是想做无 API 的启动/登录 smoke，可临时使用：

```bash
MIRU_DOCKER_ALLOW_NO_API=1 bash deploy/docker_smoke_test.sh 0.1.0
```

这只能证明容器和登录链路，不算 Milestone B 的完整验收。

本地 shell 如果配置了 HTTP 代理，访问 `http://<LAN IPv4>:<port>` 时可能被代理误伤。smoke 脚本内部已对所有 `curl` 使用 `--noproxy '*'`；手动验证时也建议这样做：

```bash
curl --noproxy '*' http://<LAN IPv4>:5081/api/health
```

## Runtime Contract

容器内路径：

```text
/opt/miru/app   application code
/opt/miru/data  persistent user/admin data
/opt/miru/logs  persistent logs
```

关键环境变量：

```text
DATA_DIR=/opt/miru/data
LOG_DIR=/opt/miru/logs
MIRU_HEADLESS=1
PORT=5001
SERVER_IP=<public or LAN IPv4>
SERVER_PORT=<port clients dial>
```

`SERVER_IP` 必须是 IPv4。第一版长邀请码只编码 IPv4 + port，不接受域名。

LLM 配置沿用三层 env：

```text
AI_VISION_HOST / AI_VISION_KEY / AI_VISION_MODEL
AI_CHAT_HOST   / AI_CHAT_KEY   / AI_CHAT_MODEL
AI_MEMORY_HOST / AI_MEMORY_KEY / AI_MEMORY_MODEL
```

Docker 镜像使用根目录 `requirements-server.txt`，只安装服务端运行依赖。不要在镜像里安装 `pywebview` / `pynput` / `pytest` 这类桌面或测试依赖；本地传感、桌宠和 APK/DMG 打包不属于 server 镜像边界。

本地 `.docker/miru-data` 与 `.docker/miru-logs` 是运行数据，已加入 `.gitignore`；它们可能包含 token、截图日志和测试用户数据，不要提交或打包。

## Invitation Bootstrap

容器启动前会运行：

```bash
python scripts/docker_init.py
```

它会：

- 校验 `SERVER_IP / SERVER_PORT`。
- 创建或复用一个本地 6 位 invite。
- 将当前可拨通的长邀请码写入 `data/_admin/docker_bootstrap.json`。
- 在日志里打印 `MIRU_INVITATION_CODE=...`。

如果同一个 data volume 从 `SERVER_IP=127.0.0.1` 切换到 `SERVER_IP=<Mac LAN IPv4>`，脚本会复用同一个本地 invite，只重新组合 server 段。

## Verified 2026-06-07

- Docker Desktop CLI 修复后，`docker --version`、`docker compose version`、`docker info` 均可用。
- `bash deploy/docker_build.sh 0.1.0` 成功构建本地 `miru/server:0.1.0`。
- `bash deploy/docker_export_image_tar.sh 0.1.2-milestone-e <tmp>` 已验证可导出约 91 MiB 的 `.tar.gz` 镜像包和 sha256 文件；正式 release 使用同一流程导出 `0.1.0+` 稳定版本。
- `MIRU_DOCKER_ENV_FILE=<三层 API env> bash deploy/docker_smoke_test.sh 0.1.0` 通过完整 API smoke。
- `MIRU_DOCKER_ALLOW_NO_API=1 SERVER_IP=<Mac LAN IPv4> SERVER_PORT=5082 MIRU_DOCKER_HOST_PORT=5082 bash deploy/docker_smoke_test.sh 0.1.0` 通过局域网 boot/login/SSE/restart smoke。
- Android 真机在同一 Wi-Fi 输入 Docker 生成的 LAN 长码后可登录本机容器，注册设备、建立 SSE，并在系统授权后上传手机截图。
