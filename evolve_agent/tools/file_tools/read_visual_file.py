# Copyright 2025 Google LLC
# SPDX-License-Identifier: Apache-2.0
"""
read_visual_file tool - Reads image and video files for multimodal LLMs.

Handles images (PNG, JPG, GIF, WEBP, SVG, BMP) and video files
(MP4, AVI, MOV, MKV, WEBM, FLV, WMV, M4V).
Video files are processed by extracting key frames via ffmpeg in the sandbox.

For text files, use the read_file tool instead.
"""

import base64
import logging
import mimetypes
import shlex
import uuid
from pathlib import Path
from typing import Any

from nexau.archs.main_sub.agent_state import AgentState
from nexau.archs.sandbox import BaseSandbox, SandboxStatus
from nexau.archs.tool.builtin._sandbox_utils import get_sandbox, resolve_path

logger = logging.getLogger(__name__)

# Max file size for images (videos are streamed by ffmpeg, so no size limit)
MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024  # 10MB

# Supported visual media types
IMAGE_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".bmp",
    ".webp",
    ".tiff",
    ".tif",
    ".svg",
}
VIDEO_EXTENSIONS = {".mp4", ".avi", ".mov", ".mkv", ".webm", ".flv", ".wmv", ".m4v"}

# Video frame extraction settings
VIDEO_FRAME_INTERVAL_SEC = 5  # Extract one frame every 5 seconds
VIDEO_MAX_FRAMES = 10  # Return at most 10 frames


def _detect_file_type(file_path: str) -> str:
    """Detect visual file type based on extension."""
    ext = Path(file_path).suffix.lower()
    if ext in IMAGE_EXTENSIONS:
        return "image"
    elif ext in VIDEO_EXTENSIONS:
        return "video"
    return "unknown"


