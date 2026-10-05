"""Sound files for CS2: .wav / .mp3 at 44100 Hz.

Files that already use 44100 Hz are copied as they are. Other sample rates (and compressed
.wav files such as ADPCM) are decoded with the audio codecs built into Windows, resampled
to 44100 Hz and written as 16-bit .wav. Loop markers (cue points) of a .wav are kept.
"""

import ctypes
import io
import struct
import sys
import wave

import numpy as np

TARGET_RATE = 44100

# --- mp3 frame header ------------------------------------------------------------------

_MP3_RATES = {3: (44100, 48000, 32000), 2: (22050, 24000, 16000), 0: (11025, 12000, 8000)}
_MP3_KBPS_V1 = (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320)
_MP3_KBPS_V2 = (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160)


def _skip_id3(data):
    if data[:3] == b"ID3" and len(data) > 10:
        size = 0
        for b in data[6:10]:
            size = (size << 7) | (b & 0x7F)
        return 10 + size + (10 if data[5] & 0x10 else 0)
    return 0


def mp3_info(data):
    """(sample_rate, channels, kbps, version) of the first frame, or None."""
    i = _skip_id3(data)
    n = len(data) - 4
    while i < n:
        if data[i] == 0xFF and (data[i + 1] & 0xE0) == 0xE0:
            h = struct.unpack(">I", data[i:i + 4])[0]
            ver = (h >> 19) & 3
            layer = (h >> 17) & 3
            br = (h >> 12) & 15
            sr = (h >> 10) & 3
            if ver != 1 and layer == 1 and 0 < br < 15 and sr < 3:
                kbps = (_MP3_KBPS_V1 if ver == 3 else _MP3_KBPS_V2)[br]
                mode = (h >> 6) & 3
                return _MP3_RATES[ver][sr], 1 if mode == 3 else 2, kbps, ver
        i += 1
    return None


# --- wav chunks ----------------------------------------------------------------------------

def wav_chunks(data):
    """{chunk id: bytes} of a RIFF/WAVE file (first of each id)."""
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError("not a wav file")
    out = {}
    pos = 12
    while pos + 8 <= len(data):
        cid, size = struct.unpack("<4sI", data[pos:pos + 8])
        out.setdefault(cid, data[pos + 8:pos + 8 + size])
        pos += 8 + size + (size & 1)
    return out


def wav_rate(data):
    fmt = wav_chunks(data).get(b"fmt ")
    if not fmt or len(fmt) < 16:
        raise ValueError("wav without fmt chunk")
    tag, ch, rate = struct.unpack("<HHI", fmt[:8])
    return tag, ch, rate


# --- Windows audio codecs (ACM) -----------------------------------------------------------

class _ACMSTREAMHEADER(ctypes.Structure):
    _fields_ = [("cbStruct", ctypes.c_uint32), ("fdwStatus", ctypes.c_uint32),
                ("dwUser", ctypes.c_size_t), ("pbSrc", ctypes.c_void_p),
                ("cbSrcLength", ctypes.c_uint32), ("cbSrcLengthUsed", ctypes.c_uint32),
                ("dwSrcUser", ctypes.c_size_t), ("pbDst", ctypes.c_void_p),
                ("cbDstLength", ctypes.c_uint32), ("cbDstLengthUsed", ctypes.c_uint32),
                ("dwDstUser", ctypes.c_size_t),
                ("dwReservedDriver", ctypes.c_uint32 * (15 if sys.maxsize > 2 ** 32 else 10))]


_SUGGEST_WFORMATTAG = 0x00010000
_OPEN_NONREALTIME = 0x4
_CONVERT_BLOCKALIGN = 0x4
_CONVERT_START = 0x10
_CONVERT_END = 0x20


def _acm():
    if sys.platform != "win32":
        raise OSError("audio decoding needs Windows")
    return ctypes.windll.msacm32


