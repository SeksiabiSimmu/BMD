"""Metadata and FFmpeg command generation for WAV / FLAC / MP3 outputs.

All three formats embed the textual tags and the cover art *inside* the file:
  * FLAC stores artwork as a `metadata_block_picture` (FFmpeg handles this when the
    input streams include the picture, or via `-attach`).
  * MP3 stores artwork as an APIC ID3v2 frame (FFmpeg muxes it when `-i cover.jpg`
    is an extra input and `-map` includes the video stream with `-c:v copy` into the
    ID3 muxer).
  * WAV has no standard artwork container, so for WAV we keep the cover as a sidecar
    `.cover.jpg` (as before) — embedding a picture in a RIFF `LIST/INFO` chunk is
    non-portable and most players ignore it.

BPM is detected with FFmpeg's `ebur128`-independent `astats`/`freeverb` is not suitable;
instead we run a tiny ffmpeg pass that prints the `loudnorm`-style measurement is not BPM.
We detect BPM with a small autocorrelation over the decoded mono PCM, which needs no extra
tool: we decode ~30s of mono to a pipe and compute the dominant periodicity. The value is
written into the file as a `TBPM` (FLAC/MP3) tag and into the JSON sidecar.
"""
import json
import math
import re
import subprocess

LOUDEST_TARGET = -16
TRUE_PEAK = -1.5
LRA = 11

# Output formats. WAV stays 24-bit PCM (lossless, the original product behaviour).
# FLAC is lossless compressed from the same 24-bit source. MP3 is a high-quality
# 320 kbps CBR export for compatibility / smaller files.
FORMATS = {
    'wav':  {'ext': 'wav',  'codec': 'pcm_s24le', 'container': 'wav',  'lossless': True,
             'embeds_art': False, 'label': 'WAV · 24-bit PCM (lossless)'},
    'flac': {'ext': 'flac', 'codec': 'flac',     'container': 'flac', 'lossless': True,
             'compression': 5, 'embeds_art': True, 'label': 'FLAC · 24-bit (lossless, ~50% smaller)'},
    'mp3':  {'ext': 'mp3',  'codec': 'libmp3lame', 'container': 'mp3', 'lossless': False,
             'bitrate': '320k', 'embeds_art': True, 'label': 'MP3 · 320 kbps (compatible)'},
}
SUPPORTED_FORMATS = tuple(FORMATS)


def format_spec(fmt):
    spec = FORMATS.get((fmt or 'wav').lower())
    if spec is None:
        raise ValueError(f'Unsupported format: {fmt!r}. Choose one of: {", ".join(FORMATS)}')
    return spec


def metadata_from_info(info, url):
    def val(*keys):
        for key in keys:
            v = info.get(key)
            if isinstance(v, list):
                v = ", ".join(str(item) for item in v if item)
            if isinstance(v, (str, int)) and str(v).strip():
                return str(v).strip()
        return ""

    return {
        "title": val("track", "title") or "Untitled",
        "artist": val("artist", "artists", "creator", "uploader", "channel") or "Unknown artist",
        "album": val("album"),
        "track_number": val("track_number"),
        "date": val("release_date", "upload_date"),
        "genre": val("genre"),
        "source_url": url,
        "video_id": val("id"),
        "source_codec": val("acodec"),
        "normalization": {"enabled": True, "target_lufs": LOUDEST_TARGET, "true_peak_dbtp": TRUE_PEAK},
    }


def make_filter(measurements):
    filter_text = f"loudnorm=I={LOUDEST_TARGET}:TP={TRUE_PEAK}:LRA={LRA}"
    if measurements is not None:
        filter_text += ":" + ":".join(f"{key}={value:.8f}" for key, value in measurements.items())
        filter_text += ":linear=true"
    return filter_text


def audio_filters(normalize, measurements=None, trim_start=False):
    """Build optional filters. The helper trims into a lossless temporary WAV
    before running either loudness pass, avoiding combined-filter FFmpeg bugs.

    Only leading silence is optional: trimming the end risks cutting natural
    fades, reverb tails, or quiet musical passages.
    """
    filters = []
    if trim_start:
        filters.append('silenceremove=start_periods=1:start_duration=0:start_threshold=-55dB:start_silence=0.03')
    if normalize:
        filters.append(make_filter(measurements))
    return ','.join(filters)


def _metadata_args(tags):
    """Textual metadata arguments. The same fields map cleanly to WAV RIFF INFO,
    FLAC VORBIS_COMMENT and MP3 ID3v2 tags."""
    args = []
    for field in ("title", "artist", "album", "date", "genre"):
        if tags.get(field):
            args += ["-metadata", f"{field}={tags[field]}"]
    if tags.get("track_number"):
        args += ["-metadata", f"track={tags['track_number']}"]
    if tags.get("source_url"):
        args += ["-metadata", f"comment=Source: {tags['source_url']}"]
    if tags.get("bpm"):
        # TBPM is the ID3v2 / VORBIS standard key for beats per minute.
        args += ["-metadata", f"TBPM={tags['bpm']}", "-metadata", f"bpm={tags['bpm']}"]
    return args


