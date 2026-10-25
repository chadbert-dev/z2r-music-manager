# What happened while you were away

Everything below is on disk now. **Nothing is committed** (the last commit is `f037d86`); `git status` shows the changes.
I could not listen to any audio, so none of the converted music has been heard by anyone.

## 1. Music conversion

All five games were converted with the repo's own converter (`tools/nsf_to_library.py`, built on `nsf2bhop.py`):
emulate the NSF, work out what each channel plays on every frame, re-encode as a Z2R (bhop) module. Each song is
checked by playing the module through `ftplayer.py` and comparing every audible frame with the NSF
(volume, pitch, duty). That proves agreement with *my port* of the bhop driver, not with Z2R itself.

| Library file (in `~/Work/FtSoundTracks/`) | Tracks | Usable (enabled) | Notes |
|---|---|---|---|
| `Castlevania.json5` | 15 | 13 | all frame-exact; Death and Game Over disabled (not useful as area music) |
| `Mega Man 2.json5` (own file, as asked) | 22 | 19 | 21 frame-exact; **Dr. Wily's Castle is 8,257 B (limit 8,192)** so it is disabled; Enemy Chosen and Dr. Wily's Map are short jingles, disabled |
| `Castlevania II - Simon's Quest.json5` | 9 | 7 | **uses DPCM drums, which are dropped** (Z2R does not support DPCM); 4 songs also hit the instrument cap (see below); *A Requiem* is 8,928 B, disabled; Game Over disabled |
| `Battletoads.json5` | 3 of 21 | 0 | no m3u, so titles are just "Track NN"; only 3 tracks fit in 8 KB and all three end (disabled). The other 18 are 10-27 KB and were left out |
| `Wizards & Warriors.json5` | 1 | 1 | from earlier: title track only |

Per-track sizes are all under 8 KB except the ones named above. Largest kept: Castlevania *Heart of Fire* 6.8 KB,
Mega Man 2 *Credits* 7.1 KB, *Crash Man* 7.4 KB.

### Things you should know / check
- **`primary_square_chan` is a guess (1) for every song**, and `streaming_safe` is false. Mute each square in turn to decide.
- **`uses` are my guesses from the titles** (e.g. MM2 Boss Battle -> Boss, Castlevania *Black Night* -> LastBoss, *Voyager* -> Credits,
  CV2 *Bloody Tears* -> Overworld, *Silence of the Daylight* -> Town). Stage songs keep the default Palace/GreatPalace. Change freely.
  Jingles for Title/Credits/GameComplete/PalaceClear are enabled even though they end (the song halts when finished).
- **Instrument cap:** a pattern can address 127 instruments. Songs needing more were *approximated* by merging the rarest
  instruments into similar ones. Affected: CV2 tracks 1, 3, 4, 9 and most Battletoads tracks (slightly different envelopes, not different notes).
  Castlevania *Heart of Fire* uses 126, close to the cap. FamiTracker itself only allows 64 instruments per module, so Z2R's loader
  might be stricter than the driver; **worth testing in Z2R**.
- **Loops:** loop points are detected automatically from the audio state and written as a jump. For Heat Man a few start-up
  frames differ from the loop and were taken from the loop pass instead. Several songs (Castlevania *Voyager*, MM2 Introduction, Title Screen, Epilogue, Credits)
  simply end (halt).
- **Loop-point accuracy** can't be heard-checked: a bad detection would show up as a hiccup at the loop.
- Mega Man 2 title prefixes ("Mega Man II - Ogeretsu Kun, ...") were stripped; the author is the m3u's composer list. CV1 author "Club Kukeiha",
  CV2 "Kenichi Matsubara, Satoe Terashima" and Battletoads "David Wise" come from my memory/the m3u, not verified.
- Earlier in the day I wrongly said Dn-FamiTracker could open NSFs; it cannot. That is fixed in the skill and memory.

### Converter changes made along the way (all in `tools/` plus `ftplayer.py`)
- 6502 + APU emulator now handles **bank-switched NSFs** (Mega Man 2 needed it).
- Notes are detected from the audio itself (volume going back up, big pitch jumps), not only length-register writes, because
  Konami/Capcom drive volume in software.
- Frame-exact **loop detection**, jump/halt/skip commands carried in the unused DPCM channel stream, patterns split only where they cannot cut a
  note (or the note is re-cut), long sequences split at 250 steps, most-used instruments get the cheap 1-byte numbers.
- `ftplayer.py`: first-row timing fix (earlier), and it now also reads the DPCM channel stream so jump/skip/halt commands work in playback.
  I replayed ~200 random existing library songs with it: no errors.

## 2. Stats panel (your request)
Under the libraries list there is now an **"Enabled songs per use"** panel: all 13 uses with a count of enabled songs in the *ticked*
libraries (the same set Save writes). Any use with 0 is shown in red with a tooltip. The libraries list shrank to make room (it keeps a
minimum height and scrolls). It updates when you toggle songs, uses or libraries. Code: `build_stats` / `update_use_stats` in `z2r_music_manager.py`.
With your saved selection right now House and PalaceClear are the two red ones.

## 3. Not done / open
- Dr. Wily's Castle (MM2) is 65 bytes over; Battletoads needs a different (lossy) approach to fit.
- Nothing committed. Files changed: `ftplayer.py`, `z2r_music_manager.py`, `tools/*` (new `nsf_to_library.py`), skill doc updated.
- The 3,900-song regression sweep from earlier never completed; I replaced it with a 200-song random sample (passed).
