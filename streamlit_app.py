"""
streamlit_app.py — Piper TTS Professional UI
============================================
Clean two-mode interface:
  - Single Speaker: type text OR upload .txt file
  - Multi-Speaker:  type/upload script → auto-detects speakers
                    → assign voice per speaker → generate

Run:
    pip install streamlit requests
    streamlit run streamlit_app.py

FIX (merge duplication bug):
    merge_wavs() was using Python's wave module to read input WAV files.
    wave.Wave_read.readframes() reads the PCM data correctly, but the
    subsequent call to getnframes() (for total_dur) can return a stale
    header value that no longer matches the read position.  If anything
    inside the try block raises, the except handler appends the ENTIRE
    raw WAV bytes (header + PCM) to frames — meaning the same audio ends
    up in the output twice: once as correctly-read PCM frames, and once
    embedded inside the raw bytes.  Result: every line plays twice.

    Fix: parse the RIFF byte structure directly with _wav_extract_pcm().
    This reads ONLY the raw PCM data from the data chunk and ignores
    everything else.  Python's wave module is now used only for WRITING
    the output file, where we have complete control.
"""

import io
import re
import struct
import time
import wave
import requests
import streamlit as st

# ── Configuration ─────────────────────────────────────────────────────────────

_DEFAULT_API_BASE = "http://localhost:80"
POLL_INTERVAL = 2.0
MAX_POLLS     = 120
MAX_CHARS     = 10_000

VOICES = {
    "Lessac (US Female · High)":  "en_US_female_high",
    "Ryan (US Male · High)":      "en_US_male_high",
    "Amy (US Female · Medium)":   "en_US_female_medium",
    "Joe (US Male · Medium)":     "en_US_male_medium",
    "Alan (GB Male · Medium)":    "en_GB_male_medium",
    "Jenny (GB Female · Medium)": "en_GB_female_medium",
}
VOICE_LABELS = list(VOICES.keys())

SPEAKER_VOICE_CYCLE = [
    "Ryan (US Male · High)",
    "Lessac (US Female · High)",
    "Joe (US Male · Medium)",
    "Amy (US Female · Medium)",
    "Alan (GB Male · Medium)",
    "Jenny (GB Female · Medium)",
]

SAMPLE_SCRIPT = """\
Host: Welcome everyone to today's technology roundtable. In this discussion we will explore how artificial intelligence is shaping the future.
Engineer: Thank you for having me. One of the most exciting aspects of AI today is how quickly it can analyze massive datasets and transform them into useful insights.
Researcher: That is correct. Modern machine learning models learn patterns from large amounts of data, allowing them to perform tasks like speech recognition with impressive accuracy.
Host: Many people interact with these technologies every day without realizing it. Recommendation systems and voice assistants all rely on intelligent algorithms.
Designer: From a design perspective, the challenge is making these systems feel natural and helpful without overwhelming the user.
Host: Thank you all for joining this conversation. The future of technology will depend on how thoughtfully we develop artificial intelligence."""


# ── API Helpers ───────────────────────────────────────────────────────────────

def _api_base() -> str:
    return st.session_state.get("_api_base_url", _DEFAULT_API_BASE).rstrip("/")


def api_health():
    try:
        r = requests.get(f"{_api_base()}/health", timeout=4)
        return r.json()
    except Exception:
        return {"status": "offline"}


def queue_job(text, voice_key, speed, pitch):
    r = requests.post(f"{_api_base()}/generate-audio", json={
        "text": text, "voice": voice_key,
        "speed": float(speed), "pitch": float(pitch),
    }, timeout=30)
    if r.status_code == 503:
        raise Exception("Server busy (503). Try again in a moment.")
    if r.status_code == 429:
        raise Exception("Rate limit reached. Please wait 1 minute.")
    if not r.ok:
        raise Exception(f"Queue error {r.status_code}: {r.text[:150]}")
    return r.json()


