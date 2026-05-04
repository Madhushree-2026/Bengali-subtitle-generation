
import sys
import os
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.stderr.reconfigure(encoding='utf-8', errors='replace')

# ── stdlib ───────────────────────────────────────────────────────────────────
import asyncio
import hashlib
import re
import shutil
import socket
import tempfile
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ── third-party ──────────────────────────────────────────────────────────────
import httpx
import yt_dlp
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

load_dotenv()

# ── API Keys (BOM-safe) ───────────────────────────────────────────────────────
GROQ_API_KEY = (
    os.getenv("GROQ_API_KEY", "")
    .strip().replace('\r', '').replace('\n', '').lstrip('\ufeff')
)
print(f"[DEBUG] GROQ key length: {len(GROQ_API_KEY)}", flush=True)

HF_TOKEN = (
    os.getenv("HF_TOKEN", "")
    .strip().replace('\r', '').replace('\n', '').lstrip('\ufeff')
)
print(f"[DEBUG] HF_TOKEN length: {len(HF_TOKEN)}", flush=True)

# ╔══════════════════════════════╗
# ║   INTERNET CHECK             ║
# ╚══════════════════════════════╝
def check_internet() -> bool:
    hosts = [("api.groq.com", 443), ("8.8.8.8", 53), ("1.1.1.1", 53)]
    for host, port in hosts:
        try:
            socket.setdefaulttimeout(3)
            with socket.create_connection((host, port), timeout=3):
                return True
        except Exception:
            continue
    return False

class _InternetCache:
    def __init__(self):
        self._value: bool = False
        self._ts: float = 0.0
        self._ttl: float = 30.0

    def get(self) -> bool:
        now = time.monotonic()
        if now - self._ts > self._ttl:
            try:
                socket.setdefaulttimeout(3)
                socket.getaddrinfo("api.groq.com", 443)
                self._value = True
            except Exception:
                self._value = False
            self._ts = now
        return self._value

    def invalidate(self):
        self._ts = 0.0

_internet_cache = _InternetCache()

import transformers
_startup_online = _internet_cache.get()
os.environ["TRANSFORMERS_OFFLINE"] = "0" if _startup_online else "1"
os.environ["HF_DATASETS_OFFLINE"]  = "0" if _startup_online else "1"
ONLINE_MODE = _startup_online
print(f"[MODE] {'ONLINE  - using Groq API' if ONLINE_MODE else 'OFFLINE - using local models'}", flush=True)

# ╔══════════════════════════════╗
# ║   CONFIGURATION              ║
# ╚══════════════════════════════╝
GROQ_TRANSCRIBE_URL   = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_CHAT_URL         = "https://api.groq.com/openai/v1/chat/completions"
WHISPER_MODEL         = "whisper-large-v3"
TRANSLATE_MODEL       = "llama-3.3-70b-versatile"

LOCAL_WHISPER_MODEL   = "medium"
LOCAL_TRANSLATE_MODEL = "facebook/nllb-200-distilled-600M"
LOCAL_MODEL_DIR       = os.path.join(os.path.dirname(__file__), "local_models")

GROQ_MAX_MB           = 24
CHUNK_DURATION_SEC    = 600
MAX_CONCURRENT_GROQ   = 3
RATE_LIMIT_PER_MINUTE = 20
TRANSLATE_BATCH_SIZE  = 10

CACHE_DIR       = "cached_mp3s"
OUTPUT_DIR      = "srt_outputs"
VIDEO_CACHE_DIR = "cached_videos"
os.makedirs(VIDEO_CACHE_DIR, exist_ok=True)
for _d in (CACHE_DIR, OUTPUT_DIR, LOCAL_MODEL_DIR):
    os.makedirs(_d, exist_ok=True)

JOBS: Dict[str, dict] = {}

# ╔══════════════════════════════════════════════════════════╗
# ║   SPEAKER DIARIZATION                                    ║
# ║                                                          ║
# ║  Two-tier system:                                        ║
# ║  1. pyannote.audio  — best quality, needs HF_TOKEN       ║
# ║  2. VAD energy fallback — zero extra deps, always works  ║
# ╚══════════════════════════════════════════════════════════╝
DIARIZE_ENABLED     = True
_diarizer           = None
_SPEAKER_LABELS: Dict[str, str] = {}


def _load_pyannote():
    """Load pyannote pipeline (lifted offline flag so HF cache works)."""
    global _diarizer
    if _diarizer is not None:
        return _diarizer
    old = os.environ.get("TRANSFORMERS_OFFLINE", "0")
    os.environ["TRANSFORMERS_OFFLINE"] = "0"
    try:
        from pyannote.audio import Pipeline
        print("  [DIARIZE] Loading pyannote pipeline...", flush=True)
        _diarizer = Pipeline.from_pretrained(
            "pyannote/speaker-diarization-3.1",
            use_auth_token=HF_TOKEN if HF_TOKEN else True,
        )
        print("  [DIARIZE] pyannote ready.", flush=True)
        return _diarizer
    finally:
        os.environ["TRANSFORMERS_OFFLINE"] = old