def acm_decode(src_fmt, payload):
    """Decodes compressed audio (src_fmt = WAVEFORMATEX bytes) to 16-bit PCM.
    Returns (pcm bytes, channels, sample rate)."""
    acm = _acm()
    src = ctypes.create_string_buffer(bytes(src_fmt), max(len(src_fmt), 18))
    dst = ctypes.create_string_buffer(struct.pack("<HHIIHHH", 1, 0, 0, 0, 0, 16, 0), 18)
    r = acm.acmFormatSuggest(None, src, dst, 18, _SUGGEST_WFORMATTAG)
    if r:
        raise OSError(f"no decoder for this audio format ({r})")
    has = ctypes.c_void_p()
    r = acm.acmStreamOpen(ctypes.byref(has), None, src, dst, None, None, None, _OPEN_NONREALTIME)
    if r:
        raise OSError(f"decoder could not be opened ({r})")
    _tag, ch, rate = struct.unpack("<HHI", dst.raw[:8])
    out = bytearray()
    try:
        pos, first, chunk = 0, True, 1 << 16
        stalls = 0
        while pos < len(payload):
            piece = payload[pos:pos + chunk]
            last = pos + len(piece) >= len(payload)
            flags = (_CONVERT_START if first else 0) | (_CONVERT_END if last else _CONVERT_BLOCKALIGN)
            dst_len = ctypes.c_uint32()
            if acm.acmStreamSize(has, len(piece), ctypes.byref(dst_len), 0) or not dst_len.value:
                dst_len.value = len(piece) * 16 + 65536
            sbuf = ctypes.create_string_buffer(piece, len(piece))
            dbuf = ctypes.create_string_buffer(dst_len.value)
            hdr = _ACMSTREAMHEADER()
            hdr.cbStruct = ctypes.sizeof(hdr)
            hdr.pbSrc = ctypes.cast(sbuf, ctypes.c_void_p)
            hdr.cbSrcLength = len(piece)
            hdr.pbDst = ctypes.cast(dbuf, ctypes.c_void_p)
            hdr.cbDstLength = dst_len.value
            if acm.acmStreamPrepareHeader(has, ctypes.byref(hdr), 0):
                raise OSError("decoder error")
            r = acm.acmStreamConvert(has, ctypes.byref(hdr), flags)
            acm.acmStreamUnprepareHeader(has, ctypes.byref(hdr), 0)
            if r:
                raise OSError(f"decoder error ({r})")
            out += dbuf.raw[:hdr.cbDstLengthUsed]
            used = hdr.cbSrcLengthUsed
            if used == 0:
                stalls += 1
                if last or stalls > 2:
                    break
                chunk *= 2
                continue
            stalls = 0
            pos += used
            first = False
    finally:
        acm.acmStreamClose(has, 0)
    return bytes(out), ch, rate


def _mp3_format(rate, ch, kbps, ver):
    samples = 1152 if ver == 3 else 576
    block = samples * kbps * 125 // rate
    # MPEGLAYER3WAVEFORMAT: WAVEFORMATEX + wID, fdwFlags, nBlockSize, nFramesPerBlock, nCodecDelay
    return struct.pack("<HHIIHHHHIHHH", 0x55, ch, rate, kbps * 125, 1, 0, 12, 1, 2, block, 1, 1393)


# --- resampling ------------------------------------------------------------------------------

