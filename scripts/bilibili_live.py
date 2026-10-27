#!/usr/bin/env python3
# Display name: Bilibili live
# 显示名称：哔哩哔哩直播
# Description: Control the user's Bilibili live room: status, areas, update title or area, start, stop, QR login. start goes public at once and notifies followers; get explicit consent first.
# 描述：控制哔哩哔哩直播间：看状态、查分区、改标题分区、开播拿推流码、下播、扫码登录。
# Timeout: 120
# Clicall: no
# Venues: local, private
# Parameters:
# {"type": "object", "properties": {
#   "command": {"type": "string", "enum": ["status", "areas", "update", "start", "stop", "login", "logout"], "description": "status, areas: read-only. update: change title and/or area. start: go live and get the stream key. stop: end the stream. login: two calls, QR code then confirm. logout: forget the login."},
#   "title": {"type": "string", "description": "update, or start: the new live title."},
#   "area": {"type": "string", "description": "update, or start: the area as an id (371), a child name (虚拟日常), pinyin, or parent/child (虚拟主播/虚拟日常)."},
#   "search": {"type": "string", "description": "areas: keyword matched against child names, pinyin and parent names."},
#   "parent": {"type": "string", "description": "areas: list only this parent's children, by name or id."},
#   "refresh": {"type": "boolean", "description": "areas: refetch the area list instead of the one-day cache."},
#   "wait": {"type": "integer", "description": "login: seconds to keep polling for the scan within this call, 0-100. Default 0: return at once."},
#   "format": {"type": "string", "enum": ["md", "json"], "description": "md (default): short text for reading. json: the full result, for code (e.g. piping miyu call into jq or python)."}
# }, "required": ["command"]}
"""Bilibili live room control, as a Miyu script tool.

Logic follows bili-live-hime (https://github.com/Rsplwe/bili-live-hime):
app signing in src/lib/app-sign.ts, startLive / updateRoomTitle /
updateRoomArea in src/api/live.ts. Standard library only.

The login (credentials.json, 0600) and a pending QR login live in
MIYU_SCRIPT_STATE_DIR, kept per account; the area list is a cache in
MIYU_SCRIPT_CACHE_DIR. Commands that change the room run only for the owner. QR codes for the user are written into the
workspace as PNG files; the caller shows them with the show tool.
"""

import gzip
import hashlib
import json
import os
import string
import struct
import sys
import time
import zlib
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

# src/lib/app-sign.ts：直播姬（bilibili link）的 appkey / appsec
APP_KEY = "aae92bc66f3edfab"
APP_SEC = "af125a0d5279fd576c1b4418a3e8276d"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36 Edg/143.0.0.0"
)
COMMON_HEADERS = {
    "accept": "*/*",
    "user-agent": USER_AGENT,
    "sec-ch-ua": '"Microsoft Edge";v="143", "Chromium";v="143", "Not A(Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-site",
}

LIVE_BASE = "https://api.live.bilibili.com"
MAIN_BASE = "https://api.bilibili.com"
PASSPORT_BASE = "https://passport.bilibili.com"

TIMEOUT = 15.0
QR_TTL = 180                       # 登录二维码的有效期（秒）
MAX_WAIT = 100                     # login 一次调用里至多轮询这么久，留出 Timeout 的余量
AREA_TTL = 24 * 3600               # 分区列表基本不动，缓存一天
AREA_LIST_LIMIT = 60               # 一次至多列这么多子分区
LIVE_STATUS_TEXT = {0: "未开播", 1: "直播中", 2: "轮播中"}
REQUIRED_COOKIES = ("SESSDATA", "bili_jct")
LOGIN_QR = "bilibili-login-qr.png"
VERIFY_QR = "bilibili-verify-qr.png"


class ToolError(Exception):
    """可预期的失败。kind 是稳定的分支依据，hint 是下一步。"""

    def __init__(self, kind, message, hint=None, code=None, detail=None):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.hint = hint
        self.code = code
        self.detail = detail

    def to_dict(self):
        out = {"kind": self.kind, "message": self.message}
        if self.code is not None:
            out["code"] = self.code
        if self.hint:
            out["hint"] = self.hint
        if self.detail:
            out["detail"] = self.detail
        return out


NOT_LOGGED_IN_HINT = "调 command=login 拿二维码，用 show 给用户扫，用户说扫完再调一次 login。"


class ApiError(ToolError):
    HINTS = {
        -101: NOT_LOGGED_IN_HINT,
        -111: NOT_LOGGED_IN_HINT,
        -1: "改直播间设置太频繁了，等十几秒再试。",
        60009: "分区不存在。先用 areas 查一个有效的分区。",
        60024: "这个分区要人脸认证才能用。换一个分区，或让用户先在 App 里认证。",
        60043: "这个分区要人脸认证才能用。换一个分区，或让用户先在 App 里认证。",
        60013: "非法请求（签名或版本号异常）。",
    }

    def __init__(self, code, message):
        super().__init__(
            "auth" if code in (-101, -111) else "api",
            "接口返回错误：%s（%s）" % (message, code),
            hint=self.HINTS.get(code), code=code)


# ---------------------------------------------------------------------------
# 表单序列化与 APP 签名（对齐 JS URLSearchParams）
# ---------------------------------------------------------------------------

_FORM_SAFE = frozenset(string.ascii_letters + string.digits + "*-._")


def form_escape(value):
    out = []
    for byte in str(value).encode("utf-8"):
        char = chr(byte)
        if byte < 128 and char in _FORM_SAFE:
            out.append(char)
        elif char == " ":
            out.append("+")
        else:
            out.append("%%%02X" % byte)
    return "".join(out)


def urlencode_pairs(pairs):
    return "&".join("%s=%s" % (form_escape(k), form_escape(v)) for k, v in pairs)


def app_sign(params):
    """追加 appkey → 按 key 排序 → md5(表单串 + appsec)。"""
    merged = {k: str(v) for k, v in params.items() if v is not None}
    merged["appkey"] = APP_KEY
    query = urlencode_pairs(sorted(merged.items(), key=lambda kv: kv[0]))
    return "%s&sign=%s" % (query, hashlib.md5((query + APP_SEC).encode()).hexdigest())


