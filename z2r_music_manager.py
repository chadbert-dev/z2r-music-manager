#!/usr/bin/env python3
"""Z2R Music Manager: browse json5 music libraries, toggle tracks, save copies.

Needs only python3 + PyGObject (GTK 4.10+). Run: ./z2r_music_manager.py [source_dir]
"""
import collections
import copy
import json
import os
import re
import sys

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GLib, GObject, Gtk  # noqa: E402

CONFIG = os.path.join(GLib.get_user_config_dir(), "z2r-music-manager.json")
SELECTED_NAME = "SelectedMusic.json5"  # Save output; skipped when loading so it never shows up as a library
SETTINGS_NAME = "z2r-music-manager.settings.json"  # lives in the music folder; not a .json5, so never loaded as a library


# ---------------------------------------------------------------- json5 ----
class Json5Error(ValueError):
    pass


class _Parser:
    ESC = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "v": "\v", "0": "\0"}

    def __init__(self, text):
        self.s = text.lstrip("﻿")
        self.i = 0

    def err(self, msg):
        line = self.s.count("\n", 0, self.i) + 1
        raise Json5Error(f"{msg} (line {line})")

    def ws(self):
        s = self.s
        while self.i < len(s):
            c = s[self.i]
            if c.isspace():
                self.i += 1
            elif s.startswith("//", self.i):
                j = s.find("\n", self.i)
                self.i = len(s) if j < 0 else j
            elif s.startswith("/*", self.i):
                j = s.find("*/", self.i + 2)
                if j < 0:
                    self.err("unterminated comment")
                self.i = j + 2
            else:
                break

    def parse(self):
        v = self.value()
        self.ws()
        if self.i != len(self.s):
            self.err("unexpected trailing data")
        return v

    def value(self):
        self.ws()
        if self.i >= len(self.s):
            self.err("unexpected end of file")
        c = self.s[self.i]
        if c == "{":
            return self.obj()
        if c == "[":
            return self.arr()
        if c in "\"'":
            return self.string()
        m = re.compile(r"[+-]?(0[xX][0-9a-fA-F]+|Infinity|NaN|(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?)").match(self.s, self.i)
        if m:
            self.i = m.end()
            t = m.group(0)
            if "x" in t.lower():
                return int(t, 16)
            if t.lstrip("+-") == "Infinity":
                return float(t)
            if t.lstrip("+-") == "NaN":
                return float("nan")
            return float(t) if re.search(r"[.eE]", t) else int(t)
        for word, val in (("true", True), ("false", False), ("null", None)):
            if self.s.startswith(word, self.i):
                self.i += len(word)
                return val
        self.err(f"unexpected character {c!r}")

    def string(self):
        q = self.s[self.i]
        self.i += 1
        out = []
        while True:
            if self.i >= len(self.s):
                self.err("unterminated string")
            c = self.s[self.i]
            self.i += 1
            if c == q:
                return "".join(out)
            if c == "\\":
                e = self.s[self.i]
                self.i += 1
                if e == "u":
                    out.append(chr(int(self.s[self.i:self.i + 4], 16)))
                    self.i += 4
                elif e == "x":
                    out.append(chr(int(self.s[self.i:self.i + 2], 16)))
                    self.i += 2
                elif e == "\n":
                    pass
                else:
                    out.append(self.ESC.get(e, e))
            else:
                out.append(c)

    def key(self):
        if self.s[self.i] in "\"'":
            return self.string()
        m = re.compile(r"[A-Za-z_$][\w$]*").match(self.s, self.i)
        if not m:
            self.err("expected a key")
        self.i = m.end()
        return m.group(0)

    def obj(self):
        self.i += 1
        d = {}
        while True:
            self.ws()
            if self.s[self.i:self.i + 1] == "}":
                self.i += 1
                return d
            k = self.key()
            self.ws()
            if self.s[self.i:self.i + 1] != ":":
                self.err("expected ':'")
            self.i += 1
            d[k] = self.value()
            self.ws()
            c = self.s[self.i:self.i + 1]
            if c == ",":
                self.i += 1
            elif c != "}":
                self.err("expected ',' or '}'")

    def arr(self):
        self.i += 1
        a = []
        while True:
            self.ws()
            if self.s[self.i:self.i + 1] == "]":
                self.i += 1
                return a
            a.append(self.value())
            self.ws()
            c = self.s[self.i:self.i + 1]
            if c == ",":
                self.i += 1
            elif c != "]":
                self.err("expected ',' or ']'")


