"""
Lo-Fi Merger v1 — Batch track merging.
Cuts silence, normalizes volume (2-pass loudnorm) and applies crossfades
to all tracks from the merger_tracks folder into one large mix.
"""
import os
import sys
import re
import json
import subprocess
import time
import shutil
import psutil
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor, as_completed
from pathlib import Path

# Windows encoding fix
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except AttributeError:
        pass

# ============== SETTINGS ==============
SILENCE_THRESH = -40         # Silence threshold (dB). Relaxed to catch background noise.
MIN_SILENCE_LEN = 0.5        # Min silence length (sec). Captures shorter pauses.
QUIET_THRESH = -30           # "Quiet sound" threshold (dB). Tails/intros below this will be cut with silence.
QUIET_SCAN_STEP = 0.5        # Scanning step for quiet tails (seconds).
FADE_SEC = 1.0               # Crossfade length (sec).

# Artifact Filters (Auto-declick)
CLICK_REMOVAL = True         # Auto-remove clicks using built-in FFmpeg filter
ADECLICK_WINDOW = 55         # Window size in ms
ADECLICK_OVERLAP = 75        # Window overlap (%)
ADECLICK_THRESHOLD = 2       # Detection threshold (lower is more aggressive)
ADECLICK_BURST = 2           # How many adjacent samples count as a click

# Loudness Settings
NORMALIZE_AUDIO = False      # Enable/disable loudness normalization
TARGET_LOUDNESS = -14.0      
OUTPUT_BITRATE = "192k"
NUM_WORKERS = 6              # Number of parallel processes


PROJECT_ROOT = Path(__file__).parent.parent
FFMPEG = str(PROJECT_ROOT / "bin" / "ffmpeg.exe") if (PROJECT_ROOT / "bin" / "ffmpeg.exe").exists() else "ffmpeg"
FFPROBE = str(PROJECT_ROOT / "bin" / "ffprobe.exe") if (PROJECT_ROOT / "bin" / "ffprobe.exe").exists() else "ffprobe"
TEMP_DIR = PROJECT_ROOT / "_temp_merger_chunks"


def fmt(seconds):
    """Formats seconds into HH:MM:SS string."""
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h:02}:{m:02}:{s:02}"


def safe_run(cmd, **kwargs):
    """Runs a subprocess with retries and memory checks."""
    max_retries = 5
    for attempt in range(max_retries):
        mem = psutil.virtual_memory()
        if mem.available < 400 * 1024 * 1024:
            time.sleep(2)
            continue
            
        try:
            return subprocess.run(cmd, **kwargs)
        except OSError as e:
            if getattr(e, 'winerror', None) == 1455 and attempt < max_retries - 1:
                time.sleep(3 + attempt * 2)
                continue
            raise e
        except MemoryError:
            print("  ⚠️ MemoryError. Waiting 5 sec...")
            time.sleep(5)
            continue
    return subprocess.run(cmd, **kwargs)


def get_duration(filepath):
    """Returns duration and metadata of audio file using ffprobe."""
    cmd = [FFPROBE, "-v", "quiet", "-print_format", "json",
           "-show_format", "-show_streams", str(filepath)]
    r = safe_run(cmd, capture_output=True, text=True, encoding="utf-8")
    
    if not r.stdout or not r.stdout.strip():
        cmd_retry = [FFPROBE, "-v", "error", "-print_format", "json",
                     "-show_format", "-show_streams", str(filepath)]
        r = safe_run(cmd_retry, capture_output=True, text=True, encoding="utf-8")
        if not r.stdout or not r.stdout.strip():
            return {"duration": 0, "bitrate": 0, "channels": 2, "sample_rate": "44100"}
    
    try:
        info = json.loads(r.stdout)
    except json.JSONDecodeError:
        return {"duration": 0, "bitrate": 0, "channels": 2, "sample_rate": "44100"}
    
    return {
        "duration": float(info["format"]["duration"]),
        "bitrate":  int(info["format"].get("bit_rate", 0)) // 1000,
        "channels": info["streams"][0].get("channels", 2),
        "sample_rate": int(info["streams"][0].get("sample_rate", "44100")),
    }


