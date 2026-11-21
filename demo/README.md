# Project Venessa — Demo Video Pipeline

Automated high-fidelity demonstration video recording pipeline for **Project Venessa** (Pre-Mortem Simulator & Architecture Hardening Engine).

This pipeline orchestrates:
1. **AI Voiceover Synthesis** (`edge-tts` with Microsoft's `en-US-AndrewNeural` voice).
2. **Precise Duration Analysis & Validation** (`ffprobe` verifying the strict `< 3:00` / `~2:40` constraint).
3. **Automated Headless/Headed Screen Recording** (`Playwright` Chromium at 1080p with smooth cursor interpolation and realistic typing).
4. **Silence-Padded Audio Muxing & Caption Burn-in** (`ffmpeg` generating `demo.mp4` and `demo_with_captions.mp4`).

---

## Architecture & Directory Contents

All pipeline assets reside in this isolated `/demo` directory:

```
demo/
├── narration.json         # 12-step narration script (389 words, structured problem → flow → impact)
├── gen_audio.py           # TTS generator + duration calculator + strict length enforcer (< 170s)
├── record.js              # Playwright test recording script with injected fake cursor & timings logger
├── build_assets.py        # Subtitle generator (.srt) & silence-padded audio assembler (.wav)
├── build.sh               # Unix/macOS build script (ffmpeg remuxing & subtitle burn-in)
├── build.bat              # Windows batch build script
├── package.json           # Node.js dependencies (Playwright)
├── requirements.txt       # Python dependencies (edge-tts)
├── README.md              # Pipeline documentation & run order
│
├── audio/                 # Generated per-step MP3 voice clips (created by gen_audio.py)
├── durations.json         # Clip durations in seconds measured by ffprobe
├── timings.json           # Start timestamp for each step logged by record.js
├── auth.json              # Pre-flight session state so initialization is never recorded
└── recordings/            # Raw Playwright video captures (.webm)
```

---

## Video Specifications & Constraints

- **Target Duration:** ~2:40 (Strict ceiling: `< 3:00` / `170s`)
- **Narration Pace:** ~150 words per minute across 12 discrete steps (389 words total).
- **Resolution:** 1920 × 1080 (Full HD, 16:9).
- **Video Codec:** H.264 (`libx264`, `yuv420p`, CRF 20, preset medium).
- **Audio Codec:** AAC 192 kbps stereo.
- **Voice Actor:** `en-US-AndrewNeural` (Microsoft Edge TTS).

---

## Prerequisites

1. **System Tools:**
   - `ffmpeg` and `ffprobe` installed and accessible on system `PATH`.
   - Node.js (v18+) and npm.
   - Python (3.10+).

2. **Web Application Running Locally:**
   - **Backend API:** `http://localhost:8080`
     ```bash
     cd ../backend
     uvicorn app.main:app --reload --port 8080
     ```
   - **Frontend UI:** `http://localhost:5173`
     ```bash
     cd ../frontend
     npm run dev
     ```

---

## Step-by-Step Run Order

### 1. Install Demo Dependencies

From within the `demo` directory:

```bash
cd demo

# Install Python requirements (edge-tts)
pip install -r requirements.txt

# Install Node requirements & Chromium browser for Playwright
npm install
npx playwright install chromium
```

---

### 2. Generate Audio & Validate Constraints (`gen_audio.py`)

Run the voice synthesis script:

```bash
python gen_audio.py
```

**What it does:**
- Downloads natural-sounding neural speech for all 12 steps into `audio/*.mp3`.
- Uses `ffprobe` to determine each clip's duration down to the millisecond.
- Calculates total audio duration plus the 0.5s inter-step pauses.
- **Constraint check:** If the total projected duration exceeds `170 seconds`, it aborts with an error so you can trim narration *before* recording.
- Outputs `durations.json`.

---

### 3. Record User Flow (`record.js`)

Ensure the web application is running at `http://localhost:5173`, then record:

```bash
node record.js
```

*(Optional: Set a custom URL with `APP_URL=http://localhost:3000 node record.js`)*

**Features implemented in `record.js`:**
- **Pre-flight setup:** Performs a pre-flight navigation to save `auth.json` so page loading or login is never recorded.
- **Visual Fake Cursor:** Injects a stylized coral-red glowing cursor dot that smoothly tracks mouse coordinates.
- **Smooth Mouse Trajectories:** Interpolates mouse movements with 25 intermediate steps before clicks.
- **Realistic Typing:** Simulates keyboard input with a 60ms delay.
- **Audio-Synced Timing:** Keeps the browser on each step for `durations[stepId] + 0.5s` and logs exact start timestamps to `timings.json`.
- Saves raw 1080p video into `recordings/*.webm`.

---

### 4. Build Final Muxed Video (`build.sh` or `build.bat`)

Assemble the final video and captions:

- **Linux / macOS / Git Bash:**
  ```bash
  chmod +x build.sh
  ./build.sh
  ```

- **Windows Command Prompt / PowerShell:**
  ```cmd
  build.bat
  ```

**What it does:**
1. Calls `build_assets.py` to create `captions.srt` and inserts exact silence padding between audio clips so each spoken line matches `timings.json`.
2. Assembles `full_narration.wav`.
3. Muxes the Playwright recording with the synchronized audio track into `demo.mp4`.
4. Burns styled subtitles into `demo_with_captions.mp4`.

---

## Generated Outputs

| File | Description |
|---|---|
| `demo/demo.mp4` | High-definition master video with synchronized audio voiceover. |
| `demo/demo_with_captions.mp4` | Master video with burned-in subtitles for social sharing or presentations. |
| `demo/captions.srt` | Standalone SubRip subtitle file for YouTube or video players. |
| `demo/timings.json` | Recorded timestamp log of each UI step. |
| `demo/durations.json` | Exact measured duration of each narration clip. |
