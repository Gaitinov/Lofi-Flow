"""
Lo-Fi Mixer v5 — Параллельная версия с анализом громкости.
FFmpeg напрямую + многопоточность + нормализация громкости.
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

# Фикс кодировки для Windows
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except AttributeError:
        pass

# ============== НАСТРОЙКИ ==============
SILENCE_THRESH = -45         # Порог тишины (дБ). Смягчен, чтобы ловить фоновый шум (не идеальный ноль).
MIN_SILENCE_LEN = 0.5        # Мин. длина тишины (сек). Захватываем более короткие паузы.
QUIET_THRESH = -30           # Порог "тихого звука" (дБ). Хвосты/вступления ниже этого будут вырезаны вместе с тишиной.
QUIET_SCAN_STEP = 0.5        # Шаг сканирования тихих хвостов (секунды).
FADE_SEC = 1.0               # Длина кроссфейда между песнями (в сек). Увеличено для плавности.

# Настройки громкости
NORMALIZE_AUDIO = False      # Вкл/выкл выравнивание громкости.
TARGET_LOUDNESS = -14.0      
OUTPUT_BITRATE = "192k"
NUM_WORKERS = 6              # Кол-во параллельных процессов
# =======================================

SCRIPT_DIR = Path(__file__).parent
FFMPEG = str(SCRIPT_DIR / "ffmpeg.exe") if (SCRIPT_DIR / "ffmpeg.exe").exists() else "ffmpeg"
FFPROBE = str(SCRIPT_DIR / "ffprobe.exe") if (SCRIPT_DIR / "ffprobe.exe").exists() else "ffprobe"
TEMP_DIR = SCRIPT_DIR / "_temp_chunks"


def fmt(seconds):
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h:02}:{m:02}:{s:02}"


def safe_run(cmd, **kwargs):
    """Безопасный запуск процесса с проверкой памяти и повторами на WinError 1455."""
    max_retries = 5
    for attempt in range(max_retries):
        # 1. Проверка физической памяти перед запуском
        mem = psutil.virtual_memory()
        if mem.available < 400 * 1024 * 1024: # Меньше 400 МБ
            time.sleep(2)
            continue
            
        try:
            return subprocess.run(cmd, **kwargs)
        except OSError as e:
            # WinError 1455: Файл подкачки слишком мал
            if getattr(e, 'winerror', None) == 1455 and attempt < max_retries - 1:
                time.sleep(3 + attempt * 2)
                continue
            raise e
    return subprocess.run(cmd, **kwargs) # Последняя попытка без отлова


def get_duration(filepath):
    cmd = [FFPROBE, "-v", "quiet", "-print_format", "json",
           "-show_format", "-show_streams", str(filepath)]
    r = safe_run(cmd, capture_output=True, text=True, encoding="utf-8")
    
    if not r.stdout or not r.stdout.strip():
        # ffprobe вернул пустоту — попробовать ещё раз без quiet
        cmd_retry = [FFPROBE, "-v", "error", "-print_format", "json",
                     "-show_format", "-show_streams", str(filepath)]
        r = safe_run(cmd_retry, capture_output=True, text=True, encoding="utf-8")
        if not r.stdout or not r.stdout.strip():
            print(f"  ⚠ ffprobe не смог прочитать файл: {filepath}")
            if r.stderr:
                print(f"    Ошибка: {r.stderr[:300]}")
            return {"duration": 0, "bitrate": 0, "channels": 2, "sample_rate": "44100"}
    
    try:
        info = json.loads(r.stdout)
    except json.JSONDecodeError:
        print(f"  ⚠ ffprobe вернул некорректный JSON: {r.stdout[:200]}")
        return {"duration": 0, "bitrate": 0, "channels": 2, "sample_rate": "44100"}
    
    return {
        "duration": float(info["format"]["duration"]),
        "bitrate":  int(info["format"].get("bit_rate", 0)) // 1000,
        "channels": info["streams"][0].get("channels", 2),
        "sample_rate": info["streams"][0].get("sample_rate", "44100"),
    }


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
    
    print(f"  Параллельный поиск ({NUM_WORKERS} потоков)...")
    
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
    
    print(f"\r  [{'█' * 30}] 100.0% | Готово за {fmt(time.time() - t0)}                    ")
    
    all_silences.sort(key=lambda x: x[0])
    merged = []
    for s in all_silences:
        if merged and s[0] <= merged[-1][1]:
            prev = merged[-1]
            merged[-1] = (prev[0], max(prev[1], s[1]), max(prev[1], s[1]) - prev[0])
        else:
            merged.append(s)
    return merged


def _get_rms_at(filepath, position, duration=0.5):
    """Замер средней громкости (RMS) в точке файла."""
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
    return -100.0  # Если не удалось замерить — считаем тишиной


def expand_silence_zones(filepath, silences, total_duration):
    """
    Расширяем каждую зону тишины, захватывая тихие хвосты/вступления песен.
    Хвост: тихий звук перед тишиной (конец песни затихает).
    Вступление: тихий звук после тишины (начало новой песни нарастает).
    """
    if not silences:
        return []
    
    expanded = []
    print(f"  Расширение зон тишины (захват тихих хвостов < {QUIET_THRESH} дБ)...")
    
    for sil_start, sil_end, sil_dur in silences:
        # Расширяем влево (захватываем тихий хвост песни)
        new_start = sil_start
        while new_start > QUIET_SCAN_STEP:
            rms = _get_rms_at(filepath, new_start - QUIET_SCAN_STEP, QUIET_SCAN_STEP)
            if rms < QUIET_THRESH:
                new_start -= QUIET_SCAN_STEP
            else:
                break
        
        # Расширяем вправо (захватываем тихое вступление следующей песни)
        new_end = sil_end
        while new_end < total_duration - QUIET_SCAN_STEP:
            rms = _get_rms_at(filepath, new_end, QUIET_SCAN_STEP)
            if rms < QUIET_THRESH:
                new_end += QUIET_SCAN_STEP
            else:
                break
        
        added = (new_start - sil_start) + (new_end - sil_end)
        if abs(added) > 0.1:
            print(f"    {fmt(sil_start)} | Тишина {sil_dur:.1f}с → расширено до {new_end - new_start:.1f}с (хвосты: {abs(added):.1f}с)")
        
        expanded.append((new_start, new_end, new_end - new_start))
    
    # Мерджим перекрывающиеся зоны
    merged = []
    for z in sorted(expanded, key=lambda x: x[0]):
        if merged and z[0] <= merged[-1][1]:
            prev = merged[-1]
            merged[-1] = (prev[0], max(prev[1], z[1]), max(prev[1], z[1]) - prev[0])
        else:
            merged.append(z)
    
    return merged


def _measure_volume_chunk(args):
    """Измерение громкости одного сегмента (параллельно)."""
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
    """Разбивает трек на N частей и замеряет громкость каждой параллельно."""
    seg_len = total_duration / num_segments
    tasks = [(i, str(filepath), i * seg_len, seg_len) for i in range(num_segments)]
    
    results = [None] * num_segments
    completed = 0
    t0 = time.time()
    
    print(f"  Параллельный замер громкости ({NUM_WORKERS} потоков, {num_segments} сегментов)...")
    
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
    
    print(f"\r  [{'█' * 30}] 100.0% | Готово за {fmt(time.time() - t0)}                    ")
    return results


# ============================================
#                 ANALYZE
# ============================================
def analyze_track(filepath):
    print("\n" + "=" * 55)
    print("   🎵 АНАЛИЗ ТРЕКА")
    print("=" * 55)
    
    meta = get_duration(filepath)
    dur = meta["duration"]
    
    print(f"  Файл:        {filepath.name}")
    print(f"  Длительность: {fmt(dur)}")
    print(f"  Битрейт:     {meta['bitrate']} kbps | Каналы: {meta['channels']} | SR: {meta['sample_rate']} Hz")
    
    # === 1. Карта громкости по сегментам ===
    print(f"\n{'─' * 55}")
    print(f"  📊 КАРТА ГРОМКОСТИ (по сегментам)")
    print(f"{'─' * 55}")
    
    vol_results = analyze_volume_segments(filepath, dur)
    
    volumes = [r[2] for r in vol_results if r and r[2] is not None]
    avg_vol = sum(volumes) / len(volumes) if volumes else 0
    min_vol = min(volumes) if volumes else 0
    max_vol_val = max(volumes) if volumes else 0
    vol_range = max_vol_val - min_vol
    
    print(f"\n  {'Время':>10} │ {'Громкость':>10} │ Уровень")
    print(f"  {'─' * 10}─┼─{'─' * 10}─┼─{'─' * 28}")
    
    for r in vol_results:
        if r is None or r[2] is None:
            continue
        start, duration, mean_v, max_v = r
        # Визуальный бар (нормализуем от min_vol до max_vol_val)
        if vol_range > 0:
            level = (mean_v - min_vol) / vol_range
        else:
            level = 0.5
        bar_len = int(level * 20)
        bar = "▓" * bar_len + "░" * (20 - bar_len)
        
        # Помечаем тихие сегменты
        marker = " ⚠ ТИХО" if mean_v < avg_vol - 3 else ""
        print(f"  {fmt(start):>10} │ {mean_v:>8.1f} dB │ {bar}{marker}")
    
    print(f"\n  Средняя: {avg_vol:.1f} dB | Разброс: {vol_range:.1f} dB | Мин: {min_vol:.1f} dB | Макс: {max_vol_val:.1f} dB")
    
    if vol_range > 5:
        print(f"  ⚠ Разброс громкости {vol_range:.1f} dB — наушники БУДУТ отключаться в тихих местах!")
        print(f"  ➡ Нормализация выровняет до {TARGET_LOUDNESS} LUFS (стандарт Spotify/YouTube).")
    else:
        print(f"  ✅ Разброс громкости {vol_range:.1f} dB — приемлемо.")
    
    # === 2. Поиск пауз ===
    print(f"\n{'─' * 55}")
    print(f"  🔇 ПОИСК ПАУЗ (порог {SILENCE_THRESH} dB, мин. {MIN_SILENCE_LEN} сек)")
    print(f"{'─' * 55}")
    
    silences = detect_silences_parallel(filepath, dur)
    
    if silences:
        total_sil = sum(s[2] for s in silences)
        print(f"\n  Найдено пауз: {len(silences)}")
        print(f"  Общая тишина: {fmt(total_sil)} ({total_sil:.1f} сек)")
        
        print(f"\n  Топ-5 самых длинных:")
        for i, (st, en, d) in enumerate(sorted(silences, key=lambda x: x[2], reverse=True)[:5]):
            print(f"    {i+1}. {fmt(st)} -> {fmt(en)} ({d:.1f} сек)")
    else:
        print(f"\n  Паузы не найдены при пороге {SILENCE_THRESH} dB.")
    
    # === Итог ===
    print("=======================================================")
    print("  ЧТО БУДЕТ СДЕЛАНО ПРИ ОБРАБОТКЕ:")
    
    if NORMALIZE_AUDIO:
        print(f"    1. Нормализация громкости → {TARGET_LOUDNESS} LUFS (выравнивание)")
    else:
        print(f"    1. Громкость → ОСТАЁТСЯ ОРИГИНАЛЬНОЙ (нормализация выключена)")
        
    print(f"    2. Вырезание {len(silences)} пауз ({fmt(total_sil)} тишины)")
    print(f"    3. Плавные переходы (настоящий кроссфейд) {FADE_SEC} сек")
    print(f"{'=' * 55}")
    
    print("\n  Автоматический переход к обработке через 2 секунды...")
    time.sleep(2)
    return True


# ============================================
#                 PROCESS
# ============================================
def _process_chunk(args):
    """Нарезка + нормализация одного чанка (отдельный процесс)."""
    idx, input_file, start, end, output_path, target_lufs = args
    duration = end - start
    
    # loudnorm: двухпроходная нормализация через FFmpeg
    if target_lufs is not None:
        # Первый проход: замер параметров
        cmd1 = [
            FFMPEG, "-y", "-hide_banner", "-nostats",
            "-ss", str(start), "-t", str(duration),
            "-i", str(input_file),
            "-af", f"loudnorm=I={target_lufs}:TP=-1.5:LRA=11:print_format=json",
            "-f", "null", "-"
        ]
        r1 = safe_run(cmd1, capture_output=True, text=True, encoding="utf-8", errors="replace")
        
        # Парсим параметры из JSON в stderr
        json_match = re.search(r'\{[^}]+\}', r1.stderr, re.DOTALL)
        
        if json_match:
            try:
                params = json.loads(json_match.group())
                measured_I = params.get("input_i", "-24.0")
                measured_TP = params.get("input_tp", "-1.0")
                measured_LRA = params.get("input_lra", "7.0")
                measured_thresh = params.get("input_thresh", "-34.0")
                target_offset = params.get("target_offset", "0.0")
                
                # Второй проход: применяем с точными параметрами
                af = (f"loudnorm=I={target_lufs}:TP=-1.5:LRA=11:"
                      f"measured_I={measured_I}:measured_TP={measured_TP}:"
                      f"measured_LRA={measured_LRA}:measured_thresh={measured_thresh}:"
                      f"offset={target_offset}:linear=true")
            except (json.JSONDecodeError, KeyError):
                af = f"loudnorm=I={target_lufs}:TP=-1.5:LRA=11"
        else:
            af = f"loudnorm=I={target_lufs}:TP=-1.5:LRA=11"
    else:
        af = "anull" # Пустой фильтр
    
    # Микро-фейды (30мс) на краях каждого чанка для устранения щелчков/пуков
    # 30мс — неслышно, но гарантирует плавный старт/стоп на нулевом уровне
    MICRO_FADE = 0.03  # 30 мс
    af_chain = f"{af},afade=t=in:d={MICRO_FADE},afade=t=out:st={max(0, duration - MICRO_FADE)}:d={MICRO_FADE}"
    
    cmd2 = [
        FFMPEG, "-y", "-hide_banner", "-nostats",
        "-ss", str(start), "-t", str(duration),
        "-i", str(input_file),
        "-af", af_chain,
        "-c:a", "pcm_s16le",
        str(output_path)
    ]
    safe_run(cmd2, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return idx



def process_mix(filepath, output_filename, original_file=None):
    print("\n" + "=" * 55)
    print("   🔧 ОБРАБОТКА МИКСА")
    print("=" * 55)
    
    meta = get_duration(filepath)
    dur = meta["duration"]
    print(f"  Файл: {filepath.name} ({fmt(dur)})")
    
    # 1. Поиск абсолютной тишины
    print(f"\n📍 Шаг 1: Поиск тишины ({SILENCE_THRESH} дБ)...")
    raw_silences = detect_silences_parallel(filepath, dur)
    print(f"  Найдено абсолютных пауз: {len(raw_silences)}")
    
    # 2. Расширение зон тишины (захват тихих хвостов)
    print(f"\n📍 Шаг 2: Расширение зон (захват тихих хвостов < {QUIET_THRESH} дБ)...")
    silences = expand_silence_zones(filepath, raw_silences, dur)
    
    # 3. Сегменты
    segments = []
    prev_end = 0.0
    for start, end, d in silences:
        if start > prev_end:
            segments.append((prev_end, start))
        prev_end = end
    if prev_end < dur:
        segments.append((prev_end, dur))
    if not segments:
        segments = [(0, dur)]
    
    total_sil = sum(s[2] for s in silences) if silences else 0
    print(f"\n  📊 Сегментов: {len(segments)} | Вырежем: {fmt(total_sil)} (тишина + тихие хвосты)")
    
    # === Сохраняем дебаг-лог ===
    # Карта времени: обработанный файл → оригинал
    time_map = []
    output_pos = 0.0
    for i, (seg_start, seg_end) in enumerate(segments):
        seg_dur = seg_end - seg_start
        
        overlap_dur = 0.0
        if i > 0:
            prev_dur = segments[i-1][1] - segments[i-1][0]
            overlap_dur = min(FADE_SEC, prev_dur / 2.0, seg_dur / 2.0)
            
        output_pos -= overlap_dur  # Вычитаем время наложения (кроссфейда)
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
        },
        "raw_silences": [{"start": s[0], "end": s[1], "dur": s[2], "start_fmt": fmt(s[0]), "end_fmt": fmt(s[1])} for s in raw_silences],
        "expanded_silences": [{"start": s[0], "end": s[1], "dur": s[2], "start_fmt": fmt(s[0]), "end_fmt": fmt(s[1])} for s in silences],
        "segments_kept": [{"start": s[0], "end": s[1], "dur": s[1]-s[0], "start_fmt": fmt(s[0]), "end_fmt": fmt(s[1])} for s in segments],
        "time_map": time_map,
        "total_silence_cut": total_sil,
    }
    debug_path = SCRIPT_DIR / f"debug_cuts_{time.strftime('%Y%m%d_%H%M%S')}.json"
    with open(debug_path, "w", encoding="utf-8") as f:
        json.dump(debug_log, f, ensure_ascii=False, indent=2)
    print(f"  📝 Дебаг-лог сохранен: {debug_path.name}")
    print(f"  📍 Карта времени (output → original): {len(time_map)} сегментов")

    
    # 3. Параллельная нарезка + нормализация
    TEMP_DIR.mkdir(exist_ok=True)
    print(f"\n📍 Шаг 2: Нарезка + нормализация ({NUM_WORKERS} потоков, 2-pass loudnorm)...")
    
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
    
    # 3.5. Валидация фактических чанков
    print(f"\n📍 Шаг 2.5: Валидация чанков...")
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
        print("Ошибка: ни один чанк не был создан!")
        return
        
    # 4. Бесшовная склейка (кроссфейд)
    interleaved = valid_chunks
    
    print(f"\n📍 Шаг 4: Бесшовная склейка ({len(valid_chunks)} фрагментов)...")
    print(f"  (Настоящий кроссфейд {FADE_SEC}с между песнями)")
    
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
        
    filter_graph = ";".join(filter_parts)
    cmd.extend(["-filter_complex", filter_graph, "-map", f"[{last_out}]"])
    
    print(f"\n📍 Шаг 4: Экспорт в итоговый MP3 (финальный рендер)...")
    output_path = SCRIPT_DIR / output_filename
    cmd.extend(["-b:a", OUTPUT_BITRATE, str(output_path)])
    
    # print("DEBUG CMD:", " ".join(cmd[:15]) + "... (усечено)")
    safe_run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    
    print(f"  Склейка и экспорт завершены за {fmt(time.time() - t0)}")
    
    # 5. Очистка
    shutil.rmtree(TEMP_DIR, ignore_errors=True)
    
    # 6. Статистика
    final_meta = get_duration(output_path)
    
    print(f"\n{'=' * 55}")
    print(f"   📊 ИТОГОВАЯ СТАТИСТИКА")
    print(f"{'=' * 55}")
    print(f"  Исходная:      {fmt(dur)}")
    print(f"  Итоговая:      {fmt(final_meta['duration'])}")
    print(f"  Вырезано:      {fmt(total_sil)} тишины")
    
    if NORMALIZE_AUDIO:
        print(f"  Нормализация:  {TARGET_LOUDNESS} LUFS (2-pass loudnorm)")
    else:
        print(f"  Нормализация:  ВЫКЛЮЧЕНА (оригинальная динамика)")
        
    print(f"  Сегментов:     {len(segments)}")
    print(f"  Файл:          {output_path}")
    print(f"{'=' * 55}")
    print("  🎉 Готово! Наушники больше не будут отключаться!")


# ============================================
#                  MAIN
# ============================================
if __name__ == "__main__":
    folder = Path("lofi_tracks")
    if not folder.exists():
        folder.mkdir()
        print(f"Создана папка '{folder}'. Положите туда ваш микс (mp3).")
        sys.exit(0)
    
    files = list(folder.glob("*.mp3"))
    if not files:
        print(f"Положите MP3 файл в папку '{folder}'!")
        sys.exit(0)
    
    input_file = files[0]
    print(f"Найден файл: {input_file.name}")
    
    TEMP_DIR.mkdir(exist_ok=True)
    
    if input_file.suffix.lower() in [".wav", ".flac"]:
        working_file = input_file
    else:
        working_file = TEMP_DIR / "source_fixed.flac"
        print("\n📍 Подготовка: переформатирование MP3 в Lossless-формат (FLAC)...")
        print("  (Это гарантирует 100% распознавание повреждённого звука и таймкодов на многочасовых миксах)")
        t0 = time.time()
        cmd = [
            FFMPEG, "-y", "-hide_banner", 
            "-err_detect", "ignore_err", 
            "-i", str(input_file), 
            "-c:a", "flac", 
            str(working_file)
        ]
        safe_run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print(f"  Готово за {fmt(time.time() - t0)}")
    
    should_process = analyze_track(working_file)
    if should_process:
        out_name = f"[PRO] {input_file.name}"
        process_mix(working_file, out_name, input_file)
