# KREA 网页视频协议链路（2026-09-27）

本记录来自 `h04-joa.bat` 打开的 Chrome（CDP `127.0.0.1:9236`）中的一次人工登录、素材上传和视频生成。原始 CDP 日志保留在工作区 `output/cdp-krea/2026-09-26T19-52-11-406516+00-00/`，其中可能含有会话、提示词及素材地址，**不进入 Git 仓库**。本文只保留字段结构和脱敏数值。

## 观察到的请求顺序

| 阶段 | 网页请求 | 观察到的结果 |
| --- | --- | --- |
| 登录前探测 | `GET /api/galactus` | 未登录时 HTTP 401；登录后 HTTP 200，`application/grpc-but-not` 流 |
| 邮箱登录判定 | `POST /api/auth/email-flow` | HTTP 200，返回 `flow`、`emailAction` |
| 密码登录 | `POST /auth/v1/token` | HTTP 200，返回 Supabase 会话；期间出现 Cloudflare Turnstile 请求 |
| 余额 | `GET /api/billing-data` | `balance.total=5100`，另有 `recurring`、`one_time`、`free` |
| 素材上传 | `POST /api/upload` | `multipart/form-data`，字段 `file`、`createAsset=true`；响应一律使用 `imageUrl`，视频另有 `duration` |
| 积分报价 | `POST /api/jobs/estimate` | JSON `scope=video`、`inputParams`、`promoOptOut=false`，返回 `status=ready`、`computeUnits` |
| 生成 | `POST /api/jobs/v2/new/videoV2` | `multipart/form-data`，`payload` 为 JSON；HTTP 200，返回任务数组，初始 `status=scheduled` |
| 查询 | `GET /api/job-status?id=<job_id>` | 初始 `processing`，最后 `completed`；视频 MP4 位于 `result.image_urls[0]` |
| 完成后余额 | `GET /api/billing-data` | `balance.total=4947.951666…` |

这笔任务使用 `seedance-2-mini`、5 秒、480p、21:9、4 张参考图、1 段参考视频、2 段参考音频，不生成额外声音。上游报价 `152.048333…` compute units，完成后余额差也是 `152.048333…`，吻合。任务所用提示词和素材地址不记录于本文。

## 关键请求结构

`POST /api/jobs/estimate`：

```json
{
  "scope": "video",
  "inputParams": {
    "provider": "seedance-2-mini",
    "duration": 5,
    "resolution": "480p",
    "width": 1280,
    "height": 720,
    "aspectRatio": "21:9",
    "generateAudio": false,
    "hasVideoReference": true
  },
  "promoOptOut": false
}
```

`POST /api/jobs/v2/new/videoV2` 的表单 `payload`：

```json
{
  "provider": "seedance-2-mini",
  "prompt": "[提示词及 @Image1/@Video1/@Audio1 等标签]",
  "duration": 5,
  "generateAudio": false,
  "width": 1280,
  "height": 720,
  "resolution": "480p",
  "aspectRatio": "21:9",
  "project": "[账号项目 UUID]",
  "referenceImages": ["[Krea 上传地址]"],
  "referenceVideos": ["[Krea 上传地址]"],
  "referenceAudios": ["[Krea 上传地址]"]
}
```

网页会按素材类型把引用归一到 `@Image1`、`@Video1`、`@Audio1`，同类素材独立编号；未在提示词中显式引用的素材标签会追加到末尾。`kre2api` 把“视频1 / video2 / v3 / 视4 / 图片1 / 图1 / img2 / Image3 / Audio1 / 音频2”等写法转换成上述格式，仅转换存在对应素材的编号。

## 模型、报价和余额判断

网页前端公开的模型配置显示：Seedance 2.0 / Fast / Mini 支持 4–15 秒；Seedance 2.5 支持 4–30 秒；MiniMax H3 与 H3 Max 支持 5–15 秒；Wan 3.0 支持 2–30 秒。各模型的分辨率与素材上限以 `/v1/models` 输出为准。原版 MiniMax H3 是固定 2K；`minimax-h3-768p` 映射到网站提供的 **H3 Max 768p** 变体。

浏览器静态资源包含模型费率表，人工任务的 `152.048333…` 与网页报价相符；不过服务**不把静态费率当成最终价格**。每次提交前先用候选账号询价，再读取余额，扣除该账号的在途预留积分，够用才进入生成；上传后如参考视频时长导致报价变化，会再次询价并调整预留。完成后记录余额差和报价，建立按模型、秒数、分辨率、参考视频时长、素材数量索引的样本。管理页可直接弹窗显示实时询价和实际样本。

## 会话与风控观察

- 网页未登录时 `/api/galactus` 返回 401；密码登录过程中出现 Cloudflare Turnstile。服务复用**用户手动登录**获得的会话，不自动处理验证码。
- 视频接口依赖浏览器会话 Cookie，关键名称包括 `sb-superb-auth-token`、`cf_clearance`、`krea-workspace-id`。Cookie 值不能写入 Git、日志或问题说明。
- 从 CDP 读取该浏览器 Cookie，使用相同代理出口与浏览器 User-Agent 调用 `GET /api/billing-data`，已在本机验证 HTTP 200。无有效会话的请求返回 HTTP 401。部署时继续沿用账号代理配置，并在 3.5 pre 实测有效性。
- `/api/galactus` 用于网页实时推送；公开的 `GET /api/job-status?id=` 在人工任务中可独立返回完整状态，因此服务使用它做持久化轮询和重启恢复。
- 本次链路没有观察到生成接口 429。若后续遇到 429，应保留上游 HTTP 状态与归一化错误，暂停该账号重试；不要通过重复提交掩盖未知的上游受理结果。

服务端联调时，以表单编码方式提交同一接口得到 HTTP 429；改为与网页相同的 `multipart/form-data`、其中 `payload` 为普通表单字段后，上游接受任务并返回 `job_id`。这表明请求体格式至少是该次 429 的一个关键差异，不能将它直接归因为账号积分或设备指纹。`curl_cffi` 的文件与表单上传均使用 `CurlMime`，因为其 `files` 参数在当前版本不会执行上传。

网页实现可能变化。每次上游字段或价格变化，都应以新的 CDP 记录和实时询价核对。
