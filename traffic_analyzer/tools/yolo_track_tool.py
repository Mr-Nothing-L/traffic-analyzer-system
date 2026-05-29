"""
YOLOv8 + ByteTrack tracking tool for traffic event detection.

Provides vehicle detection and tracking with displacement analysis.
Useful for identifying reverse driving and illegal parking events.
"""

import logging
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np
import torch
from ultralytics import YOLO

logger = logging.getLogger(__name__)


@dataclass
class TrackDisplacement:
    """位移矢量数据"""
    track_id: int
    vehicle_class: str
    start_pos: Tuple[float, float]  # (x, y) in normalized coordinates
    end_pos: Tuple[float, float]
    displacement: Tuple[float, float]  # (dx, dy) normalized
    displacement_pixels: Tuple[float, float]  # (dx, dy) in pixels
    distance: float  # 像素距离
    direction_deg: float  # 角度 0-360
    direction_text: str  # 像素方向描述，如 "dx=+150px, dy=-80px (向右下)"
    is_stationary: bool  # 是否静止
    frame_count: int  # 跟踪帧数
    confidence: float  # 平均置信度


@dataclass
class TrackResult:
    """跟踪结果"""
    success: bool
    annotated_image_path: Optional[str] = None
    displacements: List[TrackDisplacement] = field(default_factory=list)
    total_frames: int = 0
    processed_frames: int = 0
    vehicle_count: int = 0
    error_message: Optional[str] = None
    video_width: int = 0
    video_height: int = 0

    def to_dict(self) -> dict:
        """将结果转为字典格式"""
        return {
            "success": self.success,
            "total_frames": self.total_frames,
            "processed_frames": self.processed_frames,
            "vehicle_count": self.vehicle_count,
            "video_width": self.video_width,
            "video_height": self.video_height,
            "annotated_image_path": self.annotated_image_path,
            "error_message": self.error_message,
            "displacements": [
                {
                    "track_id": d.track_id,
                    "vehicle_class": d.vehicle_class,
                    "start_pos": {"x": round(d.start_pos[0], 4), "y": round(d.start_pos[1], 4)},
                    "end_pos": {"x": round(d.end_pos[0], 4), "y": round(d.end_pos[1], 4)},
                    "displacement_normalized": {"dx": round(d.displacement[0], 4), "dy": round(d.displacement[1], 4)},
                    "displacement_pixels": {"dx": round(d.displacement_pixels[0], 1), "dy": round(d.displacement_pixels[1], 1)},
                    "distance_pixels": round(d.distance, 1),
                    "direction_deg": round(d.direction_deg, 1),
                    "direction_text": d.direction_text,
                    "is_stationary": d.is_stationary,
                    "frame_count": d.frame_count,
                    "confidence": round(d.confidence, 3),
                }
                for d in self.displacements
            ],
        }


