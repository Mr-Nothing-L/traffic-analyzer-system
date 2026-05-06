#!/usr/bin/env python3
"""
车辆轨迹片段合并算法
解决同一辆车因遮挡/检测丢失导致的ID切换问题
"""

import json
import os
import cv2
import numpy as np
from collections import defaultdict

# ============== 配置 ==============
VEHICLES_JSON = "Agentworkflow/cache/vehicles.json"
VIDEO_INFO_JSON = "Agentworkflow/cache/video_info.json"
VIDEO_PATH = "08_ch1_20260401_142812_6.mp4"
OUTPUT_JSON = "Agentworkflow/cache/vehicles_merged.json"

# 合并阈值
MAX_TIME_GAP_SEC = 4.0       # 最大时间间隔（秒）
MAX_TIME_GAP_FRAMES = None   # 将由fps计算
MAX_SPATIAL_DIST = 150       # 最大空间距离（像素）
MIN_APPEARANCE_SIM = 0.6     # 最小外观相似度（巴氏距离）

# ============== 辅助函数 ==============

def get_box_center(box):
    """获取检测框中心点 (cx, cy)"""
    # box格式: [frame, x1, y1, w, h, cx, cy, area]
    return box[5], box[6]

def get_box_bbox(box):
    """获取检测框左上角和宽高"""
    return box[1], box[2], box[3], box[4]

def compute_dy(boxes, at_end=True, window=3):
    """
    计算轨迹末端或起始端的y方向变化率dy
    at_end=True: 计算末端dy（最后window帧的平均变化）
    at_end=False: 计算起始端dy（前window帧的平均变化）
    返回dy的平均值
    """
    if len(boxes) < 2:
        return 0.0
    
    if at_end:
        # 取最后window+1个box计算dy
        sel = boxes[-min(window+1, len(boxes)):]
        dys = [sel[i+1][6] - sel[i][6] for i in range(len(sel)-1)]
    else:
        # 取前window+1个box计算dy
        sel = boxes[:min(window+1, len(boxes))]
        dys = [sel[i+1][6] - sel[i][6] for i in range(len(sel)-1)]
    
    return np.mean(dys) if dys else 0.0

def extract_vehicle_appearance(video_path, frame_idx, x1, y1, w, h):
    """
    从视频指定帧提取车辆区域的HSV直方图
    返回归一化的HSV直方图特征向量
    """
    cap = cv2.VideoCapture(video_path)
    cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
    ret, frame = cap.read()
    cap.release()
    
    if not ret or frame is None:
        return None
    
    # 裁剪车辆区域（加边界检查）
    x1 = max(0, int(x1))
    y1 = max(0, int(y1))
    x2 = min(frame.shape[1], int(x1 + w))
    y2 = min(frame.shape[0], int(y1 + h))
    
    if x2 <= x1 or y2 <= y1:
        return None
    
    roi = frame[y1:y2, x1:x2]
    if roi.size == 0:
        return None
    
    # 转换到HSV
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    
    # 计算HSV直方图 (H: 0-180, S: 0-255, V: 0-255)
    h_bins = 16
    s_bins = 8
    v_bins = 8
    hist = cv2.calcHist([hsv], [0, 1, 2], None, [h_bins, s_bins, v_bins], 
                        [0, 180, 0, 256, 0, 256])
    cv2.normalize(hist, hist, alpha=1.0, beta=0.0, norm_type=cv2.NORM_L1)
    
    return hist.flatten()

