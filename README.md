# KRE2API

将 Krea 网页视频生成链路封装为可部署的异步 API，支持账号池、素材上传、提交前积分询价、任务持久化轮询与管理页面。协议依据见 [手工链路记录](docs/PROTOCOL_ANALYSIS.zh-CN.md)。

## 对外接口

使用 `Authorization: Bearer <KR_API_KEY>` 或 `X-API-Key`：

```http
GET /v1/models
POST /v1/videos
GET /v1/videos/{task_id}
GET /v1/videos/{task_id}/content
```

兼容 `POST /v1/videos/generations`、`POST /api/v3/contents/generations/tasks`、`GET /api/v3/contents/generations/tasks/{task_id}`、`POST /v1/responses` 和 `GET /v1/responses/{task_id}`。

```json
{
  "model": "sd-2-5",
  "prompt": "图1中的人物走入房间，音频1作为对白参考",
  "duration": 6,
  "resolution": "720p",
  "aspect_ratio": "16:9",
  "image_urls": ["https://example.com/reference.png"],
  "audio_urls": ["https://example.com/dialogue.mp3"],
  "generate_audio": true,
  "background": true
}
```

`background` 默认为 `true`，先返回任务 ID，再查询状态。结果在 `data[].url`；状态为 `queued`、`preparing`、`submitting`、`running`、`succeeded`、`failed`、`expired`。任务出错时读取 `error`。`background=false` 会等待一段时间；超过同步等待时间后仍可通过任务 ID 继续查询。

支持的别名：`sd-2-0`、`sd-2-0-1080p`、`sd-2-0-4k`、`sd-2-0-fast`、`sd-2-0-mini`、`sd-2-0-fast-480p`、`sd-2-5`、`sd-2-5-480p`、`sd-2-5-1080p`、`minimax-h3-768p`、`minimax-h3`、`wan-3.0`、`wan-3.0-480p`、`wan-3.0-1080p`。未指定分辨率的别名默认 720p；原版 MiniMax H3 是固定 2K，768p 别名使用 Krea 的 H3 Max 变体。

`image_urls`、`video_urls`、`audio_urls` 可包含 URL 或 `{ "url": "…", "duration": 5 }` 对象。外部素材会先上传到 Krea；`first_frame` / `last_frame` 可分别指定首尾帧。提示词里的中文和英文素材编号会转换为 Krea 标签，未显式引用的上传素材自动补充标签。

## 积分询价与管理页

`POST /api/quote` 接收同样的请求参数，但只向 Krea 询价，不创建视频。返回各账号余额、预估消耗和可用性。管理页位于 `/`，使用 `KR_ADMIN_TOKEN` 登录，可查看账号、任务、实际扣费样本，也可用弹窗询价和提交任务。一次仅向同一账号分派一个任务，避免预留积分与余额差相互干扰。

管理页布局与 ak2api 控制台一致，提供账号启停和更新、任务详情（调用者请求、实际 Krea 提交请求、上游提交与轮询响应、调用者响应）、测试提交、实时询价、历史消耗、运行设置和接入文档。运行设置通过 `GET/PATCH /api/admin/settings` 持久化轮询间隔与任务超时；任务线程数由部署环境变量 `KR_TASK_WORKERS` 控制。`GET /api/admin/tasks/{task_id}` 提供完整任务详情，`DELETE /api/admin/tasks/finished` 清理已结束任务，历史积分样本会保留。上述管理接口仅接受管理员会话或管理令牌。

## 运行

复制 `.env.example` 为 `.env`，设置三个独立随机密钥，然后执行：

```bash
docker network create my-shared-net 2>/dev/null || true
docker compose up -d --build
```

浏览器账号先由用户手动登录，随后使用 `scripts/import_cdp.py` 从 CDP 导入会话。脚本需要已登录的浏览器调试地址、账号对应项目 ID 和 `KR_SYNC_TOKEN`；不会在控制台输出 Cookie。示例：

```bash
export KR_SYNC_TOKEN='从部署环境读取，不要写入仓库'
python scripts/import_cdp.py --cdp http://127.0.0.1:9236 \
  --name h04-joa --service https://kre2api.aiid.edu.kg \
  --launcher /path/to/h04-joa.bat --capture /path/to/events.jsonl
```

服务账号应配置为与浏览器相同的代理出口。若本机启动器使用 `socks5://127.0.0.1:20004`，在 3.5 pre 同步时使用 `--proxy-url socks5://host.docker.internal:20004`，以便容器访问宿主机的代理端口。会话 Cookie 保存在部署目录的 SQLite 数据库中，数据库和 CDP 原始日志均不可提交到 Git。

## 3.5 pre 部署

推送 `pre` 分支触发 `.github/workflows/deploy-test.yml`：构建 GHCR 镜像、自托管 runner 在 3.5 pre 拉取并重启 Compose、验证本地与 Cloudflare Tunnel 健康检查。部署目录为 `/home/btcfoxman/docker/kre2api`，宿主机端口 `8800`（容器内端口 `8796`），公开地址为 `https://kre2api.aiid.edu.kg`。Tunnel 入口需在首次部署时配置；应用密钥只存放在服务器 `.env` 中。
