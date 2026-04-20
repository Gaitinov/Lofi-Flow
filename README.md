# LoFi Mixer & Audio Analysis Tools

A comprehensive suite of tools for processing audio files, detecting precise music repeats/loops, filtering out silences, and merging multiple audio tracks into a seamless megamix. Features advanced acoustic fingerprinting (multi-band bass & melody analysis) to detect repeats and visually interactive HTML reporting tools.

## 💡 About this Project

LoFi Mixer is designed to be a **lightweight, vanilla project**. It avoids heavy frameworks or complex build systems intentionally. The Python backend is simple and modular, and the frontend viewers are built completely with vanilla HTML, CSS, and JS. No bundlers or Node.js required—just open the `.html` files in your browser.

## 📂 Project Structure

```text
LoFi_Mixer/
├── 1_Process_Single_Track.bat     # Easy-to-use launch scripts
├── 2_Merge_Multiple_Tracks.bat
├── 3_Find_Repeats_in_File.bat
├── README.md
├── debug_viewer.html              # Vanilla Web UI: Artifact checker / Manual cutter
├── repeats_viewer.html            # Vanilla Web UI: Visual timeline for repeat copies
├── mixer_settings.json            # Main configuration (thresholds, bitrate, etc)
├── scripts/                       # Core python backend scripts
│   ├── config_loader.py
│   ├── find_repeats.py            # High precision audio fingerprinting logic
│   ├── merge_tracks.py            # Mixing multiple processed tracks logic
│   └── run_mixer.py               # Main silence drop / padding application logic
├── lofi_tracks/                   # Directory to place source audio tracks
└── logs/                          # Directory for export reports (.json)
```

## Features

- **High-Precision Repeat Detection**: Uses a dual-band acoustic analysis (bass/drums and mid-range/melody) to find repeated song loops and duplicate beats, avoiding false positives.
- **Smart Silence Trimming**: Detects and drops prolonged silences between tracks or within mixed files to create a seamless continuous mix.
- **Visual Web Reports**: 
  - `debug_viewer.html` - Interactive UI to view and listen to cut points, add manual cuts, and check for audio artifacts (clicks/pops).
  - `repeats_viewer.html` - Visual timeline for identified loops/repeats with side-by-side original and processed playback comparison.
- **Automated Megamix Generation**: Merges processed tracks together without jarring gaps, keeping audio levels consistent.

## Prerequisites

- **Python 3.8+**
- **FFmpeg & FFprobe**: Must be installed and accessible in the system `PATH` or placed inside the local `./bin` directory of the project.
- Dependencies: `numpy`, `psutil`

Install Python dependencies:
```bash
pip install -r requirements.txt
```

## Usage

1. **Find Repeats**: 
   Run the high-precision detection tool via GUI file selection:
   ```bash
   python scripts/find_repeats.py
   ```
   This will analyze an audio file, generate a `.json` report in the `logs/` folder, and automatically open `repeats_viewer.html` in your browser.

2. **Run Automatic Mixing / Trimming**:
   ```bash
   python scripts/run_mixer.py
   ```

3. **Merge Tracks**:
   To combine multiple segments into one continuous mix:
   ```bash
   python scripts/merge_tracks.py
   ```

4. **Review Visual Reports**:
   Open `debug_viewer.html` or `repeats_viewer.html` directly in your browser. The viewers remember your language preferences and last analyzed files. Both UI interfaces support Full English and Russian localization.

## Configuration

Core settings are managed via `mixer_settings.json`. Adjust thresholds for silence detection, export bitrate, and crossfade padding here.

```json
{
  "ffmpeg_path": "bin/ffmpeg.exe",
  "ffprobe_path": "bin/ffprobe.exe",
  "silence_db": -40,
  "silence_duration": 1.5,
  ...
}
```
