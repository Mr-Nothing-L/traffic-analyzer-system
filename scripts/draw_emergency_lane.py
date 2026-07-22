#!/usr/bin/env python3
"""在高速公路监控帧上画出应急车道区域多边形。

用法:
    python scripts/draw_emergency_lane.py <输入图片> [-o 输出图片] [-p 摄像头配置]

多边形坐标按 1920x1080 标定, 运行时按实际分辨率自动缩放。
"""
import argparse

import cv2
import numpy as np

# 以 1920x1080 为基准的多边形, 每个摄像头一组配置
BASE_W, BASE_H = 1920, 1080
PROFILES = {
    # 苏研 mask 摄像头 (01-02-04-06-11_Event_2048 帧)
    "default": {
        # 内侧白线底端 -> 内侧中部 -> 内侧远端 -> 外侧远端 -> 外侧中部 -> 外侧底端
        "left": [(330, 1080), (560, 545), (860, 70), (880, 70), (350, 545), (60, 1080)],
        "right": [(1860, 1080), (1440, 510), (1010, 70), (1055, 70), (1540, 510), (1920, 870), (1920, 1080)],
    },
    # 北京-G3京台高速 K13+200 进京-2 (01-02_Event_129_1755579215119 帧)
    # 该机位进京主线应急车道被 G230 出口打断: 画面右侧为出口车道+导流区+匝道, 无连续应急车道;
    # 仅标注对向(出京)车行道的外侧应急车道
    "g3_k13": {
        "left": [(0, 295), (450, 288), (900, 278), (900, 260), (450, 268), (0, 275)],
    },
}
COLORS = {"left": (0, 165, 255), "right": (0, 0, 255)}  # BGR: 橙 / 红


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("image", help="输入帧图片路径")
    ap.add_argument("-o", "--output", default=None, help="输出图片路径 (默认 <输入>_emergency_lane.jpg)")
    ap.add_argument("-p", "--profile", default="default", choices=sorted(PROFILES), help="摄像头配置")
    args = ap.parse_args()

    img = cv2.imread(args.image)
    if img is None:
        raise SystemExit(f"无法读取图片: {args.image}")
    h, w = img.shape[:2]
    sx, sy = w / BASE_W, h / BASE_H

    overlay = img.copy()
    for name, pts in PROFILES[args.profile].items():
        poly = np.array([(int(x * sx), int(y * sy)) for x, y in pts], dtype=np.int32)
        cv2.fillPoly(overlay, [poly], COLORS[name])
        cv2.polylines(img, [poly], isClosed=True, color=COLORS[name], thickness=3)
        label_pos = tuple(poly[0] + np.array([10, -40]))
        cv2.putText(img, f"emergency_lane_{name}", label_pos,
                    cv2.FONT_HERSHEY_SIMPLEX, 1.0, COLORS[name], 2)
    cv2.addWeighted(overlay, 0.35, img, 0.65, 0, img)

    out = args.output or args.image.rsplit(".", 1)[0] + "_emergency_lane.jpg"
    cv2.imwrite(out, img)
    print(f"已保存: {out}")


if __name__ == "__main__":
    main()
