# bilibili_live.py 读新环境变量名的测试（审计 2026-11-18 #168）。
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


class EnvNames(Sandbox):
    # 审计 #168：核心 FH-6 以后只给新名字 MIYU_STATE_DIR / MIYU_CACHE_DIR，旧名 MIYU_SCRIPT_STATE_DIR /
    # MIYU_SCRIPT_CACHE_DIR 会删。脚本 state_dir()、_cache_file() 只读旧名：只设新名时登录报 no_state，
    # 分区列表不缓存。
    def test_state_dir_new_name(self):
        state = self.root / "state"
        state.mkdir()
        os.environ["MIYU_STATE_DIR"] = str(state)
        self.assertNotIn("MIYU_SCRIPT_STATE_DIR", os.environ)
        self.mod.BiliClient.qrcode_generate = lambda client: {
            "url": "https://passport.bilibili.com/h5-app/passport/login/scan?qrcode_key=k", "qrcode_key": "k"}
        code, out = self.call({"command": "login", "format": "json"})
        self.assertEqual(code, 0, out)
        saved = json.loads((state / "login-state.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["qrcode_key"], "k")
        self.assertEqual(self.mod.state_dir(), str(state))

    def test_cache_dir_new_name(self):
        cache = self.root / "cache"
        cache.mkdir()
        os.environ["MIYU_CACHE_DIR"] = str(cache)
        self.assertNotIn("MIYU_SCRIPT_CACHE_DIR", os.environ)
        parents = [{"id": 9, "name": "虚拟主播", "list": [
            {"id": "371", "name": "虚拟日常", "parent_id": "9", "parent_name": "虚拟主播", "pinyin": "xunirichang", "lock_status": "0"}]}]
        self.mod.BiliClient.area_list = lambda client: parents
        code, out = self.call({"command": "areas", "format": "json"})
        self.assertEqual(code, 0, out)
        self.assertTrue((cache / "areas.json").is_file(), f"MIYU_CACHE_DIR 里没有分区缓存：{sorted(os.listdir(cache))}")

        # 第二次应命中缓存，不再取分区列表。
        def no_area_list(client):
            raise AssertionError("分区列表缓存在 MIYU_CACHE_DIR 里，第二次不该再取")

        self.mod.BiliClient.area_list = no_area_list
        code, out = self.call({"command": "areas", "format": "json"})
        self.assertEqual(code, 0, out)
        self.assertEqual(json.loads(out)["data"]["count"], 1)


if __name__ == "__main__":
    unittest.main()