def _stringify(mapping):
    return {k: str(v) for k, v in mapping.items() if v is not None}


def _decode_body(encoding, raw):
    encoding = (encoding or "").lower().strip()
    if encoding == "gzip":
        return gzip.decompress(raw).decode("utf-8", "replace")
    if encoding in ("deflate", "zlib"):
        try:
            return zlib.decompress(raw).decode("utf-8", "replace")
        except zlib.error:
            return zlib.decompress(raw, -zlib.MAX_WBITS).decode("utf-8", "replace")
    if encoding and encoding != "identity":
        raise ToolError("http", "不支持的 Content-Encoding：%s" % encoding)
    return raw.decode("utf-8", "replace")


class BiliClient:
    def __init__(self, cookies=None):
        self.cookies = dict(cookies or {})
        self.cookies_changed = False

    @property
    def csrf(self):
        return self.cookies.get("bili_jct", "")

    def logged_in(self):
        return all(self.cookies.get(name) for name in REQUIRED_COOKIES)

    def _absorb_cookies(self, headers):
        for raw in headers.get_all("Set-Cookie") or []:
            first = raw.split(";", 1)[0].strip()
            if "=" not in first:
                continue
            name, value = (part.strip() for part in first.split("=", 1))
            if not name or not value or value.lower() in ("deleted", "null"):
                continue
            if self.cookies.get(name) != value:
                self.cookies[name] = value
                self.cookies_changed = True

    def call(self, base, path, method="GET", params=None, data=None,
             headers=None, sign=None, raw=False):
        """sign：None、"query"（签名进 query）、"body"（签名进表单）。"""
        if sign == "query":
            query = "?" + app_sign(params or {})
        elif params:
            query = "?" + urlencode_pairs(list(_stringify(params).items()))
        else:
            query = ""

        url = base + path + query
        request_headers = dict(COMMON_HEADERS)
        if headers:
            request_headers.update(headers)
        if self.cookies:
            request_headers["cookie"] = "; ".join(
                "%s=%s" % (k, v) for k, v in self.cookies.items())

        body = None
        if data is not None and method != "GET":
            payload = _stringify(data)
            request_headers["content-type"] = "application/x-www-form-urlencoded; charset=UTF-8"
            body = (app_sign(payload) if sign == "body"
                    else urlencode_pairs(list(payload.items()))).encode("utf-8")

        request = urlrequest.Request(url, data=body, method=method, headers=request_headers)
        try:
            response = urlrequest.urlopen(request, timeout=TIMEOUT)
        except urlerror.HTTPError as exc:
            response = exc
        except urlerror.URLError as exc:
            raise ToolError("network", "请求失败：%s（%s）" % (base + path, exc.reason))
        except TimeoutError:
            raise ToolError("network", "请求超时：%s" % (base + path))

        with response:
            status = getattr(response, "status", None) or getattr(response, "code", 0)
            text = _decode_body(response.headers.get("Content-Encoding"), response.read())
            self._absorb_cookies(response.headers)

        try:
            payload = json.loads(text)
        except ValueError:
            raise ToolError("http", "接口返回的不是 JSON（HTTP %s）：%s" % (status, text[:200]))

        if raw:
            return payload
        if payload.get("code") != 0 or payload.get("data") is None:
            raise ApiError(payload.get("code"),
                           payload.get("message") or payload.get("msg") or "Unknown Error")
        return payload["data"]

    # -- 接口（对应 bili-live-hime 的 src/api/*.ts）---------------------------

    def nav(self):
        return self.call(MAIN_BASE, "/x/web-interface/nav", headers={"origin": MAIN_BASE})

    def room_id_by_uid(self, uid):
        data = self.call(LIVE_BASE, "/room/v2/Room/room_id_by_uid",
                         params={"uid": uid}, headers={"origin": LIVE_BASE})
        return int(data["room_id"])

    def room_info(self, room_id):
        return self.call(LIVE_BASE, "/room/v1/Room/get_info",
                         params={"room_id": room_id}, headers={"origin": LIVE_BASE})

    def live_version(self):
        return self.call(
            LIVE_BASE, "/xlive/app-blink/v1/liveVersionInfo/getHomePageLiveVersion",
            params={"system_version": "2", "ts": str(int(time.time() * 1000))},
            sign="query", headers={"origin": LIVE_BASE})

    def area_list(self):
        """父分区数组，每个带 list 子分区。"""
        return self.call(LIVE_BASE, "/room/v1/Area/getList",
                         params={"show_pinyin": "1"}, headers={"origin": LIVE_BASE})

    def update_room(self, room_id, title=None, area_id=None):
        """标题和分区打的是同一个接口，合成一次发：连发两次第二次会被挡成「操作太频繁」(-1)。"""
        data = {"room_id": str(room_id), "platform": "pc_link",
                "csrf": self.csrf, "csrf_token": self.csrf}
        if title is not None:
            data["title"] = title
        if area_id is not None:
            data["area_id"] = str(area_id)
        return self.call(LIVE_BASE, "/room/v1/Room/update", method="POST",
                         data=data, headers={"origin": LIVE_BASE})

    def start_live(self, room_id, area_v2, version, build):
        return self.call(
            LIVE_BASE, "/room/v1/Room/startLive", method="POST",
            data={"room_id": str(room_id), "platform": "pc_link", "backup_stream": "0",
                  "csrf": self.csrf, "csrf_token": self.csrf, "area_v2": str(area_v2),
                  "version": str(version), "build": str(build),
                  "ts": str(int(time.time() * 1000))},
            headers={"origin": LIVE_BASE}, sign="body",
            raw=True)  # 60024 / 60043 要看原始 code

    def stop_live(self, room_id):
        return self.call(LIVE_BASE, "/room/v1/Room/stopLive", method="POST",
                         data={"room_id": str(room_id), "platform": "pc_link",
                               "csrf": self.csrf, "csrf_token": self.csrf},
                         headers={"origin": LIVE_BASE})

    def qrcode_generate(self):
        return self.call(PASSPORT_BASE, "/x/passport-login/web/qrcode/generate",
                         headers={"origin": PASSPORT_BASE})

    def qrcode_poll(self, qrcode_key):
        return self.call(PASSPORT_BASE, "/x/passport-login/web/qrcode/poll",
                         params={"qrcode_key": qrcode_key},
                         headers={"origin": PASSPORT_BASE})