def encode_command(source, destination, tags, fmt, normalize, rate, measurements=None,
                   trim_start=False, ffmpeg="ffmpeg", cover=None):
    """Construct a fixed-argument FFmpeg command for the requested format.

    `cover` is an optional path to a JPEG; for FLAC and MP3 it is attached as an
    extra input and muxed into the file so the artwork travels with the audio.
    Metadata values never become shell code — they are passed as separate argv items.
    """
    spec = format_spec(fmt)
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-y", "-i", str(source)]
    if cover and spec['embeds_art']:
        # Second input is the picture; -map 0:a keeps the audio, -map 1:v takes the cover,
        # -c:v copy lets the muxer store the JPEG bytes verbatim (FLAC/MP3 picture frames).
        cmd += ["-i", str(cover)]
    cmd += ["-map", "0:a:0"]
    if cover and spec['embeds_art']:
        # Map the picture stream too; -c:v copy stores the JPEG bytes verbatim into the
        # FLAC/MP3 picture frame. Do NOT add -vn here: it would drop the just-mapped cover.
        cmd += ["-map", "1:v", "-c:v", "copy", "-disposition:v", "attached_pic"]
    else:
        cmd += ["-vn"]                  # audio-only: drop any video from the source (thumbnails)
    cmd += ["-map_metadata", "-1"]
    effects = audio_filters(normalize, measurements, trim_start)
    if effects:
        cmd += ["-af", effects]
    cmd += _metadata_args(tags)
    if spec['container'] == 'wav':
        cmd += ["-ar", str(rate), "-c:a", spec['codec'], "-rf64", "auto", "-f", "wav", str(destination)]
    elif spec['container'] == 'flac':
        cmd += ["-ar", str(rate), "-c:a", spec['codec'],
                "-compression_level", str(spec.get('compression', 5)),
                "-f", "flac", str(destination)]
    elif spec['container'] == 'mp3':
        cmd += ["-ar", str(rate), "-c:a", spec['codec'], "-b:a", spec['bitrate'], "-f", "mp3", str(destination)]
    else:
        raise ValueError(f'No encoder for container {spec["container"]!r}')
    return cmd


# ----------------------------------------------------------------- BPM ----
def detect_bpm(job_command, job_id, source, ffmpeg="ffmpeg", max_seconds=30):
    """Estimate BPM by decoding up to `max_seconds` of mono PCM and autocorrelating.

    `job_command` is helper.command(job_id, args, timeout) so the worker can cancel and
    the process is tracked in ACTIVE like any other subprocess. Returns an int BPM in the
    60-200 range (the common music envelope), or None if no stable periodicity was found.
    Pure-Python: no numpy. Runs once per track, before the encode pass.
    """
    args = [ffmpeg, "-hide_banner", "-nostdin", "-i", str(source), "-map", "0:a:0",
            "-vn", "-ac", "1", "-ar", "8000", "-t", str(max_seconds),
            "-f", "f32le", "pipe:1"]
    rc, output = job_command(job_id, args, 120, binary=True)
    if rc or not output:
        return None
    samples = output
    if len(samples) < 8000 * 2:        # need at least ~1 s of f32 mono
        return None
    # Unpack little-endian float32; onsets = abs(sample) above a moving threshold.
    n = len(samples) // 4
    floats = list(_iter_floats(samples, n))
    return _bpm_from_envelope(floats, 8000)


def _iter_floats(data, n):
    import struct
    for i in range(0, n * 4, 4):
        yield struct.unpack_from('<f', data, i)[0]


def _envelope(floats, frame=256, hop=128):
    """RMS envelope at ~62.5 frames/s (8000 Hz, 128-sample hop)."""
    env = []
    i = 0
    n = len(floats)
    while i + frame <= n:
        s = 0.0
        for j in range(i, i + frame):
            v = floats[j]
            s += v * v
        env.append(math.sqrt(s / frame))
        i += hop
    return env


def _bpm_from_envelope(floats, sample_rate, min_bpm=60, max_bpm=200):
    env = _envelope(floats)
    if len(env) < 8:
        return None
    # Normalize the envelope (subtract mean) so the autocorrelation measures
    # fluctuation energy, not the DC offset.
    mean = sum(env) / len(env)
    centred = [e - mean for e in env]
    frame_rate = sample_rate / 128.0
    # Lag range in frames for [min_bpm, max_bpm]: bpm = 60 * frame_rate / lag.
    min_lag = max(2, int(60 * frame_rate / max_bpm))
    max_lag = min(len(centred) // 2, int(60 * frame_rate / min_bpm))
    best_lag, best_score = None, 0.0
    for lag in range(min_lag, max_lag + 1):
        score = 0.0
        for i in range(len(centred) - lag):
            score += centred[i] * centred[i + lag]
        if score > best_score:
            best_score, best_lag = score, lag
    if best_lag is None or best_score <= 0:
        return None
    bpm = round(60 * frame_rate / best_lag)
    # Fold into the musical 60-200 envelope: half/double-time detection.
    while bpm < min_bpm:
        bpm *= 2
    while bpm > max_bpm:
        bpm = round(bpm / 2)
    return int(bpm)