def poll_until_done(job_id, on_progress=None):
    for poll in range(1, MAX_POLLS + 1):
        time.sleep(POLL_INTERVAL)
        p = requests.get(f"{_api_base()}/jobs/{job_id}", timeout=15).json()
        if on_progress:
            on_progress(poll, p)
        if p["status"] == "completed":
            return p
        if p["status"] == "failed":
            raise Exception(f"Synthesis failed: {p.get('error', 'unknown')}")
    raise Exception("Timeout — synthesis took too long. Try shorter text.")


def download_wav(job_id):
    dl = requests.get(f"{_api_base()}/download/{job_id}", timeout=120)
    if dl.status_code == 410:
        raise Exception("Audio expired — please generate again.")
    if not dl.ok:
        raise Exception(f"Download failed: HTTP {dl.status_code}")
    return dl.content


# ── WAV helpers ───────────────────────────────────────────────────────────────

def _wav_extract_pcm(wav_bytes: bytes):
    """
    Parse a WAV/RIFF file at the byte level and return its raw PCM data
    plus audio parameters.

    Returns (channels, sample_rate, sampwidth, pcm_bytes) or None if the
    bytes are not a valid WAV file.

    WHY direct RIFF parsing instead of Python's wave module:
      wave.Wave_read introduces subtle bugs when Piper-generated WAV files
      are involved.  Specifically, after readframes() consumes all frames,
      getnframes() can return a stale header value causing the old merge
      code's except handler to append the entire raw WAV bytes (header +
      PCM) to the frames list.  That produces the "every line plays twice"
      symptom.  Parsing the RIFF structure ourselves reads ONLY the data
      chunk's raw bytes — nothing more, nothing less.
    """
    if len(wav_bytes) < 44:
        return None

    # RIFF signature check
    if wav_bytes[:4] != b"RIFF" or wav_bytes[8:12] != b"WAVE":
        return None

    channels = sample_rate = sampwidth = None
    pcm_data = None
    pos = 12   # skip 12-byte RIFF/WAVE header

    while pos + 8 <= len(wav_bytes):
        chunk_id   = wav_bytes[pos : pos + 4]
        chunk_size = struct.unpack_from("<I", wav_bytes, pos + 4)[0]
        data_start = pos + 8
        data_end   = data_start + chunk_size

        if chunk_id == b"fmt ":
            # Standard PCM fmt chunk layout (16 bytes minimum):
            #   audio_format   : 2 bytes  (1 = PCM)
            #   channels       : 2 bytes
            #   sample_rate    : 4 bytes
            #   byte_rate      : 4 bytes  (skipped)
            #   block_align    : 2 bytes  (skipped)
            #   bits_per_sample: 2 bytes
            if chunk_size >= 16:
                channels      = struct.unpack_from("<H", wav_bytes, data_start + 2)[0]
                sample_rate   = struct.unpack_from("<I", wav_bytes, data_start + 4)[0]
                bits_per_sample = struct.unpack_from("<H", wav_bytes, data_start + 14)[0]
                sampwidth     = bits_per_sample // 8

        elif chunk_id == b"data":
            # Raw PCM lives here — this is all we need
            pcm_data = wav_bytes[data_start : data_end]
            break   # no need to scan further

        # WAV chunks must be word-aligned (padded to even byte boundary)
        pos = data_end + (chunk_size % 2)

    if pcm_data is None or channels is None or sample_rate is None or sampwidth is None:
        return None

    return channels, sample_rate, sampwidth, pcm_data


