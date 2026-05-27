"""
VideoPreprocessor module for the traffic analyzer framework.

Extracts keyframes from video using a two-pass sampling strategy:
1. Coarse sampling at low FPS for global coverage.
2. Motion analysis to detect high-motion or stationary-vehicle regions.
3. Precision sampling at higher FPS for detected motion segments.
4. Quality scoring and deduplication.
"""

from __future__ import annotations

import io
import logging
import os
import tempfile
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np
from PIL import Image

from traffic_analyzer.models.schemas import (
    Keyframe,
    KeyframeSequence,
    SamplingConfig,
    VideoMetadata,
)

logger = logging.getLogger(__name__)


class VideoPreprocessorError(Exception):
    """Base exception for video preprocessing errors."""

    pass


class VideoPreprocessor:
    """
    Preprocesses video files for VLM-based traffic event detection.

    Supports two-pass sampling (coarse + precision), motion analysis,
    quality scoring, deduplication, and thumbnail grid generation.
    """

    def __init__(
        self,
        config: Optional[SamplingConfig] = None,
        output_dir: Optional[str] = None,
        save_debug_frames: bool = False,
    ) -> None:
        """
        Initialize the VideoPreprocessor.

        Args:
            config: Sampling configuration. Uses defaults if None.
            output_dir: Directory to save extracted frames. Uses a temp
                directory if None.
            save_debug_frames: Whether to persist extracted frames to disk.
        """
        self.config = config or SamplingConfig()
        self.output_dir = output_dir
        self.save_debug_frames = save_debug_frames
        self._temp_dir: Optional[tempfile.TemporaryDirectory] = None

    def _ensure_output_dir(self, video_path: str) -> str:
        """Ensure an output directory exists for the given video."""
        if self.output_dir:
            base = Path(self.output_dir)
        else:
            if self._temp_dir is None:
                self._temp_dir = tempfile.TemporaryDirectory(prefix="video_preprocessor_")
            base = Path(self._temp_dir.name)

        video_stem = Path(video_path).stem
        target = base / video_stem
        target.mkdir(parents=True, exist_ok=True)
        return str(target)

    def _extract_metadata(self, video_path: str, cap: cv2.VideoCapture) -> VideoMetadata:
        """Extract metadata from an opened VideoCapture."""
        fps = cap.get(cv2.CAP_PROP_FPS)
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        duration_sec = total_frames / fps if fps > 0 else 0.0
        codec = str(int(cap.get(cv2.CAP_PROP_FOURCC)))
        bitrate = int(cap.get(cv2.CAP_PROP_BITRATE))

        return VideoMetadata(
            file_path=video_path,
            file_name=Path(video_path).name,
            duration_sec=duration_sec,
            fps=fps,
            total_frames=total_frames,
            width=width,
            height=height,
            codec=codec,
            bitrate=bitrate,
        )

    def _compute_quality_score(self, frame: np.ndarray) -> float:
        """
        Compute a quality score for a frame.

        Combines Laplacian variance (sharpness) and brightness balance.
        Returns a score between 0.0 and 1.0.
        """
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        laplacian_var = cv2.Laplacian(gray, cv2.CV_64F).var()
        # Normalize laplacian variance roughly to [0, 1]
        sharpness = min(laplacian_var / 500.0, 1.0)

        mean_brightness = np.mean(gray)
        # Ideal brightness around 128; penalize very dark or very bright
        brightness_score = 1.0 - abs(mean_brightness - 128.0) / 128.0

        return float(0.6 * sharpness + 0.4 * brightness_score)

    def _frame_to_bytes(self, frame: np.ndarray, fmt: str = "JPEG") -> bytes:
        """Encode an OpenCV BGR frame to image bytes."""
        success, encoded = cv2.imencode(f".{fmt.lower()}", frame)
        if not success:
            raise VideoPreprocessorError("Failed to encode frame to bytes")
        return encoded.tobytes()

    def _save_frame(
        self,
        frame: np.ndarray,
        output_dir: str,
        prefix: str,
        frame_id: int,
    ) -> str:
        """Save a frame to disk and return the path."""
        filename = f"{prefix}_frame_{frame_id:06d}.jpg"
        filepath = os.path.join(output_dir, filename)
        success = cv2.imwrite(filepath, frame)
        if not success:
            raise VideoPreprocessorError(f"Failed to write frame to {filepath}")
        return filepath

    def _extract_frames_at_fps(
        self,
        cap: cv2.VideoCapture,
        target_fps: float,
        metadata: VideoMetadata,
        output_dir: str,
        prefix: str,
        is_precision: bool = False,
        start_sec: Optional[float] = None,
        end_sec: Optional[float] = None,
    ) -> List[Keyframe]:
        """
        Extract frames from a video at a target FPS.

        Args:
            cap: OpenCV VideoCapture (will be repositioned if start_sec given).
            target_fps: Desired sampling rate.
            metadata: Video metadata.
            output_dir: Directory to save frames if saving enabled.
            prefix: Filename prefix for saved frames.
            is_precision: Whether these are precision keyframes.
            start_sec: Optional segment start in seconds.
            end_sec: Optional segment end in seconds.

        Returns:
            List of extracted Keyframes.
        """
        try:
            original_fps = metadata.fps
            if original_fps <= 0:
                return []

            interval = max(1, int(round(original_fps / target_fps)))
            keyframes: List[Keyframe] = []

            start_frame = 0
            end_frame = metadata.total_frames

            if start_sec is not None:
                start_frame = int(start_sec * original_fps)
            if end_sec is not None:
                end_frame = min(int(end_sec * original_fps), metadata.total_frames)

            cap.set(cv2.CAP_PROP_POS_FRAMES, start_frame)

            current_frame = start_frame
            local_id = 0
            while current_frame < end_frame:
                ret, frame = cap.read()
                if not ret:
                    break

                timestamp = current_frame / original_fps
                quality = self._compute_quality_score(frame)

                image_path: Optional[str] = None
                image_data: Optional[bytes] = None

                if self.save_debug_frames:
                    image_path = self._save_frame(frame, output_dir, prefix, local_id)
                else:
                    image_data = self._frame_to_bytes(frame)

                keyframes.append(
                    Keyframe(
                        frame_id=local_id,
                        timestamp_sec=timestamp,
                        image_path=image_path,
                        image_data=image_data,
                        quality_score=quality,
                        is_precision=is_precision,
                    )
                )

                local_id += 1
                current_frame += interval
                cap.set(cv2.CAP_PROP_POS_FRAMES, current_frame)

            return keyframes
        except Exception as exc:
            logger.error(
                "[video_preprocessor:_extract_frames_at_fps] EXTRACT_ERROR | video=%s fps=%.1f start=%s end=%s | %s",
                metadata.file_path,
                target_fps,
                start_sec,
                end_sec,
                exc,
                exc_info=True,
            )
            return []

    def _detect_motion_segments(
        self,
        cap: cv2.VideoCapture,
        metadata: VideoMetadata,
        coarse_frames: List[Keyframe],
    ) -> List[Tuple[float, float]]:
        """
        Detect time segments with significant motion.

        Uses frame differencing between consecutive coarse frames.
        Returns a list of (start_sec, end_sec) tuples for precision sampling.
        """
        if len(coarse_frames) < 2:
            return []

        segments: List[Tuple[float, float]] = []
        motion_flags: List[bool] = []

        for i in range(len(coarse_frames) - 1):
            ts1 = coarse_frames[i].timestamp_sec
            ts2 = coarse_frames[i + 1].timestamp_sec

            cap.set(cv2.CAP_PROP_POS_MSEC, ts1 * 1000)
            ret1, frame1 = cap.read()
            cap.set(cv2.CAP_PROP_POS_MSEC, ts2 * 1000)
            ret2, frame2 = cap.read()

            if not ret1 or not ret2:
                logger.error(
                    "[video_preprocessor:_detect_motion_segments] FRAME_READ_ERROR | pair=%d ts1=%.1f ts2=%.1f",
                    i,
                    ts1,
                    ts2,
                )
                motion_flags.append(False)
                continue

            gray1 = cv2.cvtColor(frame1, cv2.COLOR_BGR2GRAY)
            gray2 = cv2.cvtColor(frame2, cv2.COLOR_BGR2GRAY)
            diff = cv2.absdiff(gray1, gray2)
            # Threshold to reduce noise
            _, thresh = cv2.threshold(diff, 25, 255, cv2.THRESH_BINARY)
            motion_ratio = np.count_nonzero(thresh) / thresh.size
            motion_flags.append(motion_ratio > 0.02)

        # Convert per-pair motion flags into contiguous segments
        in_segment = False
        seg_start = 0.0
        for i, has_motion in enumerate(motion_flags):
            ts = coarse_frames[i].timestamp_sec
            next_ts = coarse_frames[i + 1].timestamp_sec if i + 1 < len(coarse_frames) else metadata.duration_sec
            if has_motion and not in_segment:
                in_segment = True
                seg_start = max(0.0, ts - self.config.segment_padding_sec)
            elif not has_motion and in_segment:
                in_segment = False
                seg_end = min(metadata.duration_sec, next_ts + self.config.segment_padding_sec)
                segments.append((seg_start, seg_end))

        if in_segment:
            seg_end = min(
                metadata.duration_sec,
                coarse_frames[-1].timestamp_sec + self.config.segment_padding_sec,
            )
            segments.append((seg_start, seg_end))

        # Merge overlapping segments
        if not segments:
            return []

        merged: List[Tuple[float, float]] = [segments[0]]
        for start, end in segments[1:]:
            prev_start, prev_end = merged[-1]
            if start <= prev_end:
                merged[-1] = (prev_start, max(prev_end, end))
            else:
                merged.append((start, end))

        # Limit number of segments
        if len(merged) > self.config.max_precision_segments:
            # Keep segments with largest duration
            merged.sort(key=lambda s: s[1] - s[0], reverse=True)
            merged = merged[: self.config.max_precision_segments]
            merged.sort(key=lambda s: s[0])

        return merged

    def _deduplicate_keyframes(
        self,
        keyframes: List[Keyframe],
        threshold: float = 0.99,
        min_time_gap_sec: float = 0.5,
    ) -> List[Keyframe]:
        """
        Remove visually similar consecutive keyframes.

        Uses histogram correlation as a lightweight similarity metric.
        A frame is kept if it is either visually different enough OR
        sufficiently far in time from the last kept frame.
        """
        if len(keyframes) <= 1:
            return keyframes

        def _load_frame(kf: Keyframe) -> Optional[np.ndarray]:
            if kf.image_data is not None:
                arr = np.frombuffer(kf.image_data, dtype=np.uint8)
                return cv2.imdecode(arr, cv2.IMREAD_COLOR)
            elif kf.image_path is not None:
                return cv2.imread(kf.image_path)
            return None

        filtered: List[Keyframe] = [keyframes[0]]
        prev_hist: Optional[np.ndarray] = None
        last_kept_ts: float = keyframes[0].timestamp_sec

        for kf in keyframes[1:]:
            frame = _load_frame(kf)
            if frame is None:
                logger.error(
                    "[video_preprocessor:_deduplicate_keyframes] FRAME_DECODE_ERROR | frame_id=%d",
                    kf.frame_id,
                )
                continue
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            hist = cv2.calcHist([gray], [0], None, [256], [0, 256])
            hist = cv2.normalize(hist, hist).flatten()

            time_gap = kf.timestamp_sec - last_kept_ts
            if prev_hist is not None:
                similarity = cv2.compareHist(prev_hist, hist, cv2.HISTCMP_CORREL)
                # Keep frame if it's visually different OR far enough in time
                if similarity < threshold or time_gap >= min_time_gap_sec:
                    filtered.append(kf)
                    last_kept_ts = kf.timestamp_sec
            else:
                filtered.append(kf)
                last_kept_ts = kf.timestamp_sec

            prev_hist = hist

        # Remove exact duplicates if any
        seen_ids = set()
        result: List[Keyframe] = []
        for kf in filtered:
            if kf.frame_id not in seen_ids:
                seen_ids.add(kf.frame_id)
                result.append(kf)

        return result

    def extract_segment(
        self,
        video_path: str,
        start_sec: float,
        end_sec: float,
        fps: float,
    ) -> List[Keyframe]:
        """
        Extract frames from a specific time segment at the given FPS.

        Args:
            video_path: Path to the video file.
            start_sec: Segment start time in seconds.
            end_sec: Segment end time in seconds.
            fps: Target sampling FPS.

        Returns:
            List of Keyframes for the segment.

        Raises:
            VideoPreprocessorError: If the video cannot be opened.
        """
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            raise VideoPreprocessorError(f"Cannot open video: {video_path}")

        try:
            metadata = self._extract_metadata(video_path, cap)
            output_dir = self._ensure_output_dir(video_path)
            segment_id = str(uuid.uuid4())[:8]
            keyframes = self._extract_frames_at_fps(
                cap=cap,
                target_fps=fps,
                metadata=metadata,
                output_dir=output_dir,
                prefix=f"segment_{segment_id}",
                start_sec=start_sec,
                end_sec=end_sec,
            )
            return keyframes
        except Exception as exc:
            logger.error(
                "[video_preprocessor:extract_segment] SEGMENT_ERROR | video=%s start=%.1f end=%.1f | %s",
                video_path,
                start_sec,
                end_sec,
                exc,
                exc_info=True,
            )
            return []
        finally:
            cap.release()

    def process(self, video_path: str) -> KeyframeSequence:
        """
        Process a video with two-pass sampling and return keyframes.

        Steps:
            1. Extract video metadata.
            2. Coarse sampling at ``coarse_fps``.
            3. Motion analysis between coarse frames.
            4. Precision sampling at ``precision_fps`` for motion segments.
            5. Quality scoring and deduplication.

        Args:
            video_path: Path to the video file.

        Returns:
            A KeyframeSequence containing coarse and precision frames.

        Raises:
            VideoPreprocessorError: If the video cannot be read.
        """
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            raise VideoPreprocessorError(f"Cannot open video: {video_path}")

        try:
            metadata = self._extract_metadata(video_path, cap)
            output_dir = self._ensure_output_dir(video_path)

            logger.info(
                "Processing video %s (%.1fs, %.2f fps, %dx%d)",
                metadata.file_name,
                metadata.duration_sec,
                metadata.fps,
                metadata.width,
                metadata.height,
            )

            # First pass: coarse sampling
            try:
                coarse_frames = self._extract_frames_at_fps(
                    cap=cap,
                    target_fps=self.config.coarse_fps,
                    metadata=metadata,
                    output_dir=output_dir,
                    prefix="coarse",
                    is_precision=False,
                )
                coarse_frames = [
                    kf for kf in coarse_frames
                    if kf.quality_score >= self.config.coarse_quality_threshold
                ]
                coarse_frames = self._deduplicate_keyframes(coarse_frames)
                logger.info("Coarse pass: %d frames retained", len(coarse_frames))
            except Exception as exc:
                logger.error(
                    "[video_preprocessor:process] COARSE_ERROR | video=%s | %s",
                    video_path,
                    exc,
                    exc_info=True,
                )
                coarse_frames = []

            # Motion analysis
            try:
                motion_segments = self._detect_motion_segments(cap, metadata, coarse_frames)
                logger.info("Detected %d motion segments", len(motion_segments))
            except Exception as exc:
                logger.error(
                    "[video_preprocessor:process] MOTION_ERROR | video=%s | %s",
                    video_path,
                    exc,
                    exc_info=True,
                )
                motion_segments = []

            # Second pass: precision sampling for motion segments
            precision_frames: List[Keyframe] = []
            for seg_idx, (start, end) in enumerate(motion_segments):
                try:
                    seg_frames = self._extract_frames_at_fps(
                        cap=cap,
                        target_fps=self.config.precision_fps,
                        metadata=metadata,
                        output_dir=output_dir,
                        prefix=f"precision_seg{seg_idx}",
                        is_precision=True,
                        start_sec=start,
                        end_sec=end,
                    )
                    precision_frames.extend(seg_frames)
                except Exception as exc:
                    logger.error(
                        "[video_preprocessor:process] PRECISION_ERROR | video=%s segment=%d | %s",
                        video_path,
                        seg_idx,
                        exc,
                        exc_info=True,
                    )
                    continue

            try:
                precision_frames = [
                    kf for kf in precision_frames
                    if kf.quality_score >= self.config.precision_quality_threshold
                ]
                precision_frames = self._deduplicate_keyframes(precision_frames)
                logger.info("Precision pass: %d frames retained", len(precision_frames))
            except Exception as exc:
                logger.error(
                    "[video_preprocessor:process] DEDUP_ERROR | video=%s | %s",
                    video_path,
                    exc,
                    exc_info=True,
                )
                # Fallback: return precision_frames without deduplication/quality filtering
                # If that also failed, use empty list
                if precision_frames is None:
                    precision_frames = []

            return KeyframeSequence(
                coarse_frames=coarse_frames,
                precision_frames=precision_frames,
            )
        finally:
            cap.release()

    def generate_thumbnail_grid(
        self,
        keyframes: List[Keyframe],
        grid_size: Tuple[int, int] = (4, 4),
    ) -> Image.Image:
        """
        Create a grid image from multiple keyframes for multi-frame VLM input.

        Args:
            keyframes: List of keyframes to include in the grid.
            grid_size: Tuple of (columns, rows).

        Returns:
            A PIL Image containing the thumbnail grid.

        Raises:
            VideoPreprocessorError: If no keyframes are provided or images
                cannot be loaded.
        """
        try:
            cols, rows = grid_size
            max_cells = cols * rows
            selected = keyframes[:max_cells]

            if not selected:
                raise VideoPreprocessorError("No keyframes provided for thumbnail grid")

            images: List[Image.Image] = []
            for kf in selected:
                img: Optional[Image.Image] = None
                if kf.image_data is not None:
                    img = Image.open(io.BytesIO(kf.image_data))
                elif kf.image_path is not None:
                    img = Image.open(kf.image_path)
                if img is None:
                    raise VideoPreprocessorError(
                        f"Cannot load image for keyframe at {kf.timestamp_sec}s"
                    )
                images.append(img.convert("RGB"))

            # Determine cell size from the first image
            cell_width = max(img.width for img in images)
            cell_height = max(img.height for img in images)

            grid_width = cols * cell_width
            grid_height = rows * cell_height
            grid_image = Image.new("RGB", (grid_width, grid_height), (0, 0, 0))

            for idx, img in enumerate(images):
                col = idx % cols
                row = idx // cols
                # Center the image within the cell
                x_offset = col * cell_width + (cell_width - img.width) // 2
                y_offset = row * cell_height + (cell_height - img.height) // 2
                grid_image.paste(img, (x_offset, y_offset))

            return grid_image
        except Exception as exc:
            logger.error(
                "[video_preprocessor:generate_thumbnail_grid] GRID_ERROR | keyframes=%d grid=%s | %s",
                len(keyframes),
                grid_size,
                exc,
                exc_info=True,
            )
            raise

    def cleanup(self) -> None:
        """Release temporary resources."""
        if self._temp_dir is not None:
            self._temp_dir.cleanup()
            self._temp_dir = None

    def __enter__(self) -> VideoPreprocessor:
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:  # noqa: ANN001
        """Context manager exit."""
        self.cleanup()
