"""Pi/journal_snapshot.py 的行为测试：一天一份、只留 3 份、按文件名清理。

三处容易改坏、改坏了短时间内又看不出来的地方：

1. **保留份数**：「只保存三天」是 3 份（今天 + 前 2 天），不是 4 份；边界差一天
   平时无感，跑上一个月才看出目录里多躺了一份。
2. **清理依据**：按文件名里的日期，不按 mtime，也绝不碰不认识的文件 ——
   快照目录在 /dev/shm，里面可能还有别的临时文件，误删没法恢复。
3. **原子写**：写到一半失败时，不能留下截断的 gz，更不能把上一份好快照覆盖掉。

全部落在临时目录里，不联网、不碰真实的 journald。
"""
import contextlib
import datetime as dt
import gzip
import io
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

_PI = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "Pi")
sys.path.insert(0, _PI)
import journal_snapshot as JS                                  # noqa: E402


class _BoomWriter:
    """写一半就失败的 gzip 替身：真的留下 .tmp，才能验证清理逻辑。

    只在内存里抛异常是不够的 —— 那样目录里压根没有半成品，「忘了删 .tmp」
    这个缺陷写法测了也不会红。
    """

    def __init__(self, path):
        self.path = path

    def __enter__(self):
        with open(self.path, "wb") as fh:
            fh.write(b"partial")
        return self

    def __exit__(self, *exc):
        return False

    def write(self, data):
        raise OSError("disk full")


class SnapshotTest(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _touch(self, name, data=b"x"):
        with open(os.path.join(self.dir, name), "wb") as fh:
            fh.write(data)

    def test_snapshot_name_is_iso_dated(self):
        self.assertEqual(JS.snapshot_name(dt.date(2026, 11, 6)),
                         "journal-2026-11-06.log.gz")

    def test_parse_day_accepts_only_our_shape(self):
        """形状不对 / 日期不合法的名字必须返回 None，否则清理会误删。"""
        self.assertEqual(JS.parse_day("journal-2026-11-06.log.gz"),
                         dt.date(2026, 11, 6))
        for bad in ("journal-2026-11-06.log.gz.tmp", "journal-2026-13-01.log.gz",
                    "notes.txt", "2026-11-06.log.gz", "journal-20261106.log.gz",
                    "journal-2026-11-06.log.gz.bak", ""):
            self.assertIsNone(JS.parse_day(bad), bad)

    def test_prune_names_keeps_exactly_three_days(self):
        today = dt.date(2026, 11, 6)
        names = [JS.snapshot_name(today - dt.timedelta(days=n)) for n in range(3)]
        self.assertEqual(JS.prune_names(names, today), [])

    def test_prune_names_deletes_the_fourth_day(self):
        today = dt.date(2026, 11, 6)
        names = [JS.snapshot_name(today - dt.timedelta(days=n)) for n in range(6)]
        self.assertEqual(JS.prune_names(names, today), sorted(names[3:]))

    def test_prune_names_handles_month_boundary(self):
        today = dt.date(2026, 3, 1)
        names = [JS.snapshot_name(d) for d in (
            dt.date(2026, 2, 24), dt.date(2026, 2, 26), dt.date(2026, 2, 27),
            dt.date(2026, 2, 28), dt.date(2026, 3, 1))]
        self.assertEqual(JS.prune_names(names, today), sorted(names[:2]))

    def test_prune_names_ignores_unrelated_files(self):
        today = dt.date(2026, 11, 6)
        old = JS.snapshot_name(today - dt.timedelta(days=30))
        names = [old, "notes.txt", "journal-2026-11-06.log.gz.tmp",
                 "journal-bad.log.gz", ".hidden"]
        self.assertEqual(JS.prune_names(names, today), [old])

    def test_prune_removes_expired_files_from_disk(self):
        today = dt.date(2026, 11, 6)
        for n in (0, 1, 2, 3, 9):
            self._touch(JS.snapshot_name(today - dt.timedelta(days=n)))
        self._touch("notes.txt")
        self._touch("journal-2026-11-06.log.gz.tmp")
        removed = JS.prune(self.dir, today)
        self.assertEqual(len(removed), 2)
        left = sorted(os.listdir(self.dir))
        self.assertEqual(
            sorted(n for n in left if n.startswith("journal-") and n.endswith(".gz")),
            sorted(JS.snapshot_name(today - dt.timedelta(days=n)) for n in (0, 1, 2)))
        self.assertIn("notes.txt", left)
        self.assertIn("journal-2026-11-06.log.gz.tmp", left)

    def test_write_snapshot_is_readable_without_tmp(self):
        path = JS.write_snapshot(self.dir, dt.date(2026, 11, 6), b"line one\nline two\n")
        self.assertEqual(os.path.basename(path), "journal-2026-11-06.log.gz")
        with gzip.open(path, "rb") as fh:
            self.assertEqual(fh.read(), b"line one\nline two\n")
        self.assertFalse(any(n.endswith(".tmp") for n in os.listdir(self.dir)))

    def test_write_snapshot_failure_keeps_previous_and_cleans_tmp(self):
        day = dt.date(2026, 11, 6)
        old = JS.write_snapshot(self.dir, day, b"GOOD-OLD-SNAPSHOT")
        with mock.patch.object(JS.gzip, "open",
                               side_effect=lambda path, mode: _BoomWriter(path)):
            with self.assertRaises(OSError):
                JS.write_snapshot(self.dir, day, b"NEW")
        with gzip.open(old, "rb") as fh:
            self.assertEqual(fh.read(), b"GOOD-OLD-SNAPSHOT")
        self.assertEqual(os.listdir(self.dir), [os.path.basename(old)])

    def test_collect_uses_24h_window(self):
        with mock.patch.object(JS.subprocess, "run") as run:
            run.return_value.stdout = b"a log line\n"
            self.assertEqual(JS.collect(), b"a log line\n")
        args, kwargs = run.call_args
        self.assertEqual(args[0], ["journalctl", "--since", "-24h",
                                   "--no-pager", "-o", "short-iso"])
        self.assertTrue(kwargs.get("check"))

    def test_main_writes_today_and_prunes_old(self):
        today = dt.date.today()
        self._touch(JS.snapshot_name(today - dt.timedelta(days=10)))
        with mock.patch.object(JS, "SNAPSHOT_DIR", self.dir), \
                mock.patch.object(JS, "collect", return_value=b"today's logs\n"), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(JS.main(), 0)
        self.assertEqual(os.listdir(self.dir), [JS.snapshot_name(today)])
        with gzip.open(os.path.join(self.dir, JS.snapshot_name(today)), "rb") as fh:
            self.assertEqual(fh.read(), b"today's logs\n")

    def test_main_returns_1_when_journalctl_fails(self):
        err = JS.subprocess.CalledProcessError(1, "journalctl")
        with mock.patch.object(JS, "SNAPSHOT_DIR", self.dir), \
                mock.patch.object(JS, "collect", side_effect=err), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(JS.main(), 1)
        self.assertEqual(os.listdir(self.dir), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
