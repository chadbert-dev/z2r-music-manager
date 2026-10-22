# Z2R Music Manager

A small GTK 4 app for managing [Zelda II Randomizer](https://github.com/Ellendar/Z2Randomizer) custom music libraries. It loads every `.json5` library in a folder, lets you pick which libraries to look through and which tracks are enabled, and saves copies of the libraries with the right `enabled` flags into another folder.

## Requirements

- Python 3
- PyGObject with GTK 4.10 or newer (needed for the folder picker and dialogs)

On Arch: `sudo pacman -S python-gobject gtk4`. On Debian/Ubuntu: `sudo apt install python3-gi gir1.2-gtk-4.0`.

There are no other dependencies. The app includes its own json5 reader and writer.

## Install

```sh
git clone <this repo> z2r-music-manager
cd z2r-music-manager
```

Optional desktop launcher:

1. Edit the `Exec=` line in `z2r-music-manager.desktop` so it points at `z2r_music_manager.py` in your clone.
2. `cp z2r-music-manager.desktop ~/.local/share/applications/`

## Usage

```sh
./z2r_music_manager.py [library_folder]
```

With no argument it reopens the folder you used last. Otherwise click **Open folder…**.

- **Libraries (left):** one checkbox per `.json5` file, with an `enabled/total` count. **All** and **None** tick or untick everything. The track list shows only ticked libraries. The panel can be collapsed with the sidebar button at the left of the header bar (or **F9**); the app remembers whether it was open.
- **Tracks (right):** each row has an on/off checkbox, title, author, uses, library and group. Click a column header to sort. The filter box matches title, author, uses and group. **Enable shown** and **Disable shown** act on whatever the filter currently displays.
- **Uses:** click a song's Uses cell to tick the Z2R categories it can be used for (Title, Overworld, Town, Encounter, Cave, Palace, GreatPalace, Boss, LastBoss, Credits, GameComplete, House, PalaceClear). A song always keeps at least one use. Songs with no uses of their own show their group's uses, or the Z2R default (Palace, GreatPalace) marked "(default)". **Add use…** and **Remove use…** apply one category to every track currently shown, so filter first. Use names the app doesn't know, such as `Refights`, are kept and can be unticked.
- **Target folder…** chooses where to save. **Save…** writes one file, `SelectedMusic.json5`, to that folder. It contains every enabled song from the ticked libraries; disabled songs and unticked libraries are left out, so the file is rebuilt from scratch each time and needs no cleanup. Group structure and your Uses edits are kept. It asks before replacing an existing `SelectedMusic.json5`, and warns if you close with unsaved changes. Other files in the target folder are never touched. If a `SelectedMusic.json5` sits in the folder you open, it is ignored rather than loaded as a library.

### Remembered settings

Your selection is saved automatically to `z2r-music-manager.settings.json` inside the music folder you opened, and restored the next time you open that folder. It records which libraries are ticked, the save target, and only the tracks whose on/off state or uses differ from their `.json5` file. Tracks are matched by group and title, so renaming a track in a library file drops its saved setting.

This file is separate from **Save…**: the settings file remembers your choices, while **Save…** writes the `SelectedMusic.json5` that Z2R reads. If the music folder is read-only, the app warns once and keeps working without saving settings.

## Library format

Libraries are the Z2R format: an object with `single` (a list of songs) and/or `groups` (each with an `items` list). A song's effective `enabled` is its own value, else its group's, else true. Exported songs have their `enabled` keys removed, since everything in `SelectedMusic.json5` is enabled. Fields the app doesn't use, such as `data` and `start_addr`, are written back unchanged.

## Limitations

- `SelectedMusic.json5` is written fresh in a tab-indented format with bare keys and trailing commas, so comments from the original library files are not carried over.
