#!/usr/bin/env python3
"""复测 wemm-embedding-9b 多模态输入(部署方宣称已全模态上线)。"""
import base64
import json
import os
import urllib.request
import urllib.error

BASE = "http://10.103.0.6:8000/v1/embeddings"
KEY = "sk-traffic-27b-2026"
IMG = "演示区/.agent/tracks/01-02_Event_129_1755579215119_1/20260828_115634/colored_overlay.jpg"
VIDEO = os.path.abspath("演示区/02-08_Event_257_1754288341555_1.mp4")


def probe(name, payload, quiet_ok=False):
    req = urllib.request.Request(
        BASE,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {KEY}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            d = json.load(r)
            emb = d["data"][0]["embedding"]
            if not quiet_ok:
                norm = sum(x * x for x in emb) ** 0.5
                print(f"[OK] {name}: dim={len(emb)} norm={norm:.4f}")
            return emb
    except urllib.error.HTTPError as e:
        print(f"[{e.code}] {name}: {e.read()[:300].decode(errors='replace')}")
    except Exception as e:  # noqa: BLE001
        print(f"[ERR] {name}: {type(e).__name__} {e}")
    return None


def cos(a, b):
    return sum(x * y for x, y in zip(a, b))


img_b64 = base64.b64encode(open(IMG, "rb").read()).decode()
img_url = f"data:image/jpeg;base64,{img_b64}"
vid_b64 = base64.b64encode(open(VIDEO, "rb").read()).decode()
vid_dataurl = f"data:video/mp4;base64,{vid_b64}"
print(f"video size: {os.path.getsize(VIDEO)/1e6:.1f}MB, b64 {len(vid_b64)/1e6:.1f}MB")

# 1) 图像 dataURL
v_img = probe("image-dataurl", {
    "model": "wemm-embedding-9b",
    "input": [{"type": "image_url", "image_url": {"url": img_url}}]})

# 2) 图文交错
v_imgtext = probe("image+text", {
    "model": "wemm-embedding-9b",
    "input": [
        {"type": "image_url", "image_url": {"url": img_url}},
        {"type": "text", "text": "高速公路监控画面,应急车道上的车辆"},
    ]})

# 3) 视频 file://(本机路径,服务端多半不可见)
probe("video-file-url", {
    "model": "wemm-embedding-9b",
    "input": [{"type": "video_url", "video_url": {"url": f"file://{VIDEO}"}}]})

# 4) 视频 dataURL
v_vid = probe("video-dataurl", {
    "model": "wemm-embedding-9b",
    "input": [{"type": "video_url", "video_url": {"url": vid_dataurl}}]})

# 5) 语义 sanity:视频向量 vs 相关/无关文本
if v_vid:
    t_rel = probe("text-related", {"model": "wemm-embedding-9b",
        "input": "高速公路应急车道上橙色养护车向后倒车"}, quiet_ok=True)
    t_unrel = probe("text-unrelated", {"model": "wemm-embedding-9b",
        "input": "办公室里的人在开会讨论季度报表"}, quiet_ok=True)
    if t_rel and t_unrel:
        print(f"cos(视频, '养护车倒车')   = {cos(v_vid, t_rel):.4f}")
        print(f"cos(视频, '办公室开会')   = {cos(v_vid, t_unrel):.4f}")
if v_img and v_imgtext:
    print(f"cos(图像, 图文交错)        = {cos(v_img, v_imgtext):.4f}")
