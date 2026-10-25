#!/usr/bin/env python3
"""Minimal NSF runner: emulates the 6502, runs INIT/PLAY and records the APU registers per frame."""
import struct


class CPU:
    """NMOS 6502 (official opcodes plus the common NOP variants). Memory is a flat 64K bytearray."""

    def __init__(self, mem, on_write):
        self.m, self.on_write = mem, on_write
        self.on_bank = lambda i, v: None
        self.a = self.x = self.y = 0
        self.sp = 0xFD
        self.pc = 0
        self.c = self.z = self.i = self.d = self.v = self.n = 0
        self.cycles = 0

    def rd(self, a):
        a &= 0xFFFF
        if 0x2000 <= a < 0x4000 or 0x4000 <= a < 0x4020:
            return 0x80 if a == 0x2002 else 0
        return self.m[a]

    def wr(self, a, v):
        a &= 0xFFFF
        if a < 0x2000:
            self.m[a & 0x7FF] = v
        elif 0x4000 <= a < 0x4020:
            self.on_write(a, v)
        elif 0x5FF8 <= a <= 0x5FFF:
            self.on_bank(a - 0x5FF8, v)
        elif a >= 0x6000 and a < 0x8000:
            self.m[a] = v

    def push(self, v):
        self.m[0x100 + self.sp] = v & 0xFF
        self.sp = (self.sp - 1) & 0xFF

    def pull(self):
        self.sp = (self.sp + 1) & 0xFF
        return self.m[0x100 + self.sp]

    def fetch(self):
        v = self.rd(self.pc)
        self.pc = (self.pc + 1) & 0xFFFF
        return v

    def fetch16(self):
        lo = self.fetch()
        return lo | self.fetch() << 8

    def nz(self, v):
        v &= 0xFF
        self.z, self.n = int(v == 0), v >> 7
        return v

    def flags(self, brk=0):
        return self.c | self.z << 1 | self.i << 2 | self.d << 3 | brk << 4 | 0x20 | self.v << 6 | self.n << 7

    def setflags(self, p):
        self.c, self.z, self.i, self.d, self.v, self.n = p & 1, p >> 1 & 1, p >> 2 & 1, p >> 3 & 1, p >> 6 & 1, p >> 7

    def addr(self, mode):
        if mode == "imm":
            a = self.pc
            self.pc += 1
            return a
        if mode == "zp":
            return self.fetch()
        if mode == "zpx":
            return (self.fetch() + self.x) & 0xFF
        if mode == "zpy":
            return (self.fetch() + self.y) & 0xFF
        if mode == "abs":
            return self.fetch16()
        if mode == "abx":
            return (self.fetch16() + self.x) & 0xFFFF
        if mode == "aby":
            return (self.fetch16() + self.y) & 0xFFFF
        if mode == "inx":
            p = (self.fetch() + self.x) & 0xFF
            return self.rd(p) | self.rd((p + 1) & 0xFF) << 8
        if mode == "iny":
            p = self.fetch()
            return ((self.rd(p) | self.rd((p + 1) & 0xFF) << 8) + self.y) & 0xFFFF
        raise ValueError(mode)

    def adc(self, v):
        r = self.a + v + self.c
        self.v = int(~(self.a ^ v) & (self.a ^ r) & 0x80 != 0)
        self.c = int(r > 255)
        self.a = self.nz(r)

    def cmp(self, r, v):
        t = r - v
        self.c = int(t >= 0)
        self.nz(t)

    def branch(self, cond):
        off = self.fetch()
        if cond:
            self.pc = (self.pc + (off - 256 if off > 127 else off)) & 0xFFFF

    def step(self):
        op = self.fetch()
        hi, lo = op >> 5, op & 31
        a = self
        # implied / special
        simple = {
            0x18: lambda: setattr(a, "c", 0), 0x38: lambda: setattr(a, "c", 1), 0x58: lambda: setattr(a, "i", 0),
            0x78: lambda: setattr(a, "i", 1), 0xB8: lambda: setattr(a, "v", 0), 0xD8: lambda: setattr(a, "d", 0),
            0xF8: lambda: setattr(a, "d", 1), 0xEA: lambda: None,
        }
        if op in simple:
            simple[op]()
            return
        if op == 0xAA:
            a.x = a.nz(a.a)
        elif op == 0xA8:
            a.y = a.nz(a.a)
        elif op == 0x8A:
            a.a = a.nz(a.x)
        elif op == 0x98:
            a.a = a.nz(a.y)
        elif op == 0xBA:
            a.x = a.nz(a.sp)
        elif op == 0x9A:
            a.sp = a.x
        elif op == 0xE8:
            a.x = a.nz(a.x + 1)
        elif op == 0xC8:
            a.y = a.nz(a.y + 1)
        elif op == 0xCA:
            a.x = a.nz(a.x - 1)
        elif op == 0x88:
            a.y = a.nz(a.y - 1)
        elif op == 0x48:
            a.push(a.a)
        elif op == 0x08:
            a.push(a.flags(1))
        elif op == 0x68:
            a.a = a.nz(a.pull())
        elif op == 0x28:
            a.setflags(a.pull())
        elif op == 0x60:
            a.pc = (a.pull() | a.pull() << 8) + 1
        elif op == 0x40:
            a.setflags(a.pull())
            a.pc = a.pull() | a.pull() << 8
        elif op == 0x20:
            t = a.fetch16()
            r = a.pc - 1
            a.push(r >> 8)
            a.push(r)
            a.pc = t
        elif op == 0x4C:
            a.pc = a.fetch16()
        elif op == 0x6C:
            p = a.fetch16()
            a.pc = a.rd(p) | a.rd((p & 0xFF00) | ((p + 1) & 0xFF)) << 8
        elif op == 0x00:
            a.pc += 1
            a.push(a.pc >> 8)
            a.push(a.pc)
            a.push(a.flags(1))
            a.i = 1
            a.pc = a.rd(0xFFFE) | a.rd(0xFFFF) << 8
        elif op & 0x1F == 0x10:  # branches
            cond = (a.n, a.v, a.c, a.z)[op >> 6]
            a.branch(cond == (op >> 5 & 1))
        elif op in (0x24, 0x2C):
            v = a.rd(a.addr("zp" if op == 0x24 else "abs"))
            a.z, a.n, a.v = int(a.a & v == 0), v >> 7, v >> 6 & 1
        elif op in (0xE0, 0xE4, 0xEC, 0xC0, 0xC4, 0xCC):
            mode = {0: "imm", 4: "zp", 12: "abs"}[op & 0xF]
            a.cmp(a.x if op >> 4 == 0xE else a.y, a.rd(a.addr(mode)))
        elif op in (0x84, 0x94, 0x8C, 0x86, 0x96, 0x8E):
            mode = {0x84: "zp", 0x94: "zpx", 0x8C: "abs", 0x86: "zp", 0x96: "zpy", 0x8E: "abs"}[op]
            a.wr(a.addr(mode), a.y if op & 3 == 0 else a.x)
        elif op in (0xA0, 0xA4, 0xB4, 0xAC, 0xBC, 0xA2, 0xA6, 0xB6, 0xAE, 0xBE):
            mode = {0xA0: "imm", 0xA4: "zp", 0xB4: "zpx", 0xAC: "abs", 0xBC: "abx", 0xA2: "imm", 0xA6: "zp",
                    0xB6: "zpy", 0xAE: "abs", 0xBE: "aby"}[op]
            v = a.nz(a.rd(a.addr(mode)))
            if op & 3 == 0:
                a.y = v
            else:
                a.x = v
        elif op & 3 == 1:  # group 1
            modes = ["inx", "zp", "imm", "abs", "iny", "zpx", "aby", "abx"]
            mode = modes[lo >> 2]
            if hi == 4:  # STA
                a.wr(a.addr(mode), a.a)
                return
            v = a.rd(a.addr(mode))
            if hi == 0:
                a.a = a.nz(a.a | v)
            elif hi == 1:
                a.a = a.nz(a.a & v)
            elif hi == 2:
                a.a = a.nz(a.a ^ v)
            elif hi == 3:
                if a.d:
                    raise NotImplementedError("decimal")
                a.adc(v)
            elif hi == 5:
                a.a = a.nz(v)
            elif hi == 6:
                a.cmp(a.a, v)
            else:
                a.adc(v ^ 0xFF)
        elif op & 3 == 2:  # group 2: shifts, INC/DEC
            if op in (0x0A, 0x2A, 0x4A, 0x6A):
                v = a.a
                a.a = a.shift(hi, v)
                return
            modes = {1: "zp", 3: "abs", 5: "zpx", 7: "abx"}
            ad = a.addr(modes[lo >> 2])
            v = a.rd(ad)
            if hi == 6:
                r = a.nz(v - 1)
            elif hi == 7:
                r = a.nz(v + 1)
            else:
                r = a.shift(hi, v)
            a.wr(ad, r)
        elif op & 3 == 0 and lo in (0, 4, 12, 20, 28):  # NOP variants (0x04,0x0C,0x14,0x1C,0x34...)
            if lo == 0:
                a.fetch()
            elif lo == 4:
                a.fetch()
            elif lo == 12 or lo == 28:
                a.fetch16()
            else:
                a.fetch()
        else:
            raise NotImplementedError(f"opcode {op:02x} at {a.pc - 1:04x}")

    def shift(self, kind, v):
        if kind == 0:  # ASL
            self.c = v >> 7
            return self.nz(v << 1)
        if kind == 1:  # ROL
            c = self.c
            self.c = v >> 7
            return self.nz(v << 1 | c)
        if kind == 2:  # LSR
            self.c = v & 1
            return self.nz(v >> 1)
        c = self.c  # ROR
        self.c = v & 1
        return self.nz(v >> 1 | c << 7)