def json5_loads(text):
    return _Parser(text).parse()


_IDENT = re.compile(r"^[A-Za-z_$][\w$]*$")


def json5_dumps(v, depth=0):
    """Tab-indented json5: bare keys, trailing commas, short lists inline."""
    pad, pad1 = "\t" * depth, "\t" * (depth + 1)
    if isinstance(v, dict):
        if not v:
            return "{}"
        rows = [f"{pad1}{k if _IDENT.match(k) else json.dumps(k)}: {json5_dumps(x, depth + 1)},"
                for k, x in v.items()]
        return "{\n" + "\n".join(rows) + f"\n{pad}}}"
    if isinstance(v, list):
        if not v:
            return "[]"
        if all(not isinstance(x, (dict, list)) for x in v):
            return "[" + ", ".join(json5_dumps(x) for x in v) + "]"
        rows = [f"{pad1}{json5_dumps(x, depth + 1)}," for x in v]
        return "[\n" + "\n".join(rows) + f"\n{pad}]"
    return json.dumps(v, ensure_ascii=False)


# ---------------------------------------------------------------- model ----
# Z2R's `Usage` enum (RandomizerCore/Music.cs). Songs with no uses of their own inherit their
# group's uses, else the defaults below.
USES = ["Title", "Overworld", "Town", "Encounter", "Cave", "Palace", "GreatPalace", "Boss", "LastBoss",
        "Credits", "GameComplete", "House", "PalaceClear"]
DEFAULT_USES = ["Palace", "GreatPalace"]


def canon_uses(items):
    """Dedupe case-insensitively; Z2R categories first (canonical spelling/order), unknown ones kept after."""
    known = {u.lower(): u for u in USES}
    seen, ours, extra = set(), [], []
    for u in items:
        k = str(u).lower()
        if k not in seen:
            seen.add(k)
            (ours if k in known else extra).append(known.get(k, str(u)))
    return sorted(ours, key=USES.index) + extra


class Track(GObject.Object):
    enabled = GObject.Property(type=bool, default=True)
    title = GObject.Property(type=str, default="")
    author = GObject.Property(type=str, default="")
    uses = GObject.Property(type=str, default="")
    library = GObject.Property(type=str, default="")
    group = GObject.Property(type=str, default="")

    def __init__(self, raw, parent_raw, library):
        super().__init__()
        self.raw, self.parent_raw = raw, parent_raw
        self.library_obj = library
        eff = raw.get("enabled", (parent_raw or {}).get("enabled", True))
        src = raw.get("uses", (parent_raw or {}).get("uses"))
        self.orig_default = not (isinstance(src, list) and src)  # no uses anywhere: Z2R falls back to defaults
        self.orig_uses = canon_uses(DEFAULT_USES if self.orig_default else src)
        self.use_list = list(self.orig_uses)
        self.props.enabled = bool(eff)
        self.props.title = str(raw.get("title", ""))
        self.props.author = str(raw.get("author", (parent_raw or {}).get("author", "")) or "")
        self.props.uses = self.uses_text()
        self.props.library = library.name
        self.props.group = str((parent_raw or {}).get("title", ""))
        self.orig_enabled = self.props.enabled  # state in the file on disk
        self.key = ""  # stable id within the library, set by Library

    @property
    def uses_changed(self):
        return self.use_list != self.orig_uses

    def uses_text(self):
        return ", ".join(self.use_list) + (" (default)" if self.orig_default and not self.uses_changed else "")

    def set_use_list(self, items):
        self.use_list = canon_uses(items)
        self.props.uses = self.uses_text()


