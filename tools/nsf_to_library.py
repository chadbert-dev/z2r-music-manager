#!/usr/bin/env python3
"""Convert every track listed in an .m3u next to an NSF into one Z2R library json5.

    nsf_to_library.py game.nsf playlist.m3u "Game Title" "Composer" out.json5 [--first N]

Track numbers and titles come from the m3u (`file::NSF,<track>,<title>,<length>,,<fade>`), in its order.
"""
import argparse
import base64
import os
import re
import sys
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import nsf2bhop  # noqa: E402
import verify_module  # noqa: E402
import z2r_music_manager as app  # noqa: E402


def secs(t):
    parts = [float(x) for x in t.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    return parts[0] * 3600 + parts[1] * 60 + parts[2]


def parse_m3u(path):
    rows = []
    for line in open(path, encoding="utf-8", errors="replace"):
        line = line.strip().replace("\\,", "\0")  # `\,` is a comma inside a field
        m = re.match(r".*?::NSF,(\d+),(.*),(\d+:\d+(?::\d+)?(?:\.\d+)?)?,[^,]*,[^,]*$", line)
        if m:
            rows.append((int(m.group(1)), m.group(2).replace("\0", ","), secs(m.group(3) or "0:02:00")))
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("nsf")
    ap.add_argument("m3u")
    ap.add_argument("game")
    ap.add_argument("author")
    ap.add_argument("out")
    ap.add_argument("--max-bytes", type=int, default=8192)
    ap.add_argument("--strip", default="", help="text removed from the start of every title")
    ap.add_argument("--count", type=int, help="with m3u '-': number of tracks, titled 'Track NN'")
    a = ap.parse_args()
    items, problems = [], []
    rows = [(i + 1, f"Track {i + 1:02d}", 120.0) for i in range(a.count)] if a.m3u == "-" else parse_m3u(a.m3u)
    for n, (num, title, length) in enumerate(rows):
        title = title[len(a.strip):] if a.strip and title.startswith(a.strip) else title
        frames = int(min(length * 3.2, 200) * 60.1) + 60  # enough to see the intro and two passes of the loop
        data, state, grid, ni, nf, total, loop = nsf2bhop.convert(a.nsf, num - 1, frames)
        import io
        buf = io.StringIO()
        sys.stdout, old = buf, sys.stdout
        try:
            verify_module.compare(data, state, total, loop)
        finally:
            sys.stdout = old
        bad = sum(int(x) for line in buf.getvalue().splitlines() for x in re.findall(r"'(?:vol|per|duty)': (\d+)", line))
        flag = "" if bad == 0 else f"  MISMATCHES {bad}"
        print(f"{num:3} {title:40} {len(data):5} B  {ni:3} instr  grid {grid:3}  {total/60.1:6.1f}s  "
              f"{'loops from %.1fs' % (loop / 60.1) if loop is not None else 'ends'}{flag}")
        too_big = len(data) > a.max_bytes
        if too_big:
            problems.append(f"{title}: {len(data)} bytes")
        if nsf2bhop.capture.dpcm:
            problems.append(f"{title}: uses DPCM ({nsf2bhop.capture.dpcm} register writes), samples not converted")
        c = zlib.compressobj(9, zlib.DEFLATED, -15)
        enc = "deflate:" + base64.b64encode(c.compress(data) + c.flush()).decode()
        item = {"title": f"{n:02d}. {title}", "data": enc}
        if loop is None or too_big:
            item = {"enabled": False, **item}  # jingle that ends, or over the 8 KB limit
        items.append(item)
    lib = {"groups": [{"title": a.game, "author": a.author, "primary_square_chan": 1, "streaming_safe": False,
                       "items": items}]}
    with open(a.out, "w", encoding="utf-8") as f:
        f.write(app.json5_dumps(lib) + "\n")
    print("wrote", a.out)
    for p in problems:
        print("TOO BIG:", p)


main()
