"""
Audio-Analyse für gleichmäßige Lautstärke und saubere Übergänge.

Der Stream-Producer hat jeden Song vollständig als PCM im Speicher, bevor er
abgespielt wird. Deshalb wird hier nicht live geregelt (Kompressor/AGC, was
Dynamik zerstört und pumpt), sondern EINMAL pro Song gemessen und ein
konstanter Verstärkungsfaktor angewendet — dasselbe Prinzip wie ReplayGain.

Drei Dinge werden ermittelt:

  1. **Lautheit** nach ITU-R BS.1770 (K-Bewertung + Gating), in LUFS.
     Daraus folgt eine feste Verstärkung auf einen Zielwert, begrenzt durch
     eine Spitzenwertreserve, damit nichts übersteuert.

  2. **Stille am Anfang/Ende.** Nur die Ränder werden abgeschnitten — Stille
     MITTEN im Titel (Pausen, Breaks) bleibt unangetastet, weil ausschließlich
     vom Anfang vorwärts und vom Ende rückwärts gesucht wird.

  3. **Pegel der Überblendbereiche.** Ist der Anfang des nächsten oder das Ende
     des laufenden Titels deutlich leiser als der jeweilige Songkörper (leiser
     Ausklang, sanftes Intro), wird die Überblendung verkürzt — sonst entsteht
     dort ein hörbares Loch.

Bewusst ohne scipy: die K-Bewertungsfilter werden analytisch im Frequenzbereich
angewendet (die Biquad-Koeffizienten nach BS.1770 werden zu einem Frequenzgang
ausgewertet und mit dem Spektrum multipliziert). Das Ergebnis stimmt mit
ffmpegs `ebur128` auf Bruchteile eines dB überein.
"""
from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass

import numpy as np

logger = logging.getLogger("personal_radio.audio")

SAMPLE_RATE = 44_100
CHANNELS    = 2
BYTES_PER_SEC = SAMPLE_RATE * CHANNELS * 2

# ── Lautheit ─────────────────────────────────────────────────────────────────
BLOCK_MS         = 400      # Gating-Blocklänge nach BS.1770
ABSOLUTE_GATE    = -70.0    # LUFS, absolutes Gate
RELATIVE_GATE    = -10.0    # LU unterhalb des Vorabmittels
MAX_BLOCKS       = 150      # Analyseumfang deckeln (~60 s) — schont die CPU
                            # auf kleinen Geräten; längere Titel werden
                            # gleichmäßig abgetastet (±0,5 dB Genauigkeit).

# ── Ränder / Überblendung ────────────────────────────────────────────────────
ENV_HOP_MS       = 20       # Auflösung der Hüllkurve
EDGE_DROP_DB     = 40.0     # so viel unter dem "lauten" Pegel gilt als Stille
EDGE_HOLD_MS     = 100      # so lange muss es laut bleiben, damit es zählt
EDGE_GUARD_MS    = 60       # Sicherheitsabstand, damit kein Anschlag wegfällt
MAX_TRIM_SEC     = 30.0     # mehr wird nie abgeschnitten


def _env_float(name: str, default: float) -> float:
    try:
        raw = os.environ.get(name, "")
        return float(raw) if str(raw).strip() != "" else default
    except (TypeError, ValueError):
        return default


def _env_flag(name: str, default: bool = True) -> bool:
    raw = os.environ.get(name, "")
    if str(raw).strip() == "":
        return default
    return str(raw).strip().lower() not in ("0", "false", "no", "off")


def target_lufs() -> float:
    """Ziel-Lautheit (Addon-Option). -14 LUFS ist der Streaming-Standard."""
    return _env_float("TARGET_LOUDNESS", -14.0)


def max_gain_db() -> float:
    """Obergrenze für die Anhebung sehr leiser Titel."""
    return abs(_env_float("MAX_GAIN_DB", 12.0))


def normalize_enabled() -> bool:
    return _env_flag("NORMALIZE_LOUDNESS", True)


def trim_enabled() -> bool:
    return _env_flag("TRIM_SILENCE", True)


