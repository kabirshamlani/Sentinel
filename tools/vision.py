"""Vision Agent -- multimodal image analysis using GPT-4o or Claude Sonnet.

Provides tools to analyze screenshots, video frames, diagrams, and
UI mockups using vision-capable models.
"""

from __future__ import annotations

import base64
import logging
import os
import subprocess
from pathlib import Path

logger = logging.getLogger("vision-agent")


def analyze_image(path: str, question: str | None = None) -> dict:
    """Analyze an image using a vision model.

    Args:
        path: Path to image file
        question: Optional specific question about the image
    """
    from tools.system_tools import read_image_base64

    img = read_image_base64(path)
    if "error" in img:
        return img

    prompt = question or "Describe this image in detail. What do you see? If it's a screenshot, describe the UI elements, layout, and any visible text or data."

    provider = os.getenv("VISION_PROVIDER", "openai")

    try:
        if provider == "anthropic":
            return _analyze_with_anthropic(img, prompt)
        else:
            return _analyze_with_openai(img, prompt)
    except Exception as e:
        logger.error("Vision analysis failed: %s", e)
        return {"error": f"Vision analysis failed: {e}"}


def _analyze_with_openai(img: dict, prompt: str) -> dict:
    """Use OpenAI GPT-4o for image analysis."""
    import openai

    client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    response = client.chat.completions.create(
        model="gpt-4o",
        max_tokens=2000,
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{img['mime_type']};base64,{img['base64']}",
                    },
                },
            ],
        }],
    )

    return {
        "analysis": response.choices[0].message.content,
        "model": "gpt-4o",
        "path": img["path"],
        "tokens_used": response.usage.total_tokens if response.usage else 0,
    }


def _analyze_with_anthropic(img: dict, prompt: str) -> dict:
    """Use Claude Sonnet for image analysis."""
    import anthropic

    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=2000,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": img["mime_type"],
                        "data": img["base64"],
                    },
                },
                {"type": "text", "text": prompt},
            ],
        }],
    )

    return {
        "analysis": response.content[0].text,
        "model": "claude-sonnet-4-20250514",
        "path": img["path"],
        "tokens_used": (response.usage.input_tokens + response.usage.output_tokens)
        if response.usage else 0,
    }


def analyze_video_frame(video_path: str, timestamp_seconds: float = 0.0, question: str | None = None) -> dict:
    """Extract a frame from a video at a given timestamp and analyze it."""
    p = Path(video_path).expanduser().resolve()
    if not p.exists():
        return {"error": f"Video not found: {video_path}"}

    frame_path = Path("/tmp") / f"frame_{p.stem}_{timestamp_seconds}.jpg"
    try:
        subprocess.run(
            [
                "ffmpeg", "-y", "-ss", str(timestamp_seconds),
                "-i", str(p), "-frames:v", "1",
                "-q:v", "2", str(frame_path),
            ],
            capture_output=True,
            timeout=15,
            check=True,
        )
    except FileNotFoundError:
        return {"error": "ffmpeg not installed. Install with: brew install ffmpeg"}
    except subprocess.CalledProcessError as e:
        return {"error": f"Frame extraction failed: {e.stderr.decode()[:500]}"}
    except subprocess.TimeoutExpired:
        return {"error": "Frame extraction timed out"}

    if not frame_path.exists():
        return {"error": "Frame extraction produced no output"}

    result = analyze_image(
        str(frame_path),
        question or f"This is a frame from video '{p.name}' at {timestamp_seconds}s. Describe what you see.",
    )
    frame_path.unlink(missing_ok=True)
    return result


def analyze_screenshot(path: str) -> dict:
    """Specialized screenshot analysis -- extracts UI elements, issues, and text."""
    return analyze_image(
        path,
        "This is a screenshot of a software application. Please analyze:\n"
        "1. What application/page is shown?\n"
        "2. What UI elements are visible (buttons, forms, tables, etc.)?\n"
        "3. Any visible errors, warnings, or issues?\n"
        "4. Any visible text content or data?\n"
        "5. Overall UX assessment -- is the layout clean and usable?",
    )
