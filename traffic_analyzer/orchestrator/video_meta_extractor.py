"""Video metadata extraction helpers for the analysis orchestrator."""

from __future__ import annotations

import logging
from pathlib import Path

import cv2

from traffic_analyzer.models.schemas import VideoMetadata

logger = logging.getLogger(__name__)


def extract_video_meta(video_path: str) -> VideoMetadata:
    """Extract video metadata using OpenCV.

    Args:
        video_path: Path to the video file.

    Returns:
        Populated VideoMetadata, or a zero-filled metadata object on error.
    """
    cap = cv2.VideoCapture(video_path)
    try:
        fps = cap.get(cv2.CAP_PROP_FPS)
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        duration_sec = total_frames / fps if fps > 0 else 0.0
        return VideoMetadata(
            file_path=video_path,
            file_name=Path(video_path).name,
            duration_sec=duration_sec,
            fps=fps,
            total_frames=total_frames,
            width=width,
            height=height,
        )
    except Exception as exc:
        logger.error(
            "[orchestrator:_extract_video_meta] META_ERROR | video=%s | %s",
            video_path,
            exc,
            exc_info=True,
        )
        return VideoMetadata(
            file_path=video_path,
            file_name=Path(video_path).name,
            duration_sec=0.0,
            fps=0.0,
            total_frames=0,
            width=0,
            height=0,
        )
    finally:
        cap.release()