# ---------------------------------------------------------------------------
# 二维码（纯标准库；只生成 PNG 文件，不往终端画）
# ---------------------------------------------------------------------------

# (版本, 纠错等级) -> (每块纠错码字数, [(块数, 每块数据码字数), ...])
_QR_BLOCKS = {
    (1, "L"): (7, [(1, 19)]),   (1, "M"): (10, [(1, 16)]),
    (1, "Q"): (13, [(1, 13)]),  (1, "H"): (17, [(1, 9)]),
    (2, "L"): (10, [(1, 34)]),  (2, "M"): (16, [(1, 28)]),
    (2, "Q"): (22, [(1, 22)]),  (2, "H"): (28, [(1, 16)]),
    (3, "L"): (15, [(1, 55)]),  (3, "M"): (26, [(1, 44)]),
    (3, "Q"): (18, [(2, 17)]),  (3, "H"): (22, [(2, 13)]),
    (4, "L"): (20, [(1, 80)]),  (4, "M"): (18, [(2, 32)]),
    (4, "Q"): (26, [(2, 24)]),  (4, "H"): (16, [(4, 9)]),
    (5, "L"): (26, [(1, 108)]), (5, "M"): (24, [(2, 43)]),
    (5, "Q"): (18, [(2, 15), (2, 16)]), (5, "H"): (22, [(2, 11), (2, 12)]),
    (6, "L"): (18, [(2, 68)]),  (6, "M"): (16, [(4, 27)]),
    (6, "Q"): (24, [(4, 19)]),  (6, "H"): (28, [(4, 15)]),
    (7, "L"): (20, [(2, 78)]),  (7, "M"): (18, [(4, 31)]),
    (7, "Q"): (18, [(2, 14), (4, 15)]), (7, "H"): (26, [(4, 13), (1, 14)]),
    (8, "L"): (24, [(2, 97)]),  (8, "M"): (22, [(2, 38), (2, 39)]),
    (8, "Q"): (22, [(4, 18), (2, 19)]), (8, "H"): (26, [(4, 14), (2, 15)]),
    (9, "L"): (30, [(2, 116)]), (9, "M"): (22, [(3, 36), (2, 37)]),
    (9, "Q"): (20, [(4, 16), (4, 17)]), (9, "H"): (24, [(4, 12), (4, 13)]),
    (10, "L"): (18, [(2, 68), (2, 69)]), (10, "M"): (26, [(4, 43), (1, 44)]),
    (10, "Q"): (24, [(6, 19), (2, 20)]), (10, "H"): (28, [(6, 15), (2, 16)]),
}

_QR_ALIGN = {
    1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30],
    6: [6, 34], 7: [6, 22, 38], 8: [6, 24, 42], 9: [6, 26, 46], 10: [6, 28, 50],
}

_QR_FORMAT_BITS = {"L": 0b01, "M": 0b00, "Q": 0b11, "H": 0b10}
_QR_MAX_VERSION = 10


class QRError(Exception):
    pass


# -- GF(256) 与 Reed-Solomon ------------------------------------------------

_GF_EXP = [0] * 512
_GF_LOG = [0] * 256


def _gf_init():
    value = 1
    for i in range(255):
        _GF_EXP[i] = value
        _GF_LOG[value] = i
        value <<= 1
        if value & 0x100:
            value ^= 0x11D
    for i in range(255, 512):
        _GF_EXP[i] = _GF_EXP[i - 255]


_gf_init()


def _gf_mul(a, b):
    if a == 0 or b == 0:
        return 0
    return _GF_EXP[_GF_LOG[a] + _GF_LOG[b]]


def _rs_generator(degree):
    poly = [1]
    for i in range(degree):
        nxt = [0] * (len(poly) + 1)
        for j, coef in enumerate(poly):
            nxt[j] ^= coef
            nxt[j + 1] ^= _gf_mul(coef, _GF_EXP[i])
        poly = nxt
    return poly


def _rs_encode(data, ec_len):
    generator = _rs_generator(ec_len)
    remainder = [0] * ec_len
    for byte in data:
        factor = byte ^ remainder[0]
        remainder = remainder[1:] + [0]
        for i in range(ec_len):
            remainder[i] ^= _gf_mul(generator[i + 1], factor)
    return remainder


# -- 码字编排 ---------------------------------------------------------------


def _pick_version(length, level):
    for version in range(1, _QR_MAX_VERSION + 1):
        _, blocks = _QR_BLOCKS[(version, level)]
        total = sum(count * size for count, size in blocks)
        header = 4 + (8 if version <= 9 else 16)
        if header + length * 8 <= total * 8:
            return version
    raise QRError("内容过长（%d 字节），超出版本 %d-%s 的容量"
                  % (length, _QR_MAX_VERSION, level))


def _encode_codewords(payload, version, level):
    ec_per_block, blocks = _QR_BLOCKS[(version, level)]
    total_data = sum(count * size for count, size in blocks)

    bits = []

    def put(value, length):
        for i in range(length - 1, -1, -1):
            bits.append((value >> i) & 1)

    put(0b0100, 4)                                   # 8 位字节模式
    put(len(payload), 8 if version <= 9 else 16)     # 字符数
    for byte in payload:
        put(byte, 8)
    put(0, min(4, total_data * 8 - len(bits)))       # 终止符
    while len(bits) % 8:
        bits.append(0)

    data = bytearray()
    for i in range(0, len(bits), 8):
        byte = 0
        for bit in bits[i:i + 8]:
            byte = (byte << 1) | bit
        data.append(byte)
    for i in range(total_data - len(data)):
        data.append(0xEC if i % 2 == 0 else 0x11)    # 填充码字

    data_blocks, ec_blocks, pos = [], [], 0
    for count, size in blocks:
        for _ in range(count):
            chunk = data[pos:pos + size]
            pos += size
            data_blocks.append(chunk)
            ec_blocks.append(_rs_encode(chunk, ec_per_block))

    result = bytearray()
    for i in range(max(len(b) for b in data_blocks)):
        for block in data_blocks:
            if i < len(block):
                result.append(block[i])
    for i in range(ec_per_block):
        for block in ec_blocks:
            result.append(block[i])
    return result


