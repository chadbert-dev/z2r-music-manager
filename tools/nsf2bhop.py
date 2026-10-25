#!/usr/bin/env python3
"""Convert one NSF track to a bhop (Z2R) module by running it in an emulator and re-encoding what it plays.

    nsf2bhop.py game.nsf TRACK [--frames N] [-o out.bin]

Each note onset becomes a note with an instrument built from the channel's actual per-frame volume, duty and
pitch deviation, so hardware envelopes, length counters and sweeps come out as ordinary FamiTracker sequences.
"""
import argparse
import math
import os
import sys
from functools import reduce

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from apusim import Apu  # noqa: E402
from nsfrip import Nsf  # noqa: E402
import ftplayer  # noqa: E402

ONSET_REG = (0x4003, 0x4007, 0x400B, 0x400F)
PERIODS = ftplayer.PERIODS


def capture(nsf, track, frames):
    log = nsf.run_song(track, frames)
    capture.dpcm = sum(1 for w in log for r, v in w if 0x4010 <= r <= 0x4013 or (r == 0x4015 and v & 0x10))
    apu = Apu()
    state = [apu.frame(w) for w in log]
    onsets = [[i for i, w in enumerate(log) if any(a == r for a, _ in w)] for r in ONSET_REG]
    return state, onsets


def nearest_note(period):
    best = min(range(8, 108), key=lambda n: abs(PERIODS[n] - period))
    return best


def trim(seq):
    seq = list(seq)
    while len(seq) > 1 and seq[-1] == seq[-2]:
        seq.pop()
    return tuple(seq)


class Seg:
    def __init__(self, ch, start, end, state):
        self.ch, self.start, self.end = ch, start, end
        fr = [state[f][ch] for f in range(start, end)]
        self.silent = all(v == 0 for v, _, _ in fr)
        if self.silent:
            return
        first = next(x for x in fr if x[0])
        if ch == 3:
            self.note = 16 - first[1] if first[1] else 16  # noise note n -> period index (0x10 - n) & 15
            self.vol = trim(v for v, _, _ in fr)
            self.duty = trim(m for _, _, m in fr)
            self.dev = ()
        else:
            self.note = nearest_note(first[1])
            self.vol = trim(v for v, _, _ in fr)
            self.duty = trim(d for _, _, d in fr)
            dev = trim(max(-127, min(127, p - PERIODS[self.note])) for _, p, _ in fr)
            self.dev = () if set(dev) == {0} else dev


def segments(state, onsets, total, extra_cuts=()):
    segs = []
    for ch in range(4):
        cuts = sorted(set([0] + [o for o in onsets[ch] if o < total] + [c for c in extra_cuts if 0 < c < total]))
        # engines that drive the volume in software never write the length register again: a volume that goes
        # back up starts a new note
        for f in range(1, total):
            if state[f][ch][0] > state[f - 1][ch][0]:
                cuts.append(f)
        # a note's pitch may only drift a little from its note (pitch sequences hold small signed values);
        # a bigger change starts a new note
        if ch != 3:
            base = sorted(set(cuts)) + [total]
            for a0, b0 in zip(base, base[1:]):
                ref = None
                for f in range(a0, b0):
                    v, per = state[f][ch][0], state[f][ch][1]
                    if not v:
                        continue
                    if ref is None:
                        ref = PERIODS[nearest_note(per)]
                    elif abs(per - ref) > 60:
                        cuts.append(f)
                        ref = PERIODS[nearest_note(per)]
        cuts = sorted(set(cuts)) + [total]
        segs.append([Seg(ch, a, b, state) for a, b in zip(cuts, cuts[1:])])
    return segs