def resample(samples, src_rate, dst_rate=TARGET_RATE):
    """samples: (n, channels) float array. Band-limited (FFT) resampling."""
    n = samples.shape[0]
    if n == 0 or src_rate == dst_rate:
        return samples
    m = int(round(n * dst_rate / src_rate))
    out = np.empty((m, samples.shape[1]), dtype=np.float32)
    for c in range(samples.shape[1]):
        spec = np.fft.rfft(samples[:, c])
        keep = min(len(spec), m // 2 + 1)
        new = np.zeros(m // 2 + 1, dtype=spec.dtype)
        new[:keep] = spec[:keep]
        out[:, c] = np.fft.irfft(new, m) * (m / n)
    return out


def _pcm_to_float(pcm, ch, width):
    if width == 1:
        a = (np.frombuffer(pcm, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif width == 2:
        a = np.frombuffer(pcm[:len(pcm) // 2 * 2], dtype="<i2").astype(np.float32) / 32768.0
    elif width == 3:
        b = np.frombuffer(pcm[:len(pcm) // 3 * 3], dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        a = ((b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)) << 8 >> 8).astype(np.float32) / 8388608.0
    elif width == 4:
        a = np.frombuffer(pcm[:len(pcm) // 4 * 4], dtype="<i4").astype(np.float32) / 2147483648.0
    else:
        raise ValueError(f"{width * 8}-bit audio is not supported")
    return a[:len(a) // ch * ch].reshape(-1, ch)


def _write_wav(samples, rate, cue=None):
    data = np.clip(np.round(samples * 32767.0), -32768, 32767).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(samples.shape[1])
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(data.tobytes())
    out = buf.getvalue()
    if cue:
        out += b"cue " + struct.pack("<I", len(cue)) + cue + (b"\0" if len(cue) & 1 else b"")
        out = out[:4] + struct.pack("<I", len(out) - 8) + out[8:]
    return out


def _scale_cue(cue, ratio):
    """Moves the cue points (loop markers) to the new sample rate."""
    if not cue or len(cue) < 4:
        return None
    count = struct.unpack("<I", cue[:4])[0]
    out = bytearray(cue[:4])
    for i in range(count):
        rec = cue[4 + i * 24:4 + (i + 1) * 24]
        if len(rec) < 24:
            return None
        cid, pos, chunk, cstart, bstart, off = struct.unpack("<II4sIII", rec)
        out += struct.pack("<II4sIII", cid, int(round(pos * ratio)), chunk, cstart, bstart,
                           int(round(off * ratio)))
    return bytes(out)


# --- entry point ---------------------------------------------------------------------------

def to_cs2(data, ext):
    """Returns (bytes, extension, note). note: "" when copied, else a short description."""
    ext = ext.lower()
    if ext == ".mp3":
        info = mp3_info(data)
        if info is None:
            raise ValueError("no mp3 frame found")
        rate, ch, kbps, ver = info
        if rate == TARGET_RATE:
            return data, ".mp3", ""
        pcm, dch, drate = acm_decode(_mp3_format(rate, ch, kbps, ver), data[_skip_id3(data):])
        samples = resample(_pcm_to_float(pcm, dch, 2), drate)
        return _write_wav(samples, TARGET_RATE), ".wav", f"{rate} Hz -> {TARGET_RATE} Hz"
    if ext == ".wav":
        chunks = wav_chunks(data)
        fmt = chunks.get(b"fmt ")
        if not fmt or len(fmt) < 16:
            raise ValueError("wav without fmt chunk")
        tag, ch, rate, _avg, _align, bits = struct.unpack("<HHIIHH", fmt[:16])
        if tag == 0xFFFE and len(fmt) >= 26:
            tag = struct.unpack("<H", fmt[24:26])[0]
        if rate == TARGET_RATE and tag == 1:
            return data, ".wav", ""
        payload = chunks.get(b"data", b"")
        if tag == 1:
            samples, src_rate = _pcm_to_float(payload, ch, bits // 8), rate
        elif tag == 3 and bits == 32:
            samples, src_rate = np.frombuffer(payload[:len(payload) // 4 * 4], dtype="<f4").reshape(-1, ch), rate
        else:
            src_fmt = fmt if len(fmt) >= 18 else fmt + b"\0\0"
            pcm, dch, src_rate = acm_decode(src_fmt, payload)
            samples = _pcm_to_float(pcm, dch, 2)
        out = resample(samples, src_rate)
        note = f"{rate} Hz -> {TARGET_RATE} Hz" if rate != TARGET_RATE else "PCM"
        return _write_wav(out, TARGET_RATE, _scale_cue(chunks.get(b"cue "), TARGET_RATE / src_rate)), ".wav", note
    raise ValueError(f"unsupported sound format {ext}")
