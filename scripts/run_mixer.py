"""
Lo-Fi Mixer v5 — Parallel version with volume analysis.
FFmpeg direct + multithreading + volume normalization.
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
from config_loader import S

# Windows encoding fix
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except AttributeError:
        pass

from config_loader import S

# ============== SETTINGS (from config_loader) ==============
SILENCE_THRESH = S["silence"]["thresh_db"]
MIN_SILENCE_LEN = S["silence"]["min_len_sec"]
QUIET_THRESH = S["silence"]["quiet_thresh_db"]
QUIET_SCAN_STEP = S["silence"]["quiet_scan_step"]

FADE_SEC = S["mixing"]["fade_sec"]
NORMALIZE_AUDIO = S["mixing"]["normalize_audio"]
TARGET_LOUDNESS = S["mixing"]["target_loudness_lufs"]
OUTPUT_BITRATE = S["mixing"]["output_bitrate"]

CLICK_REMOVAL = S["artifact_removal"]["enabled"]
ADECLICK_WINDOW = S["artifact_removal"]["window_ms"]
ADECLICK_OVERLAP = S["artifact_removal"]["overlap_pct"]
ADECLICK_THRESHOLD = S["artifact_removal"]["threshold"]
ADECLICK_BURST = S["artifact_removal"]["burst"]

NUM_WORKERS = S["system"]["num_workers"]
# ==========================================================

PROJECT_ROOT = Path(__file__).parent.parent
FFMPEG = str(PROJECT_ROOT / "bin" / "ffmpeg.exe") if (PROJECT_ROOT / "bin" / "ffmpeg.exe").exists() else "ffmpeg"
FFPROBE = str(PROJECT_ROOT / "bin" / "ffprobe.exe") if (PROJECT_ROOT / "bin" / "ffprobe.exe").exists() else "ffprobe"
TEMP_DIR = PROJECT_ROOT / "_temp_chunks"


def fmt(seconds):
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h:02}:{m:02}:{s:02}"


def _normalize_name(name):
    """Removes emojis, Unicode special chars and collapses spaces for fuzzy name matching."""
    cleaned = re.sub(r'[^\w\s\[\](){}\-.,!#&\'а-яА-ЯёЁ]', '', name, flags=re.UNICODE)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned.lower()


def safe_run(cmd, **kwargs):
    """Safe process launch with memory check and WinError 1455 retries."""
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
    cmd = [FFPROBE, "-v", "quiet", "-print_format", "json",
           "-show_format", "-show_streams", str(filepath)]
    r = safe_run(cmd, capture_output=True, text=True, encoding="utf-8")
    
    if not r.stdout or not r.stdout.strip():
        cmd_retry = [FFPROBE, "-v", "error", "-print_format", "json",
                     "-show_format", "-show_streams", str(filepath)]
        r = safe_run(cmd_retry, capture_output=True, text=True, encoding="utf-8")
        if not r.stdout or not r.stdout.strip():
            print(f"  ⚠ ffprobe could not read file: {filepath}")
            if r.stderr:
                print(f"    Error: {r.stderr[:300]}")
            return {"duration": 0, "bitrate": 0, "channels": 2, "sample_rate": "44100"}
    
    try:
        info = json.loads(r.stdout)
    except json.JSONDecodeError:
        print(f"  ⚠ ffprobe returned invalid JSON: {r.stdout[:200]}")
        return {"duration": 0, "bitrate": 0, "channels": 2, "sample_rate": "44100"}
    
    return {
        "duration": float(info["format"]["duration"]),
        "bitrate":  int(info["format"].get("bit_rate", 0)) // 1000,
        "channels": info["streams"][0].get("channels", 2),
        "sample_rate": int(info["streams"][0].get("sample_rate", "44100")),
    }


def scan_joints_for_clicks(filepath, time_map):
    """Scans joints in the final file and outputs the jump strength to console."""
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
    chunk_len = total_duration / NUM_WORKERS
    tasks = []
    for i in range(NUM_WORKERS):
        start = i * chunk_len
        dur = chunk_len + MIN_SILENCE_LEN * 2 if i < NUM_WORKERS - 1 else total_duration - start
        tasks.append((filepath, start, dur, i))
    
    all_silences = []
    completed = 0
    t0 = time.time()
    
    print(f"  Parallel scan ({NUM_WORKERS} threads)...")
    
    with ThreadPoolExecutor(max_workers=NUM_WORKERS) as executor:
        futures = {executor.submit(_run_silencedetect_chunk, *t): t[3] for t in tasks}
        for future in as_completed(futures):
            chunk_id, chunk_silences, chunk_dur = future.result()
            all_silences.extend(chunk_silences)
            completed += 1
            elapsed = time.time() - t0
            pct = completed / NUM_WORKERS * 100
            eta = elapsed / completed * (NUM_WORKERS - completed) if completed > 0 else 0
            filled = int(30 * pct / 100)
            bar = "█" * filled + "░" * (30 - filled)
            print(f"\r  [{bar}] {pct:5.1f}% | {completed}/{NUM_WORKERS} | ETA: {fmt(eta)}   ", end="", flush=True)
    
    print(f"\r  [{'█' * 30}] 100.0% | Ready in {fmt(time.time() - t0)}                    ")
    
    all_silences.sort(key=lambda x: x[0])
    merged = []
    for s in all_silences:
        if merged and s[0] <= merged[-1][1]:
            prev = merged[-1]
            merged[-1] = (prev[0], max(prev[1], s[1]), max(prev[1], s[1]) - prev[0])
        else:
            merged.append(s)
    return merged


def _find_manual_cuts_files(filepath):
    """Finds JSON files with manual cuts: exact match first, then fuzzy."""
    found = []
    parent = filepath.parent
    stem = filepath.stem
    
    exact_pattern = f"{stem}_manual_cuts*.json"
    for p in parent.glob(exact_pattern):
        found.append(p)
    
    if found:
        return found
    
    norm_stem = _normalize_name(stem)
    print(f"  🔎 Exact match not found, trying fuzzy match (normalized: '{norm_stem[:50]}...')")
    
    for p in parent.glob("*_manual_cuts*.json"):
        json_base = p.name.split("_manual_cuts")[0]
        norm_json = _normalize_name(json_base)
        if norm_json == norm_stem:
            found.append(p)
            print(f"  ✅ Fuzzy match: {p.name}")
    
    return found


def load_manual_cuts(filepath):
    """Finds and loads all json files with manual cuts for the given track."""
    cuts = []
    
    manual_files = _find_manual_cuts_files(filepath)
    
    if not manual_files:
        return cuts
    
    for manual_path in manual_files:
        try:
            with open(manual_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                count = 0
                for c in data:
                    st = float(c["start"])
                    en = float(c["end"])
                    cuts.append((st, en, en - st))
                    count += 1
                print(f"  📥 Loaded manual cuts: {count} from {manual_path.name}")
        except Exception as e:
            print(f"  ⚠ Error reading manual cuts from {manual_path.name}: {e}")
            
    return cuts


def get_combined_silences(filepath, dur, original_file):
    """Gets auto-cuts and merges them with manual JSON cuts."""
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
    """Measures average loudness (RMS) at a specific point."""
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
    """Expands silence zones by capturing quiet track tails/intros."""
    if not silences:
        return []
    
    expanded = []
    print(f"  Expanding silence zones (capturing quiet tails < {QUIET_THRESH} dB)...")
    
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
        
        added = (new_start - sil_start) + (new_end - sil_end)
        if abs(added) > 0.1:
            print(f"    {fmt(sil_start)} | Silence {sil_dur:.1f}s → expanded to {new_end - new_start:.1f}s (tails: {abs(added):.1f}s)")
        
        expanded.append((new_start, new_end, new_end - new_start))
    
    merged = []
    for z in sorted(expanded, key=lambda x: x[0]):
        if merged and z[0] <= merged[-1][1]:
            prev = merged[-1]
            merged[-1] = (prev[0], max(prev[1], z[1]), max(prev[1], z[1]) - prev[0])
        else:
            merged.append(z)
    
    return merged


def _measure_volume_chunk(args):
    """Measures loudness of a single segment (parallel)."""
    idx, filepath, start, duration = args
    cmd = [
        FFMPEG, "-hide_banner", "-nostats",
        "-ss", str(start), "-t", str(duration),
        "-i", str(filepath),
        "-af", "volumedetect",
        "-f", "null", "-"
    ]
    r = safe_run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    mean_vol = max_vol = None
    for line in r.stderr.split('\n'):
        if 'mean_volume' in line:
            m = re.search(r'([-\d.]+)\s*dB', line)
            if m: mean_vol = float(m.group(1))
        if 'max_volume' in line:
            m = re.search(r'([-\d.]+)\s*dB', line)
            if m: max_vol = float(m.group(1))
    return idx, start, duration, mean_vol, max_vol


def analyze_volume_segments(filepath, total_duration, num_segments=12):
    """Splits track into N parts and measures loudness of each in parallel."""
    seg_len = total_duration / num_segments
    tasks = [(i, str(filepath), i * seg_len, seg_len) for i in range(num_segments)]
    
    results = [None] * num_segments
    completed = 0
    t0 = time.time()
    
    print(f"  Parallel volume map ({NUM_WORKERS} threads, {num_segments} segments)...")
    
    with ThreadPoolExecutor(max_workers=NUM_WORKERS) as executor:
        futures = {executor.submit(_measure_volume_chunk, t): t[0] for t in tasks}
        for future in as_completed(futures):
            idx, start, duration, mean_vol, max_vol = future.result()
            results[idx] = (start, duration, mean_vol, max_vol)
            completed += 1
            pct = completed / num_segments * 100
            elapsed = time.time() - t0
            eta = elapsed / completed * (num_segments - completed) if completed > 0 else 0
            filled = int(30 * pct / 100)
            bar = "█" * filled + "░" * (30 - filled)
            print(f"\r  [{bar}] {pct:5.1f}% | {completed}/{num_segments} | ETA: {fmt(eta)}   ", end="", flush=True)
    
    print(f"\r  [{'█' * 30}] 100.0% | Ready in {fmt(time.time() - t0)}                    ")
    return results


def check_ffmpeg():
    if not shutil.which(FFMPEG) or not shutil.which(FFPROBE):
        print(f"Error: {FFMPEG} or {FFPROBE} not found.")
        print("Install FFmpeg or place ffmpeg.exe/ffprobe.exe in the script folder.")
        sys.exit(1)


def backup_source_files(input_file):
    """Creates a backup copy of the original track and all JSON cut files."""
    manual_files = _find_manual_cuts_files(input_file)
    if not manual_files:
        return  

    backup_base = input_file.parent.parent / "sources"
    backup_dir = backup_base / input_file.stem
    backup_dir.mkdir(parents=True, exist_ok=True)
    
    copied_something = False
    
    dest_audio = backup_dir / input_file.name
    if not dest_audio.exists() or dest_audio.stat().st_size != input_file.stat().st_size:
        print(f"  💾 Backing up original: {input_file.name} -> sources/{input_file.stem}/")
        shutil.copy2(input_file, dest_audio)
        copied_something = True
        
    for manual_path in manual_files:
        dest_json = backup_dir / manual_path.name
        if not dest_json.exists() or dest_json.stat().st_mtime < manual_path.stat().st_mtime:
            print(f"  💾 Backing up markers: {manual_path.name}")
            shutil.copy2(manual_path, dest_json)
            copied_something = True
            
    if copied_something:
        print("  ✅ Backup is up to date.\n")


# ============================================
#                 ANALYZE
# ============================================
def analyze_track(filepath, original_file=None):
    print("\n" + "=" * 55)
    print("   🎵 TRACK ANALYSIS")
    print("=" * 55)
    
    meta = get_duration(filepath)
    dur = meta["duration"]
    
    print(f"  File:        {filepath.name}")
    print(f"  Duration: {fmt(dur)}")
    print(f"  Bitrate:     {meta['bitrate']} kbps | Channels: {meta['channels']} | SR: {meta['sample_rate']} Hz")
    
    print(f"\n{'─' * 55}")
    print(f"  📊 VOLUME MAP (Segments)")
    print(f"{'─' * 55}")
    
    vol_results = analyze_volume_segments(filepath, dur)
    
    volumes = [r[2] for r in vol_results if r and r[2] is not None]
    avg_vol = sum(volumes) / len(volumes) if volumes else 0
    min_vol = min(volumes) if volumes else 0
    max_vol_val = max(volumes) if volumes else 0
    vol_range = max_vol_val - min_vol
    
    print(f"\n  {'Time':>10} │ {'Volume':>10} │ Level Map")
    print(f"  {'─' * 10}─┼─{'─' * 10}─┼─{'─' * 28}")
    
    for r in vol_results:
        if r is None or r[2] is None:
            continue
        start, duration, mean_v, max_v = r
        if vol_range > 0:
            level = (mean_v - min_vol) / vol_range
        else:
            level = 0.5
        bar_len = int(level * 20)
        bar = "▓" * bar_len + "░" * (20 - bar_len)
        
        marker = " ⚠ QUIET" if mean_v < avg_vol - 3 else ""
        print(f"  {fmt(start):>10} │ {mean_v:>8.1f} dB │ {bar}{marker}")
    
    print(f"\n  Mean: {avg_vol:.1f} dB | Range: {vol_range:.1f} dB | Min: {min_vol:.1f} dB | Max: {max_vol_val:.1f} dB")
    
    if vol_range > 5:
        print(f"  ⚠ Dynamic range {vol_range:.1f} dB — headphones WILL disconnect in quiet parts!")
        print(f"  ➡ Normalization will align audio to {TARGET_LOUDNESS} LUFS.")
    else:
        print(f"  ✅ Dynamic range is acceptable ({vol_range:.1f} dB).")
    
    print(f"\n{'─' * 55}")
    print(f"  🔇 SCANNING FOR SILENCE ({SILENCE_THRESH} dB, min {MIN_SILENCE_LEN} sec)")
    print(f"{'─' * 55}")
    
    silences = get_combined_silences(filepath, dur, original_file)
    
    if silences:
        total_sil = sum(s[2] for s in silences)
        print(f"\n  Silences found: {len(silences)}")
        print(f"  Total silence: {fmt(total_sil)} ({total_sil:.1f} sec)")
        
        print(f"\n  Top-5 longest parts:")
        for i, (st, en, d) in enumerate(sorted(silences, key=lambda x: x[2], reverse=True)[:5]):
            print(f"    {i+1}. {fmt(st)} -> {fmt(en)} ({d:.1f} sec)")
    else:
        print(f"\n  No silences found below {SILENCE_THRESH} dB.")
    
    print("=======================================================")
    print("  PROCESSING PLAN:")
    
    if NORMALIZE_AUDIO:
        print(f"    1. Loudness normalisation → {TARGET_LOUDNESS} LUFS")
    else:
        print(f"    1. Loudness → LEAVE ORIGINAL (normalization OFF)")
        
    print(f"    2. Cutting {len(silences)} silences ({fmt(total_sil)} total)")
    print(f"    3. Smooth transitions (crossfades) {FADE_SEC} sec")
    print(f"{'=' * 55}")
    
    print("\n  Auto-start processing in 2 seconds...")
    time.sleep(2)
    return True


# ============================================
#                 PROCESS
# ============================================
def _process_chunk(args):
    """Slicing + normalizing a single chunk (parallel)."""
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



def process_mix(filepath, output_filename, original_file=None):
    proc_t0 = time.time()
    print("\n" + "=" * 55)
    print("   🔧 PROCESSING MIX")
    print("=" * 55)
    
    meta = get_duration(filepath)
    dur = meta["duration"]
    print(f"  File: {filepath.name} ({fmt(dur)})")
    
    print(f"\n📍 Step 1: Detect silence ({SILENCE_THRESH} dB) and manual cuts...")
    raw_silences = get_combined_silences(filepath, dur, original_file)
    print(f"  Absolute silences found (including manual): {len(raw_silences)}")
    
    print(f"\n📍 Step 2: Expand zones (capturing quiet tails < {QUIET_THRESH} dB)...")
    silences = expand_silence_zones(filepath, raw_silences, dur)
    
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
    
    total_sil = sum(s[2] for s in silences) if silences else 0
    print(f"\n  📊 Segments: {len(segments)} | Total cut: {fmt(total_sil)} (silence + quiet tails)")
    
    time_map = []
    output_pos = 0.0
    for i, (seg_start, seg_end) in enumerate(segments):
        seg_dur = seg_end - seg_start
        
        overlap_dur = 0.0
        if i > 0:
            prev_dur = segments[i-1][1] - segments[i-1][0]
            overlap_dur = min(FADE_SEC, prev_dur / 2.0, seg_dur / 2.0)
            
        output_pos -= overlap_dur
        output_pos = max(0.0, output_pos)
        
        time_map.append({
            "output_start": output_pos,
            "output_end": output_pos + seg_dur,
            "output_start_fmt": fmt(output_pos),
            "output_end_fmt": fmt(output_pos + seg_dur),
            "original_start": seg_start,
            "original_end": seg_end,
            "original_start_fmt": fmt(seg_start),
            "original_end_fmt": fmt(seg_end),
            "segment_dur": round(seg_dur, 2),
        })
        output_pos += seg_dur
    
    debug_log = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source_file": str(original_file).replace('\\', '/') if original_file else str(filepath).replace('\\', '/'),
        "output_file": str(output_filename),
        "source_duration": dur,

        "settings": {
            "SILENCE_THRESH": SILENCE_THRESH,
            "MIN_SILENCE_LEN": MIN_SILENCE_LEN,
            "QUIET_THRESH": QUIET_THRESH,
            "QUIET_SCAN_STEP": QUIET_SCAN_STEP,
            "FADE_SEC": FADE_SEC,
            "NORMALIZE_AUDIO": NORMALIZE_AUDIO,
            "TARGET_LOUDNESS": TARGET_LOUDNESS,
            "CLICK_REMOVAL": CLICK_REMOVAL,
            "ADECLICK_WINDOW": ADECLICK_WINDOW,
            "ADECLICK_OVERLAP": ADECLICK_OVERLAP,
            "ADECLICK_BURST": ADECLICK_BURST,
            "ADECLICK_THRESHOLD": ADECLICK_THRESHOLD,
        },
        "raw_silences": [{"start": s[0], "end": s[1], "dur": s[2], "start_fmt": fmt(s[0]), "end_fmt": fmt(s[1])} for s in raw_silences],
        "expanded_silences": [{"start": s[0], "end": s[1], "dur": s[2], "start_fmt": fmt(s[0]), "end_fmt": fmt(s[1])} for s in silences],
        "segments_kept": [{"start": s[0], "end": s[1], "dur": s[1]-s[0], "start_fmt": fmt(s[0]), "end_fmt": fmt(s[1])} for s in segments],
        "time_map": time_map,
        "total_silence_cut": total_sil,
    }
    base_name = original_file.stem if original_file else filepath.stem
    debug_path = PROJECT_ROOT / "logs" / f"debug_{base_name}_{time.strftime('%Y%m%d_%H%M%S')}.json"
    with open(debug_path, "w", encoding="utf-8") as f:
        json.dump(debug_log, f, ensure_ascii=False, indent=2)
    print(f"  📝 Debug log saved: {debug_path.name}")
    print(f"  📍 Time map (output → original): {len(time_map)} segments")

    
    TEMP_DIR.mkdir(exist_ok=True)
    print(f"\n📍 Step 3: Cutting + Normalization ({NUM_WORKERS} threads, 2-pass loudnorm)...")
    
    tasks = []
    lufs_target = TARGET_LOUDNESS if NORMALIZE_AUDIO else None
    for i, (seg_start, seg_end) in enumerate(segments):
        out = TEMP_DIR / f"chunk_{i:04d}.wav"
        tasks.append((i, str(filepath), seg_start, seg_end, str(out), lufs_target))
    
    t0 = time.time()
    completed_count = 0
    
    with ProcessPoolExecutor(max_workers=NUM_WORKERS) as executor:
        futures = {executor.submit(_process_chunk, t): t[0] for t in tasks}
        for future in as_completed(futures):
            future.result()
            completed_count += 1
            elapsed = time.time() - t0
            pct = completed_count / len(tasks) * 100
            eta = elapsed / completed_count * (len(tasks) - completed_count)
            filled = int(30 * pct / 100)
            bar = "█" * filled + "░" * (30 - filled)
            print(f"\r  [{bar}] {pct:5.1f}% | {completed_count}/{len(tasks)} | ETA: {fmt(eta)}   ", end="", flush=True)
    
    print(f"\n📍 Step 4: Validate chunks...")
    valid_chunks = []
    for i in range(len(segments)):
        wav_file = TEMP_DIR / f"chunk_{i:04d}.wav"
        if wav_file.exists():
            actual_dur = get_duration(wav_file).get("duration", 0)
            if actual_dur > 0.1:
                valid_chunks.append({
                    "path": str(wav_file).replace("\\", "/"),
                    "dur": actual_dur
                })
    if not valid_chunks:
        print("Error: No valid chunks created!")
        return
        
    interleaved = valid_chunks
    
    print(f"\n📍 Step 5: Seamless splicing ({len(valid_chunks)} fragments)...")
    print(f"  (Real crossfade {FADE_SEC}s between tracks)")
    
    t0 = time.time()
    
    cmd = [FFMPEG, "-y", "-hide_banner"]
    for chunk in interleaved:
        cmd.extend(["-i", chunk["path"]])
        
    filter_parts = []
    last_out = "0:a"
    for i in range(1, len(interleaved)):
        out_pad = f"a{i}"
        dur1 = interleaved[i-1]["dur"]
        dur2 = interleaved[i]["dur"]
        
        safe_fade = min(FADE_SEC, dur1 / 2.0, dur2 / 2.0)
        fade_dur = round(max(0.01, safe_fade), 3)
        
        filter_parts.append(f"[{last_out}][{i}:a]acrossfade=d={fade_dur}:c1=qsin:c2=qsin[{out_pad}]")
        last_out = out_pad
        
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
    
    print(f"\n📍 Step 6: Exporting final MP3 (rendering)...")
    if CLICK_REMOVAL:
        print(f"  ✨ Anti-click filter active: threshold={ADECLICK_THRESHOLD}")

    output_path = PROJECT_ROOT / output_filename
    cmd.extend(["-b:a", OUTPUT_BITRATE, str(output_path)])
    
    process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
    
    total_dur_render = sum(c["dur"] for c in interleaved) - (len(interleaved) - 1) * FADE_SEC
    print(f"  Exporting mix length: {fmt(total_dur_render)}...")
    
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


    print(f"\n📍 Step 7: Checking joints for clicks...")
    TEMP_DIR.mkdir(exist_ok=True)
    scan_joints_for_clicks(output_path, time_map)
    
    final_meta = get_duration(output_path)
    
    debug_log["final_stats"] = {
        "output_duration": final_meta["duration"],
        "total_silence_cut": total_sil,
        "adeclick_active": CLICK_REMOVAL,
        "adeclick_threshold": ADECLICK_THRESHOLD
    }
    with open(debug_path, "w", encoding="utf-8") as f:
        json.dump(debug_log, f, ensure_ascii=False, indent=2)
    
    print(f"\n{'=' * 55}")
    print(f"  Original:      {fmt(dur)}")
    print(f"  Result:        {fmt(final_meta['duration'])}")
    print(f"  Cut:           {fmt(total_sil)} silence")
    
    if NORMALIZE_AUDIO:
        print(f"  Normalization: {TARGET_LOUDNESS} LUFS (2-pass loudnorm)")
    else:
        print(f"  Normalization: OFF (original dynamics)")
        
    print(f"  Segments:      {len(segments)}")
    print(f"  File:          {output_path}")
    print(f"{'=' * 55}")
    print(f"  🎉 Done! Saved: {output_path}")
    print(f"  Total processing time: {fmt(time.time() - proc_t0)}")
    
    try:
        if os.name == 'nt':
            os.startfile(str(output_path))
        elif sys.platform.startswith('linux'):
            subprocess.Popen(['xdg-open', str(output_path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif sys.platform == 'darwin':
            subprocess.Popen(['open', str(output_path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        print(f"  ⚠ Could not launch track: {e}")
    
    print("\n  🎧 Headphones will no longer disconnect!")
    
    shutil.rmtree(TEMP_DIR, ignore_errors=True)


# ============================================
#                  MAIN
# ============================================
if __name__ == "__main__":
    check_ffmpeg()
    folder = Path("lofi_tracks")
    if not folder.exists():
        folder.mkdir()
        print(f"Created folder '{folder}'. Place your mix (mp3) there.")
        sys.exit(0)
    
    files = list(folder.glob("*.mp3"))
    if not files:
        print(f"Place MP3 file into '{folder}' folder!")
        sys.exit(0)
    
    input_file = files[0]
    print("\n" + "=" * 42)
    print(f"Found file: {input_file.name}\n")
    
    backup_source_files(input_file)

    TEMP_DIR.mkdir(exist_ok=True)
    
    if input_file.suffix.lower() in [".wav", ".flac"]:
        working_file = input_file
    else:
        working_file = TEMP_DIR / "source_fixed.flac"
        print("\n📍 Preparation: formatting MP3 to Lossless (FLAC)...")
        print("  (This guarantees 100% correct timecodes on long mixes)")
        t0 = time.time()
        cmd = [
            FFMPEG, "-y", "-hide_banner", 
            "-err_detect", "ignore_err", 
            "-i", str(input_file), 
            "-c:a", "flac", 
            str(working_file)
        ]
        safe_run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print(f"  Ready in {fmt(time.time() - t0)}")
    
    should_process = analyze_track(working_file, input_file)
    if should_process:
        out_name = f"[PRO] {input_file.name}"
        process_mix(working_file, out_name, input_file)