def merge_wavs(chunks, pause_ms=300):
    """
    Merge a list of WAV byte-strings into one WAV file, with an optional
    silence gap inserted between consecutive segments.

    Input  : list of bytes (each element is a complete WAV file)
    Returns: (merged_wav_bytes, total_audio_duration_seconds)

    Implementation uses _wav_extract_pcm() (direct RIFF byte parsing) to
    read the raw PCM from each input chunk.  Python's wave module is used
    only to write the final output, where we control every parameter.
    This completely eliminates the double-audio bug that appeared when
    wave.Wave_read was used to parse Piper-generated WAV files.
    """
    if not chunks:
        return b"", 0.0

    wav_params = None    # (channels, sample_rate, sampwidth)
    all_pcm: list[bytes] = []

    for chunk in chunks:
        result = _wav_extract_pcm(chunk)
        if result is None:
            continue
        channels, sample_rate, sampwidth, pcm_data = result
        if not pcm_data:
            continue
        if wav_params is None:
            wav_params = (channels, sample_rate, sampwidth)
        all_pcm.append(pcm_data)

    if not all_pcm or wav_params is None:
        # No valid WAV data found — return raw concatenation as a last resort
        return b"".join(chunks), 0.0

    channels, sample_rate, sampwidth = wav_params

    # Build a silence segment for inter-speaker gaps
    if pause_ms > 0:
        silence_samples = int(sample_rate * pause_ms / 1000)
        silence = b"\x00" * (silence_samples * channels * sampwidth)
    else:
        silence = b""

    # Concatenate PCM segments; insert silence BETWEEN them (not after the last)
    combined_pcm = b""
    for i, pcm in enumerate(all_pcm):
        combined_pcm += pcm
        if silence and i < len(all_pcm) - 1:
            combined_pcm += silence

    # Write the final WAV using Python's wave module (write-only path — safe)
    out = io.BytesIO()
    with wave.open(out, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sampwidth)
        wf.setframerate(sample_rate)
        wf.writeframes(combined_pcm)

    # Compute exact duration from frame count
    total_frames = len(combined_pcm) // (channels * sampwidth)
    total_dur    = total_frames / sample_rate

    return out.getvalue(), total_dur


def split_into_chunks(text, max_chars=MAX_CHARS):
    """Split large text into synthesis-sized chunks at paragraph boundaries."""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks, current = [], ""
    for para in paragraphs:
        if len(current) + len(para) + 2 <= max_chars:
            current = (current + "\n\n" + para).strip()
        else:
            if current:
                chunks.append(current)
            if len(para) > max_chars:
                sents = re.split(r"(?<=[.!?])\s+", para)
                current = ""
                for s in sents:
                    if len(current) + len(s) + 1 <= max_chars:
                        current = (current + " " + s).strip()
                    else:
                        if current:
                            chunks.append(current)
                        current = s
            else:
                current = para
    if current:
        chunks.append(current)
    return chunks or [text]


# ── Script Parser ─────────────────────────────────────────────────────────────

def parse_script(text):
    """
    Parse lines in 'Speaker: text' format.
    Returns a list of {speaker, text} dicts IN ORDER, preserving duplicates.
    """
    lines = []
    for raw in text.strip().splitlines():
        raw = raw.strip()
        if not raw or raw.upper() in ("END", "---", "==="):
            continue
        m = re.match(r"^([A-Za-z0-9 _\-]+):\s*(.+)$", raw)
        if m:
            speaker = m.group(1).strip()
            speech  = m.group(2).strip()
            if speech:
                lines.append({"speaker": speaker, "text": speech})
    return lines


def get_unique_speakers(lines):
    seen, result = set(), []
    for l in lines:
        if l["speaker"] not in seen:
            seen.add(l["speaker"])
            result.append(l["speaker"])
    return result


# ── UI Components ─────────────────────────────────────────────────────────────

def voice_speed_pitch_controls(key_prefix, default_voice_idx=0):
    c1, c2, c3 = st.columns(3)
    with c1:
        label = st.selectbox("Voice", VOICE_LABELS,
                             index=default_voice_idx, key=f"{key_prefix}_voice")
    with c2:
        speed = st.slider("Speed", 0.5, 2.0, 1.0, 0.1, key=f"{key_prefix}_speed",
                          help="0.5 = slow · 1.0 = normal · 2.0 = fast")
    with c3:
        pitch = st.slider("Pitch (semitones)", -12, 12, 0, 1,
                          key=f"{key_prefix}_pitch",
                          help="0 = normal · positive = higher · negative = lower")
    return VOICES[label], speed, pitch


