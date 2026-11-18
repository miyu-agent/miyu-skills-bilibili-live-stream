# bilibili_live.py 的测试（审计 2026-11-18）。
#
# 跑法（在仓库根）：
#   python3 -I -B -m unittest discover -s .github/tests
# （不加 -t .：.github 不是合法的包名，加了 unittest 会报 Start directory is not importable。）
#
# 只用标准库；不联网：BiliClient.call（所有接口的唯一出口）换成桩，断网也能跑。
# HOME、XDG_*、MIYU_* 都指到临时目录，工作目录也换到临时目录（二维码 PNG 写进当前目录）。
# 放在 .github/tests/ 而不是 tests/：打包会跳过 .github，测试不会进包。

import importlib.util
import io
import json
import os
import pathlib
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

SCRIPT = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "bilibili_live.py"


def load():
    sys.dont_write_bytecode = True
    spec = importlib.util.spec_from_file_location("bilibili_live_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Sandbox(unittest.TestCase):
    """每条测试一个临时目录，环境里去掉所有 MIYU_*，网络出口先换成直接失败的桩。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = pathlib.Path(self.tmp.name)
        env = {k: v for k, v in os.environ.items() if not k.startswith(("MIYU_", "XDG_"))}
        env.update(HOME=str(root / "home"), XDG_CONFIG_HOME=str(root / "cfg"),
                   XDG_CACHE_HOME=str(root / "xdg-cache"), XDG_STATE_HOME=str(root / "xdg-state"),
                   XDG_DATA_HOME=str(root / "xdg-data"))
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        work = root / "work"
        work.mkdir()
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(work)
        self.root = root
        self.work = work
        self.mod = load()
        self.calls = []

        def no_network(client, base, path, *args, **kwargs):
            self.calls.append(base + path)
            raise AssertionError(f"测试里不该联网：{base + path}")

        self.mod.BiliClient.call = no_network

    def call(self, args):
        """照 main() 跑一次：stdin 喂 JSON，返回 (退出码, 输出)。"""
        out = io.StringIO()
        code = 0
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(args))), redirect_stdout(out):
            try:
                self.mod.main()
            except SystemExit as exc:
                code = exc.code or 0
        return code, out.getvalue()


LONG_QR = "https://www.bilibili.com/" + "a" * 300   # 325 字节，超过版本 10-L 的容量（271 字节）


class QrTooLong(Sandbox):
    # 审计 #179：QRError 直接继承 Exception，不是 ToolError；main() 只接 ToolError。
    # 二维码内容超过版本 10-L 的容量时，QRError 越过 main() 的错误处理，打 traceback，format=json 也拿不到 JSON。
    def test_qr_error_is_tool_error(self):
        try:
            self.mod.qr_png_bytes(LONG_QR)
        except Exception as exc:  # noqa: BLE001 - 要看抛的是哪一类
            error = exc
        else:
            self.fail("超长内容应报错")
        self.assertIsInstance(error, self.mod.ToolError,
                              f"抛的是 {type(error).__name__}（{error}），main() 接不住")
        self.assertIn("过长", str(error))

    # 防改坏（现在就绿）：容量以内的照常出 PNG。
    def test_short_qr_still_works(self):
        self.assertTrue(self.mod.qr_png_bytes("https://www.bilibili.com/" + "a" * 200).startswith(b"\x89PNG"))

    def test_start_verify_with_long_qr_gives_json_error(self):
        # 开播时接口回 60024（要扫码验证），data.qr 超长：op_start 在 raise ToolError 的参数里调
        # write_qr_image 生成验证二维码，QRError 顶掉 ToolError。修好后应照常交 {"ok": false, ...}。
        state = self.root / "state"
        state.mkdir()
        # 新旧两个名字都给（核心现在就是这样给的），不受 #168 影响。
        os.environ["MIYU_STATE_DIR"] = os.environ["MIYU_SCRIPT_STATE_DIR"] = str(state)
        (state / "credentials.json").write_text(
            json.dumps({"cookies": {"SESSDATA": "fake", "bili_jct": "fake", "DedeUserID": "1"}}), encoding="utf-8")
        C = self.mod.BiliClient
        C.nav = lambda client: {"mid": 1, "uname": "测试"}
        C.room_id_by_uid = lambda client, uid: 123
        C.room_info = lambda client, room_id: {"live_status": 0, "area_id": 371, "title": "t",
                                               "parent_area_name": "虚拟主播", "area_name": "虚拟日常"}
        C.live_version = lambda client: {"curr_version": "7.0.0", "build": 1}
        C.start_live = lambda client, room_id, area_v2, version, build: {
            "code": 60024, "message": "需要验证", "data": {"qr": LONG_QR}}
        try:
            code, out = self.call({"command": "start", "format": "json"})
        except Exception as exc:  # noqa: BLE001 - 除了 SystemExit，main() 不该漏出任何异常
            self.fail(f"main() 漏出 {type(exc).__name__}: {exc}（应交 JSON 错误）")
        self.assertEqual(code, 1, out)
        result = json.loads(out)
        self.assertIs(result["ok"], False)
        self.assertEqual(result["command"], "start")
        self.assertIn("error", result)


if __name__ == "__main__":
    unittest.main()
