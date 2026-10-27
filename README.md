# 哔哩哔哩直播

Miyu 包：一件工具 `bilibili_live`、一个技能 `bilibili-live`。控制自己的哔哩哔哩直播间：看状态、查分区、改标题分区、开播拿推流码、下播、扫码登录。

| 文件 | 是什么 |
|---|---|
| `scripts/bilibili_live.py` | 工具。`command`：`status`、`areas`、`update`、`start`、`stop`、`login`、`logout`；另有 `title`、`area`、`search`、`parent`、`refresh`、`wait`、`format`（md/json） |
| `skills/bilibili-live/SKILL.md` | 技能：三条硬规矩（开播先拿同意、直播中改设置观众立刻看到、分区歧义让用户挑）、登录流程、命令表 |

**登录**：两步。第一次 `login` 把二维码写进工作区 `bilibili-login-qr.png` 就返回，AI 用 `show` 给你看；你扫完说一声，AI 再调一次 `login` 完成。可以带 `wait`（至多 100 秒）在一次调用里等。

**登录态**存在 Miyu 给脚本的状态目录 `MIYU_SCRIPT_STATE_DIR`（`credentials.json`，权限 0600），按 Miyu 账号分开、重启还在：每个账号扫自己的码、开自己的直播间。分区列表是缓存，放 `MIYU_SCRIPT_CACHE_DIR`。

**在哪能用**（`Venues: local, private`）：本机的会话、通讯平台的私聊；群里没有。私聊谁都能发话，所以改直播间、换登录的（`update`、`start`、`stop`、`login`、`logout`）只给机器主人（`MIYU_CALLER_OWNER=1`），别人只能 `status`、`areas`。陌生人的私聊归系统账号，看不到你的登录。

**`Clicall: no`**：开播、下播、改标题都有对外效果，只能直接调，AI 不能在命令里经 `miyu call` 调。

只用 Python 标准库（二维码 PNG 自己编）。接口和签名取自 [bili-live-hime](https://github.com/Rsplwe/bili-live-hime)。