# ── PCM-Umwandlung ───────────────────────────────────────────────────────────

def pcm_to_float(pcm: bytes) -> np.ndarray:
    """s16le-Bytes → float32-Array der Form (frames, CHANNELS), Bereich ±1."""
    usable = (len(pcm) // (2 * CHANNELS)) * 2 * CHANNELS
    a = np.frombuffer(pcm, dtype=np.int16, count=usable // 2)
    return a.reshape(-1, CHANNELS).astype(np.float32) / 32768.0


# ── K-Bewertung (BS.1770) ────────────────────────────────────────────────────

def _highshelf(fc: float, q: float, gain_db: float, sr: int):
    """RBJ-High-Shelf — Stufe 1 der K-Bewertung."""
    a  = 10.0 ** (gain_db / 40.0)
    w0 = 2.0 * math.pi * fc / sr
    cw, sw = math.cos(w0), math.sin(w0)
    alpha  = sw / (2.0 * q)
    sq     = 2.0 * math.sqrt(a) * alpha
    b = [a * ((a + 1) + (a - 1) * cw + sq),
         -2 * a * ((a - 1) + (a + 1) * cw),
         a * ((a + 1) + (a - 1) * cw - sq)]
    d = [(a + 1) - (a - 1) * cw + sq,
         2 * ((a - 1) - (a + 1) * cw),
         (a + 1) - (a - 1) * cw - sq]
    return np.array(b), np.array(d)


def _highpass(fc: float, q: float, sr: int):
    """RBJ-Hochpass — Stufe 2 der K-Bewertung."""
    w0 = 2.0 * math.pi * fc / sr
    cw, sw = math.cos(w0), math.sin(w0)
    alpha  = sw / (2.0 * q)
    b = [(1 + cw) / 2, -(1 + cw), (1 + cw) / 2]
    d = [1 + alpha, -2 * cw, 1 - alpha]
    return np.array(b), np.array(d)


def _biquad_response(b, a, freqs: np.ndarray, sr: int) -> np.ndarray:
    z = np.exp(-2j * np.pi * freqs / sr)
    num = b[0] + b[1] * z + b[2] * z * z
    den = a[0] + a[1] * z + a[2] * z * z
    return num / den


_KW_CACHE: dict[tuple[int, int], np.ndarray] = {}


def _kweight_power(n_fft: int, sr: int) -> np.ndarray:
    """|H(f)|² der K-Bewertung an den rfft-Stützstellen (gecacht)."""
    key = (n_fft, sr)
    resp = _KW_CACHE.get(key)
    if resp is None:
        freqs = np.fft.rfftfreq(n_fft, 1.0 / sr)
        h = (_biquad_response(*_highshelf(1681.974450955533, 0.7071752369554196,
                                          3.999843853973347, sr), freqs, sr)
             * _biquad_response(*_highpass(38.13547087602444,
                                           0.5003270373238773, sr), freqs, sr))
        resp = (np.abs(h) ** 2).astype(np.float32)
        _KW_CACHE[key] = resp
    return resp


def _block_energies(x: np.ndarray, block: int, sr: int) -> np.ndarray:
    """
    Mittleres Quadrat je Block und Kanal NACH K-Bewertung, über das Spektrum
    berechnet (Parseval). Ergebnis: Array der Form (n_blocks, CHANNELS).
    """
    n_blocks = len(x) // block
    if n_blocks == 0:
        return np.zeros((0, x.shape[1]), dtype=np.float64)

    # Sehr lange Titel gleichmäßig abtasten statt vollständig zu rechnen.
    idx = np.arange(n_blocks)
    if n_blocks > MAX_BLOCKS:
        idx = np.unique(np.linspace(0, n_blocks - 1, MAX_BLOCKS).astype(int))

    resp = _kweight_power(block, sr)
    out  = np.empty((len(idx), x.shape[1]), dtype=np.float64)
    # In Häppchen, damit der Speicherbedarf klein bleibt (Raspberry Pi):
    # nur die tatsächlich analysierten Blöcke werden nach float gewandelt.
    for lo in range(0, len(idx), 32):
        part   = idx[lo:lo + 32]
        starts = part * block
        seg    = np.stack([x[s:s + block] for s in starts])      # (k, block, ch)
        if seg.dtype == np.int16:
            seg = seg.astype(np.float32) / 32768.0
        spec   = np.fft.rfft(seg, axis=1)
        power  = (np.abs(spec) ** 2) * resp[None, :, None]
        # Parseval für rfft: doppelte Gewichtung der Innenbins
        total  = 2.0 * power.sum(axis=1) - power[:, 0, :]
        if block % 2 == 0:
            total -= power[:, -1, :]
        out[lo:lo + len(part)] = total / (block * block)
    return out


def integrated_loudness(x: np.ndarray, sr: int = SAMPLE_RATE) -> float:
    """
    Integrierte Lautheit in LUFS nach BS.1770 (K-Bewertung, absolutes und
    relatives Gate). Gibt -inf für Stille zurück.
    """
    block = int(sr * BLOCK_MS / 1000)
    e = _block_energies(x, block, sr)
    if not len(e):
        return float("-inf")

    # Kanalgewichte: L und R zählen je 1.0
    z = e.sum(axis=1)
    with np.errstate(divide="ignore"):
        loud = -0.691 + 10.0 * np.log10(np.maximum(z, 1e-30))

    keep = loud > ABSOLUTE_GATE
    if not keep.any():
        return float("-inf")
    mean_z = z[keep].mean()
    thresh = -0.691 + 10.0 * math.log10(max(mean_z, 1e-30)) + RELATIVE_GATE
    keep2  = keep & (loud > thresh)
    if not keep2.any():
        keep2 = keep
    return float(-0.691 + 10.0 * math.log10(max(z[keep2].mean(), 1e-30)))


# ── Hüllkurve und Ränder ─────────────────────────────────────────────────────

def _envelope_db(x: np.ndarray, sr: int = SAMPLE_RATE) -> np.ndarray:
    """
    RMS-Hüllkurve in dBFS, ein Wert je ENV_HOP_MS.

    Für die reine Stilleerkennung genügt der linke Kanal mit Schrittweite 2 —
    das ist viermal billiger und ändert am Ergebnis nichts.
    """
    hop = max(1, int(sr * ENV_HOP_MS / 1000))
    n   = (len(x) // hop) * hop
    if n == 0:
        return np.zeros(0, dtype=np.float32)
    mono = x[:n:2, 0].reshape(-1, hop // 2)
    if mono.dtype == np.int16:
        mono = mono.astype(np.float32) / 32768.0
    rms = np.sqrt(np.mean(mono * mono, axis=1))
    return 20.0 * np.log10(np.maximum(rms, 1e-7))


def _edges(env: np.ndarray) -> tuple[int, int]:
    """
    Erster und letzter Hüllkurvenindex mit echtem Signal.

    Gesucht wird ausschließlich von den Rändern nach innen — eine Pause
    mitten im Titel kann dadurch nie als Ende gewertet werden.
    """
    if not len(env):
        return 0, 0
    ref  = float(np.percentile(env, 95))       # robuster "lauter" Pegel
    thr  = ref - EDGE_DROP_DB
    loud = env > thr
    if not loud.any():
        return 0, len(env)

    hold = max(1, EDGE_HOLD_MS // ENV_HOP_MS)
    # Anfang: erste Stelle, ab der es *hold* Rahmen am Stück laut bleibt
    start = 0
    run   = 0
    for i, is_loud in enumerate(loud):
        run = run + 1 if is_loud else 0
        if run >= hold:
            start = i - run + 1
            break
    # Ende: letzte laute Stelle (rückwärts, gleiche Logik)
    end = len(env)
    run = 0
    for i in range(len(loud) - 1, -1, -1):
        run = run + 1 if loud[i] else 0
        if run >= hold:
            end = i + run
            break
    return start, end


def _level_db(x: np.ndarray) -> float:
    """Einfacher RMS-Pegel in dBFS (für Randbereiche der Überblendung)."""
    if not len(x):
        return -120.0
    f = x.astype(np.float32)
    if x.dtype == np.int16:
        f /= 32768.0
    rms = float(np.sqrt(np.mean(f * f)))
    return 20.0 * math.log10(max(rms, 1e-7))


_GAIN_CHUNK = 1 << 20      # Samples je Durchgang (~4 MB Zwischenpuffer)


def _gain_to_buffer(x: np.ndarray, gain_db: float) -> bytearray:
    """
    Verstärkung anwenden und direkt in den Ausgabepuffer schreiben.

    Häppchenweise und ohne Zwischenkopien: eine Float-Kopie des ganzen
    Titels wäre doppelt so groß wie das PCM selbst (8 Minuten ≈ 170 MB),
    und ein numpy-Array plus anschließendes ``tobytes()`` wäre eine
    komplette Kopie zu viel. Auf einem Raspberry Pi zählt beides.
    """
    factor = np.float32(10.0 ** (gain_db / 20.0)) if gain_db else np.float32(1.0)
    flat   = x.reshape(-1)
    buf    = bytearray(flat.nbytes)
    dst    = np.frombuffer(buf, dtype=np.int16)      # teilt sich den Speicher
    for lo in range(0, len(flat), _GAIN_CHUNK):
        seg = flat[lo:lo + _GAIN_CHUNK].astype(np.float32)
        if gain_db:
            np.multiply(seg, factor, out=seg)
            np.clip(seg, -32768.0, 32767.0, out=seg)
        dst[lo:lo + _GAIN_CHUNK] = seg.astype(np.int16)
    return buf


def _peak(x: np.ndarray) -> float:
    """Spitzenwert (0…1), häppchenweise ermittelt."""
    flat = x.reshape(-1)
    if not len(flat):
        return 0.0
    m = 0
    for lo in range(0, len(flat), _GAIN_CHUNK):
        m = max(m, int(np.max(np.abs(flat[lo:lo + _GAIN_CHUNK].astype(np.int32)))))
    return m / 32768.0


# ── Ergebnis ─────────────────────────────────────────────────────────────────

@dataclass
class SongAudio:
    pcm:        bytes | bytearray   # beschnitten und pegelangepasst
    lufs:       float    # gemessene Lautheit VOR der Anpassung
    gain_db:    float    # angewendete Verstärkung
    lead_trim:  float    # abgeschnittene Sekunden am Anfang
    tail_trim:  float    # abgeschnittene Sekunden am Ende
    body_db:    float    # RMS des Songkörpers (nach Anpassung)

    @property
    def duration(self) -> float:
        return len(self.pcm) / BYTES_PER_SEC


def analyze_and_process(
    pcm: bytes,
    *,
    trim_head: bool = True,
    known_lufs: float | None = None,
) -> SongAudio:
    """
    Song messen, Ränder beschneiden und auf die Ziel-Lautheit bringen.

    *trim_head* wird beim Fortsetzen eines pausierten Titels abgeschaltet —
    dort beginnt das PCM mitten im Song.
    *known_lufs* überspringt die Messung (z.B. aus dem Cache).
    """
    if not pcm:
        return SongAudio(pcm, float("-inf"), 0.0, 0.0, 0.0, -120.0)

    # int16-Sicht ohne Kopie — gewandelt wird nur, was gebraucht wird.
    usable = (len(pcm) // (2 * CHANNELS)) * 2 * CHANNELS
    x = np.frombuffer(pcm, dtype=np.int16, count=usable // 2).reshape(-1, CHANNELS)
    if not len(x):
        return SongAudio(pcm, float("-inf"), 0.0, 0.0, 0.0, -120.0)

    # ── Ränder ───────────────────────────────────────────────────────────
    lead = tail = 0.0
    if trim_enabled():
        env = _envelope_db(x)
        s, e = _edges(env)
        hop_s = ENV_HOP_MS / 1000.0
        guard = EDGE_GUARD_MS / 1000.0
        lead  = max(0.0, s * hop_s - guard) if trim_head else 0.0
        tail  = max(0.0, (len(env) - e) * hop_s - guard)
        lead  = min(lead, MAX_TRIM_SEC)
        tail  = min(tail, MAX_TRIM_SEC)
        i0 = int(lead * SAMPLE_RATE)
        i1 = len(x) - int(tail * SAMPLE_RATE)
        if i1 - i0 > SAMPLE_RATE:          # nie unter 1 s zusammenschneiden
            x = x[i0:i1]
        else:
            lead = tail = 0.0

    # ── Lautheit ─────────────────────────────────────────────────────────
    lufs = known_lufs if known_lufs is not None else integrated_loudness(x)
    gain_db = 0.0
    if normalize_enabled() and math.isfinite(lufs):
        gain_db = max(-max_gain_db(), min(max_gain_db(), target_lufs() - lufs))
        # Spitzenwertreserve: lieber etwas leiser als übersteuert
        peak = _peak(x)
        if peak > 0:
            headroom = 20.0 * math.log10(0.891 / peak)      # Ziel: -1 dBFS
            gain_db  = min(gain_db, max(headroom, 0.0)) if gain_db > 0 else gain_db
        if abs(gain_db) < 0.1:
            gain_db = 0.0

    if gain_db or lead or tail:
        out = _gain_to_buffer(x, gain_db)
        x   = np.frombuffer(out, dtype=np.int16).reshape(-1, CHANNELS)
    else:
        out = pcm

    body_db = _level_db(x[::16])          # Stichprobe genügt als Referenz
    return SongAudio(out, lufs, gain_db, lead, tail, body_db)


def body_level_db(pcm: bytes, max_sec: float = 20.0) -> float:
    """
    Pegel des Songkörpers als Referenz für die Überblendlänge.

    Abgetastet wird das Mittelstück: die Ränder sind genau das, was gegen
    diesen Wert verglichen werden soll.
    """
    if not pcm:
        return -120.0
    span = int(max_sec * BYTES_PER_SEC)
    if len(pcm) > span:
        start = ((len(pcm) - span) // 2 // 4) * 4
        pcm = pcm[start:start + span]
    return _level_db(pcm_to_float(pcm))


# ── Adaptive Überblendlänge ──────────────────────────────────────────────────

def blend_seconds(
    tail_pcm: bytes,
    head_pcm: bytes,
    out_body_db: float,
    in_body_db: float,
    max_sec: float,
) -> float:
    """
    Wie lange soll überblendet werden?

    Ist der Ausklang des laufenden oder der Anfang des nächsten Titels
    deutlich leiser als der jeweilige Songkörper (sanftes Intro, langer
    Ausklang), entstünde bei voller Überblendlänge ein hörbares Loch —
    beide Titel wären dort gleichzeitig leise. Dann wird kürzer überblendet,
    der leise Teil bleibt aber vollständig erhalten.
    """
    if max_sec <= 0 or not tail_pcm or not head_pcm:
        return 0.0
    n = int(max_sec * BYTES_PER_SEC)
    out_edge = _level_db(pcm_to_float(tail_pcm[-n:]))
    in_edge  = _level_db(pcm_to_float(head_pcm[:n]))
    drop = max(out_body_db - out_edge, in_body_db - in_edge)

    if drop <= 6.0:
        return max_sec
    if drop <= 12.0:
        return min(max_sec, 3.0)
    if drop <= 20.0:
        return min(max_sec, 1.5)
    return min(max_sec, 0.6)


def equal_power_curves(n_frames: int) -> tuple[np.ndarray, np.ndarray]:
    """
    Aus- und Einblendkurve mit konstanter Leistung.

    Eine lineare Überblendung senkt bei unkorreliertem Material die
    wahrgenommene Lautstärke in der Mitte um ~3 dB — hörbar als kurzes
    Absacken. sin/cos hält die Summenleistung konstant.
    """
    t  = np.linspace(0.0, 1.0, n_frames, dtype=np.float32)
    fi = np.sin(t * (np.pi / 2.0))
    fo = np.cos(t * (np.pi / 2.0))
    return fo, fi