def detect_loop(state, min_period=90, glitches=8):
    """-> (start, period, body) if the audible state ends in a repeating cycle, else None.
    The song is state[:start] followed by the cycle state[body:body + period]. A few stray frames that differ
    between passes (start-up noise, one-off blips) are tolerated; the cycle is taken from the last pass."""
    n = len(state)
    last = max((f for f in range(n) if any(v for v, _, _ in state[f])), default=-1)
    if last < 2 * min_period:
        return None
    n = last + 1
    h = [hash(tuple(c if c[0] else (0, 0, 0) for c in state[f])) for f in range(n)]  # silent channels don't count
    for p in range(min_period, n // 2 + 1):
        if h[n - 1] != h[n - 1 - p]:
            continue
        bad, s = 0, 0
        for f in range(n - p - 1, -1, -1):
            if h[f] != h[f + p]:
                bad += 1
                if bad > glitches:
                    s = f + 1
                    break
        if n - s < 2 * p or s > n // 2:
            continue
        body = s + (n - p - s) // p * p
        return s, p, body
    return None


class Builder:
    def __init__(self, segs, total, grid):
        self.segs, self.total, self.g = segs, total, grid
        self.rows = -(-total // grid)
        self.instruments = {}  # key -> index

    MAX_INSTRUMENTS = 127  # a pattern's instrument command carries a 7-bit number

    def assign_instruments(self):
        """Most-used instruments get the low numbers (0-15 cost one byte in a pattern, the rest two). A song
        with more than 127 distinct instruments is the one lossy step: the rarest are replaced by the most
        similar kept instrument (see `approximated`)."""
        use = {}
        for ch in self.segs:
            for s in ch:
                if not s.silent:
                    use[(s.vol, s.duty, s.dev)] = use.get((s.vol, s.duty, s.dev), 0) + 1
        order = sorted(use, key=lambda k: -use[k])
        for key in order[:self.MAX_INSTRUMENTS]:
            self.instruments[key] = len(self.instruments)
        self.alias = {}
        self.approximated = 0

        def pad(seq, n):
            return list(seq) + [seq[-1] if seq else 0] * (n - len(seq))

        def dist(a, b):
            n = max(len(a[0]), len(b[0]), len(a[2]), len(b[2]), len(a[1]), len(b[1]))
            return (sum(abs(x - y) for x, y in zip(pad(a[0], n), pad(b[0], n)))
                    + 4 * sum(x != y for x, y in zip(pad(a[1], n), pad(b[1], n)))
                    + sum(abs(x - y) for x, y in zip(pad(a[2], n), pad(b[2], n))) // 4)

        for key in order[self.MAX_INSTRUMENTS:]:
            self.alias[key] = min(self.instruments, key=lambda k: dist(key, k))
            self.approximated += use[key]

    def instrument(self, seg):
        key = (seg.vol, seg.duty, seg.dev)
        return self.instruments[self.alias.get(key, key)]

    def stream(self, ch, r0, r1):
        """Pattern bytes for rows [r0, r1) of one channel."""
        out = bytearray()
        g = self.g
        prev = None  # instrument already selected on this channel within the stream
        for s in self.segs[ch]:
            a, b = max(s.start // g, r0), min(-(-s.end // g), r1)
            if b <= a:
                continue
            n = b - a
            if s.silent:
                first = True
                while n:
                    k = min(n, 256)
                    out += bytes([0x7F if first else 0, k - 1])  # note off, so a note never outlasts its segment
                    first = False
                    n -= k
                continue
            if s.start // g < r0:
                raise ValueError("note crosses a pattern boundary")
            idx = self.instrument(s)
            if idx != prev:
                if idx < 16:
                    out.append(0xE0 | idx)
                else:
                    out += bytes([0x80, idx * 2])
                prev = idx
            k = min(n, 256)
            out += bytes([s.note, k - 1])
            n -= k
            while n:  # a note longer than 256 rows: the rest is silence
                k = min(n, 256)
                out += bytes([0, k - 1])
                n -= k
        return bytes(out)

    def boundaries(self, force=()):
        """Pattern split rows. A split must not cut through a sounding note, but when a stretch of 255 rows has
        no clean row, the least damaging one is used and the notes crossing it are returned in `splits` (rows)
        so the caller can re-cut them as separate notes, which sound the same apart from a pulse phase reset."""
        g = self.g
        cross = [0] * (self.rows + 1)
        for ch in range(4):
            for s in self.segs[ch]:
                if not s.silent:
                    for r in range(s.start // g + 1, -(-s.end // g)):
                        cross[r] += 1
        cuts, splits, pos = [0], [], 0
        while pos < self.rows:
            hi = min(pos + 255, self.rows)
            forced = [r for r in force if pos < r <= hi]
            if forced:
                nxt = min(forced)
            else:
                nxt = min(range(pos + 1, hi + 1), key=lambda r: (cross[r], -r))
            if cross[nxt]:
                splits.append(nxt)
            cuts.append(nxt)
            pos = nxt
        return cuts, splits


def seq_bytes(values, mode=0):
    vals = bytes(v & 0xFF for v in values)
    return bytes([len(vals), 0xFF, 0, mode]) + vals


class NeedsSplit(Exception):
    def __init__(self, frames):
        super().__init__(frames)
        self.frames = frames


def build_module(segs, total, grid, loop_start=None, tempo=150):
    """loop_start: frame the song loops back to (None = the song ends with a halt)."""
    b = Builder(segs, total, grid)
    b.assign_instruments()
    loop_row = None if loop_start is None else loop_start // grid
    cuts, splits = b.boundaries([loop_row] if loop_row else [])
    if splits:
        raise NeedsSplit([r * grid for r in splits])
    plen = max(c1 - c0 for c0, c1 in zip(cuts, cuts[1:]))
    target = cuts.index(loop_row) if loop_row is not None else None
    frames = []
    for n, (r0, r1) in enumerate(zip(cuts, cuts[1:])):
        rows = r1 - r0
        last = n == len(cuts) - 2
        if last:
            tail = bytes([0x86, target + 1]) if loop_start is not None else bytes([0x88, 0])
        else:
            tail = bytes([0x87, 1]) if rows < plen else b""
        dpcm, left = bytearray(), rows - 1
        while left > 0:
            k = min(left, 256)
            dpcm += bytes([0, k - 1])
            left -= k
        dpcm += tail + bytes([0, 0])
        frames.append([b.stream(c, r0, r1) for c in range(4)] + [bytes(dpcm)])
    instr_list = sorted(b.instruments.items(), key=lambda kv: kv[1])
    n_instr = max(1, len(instr_list))

    # layout
    hdr = 15
    instr_tbl = hdr
    instr_data = instr_tbl + 2 * n_instr
    blobs = bytearray()
    seq_cache = {}

    def add_seq(vals, mode):
        key = (tuple(vals), mode)
        if key not in seq_cache:
            seq_cache[key] = len(blobs)
            blobs.extend(seq_bytes(vals, mode))
        return seq_cache[key]

    inst_defs = []
    for (vol, duty, dev), _ in instr_list:
        inst_defs.append([add_seq(vol, 0), add_seq(duty, 0), add_seq(dev, 1) if dev else None])
    if not instr_list:
        inst_defs.append([None, None, None])
    inst_size = [2 + 2 * sum(x is not None for x in d) for d in inst_defs]
    sample_addr = instr_data + sum(inst_size)
    song_list = sample_addr
    song = song_list + 2
    frame_list = song + 8
    frame_tbl = frame_list + 2 * len(frames)
    seq_base = frame_tbl + 10 * len(frames)
    pat_base = seq_base + len(blobs)

    pats = bytearray()
    pat_addr = {}
    frame_ptrs = []
    for fr in frames:
        ptrs = []
        for s in fr:
            if s not in pat_addr:
                pat_addr[s] = pat_base + len(pats)
                pats += s
            ptrs.append(pat_addr[s])
        frame_ptrs.append(ptrs)

    out = bytearray()

    def w(v):
        out.extend((v & 0xFF, v >> 8 & 0xFF))

    for v in (song_list, instr_tbl, sample_addr, sample_addr, sample_addr):
        w(v)
    out.append(0)
    w(3600)
    w(3000)
    addr = instr_data
    for sz in inst_size:
        w(addr)
        addr += sz
    for d in inst_defs:
        mask = (d[0] is not None) | (d[2] is not None) << 2 | (d[1] is not None) << 4
        out += bytes([0, mask])
        if d[0] is not None:
            w(seq_base + d[0])
        if d[2] is not None:
            w(seq_base + d[2])
        if d[1] is not None:
            w(seq_base + d[1])
    w(song)
    w(frame_list)
    out += bytes([len(frames), plen, grid, tempo, 0, 0])
    for i in range(len(frames)):
        w(frame_tbl + 10 * i)
    for ptrs in frame_ptrs:
        for p in ptrs:
            w(p)
    out += blobs
    out += pats
    return bytes(out), len(instr_list), len(frames)


def convert(nsf_path, track, frames, grid=None, loop=True):
    """-> (module bytes, per-frame state, grid, instruments, pattern frames, total frames, loop_start or None).
    `frames` is how long to capture; with loop=True a repeating tail is detected and the module loops back."""
    nsf = Nsf(nsf_path)
    state, onsets = capture(nsf, track, frames)
    found = detect_loop(state) if loop else None
    if found:
        start, period, body = found
        total, loop_start = start + period, start
        state = state[:start] + state[body:body + period]
        onsets = [[o for o in ons if o < start] + [o - body + start for o in ons if body <= o < body + period]
                  for ons in onsets]
    else:
        total = max((f for f in range(frames) if any(v for v, _, _ in state[f])), default=0) + 1
        loop_start = None
    marks = [total] + ([loop_start] if loop_start else [])
    state = state[:total] + [[(0, 0, 0)] * 4] * max(0, total - len(state))
    segs0 = segments(state, onsets, total, [loop_start] if loop_start else [])
    marks += [s.start for ch in segs0 for s in ch if not s.silent]
    if grid is None:
        grid = max(1, min(reduce(math.gcd, marks, 0) or 1, 255))
    if loop_start is None:
        total += grid  # one silent row so the final halt does not cut the last notes
        state = state + [[(0, 0, 0)] * 4] * grid
    padded = -(-total // grid) * grid
    if padded != total:  # only happens with a user-chosen grid; the tail is silence
        state = state + [[(0, 0, 0)] * 4] * (padded - total)
        total = padded
    extra = [loop_start] if loop_start else []
    while True:
        segs = segments(state, onsets, total, extra)
        long_ = [s.start + -(-200 // grid) * grid for ch in segs for s in ch if not s.silent and max(len(s.vol), len(s.duty), len(s.dev)) > 250]
        if long_:  # a sequence is at most 255 steps; cut long notes into pieces
            extra = sorted(set(extra) | set(long_))
            continue
        try:
            data, n_instr, n_frames = build_module(segs, total, grid, loop_start)
            break
        except NeedsSplit as e:
            extra = sorted(set(extra) | set(e.frames))
    return data, state, grid, n_instr, n_frames, total, loop_start


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("nsf")
    ap.add_argument("track", type=int, help="0-based track number")
    ap.add_argument("--frames", type=int, default=2200)
    ap.add_argument("--grid", type=int)
    ap.add_argument("--no-loop", action="store_true")
    ap.add_argument("-o", "--out")
    a = ap.parse_args()
    data, state, grid, ni, nf, total, ls = convert(a.nsf, a.track, a.frames, a.grid, not a.no_loop)
    print(f"{len(data)} bytes, {ni} instruments, {nf} pattern frames, grid {grid} frames/row, {total} frames, "
          f"loop {'from frame %d' % ls if ls is not None else 'none (halts)'}")
    if a.out:
        open(a.out, "wb").write(data)