def show_audio_output(audio_bytes, stats, filename="output.wav"):
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Processing time", f"{stats.get('processing_time', 0):.2f}s")
    c2.metric("Audio duration",  f"{stats.get('audio_duration',  0):.1f}s")
    c3.metric("RTF",             f"{stats.get('rtf', 0):.3f}")
    c4.metric("Cache",           "Hit ✅" if stats.get("cache_hit") else "Miss")
    st.audio(audio_bytes, format="audio/wav")
    st.download_button("⬇️  Download WAV", data=audio_bytes,
                       file_name=filename, mime="audio/wav",
                       use_container_width=True, type="secondary")
    rtf = stats.get("rtf", 0)
    if rtf > 0:
        if rtf < 0.5:
            st.success(f"🚀  {1/rtf:.1f}× faster than real-time")
        elif rtf < 1.0:
            st.info(f"✅  Good performance — RTF {rtf:.3f}")
        else:
            st.warning(f"⚠️  Slower than real-time — RTF {rtf:.3f}")


def run_single_synthesis(text, voice_key, speed, pitch, filename="output.wav"):
    prog = st.progress(0, text="Queuing synthesis job...")
    stat = st.empty()
    try:
        q      = queue_job(text, voice_key, speed, pitch)
        job_id = q["job_id"]

        if q.get("cache_hit"):
            prog.progress(90, text="Cache hit — downloading...")
            audio = download_wav(job_id)
            prog.empty(); stat.empty()
            show_audio_output(audio,
                {"cache_hit": True, "processing_time": 0,
                 "audio_duration": 0, "rtf": 0}, filename)
            return

        def on_progress(poll, p):
            pct = min(10 + int(p.get("progress", 0) * 0.78), 88)
            prog.progress(pct, text=f"Synthesising... {p.get('progress', 0)}%")
            stat.caption(f"Status: {p['status']} · poll #{poll}")

        job_data = poll_until_done(job_id, on_progress)
        prog.progress(93, text="Downloading audio...")
        audio = download_wav(job_id)
        prog.empty(); stat.empty()
        show_audio_output(audio, job_data, filename)

    except Exception as e:
        st.error(f"❌  {e}")
    finally:
        try: prog.empty(); stat.empty()
        except: pass


