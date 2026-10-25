"""Renders Z2R library songs to audio.

Songs are FamiTracker modules in the format of the bhop NES driver (github.com/TheRealQuantam/bhop).
This is a Python port of the parts of that driver that matter for sound, plus a small 2A03
synth (two pulse channels, triangle, noise; DPCM is not played). Pure Python, no dependencies.
"""
import array
import base64
import io
import sys
import wave
import zlib

_PERIOD_HI = bytes.fromhex(
    "0707070707070707070707070706060505050504040403030303030202020202"
    "0202010101010101010101010101000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000")
_PERIOD_LO = bytes.fromhex(
    "fffffffffffffffffffff17f13ad4df39d4c00b87434f8bf895626f9cea6805c"
    "3a1afbdfc4ab937c67523f2d1c0cfdefe1d5c9bdb3a99f968e867e77706a645e"
    "59544f4b46423f3b3834312f2c29272523211f1d1b1a18171514131211100f0e"
    "0d0c0c0b0a0a0908080707060606050505040404030303030302020202020201")
VIBRATO = bytes.fromhex(
    "0000000000000000000000000000000000000000000000000101010101010101"
    "0000000000010101010102020202020200000001010102020203030303030303"
    "0000000101020203030304040404040400000102020303040405050606060606"
    "0000010203040506070708080909090900010203040506070809090a0b0b0b0b"
    "0001020405060708090a0b0c0c0d0d0d000103040608090a0c0d0e0e0f101010"
    "00020406080a0c0d0f11121314151515000205080b0e10131517181a1b1c1d1d"
    "0004080c1014181b1f222426282a2b2b00060c12181e23282d3135383b3d3e3f"
    "0009121b242d353c434a4f54585b5e5f000c1825303c47515a626a70767a7d7f")

