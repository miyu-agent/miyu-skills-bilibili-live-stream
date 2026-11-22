<p align="center">
  <a href="README.md">English</a> | 简体中文
</p>

# 哔哩哔哩直播

> <sub>本文档由 Anthropic 的 Claude Fable 5.2 生成。</sub>

这是 Miyu 的一个技能软件包，提供一件工具 `bilibili_live` 和一个技能 `bilibili-live`，用来控制你自己的哔哩哔哩直播间：查看状态、查询分区、修改标题和分区、开播并获取推流码、下播，以及扫码登录。

软件包里有这些文件：

| 文件 | 说明 |
|---|---|
| `scripts/bilibili_live.py` | 工具本身。参数 `command` 的取值有 `status`、`areas`、`update`、`start`、`stop`、`login`、`logout`；另外还有参数 `title`、`area`、`search`、`parent`、`refresh`、`wait` 和 `format`（`md`/`json`）。 |
| `skills/bilibili-live/SKILL.md` | 技能的说明，包括三条硬性规则（开播之前先征得同意；直播中修改设置，观众会立刻看到；分区有歧义时让用户选择）、登录流程和命令表。 |

**登录**：登录分两步。第一次调用 `login` 时，工具把二维码写进工作区的 `bilibili-login-qr.png` 就返回，AI 会用 `show` 给你看；你扫码之后告诉 AI，AI 再调用一次 `login` 完成登录。也可以带上 `wait`（最多 100 秒），在一次调用里等待扫码。

**登录状态**存在 Miyu 给脚本的状态目录 `MIYU_STATE_DIR` 里（文件是 `credentials.json`，权限是 0600），按 Miyu 账号分开保存，重启之后仍然保留：每个账号扫自己的码，开自己的直播间。分区列表是缓存，放在 `MIYU_CACHE_DIR`。

**在哪里可以使用**（`Venues: local, private`）：本机的会话和通讯平台的私聊可以使用，群聊里不能使用。由于私聊里任何人都能发话，所以修改直播间和更换登录的命令（`update`、`start`、`stop`、`login`、`logout`）只给机器的主人使用（`MIYU_CALLER_OWNER=1`），其他人只能使用 `status`、`areas`。陌生人的私聊归属于系统账号，看不到你的登录。

**`Clicall: no`**：开播、下播、修改标题都会产生对外的效果，所以只能直接调用，AI 不能在命令里通过 `miyu call` 调用这个工具。

这个工具只使用 Python 标准库（二维码的 PNG 由工具自己编码）。接口和签名取自 [bili-live-hime](https://github.com/Rsplwe/bili-live-hime)。