def check_ffmpeg():
    """Checks if ffmpeg and ffprobe are available."""
    if not shutil.which(FFMPEG) or not shutil.which(FFPROBE):
        print(f"Error: {FFMPEG} or {FFPROBE} not found.")
        print("Install FFmpeg or put ffmpeg.exe/ffprobe.exe in the project folder.")
        sys.exit(1)


def scan_joints_for_clicks(filepath, time_map):
    """Scans splicing joints in final file and reports jump intensity."""
    if not time_map or len(time_map) < 2:
        return
    
    meta = get_duration(filepath)
    sr = meta.get("sample_rate", 44100)
    
    print(f"\n🔍 Scanning joints for clicks (post-render scan, {sr}Hz)...")
    
    window = 0.05 
    for i in range(len(time_map) - 1):
        joint = time_map[i]["output_end"]
        temp_raw = TEMP_DIR / f"joint_test_{i}.raw"
        start = max(0, joint - window)
        cmd = [
            FFMPEG, "-y", "-hide_banner", "-nostats",
            "-ss", str(start), "-t", str(window * 2),
            "-i", str(filepath),
            "-f", "s16le", "-ac", "1", "-ar", str(sr), str(temp_raw)
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        
        if temp_raw.exists():
            try:
                with open(temp_raw, "rb") as f:
                    data = f.read()
                    import struct
                    num_samples = len(data) // 2
                    if num_samples < 2: continue
                    
                    samples = struct.unpack(f"<{num_samples}h", data)
                    
                    max_jump = 0
                    for j in range(1, len(samples)):
                        jump = abs(samples[j] - samples[j-1]) / 32768.0
                        if jump > max_jump:
                            max_jump = jump
                    
                    status = "✅ OK" if max_jump < 0.15 else "⚠️ CLICK?"
                    print(f"  Joint {i+1} ({fmt(joint)}): jump {max_jump:.3f} {status}")
            except Exception as e:
                print(f"  Error scanning joint {i+1}: {e}")
            finally:
                temp_raw.unlink(missing_ok=True)


def _run_silencedetect_chunk(filepath, start_sec, duration_sec, chunk_id):
    """Internal helper to detect silence in a chunk of file."""
    cmd = [
        FFMPEG, "-hide_banner", "-nostats",
        "-ss", str(start_sec), "-t", str(duration_sec),
        "-i", str(filepath),
        "-af", f"silencedetect=noise={SILENCE_THRESH}dB:d={MIN_SILENCE_LEN}",
        "-f", "null", "-"
    ]
    result = safe_run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    
    silences = []
    current_start = None
    for line in result.stderr.split('\n'):
        if "silence_start:" in line:
            m = re.search(r'silence_start:\s*([\d.]+)', line)
            if m:
                current_start = float(m.group(1)) + start_sec
        if "silence_end:" in line:
            m_end = re.search(r'silence_end:\s*([\d.]+)', line)
            m_dur = re.search(r'silence_duration:\s*([\d.]+)', line)
            if m_end and current_start is not None:
                end = float(m_end.group(1)) + start_sec
                dur = float(m_dur.group(1)) if m_dur else end - current_start
                silences.append((current_start, end, dur))
                current_start = None
    
    return chunk_id, silences, duration_sec


def detect_silences_parallel(filepath, total_duration):
    """Parallelized silence detection."""
    chunk_len = total_duration / NUM_WORKERS
    tasks = []
    for i in range(NUM_WORKERS):
        start = i * chunk_len
        dur = chunk_len + MIN_SILENCE_LEN * 2 if i < NUM_WORKERS - 1 else total_duration - start
        tasks.append((filepath, start, dur, i))
    
    all_silences = []
    
    with ThreadPoolExecutor(max_workers=NUM_WORKERS) as executor:
        futures = [executor.submit(_run_silencedetect_chunk, *t) for t in tasks]
        for future in as_completed(futures):
            _, chunk_silences, _ = future.result()
            all_silences.extend(chunk_silences)
    
    all_silences.sort(key=lambda x: x[0])
    merged = []
    for s in all_silences:
        if merged and s[0] <= merged[-1][1]:
            prev = merged[-1]
            merged[-1] = (prev[0], max(prev[1], s[1]), max(prev[1], s[1]) - prev[0])
        else:
            merged.append(s)
    return merged


def load_manual_cuts(filepath):
    """Loads manual silence markers from JSON files."""
    cuts = []
    pattern = f"{filepath.stem}_manual_cuts*.json"
    
    for manual_path in filepath.parent.glob(pattern):
        try:
            with open(manual_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                for c in data:
                    st = float(c["start"])
                    en = float(c["end"])
                    cuts.append((st, en, en - st))
        except Exception:
            pass
            
    return cuts


def get_combined_silences(filepath, dur, original_file):
    """Combines auto-detected silences with manual cuts."""
    silences = detect_silences_parallel(filepath, dur)
    manual = load_manual_cuts(original_file if original_file else filepath)
    if manual:
        silences.extend(manual)
        silences.sort(key=lambda x: x[0])
        merged = []
        for s in silences:
            if merged and s[0] <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], s[1]), max(merged[-1][1], s[1]) - merged[-1][0])
            else:
                merged.append(s)
        return merged
    return silences


