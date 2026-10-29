#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把头条推给 Dot Quote/0 的「文本 API」内容，一屏显示 4 条。

为什么要这个脚本：
  Dot 的 RSS 卡片在设备上只有一行大字号标题（296px 宽约 13～14 个全角字），
  标题一长就被云端硬切，一屏只能看一条。
  而文本 API 的版面是「标题 1 行 + 正文 3 行 + 右下角落款」，字号 8～48、行高 0.8～3 可调，
  用 13～14px 像素字体时每行能放 19～20 个汉字 —— 一屏 4 条，信息量是 RSS 卡片的 4 倍。

用法：
  DOT_API_KEY=dot_app_xxx DOT_DEVICE_ID=设备序列号 python3 push_text.py
  DOT_API_KEY=... DOT_DEVICE_ID=... python3 push_text.py --dry-run   # 只看请求体，不发

前置条件（在 Dot App 里做一次）：
  1. App → 设置 → 开放平台 / API 密钥，创建密钥（只需要「文本」权限）；
  2. App → 内容工坊 → 添加「文本 API」内容，并放进设备的「循环内容」列表；
  3. 在 App 里能看到设备序列号（设备 ID）。

环境变量：
  DOT_API_KEY     必填，dot_app_ 开头；缺失则直接跳过（仓库里不放任何密钥）
  DOT_DEVICE_ID   必填，设备序列号
  DOT_TASK_KEY    可选，循环列表里若有多个文本 API 内容，用它指定其中一个
  DOT_TASK_TYPE   可选，loop（默认）/ fixed
  DOT_REFRESH_NOW 可选，1（默认）= 立刻在设备上显示；0 = 只存到云端等设备来取
  DOT_FEED        可选，读取的 feed 文件，默认 ./docs/feed.xml
  DOT_TITLE_WIDTH 可选，标题行宽度（全角字），默认 13
  DOT_BODY_WIDTH  可选，正文每行宽度（全角字），默认 19
  DOT_LINES       可选，正文行数，默认 3（文本 API 版面固定 3 行）
  DOT_TITLE_FONT  可选，标题字号，默认 18
  DOT_BODY_FONT   可选，正文字号，默认 13
  DOT_SIGN_FONT   可选，落款字号，默认 10
  DOT_API_BASE    可选，默认 https://dot.mindreset.tech/api/authV2/open
"""
import json
import os
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from build_feed import fit_title, plain_text  # noqa: E402  复用同一套截断/清洗规则

API_KEY = os.environ.get("DOT_API_KEY", "").strip()
DEVICE_ID = os.environ.get("DOT_DEVICE_ID", "").strip()
TASK_KEY = os.environ.get("DOT_TASK_KEY", "").strip()
TASK_TYPE = os.environ.get("DOT_TASK_TYPE", "loop").strip() or "loop"
REFRESH_NOW = os.environ.get("DOT_REFRESH_NOW", "1").strip() != "0"
FEED_FILE = os.environ.get("DOT_FEED", os.path.join(BASE_DIR, "docs", "feed.xml"))
TITLE_WIDTH = float(os.environ.get("DOT_TITLE_WIDTH", "13"))
BODY_WIDTH = float(os.environ.get("DOT_BODY_WIDTH", "19"))
LINES = max(1, int(os.environ.get("DOT_LINES", "3")))
TITLE_FONT = int(os.environ.get("DOT_TITLE_FONT", "18"))
BODY_FONT = int(os.environ.get("DOT_BODY_FONT", "13"))
SIGN_FONT = int(os.environ.get("DOT_SIGN_FONT", "10"))
API_BASE = os.environ.get("DOT_API_BASE", "https://dot.mindreset.tech/api/authV2/open")
# 设备与源站都在国内，用北京时间做落款更直观
CST = timezone(timedelta(hours=8))


def load_titles(path=FEED_FILE):
    """按 feed 里的顺序取标题（feed 已按发布时间倒序，且已跨源去重）。"""
    try:
        root = ET.parse(path).getroot()
    except Exception as err:
        print("读取 %s 失败：%s" % (path, err))
        return []
    out = []
    for it in root.iter("item"):
        t = (it.findtext("title") or "").strip()
        if t:
            out.append(t)
    return out


def compose(titles):
    """标题 1 行 + 正文 N 行 + 落款，全部压到设备放得下的宽度。"""
    if not titles:
        return None
    head = fit_title(titles[0], TITLE_WIDTH)
    body = [fit_title(t, BODY_WIDTH) for t in titles[1:1 + LINES]]
    now = datetime.now(CST)
    return {
        "title": head,
        "message": "\n".join(body) if body else "",
        "signature": now.strftime("%m-%d %H:%M"),
        "styles": {
            "title": {"fontSize": TITLE_FONT, "fontWeight": 600},
            "message": {"fontSize": BODY_FONT, "lineHeight": 1.35},
            "signature": {"fontSize": SIGN_FONT},
        },
    }


def build_payload(composed):
    payload = {
        "taskType": TASK_TYPE,
        "refreshNow": REFRESH_NOW,
        "title": plain_text(composed["title"]),
        "message": plain_text(composed["message"]),   # 换行会保留，只清装饰字符
        "signature": composed["signature"],
        "styles": composed["styles"],
    }
    if TASK_KEY:
        payload["taskKey"] = TASK_KEY
    return payload


def send(payload):
    url = "%s/device/%s/text" % (API_BASE.rstrip("/"), DEVICE_ID)
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "Authorization": "Bearer %s" % API_KEY,
        "Content-Type": "application/json",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as err:
        return err.code, err.read().decode("utf-8", "replace")
    except Exception as err:
        return 0, str(err)


def main():
    dry = "--dry-run" in sys.argv
    titles = load_titles()
    if not titles:
        print("feed 里没有条目，跳过推送")
        return 1
    payload = build_payload(compose(titles))

    if dry:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    if not API_KEY or not DEVICE_ID:
        print("未配置 DOT_API_KEY / DOT_DEVICE_ID，跳过文本 API 推送（RSS 卡片照常工作）")
        return 0

    status, body = send(payload)
    if 200 <= status < 300:
        print("已推送文本卡片：%s | %s" % (payload["title"],
                                       payload["message"].replace("\n", " / ")))
        return 0
    print("推送失败 HTTP %s：%s" % (status, body[:300]))
    return 1


if __name__ == "__main__":
    sys.exit(main())
