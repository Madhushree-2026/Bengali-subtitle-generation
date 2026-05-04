# Run this ONCE with internet to pre-download all local models.
# After this, the app can run fully offline.
#
# Usage:
#   python backend/download_models.py

import os
from transformers import AutoModelForSeq2SeqLM, AutoTokenizer
from faster_whisper import WhisperModel

BASE_DIR  = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(BASE_DIR, "local_models")
os.makedirs(CACHE_DIR, exist_ok=True)

# ── 1. NLLB translation model ─────────────────────────────────────────────
print("=" * 50)
print("Downloading NLLB-200 translation model...")
print("=" * 50)
AutoTokenizer.from_pretrained(
    "facebook/nllb-200-distilled-600M", cache_dir=CACHE_DIR)
AutoModelForSeq2SeqLM.from_pretrained(
    "facebook/nllb-200-distilled-600M", cache_dir=CACHE_DIR)
print("✓ NLLB model cached at:", CACHE_DIR)

# ── 2. faster-whisper model ───────────────────────────────────────────────
print()
print("=" * 50)
print("Downloading faster-whisper 'medium' model...")
print("=" * 50)
WhisperModel("medium", device="cpu", compute_type="int8", download_root=CACHE_DIR)
print("✓ Whisper model cached at:", CACHE_DIR)

print()
print("=" * 50)
print("All models downloaded! App can now run offline.")
print("=" * 50)