def get_track_appearance(video_path, track, num_samples=3):
    """
    对一条轨迹采样若干帧，提取外观特征并取平均
    """
    boxes = track["boxes"]
    n = len(boxes)
    
    # 采样策略：起始、中间、结束
    indices = [0, n // 2, n - 1]
    indices = list(dict.fromkeys(indices))  # 去重并保持顺序
    indices = [i for i in indices if 0 <= i < n]
    
    features = []
    for idx in indices[:num_samples]:
        box = boxes[idx]
        frame_idx = box[0]
        x1, y1, w, h = get_box_bbox(box)
        feat = extract_vehicle_appearance(video_path, frame_idx, x1, y1, w, h)
        if feat is not None:
            features.append(feat)
    
    if not features:
        return None
    
    # 平均多个采样帧的特征
    return np.mean(features, axis=0)

def compute_appearance_similarity(feat_a, feat_b):
    """
    计算两个外观特征的巴氏距离相似度
    cv2.compareHist 使用 BHATTACHARYYA 返回距离（越小越相似）
    我们转换为相似度：sim = 1 - distance
    """
    if feat_a is None or feat_b is None:
        return 0.0
    
    feat_a = feat_a.astype(np.float32).reshape(-1, 1)
    feat_b = feat_b.astype(np.float32).reshape(-1, 1)
    
    # BHATTACHARYYA距离范围[0, 1]，0表示完全相同
    dist = cv2.compareHist(feat_a, feat_b, cv2.HISTCMP_BHATTACHARYYA)
    
    # 转换为相似度：1 - dist
    # 但注意：用户要求的是距离 > 0.6（其实应该是相似度>0.6）
    # cv2.compareHist BHATTACHARYYA 返回距离，越小越相似
    # 用户说"HSV颜色直方图巴氏距离 > 0.6"，这可能是笔误，应该是相似度
    # 或者用户想表达的是相关系数/交集类的compareHist方法
    
    # 实际上，cv2.HISTCMP_CORREL 返回相关系数（越大越相似，范围[-1,1]）
    # 而 BHATTACHARYYA 返回距离（越小越相似）
    
    # 重新考虑：如果用户说"巴氏距离 > 0.6"，按字面意思距离要大于0.6
    # 但这意味着越不相似越合并，显然不合理。
    # 这里我认为用户的真实意图是相似度 > 0.6
    
    # 使用相关系数作为相似度度量
    sim = 1.0 - dist  # 近似转换
    return sim

def can_merge(track_a, track_b, fps, appearances, spatial_thresh=150, 
              time_thresh_frames=60, sim_thresh=0.6):
    """
    判断两条轨迹是否可以合并
    假设 track_a 在时间上早于或重叠于 track_b
    """
    boxes_a = track_a["boxes"]
    boxes_b = track_b["boxes"]
    
    enter_a = boxes_a[0][0]
    exit_a = boxes_a[-1][0]
    enter_b = boxes_b[0][0]
    exit_b = boxes_b[-1][0]
    
    # 1. 时间连续性
    # 如果A和B时间重叠，则不合并（已经是同时存在的不同检测）
    if not (exit_a < enter_b or exit_b < enter_a):
        # 时间重叠，不合并
        return False, "time_overlap"
    
    # 确保A在B之前
    if exit_b < enter_a:
        # B在A之前，交换检查
        return can_merge(track_b, track_a, fps, appearances, spatial_thresh, 
                        time_thresh_frames, sim_thresh)
    
    # A在B之前的情况
    gap_frames = enter_b - exit_a
    if gap_frames > time_thresh_frames:
        return False, f"time_gap_too_large({gap_frames/fps:.2f}s)"
    
    # 2. 空间连续性：A的出场位置与B的入场位置
    end_pos_a = get_box_center(boxes_a[-1])
    start_pos_b = get_box_center(boxes_b[0])
    dist = np.sqrt((end_pos_a[0] - start_pos_b[0])**2 + 
                   (end_pos_a[1] - start_pos_b[1])**2)
    if dist > spatial_thresh:
        return False, f"spatial_dist_too_large({dist:.1f}px)"
    
    # 3. 道路一致性
    road_a = track_a.get("road_id", -1)
    road_b = track_b.get("road_id", -1)
    if road_a != road_b:
        return False, "road_mismatch"
    
    # 4. 外观相似性
    feat_a = appearances.get(id(track_a))
    feat_b = appearances.get(id(track_b))
    if feat_a is not None and feat_b is not None:
        sim = compute_appearance_similarity(feat_a, feat_b)
        if sim < sim_thresh:
            return False, f"appearance_sim_too_low({sim:.3f})"
    
    # 5. 运动方向一致性
    dy_a = compute_dy(boxes_a, at_end=True)
    dy_b = compute_dy(boxes_b, at_end=False)
    
    # 检查符号是否相同（或其中一个接近0）
    if abs(dy_a) > 1.0 and abs(dy_b) > 1.0:
        if (dy_a > 0 and dy_b < 0) or (dy_a < 0 and dy_b > 0):
            return False, f"direction_mismatch(dy_a={dy_a:.2f}, dy_b={dy_b:.2f})"
    
    return True, "ok"

def merge_two_tracks(track_a, track_b):
    """
    合并两条轨迹（track_a在时间上早于track_b）
    """
    boxes_a = track_a["boxes"]
    boxes_b = track_b["boxes"]
    
    # 按帧号排序拼接
    merged_boxes = sorted(boxes_a + boxes_b, key=lambda b: b[0])
    
    # 去重（如果同一帧有两个框，保留面积较大的）
    frame_to_boxes = defaultdict(list)
    for b in merged_boxes:
        frame_to_boxes[b[0]].append(b)
    
    dedup_boxes = []
    for frame in sorted(frame_to_boxes.keys()):
        boxes = frame_to_boxes[frame]
        if len(boxes) == 1:
            dedup_boxes.append(boxes[0])
        else:
            # 保留面积最大的
            best = max(boxes, key=lambda b: b[7])  # area at index 7
            dedup_boxes.append(best)
    
    # 构建合并后的轨迹
    merged = {
        "boxes": dedup_boxes,
        "enter_frame": dedup_boxes[0][0],
        "exit_frame": dedup_boxes[-1][0],
        "last_seen": dedup_boxes[-1][0],
        "active": track_b.get("active", False),
        "road_id": track_a["road_id"],
        "merged_from": [],
        "total_displacement": 0.0,
        "lifetime_frames": len(dedup_boxes),
        "lifetime_sec": len(dedup_boxes) / 15.0,  # 后面会更新为正确fps
    }
    
    # 保留原始片段信息
    fragments = []
    if "merged_from" in track_a and track_a["merged_from"]:
        fragments.extend(track_a["merged_from"])
    else:
        fragments.append({
            "original_id": track_a.get("original_id", "unknown"),
            "frame_range": [boxes_a[0][0], boxes_a[-1][0]],
            "num_boxes": len(boxes_a)
        })
    
    if "merged_from" in track_b and track_b["merged_from"]:
        fragments.extend(track_b["merged_from"])
    else:
        fragments.append({
            "original_id": track_b.get("original_id", "unknown"),
            "frame_range": [boxes_b[0][0], boxes_b[-1][0]],
            "num_boxes": len(boxes_b)
        })
    
    merged["merged_from"] = fragments
    
    # 计算总位移
    start_center = get_box_center(dedup_boxes[0])
    end_center = get_box_center(dedup_boxes[-1])
    merged["total_displacement"] = np.sqrt(
        (end_center[0] - start_center[0])**2 + 
        (end_center[1] - start_center[1])**2
    )
    
    return merged

def merge_tracks(tracks_dict, fps, video_path):
    """
    主合并算法
    """
    time_thresh_frames = int(MAX_TIME_GAP_SEC * fps)
    
    # 1. 提取所有轨迹的外观特征
    print("[1/4] 提取车辆外观特征 (HSV直方图)...")
    appearances = {}
    track_list = []
    for vid, track in tracks_dict.items():
        track["original_id"] = vid
        track_list.append(track)
        feat = get_track_appearance(video_path, track, num_samples=3)
        appearances[id(track)] = feat
        if feat is not None:
            print(f"  Vehicle {vid}: HSV特征提取成功")
        else:
            print(f"  Vehicle {vid}: HSV特征提取失败")
    
    # 2. 排序轨迹（按入场帧）
    track_list.sort(key=lambda t: t["boxes"][0][0])
    
    # 3. 贪心合并：每次找最佳匹配对
    print(f"\n[2/4] 开始合并检测 (时间阈值={MAX_TIME_GAP_SEC}s, 空间阈值={MAX_SPATIAL_DIST}px, 外观阈值={MIN_APPEARANCE_SIM})...")
    
    merged_any = True
    iteration = 0
    merge_log = []
    
    while merged_any:
        merged_any = False
        iteration += 1
        best_pair = None
        best_score = -1
        best_reason = ""
        
        n = len(track_list)
        for i in range(n):
            for j in range(i + 1, n):
                t_a = track_list[i]
                t_b = track_list[j]
                
                can, reason = can_merge(
                    t_a, t_b, fps, appearances,
                    spatial_thresh=MAX_SPATIAL_DIST,
                    time_thresh_frames=time_thresh_frames,
                    sim_thresh=MIN_APPEARANCE_SIM
                )
                
                if can:
                    # 计算合并得分（越高越好）
                    # 基于：时间间隙小、空间距离近、外观相似度高
                    enter_a = t_a["boxes"][0][0]
                    exit_a = t_a["boxes"][-1][0]
                    enter_b = t_b["boxes"][0][0]
                    
                    if exit_a > enter_b:
                        t_a, t_b = t_b, t_a  # 确保a在b前
                        exit_a = t_a["boxes"][-1][0]
                        enter_b = t_b["boxes"][0][0]
                    
                    gap = enter_b - exit_a
                    end_pos_a = get_box_center(t_a["boxes"][-1])
                    start_pos_b = get_box_center(t_b["boxes"][0])
                    dist = np.sqrt((end_pos_a[0] - start_pos_b[0])**2 + 
                                   (end_pos_a[1] - start_pos_b[1])**2)
                    
                    feat_a = appearances.get(id(t_a))
                    feat_b = appearances.get(id(t_b))
                    sim = compute_appearance_similarity(feat_a, feat_b) if feat_a is not None and feat_b is not None else 0.5
                    
                    # 得分：外观权重高，时间和空间惩罚
                    score = sim * 100 - gap - dist * 0.1
                    
                    if score > best_score:
                        best_score = score
                        best_pair = (i, j, t_a, t_b)
                        best_reason = f"gap={gap}f, dist={dist:.1f}px, sim={sim:.3f}"
                else:
                    # 只打印可能相关的车辆的失败原因
                    pass
        
        if best_pair is not None:
            i_idx, j_idx, t_a, t_b = best_pair
            # 确保t_a在时间上早于t_b
            if t_a["boxes"][-1][0] > t_b["boxes"][0][0]:
                t_a, t_b = t_b, t_a
            
            merged_track = merge_two_tracks(t_a, t_b)
            
            id_a = t_a.get("original_id", t_a.get("merged_id", "?"))
            id_b = t_b.get("original_id", t_b.get("merged_id", "?"))
            merged_track["merged_id"] = f"{id_a}+{id_b}"
            
            log_entry = {
                "from": [id_a, id_b],
                "to": merged_track["merged_id"],
                "frame_range": [merged_track["enter_frame"], merged_track["exit_frame"]],
                "reason": best_reason,
                "num_boxes": len(merged_track["boxes"])
            }
            merge_log.append(log_entry)
            
            print(f"  [合并 #{iteration}] {id_a} + {id_b} -> {merged_track['merged_id']}")
            print(f"    原因: {best_reason}")
            print(f"    合并后帧范围: [{merged_track['enter_frame']}-{merged_track['exit_frame']}], boxes: {len(merged_track['boxes'])}")
            
            # 提取合并后轨迹的外观特征
            feat = get_track_appearance(video_path, merged_track, num_samples=3)
            appearances[id(merged_track)] = feat
            
            # 移除旧轨迹，添加合并后的轨迹
            # 注意：要先移除索引大的，再移除索引小的
            max_idx = max(i_idx, j_idx)
            min_idx = min(i_idx, j_idx)
            track_list.pop(max_idx)
            track_list.pop(min_idx)
            track_list.append(merged_track)
            
            merged_any = True
    
    print(f"\n[3/4] 合并完成，共进行 {len(merge_log)} 次合并")
    
    # 4. 构建输出
    print("[4/4] 构建输出...")
    result = {}
    for idx, track in enumerate(track_list):
        vid = track.get("original_id", f"merged_{idx}")
        if "merged_id" in track:
            vid = track["merged_id"]
        
        # 重新计算最终字段
        boxes = track["boxes"]
        track["enter_frame"] = boxes[0][0]
        track["exit_frame"] = boxes[-1][0]
        track["last_seen"] = boxes[-1][0]
        track["lifetime_frames"] = len(boxes)
        track["lifetime_sec"] = len(boxes) / fps
        
        start_center = get_box_center(boxes[0])
        end_center = get_box_center(boxes[-1])
        track["total_displacement"] = float(np.sqrt(
            (end_center[0] - start_center[0])**2 + 
            (end_center[1] - start_center[1])**2
        ))
        
        result[str(vid)] = track
    
    return result, merge_log

def main():
    print("=" * 60)
    print("车辆轨迹片段合并工具")
    print("=" * 60)
    
    # 读取输入
    with open(VEHICLES_JSON, 'r') as f:
        vehicles = json.load(f)
    
    with open(VIDEO_INFO_JSON, 'r') as f:
        video_info = json.load(f)
    
    fps = video_info.get("fps", 15.0)
    total_frames = video_info.get("total_frames", 343)
    
    print(f"\n视频信息: {video_info['video_path']}")
    print(f"FPS: {fps}, 总帧数: {total_frames}, 时长: {total_frames/fps:.2f}s")
    print(f"\n输入轨迹数: {len(vehicles)}")
    
    # 打印合并前统计
    print("\n--- 合并前轨迹概览 ---")
    total_boxes_before = 0
    for vid in sorted(vehicles.keys(), key=int):
        v = vehicles[vid]
        boxes = v["boxes"]
        total_boxes_before += len(boxes)
        print(f"  车辆 {vid:>2s}: road={v['road_id']}, "
              f"frames={len(boxes):>3d} ({len(boxes)/fps:.2f}s), "
              f"range=[{boxes[0][0]:>3d}-{boxes[-1][0]:>3d}]")
    
    # 特别关注车辆2
    if "2" in vehicles:
        v2 = vehicles["2"]
        print(f"\n*** 特别关注: 车辆2 ***")
        print(f"  原始帧范围: [{v2['boxes'][0][0]}-{v2['boxes'][-1][0]}]")
        print(f"  原始帧数: {len(v2['boxes'])} ({len(v2['boxes'])/fps:.2f}s)")
        print(f"  道路ID: {v2['road_id']}")
    
    # 执行合并
    merged_vehicles, merge_log = merge_tracks(vehicles, fps, VIDEO_PATH)
    
    # 统计合并后
    print("\n--- 合并后轨迹概览 ---")
    total_boxes_after = 0
    for vid in sorted(merged_vehicles.keys(), key=lambda x: str(x)):
        v = merged_vehicles[vid]
        boxes = v["boxes"]
        total_boxes_after += len(boxes)
        is_merged = "merged_from" in v and len(v["merged_from"]) > 1
        marker = " [MERGED]" if is_merged else ""
        print(f"  车辆 {vid:>6s}: road={v['road_id']}, "
              f"frames={len(boxes):>3d} ({len(boxes)/fps:.2f}s), "
              f"range=[{boxes[0][0]:>3d}-{boxes[-1][0]:>3d}]{marker}")
        if is_merged:
            for frag in v["merged_from"]:
                print(f"    - 片段: {frag['original_id']}, range={frag['frame_range']}, boxes={frag['num_boxes']}")
    
    # 统计对比
    print("\n" + "=" * 60)
    print("合并统计对比")
    print("=" * 60)
    print(f"  合并前轨迹数: {len(vehicles)}")
    print(f"  合并后轨迹数: {len(merged_vehicles)}")
    print(f"  合并减少数: {len(vehicles) - len(merged_vehicles)}")
    print(f"  合并前总框数: {total_boxes_before}")
    print(f"  合并后总框数: {total_boxes_after}")
    print(f"  合并操作次数: {len(merge_log)}")
    
    # 检查车辆2
    print(f"\n*** 车辆2合并检查结果 ***")
    v2_merged = None
    for vid, v in merged_vehicles.items():
        if "merged_from" in v:
            for frag in v["merged_from"]:
                if frag["original_id"] == "2":
                    v2_merged = vid
                    break
        if v.get("original_id") == "2":
            v2_merged = vid
    
    if v2_merged:
        v = merged_vehicles[v2_merged]
        print(f"  ✓ 车辆2已被合并到新轨迹: {v2_merged}")
        print(f"    新帧范围: [{v['boxes'][0][0]}-{v['boxes'][-1][0]}]")
        print(f"    新帧数: {len(v['boxes'])} ({len(v['boxes'])/fps:.2f}s)")
        if "merged_from" in v:
            print(f"    包含片段:")
            for frag in v["merged_from"]:
                print(f"      - {frag['original_id']}: frame_range={frag['frame_range']}, boxes={frag['num_boxes']}")
    else:
        print(f"  ✗ 车辆2未被合并，保持原始状态")
        if "2" in merged_vehicles:
            v = merged_vehicles["2"]
            print(f"    帧范围: [{v['boxes'][0][0]}-{v['boxes'][-1][0]}]")
            print(f"    帧数: {len(v['boxes'])} ({len(v['boxes'])/fps:.2f}s)")
    
    # 保存结果
    print(f"\n保存合并结果到: {OUTPUT_JSON}")
    os.makedirs(os.path.dirname(OUTPUT_JSON), exist_ok=True)
    
    # 清理内部字段
    output_data = {}
    for vid, v in merged_vehicles.items():
        out = dict(v)
        # 移除Python对象引用
        out.pop("original_id", None)
        out.pop("merged_id", None)
        output_data[str(vid)] = out
    
    with open(OUTPUT_JSON, 'w') as f:
        json.dump(output_data, f, indent=2, ensure_ascii=False)
    
    print("\n✓ 完成!")

if __name__ == "__main__":
    main()
