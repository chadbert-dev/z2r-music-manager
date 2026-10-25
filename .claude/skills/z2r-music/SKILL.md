---
name: z2r-music
description: Create, edit or validate Zelda II Randomizer (Z2R) custom music library .json5 files. Use when adding songs or games to a library, converting FamiTracker/NSF music for Z2R, fixing `uses`, or checking size and format limits.
---

# Z2R custom music libraries

Source: https://github.com/Ellendar/Z2Randomizer/wiki/Custom-Music (re-fetch it if details matter; this may be stale).

## File layout

A library is a `.json5` file in the `Music` folder next to `Z2Randomizer`. This project's libraries live in `/home/altair/Work/FtSoundTracks/`, one file per game, named after the game (`Balloon Fight.json5`). Match that style: tab indentation, bare keys, trailing commas (`json5_dumps` in `z2r_music_manager.py` writes it).

```json5
{
	single: [ { title, author, uses, streaming_safe, primary_square_chan, data } ],
	groups: [
		{
			title: "Game",            // shared by items
			author: "Composer",
			primary_square_chan: 0,
			items: [ { title: "00. Opening", uses: ["Town"], data: "deflate:..." } ],
		},
	],
}
```

Fields: `title` (required, "game - song"), `author`, `enabled` (default true), `uses` (default `["Palace","GreatPalace"]`), `streaming_safe`, `primary_square_chan` (0 or 1, required), `data` (required). Items inherit group fields. A `songs: [{number, title}]` list marks a multi-song module.

Valid `uses` (case-insensitive; any other name makes Z2R refuse the whole file): Title, Overworld, Town, Encounter, Cave, Palace, GreatPalace, Boss, LastBoss, Credits, GameComplete, House, PalaceClear.

## Making the data

1. Open the song's .ftm/.dnm in Dn-FamiTracker. It cannot open NSF files (it only writes them), so an NSF has to be turned into a module some other way first.
2. File > Create NSF... > file type "BIN - Raw music data" > Export.
3. Encode: plain base64 of the .bin. The existing libraries here use `deflate:` + base64 of raw deflate, which also works:
   ```python
   c = zlib.compressobj(9, zlib.DEFLATED, -15)
   data = "deflate:" + base64.b64encode(c.compress(raw) + c.flush()).decode()
   ```

Hard limits per module: 8192 bytes; no DPCM samples; no expansion chips (including MMC5); patterns up to 255 rows; no echo buffer; no hardware envelope/sweep effects (E, H, I).

`primary_square_chan`: mute each square channel in turn; the one that sounds worse muted is primary.

## Converting an NSF (no FamiTracker module available)

Dn-FamiTracker cannot open NSFs, and rainwarrior's NSF Importer (wine, GUI only) gave results the user did not like.
Use the repo's own converter instead:

```sh
python3 tools/nsf2bhop.py game.nsf TRACK --frames 2200 -o song.bin   # TRACK is 0-based; --frames >= song length
```

How it works: `tools/nsfrip.py` (6502 emulator) runs the NSF's INIT/PLAY, `tools/apusim.py` models the 2A03
(envelopes, length counters, linear counter, sweep) to get per-frame volume/pitch/duty, and `nsf2bhop.py` re-encodes
that as a bhop module: one note per register-write note onset, instruments built from the per-frame volume/duty/pitch
sequences, speed set to the GCD of the onset times, patterns split only where every channel starts a note.
Pulse 1/2, triangle and noise only; DPCM is dropped. Typical output is 1-3 KB, well under 8 KB.

Always verify: `tools/verify_module.py` (`compare(data, state, total)`, state/total come from
`nsf2bhop.convert(...)`) plays the module through `ftplayer` and compares every audible frame with the NSF.
Aim for 0 mismatches. It only proves agreement with our port of bhop, not Z2R's driver, so say so.

Packaging: put songs in a `groups` entry (title = game, author = composer from the .m3u/NSF, `streaming_safe: false`
for commercial music), item titles like `"00. Title"` from the .m3u, `data` as `deflate:`+base64. Write the file with
`json5_dumps` from `z2r_music_manager.py`. `primary_square_chan` and `uses` are guesses unless the user decides:
flag them. NSF limits: only non-bankswitched NSFs are supported by `nsfrip.py` (it raises otherwise); an NSF that
needs the init-time `$4015`/frame-counter state beyond "all channels enabled" may need `apusim.py` changes.

## Checks before finishing

- Decode every `data` (`ftplayer.decode_data`), confirm it is under 8192 bytes, and `ftplayer.Module(bytes)` parses it.
- Load the file with `Library(path)` from `z2r_music_manager.py` to confirm it parses.
- Render a song with `ftplayer.render_song` to confirm it plays (the app's Play button uses the same code).
- Do not invent music data. If only an NSF exists, say so and ask how to convert it.

The module byte format is the bhop driver's; see `ftplayer.py` for a working parser.
