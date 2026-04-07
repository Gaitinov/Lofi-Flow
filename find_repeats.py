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

# Фикс кодировки для Windows
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except AttributeError:
        pass

SCRIPT_DIR = Path(__file__).parent
FFMPEG = str(SCRIPT_DIR / "ffmpeg.exe") if (SCRIPT_DIR / "ffmpeg.exe").exists() else "ffmpeg"
FFPROBE = str(SCRIPT_DIR / "ffprobe.exe") if (SCRIPT_DIR / "ffprobe.exe").exists() else "ffprobe"

def fmt(seconds):
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h:02}:{m:02}:{s:02}"

def get_duration(filepath):
    cmd = [FFPROBE, "-v", "quiet", "-show_format", "-show_streams", "-print_format", "json", str(filepath)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
        info = json.loads(r.stdout)
        return float(info["format"]["duration"])
    except Exception:
        return 0

def find_repeats_high_precision(filepath, window_sec=12, threshold=0.92):
    print(f"\n🔍 Анализ файла: {filepath.name}")
    duration = get_duration(filepath)
    if duration == 0:
        print("❌ Не удалось определить длительность файла.")
        return

    print(f"⏳ Длительность: {fmt(duration)}")
    print(f"📦 Экстракция аудио-отпечатков (2 полосы: Bass + Melody)...")

    # Двухполосный анализ:
    # Канал 0 (L) - низкие частоты (бас, ритм)
    # Канал 1 (R) - высокие частоты (мелодия)
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
        print(f"❌ Ошибка FFmpeg: {stderr.decode('utf-8', errors='replace')}")
        return

    # Данные теперь 2-канальные (L=Low, R=High)
    audio = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32)
    audio = audio.reshape(-1, 2)
    
    # 1. Вычисляем огибающие для обеих полос (RMS на частоте 2 Гц)
    pts_per_sec = 2
    spp = target_sr // pts_per_sec
    num_pts = audio.shape[0] // spp
    
    if num_pts < 30:
        print("❌ Файл слишком короткий.")
        return

    # Решейпим и считаем RMS для каждого канала отдельно
    # audio.shape = (samples, 2)
    reshaped = audio[:num_pts * spp].reshape(num_pts, spp, 2)
    envelope = np.sqrt(np.mean(reshaped**2, axis=1)) # Результат: (num_pts, 2)
    
    env_low = envelope[:, 0]
    env_high = envelope[:, 1]
    
    # 2. Поиск дубликатов
    print(f"🧪 Поиск совпадений по ритму и мелодии...")
    
    win_pts = window_sec * pts_per_sec
    step_pts = pts_per_sec * 2 # Проверка каждые 2 секунды для скорости
    
    matches = []
    buckets = defaultdict(list)
    
    start_time = time.time()
    reported_zones = []
    
    total_steps = (num_pts - win_pts) // step_pts
    last_print = 0

    # Предварительная нормализация для экстремально быстрого расчета корреляции
    # (x - mean) / std
    def fast_norm(x):
        mu = np.mean(x)
        sd = np.std(x)
        return (x - mu) / sd if sd > 1e-6 else x - mu

    print(f"🧪 Анализ (поиск по 30-минутным интервалам)...")

    for idx, i in enumerate(range(0, num_pts - win_pts, step_pts)):
        # Отображение прогресса
        now = time.time()
        if now - last_print > 0.5:
            last_print = now
            progress = (idx + 1) / total_steps
            elapsed = now - start_time
            eta = (elapsed / progress) - elapsed if progress > 0 else 0
            filled = int(30 * progress)
            bar = "█" * filled + "░" * (30 - filled)
            print(f"\r  [{bar}] {progress*100:5.1f}% | Осталось: ~{fmt(eta)}  ", end="", flush=True)

        seg_low = env_low[i : i + win_pts]
        seg_high = env_high[i : i + win_pts]
        
        m_low, s_low = np.mean(seg_low), np.std(seg_low)
        m_high, s_high = np.mean(seg_high), np.std(seg_high)
        
        if m_low < 1.0 and m_high < 1.0: continue
        
        # Нормализуем текущий сегмент один раз
        n_seg_low = fast_norm(seg_low)
        n_seg_high = fast_norm(seg_high)
        
        # Ключ включает характеристики обеих полос
        granularity = 100
        key = (round(m_low / granularity) * granularity, round(m_high / granularity) * granularity)
        
        # Проверяем текущую корзину и соседние (для надежности при пограничных значениях)
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
                            for z1, z2, dur in reported_zones:
                                if abs(i/pts_per_sec - (z2 + dur)) < 15 and abs(j/pts_per_sec - (z1 + dur)) < 15:
                                    # Обновляем длительность (удаляем старое, добавляем новое)
                                    reported_zones.remove((z1, z2, dur))
                                    new_dur = dur + (step_pts / pts_per_sec)
                                    reported_zones.append((z1, z2, new_dur))
                                    is_new = False
                                    break
                            
                            if is_new:
                                reported_zones.append((j/pts_per_sec, i/pts_per_sec, window_sec))
        
        buckets[key].append(i)

    print(f"✨ Анализ завершен за {time.time() - start_time:.1f} сек.")

    if not reported_zones:
        print("✅ Повторений не обнаружено. Скрипт проверил ритм и мелодию отдельно.")
    else:
        print(f"\n⚠️ ОБНАРУЖЕНЫ ПОВТОРЫ ТРЕКОВ ({len(reported_zones)}):")
        results = []
        for start_a, start_b, duration in sorted(reported_zones):
            if duration >= window_sec:
                print(f"  🔁 Совпадение: {fmt(start_a)} и {fmt(start_b)} (длительность ~{int(duration)} сек)")
                results.append({
                    "time_a": start_a,
                    "time_b": start_b,
                    "time_a_fmt": fmt(start_a),
                    "time_b_fmt": fmt(start_b),
                    "duration": round(duration, 1)
                })
        
        # Сохранение в JSON
        export_data = {
            "source_file": filepath.name,
            "source_full_path": str(filepath.absolute()),
            "duration": duration,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "matches": results
        }
        json_path = SCRIPT_DIR / f"repeats_{filepath.stem}.json"
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(export_data, f, ensure_ascii=False, indent=2)
        print(f"\n📄 Результаты сохранены в: {json_path.name}")
        
        # Пытаемся открыть веб-инструмент
        viewer_path = SCRIPT_DIR / "repeats_viewer.html"
        if viewer_path.exists():
            print(f"🌐 Открытие визуального инструмента...")
            try:
                os.startfile(str(viewer_path))
            except: pass

def main():
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    
    print("==========================================")
    print("   🔎 ВЫСОКОТОЧНЫЙ ПОИСК ПОВТОРОВ")
    print("   (Анализ Бас + Мелодия)")
    print("==========================================")
    
    file_path = filedialog.askopenfilename(
        title="Выберите аудиофайл",
        filetypes=[("Audio Files", "*.mp3 *.wav *.flac *.m4a *.ogg"), ("All Files", "*.*")]
    )
    
    if not file_path:
        return

    try:
        find_repeats_high_precision(Path(file_path))
    except Exception as e:
        print(f"❌ Ошибка: {e}")
    
    print("\nНажмите Enter...")
    input()

if __name__ == "__main__":
    main()
