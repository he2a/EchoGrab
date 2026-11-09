import asyncio
import base64
import ctypes
import difflib
import io
import json
import logging
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import webbrowser
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

import numpy as np
import requests
import sounddevice as sd
from scipy.io.wavfile import write as write_wav
from PIL import Image, ImageDraw, ImageTk
from shazamio import Shazam
import yt_dlp
import sv_ttk

# Mutagen imports for multi-format metadata & cover art embedding
from mutagen.id3 import ID3, APIC, TIT2, TPE1, TALB, TDRC, ID3NoHeaderError
from mutagen.mp4 import MP4, MP4Cover
from mutagen.flac import FLAC, Picture
from mutagen.oggopus import OggOpus

# Ensure bundled ffmpeg.exe & ffprobe.exe are on PATH when running as a compiled PyInstaller .exe
if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
    os.environ["PATH"] = sys._MEIPASS + os.pathsep + os.environ.get("PATH", "")

# Windows flag to prevent subprocesses (like ffmpeg) from opening a console window
SUBPROCESS_FLAGS = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def get_app_base_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


APP_DIR = get_app_base_dir()
CONFIG_FILE = os.path.join(APP_DIR, "config.json")
LOG_FILE = os.path.join(APP_DIR, "app_debug.log")


def resolve_icon_path() -> str | None:
    """Locates app.ico across bundled PyInstaller _MEIPASS, script directory, or project root."""
    candidates = [
        os.path.join(getattr(sys, "_MEIPASS", APP_DIR), "app.ico"),
        os.path.join(APP_DIR, "app.ico"),
        os.path.join(os.path.dirname(APP_DIR), "app.ico"),
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    return None


def apply_win11_titlebar(root: tk.Tk, dark: bool = True):
    """Applies Windows 11 DWM dark title bar and rounded corner preference."""
    if sys.platform != "win32":
        return
    try:
        root.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        DWMWA_USE_IMMERSIVE_DARK_MODE = 20
        DWMWA_WINDOW_CORNER_PREFERENCE = 33
        DWMWCP_ROUND = 2

        val_dark = ctypes.c_int(1 if dark else 0)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE, ctypes.byref(val_dark), ctypes.sizeof(val_dark)
        )
        val_round = ctypes.c_int(DWMWCP_ROUND)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, ctypes.byref(val_round), ctypes.sizeof(val_round)
        )
    except Exception:
        pass


# ---------------- LIVE FILE & UI LOGGING SETUP ---------------- #
class TkQueueHandler(logging.Handler):
    def __init__(self, log_queue: queue.Queue):
        super().__init__()
        self.log_queue = log_queue

    def emit(self, record):
        try:
            msg = self.format(record)
            self.log_queue.put_nowait(msg)
        except Exception:
            self.handleError(record)


class YtdlpLoggerAdapter:
    """Routes yt-dlp internal console messages into the application logger."""

    def __init__(self, logger: logging.Logger):
        self.logger = logger

    def debug(self, msg):
        if msg and not msg.startswith("[debug] "):
            self.logger.info(f"[yt-dlp] {msg}")
        elif msg:
            self.logger.debug(f"[yt-dlp] {msg}")

    def info(self, msg):
        if msg:
            self.logger.info(f"[yt-dlp] {msg}")

    def warning(self, msg):
        if msg:
            self.logger.warning(f"[yt-dlp] {msg}")

    def error(self, msg):
        if msg:
            self.logger.error(f"[yt-dlp] {msg}")


