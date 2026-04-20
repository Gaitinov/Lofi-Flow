import os
import sys
import subprocess
import json
import time
import numpy as np
import tkinter as tk
from tkinter import filedialog
from pathlib import Path
from collections import defaultdict
from config_loader import S

# Windows encoding fix
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except AttributeError:
        pass

PROJECT_ROOT = Path(__file__).parent.parent
FFMPEG = str(PROJECT_ROOT / "bin" / "ffmpeg.exe") if (PROJECT_ROOT / "bin" / "ffmpeg.exe").exists() else "ffmpeg"
FFPROBE = str(PROJECT_ROOT / "bin" / "ffprobe.exe") if (PROJECT_ROOT / "bin" / "ffprobe.exe").exists() else "ffprobe"

def fmt(seconds):
    """Formats seconds into HH:MM:SS string."""
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h:02}:{m:02}:{s:02}"

def get_duration(filepath):
    """Returns duration of audio file in seconds using ffprobe."""
    cmd = [FFPROBE, "-v", "quiet", "-show_format", "-show_streams", "-print_format", "json", str(filepath)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
        info = json.loads(r.stdout)
        return float(info["format"]["duration"])
    except Exception:
        return 0

def find_repeats_high_precision(filepath, window_sec=None, threshold=None):
    """Analyses audio file for repeated segments using bass and melody fingerprints."""
    if window_sec is None: window_sec = S["repeat_detection"]["window_sec"]
    if threshold is None: threshold = S["repeat_detection"]["similarity_threshold"]
    print(f"\n🔍 Analyzing file: {filepath.name}")
    total_file_duration = get_duration(filepath)
    if total_file_duration == 0:
        print("❌ Failed to determine file duration.")
        return

    print(f"⏳ Duration: {fmt(total_file_duration)}")
    print(f"📦 Extracting audio fingerprints (2 bands: Bass + Melody)...")

    # Dual-band analysis:
    # Low frequencies (bass, rhythm)
    # High frequencies (melody)
    target_sr = 1000
    filter_graph = "[0:a]lowpass=f=250,pan=mono|c0=c0[low];[0:a]highpass=f=250,pan=mono|c0=c0[high];[low][high]amerge=inputs=2"
    
    cmd = [
        FFMPEG, "-hide_banner", "-loglevel", "error",
        "-i", str(filepath),
        "-filter_complex", filter_graph,
        "-f", "s16le", "-ac", "2", "-ar", str(target_sr), "-"
    ]
    
    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    audio_bytes, stderr = process.communicate()
    
    if process.returncode != 0:
        print(f"❌ FFmpeg error: {stderr.decode('utf-8', errors='replace')}")
        return

    # Data is 2-channel (L=Low, R=High)
    audio = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32)
    audio = audio.reshape(-1, 2)
    
    # 1. Compute envelopes for both bands (RMS at 2 Hz)
    pts_per_sec = 2
    spp = target_sr // pts_per_sec
    num_pts = audio.shape[0] // spp
    
    if num_pts < 30:
        print("❌ File too short.")
        return

    # Calculate RMS for each channel separately
    reshaped = audio[:num_pts * spp].reshape(num_pts, spp, 2)
    envelope = np.sqrt(np.mean(reshaped**2, axis=1)) # Result: (num_pts, 2)
    
    env_low = envelope[:, 0]
    env_high = envelope[:, 1]
    
    # 2. Search for duplicates
    print(f"🧪 Searching for rhythm and melody matches...")
    
    win_pts = window_sec * pts_per_sec
    step_pts = pts_per_sec * 2 # Check every 2 seconds for speed
    
    matches = []
    buckets = defaultdict(list)
    
    start_time = time.time()
    reported_zones = []
    
    total_steps = (num_pts - win_pts) // step_pts
    last_print = 0

    # Fast correlation helper: (x - mean) / std
    def fast_norm(x):
        mu = np.mean(x)
        sd = np.std(x)
        return (x - mu) / sd if sd > 1e-6 else x - mu

    print(f"🧪 Analyzing (searching 30min intervals)...")

    for idx, i in enumerate(range(0, num_pts - win_pts, step_pts)):
        # Progress display
        now = time.time()
        if now - last_print > 0.5:
            last_print = now
            progress = (idx + 1) / total_steps
            elapsed = now - start_time
            eta = (elapsed / progress) - elapsed if progress > 0 else 0
            filled = int(30 * progress)
            bar = "█" * filled + "░" * (30 - filled)
            print(f"\r  [{bar}] {progress*100:5.1f}% | ETA: ~{fmt(eta)}  ", end="", flush=True)

        seg_low = env_low[i : i + win_pts]
        seg_high = env_high[i : i + win_pts]
        
        m_low, s_low = np.mean(seg_low), np.std(seg_low)
        m_high, s_high = np.mean(seg_high), np.std(seg_high)
        
        if m_low < 1.0 and m_high < 1.0: continue
        
        # Normalize current segment once
        n_seg_low = fast_norm(seg_low)
        n_seg_high = fast_norm(seg_high)
        
        # Key includes characteristics of both bands
        granularity = 100
        key = (round(m_low / granularity) * granularity, round(m_high / granularity) * granularity)
        
        # Check current bucket and neighbors for reliability
        for d_low in [-granularity, 0, granularity]:
            for d_high in [-granularity, 0, granularity]:
                neighbor_key = (key[0] + d_low, key[1] + d_high)
                
                for j in buckets[neighbor_key]:
                    if abs(i - j) < 30 * 60 * pts_per_sec: 
                        continue
                    
                    t_low = fast_norm(env_low[j : j + win_pts])
                    corr_l = np.mean(n_seg_low * t_low)
                    
                    if corr_l > threshold:
                        t_high = fast_norm(env_high[j : j + win_pts])
                        corr_h = np.mean(n_seg_high * t_high)
                        
                        if corr_h > threshold:
                            is_new = True
                            avg_corr = float((corr_l + corr_h) / 2.0)
                            for z1, z2, dur, sim_list in reported_zones:
                                if abs(i/pts_per_sec - (z2 + dur)) < 15 and abs(j/pts_per_sec - (z1 + dur)) < 15:
                                    # Update duration
                                    reported_zones.remove((z1, z2, dur, sim_list))
                                    new_dur = dur + (step_pts / pts_per_sec)
                                    sim_list.append(avg_corr)
                                    reported_zones.append((z1, z2, new_dur, sim_list))
                                    is_new = False
                                    break
                            
                            if is_new:
                                reported_zones.append((j/pts_per_sec, i/pts_per_sec, window_sec, [avg_corr]))
        
        buckets[key].append(i)

    print(f"✨ Analysis completed in {time.time() - start_time:.1f} sec.")

    if not reported_zones:
        print("✅ No repeats detected. Checked rhythm and melody separately.")
    else:
        print(f"\n⚠️ DETECTED REPEATS ({len(reported_zones)}):")
        results = []
        max_overall_sim = 0.0
        
        redundant_intervals = []
        
        for start_a, start_b, m_dur, sim_list in sorted(reported_zones):
            if m_dur >= window_sec:
                mean_sim = sum(sim_list) / len(sim_list) if sim_list else threshold
                if mean_sim > max_overall_sim:
                    max_overall_sim = mean_sim
                print(f"  🔁 Match: {fmt(start_a)} and {fmt(start_b)} (dur ~{int(m_dur)} sec, similarity: {mean_sim:.1%})")
                
                redundant_intervals.append((start_b, start_b + m_dur))
                
                results.append({
                    "time_a": start_a,
                    "time_b": start_b,
                    "time_a_fmt": fmt(start_a),
                    "time_b_fmt": fmt(start_b),
                    "duration": round(m_dur, 1),
                    "similarity": round(mean_sim, 3)
                })
        
        # Calculate overall probability
        base_prob = 50.0 + (max_overall_sim - threshold) / (1.0 - threshold) * 40.0
        
        # Duration correction: protection against short drum loops
        max_single_match_dur = max(r["duration"] for r in results)
        if max_single_match_dur < 16:
            base_prob -= 50.0  # No long matches (likely short samples/loops)
        elif 16 <= max_single_match_dur <= 25:
            base_prob -= 25.0  # Too short for a full track
        else:
            base_prob += 15.0  # At least one long solid match
            
        # Overall duration correction
        total_duration_matches = sum(r["duration"] for r in results)
        if total_duration_matches < 10:
            base_prob -= 30.0  # Too short
        elif 10 <= total_duration_matches <= 30:
            base_prob -= 10.0  # Suspicious but low for a full track
        else:
            base_prob += min(20.0, (total_duration_matches - 30) / 10.0 * 5.0)
            
        # Bonus based on number of repeat locations
        count = len(results)
        if count == 1:
            base_prob -= 15.0  # Single repeat - higher chance of coincidence
        elif 2 <= count <= 10:
            base_prob += 10.0  
        elif 10 < count <= 30:
            base_prob += 25.0  
        else:
            base_prob += 40.0  # Huge issue or looped segment

        # Capture "copy-paste" patterns: A, B, C -> A, B, C
        sequential_bonus = 0.0
        is_sequential_copypaste = False
        if len(results) >= 2:
            for idx in range(1, len(results)):
                prev = results[idx-1]
                curr = results[idx]
                
                # If time_b increases with time_a (chronological order)
                if curr["time_b"] > prev["time_b"]:
                    sequential_bonus += 5.0
                    
                    # If shift between original and copy is nearly identical - it's a block copypaste!
                    shift_prev = prev["time_b"] - prev["time_a"]
                    shift_curr = curr["time_b"] - curr["time_a"]
                    if abs(shift_prev - shift_curr) < 15.0: # 15s margin
                        sequential_bonus += 15.0
                        is_sequential_copypaste = True
                        
                        # Mark as part of copypaste block
                        prev["is_copypaste_block"] = True
                        curr["is_copypaste_block"] = True
                        avg_shift = (shift_prev + shift_curr) / 2.0
                        prev["shift_seconds"] = round(avg_shift, 1)
                        curr["shift_seconds"] = round(avg_shift, 1)
                        
                        # Fill gaps between matches
                        redundant_intervals.append((prev["time_b"], curr["time_b"] + curr["duration"]))
                        
            base_prob += min(50.0, sequential_bonus)
            
        # Macro Dense Repeat Zone heuristic:
        # In long DJ mixes, an author might copy a whole hour.
        # If matches are closer than 25 min, discard the entire interval.
        results_b_sorted = sorted(results, key=lambda x: x["time_b"])
        if len(results_b_sorted) >= 2:
            for idx in range(1, len(results_b_sorted)):
                prev_b = results_b_sorted[idx-1]
                curr_b = results_b_sorted[idx]
                gap = curr_b["time_b"] - (prev_b["time_b"] + prev_b["duration"])
                if 0 < gap < 1500.0: # Less than 25 min gap
                    redundant_intervals.append((prev_b["time_b"] + prev_b["duration"], curr_b["time_b"]))

        prob = min(100.0, max(0.0, base_prob)) if max_overall_sim > 0 else 0.0
        
        # Calculate original duration 
        redundant_intervals.sort(key=lambda x: x[0])
        merged_redundant = []
        for interval in redundant_intervals:
            if not merged_redundant:
                merged_redundant.append(interval)
            else:
                last = merged_redundant[-1]
                if interval[0] <= last[1]:
                    merged_redundant[-1] = (last[0], max(last[1], interval[1]))
                else:
                    merged_redundant.append(interval)
                    
        total_redundant_sec = sum(end - start for start, end in merged_redundant)
        original_sec = max(0, total_file_duration - total_redundant_sec)

        # JSON Export
        export_data = {
            "source_file": filepath.name,
            "source_full_path": str(filepath.absolute()),
            "duration": total_file_duration,
            "original_content_duration": round(original_sec, 1),
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "probability": round(prob, 1),
            "max_similarity": round(max_overall_sim, 3),
            "is_sequential_copypaste": is_sequential_copypaste,
            "matches": results
        }
        json_path = PROJECT_ROOT / "logs" / f"repeats_{filepath.stem}.json"
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(export_data, f, ensure_ascii=False, indent=2)
        print(f"\n📄 Results saved to: {json_path.name}")
        
        # Preload data for viewer
        js_data_path = PROJECT_ROOT / "tools" / "latest_repeats_data.js"
        try:
            with open(js_data_path, "w", encoding="utf-8") as js_f:
                js_f.write(f"window.PRELOADED_REPEATS_DATA = {json.dumps(export_data, ensure_ascii=False)};")
        except Exception:
            pass

        # Open visual tool
        viewer_path = PROJECT_ROOT / "tools" / "repeats_viewer.html"
        if viewer_path.exists():
            print(f"🌐 Opening visual viewer tool...")
            try:
                os.startfile(str(viewer_path))
            except: pass

def main():
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    
    print("==========================================")
    print("   🔎 HIGH PRECISION REPEAT DETECTION")
    print("   (Bass + Melody Analysis)")
    print("==========================================")
    
    file_path = filedialog.askopenfilename(
        title="Select audio file",
        filetypes=[("Audio Files", "*.mp3 *.wav *.flac *.m4a *.ogg"), ("All Files", "*.*")]
    )
    
    if not file_path:
        return

    try:
        find_repeats_high_precision(Path(file_path))
    except Exception as e:
        print(f"❌ Error: {e}")
    
    print("\nPress Enter...")
    input()

if __name__ == "__main__":
    main()