def _read_video_frames(
    file_path: str,
    sandbox: BaseSandbox,
    frame_interval: int = VIDEO_FRAME_INTERVAL_SEC,
    max_frames: int = VIDEO_MAX_FRAMES,
    frame_width: int | None = None,
) -> list[dict[str, str]]:
    """Extract key frames from video using ffmpeg via sandbox.

    Args:
        file_path: Video file path (inside sandbox)
        sandbox: Sandbox instance
        frame_interval: Seconds between extracted frames
        max_frames: Maximum number of frames to return
        frame_width: Output frame width in pixels (preserving aspect ratio).
            None keeps original resolution.

    Returns:
        list of image dicts for coerce_tool_result_content

    Raises:
        ValueError: If numeric parameters are not positive integers.
    """
    # Sanitize numeric parameters to prevent ffmpeg filter injection
    frame_interval = int(frame_interval)
    max_frames = int(max_frames)
    if frame_interval <= 0:
        raise ValueError(f"frame_interval must be a positive integer, got {frame_interval}")
    if max_frames <= 0:
        raise ValueError(f"max_frames must be a positive integer, got {max_frames}")
    if frame_width is not None:
        frame_width = int(frame_width)
        if frame_width <= 0:
            raise ValueError(f"frame_width must be a positive integer, got {frame_width}")

    # 1. Create temp directory with restrictive permissions (owner-only)
    tmp_dir = f"/tmp/nexau_video_frames_{uuid.uuid4().hex[:12]}"
    sandbox.execute_bash(f"mkdir -m 0700 -p {shlex.quote(tmp_dir)}")

    try:
        out_pattern = f"{tmp_dir}/frame_%04d.jpg"

        # 2. Use ffmpeg to extract frames (with optional scaling)
        vf_filters = [f"fps=1/{frame_interval}"]
        if frame_width is not None and frame_width > 0:
            # scale=W:-2 preserves aspect ratio, -2 ensures even height (ffmpeg requirement)
            vf_filters.append(f"scale={frame_width}:-2")
        vf_str = ",".join(vf_filters)

        ffmpeg_cmd = f"ffmpeg -i {shlex.quote(file_path)} -vf {shlex.quote(vf_str)} -q:v 2 {shlex.quote(out_pattern)} -y 2>&1"
        cmd_result = sandbox.execute_bash(ffmpeg_cmd, timeout=60_000)

        if cmd_result.status != SandboxStatus.SUCCESS or cmd_result.exit_code != 0:
            stderr = cmd_result.stderr or cmd_result.stdout or ""
            if "not found" in stderr.lower() or cmd_result.exit_code == 127:
                raise RuntimeError("ffmpeg not found in sandbox. Install ffmpeg to process video files.")
            raise RuntimeError(f"ffmpeg failed (exit {cmd_result.exit_code}): {stderr[:500]}")

        # 3. List extracted frame files
        ls_result = sandbox.execute_bash(f"ls -1 {shlex.quote(tmp_dir)}/frame_*.jpg 2>/dev/null | sort")
        if ls_result.status != SandboxStatus.SUCCESS or not ls_result.stdout.strip():
            raise RuntimeError("No frames extracted from video")

        frame_paths = [p.strip() for p in ls_result.stdout.strip().splitlines() if p.strip()]

        # 4. Uniform sampling (when frame count exceeds limit)
        if len(frame_paths) > max_frames:
            step = len(frame_paths) / max_frames
            frame_paths = [frame_paths[int(i * step)] for i in range(max_frames)]

        # 5. Read each frame and convert to base64
        results: list[dict[str, str]] = []
        for i, fpath in enumerate(frame_paths):
            res = sandbox.read_file(fpath, binary=True)
            if res.status != SandboxStatus.SUCCESS or not res.content:
                continue

            raw: bytes
            if isinstance(res.content, (bytes, bytearray)):
                raw = bytes(res.content)
            else:
                raw = res.content.encode("utf-8", errors="replace")

            b64 = base64.b64encode(raw).decode("utf-8")

            # Estimate timestamp from filename
            fname = Path(fpath).stem
            try:
                frame_num = int(fname.split("_")[-1]) - 1
            except ValueError:
                frame_num = i
            timestamp = frame_num * frame_interval

            results.append(
                {
                    "type": "image",
                    "image_url": f"data:image/jpeg;base64,{b64}",
                    "detail": "auto",
                    "label": f"Frame {i + 1} / ~{timestamp}s",
                }
            )

        if not results:
            raise RuntimeError("Failed to read any extracted frames from video")

        return results
    finally:
        # Always cleanup temp directory, even on unexpected exceptions
        sandbox.execute_bash(f"rm -rf {shlex.quote(tmp_dir)}")


def _resize_image_in_sandbox(
    file_path: str,
    sandbox: BaseSandbox,
    max_width: int,
) -> bytes | None:
    """Resize image via ffmpeg in sandbox, returning JPEG bytes or None on failure.

    Only downscales if the image width exceeds max_width, preserving aspect ratio.
    """
    # Sanitize numeric parameter to prevent ffmpeg filter injection
    max_width = int(max_width)
    if max_width <= 0:
        raise ValueError(f"max_width must be a positive integer, got {max_width}")

    tmp_out = f"/tmp/nexau_resized_{uuid.uuid4().hex[:12]}.jpg"
    try:
        # scale='min(max_width,iw)':-2 only shrinks when width exceeds threshold
        scale_expr = f"scale='min({max_width},iw)':-2"
        cmd = f"ffmpeg -i {shlex.quote(file_path)} -vf {shlex.quote(scale_expr)} -q:v 2 {shlex.quote(tmp_out)} -y 2>&1"
        result = sandbox.execute_bash(cmd, timeout=30_000)
        if result.status != SandboxStatus.SUCCESS or result.exit_code != 0:
            return None

        res = sandbox.read_file(tmp_out, binary=True)
        if res.status != SandboxStatus.SUCCESS or not res.content:
            return None

        if isinstance(res.content, (bytes, bytearray)):
            return bytes(res.content)
        return res.content.encode("utf-8", errors="replace")
    finally:
        sandbox.execute_bash(f"rm -f {shlex.quote(tmp_out)}")