class Library:
    def __init__(self, path):
        self.path = path
        self.name = os.path.basename(path)
        with open(path, encoding="utf-8") as f:
            self.data = json5_loads(f.read())
        if not isinstance(self.data, dict):
            raise Json5Error("top level is not an object")
        self.tracks = [Track(r, None, self) for r in self.data.get("single", [])]
        for g in self.data.get("groups", []):
            self.tracks += [Track(r, g, self) for r in g.get("items", [])]
        if not self.tracks and "single" not in self.data and "groups" not in self.data:
            raise Json5Error("no 'single' or 'groups' (not a music library)")
        self.selected = True
        seen = collections.Counter()
        for t in self.tracks:
            base = f"{t.props.group}|{t.props.title}"
            seen[base] += 1
            t.key = base if seen[base] == 1 else f"{base}#{seen[base]}"

    def enabled_count(self):
        return sum(t.props.enabled for t in self.tracks)

    def export_enabled(self):
        """(singles, groups) holding only enabled tracks, with edited uses applied. Does not modify the library."""
        enabled = {id(t.raw): t for t in self.tracks if t.props.enabled}

        def prep(raw):
            r = copy.deepcopy(raw)
            r.pop("enabled", None)  # everything exported is enabled; a stale false must not veto it
            t = enabled[id(raw)]
            if t.uses_changed:
                r["uses"] = list(t.use_list)
            return r

        singles = [prep(r) for r in self.data.get("single", []) if id(r) in enabled]
        groups = []
        for g in self.data.get("groups", []):
            items = [prep(r) for r in g.get("items", []) if id(r) in enabled]
            if items:
                ng = {k: v for k, v in g.items() if k not in ("items", "enabled")}
                ng["items"] = items
                groups.append(ng)
        return singles, groups


# ------------------------------------------------------------- settings ----
def settings_dump(libs, target):
    """Selection state to persist: ticked libraries and tracks that differ from their file."""
    return {
        "version": 1,
        "target": target,
        "libraries": {
            lib.name: {
                "selected": lib.selected,
                "tracks": {t.key: bool(t.props.enabled) for t in lib.tracks if bool(t.props.enabled) != t.orig_enabled},
                "uses": {t.key: list(t.use_list) for t in lib.tracks if t.uses_changed},
            } for lib in libs
        },
    }


def settings_apply(libs, data):
    """Apply saved settings to freshly loaded libraries. Unknown libraries/tracks are ignored."""
    saved = data.get("libraries", {}) if isinstance(data, dict) else {}
    for lib in libs:
        ls = saved.get(lib.name)
        if not isinstance(ls, dict):
            continue
        lib.selected = bool(ls.get("selected", True))
        over, uses = ls.get("tracks", {}), ls.get("uses", {})
        for t in lib.tracks:
            if t.key in over:
                t.props.enabled = bool(over[t.key])
            if isinstance(uses.get(t.key), list):
                t.set_use_list(uses[t.key])


# ------------------------------------------------------------------ gui ----
def load_config():
    try:
        with open(CONFIG) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    try:
        os.makedirs(os.path.dirname(CONFIG), exist_ok=True)
        with open(CONFIG, "w") as f:
            json.dump(cfg, f)
    except OSError:
        pass


