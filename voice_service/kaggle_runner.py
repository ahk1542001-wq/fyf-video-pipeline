"""
Kaggle Runner — drives the optional VoxCPM2 Burmese partner route on Kaggle's
GPU tier via the official Kaggle API. No browser needed.
"""
import os
import json
import time
import base64
import inspect
import re
import shutil
import unicodedata
import hashlib
from string import Template
from typing import Optional

from voice_service.production_voice import apply_production_postprocessing

VOICE_REFERENCE = os.getenv(
    "VOICE_REFERENCE",
    "",
)
ACCELERATOR = os.getenv("KAGGLE_ACCELERATOR", "NvidiaTeslaT4")
KAGGLE_CONFIG = os.path.expanduser("~/.kaggle/kaggle.json")
KAGGLE_OAUTH = os.path.expanduser("~/.kaggle/credentials.json")
KERNEL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_kaggle_kernel")

_TERMINAL = {"complete", "failed", "error", "cancelled"}


def expand_ai_acronym(text: str) -> str:
    """Expand standalone or Burmese-adjacent 'AI' case-insensitively to 'အေ အိုင်'."""
    pattern = re.compile(r"(?i)(?<![a-zA-Z0-9])ai(?![a-zA-Z0-9])")
    return pattern.sub("အေ အိုင်", text)


def normalize_burmese_text(text: str) -> str:
    """Normalize text with unicodedata NFC, clean zero-width/whitespace and safe punctuation spacing."""
    if not text:
        return ""
    text = unicodedata.normalize('NFC', text)
    text = expand_ai_acronym(text)

    text = re.sub(r'[\u200b\ufeff]', '', text)
    text = text.replace("\u00a0", " ")

    text = re.sub(r'\s+([။၊!?;:=])', r'\1', text)
    text = re.sub(r'([။၊!?;:=])(?=[^\s])', r'\1 ', text)

    return " ".join(text.split())


def _is_combiner_or_virama(char: str) -> bool:
    if not char: return False
    return unicodedata.category(char) in ('Mn', 'Mc') or ord(char) in (0x1039, 0x103A)


CLAUSE_PAUSE_MS = 320
SENTENCE_PAUSE_MS = 700
INTERNAL_SPLIT_PAUSE_MS = 280


def split_long_chunk(
    chunk: dict,
    max_chars: int = 180,
    internal_pause_ms: int = INTERNAL_SPLIT_PAUSE_MS,
) -> list[dict]:
    """Split a long chunk into smaller chunks capped around 180 Unicode characters."""
    text = chunk.get("text", "")
    if not isinstance(text, str) or not text:
        return [chunk]

    if len(text) <= max_chars:
        return [chunk]

    text_to_split = text
    terminal_punctuations = ""
    while text_to_split and text_to_split[-1] in "။၊!?;:=":
        terminal_punctuations = text_to_split[-1] + terminal_punctuations
        text_to_split = text_to_split[:-1]

    tokens = text_to_split.split(" ")
    parts = []
    current_tokens = []
    current_len = 0

    for token in tokens:
        if not token: continue
        token_len = len(token)

        if current_tokens and current_len + token_len + (1 if current_len > 0 else 0) > max_chars:
            parts.append(" ".join(current_tokens))
            current_tokens = [token]
            current_len = token_len
        else:
            current_tokens.append(token)
            current_len += token_len + (1 if current_len > 0 else 0)

    if current_tokens:
        parts.append(" ".join(current_tokens))

    if not parts:
        return [chunk]

    result = []
    for i, part in enumerate(parts):
        if i < len(parts) - 1:
            result.append({"text": part, "pause_ms": internal_pause_ms})
        else:
            result.append({"text": part + terminal_punctuations, "pause_ms": chunk.get("pause_ms", 0)})
    return result