def _get_rms_at(filepath, position, duration=0.5):
    """Gets average RMS volume level at specific position."""
    cmd = [
        FFMPEG, "-hide_banner", "-nostats",
        "-ss", str(max(0, position)), "-t", str(duration),
        "-i", str(filepath),
        "-af", "volumedetect",
        "-f", "null", "-"
    ]
    r = safe_run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    for line in r.stderr.split('\n'):
        if 'mean_volume' in line:
            m = re.search(r'([\-\d.]+)\s*dB', line)
            if m:
                return float(m.group(1))
    return -100.0


def expand_silence_zones(filepath, silences, total_duration):
    """Expands silence zones to capture low-volume tails."""
    if not silences:
        return []
    
    expanded = []
    for sil_start, sil_end, sil_dur in silences:
        new_start = sil_start
        while new_start > QUIET_SCAN_STEP:
            rms = _get_rms_at(filepath, new_start - QUIET_SCAN_STEP, QUIET_SCAN_STEP)
            if rms < QUIET_THRESH:
                new_start -= QUIET_SCAN_STEP
            else:
                break
        
        new_end = sil_end
        while new_end < total_duration - QUIET_SCAN_STEP:
            rms = _get_rms_at(filepath, new_end, QUIET_SCAN_STEP)
            if rms < QUIET_THRESH:
                new_end += QUIET_SCAN_STEP
            else:
                break
        
        expanded.append((new_start, new_end, new_end - new_start))
    
    merged = []
    for z in sorted(expanded, key=lambda x: x[0]):
        if merged and z[0] <= merged[-1][1]:
            prev = merged[-1]
            merged[-1] = (prev[0], max(prev[1], z[1]), max(prev[1], z[1]) - prev[0])
        else:
            merged.append(z)
    
    return merged