def _read_image_file(
    file_path: str,
    sandbox: BaseSandbox,
    image_detail: str = "auto",
    image_max_size: int | None = None,
) -> dict[str, Any]:
    """Read image file and return in nexau-supported format for LLM.

    Returns {"type": "image", "image_url": "data:...;base64,...", "detail": "..."}
    so coerce_tool_result_content can convert to ImageBlock.
    """
    ext = Path(file_path).suffix.lower()

    res = sandbox.read_file(file_path, binary=True)
    if res.status != SandboxStatus.SUCCESS:
        raise RuntimeError(res.error or "Failed to read image file")

    content: bytes
    if isinstance(res.content, (bytes, bytearray)):
        content = bytes(res.content)
    elif isinstance(res.content, str):
        content = res.content.encode("utf-8", errors="replace")
    else:
        content = b""

    mime_type, _ = mimetypes.guess_type(file_path)
    if not mime_type:
        mime_type = f"image/{ext[1:]}"

    b64_str = base64.b64encode(content).decode("utf-8")

    # Optional: resize image via ffmpeg to reduce token consumption
    if image_max_size is not None:
        image_max_size = int(image_max_size)
        if image_max_size > 0 and ext != ".svg":
            resized = _resize_image_in_sandbox(file_path, sandbox, image_max_size)
            if resized is not None:
                b64_str = base64.b64encode(resized).decode("utf-8")
                # Resized output is always JPEG
                mime_type = "image/jpeg"

    return {
        "type": "image",
        "image_url": f"data:{mime_type};base64,{b64_str}",
        "detail": image_detail,
    }