class Window(Gtk.ApplicationWindow):
    def __init__(self, app, source):
        super().__init__(application=app, title="Z2R Music Manager", default_width=1100, default_height=700)
        self.cfg = load_config()
        self.libs = []
        self.source = None
        self.target = self.cfg.get("target")
        self.dirty = False
        self.save_id = None
        self.bulk = False  # True while a bulk change runs; per-item handlers skip their refresh
        self.store = Gio.ListStore.new(Track)
        self.filter = Gtk.CustomFilter.new(self.match)
        self.filtered = Gtk.FilterListModel(model=self.store, filter=self.filter)

        # header bar
        hb = Gtk.HeaderBar()
        self.set_titlebar(hb)
        self.side_btn = Gtk.ToggleButton(icon_name="sidebar-show-symbolic", tooltip_text="Show/hide libraries (F9)",
                                         active=self.cfg.get("sidebar", True))
        self.side_btn.connect("toggled", self.on_side_toggled)
        hb.pack_start(self.side_btn)
        b = Gtk.Button(label="Open folder…")
        b.connect("clicked", lambda *_: self.pick_folder("Library folder", self.set_source, self.source))
        hb.pack_start(b)
        self.src_label = Gtk.Label(label="No folder loaded", xalign=0, ellipsize=3)
        hb.pack_start(self.src_label)
        self.save_btn = Gtk.Button(label="Save…", css_classes=["suggested-action"],
                                  tooltip_text="Write all enabled songs from the ticked libraries to SelectedMusic.json5 in the target folder")
        self.save_btn.connect("clicked", self.on_save)
        hb.pack_end(self.save_btn)
        self.tgt_btn = Gtk.Button(label="Target folder…")
        self.tgt_btn.connect("clicked", lambda *_: self.pick_folder("Save to folder", self.set_target, self.target))
        hb.pack_end(self.tgt_btn)

        # sidebar
        self.lib_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["navigation-sidebar"])
        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        side.append(Gtk.Label(label="Libraries", xalign=0, margin_start=12, margin_top=8, margin_bottom=4,
                              css_classes=["heading"]))
        row = Gtk.Box(spacing=6, margin_start=8, margin_end=8, margin_bottom=4)
        for text, val in (("All", True), ("None", False)):
            bb = Gtk.Button(label=text)
            bb.connect("clicked", lambda _b, v=val: self.select_libs(v))
            row.append(bb)
        side.append(row)
        sw = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        sw.set_child(self.lib_list)
        side.append(sw)
        side.set_size_request(260, -1)
        self.side = side
        side.set_visible(self.side_btn.get_active())

        # track table
        self.search = Gtk.SearchEntry(placeholder_text="Filter by title, author or use…", hexpand=True)
        self.search.connect("search-changed", lambda *_: self.refilter())
        tools = Gtk.Box(spacing=6, margin_start=8, margin_end=8, margin_top=8, margin_bottom=8)
        tools.append(self.search)
        for text, val in (("Enable shown", True), ("Disable shown", False)):
            bb = Gtk.Button(label=text)
            bb.connect("clicked", lambda _b, v=val: self.set_shown(v))
            tools.append(bb)
        tools.append(self.bulk_uses_menu("Add use…", True))
        tools.append(self.bulk_uses_menu("Remove use…", False))

        self.view = Gtk.ColumnView(show_row_separators=True, show_column_separators=True)
        self.view.set_model(Gtk.NoSelection(model=self.filtered))
        self.add_check_column()
        self.add_text_column("Title", "title", 340)
        self.add_text_column("Author", "author", 140)
        self.add_uses_column(220)
        self.add_text_column("Library", "library", 160)
        self.add_text_column("Group", "group", 140)
        vsw = Gtk.ScrolledWindow(vexpand=True, hexpand=True)
        vsw.set_child(self.view)
        self.status = Gtk.Label(xalign=0, margin_start=8, margin_end=8, margin_top=4, margin_bottom=4)
        main = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        for w in (tools, vsw, self.status):
            main.append(w)

        paned = Gtk.Paned(start_child=side, end_child=main, position=270, shrink_start_child=False)
        self.set_child(paned)
        keys = Gtk.ShortcutController(scope=Gtk.ShortcutScope.GLOBAL)
        keys.add_shortcut(Gtk.Shortcut(trigger=Gtk.ShortcutTrigger.parse_string("F9"),
                                       action=Gtk.CallbackAction.new(lambda *_: self.side_btn.set_active(
                                           not self.side_btn.get_active()) or True)))
        self.add_controller(keys)
        self.connect("close-request", self.on_close)

        src = source or self.cfg.get("source")
        if src and os.path.isdir(src):
            self.set_source(src)
        else:
            self.update_status()

    # -- columns
    def add_check_column(self):
        f = Gtk.SignalListItemFactory()

        def setup(_f, li):
            li.set_child(Gtk.CheckButton(halign=Gtk.Align.CENTER))

        def bind(_f, li):
            cb, tr = li.get_child(), li.get_item()
            cb.set_active(tr.props.enabled)
            h1 = cb.connect("toggled", lambda c: tr.set_property("enabled", c.get_active()))
            h2 = tr.connect("notify::enabled", lambda t, _p: cb.set_active(t.props.enabled))
            li.handlers = (cb, h1, tr, h2)

        def unbind(_f, li):
            cb, h1, tr, h2 = li.handlers
            cb.disconnect(h1)
            tr.disconnect(h2)

        f.connect("setup", setup)
        f.connect("bind", bind)
        f.connect("unbind", unbind)
        self.view.append_column(Gtk.ColumnViewColumn(title="On", factory=f, fixed_width=50))

    def add_uses_column(self, width):
        f = Gtk.SignalListItemFactory()

        def setup(_f, li):
            mb = Gtk.MenuButton(has_frame=False, hexpand=True, tooltip_text="Click to edit this song's uses")
            mb.set_child(Gtk.Label(xalign=0, ellipsize=3))
            mb.set_create_popup_func(lambda b, _d: b.set_popover(self.build_uses_popover(b.track)), None)
            li.set_child(mb)

        def bind(_f, li):
            mb, tr = li.get_child(), li.get_item()
            mb.track = tr
            mb.set_popover(None)
            lab = mb.get_child()
            lab.set_label(tr.props.uses)
            li.handler = (tr, tr.connect("notify::uses", lambda t, _p: lab.set_label(t.props.uses)))

        def unbind(_f, li):
            tr, h = li.handler
            tr.disconnect(h)

        f.connect("setup", setup)
        f.connect("bind", bind)
        f.connect("unbind", unbind)
        col = Gtk.ColumnViewColumn(title="Uses", factory=f, resizable=True, fixed_width=width)
        col.set_sorter(Gtk.CustomSorter.new(lambda a, b, _d: (a.props.uses.lower() > b.props.uses.lower())
                                            - (a.props.uses.lower() < b.props.uses.lower())))
        self.view.append_column(col)

    def build_uses_popover(self, tr):
        pop = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin_start=10, margin_end=10,
                      margin_top=8, margin_bottom=8)
        box.append(Gtk.Label(label="Uses", xalign=0, css_classes=["heading"]))
        checks = {}

        def refresh():
            on = [n for n, c in checks.items() if c.get_active()]
            for c in checks.values():  # a song always keeps at least one use
                c.set_sensitive(not (len(on) == 1 and c.get_active()))

        def toggled(_c, _n):
            tr.set_use_list([n for n, c in checks.items() if c.get_active()])
            refresh()

        for n in USES + [u for u in tr.use_list if u not in USES]:
            c = Gtk.CheckButton(label=n, active=n in tr.use_list)
            c.connect("toggled", toggled, n)
            checks[n] = c
            box.append(c)
        refresh()
        pop.set_child(box)
        return pop

    def bulk_uses_menu(self, label, add):
        pop = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, margin_start=6, margin_end=6, margin_top=6, margin_bottom=6)
        for n in USES:
            b = Gtk.Button(label=n, has_frame=False, halign=Gtk.Align.FILL)
            b.get_child().set_xalign(0)
            b.connect("clicked", lambda _b, n=n: (pop.popdown(), self.bulk_uses(n, add)))
            box.append(b)
        pop.set_child(box)
        return Gtk.MenuButton(label=label, popover=pop,
                              tooltip_text="Applies to every track currently shown (respects the filter)")

    def bulk_uses(self, name, add):
        skipped = 0
        self.bulk = True
        try:
            for t in self.shown():
                have = name in t.use_list
                if add and not have:
                    t.set_use_list(t.use_list + [name])
                elif not add and have:
                    if len(t.use_list) == 1:  # would leave the song with no uses
                        skipped += 1
                    else:
                        t.set_use_list([u for u in t.use_list if u != name])
        finally:
            self.bulk = False
        self.dirty = True
        self.schedule_settings()
        self.refilter()
        if skipped:
            self.alert("Some songs were skipped", f"{skipped} song(s) have {name} as their only use, so it was kept.")

    def add_text_column(self, title, prop, width):
        f = Gtk.SignalListItemFactory()
        f.connect("setup", lambda _f, li: li.set_child(
            Gtk.Label(xalign=0, ellipsize=3, margin_start=6, margin_end=6, margin_top=3, margin_bottom=3)))

        def bind(_f, li):
            lab, tr = li.get_child(), li.get_item()
            lab.set_label(tr.get_property(prop))
            lab.set_tooltip_text(tr.get_property(prop))

        f.connect("bind", bind)
        col = Gtk.ColumnViewColumn(title=title, factory=f, resizable=True, fixed_width=width)
        col.set_sorter(Gtk.CustomSorter.new(lambda a, b, _d: (a.get_property(prop).lower() > b.get_property(prop).lower())
                                            - (a.get_property(prop).lower() < b.get_property(prop).lower())))
        self.view.append_column(col)

    # -- logic
    def match(self, tr, *_):
        if not tr.library_obj.selected:
            return False
        q = self.search.get_text().strip().lower()
        return not q or q in " ".join((tr.props.title, tr.props.author, tr.props.uses, tr.props.group)).lower()

    def refilter(self):
        self.filter.changed(Gtk.FilterChange.DIFFERENT)
        self.update_status()

    def shown(self):
        return [self.filtered.get_item(i) for i in range(self.filtered.get_n_items())]

    def set_shown(self, val):
        self.bulk = True
        try:
            for t in self.shown():
                t.props.enabled = val
        finally:
            self.bulk = False
        self.dirty = True
        self.schedule_settings()
        self.update_status()

    def select_libs(self, val):
        self.bulk = True
        try:
            for lib, cb in self.lib_checks:
                cb.set_active(val)
        finally:
            self.bulk = False
        self.schedule_settings()
        self.refilter()

    def update_status(self):
        sel = [l for l in self.libs if l.selected]
        on = sum(l.enabled_count() for l in sel)
        tot = sum(len(l.tracks) for l in sel)
        tgt = self.target or "(not chosen)"
        self.status.set_label(f"{len(sel)}/{len(self.libs)} libraries · {self.filtered.get_n_items()} tracks shown · "
                              f"{on}/{tot} enabled · save target: {tgt}")
        for lib, cb in getattr(self, "lib_checks", []):
            cb.set_label(f"{lib.name}  ({lib.enabled_count()}/{len(lib.tracks)})")

    def set_source(self, path):
        self.flush_settings()
        self.source = path
        self.cfg["source"] = path
        save_config(self.cfg)
        self.libs, errors = [], []
        self.store.remove_all()
        for name in sorted(os.listdir(path), key=str.lower):
            if name.lower().endswith(".json5") and name != SELECTED_NAME:
                try:
                    self.libs.append(Library(os.path.join(path, name)))
                except (OSError, ValueError, KeyError, IndexError) as e:
                    errors.append(f"{name}: {e}")
        restored = False
        try:
            with open(os.path.join(path, SETTINGS_NAME), encoding="utf-8") as f:
                saved = json.load(f)
            settings_apply(self.libs, saved)
            if isinstance(saved, dict) and saved.get("target"):
                self.target = saved["target"]
            restored = True
        except (OSError, ValueError):
            pass
        while (r := self.lib_list.get_first_child()):
            self.lib_list.remove(r)
        self.lib_checks = []
        for lib in self.libs:
            cb = Gtk.CheckButton(active=lib.selected, margin_start=8, margin_top=4, margin_bottom=4)
            cb.connect("toggled", lambda c, l=lib: self.on_lib_toggled(l, c))
            self.lib_list.append(cb)
            self.lib_checks.append((lib, cb))
            for t in lib.tracks:
                t.connect("notify::enabled", self.on_track_toggled)
                t.connect("notify::uses", self.on_track_toggled)
                self.store.append(t)
        self.src_label.set_label(path + ("  (settings restored)" if restored else ""))
        self.dirty = any(t.props.enabled != t.orig_enabled or t.uses_changed for l in self.libs for t in l.tracks)
        self.refilter()
        if errors:
            self.alert("Some files were skipped", "\n".join(errors))
        elif not self.libs:
            self.alert("No libraries found", f"No .json5 files in {path}")

    def on_side_toggled(self, btn):
        self.side.set_visible(btn.get_active())
        self.cfg["sidebar"] = btn.get_active()
        save_config(self.cfg)

    def on_lib_toggled(self, lib, cb):
        lib.selected = cb.get_active()
        if self.bulk:
            return
        self.schedule_settings()
        self.refilter()

    def on_track_toggled(self, *_):
        if self.bulk:
            return
        self.dirty = True
        self.schedule_settings()
        self.update_status()

    # -- persisted selection (debounced; written next to the music files)
    def schedule_settings(self):
        if self.save_id is None and self.source:
            self.save_id = GLib.timeout_add(500, self.flush_settings)

    def flush_settings(self):
        if self.save_id is not None:
            GLib.source_remove(self.save_id)
        self.save_id = None
        if not self.source or not self.libs:
            return False
        path = os.path.join(self.source, SETTINGS_NAME)
        try:
            with open(path + ".tmp", "w", encoding="utf-8") as f:
                json.dump(settings_dump(self.libs, self.target), f, indent=1)
            os.replace(path + ".tmp", path)
        except OSError as e:
            if not getattr(self, "warned_settings", False):
                self.warned_settings = True
                self.alert("Couldn't save settings", f"{path}\n{e}")
        return False  # one-shot timer

    def set_target(self, path):
        self.target = path
        self.cfg["target"] = path
        save_config(self.cfg)
        self.schedule_settings()
        self.update_status()

    def on_save(self, *_):
        sel = [l for l in self.libs if l.selected]
        if not sel:
            return self.alert("Nothing to save", "Tick at least one library.")
        if not any(t.props.enabled for l in sel for t in l.tracks):
            return self.alert("Nothing to save", "No songs are enabled in the ticked libraries.")
        if not self.target:
            return self.pick_folder("Save to folder", lambda p: (self.set_target(p), self.on_save()), None)
        out = os.path.join(self.target, SELECTED_NAME)
        if os.path.exists(out):
            d = Gtk.AlertDialog(message="Replace SelectedMusic.json5?", detail=f"{out} already exists.",
                                buttons=["Cancel", "Replace"], cancel_button=0, default_button=0)
            d.choose(self, None, lambda dlg, res: dlg.choose_finish(res) == 1 and self.do_save(sel))
        else:
            self.do_save(sel)

    def do_save(self, sel):
        singles, groups, libs = [], [], 0
        for lib in sel:
            sg, gr = lib.export_enabled()
            if sg or gr:
                libs += 1
            singles += sg
            groups += gr
        count = len(singles) + sum(len(g["items"]) for g in groups)
        data = {}
        if singles:
            data["single"] = singles
        if groups:
            data["groups"] = groups
        out = os.path.join(self.target, SELECTED_NAME)
        try:
            os.makedirs(self.target, exist_ok=True)
            with open(out + ".tmp", "w", encoding="utf-8") as f:
                f.write(f"// Generated by Z2R Music Manager: {count} songs from {libs} librar{'y' if libs == 1 else 'ies'}\n")
                f.write(json5_dumps(data) + "\n")
            os.replace(out + ".tmp", out)
        except OSError as e:
            return self.alert("Save failed", str(e))
        self.dirty = False
        self.alert("Saved", f"Wrote {count} songs from {libs} librar{'y' if libs == 1 else 'ies'} to {out}")

    def on_close(self, *_):
        self.flush_settings()
        if not self.dirty:
            return False
        d = Gtk.AlertDialog(message="Discard unsaved changes?", buttons=["Cancel", "Discard"],
                            cancel_button=0, default_button=0)
        d.choose(self, None, lambda dlg, res: dlg.choose_finish(res) == 1 and self.destroy())
        return True

    # -- helpers
    def pick_folder(self, title, cb, initial):
        d = Gtk.FileDialog(title=title)
        if initial and os.path.isdir(initial):
            d.set_initial_folder(Gio.File.new_for_path(initial))

        def done(dlg, res):
            try:
                f = dlg.select_folder_finish(res)
            except GLib.Error:
                return
            if f:
                cb(f.get_path())

        d.select_folder(self, None, done)

    def alert(self, msg, detail=""):
        Gtk.AlertDialog(message=msg, detail=detail).show(self)


class App(Gtk.Application):
    def __init__(self, source):
        super().__init__(application_id="local.z2r.musicmanager")
        self.source = source

    def do_activate(self):
        win = self.props.active_window or Window(self, self.source)
        win.present()


if __name__ == "__main__":
    src = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else None
    sys.exit(App(src).run([sys.argv[0]]))
