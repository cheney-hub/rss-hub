#!/bin/sh
# Kindle 端看板客户端（越狱后放到 /mnt/us/board.sh，chmod +x）
#
# 定时从树莓派拉最新一张看板图 → 用 eips 画到电子墨水屏上。
# 前置：已越狱 + 装过 Hotfix；Kindle 与树莓派在同一个 wifi。
#
# 试跑：  ssh root@<kindle-ip> 'sh /mnt/us/board.sh'
# 常驻：  ssh root@<kindle-ip> 'nohup sh /mnt/us/board.sh >/dev/null 2>&1 &'
#         （Kindle 没有 systemd，重启后要重新拉一次，或挂到 KUAL 启动项）

URL="http://192.168.6.6:8082/kindle.png"   # 树莓派 wlan0 的地址
TMP="/mnt/us/board.png.new"
IMG="/mnt/us/board.png"
INTERVAL=300          # 秒。墨水屏别刷太勤，3~5 分钟一次够了
FULL_EVERY=10         # 每 N 次清一次残影

# 画图的工具。默认用系统自带的 eips；装了 FBInk 的话改成 "fbink -g" 效果更好（有抖动和缩放）
DRAW="eips -g"

# 不让屏保把画面盖掉（这一步失败不影响出图，只是屏保可能来抢屏）
lipc-set-prop com.lab126.powerd preventScreenSaver 1 2>/dev/null

n=0
while true; do
    if wget -q -T 20 -O "$TMP" "$URL" && [ -s "$TMP" ]; then
        mv "$TMP" "$IMG"
        # eips -c 会整屏闪一次并清屏，紧接着重画 —— 这是清残影最稳的做法
        if [ $((n % FULL_EVERY)) -eq 0 ]; then
            eips -c
        fi
        $DRAW "$IMG"
        n=$((n + 1))
    else
        rm -f "$TMP"
        logger "board: fetch failed, keep last image"
    fi
    sleep "$INTERVAL"
done
