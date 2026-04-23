/**
 * src/api/ttsClient.js
 * --------------------
 * Complete React API client for the Piper TTS scalable backend.
 *
 * Usage:
 *   import { generateSpeech } from './api/ttsClient';
 *
 *   const { audioBlob, stats } = await generateSpeech({
 *     text: "Hello world",
 *     voice: "en_US_female_high",
 *     speed: 1.0,
 *     pitch: 0.0,
 *     onProgress: (pct) => setProgress(pct),
 *   });
 *   const url = URL.createObjectURL(audioBlob);
 *   new Audio(url).play();
 */

const API_BASE = process.env.REACT_APP_TTS_API_URL || 'http://localhost:80';
const POLL_INTERVAL_MS  = 1500;   // poll every 1.5 seconds
const MAX_POLL_ATTEMPTS = 120;    // give up after 3 minutes

// ── Low-level helpers ──────────────────────────────────────────────────────

async function apiFetch(path, options = {}) {
  const res = await fetch(`${API_BASE}${path}`, {
    headers: { 'Content-Type': 'application/json', ...options.headers },
    ...options,
  });
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try { detail = (await res.json()).detail || detail; } catch {}
    throw new Error(detail);
  }
  return res;
}

// ── Public API ────────────────────────────────────────────────────────────

/**
 * Fetch all available voices for the voice selector.
 * Returns array of { key, name, gender, locale, quality, description }
 */
export async function fetchVoices() {
  const res = await apiFetch('/voices');
  return res.json();
}

/**
 * Check API health.
 */
export async function fetchHealth() {
  const res = await apiFetch('/health');
  return res.json();
}

/**
 * Full generate → poll → download pipeline.
 *
 * @param {Object} options
 * @param {string}   options.text        - Text to synthesise
 * @param {string}   options.voice       - Voice key (from GET /voices)
 * @param {number}   options.speed       - 0.5 – 2.0
 * @param {number}   options.pitch       - -12 – 12
 * @param {number}   options.chunkWords  - words per parallel chunk (default 30)
 * @param {Function} options.onProgress  - (percent: number) => void
 * @param {AbortSignal} options.signal   - AbortController signal for cancellation
 *
 * @returns {{ audioBlob: Blob, stats: Object }}
 */
export async function generateSpeech({
  text,
  voice = 'en_US_female_high',
  speed = 1.0,
  pitch = 0.0,
  chunkWords = 30,
  onProgress = () => {},
  signal,
}) {
  // ── Step 1: Queue the job ──────────────────────────────────
  onProgress(5);
  const queueRes = await apiFetch('/generate-audio', {
    method: 'POST',
    body: JSON.stringify({ text, voice, speed, pitch, chunk_words: chunkWords }),
    signal,
  });
  const queueData = await queueRes.json();
  const { job_id, status, cache_hit, download_url } = queueData;

  // ── Step 2: Cache hit — download immediately ───────────────
  if ((status === 'cached' || status === 'completed') && download_url) {
    onProgress(90);
    const audioBlob = await downloadAudio(download_url, signal);
    onProgress(100);
    return { audioBlob, stats: { cache_hit: true, job_id } };
  }

  // ── Step 3: Poll for completion ────────────────────────────
  let attempts = 0;
  while (attempts < MAX_POLL_ATTEMPTS) {
    if (signal?.aborted) throw new DOMException('Cancelled', 'AbortError');

    await sleep(POLL_INTERVAL_MS);
    attempts++;

    const pollRes  = await apiFetch(`/jobs/${job_id}`, { signal });
    const pollData = await pollRes.json();

    // Update progress bar
    onProgress(Math.min(10 + (pollData.progress ?? 0) * 0.8, 88));

    if (pollData.status === 'completed' && pollData.download_url) {
      // ── Step 4: Download audio ─────────────────────────────
      onProgress(90);
      const audioBlob = await downloadAudio(pollData.download_url, signal);
      onProgress(100);
      return {
        audioBlob,
        stats: {
          job_id,
          processing_time: pollData.processing_time,
          audio_duration:  pollData.audio_duration,
          rtf:             pollData.rtf,
          cache_hit:       pollData.cache_hit,
          expires_in:      pollData.expires_in,
        },
      };
    }

    if (pollData.status === 'failed') {
      throw new Error(pollData.error || 'Synthesis failed on server.');
    }
  }

  throw new Error('Timeout: synthesis took too long. Please try again.');
}

/**
 * Download audio from the given URL and return a Blob.
 * Uses streaming fetch — memory efficient for large files.
 */
async function downloadAudio(url, signal) {
  const res = await fetch(url, { signal });
  if (!res.ok) throw new Error(`Download failed: HTTP ${res.status}`);
  return res.blob();
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}


// ── React hook (optional convenience) ─────────────────────────────────────

/**
 * useTTS — React hook wrapping generateSpeech with state management.
 *
 * Example:
 *   const { generate, audioUrl, progress, loading, error, stats } = useTTS();
 *   await generate({ text, voice });
 *   <audio src={audioUrl} controls />
 */
export function useTTS() {
  // This hook requires React — import at call site
  const { useState, useRef, useCallback, useEffect } = require('react');

  const [loading, setLoading]   = useState(false);
  const [progress, setProgress] = useState(0);
  const [audioUrl, setAudioUrl] = useState(null);
  const [error, setError]       = useState(null);
  const [stats, setStats]       = useState(null);
  const abortRef                = useRef(null);
  const blobUrlRef              = useRef(null);

  // Cleanup blob URL on unmount
  useEffect(() => {
    return () => {
      if (blobUrlRef.current) URL.revokeObjectURL(blobUrlRef.current);
      abortRef.current?.abort();
    };
  }, []);

  const generate = useCallback(async (options) => {
    // Cancel any in-flight request
    abortRef.current?.abort();
    abortRef.current = new AbortController();

    // Revoke previous audio URL
    if (blobUrlRef.current) {
      URL.revokeObjectURL(blobUrlRef.current);
      blobUrlRef.current = null;
    }

    setLoading(true);
    setError(null);
    setAudioUrl(null);
    setStats(null);
    setProgress(0);

    try {
      const { audioBlob, stats: jobStats } = await generateSpeech({
        ...options,
        onProgress: setProgress,
        signal: abortRef.current.signal,
      });

      const url = URL.createObjectURL(audioBlob);
      blobUrlRef.current = url;
      setAudioUrl(url);
      setStats(jobStats);
    } catch (err) {
      if (err.name !== 'AbortError') {
        setError(err.message);
      }
    } finally {
      setLoading(false);
    }
  }, []);

  const cancel = useCallback(() => {
    abortRef.current?.abort();
    setLoading(false);
    setProgress(0);
  }, []);

  return { generate, cancel, audioUrl, progress, loading, error, stats };
}
