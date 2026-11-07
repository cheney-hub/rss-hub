#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""持续维护「当日滚动池」ai-accum.json —— 与 AI 摘要进程解耦。

为什么独立于 ai_digest.py、挂在 rss-build.service 的 ExecStartPost 上：
  * ai-digest.timer 只在 07:00–21:00 每小时跑，21:00 之后到次日 07:00 整夜不跑；
    而 ai-accum.json 原本只在 ai_digest.py 运行时才写，于是夜间池子冻结、不含傍晚到
    现在的「当前条目」。reader.html 的「全天缓存」视图因此缺一大段。
  * rss-build 全天都在跑（白天每 5 分钟、夜间每小时），把「并入滚动池」这一步挂到
    它的 ExecStartPost，池子就随每一次构建刷新 —— 真正「当天见过的全部条目、且实时」。
  * AI 摘要进程（ai_digest.py）改为只从池子里读、挑重点，不再负责维护池子本身；
    其 main() 里仍保留一次合并作兜底，二者幂等、互不冲突。

去重：复用 ai_digest._merge_accum，按归一化标题去重（同一稿只留一份，不重复占候选位），
不做什么「排除已推荐条目」—— 用户定：全天不去重，同一稿可因重要性变化隔几小时再入选。
零点重置：_load_accum 在存储日期不是本地今天时返回空，_save_accum 落盘时写当天日期，
于是每天本地零点自动清空重攒。

只 import ai_digest 的纯函数（load_items / _merge_accum / _save_accum / 常量），
不联网、不调 CF；任何异常都以退出码 0 结束 —— rss-build 是主链路，池子维护是尽力而为，
绝不让积累步骤拖垮一次正常的 feed 构建。
"""
import os
import sys

# 与 ai_digest.py 同目录（/home/user/pi-pipeline），直接 import 复用合并逻辑，
# 保证「池子怎么合并/去重/重置」只有一份事实来源，不漂移。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ai_digest as A  # noqa: E402

# 对齐 rss-build.service 的 OUT_FILE：构建产物就写在这份 feed（tmpfs，不落盘）。
DEFAULT_FEED = "/dev/shm/rss-hub/feed.xml"


def main():
    feed = os.environ.get("FEED_XML", DEFAULT_FEED)
    accum_path = os.environ.get("AI_ACCUM_PATH", A.DEFAULT_ACCUM_PATH)
    try:
        items = A.load_items(feed)
        if not items:
            A._log("accum_update: feed 为空或不存在（%s），跳过本轮合并" % feed)
            return 0
        accum = A._merge_accum(A._load_accum(accum_path), items, A.ACCUM_CAP)
        A._save_accum(accum_path, accum)
        A._log("accum_update: 滚动池已刷新 -> %d 条（上限 %d，来源 %s）"
               % (len(accum), A.ACCUM_CAP, feed))
        return 0
    except Exception as e:  # 兜底：池子维护失败绝不影响 rss-build 主链路
        A._log("accum_update: 异常已忽略（%s: %s）" % (type(e).__name__, e))
        return 0


if __name__ == "__main__":
    sys.exit(main())
