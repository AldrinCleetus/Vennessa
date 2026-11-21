#!/usr/bin/env python3
"""
build_assets.py:
1. Generates captions.srt from narration.json and timings.json.
2. Pads and concatenates audio clips with exact silence intervals based on timings.json
   to produce full_narration.wav perfectly synchronized with the recorded video.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
NARRATION_FILE = BASE_DIR / "narration.json"
DURATIONS_FILE = BASE_DIR / "durations.json"
TIMINGS_FILE = BASE_DIR / "timings.json"
AUDIO_DIR = BASE_DIR / "audio"
CAPTIONS_FILE = BASE_DIR / "captions.srt"
OUTPUT_AUDIO_FILE = BASE_DIR / "full_narration.wav"
TEMP_DIR = BASE_DIR / "temp_audio"


def format_srt_time(seconds: float) -> str:
    """Format seconds into SRT timestamp: HH:MM:SS,mmm"""
    seconds = max(0.0, seconds)
    hrs = int(seconds // 3600)
    mins = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int(round((seconds - int(seconds)) * 1000))
    if millis >= 1000:
        secs += 1
        millis = 0
    return f"{hrs:02d}:{mins:02d}:{secs:02d},{millis:03d}"


def generate_srt(steps, durations, timings):
    """Write standard SubRip (.srt) caption file."""
    print("[SRT] Generating captions.srt...")
    srt_lines = []
    index = 1

    for step in steps:
        step_id = step["id"]
        narration = step["narration"]
        start_t = timings.get(step_id, 0.0)
        dur = durations.get(step_id, 5.0)
        end_t = start_t + dur

        srt_lines.append(str(index))
        srt_lines.append(f"{format_srt_time(start_t)} --> {format_srt_time(end_t)}")
        srt_lines.append(narration.strip())
        srt_lines.append("")
        index += 1

    with open(CAPTIONS_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(srt_lines))

    print(f"[SRT] Saved: {CAPTIONS_FILE.name}")


def create_silence_wav(duration_s: float, out_path: Path):
    """Create a silent stereo WAV audio file of exact duration using ffmpeg."""
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi",
        "-i", f"anullsrc=r=44100:cl=stereo",
        "-t", f"{duration_s:.4f}",
        "-c:a", "pcm_s16le",
        str(out_path)
    ]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)


def convert_to_wav(mp3_path: Path, out_path: Path):
    """Convert an MP3 clip to standard 44.1kHz stereo PCM WAV."""
    cmd = [
        "ffmpeg", "-y",
        "-i", str(mp3_path),
        "-ar", "44100",
        "-ac", "2",
        "-c:a", "pcm_s16le",
        str(out_path)
    ]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)


def assemble_padded_audio(steps, durations, timings):
    """Construct continuous synchronized audio track matching timings.json."""
    print("[Audio] Assembling synchronized audio track with silence padding...")
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    concat_list_file = TEMP_DIR / "concat_list.txt"

    segments = []
    current_time = 0.0

    for i, step in enumerate(steps):
        step_id = step["id"]
        target_start = timings.get(step_id, current_time)
        clip_mp3 = AUDIO_DIR / f"{step_id}.mp3"

        if not clip_mp3.exists():
            print(f"Error: Missing audio clip {clip_mp3}", file=sys.stderr)
            sys.exit(1)

        # Calculate silence gap before this clip
        gap = target_start - current_time
        if gap > 0.02:  # Insert silence if gap is noticeable
            silence_file = TEMP_DIR / f"silence_{i:02d}.wav"
            create_silence_wav(gap, silence_file)
            segments.append(silence_file)
            current_time += gap

        # Convert MP3 clip to WAV
        clip_wav = TEMP_DIR / f"clip_{i:02d}_{step_id}.wav"
        convert_to_wav(clip_mp3, clip_wav)
        segments.append(clip_wav)

        clip_dur = durations.get(step_id, 0.0)
        current_time += clip_dur

    # Write ffmpeg concat manifest
    with open(concat_list_file, "w", encoding="utf-8") as f:
        for seg in segments:
            # ffmpeg concat demuxer requires escaped single quotes or forward slashes
            safe_path = str(seg.resolve()).replace("\\", "/")
            f.write(f"file '{safe_path}'\n")

    # Run ffmpeg concat
    concat_cmd = [
        "ffmpeg", "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", str(concat_list_file),
        "-c:a", "pcm_s16le",
        str(OUTPUT_AUDIO_FILE)
    ]
    subprocess.run(concat_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    print(f"[Audio] Padded audio saved: {OUTPUT_AUDIO_FILE.name} (Total: {current_time:.2f}s)")


def main():
    if not (NARRATION_FILE.exists() and DURATIONS_FILE.exists() and TIMINGS_FILE.exists()):
        print(
            "Error: Required files (narration.json, durations.json, timings.json) missing!\n"
            "Ensure you have run gen_audio.py and record.js first.",
            file=sys.stderr
        )
        sys.exit(1)

    with open(NARRATION_FILE, "r", encoding="utf-8") as f:
        steps = json.load(f)
    with open(DURATIONS_FILE, "r", encoding="utf-8") as f:
        durations = json.load(f)
    with open(TIMINGS_FILE, "r", encoding="utf-8") as f:
        timings = json.load(f)

    generate_srt(steps, durations, timings)
    assemble_padded_audio(steps, durations, timings)
    print("[Done] Asset preparation complete.")


if __name__ == "__main__":
    main()