class Nsf:
    def __init__(self, path):
        d = open(path, "rb").read()
        if d[:5] != b"NESM\x1a":
            raise ValueError("not an NSF")
        self.songs = d[6]
        self.load, self.init, self.play = struct.unpack("<HHH", d[8:14])
        self.title = d[14:46].split(b"\0")[0].decode("latin1")
        self.banks = list(d[112:120])
        self.data = d[128:]
        self.ntsc = struct.unpack("<H", d[110:112])[0]

    def run_song(self, song, frames, init_frames=0):
        """-> list (per frame) of dict {reg: value} for writes that frame, plus register file snapshots."""
        mem = bytearray(0x10000)
        banked = any(self.banks)
        if banked:
            padded = bytes(self.load & 0xFFF) + self.data
            pages = [padded[i:i + 4096].ljust(4096, b"\0") for i in range(0, len(padded), 4096)]

            def set_bank(i, v):
                mem[0x8000 + 4096 * i:0x9000 + 4096 * i] = pages[v] if v < len(pages) else bytes(4096)

            for i, b in enumerate(self.banks):
                set_bank(i, b)
        else:
            mem[self.load:self.load + len(self.data)] = self.data[:0x10000 - self.load]
        writes = []

        def on_write(a, v):
            writes.append((a, v))

        cpu = CPU(mem, on_write)
        if banked:
            cpu.on_bank = set_bank
        cpu.a, cpu.x, cpu.y = song, 0, 0
        SENT = 0x0FF0

        def call(addr, a=0, x=0, limit=2_000_000):
            cpu.a, cpu.x = a, x
            cpu.push((SENT - 1) >> 8)
            cpu.push((SENT - 1) & 0xFF)
            cpu.pc = addr
            n = 0
            while cpu.pc != SENT and n < limit:
                cpu.step()
                n += 1
            if n >= limit:
                raise RuntimeError("routine did not return")

        for i in range(0x800):
            mem[i] = 0
        call(self.init, song, 0)
        log = []
        for f in range(frames):
            del writes[:]
            call(self.play)
            log.append(list(writes))
        return log


if __name__ == "__main__":
    import sys
    n = Nsf(sys.argv[1])
    log = n.run_song(int(sys.argv[2]), int(sys.argv[3]) if len(sys.argv) > 3 else 600)
    for i, w in enumerate(log[:40]):
        print(i, " ".join(f"{a:04x}={v:02x}" for a, v in w))