def run_multi_synthesis(lines_with_config, pause_ms=300, filename="dialogue.wav"):
    """
    Queue all lines, poll until complete, download, merge IN ORDER, display.

    job_id_per_line tracks the job_id for every line position, including
    cache hits that return the same job_id for identical (text, voice)
    combinations.  ordered_chunks maps those IDs back to audio bytes in
    correct script order so no line is skipped or duplicated.
    """
    prog    = st.progress(0, text="Starting...")
    stat    = st.empty()
    log_box = st.empty()
    logs: list[str] = []

    def log(msg):
        logs.append(msg)
        log_box.code("\n".join(logs[-12:]))

    try:
        # ── Queue ─────────────────────────────────────────────
        log(f"Queuing {len(lines_with_config)} jobs...")
        job_id_per_line: list[str] = []
        unique_job_ids:  list[str] = []
        seen_job_ids:    set[str]  = set()

        for i, cfg in enumerate(lines_with_config):
            prog.progress(int(3 + (i / len(lines_with_config)) * 12),
                          text=f"Queuing job {i+1}/{len(lines_with_config)}...")
            q   = queue_job(cfg["text"], cfg["voice"], cfg["speed"], cfg["pitch"])
            jid = q["job_id"]
            job_id_per_line.append(jid)
            if jid not in seen_job_ids:
                seen_job_ids.add(jid)
                unique_job_ids.append(jid)
            cache = " (cache)" if q.get("cache_hit") else ""
            log(f"  [{i+1}] queued{cache}: {cfg['text'][:55]}...")

        log(f"Unique synthesis jobs: {len(unique_job_ids)} / {len(lines_with_config)}")
        prog.progress(18, text="All queued — synthesising in parallel...")

        # ── Poll unique jobs ───────────────────────────────────
        completed: dict = {}
        polls = 0
        while len(completed) < len(unique_job_ids) and polls < MAX_POLLS:
            time.sleep(POLL_INTERVAL)
            polls += 1
            for jid in [j for j in unique_job_ids if j not in completed]:
                p = requests.get(f"{_api_base()}/jobs/{jid}", timeout=15).json()
                if p["status"] == "completed":
                    completed[jid] = p
                    idx = unique_job_ids.index(jid)
                    log(f"  ✓ [{idx+1}] done — {p.get('audio_duration', 0):.1f}s")
                elif p["status"] == "failed":
                    raise Exception(f"Job {jid[:8]} failed: {p.get('error')}")
            pct = int(18 + (len(completed) / len(unique_job_ids)) * 62)
            prog.progress(pct,
                text=f"Synthesising: {len(completed)}/{len(unique_job_ids)} complete...")
            stat.caption(f"Completed: {len(completed)}/{len(unique_job_ids)} · poll #{polls}")

        if len(completed) < len(unique_job_ids):
            raise Exception("Timeout — some jobs did not complete.")

        # ── Download unique audio files ────────────────────────
        log("Downloading audio chunks...")
        prog.progress(82, text="Downloading...")
        audio_cache: dict[str, bytes] = {}
        for jid in unique_job_ids:
            audio_cache[jid] = download_wav(jid)
            log(f"  ↓ {jid[:8]}: {len(audio_cache[jid]) // 1024} KB")

        # ── Build ordered segment list (one entry per script line) ─
        # IMPORTANT: look up by job_id_per_line[i], NOT by unique_job_ids[i].
        # Cache hits reuse the same job_id for multiple lines; we must emit
        # one audio segment per line in script order regardless.
        ordered_chunks = [audio_cache[jid] for jid in job_id_per_line]

        log(f"Merging {len(ordered_chunks)} segments (script order)...")
        prog.progress(94, text="Merging audio...")
        final_audio, total_dur = merge_wavs(ordered_chunks, pause_ms=pause_ms)

        prog.progress(100, text="✅  Done!")
        prog.empty(); stat.empty(); log_box.empty()

        # ── Results ───────────────────────────────────────────
        total_proc = sum(completed[j].get("processing_time", 0) for j in unique_job_ids)
        avg_rtf    = (sum(completed[j].get("rtf", 0) for j in unique_job_ids)
                      / len(unique_job_ids))

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Lines",      len(lines_with_config))
        c2.metric("Duration",   f"{total_dur:.1f}s")
        c3.metric("Synth time", f"{total_proc:.1f}s")
        c4.metric("Avg RTF",    f"{avg_rtf:.3f}")

        st.audio(final_audio, format="audio/wav")
        st.download_button("⬇️  Download WAV", data=final_audio,
                           file_name=filename, mime="audio/wav",
                           use_container_width=True, type="secondary")

        if avg_rtf > 0:
            if avg_rtf < 0.5:
                st.success(f"🚀  {1/avg_rtf:.1f}× faster than real-time")
            elif avg_rtf < 1.0:
                st.info(f"✅  Good — Avg RTF {avg_rtf:.3f}")
            else:
                st.warning(f"⚠️  Slower than real-time — RTF {avg_rtf:.3f}")

        with st.expander("📋  Per-line details"):
            for i, (jid, cfg) in enumerate(zip(job_id_per_line, lines_with_config)):
                j = completed[jid]
                voice_name = [k for k, v in VOICES.items() if v == cfg["voice"]][0]
                preview = cfg["text"][:80] + ("..." if len(cfg["text"]) > 80 else "")
                st.markdown(f"**#{i+1}** `{voice_name}` — {preview}")
                cc = st.columns(4)
                cc[0].metric("Duration",  f"{j.get('audio_duration', 0):.1f}s")
                cc[1].metric("Proc time", f"{j.get('processing_time', 0):.2f}s")
                cc[2].metric("RTF",       f"{j.get('rtf', 0):.3f}")
                cc[3].metric("Cache",     "Hit" if j.get("cache_hit") else "Miss")

    except Exception as e:
        st.error(f"❌  {e}")
    finally:
        try: prog.empty(); stat.empty()
        except: pass


