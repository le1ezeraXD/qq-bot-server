# QQ JM PDF Bot

一个基于腾讯 QQ 官方机器人 API 和 `jmcomic` 的群聊机器人。用户在群内 @机器人并发送 JM 号后，机器人会下载内容、合成为 PDF，并通过 QQ 原生大文件分片协议发送回群聊。

## 功能

- 从群聊消息中识别 `JM123456` 或纯数字格式
- 原始图片临时存放在 `buffer/`，PDF 合并到 `output/{JM号}.pdf`
- 自动复用已经生成的 PDF
- 小文件使用 Base64 上传，大文件使用 QQ 原生分片上传
- 同一 JM 号并发请求自动串行化
- 文件发送成功后 @原请求用户
- 分片上传与平台确认自动重试

## 环境要求

- Python 3.11+
- 已按照[启动接入文档](https://bot.q.qq.com/wiki/develop/api-v2/)创建并配置 QQ 官方机器人
- 已参考[事件订阅与通知文档](https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/interface-framework/event-emit.html#事件订阅intents)启用 QQ 群消息事件权限（`GROUP_AND_C2C_EVENT`）

## 安装

```bash
git clone <your-repository-url>
cd qq-bot-server
python -m venv .venv
```

Windows PowerShell：

```powershell
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Linux/macOS：

```bash
source .venv/bin/activate
pip install -r requirements.txt
```

## 配置

复制环境变量示例：

```powershell
Copy-Item jmcomic/.env.example jmcomic/.env
```

填写 `jmcomic/.env`：

```env
QQ_BOT_APPID=你的机器人AppID
QQ_BOT_SECRET=你的机器人AppSecret

# 可选：请求队列最大等待数与单用户冷却秒数
MAX_QUEUE_SIZE=5
USER_COOLDOWN_SECONDS=60
```

`.env` 已被 Git 忽略，请勿将密钥提交到仓库。

## 运行

```bash
python jmcomic/bot_server.py
```

在机器人所在群聊中发送：

```text
@机器人 JM123456
```

## 并发与防刷

机器人使用单工作器请求队列，避免大量群消息同时创建下载线程：

- 同一用户和 JM 号的重复排队请求会被忽略
- 同一用户重复请求同一 JM 号时，默认有 60 秒冷却；不同 JM 号可正常排队
- 默认最多允许 5 个任务等待，队列满后拒绝新请求
- 队列满时的群提示带有 15 秒节流，避免错误消息继续刷屏
- 下载运行在守护线程中，按 `Ctrl+C` 时不会因同步下载而无法退出

可通过 `.env` 调整 `MAX_QUEUE_SIZE` 和 `USER_COOLDOWN_SECONDS`。队列不宜设置过大，因为 QQ 群聊被动回复存在有效期。
## 文件上传说明

- 小于等于 6 MiB：通过 QQ 富媒体 Base64 接口上传
- 大于 6 MiB：通过 QQ 官方大文件分片接口上传
- QQ 附件上限按当前平台能力设为 100 MB
- 下载原图临时存放在 `buffer/`，合并成功后由 jmcomic 自动清理
- 下载生成的 PDF 保存在 `output/`，两个运行目录的内容都不会提交到 Git

## 项目结构

```text
.
├── jmcomic/
│   ├── .env.example
│   ├── bot_server.py
│   └── option.yml
├── buffer/
│   └── .gitkeep
├── output/
│   └── .gitkeep
├── .gitignore
├── LICENSE
├── README.md
└── requirements.txt
```

## 常见问题

### 机器人没有响应

确认机器人已连接 WebSocket，并在 QQ 开放平台启用了群聊公开消息事件。

### 提示文件超过限制

QQ 当前大附件能力最高为 100 MB。需要降低图片质量、拆分 PDF，或改为发送外部下载链接。

### 被动回复超时

群聊被动回复存在有效期。下载耗时过长时，最终消息可能被平台拒绝，可进一步改造成主动群消息流程。

## 安全与合规

请遵守所在地法律、内容来源网站条款、QQ 开放平台规则及版权要求。仅下载和传播你有权使用的内容。项目维护者不对滥用行为负责。

## License

MIT