def _vad_diarize(mp3_path: str) -> List[Dict]:
    """
    Pure-Python energy-based VAD diarizer.
    Uses only stdlib (wave, struct, math, subprocess).
    Converts mp3->wav via ffmpeg, reads PCM, segments by silence,
    clusters into 2 speaker buckets by energy level.
    Always returns speaker turns — no internet or models needed.
    """
    import subprocess, wave, struct, math

    wav_tmp = mp3_path + "_vad.wav"
    try:
        subprocess.run(
            [FFMPEG, "-y", "-i", mp3_path,
             "-ac", "1", "-ar", "16000", "-loglevel", "error", wav_tmp],
            check=True, timeout=120,
        )
    except Exception as e:
        print(f"  [VAD] wav conversion failed: {e}", flush=True)
        return []

    try:
        with wave.open(wav_tmp, "rb") as wf:
            rate     = wf.getframerate()
            n_frames = wf.getnframes()
            raw      = wf.readframes(n_frames)
        samples = struct.unpack(f"<{n_frames}h", raw)
    except Exception as e:
        print(f"  [VAD] wav read failed: {e}", flush=True)
        return []
    finally:
        try: os.remove(wav_tmp)
        except: pass

    # RMS energy in 50 ms windows
    win   = rate // 20
    rmses = []
    for i in range(0, len(samples) - win, win):
        chunk = samples[i: i + win]
        rms   = math.sqrt(sum(s * s for s in chunk) / len(chunk))
        rmses.append(rms)

    if not rmses:
        return []

    thresh = max(rmses) * 0.15   # silence = below 15% of peak

    # Merge voiced windows into speech segments
    raw_segs: List[Tuple[int, int]] = []
    in_speech, seg_start = False, 0
    for i, rms in enumerate(rmses):
        t_ms = i * 50
        if rms >= thresh and not in_speech:
            in_speech, seg_start = True, t_ms
        elif rms < thresh and in_speech:
            in_speech = False
            if t_ms - seg_start >= 300:
                raw_segs.append((seg_start, t_ms))
    if in_speech:
        raw_segs.append((seg_start, len(rmses) * 50))

    if not raw_segs:
        print("  [VAD] No speech segments found.", flush=True)
        return []

    # Cluster by energy: above median = SPEAKER_00, below = SPEAKER_01
    def seg_energy(s, e):
        lo, hi = s // 50, e // 50
        c = rmses[lo:hi]
        return sum(c) / max(len(c), 1)

    energies = [seg_energy(s, e) for s, e in raw_segs]
    med      = sorted(energies)[len(energies) // 2]

    turns = []
    for (s, e), eng in zip(raw_segs, energies):
        turns.append({
            "start_ms": s,
            "end_ms":   e,
            "speaker":  "SPEAKER_00" if eng >= med else "SPEAKER_01",
        })
    print(f"  [VAD] {len(turns)} turns detected.", flush=True)
    return turns


async def diarize_audio(mp3_path: str) -> List[Dict]:
    """
    Master diarization entry point.
    Always produces speaker turns (pyannote or VAD fallback).
    """
    if not DIARIZE_ENABLED:
        print("  [DIARIZE] Disabled.", flush=True)
        return []

    loop = asyncio.get_event_loop()

    # Try pyannote first if HF_TOKEN is present
    if HF_TOKEN:
        try:
            def _run_pyannote():
                pipeline    = _load_pyannote()
                diarization = pipeline(mp3_path)
                result = []
                for turn, _, speaker in diarization.itertracks(yield_label=True):
                    result.append({
                        "start_ms": int(turn.start * 1000),
                        "end_ms":   int(turn.end   * 1000),
                        "speaker":  speaker,
                    })
                return result

            turns = await loop.run_in_executor(None, _run_pyannote)
            if turns:
                print(f"  [DIARIZE] pyannote: {len(turns)} turns.", flush=True)
                return turns
            print("  [DIARIZE] pyannote returned 0 turns — using VAD.", flush=True)
        except Exception as exc:
            print(f"  [DIARIZE] pyannote failed ({exc}) — using VAD.", flush=True)
    else:
        print("  [DIARIZE] No HF_TOKEN — using VAD fallback.", flush=True)

    # VAD fallback — always works
    turns = await loop.run_in_executor(None, _vad_diarize, mp3_path)
    return turns


def assign_speakers(segments: List[Dict], diarization: List[Dict]) -> List[Dict]:
    """
    Tag every transcript segment with a stable 'Person N' label.
    Uses maximum-overlap matching between Whisper segments and diarization turns.
    """
    global _SPEAKER_LABELS
    _SPEAKER_LABELS = {}

    if not diarization:
        print("  [ASSIGN] No diarization data — labels skipped.", flush=True)
        for seg in segments:
            seg["speaker_label"] = ""
        return segments

    # 1. Best-overlap speaker per segment
    for seg in segments:
        s, e = seg["start_ms"], seg["end_ms"]
        best_speaker, best_overlap = "", 0
        for d in diarization:
            overlap = min(e, d["end_ms"]) - max(s, d["start_ms"])
            if overlap > best_overlap:
                best_overlap = overlap
                best_speaker = d["speaker"]
        seg["_raw_speaker"] = best_speaker

    # 2. Stable Person-N map (order of first appearance)
    counter = 1
    for seg in segments:
        sp = seg["_raw_speaker"]
        if sp and sp not in _SPEAKER_LABELS:
            _SPEAKER_LABELS[sp] = f"Person {counter}"
            counter += 1

    # 3. Write human label
    for seg in segments:
        seg["speaker_label"] = _SPEAKER_LABELS.get(seg["_raw_speaker"], "")

    # Debug
    counts: Dict[str, int] = {}
    for seg in segments:
        lbl = seg["speaker_label"] or "UNLABELED"
        counts[lbl] = counts.get(lbl, 0) + 1
    print(f"  [ASSIGN] Speaker distribution: {counts}", flush=True)

    return segments


# ╔══════════════════════════════╗
# ║   LOCAL MODELS (OFFLINE)     ║
# ╚══════════════════════════════╝
_local_whisper    = None
_local_translator = None
_local_tokenizer  = None

def get_local_whisper():
    global _local_whisper
    if _local_whisper is None:
        print("[LOCAL] Loading faster-whisper model...", flush=True)
        from faster_whisper import WhisperModel
        _local_whisper = WhisperModel(
            LOCAL_WHISPER_MODEL, device="cpu", compute_type="int8",
            download_root=LOCAL_MODEL_DIR,
        )
        print("[LOCAL] Whisper model ready.", flush=True)
    return _local_whisper

def get_local_translator():
    global _local_translator, _local_tokenizer
    if _local_translator is not None:
        return _local_translator, _local_tokenizer

    print("[LOCAL] Loading NLLB translation model...", flush=True)
    from transformers import AutoModelForSeq2SeqLM, AutoTokenizer
    model_name = LOCAL_TRANSLATE_MODEL
    cache_dir  = LOCAL_MODEL_DIR

    # Force offline so transformers never tries to reach HuggingFace
    old_offline = os.environ.get("TRANSFORMERS_OFFLINE", "0")
    os.environ["TRANSFORMERS_OFFLINE"] = "1"

    try:
        # Strategy 1: load directly from snapshot folder (most reliable offline)
        safe_name     = model_name.replace("/", "--")
        model_root    = os.path.join(cache_dir, f"models--{safe_name}")
        snapshot_dir  = os.path.join(model_root, "snapshots")

        local_path = None
        if os.path.isdir(snapshot_dir):
            snapshots = sorted([
                d for d in os.listdir(snapshot_dir)
                if os.path.isdir(os.path.join(snapshot_dir, d))
            ])
            if snapshots:
                local_path = os.path.join(snapshot_dir, snapshots[-1])

        if local_path and os.path.isdir(local_path):
            print(f"[LOCAL] Snapshot found: {local_path}", flush=True)
            try:
                _local_tokenizer  = AutoTokenizer.from_pretrained(
                    local_path, local_files_only=True)
                _local_translator = AutoModelForSeq2SeqLM.from_pretrained(
                    local_path, local_files_only=True)
                print("[LOCAL] Translation model loaded from snapshot.", flush=True)
                return _local_translator, _local_tokenizer
            except Exception as e:
                print(f"[LOCAL] Snapshot load failed: {e}", flush=True)

        # Strategy 2: standard cache_dir lookup
        print("[LOCAL] Trying cache_dir lookup...", flush=True)
        try:
            _local_tokenizer  = AutoTokenizer.from_pretrained(
                model_name, cache_dir=cache_dir, local_files_only=True)
            _local_translator = AutoModelForSeq2SeqLM.from_pretrained(
                model_name, cache_dir=cache_dir, local_files_only=True)
            print("[LOCAL] Translation model loaded from cache_dir.", flush=True)
            return _local_translator, _local_tokenizer
        except Exception as e:
            print(f"[LOCAL] cache_dir lookup failed: {e}", flush=True)

    finally:
        os.environ["TRANSFORMERS_OFFLINE"] = old_offline

    # Both offline strategies failed — try downloading if online
    if not check_internet():
        raise RuntimeError(
            f"\n{'='*60}\n  NLLB model not in cache!\n"
            f"  Run: python backend/download_models.py\n{'='*60}"
        )

    print("[LOCAL] Downloading NLLB model (internet available)...", flush=True)
    os.environ["TRANSFORMERS_OFFLINE"] = "0"
    try:
        _local_tokenizer  = AutoTokenizer.from_pretrained(model_name, cache_dir=cache_dir)
        _local_translator = AutoModelForSeq2SeqLM.from_pretrained(model_name, cache_dir=cache_dir)
        print("[LOCAL] Translation model downloaded.", flush=True)
        return _local_translator, _local_tokenizer
    except Exception as e:
        raise RuntimeError(f"Failed to download NLLB: {e}") from e
    finally:
        os.environ["TRANSFORMERS_OFFLINE"] = old_offline


def ensure_local_models_cached():
    if not _internet_cache.get():
        return
    try:
        print("[STARTUP] Checking local model cache...", flush=True)
        get_local_whisper()
        get_local_translator()
        print("[STARTUP] Local models ready for offline use.", flush=True)
    except Exception as exc:
        print(f"[STARTUP] Could not pre-cache models: {exc}", flush=True)


# ╔══════════════════════════════╗
# ║   FFMPEG DETECTION           ║
# ╚══════════════════════════════╝
def find_ffmpeg() -> str:
    for candidate in [
        os.getenv("FFMPEG_PATH"),
        shutil.which("ffmpeg"),
        r"C:\ffmpeg\bin\ffmpeg.exe",
        r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
        os.path.expanduser(r"~\ffmpeg\bin\ffmpeg.exe"),
    ]:
        if candidate and os.path.isfile(candidate):
            return candidate
    raise RuntimeError(
        "FFmpeg not found!\n"
        "  Windows : winget install ffmpeg\n"
        "  Mac     : brew install ffmpeg\n"
        "  Linux   : sudo apt install ffmpeg"
    )

FFMPEG = find_ffmpeg()
print(f" FFmpeg: {FFMPEG}", flush=True)

# ╔══════════════════════════════╗
# ║   PYDANTIC MODELS            ║
# ╚══════════════════════════════╝
class TranscribeRequest(BaseModel):
    video_url: str
    force_refresh: bool = False
    source_language: str = "en"

class BatchRequest(BaseModel):
    urls: List[str]
    max_concurrent: int = 3
    force_refresh: bool = False

class JobStatus(BaseModel):
    job_id: str
    status: str
    message: str = ""
    srt_ready: bool = False
    video_ready: bool = False
    progress_pct: float = 0.0
    processing_time_seconds: float = 0.0

# ╔══════════════════════════════╗
# ║   GROQ RATE-LIMIT GUARD      ║
# ╚══════════════════════════════╝
class RateLimiter:
    def __init__(self, rpm: int):
        self.rpm = rpm
        self._timestamps: deque = deque()
        self._lock = asyncio.Lock()

    async def acquire(self):
        async with self._lock:
            while True:
                now = time.monotonic()
                while self._timestamps and now - self._timestamps[0] >= 60:
                    self._timestamps.popleft()
                if len(self._timestamps) < self.rpm:
                    self._timestamps.append(now)
                    return
                wait = 60 - (now - self._timestamps[0]) + 0.1
                print(f"    Rate-limit - sleeping {wait:.1f}s ...")
                await asyncio.sleep(wait)

_audio_limiter = RateLimiter(RATE_LIMIT_PER_MINUTE)
_chat_limiter  = RateLimiter(30)

# ╔══════════════════════════════╗
# ║   VIDEO -> MP3 CONVERTER     ║
# ╚══════════════════════════════╝
def is_youtube_url(url: str) -> bool:
    return "youtube.com" in url or "youtu.be" in url

class VideoToMP3:
    @staticmethod
    def _cache_path(url_or_hash: str) -> str:
        h = hashlib.md5(url_or_hash.encode()).hexdigest()
        return os.path.join(CACHE_DIR, f"{h}.mp3")

    @staticmethod
    async def convert(source: str, force: bool = False) -> Tuple[str, bool, float]:
        cache_path = VideoToMP3._cache_path(source)
        if not force and os.path.exists(cache_path):
            size_mb = os.path.getsize(cache_path) / 1024 / 1024
            print(f"  Cache hit - {size_mb:.1f} MB")
            return cache_path, True, size_mb

        print("  Converting to MP3 ...")
        if is_youtube_url(source):
            await asyncio.get_event_loop().run_in_executor(
                None, lambda: VideoToMP3._ytdlp_download(source, cache_path)
            )
        else:
            tmp = tempfile.mktemp(suffix=".mp3")
            cmd = [FFMPEG, "-y", "-i", source, "-vn", "-ac", "1", "-ar", "16000",
                   "-b:a", "32k", "-loglevel", "error", tmp]
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            _, stderr = await proc.communicate()
            if proc.returncode != 0:
                raise RuntimeError(f"FFmpeg error: {stderr.decode()[:300]}")
            shutil.move(tmp, cache_path)

        size_mb = os.path.getsize(cache_path) / 1024 / 1024
        print(f"  MP3 ready - {size_mb:.1f} MB")
        return cache_path, False, size_mb

    @staticmethod
    def _ytdlp_download(url: str, output_path: str):
        tmp_path = output_path + ".tmp"
        ydl_opts = {
            "format": "bestaudio/best", "outtmpl": tmp_path,
            "quiet": True, "no_warnings": True,
            "postprocessors": [{"key": "FFmpegExtractAudio",
                                "preferredcodec": "mp3", "preferredquality": "32"}],
            "postprocessor_args": ["-ar", "16000", "-ac", "1"],
            "ffmpeg_location": os.path.dirname(FFMPEG),
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        downloaded = tmp_path + ".mp3"
        if not os.path.exists(downloaded):
            downloaded = tmp_path
        if not os.path.exists(downloaded):
            raise RuntimeError("yt-dlp download failed - file not found")
        shutil.move(downloaded, output_path)
        print("  YouTube audio downloaded via yt-dlp")

    @staticmethod
    async def download_video(url: str, job_id: str, force: bool = False) -> Optional[str]:
        if not is_youtube_url(url):
            return None
        cache_path = os.path.join(
            VIDEO_CACHE_DIR, f"{hashlib.md5(url.encode()).hexdigest()}.mp4")
        if not force and os.path.exists(cache_path):
            return cache_path

        def _download():
            tmp = cache_path + ".tmp"
            ydl_opts = {
                "format": "bestvideo[ext=mp4][height<=720]+bestaudio[ext=m4a]/best[ext=mp4][height<=720]/best",
                "outtmpl": tmp, "quiet": True, "no_warnings": True,
                "ffmpeg_location": os.path.dirname(FFMPEG),
                "merge_output_format": "mp4",
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
            if os.path.exists(tmp):
                shutil.move(tmp, cache_path)
            elif os.path.exists(tmp + ".mp4"):
                shutil.move(tmp + ".mp4", cache_path)

        await asyncio.get_event_loop().run_in_executor(None, _download)
        return cache_path if os.path.exists(cache_path) else None


# ╔══════════════════════════════╗
# ║   MP3 CHUNKER                ║
# ╚══════════════════════════════╝
async def split_mp3(mp3_path: str, job_id: str) -> List[Tuple[str, int]]:
    work_dir = tempfile.mkdtemp(prefix=f"srt_{job_id}_")
    pattern  = os.path.join(work_dir, "chunk_%04d.mp3")
    cmd = [FFMPEG, "-y", "-i", mp3_path, "-f", "segment",
           "-segment_time", str(CHUNK_DURATION_SEC), "-c", "copy",
           "-reset_timestamps", "1", "-loglevel", "error", pattern]
    proc = await asyncio.create_subprocess_exec(*cmd)
    await proc.wait()

    chunks: List[Tuple[str, int]] = []
    idx = 0
    while True:
        p = pattern.replace("%04d", f"{idx:04d}")
        if not os.path.exists(p):
            break
        offset_ms = idx * CHUNK_DURATION_SEC * 1000
        mb = os.path.getsize(p) / 1024 / 1024
        if mb > GROQ_MAX_MB:
            sub_pat = os.path.join(work_dir, f"sub{idx}_%04d.mp3")
            sub_cmd = [FFMPEG, "-y", "-i", p, "-f", "segment",
                       "-segment_time", "300", "-c", "copy",
                       "-reset_timestamps", "1", "-loglevel", "error", sub_pat]
            sp = await asyncio.create_subprocess_exec(*sub_cmd)
            await sp.wait()
            si = 0
            while True:
                sp_path = sub_pat.replace("%04d", f"{si:04d}")
                if not os.path.exists(sp_path):
                    break
                chunks.append((sp_path, offset_ms + si * 300_000))
                si += 1
        else:
            chunks.append((p, offset_ms))
        idx += 1

    print(f"  {len(chunks)} chunk(s) to transcribe")
    return chunks

# ╔══════════════════════════════╗
# ║   TRANSCRIPTION              ║
# ╚══════════════════════════════╝
_groq_sem = asyncio.Semaphore(MAX_CONCURRENT_GROQ)

async def transcribe_chunk_groq(chunk_path: str, offset_ms: int,
                                 lang: str, chunk_label: str) -> List[Dict]:
    async with _groq_sem:
        await _audio_limiter.acquire()
        for attempt in range(5):
            try:
                with open(chunk_path, "rb") as fh:
                    async with httpx.AsyncClient() as client:
                        resp = await client.post(
                            GROQ_TRANSCRIBE_URL,
                            headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                            files={"file": (os.path.basename(chunk_path), fh, "audio/mpeg")},
                            data={"model": WHISPER_MODEL, "response_format": "verbose_json",
                                  "timestamp_granularities[]": "segment", "language": lang},
                            timeout=180,
                        )
                if resp.status_code == 429:
                    await asyncio.sleep(min(4 ** attempt, 60)); continue
                resp.raise_for_status()
                data = resp.json()
                segs = data.get("segments", [])
                if not segs and data.get("text"):
                    segs = [{"start": 0, "end": 5, "text": data["text"]}]
                result = []
                for s in segs:
                    txt = s.get("text", "").strip()
                    if not txt: continue
                    s_ms = offset_ms + int(float(s.get("start", 0)) * 1000)
                    e_ms = offset_ms + int(float(s.get("end",   0)) * 1000)
                    if e_ms <= s_ms: e_ms = s_ms + 500
                    result.append({"start_ms": s_ms, "end_ms": e_ms, "text": txt})
                print(f"    {chunk_label}: {len(result)} segment(s)")
                return result
            except Exception as exc:
                err_str = str(exc)
                if '11001' in err_str or 'getaddrinfo' in err_str:
                    _internet_cache.invalidate()
                    raise RuntimeError("No internet - switching to offline mode") from exc
                if attempt == 4: raise
                print(f"    {chunk_label} attempt {attempt+1}: {exc}")
                await asyncio.sleep(2 ** attempt)
    raise RuntimeError(f"All retries exhausted for {chunk_label}")


def transcribe_chunk_local(chunk_path: str, offset_ms: int,
                            lang: str, chunk_label: str) -> List[Dict]:
    model = get_local_whisper()
    print(f"    [LOCAL] Transcribing {chunk_label} ...", flush=True)
    segments_gen, _ = model.transcribe(
        chunk_path, language=lang if lang != "auto" else None,
        beam_size=5, vad_filter=True,
    )
    result = []
    for s in segments_gen:
        txt = s.text.strip()
        if not txt: continue
        s_ms = offset_ms + int(s.start * 1000)
        e_ms = offset_ms + int(s.end   * 1000)
        if e_ms <= s_ms: e_ms = s_ms + 500
        result.append({"start_ms": s_ms, "end_ms": e_ms, "text": txt})
    print(f"    [LOCAL] {chunk_label}: {len(result)} segment(s)", flush=True)
    return result


async def transcribe_mp3(mp3_path: str, lang: str, job_id: str) -> List[Dict]:
    chunks = await split_mp3(mp3_path, job_id)
    online = _internet_cache.get()
    segments: List[Dict] = []

    if online and GROQ_API_KEY:
        tasks   = [transcribe_chunk_groq(p, o, lang, f"chunk-{i}")
                   for i, (p, o) in enumerate(chunks)]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        all_net = all(isinstance(r, Exception) and
                      ('internet' in str(r).lower() or '11001' in str(r))
                      for r in results)
        if all_net:
            print("  [FALLBACK] Network gone - local models", flush=True)
            online = False; _internet_cache.invalidate()
        else:
            for r in results:
                if isinstance(r, Exception): print(f"  Chunk failed: {r}")
                else: segments.extend(r)

    if not online or not GROQ_API_KEY:
        print("  [LOCAL] Running faster-whisper on CPU ...", flush=True)
        loop = asyncio.get_event_loop()
        for i, (path, off) in enumerate(chunks):
            result = await loop.run_in_executor(
                None, transcribe_chunk_local, path, off, lang, f"chunk-{i}")
            segments.extend(result)

    segments.sort(key=lambda x: x["start_ms"])
    GAP_MS = 40
    for i in range(len(segments) - 1):
        cur = segments[i]; nxt = segments[i + 1]
        if cur["end_ms"] >= nxt["start_ms"]:
            cur["end_ms"] = max(cur["start_ms"] + 100, nxt["start_ms"] - GAP_MS)
    return segments


# ╔══════════════════════════════╗
# ║   LANGUAGE HELPERS           ║
# ╚══════════════════════════════╝
_LANG_NAMES: Dict[str, str] = {
    "en": "English", "bn": "Bengali", "ben": "Bengali",
    "hi": "Hindi", "ar": "Arabic", "zh": "Chinese",
    "fr": "French", "de": "German", "es": "Spanish",
    "pt": "Portuguese", "ru": "Russian", "ja": "Japanese",
    "ko": "Korean", "it": "Italian", "tr": "Turkish",
    "ur": "Urdu", "fa": "Persian",
}
def lang_name(code: str) -> str:
    return _LANG_NAMES.get(code.lower(), code.upper())


# ╔══════════════════════════════╗
# ║   TRANSLATION                ║
# ╚══════════════════════════════╝
async def translate_batch_groq(texts: List[str], source_lang: str = "en") -> List[str]:
    if not texts: return []
    src_name = lang_name(source_lang)
    numbered = "\n".join(f"{i+1}. {t}" for i, t in enumerate(texts))
    prompt = (
        f"You are a professional subtitle translator.\n"
        f"Translate each numbered {src_name} line to natural, conversational Bengali.\n"
        "Keep the numbering. Output ONLY the numbered Bengali lines, nothing else.\n\n"
        f"{numbered}"
    )
    await _chat_limiter.acquire()
    for attempt in range(5):
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    GROQ_CHAT_URL,
                    headers={"Authorization": f"Bearer {GROQ_API_KEY}",
                             "Content-Type": "application/json"},
                    json={"model": TRANSLATE_MODEL,
                          "messages": [{"role": "user", "content": prompt}],
                          "temperature": 0.3, "max_tokens": 2048},
                    timeout=60,
                )
            if resp.status_code == 429:
                await asyncio.sleep(min(4 ** attempt, 60)); continue
            resp.raise_for_status()
            raw   = resp.json()["choices"][0]["message"]["content"]
            lines: List[str] = [""] * len(texts)
            for line in raw.strip().split("\n"):
                m = re.match(r"^(\d+)[.\)]\s*(.+)", line.strip())
                if m:
                    i = int(m.group(1)) - 1
                    if 0 <= i < len(texts): lines[i] = m.group(2).strip()
            for i, t in enumerate(texts):
                if not lines[i]: lines[i] = t
            return lines
        except Exception as exc:
            err_str = str(exc)
            if '11001' in err_str or 'getaddrinfo' in err_str:
                _internet_cache.invalidate()
                raise RuntimeError("No internet for translation") from exc
            if attempt == 4: return texts
            await asyncio.sleep(2 ** attempt)
    return texts


_NLLB_SRC: Dict[str, str] = {
    "en": "eng_Latn", "hi": "hin_Deva", "ar": "arb_Arab",
    "zh": "zho_Hans", "fr": "fra_Latn", "de": "deu_Latn",
    "es": "spa_Latn", "pt": "por_Latn", "ru": "rus_Cyrl",
    "ja": "jpn_Jpan", "ko": "kor_Hang", "it": "ita_Latn",
    "tr": "tur_Latn", "ur": "urd_Arab", "fa": "pes_Arab",
    "bn": "ben_Beng", "ben": "ben_Beng",
}

def translate_batch_local(texts: List[str], source_lang: str = "en") -> List[str]:
    if not texts: return []
    model, tokenizer = get_local_translator()
    print(f"    [LOCAL] Translating batch of {len(texts)} from {source_lang}...", flush=True)
    src_code = _NLLB_SRC.get(source_lang.lower(), "eng_Latn")
    tokenizer.src_lang = src_code
    inputs = tokenizer(texts, return_tensors="pt", padding=True,
                       truncation=True, max_length=512)
    target_lang_id = tokenizer.convert_tokens_to_ids("ben_Beng")
    outputs = model.generate(**inputs, forced_bos_token_id=target_lang_id,
                             num_beams=4, max_length=512)
    return [tokenizer.decode(o, skip_special_tokens=True) for o in outputs]


async def translate_segments(segments: List[Dict], source_lang: str = "en") -> List[Dict]:
    if not segments: return segments
    if source_lang.lower() in ("bn", "ben"):
        for seg in segments: seg["text_bn"] = seg["text"]
        return segments

    texts: List[str] = [s["text"] for s in segments]
    translated: List[str] = []
    online   = _internet_cache.get()
    use_groq = online and bool(GROQ_API_KEY)
    loop     = asyncio.get_event_loop()

    print(f"  Translating {len(texts)} segment(s) to Bengali ...", flush=True)

    for i in range(0, len(texts), TRANSLATE_BATCH_SIZE):
        batch = texts[i: i + TRANSLATE_BATCH_SIZE]
        if use_groq:
            try:
                result = await translate_batch_groq(batch, source_lang=source_lang)
            except Exception as exc:
                if 'internet' in str(exc).lower():
                    print("  [FALLBACK] No internet - local translation", flush=True)
                    use_groq = False
                    result = await loop.run_in_executor(
                        None, translate_batch_local, batch, source_lang)
                else:
                    result = batch
        else:
            result = await loop.run_in_executor(
                None, translate_batch_local, batch, source_lang)
        translated.extend(result)
        print(f"    Translated {min(i+TRANSLATE_BATCH_SIZE, len(texts))}/{len(texts)}", flush=True)

    for seg, bn in zip(segments, translated):
        seg["text_bn"] = bn
    return segments


# ╔══════════════════════════════╗
# ║   SRT BUILDER                ║
# ╚══════════════════════════════╝
def ms_to_srt_time(ms: int) -> str:
    ms = max(ms, 0)
    h  = ms // 3_600_000; ms %= 3_600_000
    m  = ms // 60_000;    ms %= 60_000
    s  = ms // 1_000;     ms %= 1_000
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

def wrap_text(text: str, max_chars: int = 42) -> str:
    def vlen(s: str) -> float:
        return sum(1.5 if "\u0980" <= c <= "\u09FF" else 1.0 for c in s)
    if vlen(text) <= max_chars:
        return text
    words = text.split()
    if len(words) == 1:
        return text
    best_split_idx, best_balance = 1, float("inf")
    for i in range(1, len(words)):
        l1 = " ".join(words[:i]); l2 = " ".join(words[i:])
        if vlen(l1) > max_chars: break
        balance = abs(vlen(l1) - vlen(l2))
        if balance < best_balance:
            best_balance = balance; best_split_idx = i
    line1 = " ".join(words[:best_split_idx])
    line2 = " ".join(words[best_split_idx:])
    if vlen(line2) > max_chars:
        words2, truncated = line2.split(), ""
        for w in words2:
            test = (truncated + " " + w).strip() if truncated else w
            if vlen(test + "...") <= max_chars: truncated = test
            else: break
        line2 = (truncated + "...") if truncated else (words2[0][:8] + "...")
    return f"{line1}\n{line2}"

def build_srt(segments: List[Dict]) -> str:
    out_lines:   List[str] = []
    GAP_MS     : int       = 40
    MIN_DUR_MS : int       = 300
    MAX_DUR_MS : int       = 5000

    for idx, seg in enumerate(segments, 1):
        start_ms = seg["start_ms"]
        end_ms   = seg["end_ms"]
        bn_text  = seg.get("text_bn", "") or seg.get("text", "")

        if idx < len(segments):
            next_start = segments[idx]["start_ms"]
            end_ms = min(end_ms, next_start - GAP_MS)
        end_ms = max(end_ms, start_ms + MIN_DUR_MS)
        end_ms = min(end_ms, start_ms + MAX_DUR_MS)
        if idx < len(segments):
            next_start = segments[idx]["start_ms"]
            if end_ms >= next_start:
                end_ms = next_start - 1
        if end_ms <= start_ms:
            continue

        start_str     = ms_to_srt_time(start_ms)
        end_str       = ms_to_srt_time(end_ms)
        speaker_label = seg.get("speaker_label", "")
        prefix        = f"{speaker_label}:\n" if speaker_label else ""
        wrapped       = wrap_text(bn_text, max_chars=42)
        display       = prefix + "\n".join(wrapped.split("\n")[:2])

        out_lines.append("\n".join([
            str(len(out_lines) + 1),
            f"{start_str} --> {end_str}",
            display,
        ]))

    return "\n\n".join(out_lines) + "\n"


# ╔══════════════════════════════╗
# ║   PIPELINE ORCHESTRATOR      ║
# ╚══════════════════════════════╝
async def run_pipeline(job_id: str, source: str, is_local: bool,
                       force_refresh: bool, lang: str):
    t0 = time.time()
    JOBS[job_id].update({"status": "processing", "progress_pct": 5.0})
    online = _internet_cache.get()
    JOBS[job_id]["mode"] = "online" if online else "offline"

    try:
        # Step 1 — Convert to MP3
        print(f"\n[{job_id}] Step 1/4 - Convert to MP3")
        mp3_path, from_cache, mp3_mb = await VideoToMP3.convert(source, force_refresh)
        video_path = await VideoToMP3.download_video(source, job_id, force_refresh)
        JOBS[job_id]["video_path"] = video_path
        JOBS[job_id].update({"progress_pct": 20.0, "from_cache": from_cache})

        # Step 2 — Speaker diarization
        print(f"\n[{job_id}] Step 2/4 - Speaker diarization")
        diarization = await diarize_audio(mp3_path)
        JOBS[job_id]["progress_pct"] = 30.0

        # Step 3 — Transcribe
        print(f"\n[{job_id}] Step 3/4 - Transcribe ({lang})")
        segments = await transcribe_mp3(mp3_path, lang, job_id)
        print(f"  {len(segments)} raw segment(s)")

        # Assign speakers right after transcription
        segments = assign_speakers(segments, diarization)
        JOBS[job_id]["progress_pct"] = 60.0

        # Step 4 — Translate
        print(f"\n[{job_id}] Step 4/4 - Translate to Bengali")
        segments = await translate_segments(segments, source_lang=lang)
        JOBS[job_id]["progress_pct"] = 90.0

        srt_content = build_srt(segments)
        srt_path    = os.path.join(OUTPUT_DIR, f"{job_id}.srt")
        with open(srt_path, "w", encoding="utf-8-sig") as fh:
            fh.write(srt_content)

        elapsed = round(time.time() - t0, 2)
        JOBS[job_id].update({
            "status": "done", "srt_ready": True, "srt_path": srt_path,
            "video_ready": video_path is not None,
            "progress_pct": 100.0,
            "processing_time_seconds": elapsed,
            "segment_count": len(segments),
            "mp3_size_mb": round(mp3_mb, 2),
            "message": f"Done in {elapsed}s - {len(segments)} subtitle(s)",
        })
        print(f"\n[{job_id}] Complete in {elapsed}s - SRT: {srt_path}")

    except Exception as exc:
        import traceback
        JOBS[job_id].update({
            "status": "failed", "message": str(exc),
            "processing_time_seconds": round(time.time() - t0, 2),
        })
        print(f"\n[{job_id}] Failed: {exc}")
        traceback.print_exc()
    finally:
        if is_local and os.path.exists(source):
            os.remove(source)


# ╔══════════════════════════════╗
# ║   FASTAPI APP                ║
# ╚══════════════════════════════╝
app = FastAPI(title="Video -> Bengali SRT Service", version="2.5.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])

@app.on_event("startup")
async def on_startup():
    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, ensure_local_models_cached)

@app.post("/transcribe")
async def transcribe_url(req: TranscribeRequest, bg: BackgroundTasks):
    job_id = str(uuid.uuid4())[:8]
    JOBS[job_id] = {"status": "queued", "progress_pct": 0.0,
                    "srt_ready": False, "message": "Queued"}
    bg.add_task(run_pipeline, job_id=job_id, source=req.video_url,
                is_local=False, force_refresh=req.force_refresh, lang=req.source_language)
    return {"job_id": job_id, "status": "queued", "poll_url": f"/jobs/{job_id}"}

@app.post("/transcribe/file")
async def transcribe_file(bg: BackgroundTasks, file: UploadFile = File(...),
                          source_language: str = "en"):
    job_id = str(uuid.uuid4())[:8]
    suffix = Path(file.filename or "upload.mp4").suffix or ".mp4"
    tmp    = tempfile.mktemp(suffix=suffix)
    with open(tmp, "wb") as fh:
        shutil.copyfileobj(file.file, fh)
    JOBS[job_id] = {"status": "queued", "progress_pct": 0.0,
                    "srt_ready": False, "message": "Queued"}
    bg.add_task(run_pipeline, job_id=job_id, source=tmp, is_local=True,
                force_refresh=True, lang=source_language)
    return {"job_id": job_id, "status": "queued", "poll_url": f"/jobs/{job_id}"}

@app.get("/jobs/{job_id}", response_model=JobStatus)
async def job_status(job_id: str):
    if job_id not in JOBS:
        raise HTTPException(404, f"Job {job_id!r} not found")
    j = JOBS[job_id]
    return JobStatus(
        job_id=job_id, status=j["status"],
        message=j.get("message", ""),
        srt_ready=j.get("srt_ready", False),
        video_ready=j.get("video_ready", False),
        progress_pct=j.get("progress_pct", 0.0),
        processing_time_seconds=j.get("processing_time_seconds", 0.0),
    )

@app.get("/download/{job_id}")
async def download_srt(job_id: str):
    if job_id not in JOBS:
        raise HTTPException(404, f"Job {job_id!r} not found")
    j = JOBS[job_id]
    if j["status"] != "done":
        raise HTTPException(400, f"Job not ready - status: {j['status']}")
    srt_path = j.get("srt_path")
    if not srt_path or not os.path.exists(srt_path):
        raise HTTPException(500, "SRT file missing - please retry")
    return FileResponse(srt_path, media_type="text/plain; charset=utf-8",
                        filename=f"subtitle_bengali_{job_id}.srt")

@app.get("/video/{job_id}")
async def stream_video(job_id: str):
    if job_id not in JOBS:
        raise HTTPException(404, "Job not found")
    video_path = JOBS[job_id].get("video_path")
    if not video_path or not os.path.exists(video_path):
        raise HTTPException(404, "Video not available")
    return FileResponse(video_path, media_type="video/mp4",
                        filename=f"video_{job_id}.mp4")

@app.get("/health")
async def health():
    online = _internet_cache.get()
    return {
        "status": "healthy",
        "mode": "online" if online else "offline",
        "groq_key_set": bool(GROQ_API_KEY),
        "hf_token_set": bool(HF_TOKEN),
        "diarize_enabled": DIARIZE_ENABLED,
        "whisper_model": WHISPER_MODEL if online else LOCAL_WHISPER_MODEL,
        "ffmpeg": FFMPEG,
    }

@app.get("/mode")
async def get_mode():
    online = _internet_cache.get()
    return {"online": online,
            "mode": "online (Groq API)" if online else "offline (local models)"}

if __name__ == "__main__":
    import uvicorn
    print("\n" + "=" * 62)
    print("  VIDEO -> BENGALI SRT GENERATOR  v2.5")
    print("=" * 62)
    print(f"  Mode            : {'ONLINE (Groq)' if ONLINE_MODE else 'OFFLINE (local)'}")
    print(f"  Diarization     : pyannote (if HF_TOKEN) + VAD fallback")
    print(f"  HF_TOKEN        : {'SET ✓' if HF_TOKEN else 'NOT SET — using VAD only'}")
    print(f"  FFmpeg          : {FFMPEG}")
    print(f"  Rate limit      : {RATE_LIMIT_PER_MINUTE} req/min")
    print(f"  Cache dir       : {CACHE_DIR}/")
    print(f"  SRT output dir  : {OUTPUT_DIR}/")
    print("=" * 62)
    print("  http://0.0.0.0:8000")
    print("=" * 62 + "\n")
    uvicorn.run("without_face_detection:app", host="0.0.0.0", port=8000, reload=False)
