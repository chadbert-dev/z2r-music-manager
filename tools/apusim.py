"""Per-frame model of the NES APU (pulse 1/2, triangle, noise): turns register writes into audible state."""
LENGTHS = [10, 254, 20, 2, 40, 4, 80, 6, 160, 8, 60, 10, 14, 12, 26, 14,
           12, 16, 24, 18, 48, 20, 96, 22, 192, 24, 72, 26, 16, 28, 32, 30]


class Env:
    def __init__(self):
        self.start = False
        self.div = self.decay = 0

    def clock(self, reg):
        if self.start:
            self.start = False
            self.decay, self.div = 15, reg & 15
        elif self.div == 0:
            self.div = reg & 15
            if self.decay:
                self.decay -= 1
            elif reg & 0x20:
                self.decay = 15
        else:
            self.div -= 1

    def vol(self, reg):
        return reg & 15 if reg & 0x10 else self.decay


class Pulse:
    def __init__(self, ones):
        self.r = [0, 0, 0, 0]
        self.len = 0
        self.env = Env()
        self.ones = ones  # sweep negate uses one's complement on pulse 1
        self.sdiv, self.sreload = 0, False
        self.period = 0
        self.enabled = False

    def write(self, i, v):
        self.r[i] = v
        if i == 1:
            self.sreload = True
        elif i == 2:
            self.period = (self.period & 0x700) | v
        elif i == 3:
            self.period = (self.period & 0xFF) | (v & 7) << 8
            if self.enabled:
                self.len = LENGTHS[v >> 3]
            self.env.start = True

    def target(self):
        d = self.period >> (self.r[1] & 7)
        if self.r[1] & 8:
            return self.period - d - (1 if self.ones else 0)
        return self.period + d

    def half(self):
        if not self.r[0] & 0x20 and self.len:
            self.len -= 1
        r1 = self.r[1]
        if self.sdiv == 0 and r1 & 0x80 and r1 & 7 and self.period >= 8 and self.target() <= 0x7FF:
            self.period = max(0, self.target())
        if self.sdiv == 0 or self.sreload:
            self.sdiv, self.sreload = (r1 >> 4) & 7, False
        else:
            self.sdiv -= 1

    def out(self):
        """(volume, period, duty)"""
        if not self.len or self.period < 8 or self.target() > 0x7FF:
            return 0, self.period, self.r[0] >> 6
        return self.env.vol(self.r[0]), self.period, self.r[0] >> 6


class Triangle:
    def __init__(self):
        self.r = [0, 0, 0, 0]
        self.len = self.lin = 0
        self.reload = False
        self.period = 0
        self.enabled = False

    def write(self, i, v):
        self.r[i] = v
        if i == 2:
            self.period = (self.period & 0x700) | v
        elif i == 3:
            self.period = (self.period & 0xFF) | (v & 7) << 8
            if self.enabled:
                self.len = LENGTHS[v >> 3]
            self.reload = True

    def quarter(self):
        if self.reload:
            self.lin = self.r[0] & 0x7F
        elif self.lin:
            self.lin -= 1
        if not self.r[0] & 0x80:
            self.reload = False

    def half(self):
        if not self.r[0] & 0x80 and self.len:
            self.len -= 1

    def out(self):
        return (15 if self.len and self.lin and self.period >= 2 else 0), self.period, 0


class Noise:
    def __init__(self):
        self.r = [0, 0, 0, 0]
        self.len = 0
        self.env = Env()
        self.enabled = False

    def write(self, i, v):
        self.r[i] = v
        if i == 3:
            if self.enabled:
                self.len = LENGTHS[v >> 3]
            self.env.start = True

    def half(self):
        if not self.r[0] & 0x20 and self.len:
            self.len -= 1

    def out(self):
        return (self.env.vol(self.r[0]) if self.len else 0), self.r[2] & 15, self.r[2] >> 7


class Apu:
    def __init__(self):
        self.p1, self.p2, self.tri, self.noi = Pulse(True), Pulse(False), Triangle(), Noise()
        self.chans = (self.p1, self.p2, self.tri, self.noi)
        self.write(0x4015, 0x0F)  # NSF players enable the channels before INIT

    def write(self, a, v):
        if a == 0x4015:
            for c, bit in zip(self.chans, (1, 2, 4, 8)):
                c.enabled = bool(v & bit)
                if not c.enabled:
                    c.len = 0
        elif 0x4000 <= a < 0x4010:
            c = self.chans[(a - 0x4000) >> 2]
            c.write(a & 3, v)

    def frame(self, writes):
        """Apply a frame's register writes, run the frame sequencer for one frame, return per-channel
        (volume 0-15, period or noise index, duty or noise mode) sampled mid-frame."""
        for a, v in writes:
            self.write(a, v)
        out = None
        for q in range(4):
            self.p1.env.clock(self.p1.r[0])
            self.p2.env.clock(self.p2.r[0])
            self.noi.env.clock(self.noi.r[0])
            self.tri.quarter()
            if q & 1:
                for c in self.chans:
                    c.half()
            if q == 1:
                out = [c.out() for c in self.chans]
        return out