# ── Page layout ───────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="Piper TTS",
    page_icon="🎙️",
    layout="wide",
    initial_sidebar_state="collapsed",
)

col_title, col_health = st.columns([3, 1])
with col_title:
    st.title("🎙️  Piper TTS")
    st.caption("Production-grade Text-to-Speech · FastAPI + Celery + Redis")

with col_health:
    h      = api_health()
    status = h.get("status", "offline")
    if status in ("ok", "degraded"):
        if status == "ok":
            st.success("● API online")
        else:
            st.warning("● API degraded")
        st.caption(f"Queue: {h.get('queue_length', 0)} jobs")
    else:
        st.error("● API offline")
        st.caption("Run: `docker-compose up -d` then refresh")
        st.stop()

st.divider()

mode = st.radio(
    "Select mode",
    ["🎤  Single Speaker", "🎭  Multi-Speaker"],
    horizontal=True,
    label_visibility="collapsed",
)

st.divider()

# ═══════════════════════════════════════════════════════════════════════════════
# SINGLE SPEAKER MODE
# ═══════════════════════════════════════════════════════════════════════════════

if mode == "🎤  Single Speaker":
    st.subheader("🎤  Single Speaker")

    input_method = st.radio("Input method", ["✏️  Type text", "📄  Upload .txt file"],
                            horizontal=True, key="ss_input")
    st.divider()

    text_to_speak  = ""
    output_filename = "output.wav"

    if input_method == "✏️  Type text":
        text_to_speak = st.text_area(
            "Enter your text",
            height=180,
            max_chars=MAX_CHARS,
            placeholder="Type or paste your text here...",
            key="ss_text",
        )
        st.caption(f"{len(text_to_speak):,} / {MAX_CHARS:,} characters")

    else:
        uploaded = st.file_uploader("Upload a .txt file", type=["txt"], key="ss_file")
        if uploaded:
            text_to_speak   = uploaded.read().decode("utf-8", errors="ignore").strip()
            output_filename = uploaded.name.replace(".txt", ".wav")
            st.success(f"📄  {uploaded.name} — {len(text_to_speak):,} characters loaded")
            chunks = split_into_chunks(text_to_speak)
            if len(chunks) > 1:
                st.info(f"Large file — will be processed as {len(chunks)} chunks and merged.")
            with st.expander("👁️  Preview content"):
                st.text(text_to_speak[:1500] + ("..." if len(text_to_speak) > 1500 else ""))

    if text_to_speak:
        st.divider()
        st.markdown("**Voice settings**")
        voice_key, speed, pitch = voice_speed_pitch_controls("ss")

        st.divider()
        if st.button("🎵  Generate Audio", type="primary",
                     use_container_width=True, key="ss_generate"):
            chunks = split_into_chunks(text_to_speak)
            if len(chunks) == 1:
                run_single_synthesis(text_to_speak, voice_key, speed, pitch, output_filename)
            else:
                st.info(f"Processing {len(chunks)} chunks in parallel...")
                lines_cfg = [{"text": c, "voice": voice_key,
                              "speed": speed, "pitch": pitch} for c in chunks]
                run_multi_synthesis(lines_cfg, pause_ms=0, filename=output_filename)