# -- 矩阵 -------------------------------------------------------------------


def _draw_function_patterns(modules, reserved, version, size):
    def finder(row, col):
        for r in range(-1, 8):
            for c in range(-1, 8):
                rr, cc = row + r, col + c
                if not (0 <= rr < size and 0 <= cc < size):
                    continue
                dark = (
                    (0 <= r <= 6 and c in (0, 6))
                    or (0 <= c <= 6 and r in (0, 6))
                    or (2 <= r <= 4 and 2 <= c <= 4)
                )
                modules[rr][cc] = 1 if dark else 0
                reserved[rr][cc] = True

    finder(0, 0)
    finder(0, size - 7)
    finder(size - 7, 0)

    for i in range(size):                             # 定位图形
        if not reserved[6][i]:
            modules[6][i] = 1 if i % 2 == 0 else 0
            reserved[6][i] = True
        if not reserved[i][6]:
            modules[i][6] = 1 if i % 2 == 0 else 0
            reserved[i][6] = True

    centers = _QR_ALIGN[version]                      # 校正图形
    last = len(centers) - 1
    for i, row in enumerate(centers):
        for j, col in enumerate(centers):
            # 只跳过与三个定位图形重叠的角，落在定位图形（timing）行列上的要照画
            if (i, j) in ((0, 0), (0, last), (last, 0)):
                continue
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    modules[row + dr][col + dc] = 0 if max(abs(dr), abs(dc)) == 1 else 1
                    reserved[row + dr][col + dc] = True

    modules[size - 8][8] = 1                          # 固定的暗模块
    reserved[size - 8][8] = True

    for i in range(9):                                # 预留格式信息区
        if i != 6:
            reserved[i][8] = True
            reserved[8][i] = True
    for i in range(8):
        reserved[8][size - 1 - i] = True
        reserved[size - 1 - i][8] = True

    if version >= 7:                                  # 预留版本信息区
        for i in range(18):
            a, b = size - 11 + i % 3, i // 3
            reserved[b][a] = True
            reserved[a][b] = True


def _place_data(modules, reserved, data, size):
    index = 0
    total = len(data) * 8
    right = size - 1
    while right >= 1:
        if right == 6:
            right = 5
        for vert in range(size):
            for j in range(2):
                col = right - j
                upward = ((right + 1) & 2) == 0
                row = (size - 1 - vert) if upward else vert
                if reserved[row][col] or index >= total:
                    continue
                modules[row][col] = (data[index >> 3] >> (7 - (index & 7))) & 1
                index += 1
        right -= 2