def parse_and_chunk_script(text: str) -> list[dict]:
    """Create natural speech units and preserve audible punctuation pauses."""
    text = normalize_burmese_text(text)
    if not text: return []

    pause_by_mark = {
        "။": SENTENCE_PAUSE_MS,
        ".": SENTENCE_PAUSE_MS,
        "!": SENTENCE_PAUSE_MS,
        "?": SENTENCE_PAUSE_MS,
        "၊": CLAUSE_PAUSE_MS,
        ",": CLAUSE_PAUSE_MS,
        ";": CLAUSE_PAUSE_MS,
        ":": CLAUSE_PAUSE_MS,
        "=": CLAUSE_PAUSE_MS,
    }

    # Split by punctuation to evaluate natural boundaries
    pattern = r"([။\.!\?၊,;:=]+)"
    parts = re.split(pattern, text)

    segments = []
    for i in range(0, len(parts), 2):
        chunk_text = parts[i]
        punctuation = parts[i+1] if i+1 < len(parts) else ""
        combined = chunk_text + punctuation
        if combined.strip():
            segments.append(combined)

    chunks = []
    for segment in segments:
        unit = segment.strip()
        if not unit:
            continue
        last_char = unit[-1]
        pause_ms = pause_by_mark.get(last_char, INTERNAL_SPLIT_PAUSE_MS)
        chunks.append({"text": unit, "pause_ms": pause_ms})

    expanded_chunks = []
    for chunk in chunks:
        expanded_chunks.extend(
            split_long_chunk(
                chunk,
                max_chars=180,
                internal_pause_ms=INTERNAL_SPLIT_PAUSE_MS,
            )
        )
    return expanded_chunks


def prepare_burmese_pronunciation_preview(original: str, lexicon: Optional[dict[str, str]] = None) -> dict:
    """Pure longest-source-first pronunciation replacement."""
    if not original:
        return {
            "original_script": original,
            "normalized_script": "",
            "changes": [],
            "chunks": [],
            "coverage_ok": False
        }

    lexicon = lexicon or {}
    text = normalize_burmese_text(original)

    changes = []
    if lexicon:
        sorted_lexicon = sorted(lexicon.items(), key=lambda x: len(x[0]), reverse=True)
        for src, dst in sorted_lexicon:
            idx = 0
            while True:
                idx = text.find(src, idx)
                if idx == -1:
                    break

                if _is_combiner_or_virama(src[0]):
                    idx += 1
                    continue
                if idx > 0 and text[idx-1] == '\u1039':
                    idx += 1
                    continue

                next_idx = idx + len(src)
                if next_idx < len(text) and _is_combiner_or_virama(text[next_idx]):
                    idx += 1
                    continue

                text = text[:idx] + dst + text[next_idx:]
                changes.append({"source": src, "destination": dst})
                idx += len(dst)

    normalized = normalize_burmese_text(text)
    chunks = parse_and_chunk_script(normalized)

    recon_norm = normalize_burmese_text(" ".join([c["text"] for c in chunks]))
    coverage_ok = (recon_norm == normalized) and len(normalized) > 0

    return {
        "original_script": original,
        "normalized_script": normalized,
        "changes": changes,
        "chunks": chunks,
        "coverage_ok": coverage_ok
    }


def base_stable_voice_seed(reference_bytes: bytes, cfg_value: float, steps: int, denoise: bool) -> int:
    """Compute stable voice base seed independent of normalize A/B or variant name."""
    settings_str = f"cfg={cfg_value}_steps={steps}_denoise={denoise}"
    h = hashlib.sha256(reference_bytes + settings_str.encode()).hexdigest()
    return int(h[:8], 16) & 0x7FFFFFFF


def stable_voice_seed(reference_bytes: bytes, variant: str) -> int:
    h = hashlib.sha256(reference_bytes + variant.encode()).hexdigest()
    return int(h[:8], 16) & 0x7FFFFFFF


def alternate_take_seed(seed: int, index: int) -> int:
    return (seed + index * 104729) & 0x7FFFFFFF