def setup_logger(log_queue: queue.Queue) -> tuple[logging.Logger, logging.FileHandler]:
    if os.path.exists(LOG_FILE):
        try:
            os.remove(LOG_FILE)
        except OSError:
            pass

    logger = logging.getLogger("EchoGrab")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    fmt = logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s", datefmt="%H:%M:%S")

    file_handler = logging.FileHandler(LOG_FILE, mode="w", encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    ui_handler = TkQueueHandler(log_queue)
    ui_handler.setLevel(logging.DEBUG)
    ui_handler.setFormatter(fmt)
    logger.addHandler(ui_handler)

    return logger, file_handler


# ---------------- PERSISTENT CONFIG MANAGER ---------------- #
class ConfigManager:
    DEFAULTS = {
        "theme": "dark",
        "microphone_name": "",
        "listen_mode": "stream",
        "fixed_duration": 8,
        "cooldown_seconds": 5,  # Configurable via config.json
        "retain_logs_on_exit": True,
        "target_ext": "mp3",
        "bitrate": "320k",
        "embed_meta": True,
        "embed_thumb": True,
        "out_dir": os.path.join(os.path.expanduser("~"), "Downloads"),
    }

    @classmethod
    def load(cls, logger: logging.Logger) -> dict:
        config = cls.DEFAULTS.copy()
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                config.update(loaded)
                logger.info(f"Loaded configuration from {CONFIG_FILE}")
            except Exception as e:
                logger.warning(f"Failed to read config.json, using defaults: {e}")
        else:
            cls.save(config, logger)
            logger.info("Initialized default config.json.")
        return config

    @classmethod
    def save(cls, config: dict, logger: logging.Logger):
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(config, f, indent=2)
            logger.debug("Saved configuration to config.json")
        except Exception as e:
            logger.error(f"Failed to save config.json: {e}")


# ---------------- CANCELLABLE SHAZAM RATE LIMITER & AUDIO CONDITIONER ---------------- #
class ShazamRateLimiter:
    OPTIMAL_WINDOW_SECONDS = 12

    def __init__(self, logger: logging.Logger):
        self.logger = logger
        self._lock = threading.Lock()
        self.last_request_time = 0.0
        self.penalty_until = 0.0

    def get_remaining_cooldown(self, base_cooldown: float) -> float:
        now = time.time()
        with self._lock:
            normal_wait = max(0.0, (self.last_request_time + base_cooldown) - now)
            penalty_wait = max(0.0, self.penalty_until - now)
            return max(normal_wait, penalty_wait)

    def wait_if_needed(self, base_cooldown: float, cancel_event: threading.Event, status_cb=None) -> bool:
        while not cancel_event.is_set():
            rem = self.get_remaining_cooldown(base_cooldown)
            if rem <= 0.05:
                return True
            if status_cb:
                status_cb(f"Cooldown active ({rem:.1f}s remaining)")
            if cancel_event.wait(timeout=min(0.1, rem)):
                return False
        return False

    def mark_request_completed(self, is_rate_limited: bool = False):
        now = time.time()
        with self._lock:
            self.last_request_time = now
            if is_rate_limited:
                self.penalty_until = now + 15.0
                self.logger.warning("Rate limit detected. Applied 15.0s penalty cooldown.")

    def prepare_optimal_wav(self, pcm_int16: np.ndarray, samplerate: int) -> tuple[bytes | None, float]:
        max_samples = int(self.OPTIMAL_WINDOW_SECONDS * samplerate)
        sliced = pcm_int16[-max_samples:].copy() if len(pcm_int16) > max_samples else pcm_int16.copy()

        float_audio = sliced.astype(np.float32)
        float_audio -= np.mean(float_audio)

        rms = np.sqrt(np.mean(float_audio ** 2)) + 1e-9
        rms_dbfs = 20.0 * np.log10(rms / 32768.0)

        if rms_dbfs < -55.0:
            return None, rms_dbfs

        peak = np.max(np.abs(float_audio))
        if peak > 10.0:
            gain = min(25.0, 29490.0 / peak)
            float_audio = np.clip(float_audio * gain, -32767.0, 32767.0)
            self.logger.debug(
                f"Conditioned {len(sliced)/samplerate:.1f}s window | RMS={rms_dbfs:.1f} dBFS | Gain={gain:.1f}x"
            )

        norm_int16 = float_audio.astype(np.int16)
        wav_buf = io.BytesIO()
        write_wav(wav_buf, samplerate, norm_int16)
        return wav_buf.getvalue(), rms_dbfs

    async def _cancellable_recognize(self, wav_bytes: bytes, cancel_event: threading.Event) -> dict:
        shazam = Shazam(segment_duration_seconds=self.OPTIMAL_WINDOW_SECONDS)
        recognize_task = asyncio.create_task(shazam.recognize(wav_bytes))

        while not recognize_task.done():
            if cancel_event.is_set():
                recognize_task.cancel()
                self.logger.info("Aborted in-flight Shazam network request.")
                try:
                    await recognize_task
                except asyncio.CancelledError:
                    pass
                return {}
            await asyncio.sleep(0.05)

        return recognize_task.result()

    def recognize_safe(
        self, wav_bytes: bytes, base_cooldown: float, cancel_event: threading.Event, status_cb=None
    ) -> dict:
        if not self.wait_if_needed(base_cooldown, cancel_event, status_cb):
            return {}

        t0 = time.time()
        try:
            res = asyncio.run(self._cancellable_recognize(wav_bytes, cancel_event))
            if cancel_event.is_set():
                return {}

            elapsed = time.time() - t0
            self.mark_request_completed(is_rate_limited=False)

            has_match = bool(res and "track" in res)
            matches_list = res.get("matches", []) if isinstance(res, dict) else []
            self.logger.debug(
                f"Shazam response in {elapsed:.2f}s | match={has_match} | candidates={len(matches_list)}"
            )

            if not has_match and elapsed < 0.22:
                self.logger.warning(f"Fast empty response ({elapsed:.2f}s); adding 6s buffer.")
                with self._lock:
                    self.penalty_until = time.time() + 6.0

            return res
        except Exception as e:
            if cancel_event.is_set():
                return {}
            err_str = str(e).lower()
            is_429 = "429" in err_str or "rate" in err_str or "too many" in err_str
            self.mark_request_completed(is_rate_limited=is_429)
            raise


# ---------------- SMART CANDIDATE SCORING & PLATFORM PRIORITY ENGINE ---------------- #
OTHER_PLATFORMS_ORDER = [
    "YouTube Music",
    "YouTube",
    "SoundCloud",
    "Apple Music",
    "Spotify",
    "Tidal",
    "Deezer",
    "Amazon Music",
]

VARIANT_KEYWORDS = [
    "remix", "rework", "bootleg", "vip", "extended", "1 hour", "1hr", "2 hours",
    "10 hours", "loop", "slowed", "reverb", "sped up", "nightcore", "daycore",
    "8d", "bass boosted", "karaoke", "instrumental", "backing track", "tribute",
    "cover", "live", "concert", "acoustic", "mashup", "megamix", "dj mix", "lofi", "lo-fi",
]


def normalize_text(text: str) -> str:
    text = text.lower()
    text = re.sub(r"\((?:feat|ft|with|prod)[^)]*\)", "", text)
    text = re.sub(r"\[(?:feat|ft|with|prod)[^\]]*\]", "", text)
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def compute_variant_penalty(target_title: str, cand_title: str) -> float:
    t_low = target_title.lower()
    c_low = cand_title.lower()
    penalty = 0.0
    for kw in VARIANT_KEYWORDS:
        pattern = rf"\b{re.escape(kw)}\b"
        in_target = bool(re.search(pattern, t_low))
        in_cand = bool(re.search(pattern, c_low))
        if in_cand and not in_target:
            penalty += 0.45
        elif in_target and not in_cand:
            penalty += 0.25
    return penalty


def compute_duration_modifier(
    cand_duration_sec: float | None, ref_duration_sec: float | None
) -> tuple[bool, float]:
    if not cand_duration_sec or cand_duration_sec <= 0:
        return True, 0.0

    max_allowed = max(720.0, (ref_duration_sec or 240.0) * 1.4)
    if cand_duration_sec < 50.0 or cand_duration_sec > max_allowed:
        return False, -1.0

    if ref_duration_sec and ref_duration_sec > 0:
        diff = abs(cand_duration_sec - ref_duration_sec)
        if diff > 45.0:
            return False, -1.0
        elif diff <= 4.0:
            return True, 0.06
        elif diff <= 12.0:
            return True, 0.03
        else:
            return True, -0.05

    if 110.0 <= cand_duration_sec <= 340.0:
        return True, 0.02
    return True, -0.04


def compute_candidate_score(
    target_title: str,
    target_artist: str,
    cand_title: str,
    cand_artist: str = "",
    cand_duration_sec: float | None = None,
    ref_duration_sec: float | None = None,
) -> float:
    allowed, dur_mod = compute_duration_modifier(cand_duration_sec, ref_duration_sec)
    if not allowed:
        return 0.0

    var_penalty = compute_variant_penalty(target_title, f"{cand_title} {cand_artist}")
    t_norm = normalize_text(target_title)
    a_norm = normalize_text(target_artist)

    ct_clean = re.sub(
        r"\b(official|music|video|audio|lyrics|lyric|hd|hq|4k|visualizer|remastered|remaster|explicit|clean|version)\b",
        "",
        normalize_text(cand_title),
    )
    ct_clean = re.sub(r"\s+", " ", ct_clean).strip()
    ca_clean = re.sub(r"\b(topic|vevo|official)\b", "", normalize_text(cand_artist)).strip()

    if ca_clean:
        title_ratio = difflib.SequenceMatcher(None, t_norm, ct_clean).ratio()
        comb_1 = f"{a_norm} {t_norm}".strip()
        comb_2 = f"{t_norm} {a_norm}".strip()
        title_comb_ratio = max(
            difflib.SequenceMatcher(None, comb_1, ct_clean).ratio(),
            difflib.SequenceMatcher(None, comb_2, ct_clean).ratio(),
        )
        best_title_ratio = max(title_ratio, title_comb_ratio)

        if a_norm in ca_clean or ca_clean in a_norm:
            artist_ratio = max(0.94, difflib.SequenceMatcher(None, a_norm, ca_clean).ratio())
        elif a_norm in ct_clean:
            artist_ratio = 0.92
        else:
            artist_ratio = difflib.SequenceMatcher(None, a_norm, ca_clean).ratio()

        base_sim = (best_title_ratio * 0.65) + (artist_ratio * 0.35)
    else:
        comb_1 = f"{a_norm} {t_norm}".strip()
        comb_2 = f"{t_norm} {a_norm}".strip()
        base_sim = max(
            difflib.SequenceMatcher(None, comb_1, ct_clean).ratio(),
            difflib.SequenceMatcher(None, comb_2, ct_clean).ratio(),
            difflib.SequenceMatcher(None, t_norm, ct_clean).ratio(),
        )

    channel_bonus = 0.0
    if "- topic" in cand_artist.lower():
        channel_bonus += 0.05
    elif "vevo" in cand_artist.lower() or "official" in cand_artist.lower():
        channel_bonus += 0.02

    final_score = base_sim - var_penalty + dur_mod + channel_bonus
    return max(0.0, min(1.0, final_score))


def sort_other_platforms(platforms: dict) -> dict:
    ordered = {}
    for p_name in OTHER_PLATFORMS_ORDER:
        if p_name in platforms:
            ordered[p_name] = platforms[p_name]
    return ordered


def resolve_verified_platforms(
    title: str,
    artist: str,
    shazam_track: dict,
    cancel_event: threading.Event,
    logger: logging.Logger,
) -> dict:
    verified_links = {}
    shazam_url = shazam_track.get("url", "")
    ref_duration_sec = None
    threshold = 0.90

    if cancel_event.is_set():
        return {"platforms": {}, "shazam_url": "", "preferred_download_url": "", "ref_duration_sec": None}

    logger.info(f"Resolving canonical platform links for '{title}' by '{artist}'")

    # 1. Apple Music / iTunes API
    apple_track_url = None
    if not cancel_event.is_set():
        try:
            q = urllib.parse.quote(f"{title} {artist}")
            resp = requests.get(f"https://itunes.apple.com/search?term={q}&entity=song&limit=8", timeout=4)
            if resp.ok and not cancel_event.is_set():
                best_apple_score = 0.0
                best_apple_item = None
                for item in resp.json().get("results", []):
                    c_title = item.get("trackName", "")
                    c_artist = item.get("artistName", "")
                    c_dur = (item.get("trackTimeMillis") or 0) / 1000.0
                    score = compute_candidate_score(title, artist, c_title, c_artist, c_dur, None)
                    if score > best_apple_score and item.get("trackViewUrl"):
                        best_apple_score = score
                        best_apple_item = item

                if best_apple_item and best_apple_score >= threshold:
                    apple_track_url = best_apple_item["trackViewUrl"]
                    verified_links["Apple Music"] = apple_track_url
                    if best_apple_item.get("trackTimeMillis"):
                        ref_duration_sec = best_apple_item["trackTimeMillis"] / 1000.0
                    logger.info(f"Matched Apple Music ({best_apple_score*100:.1f}%): {apple_track_url}")
        except Exception as e:
            logger.warning(f"Apple Music lookup skipped: {e}")

    # 2. Deezer API
    if not cancel_event.is_set():
        try:
            q = urllib.parse.quote(f'track:"{title}" artist:"{artist}"')
            resp = requests.get(f"https://api.deezer.com/search?q={q}&limit=8", timeout=4)
            if resp.ok and not cancel_event.is_set():
                best_dz_score = 0.0
                best_dz_item = None
                for item in resp.json().get("data", []):
                    c_title = item.get("title", "")
                    c_artist = item.get("artist", {}).get("name", "")
                    c_dur = float(item.get("duration") or 0)
                    score = compute_candidate_score(title, artist, c_title, c_artist, c_dur, ref_duration_sec)
                    if score > best_dz_score and item.get("link"):
                        best_dz_score = score
                        best_dz_item = item

                if best_dz_item and best_dz_score >= threshold:
                    verified_links["Deezer"] = best_dz_item["link"]
                    if not ref_duration_sec and best_dz_item.get("duration"):
                        ref_duration_sec = float(best_dz_item["duration"])
                    logger.info(f"Matched Deezer ({best_dz_score*100:.1f}%): {best_dz_item['link']}")
        except Exception as e:
            logger.warning(f"Deezer lookup skipped: {e}")

    # 3. Odesli / Songlink API
    seed_url = apple_track_url or verified_links.get("Deezer")
    if seed_url and not cancel_event.is_set():
        try:
            sl_url = f"https://api.song.link/v1-alpha.1/links?url={urllib.parse.quote(seed_url)}"
            sl_resp = requests.get(sl_url, timeout=5)
            if sl_resp.ok and not cancel_event.is_set():
                sl_data = sl_resp.json()
                entities = sl_data.get("entitiesByUniqueId", {})
                links_by_platform = sl_data.get("linksByPlatform", {})

                platform_map = {
                    "youtubeMusic": "YouTube Music",
                    "youtube": "YouTube",
                    "soundcloud": "SoundCloud",
                    "appleMusic": "Apple Music",
                    "spotify": "Spotify",
                    "tidal": "Tidal",
                    "deezer": "Deezer",
                    "amazonMusic": "Amazon Music",
                }

                for p_key, display_name in platform_map.items():
                    if p_key in links_by_platform and display_name not in verified_links:
                        p_info = links_by_platform[p_key]
                        entity_id = p_info.get("entityUniqueId")
                        entity = entities.get(entity_id, {})
                        e_title = entity.get("title", title)
                        e_artist = entity.get("artistName", artist)
                        score = compute_candidate_score(title, artist, e_title, e_artist, None, ref_duration_sec)
                        if score >= threshold and p_info.get("url"):
                            verified_links[display_name] = p_info["url"]
                            logger.info(f"Matched {display_name} ({score*100:.1f}%): {p_info['url']}")
        except Exception as e:
            logger.warning(f"Songlink lookup skipped: {e}")

    # 4. YouTube Music ("- Topic") & YouTube Search via yt-dlp
    if not cancel_event.is_set():
        try:
            ydl_opts = {
                "quiet": True,
                "no_warnings": True,
                "extract_flat": True,
                "skip_download": True,
                "logger": YtdlpLoggerAdapter(logger),
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                queries = [
                    f"ytsearch6:{artist} - {title} Topic",
                    f"ytsearch8:{artist} - {title} official audio",
                ]
                seen_ids = set()
                ytm_candidates = []
                yt_candidates = []

                for q_str in queries:
                    if cancel_event.is_set():
                        break
                    search_res = ydl.extract_info(q_str, download=False)
                    for entry in search_res.get("entries", []) or []:
                        v_id = entry.get("id")
                        if not v_id or v_id in seen_ids:
                            continue
                        seen_ids.add(v_id)

                        v_title = entry.get("title") or ""
                        v_channel = entry.get("uploader") or entry.get("channel") or ""
                        v_dur = float(entry.get("duration") or 0)

                        score = compute_candidate_score(
                            title, artist, v_title, v_channel, v_dur, ref_duration_sec
                        )
                        is_topic = "- topic" in v_channel.lower()
                        logger.debug(
                            f"[YouTube Candidate] '{v_title}' | {v_channel} | {v_dur:.0f}s -> score={score:.2f}"
                        )

                        if score >= threshold:
                            yt_url = f"https://www.youtube.com/watch?v={v_id}"
                            ytm_url = f"https://music.youtube.com/watch?v={v_id}"
                            if is_topic:
                                ytm_candidates.append((score + 0.03, ytm_url, v_id, v_title, v_dur))
                                yt_candidates.append((score + 0.02, yt_url, v_id, v_title, v_dur))
                            else:
                                yt_candidates.append((score, yt_url, v_id, v_title, v_dur))
                                ytm_candidates.append((score - 0.01, ytm_url, v_id, v_title, v_dur))

                ytm_candidates.sort(key=lambda x: x[0], reverse=True)
                yt_candidates.sort(key=lambda x: x[0], reverse=True)

                if ytm_candidates and not cancel_event.is_set():
                    best_ytm = ytm_candidates[0]
                    verified_links["YouTube Music"] = best_ytm[1]
                    logger.info(f"Selected YouTube Music: '{best_ytm[3]}' ({best_ytm[4]:.0f}s) -> {best_ytm[1]}")

                if yt_candidates and not cancel_event.is_set():
                    best_yt = yt_candidates[0]
                    verified_links["YouTube"] = best_yt[1]
                    logger.info(f"Selected YouTube: '{best_yt[3]}' ({best_yt[4]:.0f}s) -> {best_yt[1]}")
        except Exception as e:
            if not cancel_event.is_set():
                logger.warning(f"YouTube candidate evaluation warning: {e}")

    ordered_platforms = sort_other_platforms(verified_links)
    preferred_download_url = (
        ordered_platforms.get("YouTube Music")
        or ordered_platforms.get("YouTube")
        or ordered_platforms.get("SoundCloud")
        or ""
    )

    return {
        "platforms": ordered_platforms,
        "shazam_url": shazam_url,
        "preferred_download_url": preferred_download_url,
        "ref_duration_sec": ref_duration_sec,
    }


# ---------------- MAIN UNIFIED WINDOWS 11 FLUENT APPLICATION ---------------- #
class EchoGrabApp:
    STREAM_CHECKPOINTS = [5, 12, 20, 28]
    ART_SIZE = 148
    ART_CORNER_RADIUS = 10
    SPEC_WINDOW_SECONDS = 6.0
    SPEC_COLS = 1024
    AUDIO_BLOCK_SECONDS = SPEC_WINDOW_SECONDS / SPEC_COLS  # ~5.86ms hop -> 1024 columns across 6.0s window

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("EchoGrab")
        self.root.geometry("1024x768")
        self.root.resizable(False, False)

        self.icon_path = resolve_icon_path()
        self.header_logo_img = None
        if self.icon_path:
            try:
                self.root.iconbitmap(self.icon_path)
            except Exception:
                pass

        # Initialize Live Logger, Config & Rate-Limiter
        self.log_queue = queue.Queue()
        self.logger, self.file_log_handler = setup_logger(self.log_queue)
        self.config = ConfigManager.load(self.logger)
        self.rate_limiter = ShazamRateLimiter(self.logger)

        # Apply Windows 11 Sun Valley Theme (sv-ttk)
        theme_mode = self.config.get("theme", "dark")
        sv_ttk.set_theme(theme_mode)
        apply_win11_titlebar(self.root, dark=(theme_mode == "dark"))
        self._suppress_notebook_focus_ring()

        # Recognizer (Echo) state
        self.devices = []
        self.album_art_raw_pil = None
        self.album_art_image = None
        self.placeholder_art_image = None
        self.is_listening = False
        self.stop_event = threading.Event()
        self.session_id = 0
        self.last_recognized_info = {}

        # Per-tab Header Status Variables
        self.echo_status_text = "Ready"
        self.grab_status_text = "Enter a direct song URL and fetch streams."
        self.console_status_text = f"Log file: {LOG_FILE}"
        self.header_status_var = tk.StringVar(value=self.echo_status_text)

        # 6-Second High-Res Mel Spectrogram State (1024 columns x 96 Mel bins, 20 Hz - 24,000 Hz)
        self.spec_cols = self.SPEC_COLS
        self.spec_rows = 96
        self.n_fft = 2048
        self.mel_fmin = 20.0
        self.mel_fmax = 24000.0
        self.mel_filterbank = None
        self.mel_filterbank_sr = 0
        self.audio_ring_buffer = np.zeros(self.n_fft, dtype=np.float32)

        self.spec_data = np.zeros((self.spec_rows, self.spec_cols), dtype=np.float32)
        self.spec_lut = self._build_colormap_lut()
        self.spec_tk_img = None
        self.spec_canvas_w = 932
        self.spec_canvas_h = 110
        self.audio_viz_queue = queue.Queue(maxsize=256)

        # Downloader (Grab) tab state
        self.ytdlp_info = {}
        self.available_formats = []
        self.raw_pil_image = None
        self.cropped_pil_image = None
        self.display_tk_image = None
        self.scale_factor = 1.0
        self.offset_x = 0
        self.offset_y = 0
        self.crop_rect_id = None
        self.drag_start = None
        self.crop_coords_pil = None

        self._build_ui()
        self._apply_theme_colors()
        self._load_microphones()
        self._apply_saved_ui_state()
        self._poll_queues()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------------- STYLE HELPERS, MEL FILTERBANK & COLORMAP ---------------- #
    def _suppress_notebook_focus_ring(self):
        """Removes the dashed focus rectangle on Notebook tabs when clicked or double-clicked."""
        style = ttk.Style(self.root)
        try:
            style.layout(
                "TNotebook.Tab",
                [
                    (
                        "Notebook.tab",
                        {
                            "sticky": "nswe",
                            "children": [
                                (
                                    "Notebook.padding",
                                    {
                                        "side": "top",
                                        "sticky": "nswe",
                                        "children": [("Notebook.label", {"side": "top", "sticky": ""})],
                                    },
                                )
                            ],
                        },
                    )
                ],
            )
        except Exception:
            pass

    @staticmethod
    def _build_colormap_lut() -> np.ndarray:
        lut = np.zeros((256, 3), dtype=np.uint8)
        for i in range(256):
            t = i / 255.0
            if t < 0.33:
                s = t / 0.33
                r, g, b = int(18 + 70 * s), int(18 + 20 * s), int(30 + 110 * s)
            elif t < 0.66:
                s = (t - 0.33) / 0.33
                r, g, b = int(88 + 130 * s), int(38 + 70 * s), int(140 - 80 * s)
            else:
                s = (t - 0.66) / 0.34
                r, g, b = int(218 + 37 * s), int(108 + 140 * s), int(60 + 140 * s)
            lut[i] = [r, g, b]
        return lut

    def _get_mel_filterbank(self, samplerate: int) -> np.ndarray:
        """Builds a 96-bin triangular Mel filterbank from 20 Hz up to 24,000 Hz (clamped to Nyquist)."""
        if self.mel_filterbank is not None and self.mel_filterbank_sr == samplerate:
            return self.mel_filterbank

        f_min = self.mel_fmin
        f_max = min(self.mel_fmax, (samplerate / 2.0) - 1.0)

        def hz_to_mel(hz):
            return 2595.0 * np.log10(1.0 + hz / 700.0)

        def mel_to_hz(mel):
            return 700.0 * (10.0 ** (mel / 2595.0) - 1.0)

        mel_points = np.linspace(hz_to_mel(f_min), hz_to_mel(f_max), self.spec_rows + 2)
        hz_points = mel_to_hz(mel_points)
        bin_points = np.floor((self.n_fft + 1) * hz_points / samplerate).astype(int)

        num_fft_bins = self.n_fft // 2 + 1
        fb = np.zeros((self.spec_rows, num_fft_bins), dtype=np.float32)

        for m in range(1, self.spec_rows + 1):
            f_left = bin_points[m - 1]
            f_center = bin_points[m]
            f_right = bin_points[m + 1]

            if f_center <= f_left:
                f_center = f_left + 1
            if f_right <= f_center:
                f_right = f_center + 1

            for k in range(f_left, min(f_center, num_fft_bins)):
                fb[m - 1, k] = (k - f_left) / float(f_center - f_left)
            for k in range(f_center, min(f_right, num_fft_bins)):
                fb[m - 1, k] = (f_right - k) / float(f_right - f_center)

        self.mel_filterbank = fb
        self.mel_filterbank_sr = samplerate
        return fb

    def _create_fluent_album_placeholder(self) -> ImageTk.PhotoImage:
        """Generates a rounded-corner square placeholder with #2F2F2F / #E7E7E7 theme colors."""
        is_dark = sv_ttk.get_theme() == "dark"
        card_bg = (28, 28, 28) if is_dark else (250, 250, 250)
        box_fill = (0x2F, 0x2F, 0x2F) if is_dark else (0xE7, 0xE7, 0xE7)
        fg_and_border = (0xE7, 0xE7, 0xE7) if is_dark else (0x2F, 0x2F, 0x2F)

        scale = 4
        S = self.ART_SIZE * scale
        img = Image.new("RGB", (S, S), card_bg)
        draw = ImageDraw.Draw(img)

        pad = 2 * scale
        radius = self.ART_CORNER_RADIUS * scale
        draw.rounded_rectangle(
            [pad, pad, S - pad - 1, S - pad - 1],
            radius=radius,
            fill=box_fill,
            outline=fg_and_border,
            width=scale,
        )

        cx, cy = S // 2, S // 2
        stem_top = cy - 26 * scale
        stem_bot = cy + 16 * scale
        left_x = cx - 14 * scale
        right_x = cx + 22 * scale
        stroke = 4 * scale

        draw.line([(left_x, stem_top + 6 * scale), (left_x, stem_bot)], fill=fg_and_border, width=stroke)
        draw.line([(right_x, stem_top), (right_x, stem_bot - 6 * scale)], fill=fg_and_border, width=stroke)

        beam_poly = [
            (left_x - stroke // 2, stem_top + 4 * scale),
            (right_x + stroke // 2, stem_top - 2 * scale),
            (right_x + stroke // 2, stem_top + 8 * scale),
            (left_x - stroke // 2, stem_top + 14 * scale),
        ]
        draw.polygon(beam_poly, fill=fg_and_border)

        r_x, r_y = 10 * scale, 7 * scale
        draw.ellipse(
            [left_x - r_x * 1.6, stem_bot - r_y, left_x + stroke // 2, stem_bot + r_y],
            fill=fg_and_border,
        )
        draw.ellipse(
            [right_x - r_x * 1.6, stem_bot - 6 * scale - r_y, right_x + stroke // 2, stem_bot - 6 * scale + r_y],
            fill=fg_and_border,
        )

        smooth = img.resize((self.ART_SIZE, self.ART_SIZE), Image.Resampling.LANCZOS)
        return ImageTk.PhotoImage(smooth)

    def _render_rounded_album_art(self, pil_img: Image.Image) -> ImageTk.PhotoImage:
        """Stretches any aspect-ratio image to a square and applies anti-aliased rounded corners."""
        is_dark = sv_ttk.get_theme() == "dark"
        card_bg = (28, 28, 28) if is_dark else (250, 250, 250)

        # Fill-stretch to exact square (ART_SIZE x ART_SIZE) regardless of source aspect ratio
        stretched = pil_img.convert("RGB").resize(
            (self.ART_SIZE, self.ART_SIZE), Image.Resampling.LANCZOS
        )

        # Create 4x super-sampled rounded rectangle mask for smooth anti-aliased corners
        scale = 4
        S = self.ART_SIZE * scale
        pad = 2 * scale
        radius = self.ART_CORNER_RADIUS * scale

        mask_hi = Image.new("L", (S, S), 0)
        mask_draw = ImageDraw.Draw(mask_hi)
        mask_draw.rounded_rectangle(
            [pad, pad, S - pad - 1, S - pad - 1],
            radius=radius,
            fill=255,
        )
        mask = mask_hi.resize((self.ART_SIZE, self.ART_SIZE), Image.Resampling.LANCZOS)

        bg_img = Image.new("RGB", (self.ART_SIZE, self.ART_SIZE), card_bg)
        composited = Image.composite(stretched, bg_img, mask)
        return ImageTk.PhotoImage(composited)

    # ---------------- UI CONSTRUCTION ---------------- #
    def _build_ui(self):
        # Top Header Bar: Vertically Centered Logo + "EchoGrab" Title + Context Status + Toggle Theme Button
        header = ttk.Frame(self.root, padding=(20, 14, 20, 6))
        header.pack(fill="x")
        header.columnconfigure(2, weight=1)

        col_idx = 0
        if self.icon_path:
            try:
                ico_img = Image.open(self.icon_path).convert("RGBA")
                ico_img = ico_img.resize((28, 28), Image.Resampling.LANCZOS)
                self.header_logo_img = ImageTk.PhotoImage(ico_img)
                logo_lbl = ttk.Label(header, image=self.header_logo_img)
                logo_lbl.grid(row=0, column=col_idx, sticky="w", padx=(0, 10))
                col_idx += 1
            except Exception as e:
                self.logger.debug(f"Could not render header logo from app.ico: {e}")

        title_lbl = ttk.Label(header, text="EchoGrab", font=("Segoe UI Variable Display", 16, "bold"))
        title_lbl.grid(row=0, column=col_idx, sticky="w", padx=(0, 18))

        # Status moved 2 pixels lower via pady=(2, 0)
        self.header_status_lbl = ttk.Label(
            header, textvariable=self.header_status_var, font=("Segoe UI", 9), anchor="w"
        )
        self.header_status_lbl.grid(row=0, column=2, sticky="ew", padx=(4, 16), pady=(2, 0))

        self.theme_btn = ttk.Button(header, text="Toggle Theme", width=14, command=self._toggle_theme)
        self.theme_btn.grid(row=0, column=3, sticky="e")

        # Main 3-Tab Notebook (Echo, Grab, Console)
        self.notebook = ttk.Notebook(self.root, takefocus=False)
        self.notebook.pack(fill="both", expand=True, padx=20, pady=(4, 20))

        self.tab_recognizer = ttk.Frame(self.notebook, padding=16)
        self.tab_downloader = ttk.Frame(self.notebook, padding=16)
        self.tab_console = ttk.Frame(self.notebook, padding=16)

        self.notebook.add(self.tab_recognizer, text="  Echo  ")
        self.notebook.add(self.tab_downloader, text="  Grab  ")
        self.notebook.add(self.tab_console, text="  Console  ")
        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)

        self._build_recognizer_tab()
        self._build_downloader_tab()
        self._build_console_tab()

    def _build_recognizer_tab(self):
        # 1. Audio Capture Card (Status moved to header)
        ctrl_card = ttk.LabelFrame(self.tab_recognizer, text="Audio Capture", padding=14)
        ctrl_card.pack(fill="x", pady=(0, 12))

        row1 = ttk.Frame(ctrl_card)
        row1.pack(fill="x", pady=(0, 10))

        ttk.Label(row1, text="Input Device", width=13).pack(side="left")
        self.mic_combo = ttk.Combobox(row1, state="readonly")
        self.mic_combo.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.mic_combo.bind("<<ComboboxSelected>>", lambda e: self.save_preferences())

        ttk.Button(row1, text="Refresh", width=10, command=self._load_microphones).pack(side="right")

        row2 = ttk.Frame(ctrl_card)
        row2.pack(fill="x")

        ttk.Label(row2, text="Mode", width=13).pack(side="left")
        self.listen_mode_var = tk.StringVar(value=self.config.get("listen_mode", "stream"))
        ttk.Radiobutton(
            row2, text="Progressive Stream", value="stream",
            variable=self.listen_mode_var, command=self._on_mode_change
        ).pack(side="left", padx=(0, 18))

        ttk.Radiobutton(
            row2, text="Fixed Duration", value="fixed",
            variable=self.listen_mode_var, command=self._on_mode_change
        ).pack(side="left", padx=(0, 8))

        saved_dur = max(4, min(14, int(self.config.get("fixed_duration", 8))))
        self.duration_values = [f"{i} seconds" for i in range(4, 15)]
        self.duration_var = tk.StringVar(value=f"{saved_dur} seconds")
        self.duration_spin = ttk.Spinbox(
            row2,
            values=self.duration_values,
            textvariable=self.duration_var,
            width=12,
            state="readonly",
            command=self.save_preferences,
        )
        self.duration_spin.pack(side="left")

        self.listen_btn = ttk.Button(
            row2, text="Start Listening", style="Accent.TButton", width=18, command=self.toggle_listening
        )
        self.listen_btn.pack(side="right")

        # 2. Spectrogram Card (Hidden at startup; shown when recording starts)
        self.spec_card = ttk.LabelFrame(self.tab_recognizer, text="Spectrogram", padding=12)

        self.spec_label = tk.Label(
            self.spec_card, bg="#12121e", height=self.spec_canvas_h, bd=0, highlightthickness=0
        )
        self.spec_label.pack(fill="x", expand=True)
        self.spec_label.bind("<Configure>", self._on_spec_resize)

        # 3. Recognized Track Card (Hidden until a match is found)
        self.result_card = ttk.LabelFrame(self.tab_recognizer, text="Recognized Track", padding=16)

        res_top = ttk.Frame(self.result_card)
        res_top.pack(fill="x")
        res_top.columnconfigure(1, weight=1)
        res_top.rowconfigure(0, weight=1)
        res_top.rowconfigure(1, weight=0)

        # Album Art (spans rows 0 and 1 on the left)
        self.art_label = ttk.Label(res_top)
        self.art_label.grid(row=0, column=0, rowspan=2, sticky="nw", padx=(0, 18))

        # Top-Right of Album Art: Track Metadata
        meta_top_frame = ttk.Frame(res_top)
        meta_top_frame.grid(row=0, column=1, sticky="new")

        self.title_lbl = ttk.Label(meta_top_frame, font=("Segoe UI Variable Display", 15, "bold"), wraplength=740)
        self.artist_lbl = ttk.Label(meta_top_frame, font=("Segoe UI", 11), wraplength=740)
        self.album_lbl = ttk.Label(meta_top_frame, font=("Segoe UI", 9), wraplength=740)

        # Bottom-Right of Album Art: Action Buttons (Download Track + Open in Shazam)
        self.action_row = ttk.Frame(res_top)
        self.action_row.grid(row=1, column=1, sticky="sew", pady=(8, 0))

        self.dl_jump_btn = ttk.Button(
            self.action_row, text="Download Track", style="Accent.TButton", width=18, command=self.switch_to_downloader
        )
        self.dl_jump_btn.pack(side="left", padx=(0, 8))

        self.shazam_btn = ttk.Button(
            self.action_row, text="Open in Shazam", width=16, command=self._open_shazam_url
        )

        # Other Platforms Section (2 rows x 4 columns)
        self.platforms_sep = ttk.Separator(self.result_card, orient="horizontal")
        self.platforms_header = ttk.Label(self.result_card, text="Other Platforms", font=("Segoe UI", 9, "bold"))
        self.links_frame = ttk.Frame(self.result_card)

    def _build_downloader_tab(self):
        pane = ttk.Frame(self.tab_downloader)
        pane.pack(fill="both", expand=True)
        pane.columnconfigure(0, weight=1)
        pane.columnconfigure(1, weight=0)
        pane.rowconfigure(0, weight=1)

        # Left Column: Source, Stream Quality, Format, Metadata, Output
        left_col = ttk.Frame(pane)
        left_col.grid(row=0, column=0, sticky="nsew", padx=(0, 14))

        # Source URL
        src_card = ttk.LabelFrame(left_col, text="Source URL", padding=12)
        src_card.pack(fill="x", pady=(0, 10))

        self.url_var = tk.StringVar()
        ttk.Entry(src_card, textvariable=self.url_var).pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.fetch_btn = ttk.Button(src_card, text="Fetch Streams", width=14, command=self.fetch_ytdlp_streams)
        self.fetch_btn.pack(side="right")

        # Stream & Output Format (Status moved to header)
        fmt_card = ttk.LabelFrame(left_col, text="Stream & Output Format", padding=12)
        fmt_card.pack(fill="x", pady=(0, 10))

        stream_row = ttk.Frame(fmt_card)
        stream_row.pack(fill="x", pady=(0, 6))
        ttk.Label(stream_row, text="Stream", width=8).pack(side="left")
        self.quality_combo = ttk.Combobox(stream_row, state="readonly")
        self.quality_combo.pack(side="left", fill="x", expand=True)
        self.quality_combo["values"] = ["bestaudio/best (Automatic)"]
        self.quality_combo.current(0)

        # Format and Bitrate fixed immediately next to each other
        fmt_subrow = ttk.Frame(fmt_card)
        fmt_subrow.pack(fill="x", pady=(4, 0))

        ttk.Label(fmt_subrow, text="Format", width=8).pack(side="left")
        self.target_ext_var = tk.StringVar(value=self.config.get("target_ext", "mp3"))
        fmt_combo = ttk.Combobox(
            fmt_subrow, textvariable=self.target_ext_var,
            values=["mp3", "m4a", "flac", "opus", "wav"], state="readonly", width=10
        )
        fmt_combo.pack(side="left", padx=(0, 16))
        fmt_combo.bind("<<ComboboxSelected>>", lambda e: self.save_preferences())

        ttk.Label(fmt_subrow, text="Bitrate").pack(side="left", padx=(0, 8))
        self.bitrate_var = tk.StringVar(value=self.config.get("bitrate", "320k"))
        bitrate_combo = ttk.Combobox(
            fmt_subrow, textvariable=self.bitrate_var,
            values=["320k", "256k", "192k", "128k", "Lossless"], state="readonly", width=12
        )
        bitrate_combo.pack(side="left")
        bitrate_combo.bind("<<ComboboxSelected>>", lambda e: self.save_preferences())

        # Metadata Tags
        meta_card = ttk.LabelFrame(left_col, text="Metadata", padding=12)
        meta_card.pack(fill="x", pady=(0, 10))

        meta_top = ttk.Frame(meta_card)
        meta_top.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))

        self.embed_meta_var = tk.BooleanVar(value=self.config.get("embed_meta", True))
        ttk.Checkbutton(
            meta_top, text="Embed Metadata", variable=self.embed_meta_var, command=self.save_preferences
        ).pack(side="left")

        ttk.Button(meta_top, text="Load yt-dlp Tags", command=self._apply_ytdlp_meta).pack(side="right", padx=(6, 0))
        ttk.Button(meta_top, text="Load Recognized Tags", command=self._apply_shazam_meta).pack(side="right")

        fields = [("Title", "meta_title"), ("Artist", "meta_artist"), ("Album", "meta_album"), ("Year", "meta_year")]
        self.meta_vars = {}
        for idx, (label_text, key) in enumerate(fields, start=1):
            ttk.Label(meta_card, text=label_text, width=8).grid(row=idx, column=0, sticky="w", pady=3)
            var = tk.StringVar()
            self.meta_vars[key] = var
            ttk.Entry(meta_card, textvariable=var).grid(row=idx, column=1, sticky="ew", pady=3)
        meta_card.columnconfigure(1, weight=1)

        # Output Folder & Execution
        out_card = ttk.LabelFrame(left_col, text="Output", padding=12)
        out_card.pack(fill="both", expand=True)

        dir_row = ttk.Frame(out_card)
        dir_row.pack(fill="x", pady=(0, 8))
        self.out_dir_var = tk.StringVar(
            value=self.config.get("out_dir", os.path.join(os.path.expanduser("~"), "Downloads"))
        )
        ttk.Entry(dir_row, textvariable=self.out_dir_var).pack(side="left", fill="x", expand=True, padx=(0, 8))
        ttk.Button(dir_row, text="Browse", width=10, command=self._choose_dir).pack(side="right")

        self.progress = ttk.Progressbar(out_card, mode="determinate")
        self.progress.pack(fill="x", pady=(4, 10))

        self.download_btn = ttk.Button(
            out_card, text="Download and Convert", style="Accent.TButton", command=self.start_download_pipeline
        )
        self.download_btn.pack(fill="x")

        # Right Column: Cover Art Cropper (340x340 preview matching section)
        right_col = ttk.LabelFrame(pane, text="Cover Art", padding=12)
        right_col.grid(row=0, column=1, sticky="nsew")

        self.embed_thumb_var = tk.BooleanVar(value=self.config.get("embed_thumb", True))
        ttk.Checkbutton(
            right_col, text="Embed Cover Art", variable=self.embed_thumb_var, command=self.save_preferences
        ).pack(anchor="w", pady=(0, 8))

        thumb_btns = ttk.Frame(right_col)
        thumb_btns.pack(fill="x", pady=(0, 8))
        ttk.Button(thumb_btns, text="Shazam", command=self._load_shazam_thumb).pack(
            side="left", expand=True, fill="x", padx=(0, 3)
        )
        ttk.Button(thumb_btns, text="Source", command=self._load_ytdlp_thumb).pack(
            side="left", expand=True, fill="x", padx=3
        )
        ttk.Button(thumb_btns, text="Browse File", command=self._load_local_thumb).pack(
            side="left", expand=True, fill="x", padx=(3, 0)
        )

        self.canvas_size = 340
        self.canvas = tk.Canvas(
            right_col, width=self.canvas_size, height=self.canvas_size,
            bg="#2F2F2F", bd=0, highlightthickness=1, highlightbackground="#3A3A3A", cursor="cross"
        )
        self.canvas.pack(pady=6)
        self.canvas.bind("<ButtonPress-1>", self._on_crop_start)
        self.canvas.bind("<B1-Motion>", self._on_crop_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_crop_end)

        crop_btns = ttk.Frame(right_col)
        crop_btns.pack(fill="x", pady=6)
        ttk.Button(crop_btns, text="Square Center", command=self.crop_square_center).pack(
            side="left", expand=True, fill="x", padx=(0, 3)
        )
        ttk.Button(crop_btns, text="Apply Selection", command=self.apply_drawn_crop).pack(
            side="left", expand=True, fill="x", padx=3
        )
        ttk.Button(crop_btns, text="Reset", command=self.reset_crop).pack(
            side="left", expand=True, fill="x", padx=(3, 0)
        )

        self.crop_info_var = tk.StringVar(value="")
        ttk.Label(right_col, textvariable=self.crop_info_var, font=("Segoe UI", 8)).pack(anchor="w", pady=(4, 0))

    def _build_console_tab(self):
        top_bar = ttk.Frame(self.tab_console)
        top_bar.pack(fill="x", pady=(0, 8))

        self.retain_logs_var = tk.BooleanVar(value=self.config.get("retain_logs_on_exit", True))
        ttk.Checkbutton(
            top_bar,
            text="Retain console log on exit",
            variable=self.retain_logs_var,
            command=self.save_preferences,
        ).pack(side="left")

        ttk.Button(top_bar, text="Clear Console", width=14, command=self._clear_console_view).pack(side="right")

        # Scrollable Text widget without visible scrollbar
        self.log_text = tk.Text(
            self.tab_console,
            font=("Cascadia Mono", 9),
            bg="#101014",
            fg="#d4d4d4",
            insertbackground="#ffffff",
            state="disabled",
            bd=0,
            padx=10,
            pady=8,
            wrap="word",
            highlightthickness=1,
            highlightbackground="#2d2d30",
        )
        self.log_text.pack(fill="both", expand=True)
        self.log_text.bind("<MouseWheel>", self._on_console_mousewheel)

    def _on_console_mousewheel(self, event):
        self.log_text.yview_scroll(int(-1 * (event.delta / 120)), "units")
        return "break"

    # ---------------- HEADER STATUS & THEME MANAGEMENT ---------------- #
    def _on_tab_changed(self, event=None):
        self._refresh_header_status()

    def _refresh_header_status(self):
        try:
            current_tab = self.notebook.index(self.notebook.select())
        except Exception:
            current_tab = 0

        if current_tab == 0:
            self.header_status_var.set(self.echo_status_text)
        elif current_tab == 1:
            self.header_status_var.set(self.grab_status_text)
        else:
            self.header_status_var.set(self.console_status_text)

    def _set_status(self, message: str):
        def _update():
            self.echo_status_text = message
            self._refresh_header_status()
        self.root.after(0, _update)

    def _set_grab_status(self, message: str):
        def _update():
            self.grab_status_text = message
            self._refresh_header_status()
        self.root.after(0, _update)

    def _apply_theme_colors(self):
        """Updates custom Canvas, Console Text, Spectrogram, and Album Art colors for Dark/Light modes."""
        is_dark = sv_ttk.get_theme() == "dark"

        # Cover Art Crop Canvas (#2F2F2F in dark mode, #E7E7E7 in light mode)
        canvas_bg = "#2F2F2F" if is_dark else "#E7E7E7"
        canvas_border = "#3F3F3F" if is_dark else "#CCCCCC"
        self.canvas.config(bg=canvas_bg, highlightbackground=canvas_border)

        # Console Text Widget (Dark vs Light color scheme)
        if is_dark:
            self.log_text.config(
                bg="#101014",
                fg="#D4D4D4",
                insertbackground="#FFFFFF",
                selectbackground="#264F78",
                selectforeground="#FFFFFF",
                highlightbackground="#2D2D30",
            )
        else:
            self.log_text.config(
                bg="#F5F5F7",
                fg="#1E1E1E",
                insertbackground="#000000",
                selectbackground="#ADD6FF",
                selectforeground="#000000",
                highlightbackground="#D0D0D5",
            )

        # Spectrogram Label background & re-render with inverted LUT if light mode
        spec_bg = "#12121e" if is_dark else "#edede1"
        self.spec_label.config(bg=spec_bg)
        if self.spec_card.winfo_ismapped():
            self._render_spectrogram_frame()

        # Re-render Recognizer Album Art or Placeholder so rounded corners match card background
        if self.last_recognized_info:
            if self.album_art_raw_pil is not None:
                self.album_art_image = self._render_rounded_album_art(self.album_art_raw_pil)
                self.art_label.config(image=self.album_art_image)
            else:
                self.placeholder_art_image = self._create_fluent_album_placeholder()
                self.art_label.config(image=self.placeholder_art_image)

    def _toggle_theme(self):
        current = sv_ttk.get_theme()
        new_theme = "light" if current == "dark" else "dark"
        sv_ttk.set_theme(new_theme)
        apply_win11_titlebar(self.root, dark=(new_theme == "dark"))
        self._suppress_notebook_focus_ring()
        self.config["theme"] = new_theme
        self._apply_theme_colors()
        self.save_preferences()

    def _apply_saved_ui_state(self):
        self._on_mode_change(save=False)

    def _on_mode_change(self, save: bool = True):
        if self.listen_mode_var.get() == "stream":
            self.duration_spin.config(state="disabled")
        else:
            self.duration_spin.config(state="readonly")
        if save:
            self.save_preferences()

    def _parse_fixed_duration(self) -> int:
        raw = self.duration_var.get().split()[0]
        try:
            return max(4, min(14, int(raw)))
        except ValueError:
            return 8

    def save_preferences(self):
        if self.devices and self.mic_combo.current() >= 0:
            self.config["microphone_name"] = self.mic_combo.get()
        self.config["listen_mode"] = self.listen_mode_var.get()
        self.config["fixed_duration"] = self._parse_fixed_duration()
        self.config["retain_logs_on_exit"] = bool(self.retain_logs_var.get())
        self.config["target_ext"] = self.target_ext_var.get()
        self.config["bitrate"] = self.bitrate_var.get()
        self.config["embed_meta"] = bool(self.embed_meta_var.get())
        self.config["embed_thumb"] = bool(self.embed_thumb_var.get())
        self.config["out_dir"] = self.out_dir_var.get()
        ConfigManager.save(self.config, self.logger)

    def _clear_console_view(self):
        self.log_text.config(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.config(state="disabled")

    def _on_close(self):
        self.stop_event.set()
        self.save_preferences()
        retain = bool(self.retain_logs_var.get())
        self.logger.info("EchoGrab closing.")

        if self.file_log_handler:
            try:
                self.logger.removeHandler(self.file_log_handler)
                self.file_log_handler.flush()
                self.file_log_handler.close()
            except Exception:
                pass

        if not retain and os.path.exists(LOG_FILE):
            try:
                os.remove(LOG_FILE)
            except OSError:
                pass

        self.root.destroy()

    # ---------------- QUEUE POLLING & 6-SECOND 1024-RES MEL SPECTROGRAM ---------------- #
    def _on_spec_resize(self, event):
        if event.width > 100 and abs(event.width - self.spec_canvas_w) > 4:
            self.spec_canvas_w = event.width
            self._render_spectrogram_frame()

    def _show_spectrogram_card(self):
        if not self.spec_card.winfo_ismapped():
            if self.result_card.winfo_ismapped():
                self.spec_card.pack(fill="x", pady=(0, 12), before=self.result_card)
            else:
                self.spec_card.pack(fill="x", pady=(0, 12))
            self.root.update_idletasks()
            w = self.spec_label.winfo_width()
            if w > 100:
                self.spec_canvas_w = w
            self._render_spectrogram_frame()

    def _poll_queues(self):
        while not self.log_queue.empty():
            try:
                msg = self.log_queue.get_nowait()
                self.log_text.config(state="normal")
                self.log_text.insert("end", msg + "\n")
                self.log_text.see("end")
                self.log_text.config(state="disabled")
            except queue.Empty:
                break

        cols_batch = []
        while not self.audio_viz_queue.empty():
            try:
                cols_batch.append(self.audio_viz_queue.get_nowait())
            except queue.Empty:
                break

        if cols_batch:
            k = len(cols_batch)
            if k >= self.spec_cols:
                self.spec_data[:, :] = np.column_stack(cols_batch[-self.spec_cols :])
            else:
                self.spec_data = np.roll(self.spec_data, -k, axis=1)
                self.spec_data[:, -k:] = np.column_stack(cols_batch)

            if self.spec_card.winfo_ismapped():
                self._render_spectrogram_frame()

        self.root.after(25, self._poll_queues)

    def _render_spectrogram_frame(self):
        indices = np.clip(self.spec_data * 255.0, 0, 255).astype(np.uint8)
        lut = self.spec_lut if sv_ttk.get_theme() == "dark" else (255 - self.spec_lut)
        rgb_arr = lut[indices[::-1, :]]
        pil_img = Image.fromarray(rgb_arr, mode="RGB").resize(
            (self.spec_canvas_w, self.spec_canvas_h), Image.Resampling.BILINEAR
        )
        self.spec_tk_img = ImageTk.PhotoImage(pil_img)
        self.spec_label.config(image=self.spec_tk_img)

    def _feed_spectrogram_chunk(self, mono_float_chunk: np.ndarray, samplerate: int):
        """Updates the rolling 2048-sample buffer and computes a Mel-scale column (20 Hz - 24,000 Hz)."""
        n = len(mono_float_chunk)
        if n == 0:
            return
        if n >= self.n_fft:
            self.audio_ring_buffer[:] = mono_float_chunk[-self.n_fft :]
        else:
            self.audio_ring_buffer = np.roll(self.audio_ring_buffer, -n)
            self.audio_ring_buffer[-n:] = mono_float_chunk

        windowed = self.audio_ring_buffer * np.hanning(self.n_fft)
        power_spec = np.abs(np.fft.rfft(windowed)) ** 2
        mel_fb = self._get_mel_filterbank(samplerate)
        mel_energy = np.dot(mel_fb, power_spec)
        mel_db = 10.0 * np.log10(mel_energy + 1e-10)
        norm = np.clip((mel_db + 45.0) / 55.0, 0.0, 1.0)
        try:
            self.audio_viz_queue.put_nowait(norm)
        except queue.Full:
            pass

    # ---------------- MICROPHONE ENUMERATION ---------------- #
    def _load_microphones(self):
        self.devices = []
        device_names = []
        saved_mic = self.config.get("microphone_name", "")
        try:
            all_devices = sd.query_devices()
            default_in = sd.default.device[0]
            default_sel = 0
            for idx, dev in enumerate(all_devices):
                if dev.get("max_input_channels", 0) > 0:
                    api_name = sd.query_hostapis(dev["hostapi"]).get("name", "")
                    label = f"[{idx}] {dev['name']} ({api_name})"
                    if label == saved_mic:
                        default_sel = len(self.devices)
                    elif not saved_mic and idx == default_in:
                        default_sel = len(self.devices)
                    self.devices.append((idx, dev))
                    device_names.append(label)

            self.mic_combo["values"] = device_names
            if device_names:
                self.mic_combo.current(default_sel)
                self.logger.info(f"Loaded {len(device_names)} input devices.")
        except Exception as e:
            self.logger.error(f"Device query error: {e}")
            messagebox.showerror("Audio Device Error", f"Failed to query audio devices:\n{e}")

    # ---------------- AUDIO RECOGNITION WITH INSTANT CANCELLATION ---------------- #
    def toggle_listening(self):
        if self.is_listening:
            self.logger.info("Stop requested; cancelling capture and pending network requests.")
            self.stop_event.set()
            self.session_id += 1
            self.is_listening = False
            self.listen_btn.config(text="Start Listening")
            self._set_status("Stopped")
            return

        if not self.devices:
            return

        self.save_preferences()
        combo_idx = self.mic_combo.current()
        device_id, device_info = self.devices[combo_idx]
        mode = self.listen_mode_var.get()
        fixed_dur = self._parse_fixed_duration()
        cooldown_sec = max(1, int(self.config.get("cooldown_seconds", 5)))

        self.is_listening = True
        self.stop_event = threading.Event()
        self.session_id += 1
        current_session = self.session_id

        self.spec_data.fill(0.0)
        self.audio_ring_buffer.fill(0.0)
        self._hide_results()
        self._show_spectrogram_card()
        self.listen_btn.config(text="Stop")

        self.logger.info(
            f"Recognition started on [{device_id}] '{device_info.get('name')}' "
            f"(mode={mode}, duration={fixed_dur}s, cooldown={cooldown_sec}s)"
        )

        threading.Thread(
            target=self._capture_and_recognize_worker,
            args=(current_session, self.stop_event, device_id, device_info, mode, fixed_dur, cooldown_sec),
            daemon=True,
        ).start()

    def _capture_and_recognize_worker(
        self,
        session_id: int,
        cancel_event: threading.Event,
        device_id: int,
        device_info: dict,
        mode: str,
        fixed_dur: int,
        cooldown_sec: int,
    ):
        def update_status_if_active(msg: str):
            if not cancel_event.is_set() and self.session_id == session_id:
                self._set_status(msg)

        rem_cd = self.rate_limiter.get_remaining_cooldown(cooldown_sec)
        if rem_cd > 0.1:
            self.logger.info(f"Waiting {rem_cd:.1f}s cooldown before recording.")
            if not self.rate_limiter.wait_if_needed(cooldown_sec, cancel_event, update_status_if_active):
                return

        # Use at least 48000 Hz when supported so Nyquist reaches 24,000 Hz
        dev_sr = int(device_info.get("default_samplerate", 48000))
        samplerate = max(48000, dev_sr)
        try:
            sd.check_input_settings(device=device_id, channels=1, dtype="int16", samplerate=samplerate)
        except Exception:
            samplerate = dev_sr

        max_duration = self.STREAM_CHECKPOINTS[-1] if mode == "stream" else fixed_dur
        checkpoints = list(self.STREAM_CHECKPOINTS) if mode == "stream" else [fixed_dur]

        recorded_chunks = []
        chunk_lock = threading.Lock()
        next_cp_idx = 0
        match_found = threading.Event()
        active_query_lock = threading.Lock()

        def audio_callback(indata, frames, time_info, status):
            if cancel_event.is_set():
                return
            pcm_copy = indata[:, 0].copy()
            with chunk_lock:
                recorded_chunks.append(pcm_copy)
            self._feed_spectrogram_chunk(pcm_copy.astype(np.float32) / 32768.0, samplerate)

        def query_shazam_checkpoint(pcm_snapshot: np.ndarray, sec_mark: int):
            with active_query_lock:
                if cancel_event.is_set() or match_found.is_set():
                    return

                wav_bytes, rms_dbfs = self.rate_limiter.prepare_optimal_wav(pcm_snapshot, samplerate)
                if wav_bytes is None:
                    self.logger.warning(f"Skipped {sec_mark}s sample: low input level ({rms_dbfs:.1f} dBFS).")
                    update_status_if_active(f"Low input level ({rms_dbfs:.0f} dBFS) at {sec_mark}s. Waiting for audio...")
                    return

                try:
                    win_len = min(sec_mark, self.rate_limiter.OPTIMAL_WINDOW_SECONDS)
                    self.logger.info(f"Querying Shazam with {win_len}s sample at {sec_mark}s mark ({rms_dbfs:.1f} dBFS)")
                    update_status_if_active(f"Identifying ({sec_mark}s sample)...")

                    res = self.rate_limiter.recognize_safe(
                        wav_bytes, cooldown_sec, cancel_event, status_cb=update_status_if_active
                    )
                    if cancel_event.is_set():
                        return

                    if res and "track" in res:
                        match_found.set()
                        track_title = res["track"].get("title", "")
                        track_artist = res["track"].get("subtitle", "")
                        self.logger.info(f"Matched at {sec_mark}s: '{track_title}' by '{track_artist}'")
                        update_status_if_active("Match found. Resolving platform links...")

                        info = self._extract_and_verify_metadata(res, cancel_event)
                        if not cancel_event.is_set() and self.session_id == session_id:
                            cancel_event.set()
                            self.root.after(0, lambda: self._display_result(info, session_id))
                    else:
                        self.logger.info(f"No match at {sec_mark}s checkpoint.")
                        if not match_found.is_set() and not cancel_event.is_set():
                            update_status_if_active(f"Listening ({sec_mark}s checked)...")
                except Exception as e:
                    if not cancel_event.is_set():
                        self.logger.error(f"Recognition error at {sec_mark}s: {e}")

        try:
            blocksize = max(64, int(round(samplerate * self.AUDIO_BLOCK_SECONDS)))
            with sd.InputStream(
                samplerate=samplerate,
                device=device_id,
                channels=1,
                dtype="int16",
                blocksize=blocksize,
                callback=audio_callback,
            ):
                start_t = time.time()
                while not cancel_event.is_set() and not match_found.is_set():
                    elapsed = time.time() - start_t
                    if not active_query_lock.locked():
                        update_status_if_active(f"Recording ({elapsed:.1f}s / {max_duration}s)")

                    if next_cp_idx < len(checkpoints) and elapsed >= checkpoints[next_cp_idx]:
                        sec_mark = checkpoints[next_cp_idx]
                        next_cp_idx += 1

                        with chunk_lock:
                            pcm_snapshot = (
                                np.concatenate(recorded_chunks, axis=0)
                                if recorded_chunks
                                else np.zeros(samplerate, dtype=np.int16)
                            )

                        if mode == "stream":
                            if not active_query_lock.locked():
                                threading.Thread(
                                    target=query_shazam_checkpoint, args=(pcm_snapshot, sec_mark), daemon=True
                                ).start()
                        else:
                            break

                    if elapsed >= max_duration and next_cp_idx >= len(checkpoints):
                        break
                    if cancel_event.wait(timeout=0.04):
                        break

            if mode == "fixed" and not cancel_event.is_set() and not match_found.is_set():
                with chunk_lock:
                    pcm_snapshot = (
                        np.concatenate(recorded_chunks, axis=0)
                        if recorded_chunks
                        else np.zeros(samplerate, dtype=np.int16)
                    )
                query_shazam_checkpoint(pcm_snapshot, fixed_dur)

            while active_query_lock.locked() and not cancel_event.is_set():
                time.sleep(0.05)

            if not cancel_event.is_set() and not match_found.is_set() and self.session_id == session_id:
                self.logger.info("Session finished with no match.")
                update_status_if_active("No match found")

        except Exception as e:
            if not cancel_event.is_set():
                self.logger.error(f"Audio capture failure: {e}")
                self.root.after(0, lambda: messagebox.showerror("Capture Error", f"Audio capture error:\n{e}"))
                update_status_if_active("Capture error")
        finally:
            if self.session_id == session_id:
                self.is_listening = False
                self.root.after(0, lambda: self.listen_btn.config(text="Start Listening"))

    def _extract_and_verify_metadata(self, data: dict, cancel_event: threading.Event) -> dict:
        track = data.get("track", {})
        title = track.get("title", "").strip()
        artist = track.get("subtitle", "").strip()
        album = ""
        year = ""

        for section in track.get("sections", []):
            if section.get("type") == "SONG":
                for meta in section.get("metadata", []):
                    m_title = meta.get("title", "").lower()
                    if m_title == "album":
                        album = meta.get("text", "").strip()
                    elif m_title == "released":
                        year = meta.get("text", "").strip()

        images = track.get("images", {})
        cover_art_url = images.get("coverarthq") or images.get("coverart") or ""

        resolved = resolve_verified_platforms(title, artist, track, cancel_event, self.logger)

        return {
            "title": title,
            "artist": artist,
            "album": album,
            "year": year,
            "cover_art_url": cover_art_url,
            "shazam_url": resolved["shazam_url"],
            "platforms": resolved["platforms"],
            "preferred_download_url": resolved["preferred_download_url"],
            "ref_duration_sec": resolved["ref_duration_sec"],
        }

    # ---------------- CLEAN RESULT DISPLAY & PLACEHOLDER ART ---------------- #
    def _hide_results(self):
        self.last_recognized_info = {}
        self.album_art_raw_pil = None
        self.album_art_image = None
        self.art_label.config(image="", text="")
        self.result_card.pack_forget()

    def _display_result(self, info: dict, session_id: int):
        if self.session_id != session_id:
            return

        self.last_recognized_info = info
        self.album_art_raw_pil = None
        self.album_art_image = None

        title = info.get("title", "")
        artist = info.get("artist", "")
        album = info.get("album", "")
        year = info.get("year", "")

        status_summary = f"Matched: {title} - {artist}" if (title and artist) else "Match found"
        self._set_status(status_summary)

        for lbl in (self.title_lbl, self.artist_lbl, self.album_lbl):
            lbl.pack_forget()

        if title:
            self.title_lbl.config(text=title)
            self.title_lbl.pack(anchor="w", pady=(0, 2))

        if artist:
            self.artist_lbl.config(text=artist)
            self.artist_lbl.pack(anchor="w", pady=(0, 2))

        album_parts = [p for p in (album, year) if p]
        if album_parts:
            self.album_lbl.config(text=" • ".join(album_parts))
            self.album_lbl.pack(anchor="w", pady=(0, 4))

        self.placeholder_art_image = self._create_fluent_album_placeholder()
        self.art_label.config(image=self.placeholder_art_image, text="")
        if info.get("cover_art_url"):
            threading.Thread(
                target=self._load_album_art, args=(info["cover_art_url"], session_id), daemon=True
            ).start()

        if info.get("shazam_url"):
            self.shazam_btn.pack(side="left")
        else:
            self.shazam_btn.pack_forget()

        for widget in self.links_frame.winfo_children():
            widget.destroy()

        platforms = info.get("platforms", {})
        if platforms:
            self.platforms_sep.pack(fill="x", pady=(14, 10))
            self.platforms_header.pack(anchor="w", pady=(0, 6))
            self.links_frame.pack(fill="x")

            for idx, (platform_name, url) in enumerate(platforms.items()):
                row, col = divmod(idx, 4)
                ttk.Button(
                    self.links_frame,
                    text=platform_name,
                    command=lambda p=platform_name, u=url: self._open_platform_url(p, u),
                ).grid(row=row, column=col, padx=(0, 8), pady=4, sticky="ew")

            for col in range(4):
                self.links_frame.columnconfigure(col, weight=1)
        else:
            self.platforms_sep.pack_forget()
            self.platforms_header.pack_forget()
            self.links_frame.pack_forget()

        self.result_card.pack(fill="x", pady=(0, 8))
        self._populate_downloader_from_match()

    def _load_album_art(self, url: str, session_id: int):
        try:
            resp = requests.get(url, timeout=6)
            resp.raise_for_status()
            img_data = Image.open(io.BytesIO(resp.content)).convert("RGB")
            if self.session_id == session_id:
                self.root.after(0, lambda: self._set_art(img_data))
        except Exception as e:
            self.logger.warning(f"Album art download failed (keeping placeholder): {e}")

    def _set_art(self, raw_pil_img: Image.Image):
        self.album_art_raw_pil = raw_pil_img
        self.album_art_image = self._render_rounded_album_art(raw_pil_img)
        self.art_label.config(image=self.album_art_image)

    def _open_shazam_url(self):
        url = self.last_recognized_info.get("shazam_url")
        if url:
            self._open_platform_url("Shazam", url)

    def _open_platform_url(self, platform_name: str, url: str):
        self.logger.info(f"Opening {platform_name}: {url}")
        webbrowser.open(url)

    def switch_to_downloader(self):
        self.notebook.select(self.tab_downloader)

    def _populate_downloader_from_match(self):
        self._apply_shazam_meta()
        self._load_shazam_thumb()
        matched_url = self.last_recognized_info.get("preferred_download_url", "")
        if matched_url and self._is_valid_url(matched_url):
            self.url_var.set(matched_url)
            self.logger.info(f"Auto-fetching streams for matched URL: {matched_url}")
            self.fetch_ytdlp_streams()

    # ---------------- INTEGRATED DOWNLOADER (GRAB) TAB METHODS ---------------- #
    @staticmethod
    def _is_valid_url(text: str) -> bool:
        try:
            parsed = urllib.parse.urlparse(text.strip())
            return parsed.scheme in ("http", "https") and bool(parsed.netloc)
        except Exception:
            return False

    def _apply_shazam_meta(self):
        self.meta_vars["meta_title"].set(self.last_recognized_info.get("title", ""))
        self.meta_vars["meta_artist"].set(self.last_recognized_info.get("artist", ""))
        self.meta_vars["meta_album"].set(self.last_recognized_info.get("album", ""))
        self.meta_vars["meta_year"].set(self.last_recognized_info.get("year", ""))

    def _apply_ytdlp_meta(self):
        if not self.ytdlp_info:
            messagebox.showinfo("Metadata", "Fetch streams from a URL first to load stream metadata.")
            return
        self.meta_vars["meta_title"].set(self.ytdlp_info.get("track") or self.ytdlp_info.get("title", ""))
        self.meta_vars["meta_artist"].set(self.ytdlp_info.get("artist") or self.ytdlp_info.get("uploader", ""))
        self.meta_vars["meta_album"].set(self.ytdlp_info.get("album", ""))
        upload_date = self.ytdlp_info.get("release_year") or self.ytdlp_info.get("upload_date", "")
        self.meta_vars["meta_year"].set(str(upload_date)[:4] if upload_date else "")

    def _choose_dir(self):
        folder = filedialog.askdirectory(initialdir=self.out_dir_var.get())
        if folder:
            self.out_dir_var.set(folder)
            self.save_preferences()

    def _load_shazam_thumb(self):
        url = self.last_recognized_info.get("cover_art_url")
        if url:
            threading.Thread(target=self._download_image_to_canvas, args=(url,), daemon=True).start()

    def _load_ytdlp_thumb(self):
        thumb_url = self.ytdlp_info.get("thumbnail")
        if not thumb_url:
            messagebox.showinfo("Cover Art", "Fetch streams from a URL first to load source artwork.")
            return
        threading.Thread(target=self._download_image_to_canvas, args=(thumb_url,), daemon=True).start()

    def _load_local_thumb(self):
        path = filedialog.askopenfilename(filetypes=[("Image Files", "*.jpg *.jpeg *.png *.webp")])
        if path:
            img = Image.open(path).convert("RGB")
            self._set_working_image(img)

    def _download_image_to_canvas(self, url: str):
        try:
            resp = requests.get(url, timeout=10)
            resp.raise_for_status()
            img = Image.open(io.BytesIO(resp.content)).convert("RGB")
            self.root.after(0, lambda: self._set_working_image(img))
        except Exception as e:
            self.logger.warning(f"Cover art fetch failed ({url}): {e}")

    def _set_working_image(self, pil_img: Image.Image):
        self.raw_pil_image = pil_img.copy()
        self.cropped_pil_image = pil_img.copy()
        self.crop_coords_pil = None
        self._render_canvas_image(self.cropped_pil_image)

    def _render_canvas_image(self, pil_img: Image.Image):
        self.canvas.delete("all")
        w, h = pil_img.size
        self.scale_factor = min(self.canvas_size / w, self.canvas_size / h)
        new_w, new_h = int(w * self.scale_factor), int(h * self.scale_factor)
        self.offset_x = (self.canvas_size - new_w) // 2
        self.offset_y = (self.canvas_size - new_h) // 2

        resized = pil_img.resize((new_w, new_h), Image.Resampling.LANCZOS)
        self.display_tk_image = ImageTk.PhotoImage(resized)
        self.canvas.create_image(self.offset_x, self.offset_y, anchor="nw", image=self.display_tk_image)
        self.crop_info_var.set(f"Resolution: {w} x {h} px")

    def _on_crop_start(self, event):
        if not self.cropped_pil_image:
            return
        self.drag_start = (event.x, event.y)
        if self.crop_rect_id:
            self.canvas.delete(self.crop_rect_id)
        self.crop_rect_id = self.canvas.create_rectangle(
            event.x, event.y, event.x, event.y, outline="#60cdff", width=2, dash=(4, 2)
        )

    def _on_crop_drag(self, event):
        if not self.drag_start or not self.crop_rect_id:
            return
        x0, y0 = self.drag_start
        self.canvas.coords(self.crop_rect_id, x0, y0, event.x, event.y)

    def _on_crop_end(self, event):
        if not self.drag_start or not self.cropped_pil_image:
            return
        x0, y0 = self.drag_start
        x1, y1 = event.x, event.y
        self.drag_start = None

        left_c, right_c = sorted([x0, x1])
        top_c, bottom_c = sorted([y0, y1])

        img_w, img_h = self.cropped_pil_image.size
        left_p = max(0, int((left_c - self.offset_x) / self.scale_factor))
        top_p = max(0, int((top_c - self.offset_y) / self.scale_factor))
        right_p = min(img_w, int((right_c - self.offset_x) / self.scale_factor))
        bottom_p = min(img_h, int((bottom_c - self.offset_y) / self.scale_factor))

        if right_p - left_p > 20 and bottom_p - top_p > 20:
            self.crop_coords_pil = (left_p, top_p, right_p, bottom_p)
            self.crop_info_var.set(f"Selection: {right_p - left_p} x {bottom_p - top_p} px")

    def apply_drawn_crop(self):
        if not self.cropped_pil_image or not self.crop_coords_pil:
            return
        self.cropped_pil_image = self.cropped_pil_image.crop(self.crop_coords_pil)
        self.crop_coords_pil = None
        self.logger.info(f"Applied custom crop: {self.cropped_pil_image.size}")
        self._render_canvas_image(self.cropped_pil_image)

    def crop_square_center(self):
        if not self.cropped_pil_image:
            return
        w, h = self.cropped_pil_image.size
        min_dim = min(w, h)
        left = (w - min_dim) // 2
        top = (h - min_dim) // 2
        self.cropped_pil_image = self.cropped_pil_image.crop((left, top, left + min_dim, top + min_dim))
        self.crop_coords_pil = None
        self.logger.info(f"Applied square center crop: {self.cropped_pil_image.size}")
        self._render_canvas_image(self.cropped_pil_image)

    def reset_crop(self):
        if self.raw_pil_image:
            self.cropped_pil_image = self.raw_pil_image.copy()
            self.crop_coords_pil = None
            self.logger.info("Reset cover art crop.")
            self._render_canvas_image(self.cropped_pil_image)

    def fetch_ytdlp_streams(self):
        url = self.url_var.get().strip()
        if not self._is_valid_url(url):
            messagebox.showwarning("Invalid URL", "Enter a valid URL starting with http:// or https://")
            return

        self.fetch_btn.config(state="disabled")
        self._set_grab_status("Fetching available audio streams...")
        self.logger.info(f"Fetching streams for URL: {url}")
        threading.Thread(target=self._fetch_streams_worker, args=(url,), daemon=True).start()

    def _fetch_streams_worker(self, url: str):
        try:
            ydl_opts = {
                "quiet": True,
                "no_warnings": True,
                "noplaylist": True,
                "logger": YtdlpLoggerAdapter(self.logger),
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                if "entries" in info:
                    info = info["entries"][0]

            self.ytdlp_info = info
            formats = info.get("formats", [])

            audio_streams = []
            for f in formats:
                if f.get("acodec") != "none":
                    fmt_id = f.get("format_id")
                    ext = f.get("ext", "")
                    abr = f.get("abr") or f.get("tbr") or 0
                    acodec = f.get("acodec", "")
                    vcodec = "Audio" if f.get("vcodec") == "none" else "Audio+Video"
                    filesize = f.get("filesize") or f.get("filesize_approx")
                    size_mb = f"{filesize / (1024 * 1024):.1f} MB" if filesize else "Size N/A"
                    label = f"ID {fmt_id} | {ext.upper()} ({acodec}) | {int(abr)} kbps | {vcodec} | {size_mb}"
                    audio_streams.append((abr, fmt_id, label))

            audio_streams.sort(key=lambda x: x[0], reverse=True)
            self.available_formats = [("bestaudio/best", "Best Available Audio (Automatic)")] + [
                (fmt_id, label) for _, fmt_id, label in audio_streams
            ]
            self.logger.info(f"Loaded {len(audio_streams)} streams for '{info.get('title', url)}'")

            def _update_ui():
                self.quality_combo["values"] = [label for _, label in self.available_formats]
                self.quality_combo.current(0)
                self._set_grab_status(f"Found {len(audio_streams)} streams")
                if not self.meta_vars["meta_title"].get().strip():
                    self._apply_ytdlp_meta()
                if not self.raw_pil_image and info.get("thumbnail"):
                    self._load_ytdlp_thumb()

            self.root.after(0, _update_ui)
        except Exception as e:
            self.logger.error(f"Stream fetch error: {e}")
            self.root.after(0, lambda: messagebox.showerror("Stream Error", f"Failed to fetch streams:\n{e}"))
            self._set_grab_status("Failed to fetch streams.")
        finally:
            self.root.after(0, lambda: self.fetch_btn.config(state="normal"))

    def start_download_pipeline(self):
        url = self.url_var.get().strip()
        if not self._is_valid_url(url):
            messagebox.showwarning("Invalid URL", "Enter a valid http:// or https:// URL.")
            return

        self.save_preferences()
        self.download_btn.config(state="disabled")
        self.progress["value"] = 0
        self.logger.info(f"Starting download pipeline for: {url}")
        threading.Thread(target=self._download_convert_worker, args=(url,), daemon=True).start()

    def _run_ffmpeg_silently(self, cmd: list):
        self.logger.info(f"Executing FFmpeg: {' '.join(cmd)}")
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=SUBPROCESS_FLAGS,
        )
        for line in proc.stdout:
            cleaned = line.strip()
            if cleaned:
                self.logger.debug(f"[ffmpeg] {cleaned}")
        ret_code = proc.wait()
        if ret_code != 0:
            raise RuntimeError(f"FFmpeg exited with code {ret_code}")

    def _download_convert_worker(self, url: str):
        temp_dir = tempfile.mkdtemp(prefix="echograb_ytdlp_")
        try:
            sel_idx = self.quality_combo.current()
            fmt_spec = self.available_formats[sel_idx][0] if self.available_formats and sel_idx >= 0 else "bestaudio/best"

            def progress_hook(d):
                if d["status"] == "downloading":
                    total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                    downloaded = d.get("downloaded_bytes", 0)
                    if total > 0:
                        pct = (downloaded / total) * 100
                        self.root.after(0, lambda p=pct: self.progress.configure(value=p))
                        self._set_grab_status(f"Downloading... {pct:.1f}%")

            ydl_opts = {
                "format": fmt_spec,
                "outtmpl": os.path.join(temp_dir, "raw_download.%(ext)s"),
                "noplaylist": True,
                "progress_hooks": [progress_hook],
                "quiet": True,
                "logger": YtdlpLoggerAdapter(self.logger),
            }

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                if "entries" in info:
                    info = info["entries"][0]
                raw_file = ydl.prepare_filename(info)

            target_ext = self.target_ext_var.get().lower()
            bitrate_choice = self.bitrate_var.get()
            bitrate = "192k" if "Lossless" in bitrate_choice else bitrate_choice

            title_clean = self.meta_vars["meta_title"].get().strip() or info.get("title", "track")
            artist_clean = self.meta_vars["meta_artist"].get().strip()
            base_name = f"{artist_clean} - {title_clean}" if artist_clean else title_clean
            safe_name = "".join(c for c in base_name if c not in r'\/:*?"<>|').strip() or "output_audio"

            out_dir = self.out_dir_var.get()
            os.makedirs(out_dir, exist_ok=True)
            final_path = os.path.join(out_dir, f"{safe_name}.{target_ext}")

            self._set_grab_status(f"Converting to {target_ext.upper()}...")

            ffmpeg_cmd = ["ffmpeg", "-hide_banner", "-y", "-i", raw_file, "-vn"]
            if target_ext == "mp3":
                ffmpeg_cmd += ["-c:a", "libmp3lame", "-b:a", bitrate, "-id3v2_version", "3"]
            elif target_ext == "m4a":
                ffmpeg_cmd += ["-c:a", "aac", "-b:a", bitrate]
            elif target_ext == "opus":
                ffmpeg_cmd += ["-c:a", "libopus", "-b:a", bitrate]
            elif target_ext == "flac":
                ffmpeg_cmd += ["-c:a", "flac"]
            elif target_ext == "wav":
                ffmpeg_cmd += ["-c:a", "pcm_s16le"]

            if self.embed_meta_var.get():
                ffmpeg_cmd += [
                    "-metadata", f"title={self.meta_vars['meta_title'].get()}",
                    "-metadata", f"artist={self.meta_vars['meta_artist'].get()}",
                    "-metadata", f"album={self.meta_vars['meta_album'].get()}",
                    "-metadata", f"date={self.meta_vars['meta_year'].get()}",
                ]

            ffmpeg_cmd.append(final_path)
            self._run_ffmpeg_silently(ffmpeg_cmd)

            self._set_grab_status("Embedding metadata and cover art...")
            self._embed_tags_and_art(final_path, target_ext)
            self.logger.info(f"Saved audio file: {final_path}")

            self.root.after(0, lambda: self.progress.configure(value=100))
            self._set_grab_status(f"Saved: {final_path}")
            self.root.after(0, lambda: messagebox.showinfo("Download Complete", f"Saved to:\n{final_path}"))

        except Exception as e:
            self.logger.error(f"Download/conversion error: {e}")
            self.root.after(0, lambda: messagebox.showerror("Error", f"Processing failed:\n{e}"))
            self._set_grab_status("Conversion failed.")
        finally:
            self.root.after(0, lambda: self.download_btn.config(state="normal"))

    def _embed_tags_and_art(self, filepath: str, ext: str):
        title = self.meta_vars["meta_title"].get().strip()
        artist = self.meta_vars["meta_artist"].get().strip()
        album = self.meta_vars["meta_album"].get().strip()
        year = self.meta_vars["meta_year"].get().strip()

        img_bytes = None
        if self.embed_thumb_var.get() and self.cropped_pil_image:
            buf = io.BytesIO()
            self.cropped_pil_image.save(buf, format="JPEG", quality=92)
            img_bytes = buf.getvalue()

        if ext == "mp3":
            try:
                tags = ID3(filepath)
            except ID3NoHeaderError:
                tags = ID3()
            if self.embed_meta_var.get():
                if title:
                    tags["TIT2"] = TIT2(encoding=3, text=title)
                if artist:
                    tags["TPE1"] = TPE1(encoding=3, text=artist)
                if album:
                    tags["TALB"] = TALB(encoding=3, text=album)
                if year:
                    tags["TDRC"] = TDRC(encoding=3, text=year)
            if img_bytes:
                tags.delall("APIC")
                tags["APIC"] = APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=img_bytes)
            tags.save(filepath, v2_version=3)

        elif ext == "m4a":
            audio = MP4(filepath)
            if self.embed_meta_var.get():
                if title:
                    audio["\xa9nam"] = [title]
                if artist:
                    audio["\xa9ART"] = [artist]
                if album:
                    audio["\xa9alb"] = [album]
                if year:
                    audio["\xa9day"] = [year]
            if img_bytes:
                audio["covr"] = [MP4Cover(img_bytes, imageformat=MP4Cover.FORMAT_JPEG)]
            audio.save()

        elif ext == "flac":
            audio = FLAC(filepath)
            if self.embed_meta_var.get():
                if title:
                    audio["title"] = title
                if artist:
                    audio["artist"] = artist
                if album:
                    audio["album"] = album
                if year:
                    audio["date"] = year
            if img_bytes:
                pic = Picture()
                pic.type = 3
                pic.mime = "image/jpeg"
                pic.desc = "Cover"
                pic.width, pic.height = self.cropped_pil_image.size
                pic.depth = 24
                pic.data = img_bytes
                audio.clear_pictures()
                audio.add_picture(pic)
            audio.save()

        elif ext == "opus":
            audio = OggOpus(filepath)
            if self.embed_meta_var.get():
                if title:
                    audio["title"] = [title]
                if artist:
                    audio["artist"] = [artist]
                if album:
                    audio["album"] = [album]
                if year:
                    audio["date"] = [year]
            if img_bytes:
                pic = Picture()
                pic.type = 3
                pic.mime = "image/jpeg"
                pic.desc = "Cover"
                pic.width, pic.height = self.cropped_pil_image.size
                pic.depth = 24
                pic.data = img_bytes
                encoded_pic = base64.b64encode(pic.write()).decode("ascii")
                audio["metadata_block_picture"] = [encoded_pic]
            audio.save()


if __name__ == "__main__":
    root = tk.Tk()
    app = EchoGrabApp(root)
    root.mainloop()