def _mask_bit(mask, row, col):
    if mask == 0:
        return (row + col) % 2 == 0
    if mask == 1:
        return row % 2 == 0
    if mask == 2:
        return col % 3 == 0
    if mask == 3:
        return (row + col) % 3 == 0
    if mask == 4:
        return (row // 2 + col // 3) % 2 == 0
    if mask == 5:
        return (row * col) % 2 + (row * col) % 3 == 0
    if mask == 6:
        return ((row * col) % 2 + (row * col) % 3) % 2 == 0
    return ((row + col) % 2 + (row * col) % 3) % 2 == 0


def _draw_format(modules, level, mask, size):
    data = (_QR_FORMAT_BITS[level] << 3) | mask
    rem = data
    for _ in range(10):
        rem = (rem << 1) ^ ((rem >> 9) * 0x537)
    value = ((data << 10) | rem) ^ 0x5412

    def bit(i):
        return (value >> i) & 1

    for i in range(6):
        modules[i][8] = bit(i)
    modules[7][8] = bit(6)
    modules[8][8] = bit(7)
    modules[8][7] = bit(8)
    for i in range(9, 15):
        modules[8][14 - i] = bit(i)
    for i in range(8):
        modules[8][size - 1 - i] = bit(i)
    for i in range(8, 15):
        modules[size - 15 + i][8] = bit(i)
    modules[size - 8][8] = 1


def _draw_version(modules, version, size):
    if version < 7:
        return
    rem = version
    for _ in range(12):
        rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
    value = (version << 12) | rem
    for i in range(18):
        bit = (value >> i) & 1
        a, b = size - 11 + i % 3, i // 3
        modules[b][a] = bit
        modules[a][b] = bit


def _finder_history_add(run, history, size):
    if history[0] == 0:
        run += size                    # 符号外一律当作浅色边界
    history.pop()
    history.insert(0, run)


def _finder_history_count(history):
    """数 1:1:3:1:1 且一侧有 4 倍浅色的取景图形样式（可按比例放大）。"""
    n = history[1]
    core = (n > 0 and history[2] == n and history[3] == n * 3
            and history[4] == n and history[5] == n)
    return ((1 if core and history[0] >= n * 4 and history[6] >= n else 0)
            + (1 if core and history[6] >= n * 4 and history[0] >= n else 0))


def _penalty(modules, size):
    """ISO/IEC 18004 的四条掩码评分规则。"""
    score = 0
    lines = [row[:] for row in modules]
    lines += [[modules[r][c] for r in range(size)] for c in range(size)]

    for line in lines:
        run_color, run_len, history = 0, 0, [0] * 7
        for value in line:
            if value == run_color:
                run_len += 1
                if run_len == 5:
                    score += 3                     # 规则 1
                elif run_len > 5:
                    score += 1
            else:
                _finder_history_add(run_len, history, size)
                if not run_color:
                    score += _finder_history_count(history) * 40   # 规则 3
                run_color, run_len = value, 1
        if run_color:
            _finder_history_add(run_len, history, size)
            run_len = 0
        _finder_history_add(run_len + size, history, size)
        score += _finder_history_count(history) * 40

    for r in range(size - 1):                       # 规则 2
        for c in range(size - 1):
            block = modules[r][c] + modules[r][c + 1] + modules[r + 1][c] + modules[r + 1][c + 1]
            if block in (0, 4):
                score += 3

    dark = sum(sum(row) for row in modules)         # 规则 4
    score += 10 * (int(abs(dark * 100.0 / (size * size) - 50)) // 5)
    return score


def qr_matrix(text, level="M", force_mask=None, force_version=None):
    """把文本编成二维码矩阵，返回 size×size 的 0/1 列表（1 = 暗）。"""
    payload = text.encode("utf-8")
    version = force_version or _pick_version(len(payload), level)
    size = version * 4 + 17
    codewords = _encode_codewords(payload, version, level)

    base_modules = [[0] * size for _ in range(size)]
    reserved = [[False] * size for _ in range(size)]
    _draw_function_patterns(base_modules, reserved, version, size)
    _place_data(base_modules, reserved, codewords, size)

    best, best_score = None, None
    masks = [force_mask] if force_mask is not None else range(8)
    for mask in masks:
        candidate = [row[:] for row in base_modules]
        for r in range(size):
            for c in range(size):
                if not reserved[r][c] and _mask_bit(mask, r, c):
                    candidate[r][c] ^= 1
        _draw_format(candidate, level, mask, size)
        _draw_version(candidate, version, size)
        score = _penalty(candidate, size)
        if best_score is None or score < best_score:
            best, best_score = candidate, score
    return best


def qr_png_bytes(text, level="L", scale=10, quiet=4):
    """把二维码编成 PNG 字节流（8 位灰度，纯 zlib，无第三方依赖）。"""
    matrix = qr_matrix(text, level)
    size = len(matrix)
    side = (size + quiet * 2) * scale

    rows = []
    for y in range(side):
        my = y // scale - quiet
        row = bytearray([0])                       # 每行的 filter type
        inside_y = 0 <= my < size
        for x in range(side):
            mx = x // scale - quiet
            dark = inside_y and 0 <= mx < size and matrix[my][mx]
            row.append(0 if dark else 255)
        rows.append(bytes(row))

    def chunk(tag, payload):
        return (struct.pack(">I", len(payload)) + tag + payload
                + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", side, side, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"".join(rows), 9))
            + chunk(b"IEND", b""))



# ---------------------------------------------------------------------------
# 状态：凭据、未完成的登录放在 MIYU_SCRIPT_STATE_DIR（按账号分开、重启还在），
# 分区列表是缓存，放 MIYU_SCRIPT_CACHE_DIR。
# 凭据只有一个来处：login 写，logout 删，别的命令读。
# ---------------------------------------------------------------------------


def state_dir(required=True):
    path = os.environ.get("MIYU_SCRIPT_STATE_DIR")
    if path:
        return path
    if required:
        raise ToolError("no_state", "没有 MIYU_SCRIPT_STATE_DIR，登录态存不住。",
                        hint="这个工具要在 Miyu 的会话里跑；Miyu 会按账号给它一个状态目录。")
    return None


def _state_file(name, required=True):
    base = state_dir(required)
    return os.path.join(base, name) if base else None


def _cache_file(name):
    base = os.environ.get("MIYU_SCRIPT_CACHE_DIR")
    return os.path.join(base, name) if base else None


def _read_json(path):
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def _write_json(path, payload, private=False):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    if private:
        os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return path


def _remove(path):
    try:
        os.remove(path)
        return True
    except (OSError, TypeError):
        return False


def load_cookies():
    data = _read_json(_state_file("credentials.json")) or {}
    return dict(data.get("cookies") or {})


def save_cookies(cookies):
    _write_json(_state_file("credentials.json"),
                {"cookies": cookies, "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")},
                private=True)


def write_qr_image(url, filename):
    """给人扫的二维码写进工作区，交回绝对路径；由 AI 用 show 给人看。"""
    path = os.path.abspath(filename)
    with open(path, "wb") as handle:
        handle.write(qr_png_bytes(url))
    return path


# ---------------------------------------------------------------------------
# 分区：列表缓存一天，把中文名、拼音、id 解析成唯一的子分区
# ---------------------------------------------------------------------------


def load_area_list(client, refresh=False):
    path = _cache_file("areas.json")
    if path and not refresh:
        cached = _read_json(path) or {}
        if cached.get("parents") and time.time() - cached.get("fetched_at", 0) < AREA_TTL:
            return cached["parents"]
    parents = client.area_list()
    if path:
        try:
            _write_json(path, {"fetched_at": int(time.time()), "parents": parents})
        except OSError:
            pass                      # 缓存写不下去不算错
    return parents


def flatten_areas(parents):
    areas = []
    for parent in parents:
        for area in parent.get("list") or []:
            areas.append({
                "id": int(area["id"]),
                "name": area.get("name") or "",
                "parent_id": int(parent["id"]),
                "parent_name": parent.get("name") or "",
                "pinyin": (area.get("pinyin") or "").lower(),
                "locked": str(area.get("lock_status") or "0") not in ("0", ""),
            })
    return areas


def area_label(area):
    return "%s / %s" % (area["parent_name"], area["name"])


def resolve_area(areas, spec):
    """数字 id、子分区名、拼音，或「父分区/子分区」。"""
    spec = str(spec).strip()
    if not spec:
        raise ToolError("usage", "分区不能是空的。", hint="用 command=areas 查有哪些分区。")

    if spec.isdigit():
        for area in areas:
            if area["id"] == int(spec):
                return area
        raise ToolError("usage", "没有 id 为 %s 的分区。" % spec,
                        hint="用 command=areas 加 search 查正确的分区 id。")

    parent_hint, needle = None, spec.lower()
    if "/" in spec:
        head, tail = spec.split("/", 1)
        parent_hint, needle = head.strip().lower(), tail.strip().lower()
    if not needle:
        raise ToolError("usage", "分区写法看不懂：%s" % spec,
                        hint="给 id，或「父分区/子分区」，或直接给子分区名。")

    pool = areas
    if parent_hint:
        pool = [a for a in pool if parent_hint in a["parent_name"].lower()]
        if not pool:
            raise ToolError("usage", "没有叫「%s」的父分区。" % spec.split("/", 1)[0].strip(),
                            hint="不带 search、parent 跑一次 command=areas 看父分区。")

    exact = [a for a in pool if needle in (a["name"].lower(), a["pinyin"])]
    matches = exact or [a for a in pool if needle in a["name"].lower() or needle in a["pinyin"]]
    if not matches:
        raise ToolError(
            "usage",
            ("父分区「%s」下面没有叫「%s」的分区。" % (pool[0]["parent_name"], needle)
             if parent_hint else "找不到叫「%s」的分区。" % spec),
            hint=("用 command=areas、parent=%s 看这个父分区下有哪些。" % pool[0]["parent_name"]
                  if parent_hint else "用 command=areas 加 search 查一下正确的名字。"))
    if len(matches) > 1:
        raise ToolError(
            "ambiguous_area", "「%s」匹配到 %d 个分区。" % (spec, len(matches)),
            hint="把候选摆给用户挑，挑定了用 id 重试；别自己猜。",
            detail={"candidates": [{"id": a["id"], "name": area_label(a)}
                                   for a in matches[:AREA_LIST_LIMIT]]})
    return matches[0]


def check_area_usable(area):
    if area["locked"]:
        raise ToolError("usage", "分区「%s」现在被 B 站锁着，不能用。" % area_label(area),
                        hint="换一个分区。")
    return area


class Session:
    def __init__(self, with_login=True):
        self.client = BiliClient(load_cookies() if with_login else None)
        self._nav = None
        self._room_id = None

    def require_login(self):
        if not self.client.logged_in():
            raise ToolError("auth", "not logged in：还没登录哔哩哔哩。", hint=NOT_LOGGED_IN_HINT)

    def nav(self):
        if self._nav is None:
            self.require_login()
            self._nav = self.client.nav()
        return self._nav

    def room_id(self):
        if self._room_id is None:
            self._room_id = self.client.room_id_by_uid(int(self.nav()["mid"]))
        if not self._room_id:
            raise ToolError("usage", "这个账号还没有直播间。",
                            hint="让用户先去 https://link.bilibili.com 开通直播间。")
        return self._room_id

    def flush(self):
        if self.client.cookies_changed and self.client.logged_in():
            save_cookies(self.client.cookies)


# ---------------------------------------------------------------------------
# login：两步。第一次出二维码就交回，第二次才查扫没扫。
# 一次调用里等着扫没有意义：调用没结束，AI 拿不到图，也就没法给人看。
# ---------------------------------------------------------------------------


def _new_login_qr(client):
    qrcode = client.qrcode_generate()
    url, key = qrcode["url"], qrcode["qrcode_key"]
    image = write_qr_image(url, LOGIN_QR)
    _write_json(_state_file("login-state.json"),
                {"qrcode_key": key, "url": url, "image": image, "created_at": int(time.time())},
                private=True)
    return {"status": "pending", "qr_image": image, "expires_in": QR_TTL}


def _left(state):
    return max(0, QR_TTL - (int(time.time()) - int(state.get("created_at", 0))))


def _poll_login(client, state, wait):
    """查扫码结果；二维码失效了交回 None。"""
    key = state.get("qrcode_key")
    if not key or _left(state) <= 0:
        return None
    deadline = time.time() + min(wait, _left(state))
    status = {}
    while True:
        status = client.qrcode_poll(key)
        code = status.get("code")
        if code == 0:
            # poll 交回的跳转地址里也带着 Cookie，兜一下
            query = urlparse.parse_qs(urlparse.urlparse(status.get("url") or "").query)
            for name in ("SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5", "sid"):
                if query.get(name):
                    client.cookies.setdefault(name, query[name][0])
            if not client.logged_in():
                raise ToolError("auth", "扫码成功但没拿到 Cookie。", hint="重新调 command=login。")
            save_cookies(client.cookies)          # 先落盘，别因为 nav 抖一下让人重扫
            _remove(_state_file("login-state.json"))
            _remove(state.get("image"))
            result = {"status": "success"}
            try:
                nav = client.nav()
                result["uid"], result["username"] = nav.get("mid"), nav.get("uname")
            except ToolError as exc:
                result["warning"] = "登录态已保存，但取用户信息失败：%s" % exc.message
            return result
        if code == 86038:                         # 二维码失效
            _remove(_state_file("login-state.json"))
            return None
        if time.time() >= deadline:
            break
        time.sleep(2)
    image = state.get("image")
    return {"status": "waiting", "qr_image": image if image and os.path.exists(image) else None,
            "detail": status.get("message") or "等待扫码", "expires_in": _left(state)}


def op_login(sess, args):
    client = sess.client
    if client.logged_in():
        try:
            nav = client.nav()
            return {"status": "already_logged_in", "uid": nav.get("mid"), "username": nav.get("uname")}
        except ToolError:
            client.cookies = {}                   # 登录态失效，重新登

    wait = args.get("wait") or 0
    if isinstance(wait, bool) or not isinstance(wait, int) or not 0 <= wait <= MAX_WAIT:
        raise ToolError("usage", "wait 要是 0 到 %d 的整数（秒）。" % MAX_WAIT)

    state = _read_json(_state_file("login-state.json"))
    if not state or _left(state) <= 0:
        return _new_login_qr(client)              # 新码没人扫过，不轮询，直接交出去
    result = _poll_login(client, state, wait)
    if result is not None:
        return result
    fresh = _new_login_qr(client)
    fresh["detail"] = "上一张二维码已失效，换了一张新的。"
    return fresh


def op_logout(sess, args):
    path = _state_file("credentials.json")
    existed = os.path.exists(path)
    _remove(path)
    state = _read_json(_state_file("login-state.json")) or {}
    _remove(state.get("image"))
    _remove(_state_file("login-state.json"))
    return {"logged_out": existed}


def op_status(sess, args):
    room_id = sess.room_id()
    info = sess.client.room_info(room_id)
    live_status = int(info.get("live_status", 0))
    return {
        "username": sess.nav().get("uname"),
        "uid": int(sess.nav()["mid"]),
        "room_id": room_id,
        "room_url": "https://live.bilibili.com/%s" % room_id,
        "title": info.get("title"),
        "area": "%s / %s" % (info.get("parent_area_name"), info.get("area_name")),
        "area_id": info.get("area_id"),
        "live_status": live_status,
        "live_status_text": LIVE_STATUS_TEXT.get(live_status, "未知"),
        "is_live": live_status == 1,
    }


def op_areas(sess, args):
    """公开接口，不用登录。"""
    search, parent = args.get("search"), args.get("parent")
    parents = load_area_list(sess.client, bool(args.get("refresh")))
    if not (search or parent):
        return {"scope": "parents", "count": len(parents),
                "parents": [{"id": int(p["id"]), "name": p.get("name") or "",
                             "area_count": len(p.get("list") or [])} for p in parents]}

    pool = flatten_areas(parents)
    if parent:
        key = str(parent).strip().lower()
        if key.isdigit():
            pool = [a for a in pool if a["parent_id"] == int(key)]
        else:
            pool = [a for a in pool if key in a["parent_name"].lower()]
        if not pool:
            raise ToolError("usage", "没有叫「%s」的父分区。" % parent,
                            hint="不带 search、parent 跑一次 command=areas 看父分区。")
    if search:
        key = str(search).strip().lower()
        pool = [a for a in pool if key in a["name"].lower() or key in a["pinyin"]
                or key in a["parent_name"].lower()]

    shown = pool[:AREA_LIST_LIMIT]
    result = {"scope": "areas", "search": search, "parent": parent, "count": len(pool),
              "areas": [{"id": a["id"], "name": a["name"], "full_name": area_label(a),
                         "pinyin": a["pinyin"], "locked": a["locked"]} for a in shown]}
    if len(pool) > len(shown):
        result["truncated"] = len(pool) - len(shown)
    return result


def _pick_area(sess, spec):
    return check_area_usable(resolve_area(flatten_areas(load_area_list(sess.client)), spec))


def op_update(sess, args):
    title, area = args.get("title"), args.get("area")
    if not title and not area:
        raise ToolError("usage", "没说要改什么。", hint="至少给 title 或 area。")
    room_id = sess.room_id()
    info = sess.client.room_info(room_id)
    previous = {"title": info.get("title"),
                "area": "%s / %s" % (info.get("parent_area_name"), info.get("area_name")),
                "area_id": info.get("area_id")}
    # 先解析分区再动直播间：名字写错不该留下改了一半的状态
    new_area = _pick_area(sess, area) if area else None
    sess.client.update_room(room_id, title=title, area_id=new_area["id"] if new_area else None)
    changed = (["title"] if title else []) + (["area"] if new_area else [])
    live = int(info.get("live_status", 0)) == 1
    result = {"room_id": room_id, "title": title or previous["title"],
              "area": area_label(new_area) if new_area else previous["area"],
              "area_id": new_area["id"] if new_area else previous["area_id"],
              "changed": changed, "previous": previous, "is_live": live}
    if live:
        result["warning"] = "直播间正在直播，改动已经对观众生效。"
    return result


def _collect_streams(data):
    """src/view/live-stream-settings.tsx handleStartStream 的整理办法。"""
    streams = []
    rtmp_count, srt_count = 1, 0
    rtmp = data.get("rtmp") or {}
    if rtmp.get("addr") and rtmp.get("code"):
        streams.append({"type": "rtmp-1", "protocol": "rtmp", "address": rtmp["addr"], "key": rtmp["code"]})
    for item in data.get("protocols") or []:
        if not (item.get("addr") and item.get("code")):
            continue
        if item.get("protocol") == "rtmp":
            rtmp_count += 1
            name = "rtmp-%d" % rtmp_count
        elif item.get("protocol") == "srt":
            srt_count += 1
            name = "srt-%d" % srt_count
        else:
            continue
        streams.append({"type": name, "protocol": item["protocol"], "address": item["addr"], "key": item["code"]})
    streams.sort(key=lambda s: s["type"])
    for stream in streams:
        address, key = stream["address"], stream["key"]
        stream["full_url"] = (address + key if key.startswith("?") or address.endswith("/")
                              else address + "/" + key)
    return streams


def op_start(sess, args):
    title, area = args.get("title"), args.get("area")
    room_id = sess.room_id()
    info = sess.client.room_info(room_id)
    was_live = int(info.get("live_status", 0)) == 1
    chosen = _pick_area(sess, area) if area else None
    if title or chosen:
        sess.client.update_room(room_id, title=title, area_id=chosen["id"] if chosen else None)
    if title:
        info["title"] = title
    if chosen:
        info.update(area_id=chosen["id"], area_name=chosen["name"], parent_area_name=chosen["parent_name"])

    area_v2 = info.get("area_id")
    if not area_v2:
        raise ToolError("usage", "直播间没有设分区，开不了播。", hint="带上 area 再 start，或先 update 设一个分区。")

    version = sess.client.live_version()
    response = sess.client.start_live(room_id, area_v2, version["curr_version"], version["build"])
    code = response.get("code")
    payload = response.get("data") or {}
    if code in (60024, 60043):
        url = (payload.get("qr") if code == 60024 else
               "https://www.bilibili.com/blackboard/live/face-auth-middle.html"
               "?source_event=400&mid=%s" % sess.nav()["mid"])
        kind, what = (("verify_required", "这次开播要扫码验证") if code == 60024
                      else ("face_auth_required", "这次开播要人脸认证"))
        raise ToolError(kind, what + "。", code=code,
                        hint="用 show 把 qr_image 给用户，用户用哔哩哔哩 App 扫码做完后，再调一次 start。",
                        detail={"qr_url": url, "qr_image": write_qr_image(url, VERIFY_QR) if url else None})
    if code != 0:
        raise ApiError(code, response.get("message") or "开播失败")

    streams = _collect_streams(payload)
    primary = next((s for s in streams if s["protocol"] == "rtmp"), None)
    result = {"room_id": room_id, "room_url": "https://live.bilibili.com/%s" % room_id,
              "title": info.get("title"),
              "area": "%s / %s" % (info.get("parent_area_name"), info.get("area_name")),
              "area_id": area_v2, "was_live": was_live, "streams": streams}
    if primary:
        result["server"], result["stream_key"] = primary["address"], primary["key"]
    return result


def op_stop(sess, args):
    room_id = sess.room_id()
    data = sess.client.stop_live(room_id)
    return {"room_id": room_id, "stopped": True, "status": data.get("status")}


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------

SHOW_HINT = "用 show 工具把这张图给用户看，让用户用哔哩哔哩 App 扫码并在手机上确认。"


def render(command, data):
    lines = []
    if command == "status":
        lines.append("%s · 直播间 %s · %s" % (data["live_status_text"], data["room_id"], data["username"]))
        lines.append("标题：%s" % data["title"])
        lines.append("分区：%s（id %s）" % (data["area"], data.get("area_id", "-")))
    elif command == "start":
        lines.append("%s · 直播间 %s · %s" % ("此前已在直播，这次只是重新取了推流码" if data["was_live"]
                                              else "已开播，直播间对外可见，粉丝收到了开播推送",
                                              data["room_id"], data["title"]))
        lines.append("分区：%s" % data["area"])
        lines.append("服务器：%s" % data.get("server", "-"))
        lines.append("推流码：%s" % data.get("stream_key", "-"))
        extra = len(data["streams"]) - (1 if data.get("server") else 0)
        if extra:
            lines.append("另有 %d 条备用线路，format=json 可见。" % extra)
        lines.append("服务器填 OBS 的「服务器」，推流码填「串流密钥」；推流码是敏感凭据，别发给别人。下播用 command=stop。")
    elif command == "update":
        changed = data["changed"]
        lines.append("已改%s · 直播间 %s" % ("、".join({"title": "标题", "area": "分区"}[c] for c in changed),
                                          data["room_id"]))
        lines.append("标题：%s%s" % (data["title"], "（原：%s）" % data["previous"]["title"]
                                    if "title" in changed else ""))
        lines.append("分区：%s（id %s%s）" % (data["area"], data["area_id"],
                                             "，原 %s" % data["previous"]["area"] if "area" in changed else ""))
        if data.get("warning"):
            lines.append(data["warning"])
    elif command == "areas":
        if data["scope"] == "parents":
            lines.append("父分区 %d 个（id 名称（子分区数））：" % data["count"])
            lines.extend("%s %s（%d）" % (p["id"], p["name"], p["area_count"]) for p in data["parents"])
            lines.append("用 parent 列一个父分区下的子分区，或用 search 搜。")
        else:
            scope = "、".join(filter(None, ["父分区 %s" % data["parent"] if data["parent"] else None,
                                           "关键词 %s" % data["search"] if data["search"] else None]))
            lines.append("匹配 %d 个子分区（%s）：" % (data["count"], scope))
            lines.extend("%s %s%s" % (a["id"], a["full_name"], "  [锁定]" if a["locked"] else "")
                         for a in data["areas"])
            if data.get("truncated"):
                lines.append("……还有 %d 个没列，缩小关键词。" % data["truncated"])
            if not data["count"]:
                lines.append("没匹配上，换个关键词，或不带 search、parent 看父分区。")
    elif command == "stop":
        lines.append("已下播 · 直播间 %s" % data["room_id"])
    elif command == "login":
        status = data["status"]
        if status in ("success", "already_logged_in"):
            lines.append("%s：%s（uid %s）" % ("登录成功" if status == "success" else "已经登录",
                                            data.get("username", "?"), data.get("uid", "?")))
        elif status == "pending":
            if data.get("detail"):
                lines.append(data["detail"])
            lines.append("待扫码，二维码：%s（%d 秒内有效）" % (data["qr_image"], data["expires_in"]))
            lines.append(SHOW_HINT + "用户说扫完了再调一次 command=login。")
        else:
            lines.append("%s，二维码还剩 %d 秒，没换%s。用户扫完再调一次 command=login。"
                         % (data.get("detail", "等待扫码"), data["expires_in"],
                            "：%s" % data["qr_image"] if data.get("qr_image") else ""))
        if data.get("warning"):
            lines.append(data["warning"])
    elif command == "logout":
        lines.append("已登出，本地登录态删了。" if data["logged_out"] else "本来就没登录。")
    return "\n".join(lines)


def render_error(error):
    lines = ["错误 [%s] %s" % (error.kind, error.message)]
    detail = error.detail or {}
    if detail.get("candidates"):
        lines.append("候选：")
        lines.extend("%s %s" % (c["id"], c["name"]) for c in detail["candidates"])
    if detail.get("qr_image"):
        lines.append("二维码：%s" % detail["qr_image"])
    if error.hint:
        lines.append(error.hint)
    return "\n".join(lines)


OPS = {"status": op_status, "areas": op_areas, "update": op_update, "start": op_start,
       "stop": op_stop, "login": op_login, "logout": op_logout}
NEEDS_LOGIN = {"status", "update", "start", "stop"}
# 改直播间、换登录的只给主人：私聊里谁都能发话。会话外面手动跑的没有这个变量，照常。
OWNER_ONLY = {"update", "start", "stop", "login", "logout"}


def main():
    try:
        args = json.load(sys.stdin)
    except ValueError as exc:
        args, error = {}, ToolError("usage", "参数不是 JSON：%s" % exc)
    else:
        error = None if isinstance(args, dict) else ToolError("usage", "参数要是一个 JSON 对象。")
        if not isinstance(args, dict):
            args = {}
    command = args.get("command")
    as_json = args.get("format") == "json"
    try:
        if error:
            raise error
        if command not in OPS:
            raise ToolError("usage", "command 要是其中之一：%s" % "、".join(OPS))
        if args.get("format") not in (None, "md", "json"):
            raise ToolError("usage", "format 只认 md、json。")
        if command in OWNER_ONLY and os.environ.get("MIYU_CALLER_OWNER", "1") != "1":
            raise ToolError("not_owner", "只有机器主人能用 %s：开播、下播、改标题、登录都会动主人的直播间。" % command,
                            hint="看状态（status）、查分区（areas）谁都能用。")
        sess = Session(with_login=command != "areas")
        if command in NEEDS_LOGIN:
            sess.require_login()
        data = OPS[command](sess, args)
        if command != "areas":
            sess.flush()
    except ToolError as exc:
        if as_json:
            print(json.dumps({"ok": False, "command": command, "error": exc.to_dict()},
                             ensure_ascii=False, indent=2))
        else:
            print(render_error(exc))
        sys.exit(1)
    if as_json:
        print(json.dumps({"ok": True, "command": command, "data": data}, ensure_ascii=False, indent=2))
    else:
        print(render(command, data))


if __name__ == "__main__":
    main()