def read_visual_file(
    file_path: str,
    image_detail: str | None = None,
    image_max_size: int | None = None,
    video_frame_interval: int | None = None,
    video_max_frames: int | None = None,
    video_frame_width: int | None = None,
    agent_state: AgentState | None = None,
) -> dict[str, Any]:
    """
    Reads image and video files, returning visual content for multimodal LLMs.

    Supports images (PNG, JPG, GIF, WEBP, SVG, BMP) and video files
    (MP4, AVI, MOV, MKV, WEBM, FLV, WMV, M4V). Video files are processed
    by extracting key frames via ffmpeg in the sandbox.

    For text files, use the read_file tool instead.

    Args:
        file_path: The path to the image or video file to read.
        image_detail: Image detail level for LLM ("low", "high", "auto"). Default "auto".
        image_max_size: Max image width in pixels; images wider than this are
            downscaled via ffmpeg (preserving aspect ratio). None keeps original.
        video_frame_interval: Seconds between extracted video frames. Default 5.
        video_max_frames: Maximum number of video frames to return. Default 10.
        video_frame_width: Width in pixels for extracted video frames. None keeps
            original resolution.

    Returns:
        Dict with content and returnDisplay for the agent framework.
    """
    try:
        sandbox = get_sandbox(agent_state)

        # Resolve path (relative -> sandbox work_dir)
        resolved_path = resolve_path(file_path, sandbox)

        # Sanitize and validate numeric parameters to prevent ffmpeg filter injection
        try:
            if video_frame_interval is not None:
                video_frame_interval = int(video_frame_interval)
            if video_max_frames is not None:
                video_max_frames = int(video_max_frames)
            if video_frame_width is not None:
                video_frame_width = int(video_frame_width)
            if image_max_size is not None:
                image_max_size = int(image_max_size)
        except (TypeError, ValueError) as e:
            error_msg = f"Invalid parameter value (expected integer): {e}"
            return {
                "content": error_msg,
                "returnDisplay": "Invalid parameter.",
                "error": {
                    "message": error_msg,
                    "type": "INVALID_PARAMETER",
                },
            }

        if image_detail is not None and image_detail not in ("low", "high", "auto"):
            error_msg = f"Invalid image_detail value: {image_detail!r}. Must be 'low', 'high', or 'auto'."
            return {
                "content": error_msg,
                "returnDisplay": "Invalid parameter.",
                "error": {
                    "message": error_msg,
                    "type": "INVALID_PARAMETER",
                },
            }

        # Check if file exists
        if not sandbox.file_exists(resolved_path):
            error_msg = f"File not found: {file_path}"
            return {
                "content": error_msg,
                "returnDisplay": "File not found.",
                "error": {
                    "message": error_msg,
                    "type": "FILE_NOT_FOUND",
                },
            }

        # Check if it's a directory
        info = sandbox.get_file_info(resolved_path)
        if info.is_directory:
            error_msg = f"Path is a directory, not a file: {file_path}"
            return {
                "content": error_msg,
                "returnDisplay": "Path is a directory.",
                "error": {
                    "message": error_msg,
                    "type": "PATH_IS_DIRECTORY",
                },
            }

        file_type = _detect_file_type(resolved_path)

        # Reject non-visual files
        if file_type == "unknown":
            ext = Path(resolved_path).suffix.lower()
            error_msg = f"File '{file_path}' ({ext}) is not an image or video file. Use the read_file tool for text files."
            return {
                "content": error_msg,
                "returnDisplay": f"Not a visual file ({ext}) — use read_file.",
                "error": {
                    "message": error_msg,
                    "type": "NOT_VISUAL_FILE",
                },
            }

        # Handle video files (extract key frames via ffmpeg)
        if file_type == "video":
            v_interval = video_frame_interval if video_frame_interval is not None else VIDEO_FRAME_INTERVAL_SEC
            v_max = video_max_frames if video_max_frames is not None else VIDEO_MAX_FRAMES
            frames = _read_video_frames(
                resolved_path,
                sandbox,
                frame_interval=v_interval,
                max_frames=v_max,
                frame_width=video_frame_width,
            )
            num_frames = len(frames)
            # Prepend description text before frame list
            content_parts: list[dict[str, str]] = [
                {
                    "type": "text",
                    "text": (f"Video: {file_path} ({num_frames} key frames extracted, 1 frame every {v_interval}s)"),
                },
                *frames,
            ]
            return {
                "content": content_parts,
                "returnDisplay": f"Read video file: {file_path} ({num_frames} frames)",
            }

        # Handle image files
        if file_type == "image":
            # Check file size for images
            file_size = int(info.size or 0)
            if file_size > MAX_FILE_SIZE_BYTES:
                error_msg = f"Image file too large ({file_size} bytes). Maximum size is {MAX_FILE_SIZE_BYTES} bytes."
                return {
                    "content": error_msg,
                    "returnDisplay": "Image file too large.",
                    "error": {
                        "message": error_msg,
                        "type": "FILE_TOO_LARGE",
                    },
                }

            image_content = _read_image_file(
                resolved_path,
                sandbox,
                image_detail=image_detail or "auto",
                image_max_size=image_max_size,
            )
            return {
                "content": image_content,
                "returnDisplay": f"Read image file: {file_path}",
            }

        # Should not reach here
        error_msg = f"Unexpected file type for: {file_path}"
        return {
            "content": error_msg,
            "returnDisplay": "Unexpected file type.",
            "error": {
                "message": error_msg,
                "type": "UNEXPECTED_TYPE",
            },
        }

    except PermissionError:
        error_msg = f"Permission denied: {file_path}"
        return {
            "content": error_msg,
            "returnDisplay": "Permission denied.",
            "error": {
                "message": error_msg,
                "type": "PERMISSION_DENIED",
            },
        }
    except Exception as e:
        error_msg = f"Error reading visual file: {str(e)}"
        return {
            "content": error_msg,
            "returnDisplay": "Error reading visual file.",
            "error": {
                "message": error_msg,
                "type": "READ_ERROR",
            },
        }