def _process_chunk(args):
    """Cut + normalize one chunk (separate process)."""
    idx, input_file, start, end, output_path, target_lufs = args
    duration = end - start
    
    if target_lufs is not None:
        cmd1 = [
            FFMPEG, "-y", "-hide_banner", "-nostats",
            "-ss", str(start), "-t", str(duration),
            "-i", str(input_file),
            "-af", f"loudnorm=I={target_lufs}:TP=-1.5:LRA=11:print_format=json",
            "-f", "null", "-"
        ]
        r1 = safe_run(cmd1, capture_output=True, text=True, encoding="utf-8", errors="replace")
        
        json_match = re.search(r'\{[^}]+\}', r1.stderr, re.DOTALL)
        if json_match:
            try:
                params = json.loads(json_match.group())
                measured_I = params.get("input_i", "-24.0")
                measured_TP = params.get("input_tp", "-1.0")
                measured_LRA = params.get("input_lra", "7.0")
                measured_thresh = params.get("input_thresh", "-34.0")
                target_offset = params.get("target_offset", "0.0")
                
                af = (f"loudnorm=I={target_lufs}:TP=-1.5:LRA=11:"
                      f"measured_I={measured_I}:measured_TP={measured_TP}:"
                      f"measured_LRA={measured_LRA}:measured_thresh={measured_thresh}:"
                      f"offset={target_offset}:linear=true")
            except (json.JSONDecodeError, KeyError):
                af = f"loudnorm=I={target_lufs}:TP=-1.5:LRA=11"
        else:
            af = f"loudnorm=I={target_lufs}:TP=-1.5:LRA=11"
    else:
        af = ""
    
    af_chain = af
    
    cmd2 = [
        FFMPEG, "-y", "-hide_banner", "-nostats",
        "-ss", str(start), "-t", str(duration),
        "-i", str(input_file),
    ]
    if af_chain:
        cmd2.extend(["-af", af_chain])
        
    cmd2.extend([
        "-c:a", "pcm_s16le",
        str(output_path)
    ])
    safe_run(cmd2, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return idx


def assemble_mega_mix(all_segments, output_filename):
    """Main assembly process for mega-mix."""
    print("\n" + "=" * 55)
    print("   🔧 ASSEMBLING AND PROCESSING MEGA-MIX")
    print("=" * 55)
    
    TEMP_DIR.mkdir(exist_ok=True)
    
    # 1. Parallel cutting + normalization
    print(f"\n📍 Step 1: Cutting + Normalization ({len(all_segments)} segments, {NUM_WORKERS} threads)...")
    tasks = []
    lufs_target = TARGET_LOUDNESS if NORMALIZE_AUDIO else None
    
    for i, seg in enumerate(all_segments):
        out = TEMP_DIR / f"chunk_{i:04d}.wav"
        tasks.append((i, str(seg["working_file"]), seg["start"], seg["end"], str(out), lufs_target))
        seg["chunk_path"] = str(out)
        
    t0 = time.time()
    completed_count = 0
    
    with ProcessPoolExecutor(max_workers=NUM_WORKERS) as executor:
        futures = {executor.submit(_process_chunk, t): t[0] for t in tasks}
        for future in as_completed(futures):
            future.result()
            completed_count += 1
            pct = completed_count / len(tasks) * 100
            elapsed = time.time() - t0
            eta = elapsed / completed_count * (len(tasks) - completed_count)
            filled = int(30 * pct / 100)
            bar = "█" * filled + "░" * (30 - filled)
            print(f"\r  [{bar}] {pct:5.1f}% | {completed_count}/{len(tasks)} | ETA: {fmt(eta)}   ", end="", flush=True)
            
    print(f"\n📍 Step 2: Validating chunks...")
    valid_chunks = []
    for seg in all_segments:
        wav_file = Path(seg["chunk_path"])
        if wav_file.exists():
            actual_dur = get_duration(wav_file).get("duration", 0)
            if actual_dur > 0.1:
                seg["actual_dur"] = actual_dur
                valid_chunks.append(seg)
                
    if not valid_chunks:
        print("Error: No valid chunks created!")
        return

    # 3. Seamless splicing
    print(f"\n📍 Step 3: Seamless splicing ({len(valid_chunks)} fragments)...")
    print(f"  (Real crossfade {FADE_SEC}s between tracks)")
    
    t0 = time.time()
    cmd = [FFMPEG, "-y", "-hide_banner"]
    for seg in valid_chunks:
        cmd.extend(["-i", seg["chunk_path"]])
        
    filter_parts = []
    last_out = "0:a"
    for i in range(1, len(valid_chunks)):
        out_pad = f"a{i}"
        dur1 = valid_chunks[i-1]["actual_dur"]
        dur2 = valid_chunks[i]["actual_dur"]
        
        safe_fade = min(FADE_SEC, dur1 / 2.0, dur2 / 2.0)
        fade_dur = round(max(0.01, safe_fade), 3)
        
        filter_parts.append(f"[{last_out}][{i}:a]acrossfade=d={fade_dur}:c1=qsin:c2=qsin[{out_pad}]")
        last_out = out_pad
        
    # Artifact filters: adeclick + adeclip
    click_filter = (f"adeclick=w={ADECLICK_WINDOW}:o={ADECLICK_OVERLAP}:b={ADECLICK_BURST}:t={ADECLICK_THRESHOLD},"
                    f"adeclip")
    
    if filter_parts:
        if CLICK_REMOVAL:
            filter_parts.append(f"[{last_out}]{click_filter}[final_out]")
            last_out = "final_out"
            
        filter_graph = ";".join(filter_parts)
        cmd.extend(["-filter_complex", filter_graph, "-map", f"[{last_out}]"])
    else:
        if CLICK_REMOVAL:
            cmd.extend(["-af", click_filter])
        else:
            cmd.extend(["-map", "0:a"])
    
    print(f"\n📍 Step 4: Exporting final MP3 (final render)...")
    if CLICK_REMOVAL:
        print(f"  ✨ Applying anti-click filter: threshold={ADECLICK_THRESHOLD}")

    output_path = PROJECT_ROOT / output_filename
    cmd.extend(["-b:a", OUTPUT_BITRATE, str(output_path)])
    
    process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
    
    total_dur_render = sum(c["actual_dur"] for c in valid_chunks) - (len(valid_chunks) - 1) * FADE_SEC
    print(f"  Exporting mix length {fmt(total_dur_render)}...")
    
    render_start = time.time()
    while True:
        line = process.stderr.readline()
        if not line: break
        if "time=" in line:
            m = re.search(r'time=([\d:.]+)', line)
            if m:
                cur_time_str = m.group(1)
                try:
                    parts = cur_time_str.split(':')
                    cur_sec = float(parts[0])*3600 + float(parts[1])*60 + float(parts[2])
                    pct = min(99.9, (cur_sec / total_dur_render) * 100)
                    
                    elapsed = time.time() - render_start
                    eta_str = "--:--"
                    if pct > 1.0:
                        eta_sec = (elapsed / pct) * (100 - pct)
                        eta_str = fmt(eta_sec)
                    
                    print(f"\r    Processed: {cur_time_str} | {pct:5.1f}% | ETA: ~{eta_str}", end="", flush=True)
                except: pass
    process.wait()

    print(f"\n  ✅ Splicing and export finished in {fmt(time.time() - t0)}")

    # Time map and debug log
    time_map = []
    output_pos = 0.0
    for i, seg in enumerate(valid_chunks):
        seg_dur = seg["actual_dur"]
        overlap_dur = 0.0
        if i > 0:
            prev_dur = valid_chunks[i-1]["actual_dur"]
            overlap_dur = min(FADE_SEC, prev_dur / 2.0, seg_dur / 2.0)
            
        output_pos -= overlap_dur
        output_pos = max(0.0, output_pos)
        
        time_map.append({
            "output_start": output_pos,
            "output_end": output_pos + seg_dur,
            "output_start_fmt": fmt(output_pos),
            "output_end_fmt": fmt(output_pos + seg_dur),
            "original_start": seg["start"],
            "original_end": seg["end"],
            "original_start_fmt": fmt(seg["start"]),
            "original_end_fmt": fmt(seg["end"]),
            "segment_dur": round(seg_dur, 2),
            "source_track": seg["original_file"].name
        })
        output_pos += seg_dur

    print(f"\n📍 Step 5: Scanning joints for clicks...")
    scan_joints_for_clicks(output_path, time_map)

    # Save stats log
    debug_path = PROJECT_ROOT / "logs" / f"debug_merger_{time.strftime('%Y%m%d_%H%M%S')}.json"
    final_meta = get_duration(output_path)
    debug_log = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source_file": "MULTIPLE_TRACKS",
        "output_file": str(output_filename),
        "source_duration": output_pos,
        "time_map": time_map,
        "final_stats": {
            "output_duration": final_meta["duration"],
            "adeclick_active": CLICK_REMOVAL,
            "adeclick_threshold": ADECLICK_THRESHOLD
        }
    }
    with open(debug_path, "w", encoding="utf-8") as f:
        json.dump(debug_log, f, ensure_ascii=False, indent=2)
        
    print(f"\n🎉 Mega-mix ready! Saved: {output_path}")
    shutil.rmtree(TEMP_DIR, ignore_errors=True)
    
    try:
        if os.name == 'nt':
            os.startfile(str(output_path))
        elif sys.platform.startswith('linux'):
            subprocess.Popen(['xdg-open', str(output_path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif sys.platform == 'darwin':
            subprocess.Popen(['open', str(output_path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


if __name__ == "__main__":
    check_ffmpeg()
    folder = PROJECT_ROOT / "merger_tracks"
    if not folder.exists():
        folder.mkdir()
        print(f"Folder '{folder}' created. Put your MP3/FLAC files there for merging.")
        sys.exit(0)
    
    files = list(folder.glob("*.mp3")) + list(folder.glob("*.flac"))
    files.sort(key=lambda x: x.name) 
    
    if len(files) < 2:
        print(f"Folder '{folder}' must contain at least 2 files to merge!")
        sys.exit(0)
        
    print("\n" + "=" * 42)
    print(f"🎵 MEGAMIX MODE: Found files: {len(files)}")
    print("=" * 42)
    
    TEMP_DIR.mkdir(exist_ok=True)
    all_segments = []
    
    for idx, input_file in enumerate(files):
        print(f"\n[{idx+1}/{len(files)}] Preparing: {input_file.name}")
        
        # Convert to FLAC if MP3
        if input_file.suffix.lower() == ".mp3":
            working_file = TEMP_DIR / f"source_{idx:03d}.flac"
            cmd = [
                FFMPEG, "-y", "-hide_banner", 
                "-err_detect", "ignore_err", 
                "-i", str(input_file), 
                "-c:a", "flac", 
                str(working_file)
            ]
            safe_run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            working_file = input_file
            
        dur = get_duration(working_file)["duration"]
        
        # Scan silence
        raw_silences = get_combined_silences(working_file, dur, input_file)
        silences = expand_silence_zones(working_file, raw_silences, dur)
        
        # Filter segments
        segments = []
        prev_end = 0.0
        for start, end, d in silences:
            if start - prev_end > 0.1:
                segments.append((prev_end, start))
            prev_end = end
        if dur - prev_end > 0.1:
            segments.append((prev_end, dur))
        
        if not segments and dur > 0.1:
            segments = [(0.0, dur)]
            
        print(f"  -> Found {len(segments)} valid segments.")
        
        for (seg_start, seg_end) in segments:
            all_segments.append({
                "original_file": input_file,
                "working_file": working_file,
                "start": seg_start,
                "end": seg_end
            })

    # Assemble final mix
    out_name = f"[MEGAMIX] Compiled_{time.strftime('%Y%m%d_%H%M%S')}.mp3"
    assemble_mega_mix(all_segments, out_name)
    
    # Final cleanup
    for seg in all_segments:
        wf = Path(seg["working_file"])
        if "source_" in wf.name:
            try:
                wf.unlink(missing_ok=True)
            except:
                pass