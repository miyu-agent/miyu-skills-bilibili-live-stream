---
name: bilibili-live
description: "Run the user's Bilibili live room with the bilibili_live tool: go live and get the stream key, end the stream, change the title or area, check status, log in by QR code. Use when the user says 开播、下播、关播、改直播标题、换分区、推流码、RTMP、OBS、直播间状态、b站直播."
metadata:
  display_name: Bilibili live
  display_name_zh: 哔哩哔哩直播
  summary: Go live, end the stream, change title and area
  summary_zh: 开播拿推流码、下播、改直播标题和分区
---

# 哔哩哔哩直播间

调 `bilibili_live` 工具，`command` 必填。默认交回一段短文字；要完整数据加 `"format": "json"`。

## 三条硬规矩

1. **`start` 之前必须拿到用户明确的同意。** 它让直播间立刻公开，粉丝马上收到开播推送，撤不回来。「准备一下」「改个标题」「把推流码给我看看」都不是要开播；拿不准就问一句「现在开播吗？」。
2. **直播中 `update` 观众立刻看得到。** 改标题、分区前先 `status` 看是不是在播；在播的，告诉用户改了会马上生效，再改。
3. **分区有歧义时把候选摆给用户挑。** 交回 `ambiguous_area` 时，候选列在输出里（json 在 `error.detail.candidates`）。原样列给用户，用户挑定了用 id 重试；别自己猜一个。

## 命令

| command | 做什么 | 要登录 |
|---|---|---|
| `status` | 直播间状态：房间号、标题、分区、在不在播 | 是 |
| `areas` | 不带参数列父分区；`search` 搜子分区；`parent` 列一个父分区下的；`refresh` 跳过一天的缓存 | 否 |
| `update` | 改 `title`、`area`，两个一起给是一次请求（分开连着改会被限流） | 是 |
| `start` | 开播，交回服务器和推流码；可以同时带 `title`、`area`，先改再开 | 是 |
| `stop` | 下播 | 是 |
| `login` | 扫码登录，两步（见下） | — |
| `logout` | 删掉本地登录态 | — |

分区可以写 id（`371`）、子分区名（`虚拟日常`）、拼音（`xunirichang`）或 `父分区/子分区`（`虚拟主播/虚拟日常`）。

## 登录

输出里有 `not logged in`（json 的 `error.kind` 是 `auth`）就是没登录或登录失效：

1. 调 `{"command": "login"}`。它把二维码写进工作区的 `bilibili-login-qr.png`，交回绝对路径，**马上返回，不等**。
2. 用 `show` 工具把这张图给用户看，请用户用哔哩哔哩 App 扫码、在手机上点确认。
3. 用户说扫完了，再调一次 `{"command": "login"}`：交回「登录成功」就好了；交回「未扫码」「已扫码未确认」的，二维码没换，等用户弄完再调。用户说马上就扫的，可以带 `"wait": 60`，在这一次调用里等最多 60 秒。
4. 二维码 180 秒失效；失效后再调 `login` 会自动换一张新的，重新 `show` 给用户。
5. 登录成功后，接着做原来要做的那件事。

已经登录时调 `login` 只交回「已经登录」，什么都不动；换账号先 `logout`。

## 开播要验证

`start` 交回 `verify_required`（要扫码验证）或 `face_auth_required`（要人脸认证）时，工作区里有 `bilibili-verify-qr.png`：`show` 给用户，用户在 App 里做完，再调一次 `start`。有些分区（比如虚拟主播区）要账号做过人脸认证才能用；用户不想认证的，换分区。

## 其他

- **推流码是敏感凭据**：只给用户本人看，别发进群聊，别交给别的服务。
- 私聊里只有机器主人能开播、下播、改标题、登录；别人要，脚本交回 `not_owner`，照实说就行。
- 撞上「操作太频繁」（code -1）等十几秒再试。
- 开播后提醒用户：服务器填 OBS 的「服务器」，推流码填「串流密钥」；播完记得 `stop`。
