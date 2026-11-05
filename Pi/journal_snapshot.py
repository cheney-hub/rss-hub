#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把过去 24 小时的 journald 日志导成一份 gzip 快照，写到磁盘上持久保存。

由 journal-snapshot.timer 每天 00:05 触发一次。存在的理由：journald 仍是
volatile，实时日志只在 /run/log/journal（tmpfs），重启即全清；而 journald 自己按
MaxFileSec=1day 轮换出来的 .journal 是二进制文件，既不能直接读，也不能单独
取走。这里出的是纯文本 gz，zcat 就能看，也能 scp 下来。

快照目录 /var/log/journal-archive 在 SD 卡上：每天只写一次 gz 快照加一点目录
元数据，体积随当天日志量波动（1.18.0 样本约 10KB，1.19.0 实测 20,449 B），
实时日志仍全程留在内存。这份快照能跨重启查到，是「每天只写一次盘」换来的；
它同样只留 3 份，所以可回溯的上限仍是 3 天。目录由单元的
LogsDirectory=journal-archive 预先建好并赋权给 User=user；父目录 /var/log 是
root:root 755，普通用户建不了，脚本里的 os.makedirs(..., exist_ok=True) 在目录
已存在时只是空操作、不会改动已有目录的权限，本地要直接跑得自己先建好目录或用 sudo。

清理按**文件名里的日期**，不按 mtime：备份还原、整目录复制或改过系统时钟都可能
让 mtime 与文件名里的日期对不上，用 find -mtime +3 会留着该删的、删掉该留的。
只删符合 journal-YYYY-MM-DD.log.gz 这一形状的文件，同目录里别的东西一律不动。
"""
import datetime as dt
import gzip
import os
import re
import subprocess
import sys

SNAPSHOT_DIR = os.environ.get("JOURNAL_SNAPSHOT_DIR", "/var/log/journal-archive")
# 只留最近 3 份（今天 + 前 2 天）；第 4 份起自动清除
KEEP_DAYS = int(os.environ.get("JOURNAL_SNAPSHOT_KEEP_DAYS", "3"))
WINDOW = "-24h"
_NAME_RE = re.compile(r"^journal-(\d{4}-\d{2}-\d{2})\.log\.gz$")


def snapshot_name(day):
    """某一天的快照文件名；ISO 日期让字典序等于时间序。"""
    return "journal-%s.log.gz" % day.isoformat()


def parse_day(name):
    """从文件名取日期；不是我们生成的形状就返回 None（绝不误删）。"""
    m = _NAME_RE.match(name)
    if not m:
        return None
    try:
        return dt.date.fromisoformat(m.group(1))
    except ValueError:              # 2026-13-01 能匹配正则，却不是合法日期
        return None


def prune_names(names, today, keep_days=None):
    """返回该删的文件名：早于「最近 keep_days 天」的那些。

    keep_days=3 时只留今天、昨天、前天共 3 份，3 天前那一份就开始清。
    按日期算而不是按天数差，跨月跨年都不会错。
    """
    days = KEEP_DAYS if keep_days is None else keep_days
    cutoff = today - dt.timedelta(days=max(days, 1) - 1)
    out = []
    for n in names:
        d = parse_day(n)
        if d is not None and d < cutoff:
            out.append(n)
    return sorted(out)


def collect(window=WINDOW):
    """取过去 24 小时的日志纯文本；失败就抛，让 oneshot 明确失败。"""
    done = subprocess.run(
        ["journalctl", "--since", window, "--no-pager", "-o", "short-iso"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    return done.stdout


def write_snapshot(directory, day, data):
    """原子写一份 gz：先写 .tmp 再 os.replace，失败时清 .tmp 且不碰旧文件。

    原子替换是必要的：写到一半被读走会拿到截断的 gz；直接覆盖 final 还会在
    写失败时把上一份好快照也毁掉。
    """
    os.makedirs(directory, mode=0o700, exist_ok=True)
    final = os.path.join(directory, snapshot_name(day))
    tmp = final + ".tmp"
    try:
        with gzip.open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, final)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return final


def prune(directory, today, keep_days=None):
    """按文件名日期删掉过期快照，返回真的删掉的名字。"""
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    removed = []
    for n in prune_names(names, today, keep_days):
        try:
            os.unlink(os.path.join(directory, n))
            removed.append(n)
        except OSError:
            pass                    # 删不掉就下一天再试，不让清理失败砸掉本次快照
    return removed


def main():
    """导一份当天的快照，然后清理过期的；返回进程退出码。"""
    try:
        data = collect()
    except (OSError, subprocess.CalledProcessError) as e:
        print("journalctl 读取失败：%s" % e, file=sys.stderr)
        return 1
    today = dt.date.today()
    path = write_snapshot(SNAPSHOT_DIR, today, data)
    removed = prune(SNAPSHOT_DIR, today)
    print("快照 %s（原始 %d B / gz %d B），清理 %d 份：%s" % (
        path, len(data), os.path.getsize(path), len(removed),
        ", ".join(removed) or "无"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
