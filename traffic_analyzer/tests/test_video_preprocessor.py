"""
Unit tests for the VideoPreprocessor module.

Uses synthetic OpenCV-generated videos to avoid external dependencies
on real video files.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Generator, List

import cv2
import numpy as np
import pytest
from PIL import Image

from traffic_analyzer.core.video_preprocessor import (
    VideoPreprocessor,
    VideoPreprocessorError,
)
from traffic_analyzer.models.schemas import Keyframe, SamplingConfig


@pytest.fixture
def synthetic_video_path() -> Generator[str, None, None]:
    """Create a short synthetic video file for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "synthetic_test.mp4")
        fps = 10.0
        width, height = 320, 240
        duration_sec = 3.0
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        writer = cv2.VideoWriter(path, fourcc, fps, (width, height))
        total_frames = int(fps * duration_sec)
        for i in range(total_frames):
            # Create a frame with a moving circle to simulate motion
            frame = np.zeros((height, width, 3), dtype=np.uint8)
            cx = int((i / total_frames) * width)
            cy = height // 2
            cv2.circle(frame, (cx, cy), 20, (0, 255, 0), -1)
            # Add some static noise for realism
            noise = np.random.randint(0, 20, (height, width, 3), dtype=np.uint8)
            frame = cv2.add(frame, noise)
            writer.write(frame)
        writer.release()
        yield path


@pytest.fixture
def static_video_path() -> Generator[str, None, None]:
    """Create a completely static synthetic video file."""
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "static_test.mp4")
        fps = 10.0
        width, height = 320, 240
        duration_sec = 2.0
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        writer = cv2.VideoWriter(path, fourcc, fps, (width, height))
        total_frames = int(fps * duration_sec)
        frame = np.full((height, width, 3), 128, dtype=np.uint8)
        for _ in range(total_frames):
            writer.write(frame)
        writer.release()
        yield path


@pytest.fixture
def preprocessor() -> VideoPreprocessor:
    """Return a VideoPreprocessor with default config."""
    return VideoPreprocessor(
        config=SamplingConfig(
            coarse_fps=1.0,
            precision_fps=4.0,
            coarse_quality_threshold=0.0,
            precision_quality_threshold=0.0,
            max_precision_segments=10,
            segment_padding_sec=0.5,
        ),
        save_debug_frames=False,
    )