class YoloTrackTool:
    """
    YOLOv8 + ByteTrack 车辆跟踪工具
    
    输出:
    1. 带跟踪框和轨迹的关键帧图片
    2. 每个跟踪ID的位移矢量数据（含像素位移）
    """
    
    # 车辆类别映射 (COCO)
    VEHICLE_CLASSES = {2: "car", 3: "motorcycle", 5: "bus", 7: "truck"}
    
    def __init__(
        self,
        model_path: str = "yolov8n.pt",
        stationary_threshold: float = 5.0,  # 静止阈值(像素)
        conf_threshold: float = 0.3,
        device: str = "cuda" if torch.cuda.is_available() else "cpu",
    ):
        self.model_path = model_path
        self.stationary_threshold = stationary_threshold
        self.conf_threshold = conf_threshold
        self.device = device
        self._model: Optional[YOLO] = None
        
        logger.info(f"YoloTrackTool initialized: model={model_path}, "
                   f"stationary_threshold={stationary_threshold}, device={device}")
    
    def _load_model(self) -> Optional[YOLO]:
        """加载YOLO模型，带错误处理"""
        if self._model is not None:
            return self._model
        
        try:
            logger.info(f"Loading YOLO model from {self.model_path}")
            self._model = YOLO(self.model_path)
            logger.info("YOLO model loaded successfully")
            return self._model
        except Exception as e:
            logger.error(f"Failed to load YOLO model: {e}")
            return None
    
    def _get_direction_text(self, dx_pixels: float, dy_pixels: float) -> str:
        """
        像素位移转方向描述
        
        Args:
            dx_pixels: x方向位移(像素)，正=向右，负=向左
            dy_pixels: y方向位移(像素)，正=向下，负=向上(图像坐标系)
        
        Returns:
            如 "dx=+150px, dy=-80px (向右下)"
        """
        dx_str = f"+{dx_pixels:.0f}" if dx_pixels >= 0 else f"{dx_pixels:.0f}"
        dy_str = f"+{dy_pixels:.0f}" if dy_pixels >= 0 else f"{dy_pixels:.0f}"
        
        horiz = ""
        vert = ""
        
        if abs(dx_pixels) > 1:
            horiz = "右" if dx_pixels > 0 else "左"
        if abs(dy_pixels) > 1:
            vert = "下" if dy_pixels > 0 else "上"
        
        direction = horiz + vert if (horiz or vert) else "静止"
        
        return f"dx={dx_str}px, dy={dy_str}px ({direction})"
    
    def _calculate_displacement(
        self,
        track_id: int,
        positions: List[Tuple[float, float]],
        class_id: int,
        confidences: List[float],
        video_width: int,
        video_height: int,
    ) -> Optional[TrackDisplacement]:
        """计算单个跟踪目标的位移矢量"""
        if len(positions) < 2:
            return None
        
        start_pos = positions[0]
        end_pos = positions[-1]
        dx_norm = end_pos[0] - start_pos[0]
        dy_norm = end_pos[1] - start_pos[1]
        
        # 像素位移
        dx_pixels = dx_norm * video_width
        dy_pixels = dy_norm * video_height
        distance = np.sqrt(dx_pixels**2 + dy_pixels**2)
        
        # 计算角度 (0度=北，顺时针)
        angle_rad = np.arctan2(dx_pixels, -dy_pixels)
        angle_deg = np.degrees(angle_rad) % 360
        
        is_stationary = bool(distance < self.stationary_threshold)
        
        return TrackDisplacement(
            track_id=track_id,
            vehicle_class=self.VEHICLE_CLASSES.get(class_id, f"class_{class_id}"),
            start_pos=start_pos,
            end_pos=end_pos,
            displacement=(dx_norm, dy_norm),
            displacement_pixels=(dx_pixels, dy_pixels),
            distance=float(distance),
            direction_deg=float(angle_deg),
            direction_text=self._get_direction_text(dx_pixels, dy_pixels),
            is_stationary=is_stationary,
            frame_count=len(positions),
            confidence=float(np.mean(confidences)) if confidences else 0.0,
        )
    
    def track(
        self,
        video_path: str,
        output_dir: Optional[str] = None,
        sample_frame_idx: Optional[int] = None,
    ) -> TrackResult:
        """
        执行跟踪分析
        
        Args:
            video_path: 输入视频路径
            output_dir: 输出目录，None则使用临时目录
            sample_frame_idx: 输出标注图的帧索引，None则取中间帧
        
        Returns:
            TrackResult: 跟踪结果
        """
        logger.info(f"Starting track analysis: video={video_path}")
        
        # 1. 加载模型
        model = self._load_model()
        if model is None:
            return TrackResult(
                success=False,
                error_message="模型加载失败，请检查模型文件是否存在"
            )
        
        # 2. 打开视频
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            logger.error(f"Cannot open video: {video_path}")
            return TrackResult(
                success=False,
                error_message=f"无法打开视频文件: {video_path}"
            )
        
        fps = cap.get(cv2.CAP_PROP_FPS)
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        
        logger.info(f"Video opened: {width}x{height} @ {fps}fps, {total_frames} frames")
        
        if total_frames == 0:
            cap.release()
            return TrackResult(
                success=False,
                error_message="视频为空，无帧可处理",
                video_width=width,
                video_height=height,
            )
        
        # 3. 确定采样帧
        if sample_frame_idx is None:
            sample_frame_idx = total_frames // 2
        sample_frame_idx = max(0, min(sample_frame_idx, total_frames - 1))
        
        # 4. 执行跟踪
        track_history: Dict[int, List[Tuple[float, float]]] = {}
        track_classes: Dict[int, int] = {}
        track_confs: Dict[int, List[float]] = {}
        sample_annotated_frame: Optional[np.ndarray] = None
        
        frame_count = 0
        vehicle_detected = False
        
        try:
            while cap.isOpened():
                success, frame = cap.read()
                if not success:
                    break
                
                frame_count += 1
                
                # 运行跟踪
                try:
                    results = model.track(
                        frame,
                        persist=True,
                        tracker="bytetrack.yaml",
                        verbose=False,
                        conf=self.conf_threshold,
                        device=self.device,
                    )
                except Exception as e:
                    logger.warning(f"Tracking failed at frame {frame_count}: {e}")
                    continue
                
                # 提取跟踪结果
                if results[0].boxes.id is not None:
                    boxes = results[0].boxes.xywh.cpu().numpy()
                    track_ids = results[0].boxes.id.cpu().numpy().astype(int)
                    class_ids = results[0].boxes.cls.cpu().numpy().astype(int)
                    confs = results[0].boxes.conf.cpu().numpy()
                    
                    for box, track_id, class_id, conf in zip(
                        boxes, track_ids, class_ids, confs
                    ):
                        # 只处理车辆
                        if class_id not in self.VEHICLE_CLASSES:
                            continue
                        
                        vehicle_detected = True
                        x, y, w, h = box
                        
                        # 记录位置 (归一化坐标)
                        norm_x = x / width
                        norm_y = y / height
                        
                        if track_id not in track_history:
                            track_history[track_id] = []
                            track_classes[track_id] = class_id
                            track_confs[track_id] = []
                        
                        track_history[track_id].append((norm_x, norm_y))
                        track_confs[track_id].append(float(conf))
                
                # 保存采样帧的标注图
                if frame_count == sample_frame_idx + 1:
                    sample_annotated_frame = self._annotate_frame(
                        frame, results, track_history, width, height
                    )
        
        except Exception as e:
            logger.error(f"Error during tracking: {e}", exc_info=True)
            cap.release()
            return TrackResult(
                success=False,
                error_message=f"跟踪过程出错: {str(e)}",
                video_width=width,
                video_height=height,
            )
        
        cap.release()
        
        # 5. 检查是否有车辆
        if not vehicle_detected:
            logger.warning("No vehicles detected in video")
            return TrackResult(
                success=True,
                processed_frames=frame_count,
                total_frames=total_frames,
                error_message="未检测到车辆",
                video_width=width,
                video_height=height,
            )
        
        # 6. 计算位移矢量
        displacements = []
        for track_id, positions in track_history.items():
            disp = self._calculate_displacement(
                track_id,
                positions,
                track_classes[track_id],
                track_confs[track_id],
                width,
                height,
            )
            if disp:
                displacements.append(disp)
        
        # 按距离排序
        displacements.sort(key=lambda x: x.distance, reverse=True)
        
        logger.info(f"Tracking complete: {len(displacements)} vehicles tracked, "
                   f"{sum(1 for d in displacements if d.is_stationary)} stationary")
        
        # 7. 保存标注图片
        annotated_image_path = None
        if sample_annotated_frame is not None:
            try:
                if output_dir is None:
                    output_dir = tempfile.gettempdir()
                os.makedirs(output_dir, exist_ok=True)
                
                annotated_image_path = os.path.join(
                    output_dir,
                    f"track_annotated_{Path(video_path).stem}.jpg"
                )
                cv2.imwrite(annotated_image_path, sample_annotated_frame)
                logger.info(f"Annotated image saved: {annotated_image_path}")
            except Exception as e:
                logger.error(f"Failed to save annotated image: {e}")
        
        return TrackResult(
            success=True,
            annotated_image_path=annotated_image_path,
            displacements=displacements,
            total_frames=total_frames,
            processed_frames=frame_count,
            vehicle_count=len(displacements),
            video_width=width,
            video_height=height,
        )
    
    def _annotate_frame(
        self,
        frame: np.ndarray,
        results,
        track_history: Dict[int, List[Tuple[float, float]]],
        width: int,
        height: int,
    ) -> np.ndarray:
        """生成带标注的帧"""
        annotated = frame.copy()
        
        if results[0].boxes.id is None:
            return annotated
        
        boxes = results[0].boxes.xywh.cpu().numpy()
        track_ids = results[0].boxes.id.cpu().numpy().astype(int)
        class_ids = results[0].boxes.cls.cpu().numpy().astype(int)
        confs = results[0].boxes.conf.cpu().numpy()
        
        for box, track_id, class_id, conf in zip(boxes, track_ids, class_ids, confs):
            if class_id not in self.VEHICLE_CLASSES:
                continue
            
            x, y, w, h = box
            x1, y1 = int(x - w/2), int(y - h/2)
            x2, y2 = int(x + w/2), int(y + h/2)
            
            # 颜色根据是否静止
            is_stationary = False
            if track_id in track_history and len(track_history[track_id]) >= 2:
                positions = track_history[track_id]
                dx = (positions[-1][0] - positions[0][0]) * width
                dy = (positions[-1][1] - positions[0][1]) * height
                distance = np.sqrt(dx**2 + dy**2)
                is_stationary = distance < self.stationary_threshold
            
            color = (128, 128, 128) if is_stationary else (0, 255, 0)
            
            # 画框
            cv2.rectangle(annotated, (x1, y1), (x2, y2), color, 2)
            
            # 标签
            status = "[静止]" if is_stationary else ""
            label = f"ID:{track_id} {self.VEHICLE_CLASSES[class_id]}{status} {conf:.2f}"
            
            (text_w, text_h), _ = cv2.getTextSize(
                label, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 2
            )
            cv2.rectangle(
                annotated,
                (x1, y1 - text_h - 8),
                (x1 + text_w, y1),
                color,
                -1,
            )
            cv2.putText(
                annotated, label, (x1, y1 - 4),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 2
            )
            
            # 画轨迹
            if track_id in track_history and len(track_history[track_id]) > 1:
                points = []
                for px, py in track_history[track_id]:
                    points.append((int(px * width), int(py * height)))
                
                if len(points) >= 2:
                    points_array = np.array(points, dtype=np.int32)
                    cv2.polylines(
                        annotated, [points_array], False, (0, 255, 255), 2
                    )
                    # 画方向箭头
                    if len(points) >= 2:
                        cv2.arrowedLine(
                            annotated,
                            points[-2],
                            points[-1],
                            (0, 0, 255),
                            2,
                            tipLength=0.3,
                        )
        
        # 添加统计信息
        stats_text = f"Tracks: {len(track_history)} | Stationary: " \
                    f"{sum(1 for tid, pos in track_history.items() if len(pos) >= 2 and np.sqrt(((pos[-1][0]-pos[0][0])*width)**2 + ((pos[-1][1]-pos[0][1])*height)**2) < self.stationary_threshold)}"
        cv2.putText(
            annotated, stats_text, (10, 30),
            cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2
        )
        
        return annotated
    
    def to_json(self, result: TrackResult) -> dict:
        """将结果转为JSON格式"""
        return {
            "success": result.success,
            "total_frames": result.total_frames,
            "processed_frames": result.processed_frames,
            "vehicle_count": result.vehicle_count,
            "video_width": result.video_width,
            "video_height": result.video_height,
            "annotated_image_path": result.annotated_image_path,
            "error_message": result.error_message,
            "displacements": [
                {
                    "track_id": d.track_id,
                    "vehicle_class": d.vehicle_class,
                    "start_pos": {"x": round(d.start_pos[0], 4), "y": round(d.start_pos[1], 4)},
                    "end_pos": {"x": round(d.end_pos[0], 4), "y": round(d.end_pos[1], 4)},
                    "displacement_normalized": {"dx": round(d.displacement[0], 4), "dy": round(d.displacement[1], 4)},
                    "displacement_pixels": {"dx": round(d.displacement_pixels[0], 1), "dy": round(d.displacement_pixels[1], 1)},
                    "distance_pixels": round(d.distance, 1),
                    "direction_deg": round(d.direction_deg, 1),
                    "direction_text": d.direction_text,
                    "is_stationary": d.is_stationary,
                    "frame_count": d.frame_count,
                    "confidence": round(d.confidence, 3),
                }
                for d in result.displacements
            ],
        }
