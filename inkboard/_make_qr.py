# -*- coding: utf-8 -*-
"""生成手机扫码入口二维码。优先用公网域名（任意网络都能开），
回退地址用法：手机/墨水屏连家里 WiFi 时 192.168.3.142 也可用。
显示端二维码内嵌只读令牌 ?rt=，墨水屏扫码即带令牌，无需交互。"""
import os
import qrcode

OUT = os.path.dirname(os.path.abspath(__file__))
PORT = 8765
LAN = "192.168.3.142"
PUB = "board.goodinsight.online"

# 读取只读令牌（无则显示端不加令牌）
tok = ""
tok_path = os.path.join(OUT, "READONLY_TOKEN.txt")
if os.path.isfile(tok_path):
    with open(tok_path, "r", encoding="utf-8-sig") as f:
        tok = f.read().replace("\ufeff", "").strip()
rt = ("?rt=" + tok) if tok else ""

targets = [
    ("qr_admin.png",       "https://%s/admin"        % PUB, "InkBoard 管理端（公网）"),
    ("qr_display.png",     "https://%s/%s"           % (PUB, rt), "InkBoard 显示端（公网，含令牌）"),
    ("qr_admin_lan.png",   "http://%s:%d/admin"      % (LAN, PORT), "InkBoard 管理端（家里WiFi）"),
    ("qr_display_lan.png", "http://%s:%d/%s"         % (LAN, PORT, rt), "InkBoard 显示端（家里WiFi，含令牌）"),
]

for fn, url, _title in targets:
    qr = qrcode.QRCode(box_size=12, border=3,
                       error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    img.save(os.path.join(OUT, fn))
    print(fn, "->", url)
