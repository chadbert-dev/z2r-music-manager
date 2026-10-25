"""Compare a converted module's per-frame output with the NSF's own (volume, period, duty) per channel."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import ftplayer as F


def module_frames(data, nframes):
    r = F.Renderer(F.Module(data))
    out = []
    for _ in range(nframes):
        r.tick_rows()
        for c in r.ch:
            r.tick_channel(c)
        out.append(r.frame_params())
        if r.halted:
            break
    return out


def compare(data, state, total):
    got = module_frames(data, total)
    names = ["pulse1", "pulse2", "tri", "noise"]
    for ch in range(4):
        bad = {"vol": 0, "per": 0, "duty": 0}
        n = 0
        first = None
        for f in range(min(total, len(got))):
            tv, tp, td = state[f][ch]
            gv, gp, gd, _ = got[f][ch]
            if tv == 0 and gv == 0:
                continue
            n += 1
            if ch == 2:
                tv = tv  # tri on/off
            if tv != gv:
                bad["vol"] += 1
                first = first or (f, "vol", tv, gv)
            elif ch < 3 and tp != gp:
                bad["per"] += 1
                first = first or (f, "per", tp, gp)
            elif ch < 2 and td != gd:
                bad["duty"] += 1
                first = first or (f, "duty", td, gd)
        print(f"{names[ch]:7} audible frames {n:5}  mismatches {bad}  first {first}")
