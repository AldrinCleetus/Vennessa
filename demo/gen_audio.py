#!/usr/bin/env python3
"""
gen_audio.py: Generate speech audio for each narration step using edge-tts.
Measures audio clip durations with ffprobe and validates the total video length constraint.
"""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

try:
    import edge_tts
except ImportError:
    print("Error: edge-tts is not installed. Run: pip install -r requirements.txt", file=sys.stderr)
    sys.exit(1)

VOICE = "en-US-AndrewNeural"
MAX_ALLOWED_TOTAL_SECONDS = 170.0  # Video must be strictly under 3:00 (180s), target ~2:40 (160s)
PAUSE_PER_STEP_SECONDS = 0.5

BASE_DIR = Path(__file__).resolve().parent
NARRATION_FILE = BASE_DIR / "narration.json"
AUDIO_DIR = BASE_DIR / "audio"
DURATIONS_FILE = BASE_DIR / "durations.json"


def get_audio_duration(file_path: Path) -> float:
    """Use ffprobe to accurately read duration in seconds."""
    cmd = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(file_path)
    ]
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
    return float(res.stdout.strip())


async def generate_clip(step_id: str, text: str, output_path: Path):
    """Generate audio file for a single narration step using edge-tts."""
    communicate = edge_tts.Communicate(text, VOICE)
    await communicate.save(str(output_path))


async def main():
    if not NARRATION_FILE.exists():
        print(f"Error: {NARRATION_FILE} not found!", file=sys.stderr)
        sys.exit(1)

    with open(NARRATION_FILE, "r", encoding="utf-8") as f:
        steps = json.load(f)

    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    durations = {}

    print(f"Generating audio for {len(steps)} steps using voice '{VOICE}'...\n")

    for step in steps:
        step_id = step["id"]
        narration = step["narration"]
        out_file = AUDIO_DIR / f"{step_id}.mp3"

        print(f"[{step_id}] Synthesizing speech...")
        await generate_clip(step_id, narration, out_file)

        duration = get_audio_duration(out_file)
        durations[step_id] = round(duration, 3)
        print(f"  -> Generated: {out_file.name} ({duration:.2f}s)")

    # Save durations to JSON
    with open(DURATIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(durations, f, indent=2)

    total_audio_seconds = sum(durations.values())
    total_pause_seconds = len(steps) * PAUSE_PER_STEP_SECONDS
    total_projected_seconds = total_audio_seconds + total_pause_seconds

    minutes = int(total_projected_seconds // 60)
    seconds = total_projected_seconds % 60

    print("\n" + "=" * 55)
    print("DURATION SUMMARY:")
    print(f"  Steps Count:          {len(steps)}")
    print(f"  Total Audio Time:     {total_audio_seconds:.2f}s")
    print(f"  Total Step Pauses:    {total_pause_seconds:.2f}s ({PAUSE_PER_STEP_SECONDS}s per step)")
    print(f"  Projected Video Time: {total_projected_seconds:.2f}s (~{minutes}m {seconds:.1f}s)")
    print(f"  Max Allowed Time:     {MAX_ALLOWED_TOTAL_SECONDS:.1f}s (2m 50s)")
    print("=" * 55)

    if total_projected_seconds > MAX_ALLOWED_TOTAL_SECONDS:
        print(
            f"\n[ERROR] Total projected length ({total_projected_seconds:.2f}s) EXCEEDS "
            f"the maximum allowed constraint of {MAX_ALLOWED_TOTAL_SECONDS:.1f}s!\n"
            "Please shorten the narration text in narration.json and run again.",
            file=sys.stderr
        )
        sys.exit(1)

    print(f"\n[SUCCESS] Durations successfully verified and saved to {DURATIONS_FILE.name}!")


if __name__ == "__main__":
    asyncio.run(main())
