import json
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
CONFIG_PATH = PROJECT_ROOT / "mixer_settings.json"

# Default values if setting file is missing
DEFAULTS = {
    "silence": {
        "thresh_db": -40.0,
        "min_len_sec": 0.5,
        "quiet_thresh_db": -30.0,
        "quiet_scan_step": 0.5
    },
    "mixing": {
        "fade_sec": 1.2,
        "normalize_audio": False,
        "target_loudness_lufs": -14.0,
        "output_bitrate": "192k"
    },
    "artifact_removal": {
        "enabled": True,
        "window_ms": 55,
        "overlap_pct": 75,
        "threshold": 2,
        "burst": 2
    },
    "repeat_detection": {
        "window_sec": 12,
        "similarity_threshold": 0.92
    },
    "system": {
        "num_workers": 6
    }
}

def load_settings():
    """Loads settings from JSON file, falling back to DEFAULTS."""
    settings = DEFAULTS.copy()
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                user_settings = json.load(f)
                # Deep update for sections
                for section, values in user_settings.items():
                    if section in settings:
                        if isinstance(values, dict):
                            settings[section].update(values)
                        else:
                            settings[section] = values
        except Exception as e:
            print(f"  ⚠ Error loading {CONFIG_PATH.name}: {e}")
            print("  Using default settings.")
    
    return settings

# Global settings object
S = load_settings()