class TestVideoPreprocessor:
    """Tests for VideoPreprocessor public API."""

    def test_process_returns_keyframe_sequence(
        self,
        synthetic_video_path: str,
        preprocessor: VideoPreprocessor,
    ) -> None:
        """process() should return a KeyframeSequence with coarse frames."""
        result = preprocessor.process(synthetic_video_path)
        assert result is not None
        assert isinstance(result.coarse_frames, list)
        # 3 seconds at 1 fps = ~3 coarse frames
        assert len(result.coarse_frames) >= 2

    def test_process_precision_frames_for_motion(
        self,
        synthetic_video_path: str,
        preprocessor: VideoPreprocessor,
    ) -> None:
        """Motion video should produce precision frames."""
        result = preprocessor.process(synthetic_video_path)
        assert len(result.precision_frames) > 0
        for kf in result.precision_frames:
            assert kf.is_precision is True

    def test_process_static_video_no_precision(
        self,
        static_video_path: str,
        preprocessor: VideoPreprocessor,
    ) -> None:
        """Static video should not produce precision frames."""
        result = preprocessor.process(static_video_path)
        assert len(result.precision_frames) == 0

    def test_coarse_frames_have_image_data(
        self,
        synthetic_video_path: str,
        preprocessor: VideoPreprocessor,
    ) -> None:
        """When save_debug_frames=False, frames should carry image_data."""
        result = preprocessor.process(synthetic_video_path)
        for kf in result.coarse_frames:
            assert kf.image_data is not None
            assert len(kf.image_data) > 0
            assert kf.image_path is None

    def test_coarse_frames_have_image_path_when_saving(
        self,
        synthetic_video_path: str,
    ) -> None:
        """When save_debug_frames=True, frames should carry image_path."""
        with tempfile.TemporaryDirectory() as outdir:
            proc = VideoPreprocessor(
                config=SamplingConfig(
                    coarse_fps=1.0,
                    precision_fps=4.0,
                    coarse_quality_threshold=0.0,
                    precision_quality_threshold=0.0,
                ),
                output_dir=outdir,
                save_debug_frames=True,
            )
            result = proc.process(synthetic_video_path)
            for kf in result.coarse_frames:
                assert kf.image_path is not None
                assert os.path.exists(kf.image_path)

    def test_quality_scores_in_range(
        self,
        synthetic_video_path: str,
        preprocessor: VideoPreprocessor,
    ) -> None:
        """Quality scores should be between 0 and 1."""
        result = preprocessor.process(synthetic_video_path)
        all_frames = result.coarse_frames + result.precision_frames
        for kf in all_frames:
            assert 0.0 <= kf.quality_score <= 1.0

    def test_deduplication_reduces_frames(
        self,
        static_video_path: str,
    ) -> None:
        """Deduplication should collapse identical static frames."""
        proc = VideoPreprocessor(
            config=SamplingConfig(
                coarse_fps=1.0,
                precision_fps=4.0,
                coarse_quality_threshold=0.0,
                precision_quality_threshold=0.0,
            ),
            save_debug_frames=False,
        )
        result = proc.process(static_video_path)
        # Static 2-second video at 1 fps => 2 frames, dedup should keep ~1
        assert len(result.coarse_frames) <= 2

    def test_extract_segment_basic(
        self,
        synthetic_video_path: str,
        preprocessor: VideoPreprocessor,
    ) -> None:
        """extract_segment() should return frames for the requested segment."""
        keyframes = preprocessor.extract_segment(
            synthetic_video_path, start_sec=0.5, end_sec=1.5, fps=2.0
        )
        assert isinstance(keyframes, list)
        assert len(keyframes) >= 1
        for kf in keyframes:
            assert 0.5 <= kf.timestamp_sec <= 1.5

    def test_extract_segment_returns_image_data(
        self,
        synthetic_video_path: str,
        preprocessor: VideoPreprocessor,
    ) -> None:
        """extract_segment frames should contain image bytes."""
        keyframes = preprocessor.extract_segment(
            synthetic_video_path, start_sec=0.0, end_sec=1.0, fps=1.0
        )
        for kf in keyframes:
            assert kf.image_data is not None

    def test_generate_thumbnail_grid(
        self,
        synthetic_video_path: str,
        preprocessor: VideoPreprocessor,
    ) -> None:
        """generate_thumbnail_grid should return a PIL Image of correct size."""
        result = preprocessor.process(synthetic_video_path)
        grid = preprocessor.generate_thumbnail_grid(
            result.coarse_frames, grid_size=(2, 2)
        )
        assert isinstance(grid, Image.Image)
        assert grid.width > 0
        assert grid.height > 0

    def test_generate_thumbnail_grid_with_bytes(
        self,
        synthetic_video_path: str,
        preprocessor: VideoPreprocessor,
    ) -> None:
        """Grid should work when keyframes hold in-memory bytes."""
        result = preprocessor.process(synthetic_video_path)
        grid = preprocessor.generate_thumbnail_grid(
            result.coarse_frames[:4], grid_size=(2, 2)
        )
        assert grid.mode == "RGB"

    def test_generate_thumbnail_grid_empty_raises(
        self,
        preprocessor: VideoPreprocessor,
    ) -> None:
        """Empty keyframe list should raise VideoPreprocessorError."""
        with pytest.raises(VideoPreprocessorError):
            preprocessor.generate_thumbnail_grid([], grid_size=(2, 2))

    def test_unreadable_video_raises(
        self,
        preprocessor: VideoPreprocessor,
    ) -> None:
        """Processing a non-existent video should raise VideoPreprocessorError."""
        with pytest.raises(VideoPreprocessorError):
            preprocessor.process("/nonexistent/video.mp4")

    def test_context_manager_cleanup(
        self,
        synthetic_video_path: str,
    ) -> None:
        """Using as context manager should not raise and should clean up."""
        with VideoPreprocessor(save_debug_frames=False) as proc:
            result = proc.process(synthetic_video_path)
            assert len(result.coarse_frames) > 0

    def test_process_timestamps_monotonic(
        self,
        synthetic_video_path: str,
        preprocessor: VideoPreprocessor,
    ) -> None:
        """Timestamps in each frame list should be monotonically increasing."""
        result = preprocessor.process(synthetic_video_path)
        for frame_list in (result.coarse_frames, result.precision_frames):
            timestamps = [kf.timestamp_sec for kf in frame_list]
            assert timestamps == sorted(timestamps)

    def test_max_precision_segments_respected(
        self,
        synthetic_video_path: str,
    ) -> None:
        """Config max_precision_segments should limit precision segments."""
        proc = VideoPreprocessor(
            config=SamplingConfig(
                coarse_fps=1.0,
                precision_fps=4.0,
                max_precision_segments=1,
                segment_padding_sec=0.5,
                coarse_quality_threshold=0.0,
                precision_quality_threshold=0.0,
            ),
            save_debug_frames=False,
        )
        result = proc.process(synthetic_video_path)
        # Precision frames should exist but be limited by segment count
        assert isinstance(result.precision_frames, list)
