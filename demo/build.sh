#!/usr/bin/env bash
# build.sh: Mux recorded Playwright video with synchronized narration audio into demo.mp4.
# Optionally produces demo_with_captions.mp4 with burned-in subtitles.

set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

echo "=========================================================="
echo " Project Venessa - Video Assembly Pipeline (build.sh)"
echo "=========================================================="

# Check prerequisites
command -v ffmpeg >/dev/null 2>&1 || { echo "Error: ffmpeg is required but not installed." >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || command -v python >/dev/null 2>&1 || { echo "Error: python is required." >&2; exit 1; }

PYTHON_BIN="python3"
if ! command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN="python"
fi

# Step 1: Build padded audio and SRT captions
echo ""
echo ">> Step 1: Aligning audio and generating captions..."
$PYTHON_BIN build_assets.py

# Step 2: Locate the recorded video from Playwright
RECORDINGS_DIR="$DIR/recordings"
if [ ! -d "$RECORDINGS_DIR" ]; then
  echo "Error: recordings directory not found. Run record.js first." >&2
  exit 1
fi

LATEST_VIDEO=$(ls -t "$RECORDINGS_DIR"/*.webm 2>/dev/null | head -n 1)
if [ -z "$LATEST_VIDEO" ]; then
  echo "Error: No .webm recording found in $RECORDINGS_DIR. Run record.js first." >&2
  exit 1
fi

echo ">> Found recorded video: $LATEST_VIDEO"

# Step 3: Mux video and audio into final demo.mp4 (H.264 / AAC)
echo ""
echo ">> Step 2: Muxing video and audio into demo.mp4..."
ffmpeg -y \
  -i "$LATEST_VIDEO" \
  -i "$DIR/full_narration.wav" \
  -c:v libx264 \
  -pix_fmt yuv420p \
  -preset medium \
  -crf 20 \
  -c:a aac \
  -b:a 192k \
  -shortest \
  "$DIR/demo.mp4"

echo ">> Successfully built: $DIR/demo.mp4"

# Step 4: Burn in captions (optional artifact)
echo ""
echo ">> Step 3: Burning subtitles into demo_with_captions.mp4..."
ffmpeg -y \
  -i "$DIR/demo.mp4" \
  -vf "subtitles=captions.srt:force_style='FontSize=18,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BackColour=&H80000000,BorderStyle=4,MarginV=30'" \
  -c:a copy \
  "$DIR/demo_with_captions.mp4"

echo ">> Successfully built: $DIR/demo_with_captions.mp4"
echo ""
echo "=========================================================="
echo " Pipeline Complete!"
echo " Outputs:"
echo "   - demo.mp4 (Clean video + narration)"
echo "   - demo_with_captions.mp4 (Burned-in captions)"
echo "   - captions.srt (SubRip subtitles)"
echo "=========================================================="