# ═══════════════════════════════════════════════════════════════════════════════
# MULTI-SPEAKER MODE
# ═══════════════════════════════════════════════════════════════════════════════

else:
    st.subheader("🎭  Multi-Speaker")
    st.caption("Write a script with `SpeakerName: text` format — speakers are detected automatically")

    input_method = st.radio("Input method",
                            ["✏️  Type / paste script", "📄  Upload .txt script"],
                            horizontal=True, key="ms_input")
    st.divider()

    script_text = ""

    if input_method == "✏️  Type / paste script":
        if "ms_script_content" not in st.session_state:
            st.session_state["ms_script_content"] = SAMPLE_SCRIPT
        script_text = st.text_area(
            "Paste your script",
            value=st.session_state["ms_script_content"],
            height=260,
            key="ms_text_input",
            placeholder="Host: Welcome everyone...\nGuest: Thank you for having me...",
        )
        st.session_state["ms_script_content"] = script_text
        st.caption("Format: `SpeakerName: What they say`  ·  One line per speech turn")

    else:
        uploaded = st.file_uploader("Upload a .txt script", type=["txt"], key="ms_file")
        if uploaded:
            raw_uploaded = uploaded.read().decode("utf-8", errors="ignore").strip()
            st.session_state["ms_script_content"] = raw_uploaded
            script_text = raw_uploaded
            st.success(f"📄  {uploaded.name} — {len(script_text):,} characters loaded")
            with st.expander("👁️  Preview content"):
                st.text(script_text[:1500] + ("..." if len(script_text) > 1500 else ""))

    if script_text:
        parsed   = parse_script(script_text)
        speakers = get_unique_speakers(parsed)

        if not parsed:
            st.warning("⚠️  No valid lines detected. Use format: `SpeakerName: text`")
            st.stop()

        st.divider()

        col_d1, col_d2, col_d3 = st.columns(3)
        col_d1.metric("Lines detected",    len(parsed))
        col_d2.metric("Speakers detected", len(speakers))
        col_d3.metric("Total characters",  sum(len(l["text"]) for l in parsed))

        st.divider()
        st.markdown("**Assign a voice to each detected speaker**")
        st.caption("Voices are auto-assigned — change any speaker below")

        speaker_cfg = {}
        for row_start in range(0, len(speakers), 3):
            row_speakers = speakers[row_start : row_start + 3]
            cols = st.columns(len(row_speakers))
            for col, speaker in zip(cols, row_speakers):
                idx = speakers.index(speaker)
                with col:
                    with st.container(border=True):
                        st.markdown(f"**🎙️  {speaker}**")
                        default_idx = VOICE_LABELS.index(
                            SPEAKER_VOICE_CYCLE[idx % len(SPEAKER_VOICE_CYCLE)])
                        chosen = st.selectbox(
                            "Voice", VOICE_LABELS,
                            index=default_idx,
                            key=f"ms_voice_{speaker}",
                            label_visibility="collapsed",
                        )
                        c1, c2 = st.columns(2)
                        with c1:
                            spd = st.slider("Speed", 0.5, 2.0, 1.0, 0.1,
                                            key=f"ms_spd_{speaker}",
                                            label_visibility="collapsed")
                        with c2:
                            pit = st.slider("Pitch", -12, 12, 0, 1,
                                            key=f"ms_pit_{speaker}",
                                            label_visibility="collapsed")
                        speaker_cfg[speaker] = {
                            "voice": VOICES[chosen],
                            "speed": spd,
                            "pitch": pit,
                        }

        st.divider()

        with st.expander(f"👁️  Preview all {len(parsed)} lines"):
            for i, line in enumerate(parsed):
                cfg   = speaker_cfg.get(line["speaker"], {})
                vname = ([k for k, v in VOICES.items()
                          if v == cfg.get("voice", "")][0]
                         if cfg else "—")
                st.write(
                    f"**{i+1}.** `{line['speaker']}` · *{vname.split('(')[0].strip()}* "
                    f"— {line['text'][:90]}{'...' if len(line['text']) > 90 else ''}"
                )

        pause_ms = st.slider(
            "Pause between speakers (ms)", 0, 1000, 300, 50,
            help="Silence gap inserted between each speaker's line",
        )

        st.divider()

        if "ms_running" not in st.session_state:
            st.session_state.ms_running = False

        if (st.button("🎬  Generate Dialogue", type="primary",
                      use_container_width=True, key="ms_generate")
                and not st.session_state.ms_running):
            st.session_state.ms_running = True
            lines_cfg = [
                {
                    "text":  line["text"],
                    "voice": speaker_cfg[line["speaker"]]["voice"],
                    "speed": speaker_cfg[line["speaker"]]["speed"],
                    "pitch": speaker_cfg[line["speaker"]]["pitch"],
                }
                for line in parsed
            ]
            run_multi_synthesis(lines_cfg, pause_ms=pause_ms, filename="dialogue.wav")
            st.session_state.ms_running = False

    else:
        st.info("👆  Paste your script or upload a file above to get started.")
        with st.expander("📖  Script format guide"):
            st.markdown("""
**Format each line as:**
```
SpeakerName: What they say here.
```
**Rules:**
- Speaker names can be anything: `Alice`, `Host`, `Engineer`, `Narrator`
- Each unique name gets its own voice (auto-assigned, you can change it)
- Blank lines and lines like `END` or `---` are ignored
- You can have as many speakers as you like
            """)
        st.markdown("**Quick example presets:**")
        c1, c2, c3 = st.columns(3)
        with c1:
            if st.button("📄  Interview", use_container_width=True):
                st.session_state["ms_script_content"] = (
                    "Host: Welcome to the show.\n"
                    "Guest: Thank you for having me.\n"
                    "Host: Tell us about your work in AI.\n"
                    "Guest: I build systems that can understand and generate human speech."
                )
                st.rerun()
        with c2:
            if st.button("📄  Debate", use_container_width=True):
                st.session_state["ms_script_content"] = (
                    "Moderator: Welcome to tonight's debate on technology.\n"
                    "Speaker A: AI will create more jobs than it eliminates.\n"
                    "Speaker B: Automation will displace workers in many industries.\n"
                    "Moderator: Both speakers raise important points."
                )
                st.rerun()
        with c3:
            if st.button("📄  News", use_container_width=True):
                st.session_state["ms_script_content"] = (
                    "Anchor 1: Good evening. I am reporting from the technology desk.\n"
                    "Anchor 2: Tonight we cover the latest in artificial intelligence.\n"
                    "Anchor 1: Researchers have announced a major breakthrough.\n"
                    "Anchor 2: We will have more details as the story develops."
                )
                st.rerun()

# ── Sidebar ───────────────────────────────────────────────────────────────────

with st.sidebar:
    st.header("ℹ️  System Info")

    st.subheader("Available voices")
    for label in VOICE_LABELS:
        dot = "🔵" if "High" in label else "🟡"
        st.write(f"{dot}  {label}")

    st.divider()
    st.subheader("RTF guide")
    st.write("**< 0.5** 🚀 Excellent")
    st.write("**< 1.0** ✅ Good")
    st.write("**> 1.0** ⚠️ Slow")
    st.caption("RTF = Real-Time Factor. Lower is better.")

    st.divider()
    st.subheader("Quick links")
    st.write(f"[API Docs]({_DEFAULT_API_BASE}/docs)")
    st.write(f"[Health check]({_DEFAULT_API_BASE}/health)")

    st.divider()
    if st.button("🔄  Refresh health", use_container_width=True):
        st.rerun()