PERIODS = [(h << 8) | l for h, l in zip(_PERIOD_HI, _PERIOD_LO)]
VOLUME = [0 if not (i >> 4) or not (i & 15) else max(1, (i >> 4) * (i & 15) // 15) for i in range(256)]
NOISE_PERIODS = [4, 8, 16, 32, 64, 96, 128, 160, 202, 254, 380, 508, 762, 1016, 2034, 4068]
TRIANGLE = [15 - i for i in range(16)] + list(range(16))
DUTY = [0.125, 0.25, 0.5, 0.75]
CPU = 1789773.0
FRAME_RATE = 60.0988
RATE = 22050
MAX_SECONDS = 600

# Status bits
MUTED, GLOBAL_DUR, TRIGGERED, RELEASED, FRESH_CUT = 0x80, 0x40, 0x20, 0x10, 0x04
# Pitch-effect values (the order of UP..NDOWN is the order the driver checks them)
P_UP, P_DOWN, P_PORTA, P_NUP, P_NDOWN, P_ARP, P_TRIG = 1, 2, 4, 8, 16, 32, 64
NO_PARAM = {0x01, 0x03, 0x0A, 0x12}  # commands with no argument byte


class FtError(ValueError):
    pass


def decode_data(text):
    """A library's `data:` string -> module bytes."""
    kind, _, body = text.partition(":")
    if kind != "deflate":
        raise FtError(f"unsupported data encoding {kind!r}")
    return zlib.decompress(base64.b64decode(body), -15)


def s8(v):
    return v - 256 if v > 127 else v


class Module:
    """FamiTracker module as exported for the bhop driver (addresses are relative to the blob)."""

    def __init__(self, data):
        self.d = d = bytes(data) + bytes(8)
        n = len(data)

        def w(a):
            if a + 1 >= n:
                raise FtError("module data is truncated")
            return d[a] | d[a + 1] << 8

        self.w = w
        song_list, instr_list, sample_map, _samples, self.grooves = (w(i) for i in (0, 2, 4, 6, 8))
        self.instruments = []
        if instr_list != sample_map:
            for i in range((w(instr_list) - instr_list) // 2):
                a = w(instr_list + 2 * i)
                mask, p, seqs = d[a + 1], a + 2, []
                for bit in range(5):
                    seqs.append(w(p) if mask >> bit & 1 else None)
                    p += 2 if mask >> bit & 1 else 0
                self.instruments.append((mask, seqs))
        self.songs = []
        if song_list != instr_list:
            for i in range((w(song_list) - song_list) // 2):
                a = w(song_list + 2 * i)
                fl, nframes = w(a), d[a + 2]
                frames = []
                for f in range(nframes):
                    fa = w(fl + 2 * f)
                    frames.append([w(fa + 2 * c) for c in range(4)])
                self.songs.append({"frames": frames, "rows": d[a + 3], "speed": d[a + 4], "tempo": d[a + 5],
                                   "groove": d[a + 6]})
        if not self.songs:
            raise FtError("module has no songs")


class Chan:
    def __init__(self, index):
        self.index = index
        self.ptr = 0
        self.delay = 0
        self.status = MUTED
        self.gdur = 0
        self.volume = 15
        self.note = 0
        self.base = self.rel = self.det = 0
        self.mask = 0
        self.seqs = [None] * 5
        self.active = 0
        self.idx = [0] * 5
        self.ivol = 15
        self.iduty = 0
        self.duty = 0
        self.pe = 0
        self.pe_set = 0
        self.arp_set = self.arp_ctr = 0
        self.rel_off = 0
        self.vib = self.vib_acc = 0
        self.trem = self.trem_acc = 0
        self.tvol = 15
        self.vsl = self.vsl_acc = 0
        self.tuning = 0
        self.note_delay = self.cut_delay = 0
        self.phase = 0.0
        self.lfsr = 1
        self.noise_acc = 0.0
        self.noise_out = 0


class Renderer:
    def __init__(self, module, song_index=0):
        self.m, self.d = module, module.d
        self.song = module.songs[min(song_index, len(module.songs) - 1)]
        self.ch = [Chan(i) for i in range(4)]
        self.frame_n = 0
        self.nframes = len(self.song["frames"])
        self.rows = self.song["rows"]
        self.tempo = self.song["tempo"]
        self.cmp = 0
        self.groove = self.gpos = self.song["groove"]
        if self.song["speed"]:
            self.set_speed(self.song["speed"])
        self.update_groove()
        self.gpos = self.groove
        self.counter = self.cmp  # first row plays immediately
        self.row_n = 0
        self.skip = 0
        self.pending_jump = None
        self.halted = False
        self.loop_target = None  # set when the song loops back
        self.load_frame()

    # -- sequencing
    def set_speed(self, n):
        self.cmp = n * 150

    def update_groove(self):
        if not self.groove:
            return
        d = self.d
        b = d[self.m.grooves + self.gpos]
        if not b:
            b = d[self.m.grooves + self.groove]
            self.gpos = self.groove
        self.gpos += 1
        self.set_speed(b)

    def load_frame(self):
        for c, ptr in zip(self.ch, self.song["frames"][self.frame_n]):
            c.ptr, c.delay = ptr, 0

    def advance_frame(self):
        if self.pending_jump is not None:
            target, self.pending_jump = self.pending_jump, None
            if target <= self.frame_n:
                self.loop_target = target
            self.frame_n = target
            if self.frame_n >= self.nframes:
                self.frame_n = 0
        else:
            self.frame_n += 1
            if self.frame_n >= self.nframes:
                self.frame_n = 0
                self.loop_target = 0
        self.load_frame()

    def tick_rows(self):
        if not self.tempo:
            self.halted = True
            return
        self.counter += self.tempo
        if self.counter < self.cmp:
            return
        if self.skip:
            self.advance_frame()
            self.row_n = 0
            self.skip -= 1
            while self.skip:
                for c in self.ch:
                    self.read_row(c, True)
                self.row_n += 1
                self.skip -= 1
        elif self.row_n >= self.rows:
            self.advance_frame()
            self.row_n = 0
        self.counter -= self.cmp
        for c in self.ch:
            self.read_row(c)
        self.fix_noise()
        self.update_groove()
        self.row_n += 1

    def fix_noise(self):
        n = self.ch[3]
        if n.status & TRIGGERED:
            n.base = n.rel = n.note

    def set_instrument(self, c, i):
        c.mask, c.seqs = self.m.instruments[i] if i < len(self.m.instruments) else (0, [None] * 5)
        self.reset_instrument(c)

    def reset_instrument(self, c):
        c.idx = [0] * 5
        c.active = c.mask

    def read_row(self, c, skip=False):
        if c.delay:
            c.delay -= 1
            return
        d, p = self.d, c.ptr
        while True:
            if p >= len(d) - 8:
                c.delay = 255
                break
            b = d[p]
            p += 1
            if b & 0x80:
                hi = b & 0xF0
                if hi == 0xF0:
                    if not skip:
                        c.volume = b & 15
                    continue
                if hi == 0xE0:
                    if not skip:
                        self.set_instrument(c, b & 15)
                    continue
                cmd = b & 0x7F
                arg = 0
                if cmd not in NO_PARAM:
                    arg = d[p]
                    p += 1
                if cmd == 0x02:
                    c.gdur, c.status = arg, c.status | GLOBAL_DUR
                elif cmd == 0x03:
                    c.status &= ~GLOBAL_DUR
                elif not skip:
                    self.command(c, cmd, arg)
                    if c.note_delay:
                        c.ptr = p
                        return
                continue
            if not skip:
                self.note(c, b)
            if c.status & GLOBAL_DUR:
                c.delay = c.gdur
            else:
                c.delay = d[p]
                p += 1
            break
        c.ptr = p

    def command(self, c, cmd, arg):
        if cmd == 0x00:
            self.set_instrument(c, arg >> 1)
        elif cmd == 0x04:
            self.set_speed(arg or 1)
        elif cmd == 0x05:
            self.tempo = arg
        elif cmd == 0x06:
            self.pending_jump = (arg - 1) & 0xFF
            self.row_n = self.rows - 1
            for o in self.ch:
                o.status &= ~GLOBAL_DUR
        elif cmd == 0x07:
            self.skip = arg
        elif cmd == 0x08:
            self.tempo = 0
            for o in self.ch:
                o.status |= MUTED
        elif cmd == 0x0A:
            c.pe = 0
        elif cmd in (0x0B, 0x0C, 0x0D):
            c.pe_set, c.pe = arg, {0x0B: P_UP, 0x0C: P_DOWN, 0x0D: P_PORTA}[cmd]
        elif cmd == 0x0E:
            c.arp_set, c.arp_ctr, c.pe = arg, 0, P_ARP
        elif cmd == 0x0F:
            c.vib = arg
            if not arg:
                c.vib_acc = 0
        elif cmd == 0x10:
            c.trem = arg
            if not arg:
                c.trem_acc = 0
        elif cmd == 0x11:
            c.tuning = 0x80 - arg
        elif cmd == 0x12:
            c.tuning = 0
        elif cmd == 0x13:
            c.duty = arg
        elif cmd == 0x14:
            c.note_delay = (arg & 0x7F) + 1
        elif cmd in (0x18, 0x19):
            c.pe_set, c.pe = arg, (P_NUP if cmd == 0x18 else P_NDOWN) | P_TRIG
        elif cmd == 0x1A:
            c.vsl, c.vsl_acc = arg, 0
        elif cmd == 0x1B:
            c.cut_delay, c.status = arg + 1, c.status | FRESH_CUT
        elif cmd == 0x20:
            self.groove = self.gpos = arg
            self.update_groove()

    def note(self, c, b):
        if b == 0:
            return
        if b == 0x7F:
            c.status |= MUTED
            c.cut_delay = 0
            return
        if b == 0x7E:
            c.status |= RELEASED
            for k in (0, 2, 1, 4):
                if c.active >> k & 1 and self.d[c.seqs[k] + 2]:
                    c.idx[k] = self.d[c.seqs[k] + 2]
            return
        c.note = b
        c.base = PERIODS[b]
        if c.status & MUTED or not c.pe & P_PORTA:
            c.rel = c.base
        if not c.status & FRESH_CUT:
            c.cut_delay = 0
        c.status = (c.status | TRIGGERED) & ~(MUTED | RELEASED)
        self.reset_instrument(c)
        c.ivol = 15
        c.iduty = c.duty

    # -- per-frame envelopes and effects
    def seq_next(self, a, x):
        d = self.d
        x += 1
        length, loop, rel = d[a], d[a + 1], d[a + 2]
        if x == length:
            return x if loop == 0xFF or loop < rel else loop
        if x == rel:
            return x - 1 if loop == 0xFF or loop >= rel else loop
        return x

    def tick_value_seq(self, c, k):
        if not c.active >> k & 1:
            return None
        a, d = c.seqs[k], self.d
        v = d[a + 4 + c.idx[k]]
        x = self.seq_next(a, c.idx[k])
        if x == d[a]:
            c.active &= ~(1 << k)
        else:
            c.idx[k] = x
        return v

    def tick_arp_seq(self, c):
        if not c.active & 2:
            return
        a, d = c.seqs[1], self.d
        mode = d[a + 3]
        if c.idx[1] == d[a]:
            c.active &= ~2
            if mode == 1:
                c.rel = PERIODS[c.note]
            return
        v = s8(d[a + 4 + c.idx[1]])
        if c.index == 3:
            if mode == 0:
                val = c.note + v
            elif mode == 2:
                c.note += v
                val = c.note
            else:
                val = v + 1
            c.rel = val & 0xFF
        else:
            if mode == 0:
                n = c.note + v
            elif mode == 2:
                if c.status & TRIGGERED:
                    c.rel_off = 0
                c.rel_off += v
                n = c.note + c.rel_off
            else:
                n = v + 1
            c.rel = PERIODS[max(0, min(127, n))]
        c.idx[1] = self.seq_next(a, c.idx[1])

    def tick_pitch_seq(self, c):
        if not c.active & 4:
            return
        a, d = c.seqs[2], self.d
        v = s8(d[a + 4 + c.idx[2]])
        if d[a + 3] != 0 and not c.active & 2:
            c.rel = c.base
        c.rel = (c.rel + v) & 0xFFFF
        x = self.seq_next(a, c.idx[2])
        if x == d[a]:
            c.active &= ~4
        else:
            c.idx[2] = x

    def effect_arp(self, c):
        if not c.pe & P_ARP:
            return
        step = (0, c.arp_set >> 4, c.arp_set & 15)[c.arp_ctr]
        c.rel = PERIODS[min(127, c.note + step)]
        c.arp_ctr = (c.arp_ctr + 1) % 3

    def effect_pitch(self, c):
        pe = c.pe & ~P_ARP
        if not pe:
            return
        if pe & P_UP:
            c.rel = (c.rel - c.pe_set) & 0xFFFF
        elif pe & P_DOWN:
            c.rel = (c.rel + c.pe_set) & 0xFFFF
        elif pe & P_PORTA:
            self.portamento(c)
        elif pe & (P_NUP | P_NDOWN):
            if pe & P_TRIG:
                semis = c.pe_set & 15
                c.note += semis if pe & P_NUP else -semis
                c.note = max(0, min(127, c.note))
                c.pe_set = ((c.pe_set >> 3) | 1) & 0xFF
                c.pe &= ~P_TRIG
            elif c.status & TRIGGERED:
                c.pe = 0
                return
            c.base = PERIODS[c.note]
            self.portamento(c)

    def portamento(self, c):
        target = PERIODS[max(0, min(127, c.note))]
        if c.rel > target:
            c.rel = max(target, c.rel - c.pe_set)
        elif c.rel < target:
            c.rel = min(target, c.rel + c.pe_set)

    def effect_volume(self, c):
        if c.trem:
            c.trem_acc = (c.trem_acc + (c.trem & 15)) & 0xFF
            i = c.trem_acc >> 1
            i = (~i & 15) if i & 16 else i & 15
            v = VIBRATO[(c.trem & 0xF0) | i] >> 1
            c.tvol = c.volume - v if c.volume - v >= 0 else 1
        else:
            c.tvol = c.volume
        if c.vsl:
            c.vsl_acc = (c.vsl_acc + (c.vsl >> 4)) & 0xFF
            t = (c.vsl_acc - (c.vsl & 15)) & 0xFF
            c.vsl_acc = t & 7
            c.volume = max(0, min(15, c.volume + (s8(t) >> 3)))

    def tick_channel(self, c):
        # delayed note / delayed cut
        if c.note_delay:
            c.note_delay -= 1
            if not c.note_delay:
                self.read_row(c)
                if c.index == 3:
                    self.fix_noise()
        if c.cut_delay:
            c.status &= ~FRESH_CUT
            c.cut_delay -= 1
            if not c.cut_delay:
                c.status |= MUTED
        i = c.index
        if i == 3:
            self.effect_volume(c)
            v = self.tick_value_seq(c, 0)
            if v is not None:
                c.ivol = v
            v = self.tick_value_seq(c, 4)
            if v is not None:
                c.iduty = v
            self.tick_arp_seq(c)
            c.det = c.rel
            return
        v = self.tick_value_seq(c, 0)
        if v is not None:
            c.ivol = v
        if i < 2:
            v = self.tick_value_seq(c, 4)
            if v is not None:
                c.iduty = v
        self.effect_arp(c)
        self.effect_pitch(c)
        if i < 2:
            self.effect_volume(c)
        self.tick_arp_seq(c)
        self.tick_pitch_seq(c)
        det = c.rel
        if c.vib:
            c.vib_acc = (c.vib_acc + (c.vib & 15)) & 0xFF
            a = c.vib_acc
            idx = ((~a & 15) if a & 16 else a & 15) | (c.vib & 0xF0)
            val = VIBRATO[idx]
            det += -val if a & 32 else val
        det += c.tuning
        c.det = det & 0xFFFF

    # -- synthesis
    def frame_params(self):
        out = []
        for c in self.ch:
            muted = c.status & MUTED
            trig = bool(c.status & TRIGGERED)
            if c.index < 2:
                vol = 0 if muted else VOLUME[(c.tvol << 4) | c.ivol]
                out.append((vol, c.det & 0x7FF, c.iduty & 3, trig))
            elif c.index == 2:
                on = not muted and c.volume and c.ivol
                out.append((15 if on else 0, c.det & 0x7FF, 0, trig))
            else:
                vol = 0 if muted else VOLUME[(c.tvol << 4) | c.ivol]
                out.append((vol, (0x10 - (c.rel & 0xFF)) & 15, c.iduty & 1, trig))
            c.status &= ~TRIGGERED
        return out

    def synth(self, params, n):
        mix = [-8000] * n
        rng = range(n)
        for c, (vol, per, duty, trig) in zip(self.ch[:2], params[:2]):
            if trig:
                c.phase = 0.0
            if not vol or per < 8:
                continue
            step = CPU / (16 * (per + 1)) / RATE
            ph, w = c.phase, vol * 560
            thr = DUTY[duty]
            if duty == 3:
                mix = [m + (0 if ((ph + i * step) % 1.0) < 0.25 else w) for m, i in zip(mix, rng)]
            else:
                mix = [m + (w if ((ph + i * step) % 1.0) < thr else 0) for m, i in zip(mix, rng)]
            c.phase = (ph + n * step) % 1.0
        c = self.ch[2]
        vol, per = params[2][0], params[2][1]
        if vol and per >= 2:
            step = CPU / (32 * (per + 1)) / RATE
            ph = c.phase
            mix = [m + TRIANGLE[int(((ph + i * step) % 1.0) * 32)] * 640 for m, i in zip(mix, rng)]
            c.phase = (ph + n * step) % 1.0
        c = self.ch[3]
        vol, idx, mode, _ = params[3]
        if vol:
            rate = CPU / NOISE_PERIODS[idx] / RATE
            acc, lf, cur = c.noise_acc, c.lfsr, c.noise_out
            tap = 6 if mode else 1
            w = vol * 370
            for i in rng:
                acc += rate
                k = 0
                while acc >= 1 and k < 6:
                    acc -= 1
                    k += 1
                    fb = (lf ^ (lf >> tap)) & 1
                    lf = (lf >> 1) | (fb << 14)
                    cur = lf & 1
                if acc >= 1:
                    acc %= 1
                if not cur:
                    mix[i] += w
            c.noise_acc, c.lfsr, c.noise_out = acc, lf, cur
        return mix

    def render(self, should_stop=lambda: False):
        """-> (intro_samples, loop_samples): PCM int16 lists. loop is empty for songs that end."""
        out = array.array("h")
        frame_start = {}
        carry = 0.0
        per_frame = RATE / FRAME_RATE
        for _ in range(int(MAX_SECONDS * FRAME_RATE)):
            if should_stop():
                raise FtError("cancelled")
            if self.row_n == 0 and self.counter >= self.cmp - self.tempo:
                frame_start.setdefault(self.frame_n, len(out))
            self.tick_rows()
            if self.halted:
                break
            if self.loop_target is not None:
                start = frame_start.get(self.loop_target, 0)
                return out, start
            for c in self.ch:
                self.tick_channel(c)
            carry += per_frame
            n = int(carry)
            carry -= n
            mix = self.synth(self.frame_params(), n)
            out.extend(max(-32768, min(32767, m)) for m in mix)
        return out, None


def render_song(data, number=0, should_stop=lambda: False):
    """Module bytes + song number -> WAV bytes (mono, 16-bit) that loops by repeating the loop section."""
    r = Renderer(Module(data), number)
    pcm, loop_start = r.render(should_stop)
    if not len(pcm):
        raise FtError("song is silent")
    if loop_start is not None and len(pcm) - loop_start > RATE // 2:
        body = pcm[loop_start:]
        reps = max(1, min(30, int(240 * RATE // len(body))))
        pcm = pcm[:loop_start] + body * reps
    fade = RATE // 100
    for i in range(min(fade, len(pcm))):
        pcm[i] = pcm[i] * i // fade
        pcm[-1 - i] = pcm[-1 - i] * i // fade
    if sys.byteorder == "big":
        pcm.byteswap()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()