def median(values: list[float]) -> float:
    if not values: return 0.0
    s = sorted(values)
    n = len(s)
    return float(s[n//2] if n % 2 != 0 else (s[n//2 - 1] + s[n//2]) / 2.0)


def is_consistency_outlier(cps: float, duration: float, accepted_cps: list[float], ratio: float = 1.35, long_seconds: float = 6.0, min_cps: float = 4.5) -> bool:
    if duration >= long_seconds and cps < min_cps:
        return True
    if not accepted_cps:
        return False
    med = median(accepted_cps)
    if med == 0:
        return False
    return (cps / med > ratio) or (med / cps > ratio)


def trim_silence_edges(audio, sample_rate, threshold_ratio=0.015, guard_ms=25):
    """Pure numpy conservative silence trim. No clipping into voiced audio."""
    import numpy as np
    if len(audio) == 0:
        return audio
    if len(audio.shape) > 1:
        mono = np.mean(audio, axis=1)
    else:
        mono = audio
    max_amp = np.max(np.abs(mono))
    if max_amp == 0:
        return audio
    threshold = threshold_ratio * max_amp
    guard_samples = int(sample_rate * (guard_ms / 1000.0))
    above_thresh = np.abs(mono) > threshold
    if not np.any(above_thresh):
        return audio
    first_idx = np.argmax(above_thresh)
    last_idx = len(mono) - 1 - np.argmax(above_thresh[::-1])
    start = max(0, first_idx - guard_samples)
    end = min(len(audio), last_idx + guard_samples + 1)
    return audio[start:end]


def normalize_kernel_ref(ref: str) -> str:
    ref = ref.strip()
    if ref.startswith("/code/"):
        ref = ref[len("/code/"):]
    elif ref.startswith("code/"):
        ref = ref[len("code/"):]
    return ref.strip("/")


def normalize_kernel_status(status) -> str:
    if status is None:
        return ""
    if hasattr(status, "name") and status.name is not None:
        status_str = str(status.name)
    elif hasattr(status, "value") and status.value is not None:
        status_str = str(status.value)
    else:
        status_str = str(status)

    status_str = status_str.strip().lower()
    if "." in status_str:
        status_str = status_str.split(".")[-1]
    return status_str


def select_voice_wav_files(names: list[str]) -> list[str]:
    return [name for name in names if name.startswith("voice_") and name.endswith(".wav")]


# === KERNEL main.py (runs inside the Kaggle T4 VM) ===
KERNEL_MAIN = r'''
import subprocess, sys, os, base64, io, wave, re, unicodedata, hashlib, random
import numpy as np
import torch

def run(cmd):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if r.returncode != 0:
        print(f"CMD FAILED: {cmd}\n{r.stderr}")
    return r.stdout

print("[Init] Installing dependencies...")
run("pip install -q voxcpm soundfile librosa numpy")

import librosa
import soundfile as sf
from voxcpm import VoxCPM

print("[Init] Loading VoxCPM2 model...")
model = VoxCPM.from_pretrained(
    "openbmb/VoxCPM2",
    load_denoiser=True,
    device="cuda" if torch.cuda.is_available() else "cpu",
)
sample_rate = model.tts_model.sample_rate

print("[Init] Decoding reference audio...")
ref_b64 = $REF_B64
ref_path = "/kaggle/working/reference.wav"
ref_bytes = base64.b64decode(ref_b64)
with open(ref_path, "wb") as f:
    f.write(ref_bytes)
audio, sr = librosa.load(ref_path, sr=16000, mono=True)
sf.write("/kaggle/working/ref_16k.wav", audio, 16000)

script_text = $SCRIPT_TEXT

# These exact helpers are injected from the host module
$PACING_HELPERS

chunks = parse_and_chunk_script(script_text)

variants = $VARIANT_SPECS
print(f"[Gen] {len(chunks)} chunks x {len(variants)} variants")

def set_seed(seed: int):
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)

def gen_full(variant_name, variant_params):
    all_audio = []

    def apply_equal_power_crossfade(audio_array, fade_duration_ms=8):
        fade_samples = int(sample_rate * (fade_duration_ms / 1000.0))
        if len(audio_array) < fade_samples * 2:
            return audio_array

        t = np.linspace(0, np.pi/2, fade_samples)
        fade_out = np.cos(t).astype(np.float32)
        fade_in = np.sin(t).astype(np.float32)

        audio_array[:fade_samples] *= fade_in
        audio_array[-fade_samples:] *= fade_out

        return audio_array

    base_seed = base_stable_voice_seed(ref_bytes, variant_params["cfg"], variant_params["steps"], variant_params["denoise"])
    accepted_cps_list = []

    for chunk in chunks:
        safe_text = normalize_burmese_text(chunk["text"])
        speech_unit_count = sum(
            not char.isspace() and char not in "။၊!?" for char in safe_text
        )

        print(f"  -> Generating: '{safe_text}' (Pause after: {chunk['pause_ms']}ms)")

        kwargs = {
            "text": safe_text,
            "reference_wav_path": "/kaggle/working/ref_16k.wav",
            "cfg_value": variant_params["cfg"],
            "inference_timesteps": variant_params["steps"],
            "normalize": variant_params.get("normalize", False),
            "denoise": variant_params["denoise"],
            "retry_badcase": True,
            "retry_badcase_max_times": 2,
        }

        max_retries = 3
        takes = []

        for attempt in range(max_retries):
            seed = alternate_take_seed(base_seed, attempt)
            set_seed(seed)
            wav = model.generate(**kwargs)
            wav = np.asarray(wav, dtype=np.float32)
            if wav.size == 0:
                print(f"     [Take {attempt}] Empty waveform.")
                continue

            duration_sec = len(wav) / sample_rate
            cps = speech_unit_count / duration_sec if duration_sec > 0 else 0

            takes.append({"wav": wav, "cps": cps, "duration": duration_sec, "take": attempt})

            outlier = is_consistency_outlier(cps, duration_sec, accepted_cps_list)
            if not outlier:
                print(f"     [Take {attempt}] OK! CPS: {cps:.2f} (Duration: {duration_sec:.1f}s)")
                break
            else:
                print(f"     [Take {attempt}] REJECTED OUTLIER! CPS: {cps:.2f}")

        if not takes:
            raise RuntimeError(f"VoxCPM2 returned no audio for chunk: {safe_text!r}")

        if not is_consistency_outlier(takes[-1]["cps"], takes[-1]["duration"], accepted_cps_list):
            best = takes[-1]
        else:
            if not accepted_cps_list:
                best = takes[0]
            else:
                med = median(accepted_cps_list)
                best = min(takes, key=lambda t: abs(t["cps"] - med))
                print(f"     [Fallback] Selected Take {best['take']} with CPS {best['cps']:.2f} closest to median {med:.2f}")

        accepted_cps_list.append(best["cps"])

        trimmed_wav = trim_silence_edges(best["wav"], sample_rate)
        processed_wav = apply_equal_power_crossfade(trimmed_wav, fade_duration_ms=8)
        all_audio.append(processed_wav)

        if chunk["pause_ms"] > 0:
            silence_samples = int(sample_rate * (chunk["pause_ms"] / 1000.0))
            all_audio.append(np.zeros(silence_samples, dtype=np.float32))

    return np.concatenate(all_audio)

def save_wav(data, path):
    buf = io.BytesIO()
    pcm16 = np.clip(data * 32767, -32768, 32767).astype(np.int16)
    w = wave.open(buf, "wb")
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(sample_rate)
    w.writeframes(pcm16.tobytes())
    w.close()
    with open(path, "wb") as f:
        f.write(buf.getvalue())

for name, variant in variants.items():
    try:
        final = gen_full(name, variant)
        save_wav(final, f"/kaggle/working/voice_{name}.wav")
        print(f"[Done] voice_{name}.wav saved ({len(final)/sample_rate:.1f}s)")
    except Exception as e:
        print(f"[Err] variant {name} failed: {e}")
        raise
'''


def _check_auth() -> None:
    if not (os.path.exists(KAGGLE_CONFIG) or os.path.exists(KAGGLE_OAUTH)):
        raise RuntimeError(
            "Kaggle auth missing. Setup (1 min):\n"
            "  1. Run: kaggle auth login  (OAuth, no token file needed)\n"
            "  or https://www.kaggle.com/settings/api → Create New API Token\n"
            "  2. Save the downloaded kaggle.json to ~/.kaggle/kaggle.json\n"
        )


def _username(api) -> str:
    return api.get_config_value(api.CONFIG_NAME_USER) or ""


def _build_kernel_folder(script_text: str, reference_path: str, username: str, run_id: str, variants: Optional[dict] = None) -> str:
    folder = os.path.join(KERNEL_DIR, run_id)
    os.makedirs(folder, exist_ok=True)

    with open(reference_path, "rb") as f:
        ref_b64 = base64.b64encode(f.read()).decode()

    pacing_constants = (
        f"CLAUSE_PAUSE_MS = {CLAUSE_PAUSE_MS}\n"
        f"SENTENCE_PAUSE_MS = {SENTENCE_PAUSE_MS}\n"
        f"INTERNAL_SPLIT_PAUSE_MS = {INTERNAL_SPLIT_PAUSE_MS}"
    )
    pacing_helpers = pacing_constants + "\n\n" + "\n\n".join(
        inspect.getsource(helper)
        for helper in (
            expand_ai_acronym,
            normalize_burmese_text,
            _is_combiner_or_virama,
            split_long_chunk,
            parse_and_chunk_script,
            stable_voice_seed,
            alternate_take_seed,
            base_stable_voice_seed,
            median,
            is_consistency_outlier,
            trim_silence_edges,
        )
    )
    var_str = repr(variants)
    main = Template(KERNEL_MAIN).substitute(
        REF_B64=json.dumps(ref_b64),
        SCRIPT_TEXT=json.dumps(script_text),
        PACING_HELPERS=pacing_helpers,
        VARIANT_SPECS=var_str,
    )
    with open(os.path.join(folder, "main.py"), "w") as f:
        f.write(main)

    metadata = {
        "id": f"{username}/voxcpm2-burmese-tts-{run_id}",
        "title": f"voxcpm2-burmese-tts-{run_id}",
        "code_file": "main.py",
        "language": "python",
        "kernel_type": "script",
        "is_private": True,
        "enable_gpu": True,
        "enable_internet": True,
        "machine_shape": ACCELERATOR,
    }
    with open(os.path.join(folder, "kernel-metadata.json"), "w") as f:
        json.dump(metadata, f, indent=2)
    return folder


PRODUCTION_VARIANT = {"cfg": 1.65, "steps": 20, "denoise": True}

DEFAULT_VARIANTS = {
    "cfg165_s20_dn":  {"cfg": 1.65, "steps": 20, "denoise": True},
    "cfg165_s15_dn":  {"cfg": 1.65, "steps": 15, "denoise": True},
    "cfg185_s20_dn":  {"cfg": 1.85, "steps": 20, "denoise": True},
}


def postprocess_saved_voice_files(
    paths: list[str],
    enabled: bool = True,
    *,
    script_text: str = "",
) -> list[str]:
    """Apply the production preset once per unique saved WAV unless opted out."""
    unique_paths = sorted(set(paths))
    if enabled:
        for path in unique_paths:
            apply_production_postprocessing(path, script_text=script_text)
    return unique_paths


def generate_voice_on_kaggle(
    script_text: str,
    reference_path: str = VOICE_REFERENCE,
    output_path: str = "output/voice.wav",
    variants: Optional[dict] = None,
    poll_interval: int = 30,
    timeout_minutes: int = 40,
    lexicon: Optional[dict[str, str]] = None,
    production_postprocess: bool = True,
) -> str:
    if not reference_path:
        raise ValueError("VOICE_REFERENCE must point to an approved local reference WAV")
    _check_auth()

    preview = prepare_burmese_pronunciation_preview(script_text, lexicon)
    if not preview["coverage_ok"]:
        raise ValueError("Coverage check failed for script text.")
    script_text = preview["normalized_script"]

    from kaggle.api.kaggle_api_extended import KaggleApi
    api = KaggleApi()
    api.authenticate()
    username = _username(api)
    if not username:
        raise RuntimeError("Could not read username from kaggle.json")

    tuning = variants is not None
    variants = variants or {"prod": PRODUCTION_VARIANT}
    run_id = str(int(time.time()))
    folder = _build_kernel_folder(script_text, reference_path, username, run_id, variants)

    try:
        print(f"[Kaggle] Pushing kernel (T4 GPU, {len(variants)} variants, ~15 min)...")
        resp = api.kernels_push(folder)
        kernel_ref = normalize_kernel_ref(str(getattr(resp, "ref", "")))
        if not kernel_ref or "/" not in kernel_ref:
            raise RuntimeError(f"kernels_push returned no ref: {resp}")
        print(f"[Kaggle] Kernel ref: {kernel_ref}")

        deadline = time.time() + timeout_minutes * 60
        last_status = ""
        while time.time() < deadline:
            try:
                k = api.kernels_status(kernel_ref)
                status_raw = getattr(k, "status", k)
                status = normalize_kernel_status(status_raw)
            except Exception as e:
                print(f"[Kaggle] status check error: {e}")
                status = ""
            if status != last_status:
                print(f"[Kaggle] status: {status}")
                last_status = status
            if status in _TERMINAL:
                break
            time.sleep(poll_interval)

        if status != "complete":
            try:
                api.kernels_logs(kernel_ref)
            except Exception:
                pass
            raise RuntimeError(f"Kernel finished with status: {status}")

        out_dir = os.path.join(folder, "out")
        api.kernels_output(kernel_ref, out_dir, quiet=False)
        wav_files = sorted(select_voice_wav_files(os.listdir(out_dir)))
        if not wav_files:
            raise RuntimeError("Kernel completed but no voice_*.wav in output")

        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
        out_root = os.path.dirname(output_path) or "."
        saved = []
        for f in wav_files:
            if tuning and f != "voice_prod.wav":
                variant_name = f.removeprefix("voice_").removesuffix(".wav")
                dest = os.path.join(out_root, f"{variant_name}.wav")
            else:
                dest = output_path
            shutil.copy(os.path.join(out_dir, f), dest)
            saved.append(dest)

        if tuning:
            print(f"[Kaggle] ✅ {len(saved)} tuning variants saved:")
        else:
            print(f"[Kaggle] ✅ Voice clone saved (production):")
        processed_paths = postprocess_saved_voice_files(saved, script_text=script_text, enabled=production_postprocess)
        for s in processed_paths:
            print(f"         {s}")
            if production_postprocess:
                print(f"         [Postprocess] Applied production preset to {s}")
        return output_path
    finally:
        shutil.rmtree(folder, ignore_errors=True)


if __name__ == "__main__":
    generate_voice_on_kaggle(
        "မင်္ဂလာပါ။ ဒါက စမ်းသပ်မှုပါ။ ကျွန်တော်တို့၊ အသံထွက်ကို စမ်းကြည့်တာပါ။ ပိုကောင်းလာမလား?",
        reference_path=VOICE_REFERENCE,
        output_path="/tmp/test_voice_kaggle.wav",
    )
