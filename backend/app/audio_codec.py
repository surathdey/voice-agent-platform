"""Audio codec helpers for the browser test harness (A11).

The self-hosted pipeline speaks exactly one audio format end to end:
G.711 μ-law at 8 kHz — that is what Plivo streams, what the Saaras STT
session is fed (app.stt), and what Bulbul TTS audio is pumped as
(app.conversation). A browser speaks none of it: the microphone path
delivers float PCM at the AudioContext rate, and Web Audio playback
wants PCM. This module is the honest conversion layer between the two
worlds, used ONLY by the browser transport (routers/browser_test.py):

- INBOUND (operator → agent): the browser client captures microphone
  audio and ships **PCM16 little-endian at 16 kHz** over the harness
  WebSocket. `pcm16_16k_to_mulaw8k()` halves that to 8 kHz and μ-law
  encodes it, producing exactly the 160-byte / 20 ms frames the Plivo
  path feeds Saaras — so the STT leg, the conversation engine and the
  classifier run completely unchanged.
- OUTBOUND (agent → operator): each μ-law frame the engine pumps is
  decoded back to PCM16 at 8 kHz (`mulaw_to_pcm16`) and sent as a
  binary WebSocket message; the browser schedules the PCM into Web
  Audio, which resamples to the output device's rate itself.

The conversions are deliberately simple, and their limits are stated,
not hidden:

- The 16 kHz → 8 kHz step is pair-averaging decimation (each output
  sample is the mean of two input samples). That is a crude anti-alias
  filter — entirely adequate for speech-band STT, not a studio
  polyphase resampler. The browser side already band-limits the
  capture by averaging its own downsample to 16 kHz.
- μ-law encode/decode is the standard G.711 algorithm (the same one
  `audioop` implements; hand-rolled here because audioop is removed
  from the stdlib in Python 3.13). μ-law is lossy by design — it is
  the format telephony itself runs on, so a browser test hears what a
  phone callee hears, quantisation included.
"""
import struct

_BIAS = 0x84          # G.711 μ-law bias
_CLIP = 32635         # maximum magnitude before clipping


def _encode_sample(sample: int) -> int:
    sign = 0x80 if sample < 0 else 0x00
    if sample < 0:
        sample = -sample
    if sample > _CLIP:
        sample = _CLIP
    sample += _BIAS
    exponent = 7
    mask = 0x4000
    while exponent > 0 and not (sample & mask):
        exponent -= 1
        mask >>= 1
    mantissa = (sample >> (exponent + 3)) & 0x0F
    return (~(sign | (exponent << 4) | mantissa)) & 0xFF


def _decode_sample(byte: int) -> int:
    b = (~byte) & 0xFF
    sign = b & 0x80
    exponent = (b >> 4) & 0x07
    mantissa = b & 0x0F
    sample = (((mantissa << 3) + _BIAS) << exponent) - _BIAS
    return -sample if sign else sample


_DECODE_TABLE = [_decode_sample(b) for b in range(256)]


def mulaw_encode(pcm16: bytes) -> bytes:
    """PCM16 little-endian (any sample rate) → μ-law bytes."""
    n = len(pcm16) // 2
    samples = struct.unpack(f"<{n}h", pcm16[: n * 2])
    return bytes(_encode_sample(int(s)) for s in samples)


def mulaw_to_pcm16(mulaw: bytes) -> bytes:
    """μ-law bytes → PCM16 little-endian at the same sample rate."""
    return struct.pack(f"<{len(mulaw)}h",
                       *(_DECODE_TABLE[b] for b in mulaw))


def downsample_16k_to_8k(pcm16: bytes) -> bytes:
    """PCM16 at 16 kHz → PCM16 at 8 kHz by pair-averaging (see module
    doc for the honest limits of this filter). A trailing odd sample
    is carried by the caller; here it is simply left unpaired."""
    n = len(pcm16) // 2
    samples = struct.unpack(f"<{n}h", pcm16[: n * 2])
    out = []
    for i in range(0, n - 1, 2):
        out.append((samples[i] + samples[i + 1]) // 2)
    return struct.pack(f"<{len(out)}h", *out)


def pcm16_16k_to_mulaw8k(pcm16: bytes) -> bytes:
    """One inbound conversion step for the harness: PCM16 @16 kHz in,
    μ-law @8 kHz out — the exact frames the Saaras session is fed on
    the Plivo path. Input should be a whole number of 20 ms chunks
    (640 bytes → 160 bytes out); partial chunks convert proportionally."""
    return mulaw_encode(downsample_16k_to_8k(pcm16))
