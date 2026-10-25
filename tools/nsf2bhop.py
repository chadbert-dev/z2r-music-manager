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


def segments(state, onsets, total):
    segs = []
    for ch in range(4):
        cuts = sorted(set([0] + [o for o in onsets[ch] if o < total]))
        # a pitch jump too large for a pitch sequence also starts a new note
        for f in range(1, total):
            if ch != 3 and state[f][ch][0] and state[f - 1][ch][0] and abs(state[f][ch][1] - state[f - 1][ch][1]) > 100:
                cuts.append(f)
        cuts = sorted(set(cuts)) + [total]
        segs.append([Seg(ch, a, b, state) for a, b in zip(cuts, cuts[1:])])
    return segs


class Builder:
    def __init__(self, segs, total, grid):
        self.segs, self.total, self.g = segs, total, grid
        self.rows = -(-total // grid)
        self.instruments = {}  # key -> index

    def instrument(self, seg):
        key = (seg.vol, seg.duty, seg.dev)
        if key not in self.instruments:
            self.instruments[key] = len(self.instruments)
        return self.instruments[key]

    def stream(self, ch, r0, r1):
        """Pattern bytes for rows [r0, r1) of one channel."""
        out = bytearray()
        g = self.g
        for s in self.segs[ch]:
            a, b = max(s.start // g, r0), min(-(-s.end // g), r1)
            if b <= a:
                continue
            n = b - a
            if s.silent:
                while n:
                    k = min(n, 256)
                    out += bytes([0, k - 1])
                    n -= k
                continue
            if s.start // g < r0:
                raise ValueError("note crosses a pattern boundary")
            idx = self.instrument(s)
            if idx < 16:
                out.append(0xE0 | idx)
            else:
                out += bytes([0x80, idx * 2])
            k = min(n, 256)
            out += bytes([s.note, k - 1])
            n -= k
            while n:  # a note longer than 256 rows: the rest is silence
                k = min(n, 256)
                out += bytes([0, k - 1])
                n -= k
        return bytes(out)

    def boundaries(self):
        """Pattern split rows: every channel must start a new note (or be resting) at each one."""
        g = self.g
        ok = set(range(self.rows + 1))
        for ch in range(4):
            starts = {s.start // g for s in self.segs[ch] if not s.silent}
            ends = {-(-s.end // g) for s in self.segs[ch] if not s.silent}
            inside = set()
            for s in self.segs[ch]:
                if not s.silent:
                    inside |= set(range(s.start // g + 1, -(-s.end // g)))
            ok -= inside
        cuts, pos = [0], 0
        while pos < self.rows:
            nxt = max((r for r in ok if pos < r <= min(pos + 255, self.rows)), default=None)
            if nxt is None:
                raise ValueError(f"cannot split at {pos}")
            cuts.append(nxt)
            pos = nxt
        return cuts


def seq_bytes(values, mode=0):
    vals = bytes(v & 0xFF for v in values)
    return bytes([len(vals), 0xFF, 0, mode]) + vals


def build_module(segs, total, grid, tempo=150):
    b = Builder(segs, total, grid)
    cuts = b.boundaries()
    frames = []  # per pattern frame: [stream bytes x5]
    for r0, r1 in zip(cuts, cuts[1:]):
        rows = r1 - r0
        dpcm = bytearray()
        left = rows
        while left:
            k = min(left, 256)
            dpcm += bytes([0, k - 1])
            left -= k
        frames.append([b.stream(c, r0, r1) for c in range(4)] + [bytes(dpcm)])
    # pattern rows value is per song: use the largest pattern length; shorter ones must be padded by caller
    plen = max(c1 - c0 for c0, c1 in zip(cuts, cuts[1:]))
    if any(c1 - c0 != plen for c0, c1 in zip(cuts, cuts[1:])):
        # shorter frames end early: pad channels with rests so every stream spans plen rows? Not needed: the
        # driver advances to the next frame when row_counter reaches plen, so short frames must be padded.
        frames = []
        for r0, r1 in zip(cuts, cuts[1:]):
            rows = r1 - r0
            pad = bytearray()
            left = plen - rows
            while left > 0:
                k = min(left, 256)
                pad += bytes([0, k - 1])
                left -= k
            frames.append([b.stream(c, r0, r1) + bytes(pad) for c in range(4)] + [bytes(
                sum(([0, min(plen - i * 256, 256) - 1] for i in range(-(-plen // 256))), []))])
    instr_list = sorted(b.instruments.items(), key=lambda kv: kv[1])
    n_instr = max(1, len(instr_list))

    # layout
    hdr = 15
    instr_tbl = hdr
    instr_data = instr_tbl + 2 * n_instr
    blobs = bytearray()
    instr_addrs = []
    seq_cache = {}

    def add_seq(vals, mode):
        key = (tuple(vals), mode)
        if key not in seq_cache:
            seq_cache[key] = len(blobs)
            blobs.extend(seq_bytes(vals, mode))
        return seq_cache[key]

    # sequences first (positions are relative to the blob area, fixed up below)
    inst_defs = []
    for (vol, duty, dev), _ in instr_list:
        d = [add_seq(vol, 0), add_seq(duty, 0), add_seq(dev, 1) if dev else None]
        inst_defs.append(d)
    if not instr_list:
        inst_defs.append([None, None, None])
    inst_size = [2 + 2 * sum(x is not None for x in d) for d in inst_defs]
    instr_end = instr_data + sum(inst_size)
    sample_addr = instr_end
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


def convert(nsf_path, track, frames, grid=None):
    nsf = Nsf(nsf_path)
    state, onsets = capture(nsf, track, frames)
    # end of the song: last frame where anything audible
    last = max((f for f in range(frames) if any(v for v, _, _ in state[f])), default=0) + 1
    total = last
    segs = segments(state, onsets, total)
    if grid is None:
        marks = [s.start for ch in segs for s in ch if not s.silent]
        grid = reduce(math.gcd, marks + [total], 0) or 1
        grid = max(1, min(grid, 255))
    total = -(-total // grid) * grid
    data, n_instr, n_frames = build_module(segmentss(segs, state, total, onsets), total, grid)
    return data, state, grid, n_instr, n_frames, total


def segmentss(segs, state, total, onsets):
    return segments(state + [[(0, 0, 0)] * 4] * (total - len(state)), onsets, total) if total > len(state) else segs


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("nsf")
    ap.add_argument("track", type=int, help="0-based track number")
    ap.add_argument("--frames", type=int, default=2200)
    ap.add_argument("--grid", type=int)
    ap.add_argument("-o", "--out")
    a = ap.parse_args()
    data, state, grid, ni, nf, total = convert(a.nsf, a.track, a.frames, a.grid)
    print(f"{len(data)} bytes, {ni} instruments, {nf} pattern frames, grid {grid} frames/row, {total} frames")
    if a.out:
        open(a.out, "wb").write(data)
