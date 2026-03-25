#!/usr/bin/env python3
"""
VIAL RGB control library for KEEBMONKEY Megalodon (DOIO KB16 rev2).

Protocol: VIA Raw HID over usage_page=0xff60 / usage=0x0061
  id_lighting_get_value = 0x08  →  vialrgb_get_value()
  id_lighting_set_value = 0x07  →  vialrgb_set_value()

VialRGB GET sub-commands:
  0x40  get_info        → [protocol_ver_lo, ver_hi, max_brightness, ...]
  0x41  get_mode        → [_, _, effect_lo, effect_hi, speed, hue, sat, val, ...]
  0x43  get_number_leds → [_, _, count_lo, count_hi, ...]

VialRGB SET sub-commands:
  0x41  set_mode        args: [effect_lo, effect_hi, speed, hue, sat, val]
  0x42  direct_fastset  args: [first_led_lo, first_led_hi, num_leds, H,S,V, ...]
                             max 9 LEDs per packet

Requires VIAL firmware compiled with ENABLE_RGB_MATRIX_VIALRGB_DIRECT = yes.

LED layout (one LED per key, row-major):
  LED  0  LED  1  LED  2  LED  3   ← row 0 (top)
  LED  4  LED  5  LED  6  LED  7   ← row 1
  LED  8  LED  9  LED 10  LED 11   ← row 2
  LED 12  LED 13  LED 14  LED 15   ← row 3 (bottom)

Hue scale: 0=red, 85=green, 170=blue  (0-255 wraps around)
"""

import argparse
import json
import os
import signal
import sys
import time

import hid

VID = 0xD010
PID = 0x1601
USAGE_PAGE = 0xFF60
USAGE = 0x0061
RAW_EPSIZE = 32

LED_COUNT = 16
LEDS_PER_PACKET = 9  # (32 - 1(cmd) - 1(sub) - 2(idx) - 1(num)) / 3 = 9

# VIA command IDs
ID_LIGHTING_SET_VALUE = 0x07
ID_LIGHTING_GET_VALUE = 0x08
ID_DYNAMIC_KEYMAP_GET_KEYCODE = 0x04
ID_DYNAMIC_KEYMAP_SET_KEYCODE = 0x05

# Common keycodes (QMK / HID usage IDs)
KC = {
    "KC_NO": 0x0000,
    "KC_TRNS": 0x0001,
    "F1": 0x003A,
    "F2": 0x003B,
    "F3": 0x003C,
    "F4": 0x003D,
    "F5": 0x003E,
    "F6": 0x003F,
    "F7": 0x0040,
    "F8": 0x0041,
    "F9": 0x0042,
    "F10": 0x0043,
    "F11": 0x0044,
    "F12": 0x0045,
    "F13": 0x0068,
    "F14": 0x0069,
    "F15": 0x006A,
    "F16": 0x006B,
    "F17": 0x006C,
    "F18": 0x006D,
    "F19": 0x006E,
    "F20": 0x006F,
    "F21": 0x0070,
    "F22": 0x0071,
    "F23": 0x0072,
    "F24": 0x0073,
    "MUTE": 0x00E2,
    "VOLU": 0x00E9,
    "VOLD": 0x00EA,
    "MEDIA_PLAY": 0x00CD,
    "MEDIA_NEXT": 0x00B5,
    "MEDIA_PREV": 0x00B6,
}

# VialRGB GET sub-commands
VIALRGB_GET_INFO = 0x40
VIALRGB_GET_MODE = 0x41
VIALRGB_GET_SUPPORTED = 0x42
VIALRGB_GET_NUMBER_LEDS = 0x43

# VialRGB SET sub-commands
VIALRGB_SET_MODE = 0x41
VIALRGB_DIRECT_FASTSET = 0x42

# Effect IDs
VIALRGB_EFFECT_OFF = 0
VIALRGB_EFFECT_DIRECT = 1

DAEMON_PID_FILE = "/tmp/megalodon_alarm_daemon.pid"
SLOTS_DIR = "/tmp/megalodon_slots"
DAEMON_POLL_INTERVAL = 0.05  # seconds per render tick (~20 fps)
DAEMON_IDLE_TIMEOUT = 1.0  # seconds after last slot removed before auto-exit
LABEL_FILE = os.path.expanduser("~/.config/megalodon_colors.json")

# Human-readable color name → (hue, sat) on the 0-255 HSV scale
# hue: 0=red, 43=yellow, 85=green, 128=cyan, 170=blue, 213=purple
COLOR_NAMES = {
    "red": (0, 255),
    "orange": (21, 255),
    "yellow": (43, 255),
    "green": (85, 255),
    "cyan": (128, 255),
    "blue": (170, 255),
    "purple": (213, 255),
    "magenta": (213, 255),
    "pink": (234, 200),
    "white": (0, 0),
    "off": None,
}


# ---------------------------------------------------------------------------
# Low-level HID helpers
# ---------------------------------------------------------------------------


def open_device():
    """Open the VIA/VIAL Raw HID interface. Raises RuntimeError if not found."""
    for dev in hid.enumerate(VID, PID):
        if dev["usage_page"] == USAGE_PAGE and dev["usage"] == USAGE:
            h = hid.device()
            h.open_path(dev["path"])
            h.set_nonblocking(True)
            return h
    raise RuntimeError(
        f"Megalodon not found — is it plugged in? "
        f"(looking for VID={VID:#06x} PID={PID:#06x} usage_page={USAGE_PAGE:#06x})"
    )


def _send(h, data: list):
    """Send a 32-byte Raw HID packet (prepend 0x00 report ID for hidapi)."""
    pkt = [0x00] + list(data)[:RAW_EPSIZE]
    pkt += [0x00] * (RAW_EPSIZE + 1 - len(pkt))
    h.write(pkt)


def _recv(h, timeout_ms=200):
    """Read a 32-byte response."""
    h.set_nonblocking(False)
    r = h.read(RAW_EPSIZE, timeout_ms)
    h.set_nonblocking(True)
    return r


# ---------------------------------------------------------------------------
# VialRGB commands
# ---------------------------------------------------------------------------


def get_mode(h):
    """Return current (effect_id, speed, hue, sat, val)."""
    _send(h, [ID_LIGHTING_GET_VALUE, VIALRGB_GET_MODE])
    r = _recv(h)
    effect = r[2] | (r[3] << 8)
    return effect, r[4], r[5], r[6], r[7]


def set_mode(h, effect=VIALRGB_EFFECT_DIRECT, speed=128, hue=0, sat=255, val=200):
    """Switch the RGB matrix to the given VialRGB effect mode."""
    _send(
        h,
        [
            ID_LIGHTING_SET_VALUE,
            VIALRGB_SET_MODE,
            effect & 0xFF,
            (effect >> 8) & 0xFF,
            speed & 0xFF,
            hue & 0xFF,
            sat & 0xFF,
            val & 0xFF,
        ],
    )


def set_leds(h, colors: list):
    """
    Set all 16 LEDs via direct_fastset (requires DIRECT mode active).

    colors: list of 16 (H, S, V) tuples
      H 0-255 (0=red, 85=green, 170=blue)
      S 0-255 (255=fully saturated)
      V 0-255 (0=off, 255=max brightness)
    """
    for start in range(0, LED_COUNT, LEDS_PER_PACKET):
        batch = colors[start : start + LEDS_PER_PACKET]
        n = len(batch)
        data = [
            ID_LIGHTING_SET_VALUE,
            VIALRGB_DIRECT_FASTSET,
            start & 0xFF,
            (start >> 8) & 0xFF,
            n,
        ]
        for h_val, s_val, v_val in batch:
            data += [h_val & 0xFF, s_val & 0xFF, v_val & 0xFF]
        _send(h, data)


def set_led(h, index: int, hue: int, sat: int, val: int):
    """Set a single LED by index (0-15)."""
    _send(
        h,
        [
            ID_LIGHTING_SET_VALUE,
            VIALRGB_DIRECT_FASTSET,
            index & 0xFF,
            (index >> 8) & 0xFF,
            1,
            hue & 0xFF,
            sat & 0xFF,
            val & 0xFF,
        ],
    )


def all_off(h):
    """Turn every LED off (sets brightness to 0)."""
    set_leds(h, [(0, 0, 0)] * LED_COUNT)


def all_color(h, hue: int, sat: int, val: int):
    """Set all LEDs to one color."""
    set_leds(h, [(hue, sat, val)] * LED_COUNT)


# ---------------------------------------------------------------------------
# Per-key label color persistence
# ---------------------------------------------------------------------------


def load_label_colors():
    """
    Load saved per-key label colors from LABEL_FILE.
    Returns a list of 16 (H, S, V) tuples; unset keys default to (0, 0, 0).
    Returns None if no label file exists.
    """
    try:
        with open(LABEL_FILE) as f:
            data = json.load(f)
        return [tuple(data.get(str(i), [0, 0, 0])) for i in range(LED_COUNT)]
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        return None


def save_label_colors(colors):
    """Persist per-key label colors to LABEL_FILE."""
    os.makedirs(os.path.dirname(LABEL_FILE), exist_ok=True)
    data = {str(i): list(c) for i, c in enumerate(colors)}
    with open(LABEL_FILE, "w") as f:
        json.dump(data, f, indent=2)


# ---------------------------------------------------------------------------
# VIA dynamic keymap — set/get individual key assignments
# ---------------------------------------------------------------------------


def get_keycode(h, layer: int, row: int, col: int) -> int:
    """Return the QMK keycode currently assigned to (layer, row, col)."""
    _send(h, [ID_DYNAMIC_KEYMAP_GET_KEYCODE, layer & 0xFF, row & 0xFF, col & 0xFF])
    r = _recv(h)
    return (r[4] << 8) | r[5]


def set_keycode(h, layer: int, row: int, col: int, keycode: int):
    """Write a QMK keycode to (layer, row, col) via VIA dynamic keymap."""
    _send(
        h,
        [
            ID_DYNAMIC_KEYMAP_SET_KEYCODE,
            layer & 0xFF,
            row & 0xFF,
            col & 0xFF,
            (keycode >> 8) & 0xFF,
            keycode & 0xFF,
        ],
    )


# ---------------------------------------------------------------------------
# Alarm blink daemon (supports multiple simultaneous named alarms)
# ---------------------------------------------------------------------------


def _daemon_running() -> bool:
    """Return True if the blink daemon process is alive."""
    try:
        with open(DAEMON_PID_FILE) as f:
            pid = int(f.read().strip())
        os.kill(pid, 0)  # signal 0 = probe only
        return True
    except (FileNotFoundError, ValueError, ProcessLookupError, PermissionError):
        return False


def blink_daemon():
    """
    Long-running daemon that manages multiple named alarm blink slots.

    Slot files in SLOTS_DIR/<name>.json define active alarms:
      {"leds": [15], "hue": 170, "sat": 255, "val": 200, "interval": 0.5}

    The daemon renders all slots simultaneously on one HID device, each with
    independent timing.  When all slot files are removed it restores label
    colors and exits.
    """
    os.makedirs(SLOTS_DIR, exist_ok=True)
    h = open_device()

    orig_effect, orig_speed, orig_hue, orig_sat, orig_val = get_mode(h)
    if orig_effect == VIALRGB_EFFECT_DIRECT:
        orig_effect = 2
        orig_speed = 128
        orig_hue = 0
        orig_sat = 255
        orig_val = 150

    label_colors = load_label_colors()

    def restore_and_exit(signum=None, frame=None):
        try:
            if label_colors is not None:
                set_mode(h, VIALRGB_EFFECT_DIRECT)
                time.sleep(0.05)
                set_leds(h, label_colors)
            else:
                all_off(h)
                time.sleep(0.05)
                set_mode(h, orig_effect, orig_speed, orig_hue, orig_sat, orig_val)
        except Exception:
            pass
        try:
            os.unlink(DAEMON_PID_FILE)
        except FileNotFoundError:
            pass
        sys.exit(0)

    signal.signal(signal.SIGTERM, restore_and_exit)
    signal.signal(signal.SIGUSR1, restore_and_exit)

    with open(DAEMON_PID_FILE, "w") as f:
        f.write(str(os.getpid()))

    set_mode(h, VIALRGB_EFFECT_DIRECT, speed=128, hue=0, sat=0, val=0)
    time.sleep(0.05)

    # Per-slot state: name -> {cfg, on_state, next_toggle}
    slot_states = {}
    idle_since = None

    try:
        while True:
            now = time.monotonic()

            # Scan slot directory for active alarm definitions
            try:
                slot_files = {
                    os.path.splitext(fname)[0]: os.path.join(SLOTS_DIR, fname)
                    for fname in os.listdir(SLOTS_DIR)
                    if fname.endswith(".json")
                }
            except FileNotFoundError:
                slot_files = {}

            # Drop slots whose files have been removed
            for name in list(slot_states.keys()):
                if name not in slot_files:
                    del slot_states[name]

            # Load new or updated slot configs
            for name, path in slot_files.items():
                try:
                    with open(path) as f:
                        cfg = json.load(f)
                    if name not in slot_states:
                        slot_states[name] = {
                            "cfg": cfg,
                            "on_state": False,
                            "next_toggle": now,
                        }
                    else:
                        slot_states[name]["cfg"] = cfg
                except Exception:
                    pass

            # Exit automatically when all alarms have been cancelled
            if not slot_states:
                if idle_since is None:
                    idle_since = now
                elif now - idle_since >= DAEMON_IDLE_TIMEOUT:
                    restore_and_exit()
            else:
                idle_since = None

            # Build frame: label colors as background, alarms overlaid
            if label_colors is not None:
                frame = list(label_colors)
            else:
                frame = [(orig_hue, orig_sat, orig_val)] * LED_COUNT

            for state in slot_states.values():
                cfg = state["cfg"]
                leds = cfg.get("leds", [])
                hue = cfg.get("hue", 0)
                sat = cfg.get("sat", 255)
                val = cfg.get("val", 200)
                interval = cfg.get("interval", 0.5)

                if now >= state["next_toggle"]:
                    state["on_state"] = not state["on_state"]
                    state["next_toggle"] = now + interval

                for idx in leds:
                    if 0 <= idx < LED_COUNT:
                        frame[idx] = (hue, sat, val) if state["on_state"] else (0, 0, 0)

            set_leds(h, frame)
            time.sleep(DAEMON_POLL_INTERVAL)

    except KeyboardInterrupt:
        restore_and_exit()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def cmd_label(args):
    """
    Set static per-key colors, save them, and apply immediately (DIRECT mode).
    These saved colors are used as the background during alarm blink, and
    restored when the alarm stops.
    """
    val = args.val
    existing = load_label_colors()
    colors = list(existing) if existing else [(0, 0, 0)] * LED_COUNT

    if args.preset:
        preset = args.preset.lower()
        if preset == "rows":
            row_hues = [0, 85, 170, 43]  # top=red, row1=green, row2=blue, bottom=yellow
            colors = [(row_hues[i // 4], 255, val) for i in range(LED_COUNT)]
        elif preset == "columns":
            col_hues = [
                0,
                21,
                128,
                213,
            ]  # col0=red, col1=orange, col2=cyan, col3=purple
            colors = [(col_hues[i % 4], 255, val) for i in range(LED_COUNT)]
        elif preset == "rainbow":
            colors = [(i * 255 // LED_COUNT, 255, val) for i in range(LED_COUNT)]
        elif preset == "off":
            colors = [(0, 0, 0)] * LED_COUNT
        else:
            print(f"Unknown preset '{preset}'. Options: rows, columns, rainbow, off")
            sys.exit(1)

    if args.colors:
        for spec in args.colors.split(","):
            spec = spec.strip()
            if ":" not in spec:
                continue
            idx_str, color_str = spec.split(":", 1)
            idx = int(idx_str.strip())
            color_str = color_str.strip().lower()
            if color_str == "off":
                colors[idx] = (0, 0, 0)
            elif color_str in COLOR_NAMES:
                hs = COLOR_NAMES[color_str]
                colors[idx] = (0, 0, 0) if hs is None else (hs[0], hs[1], val)
            else:
                parts = [int(p) for p in color_str.split(":")]
                if len(parts) == 1:
                    colors[idx] = (parts[0], 255, val)
                elif len(parts) == 2:
                    colors[idx] = (parts[0], parts[1], val)
                elif len(parts) == 3:
                    colors[idx] = (parts[0], parts[1], parts[2])
                else:
                    print(f"Unrecognised color spec '{color_str}' for LED {idx}")
                    sys.exit(1)

    # Only save if we have something to save: either the file loaded OK, or
    # the caller explicitly specified new colors via --preset or --colors.
    # This prevents a load failure (e.g. NFS not mounted) from wiping the file.
    if existing is not None or args.preset or args.colors:
        save_label_colors(colors)
    h = open_device()
    set_mode(h, VIALRGB_EFFECT_DIRECT)
    time.sleep(0.05)
    set_leds(h, colors)
    h.close()
    n_lit = sum(1 for c in colors if c[2] > 0)
    print(f"Label applied: {n_lit}/16 keys lit — saved to {LABEL_FILE}")


def cmd_blink(args):
    """Register a named alarm slot then start or join the blink daemon."""
    leds = [int(x) for x in args.leds.split(",")]
    os.makedirs(SLOTS_DIR, exist_ok=True)
    slot = {
        "leds": leds,
        "hue": args.hue,
        "sat": args.sat,
        "val": args.val,
        "interval": args.interval,
    }
    slot_path = os.path.join(SLOTS_DIR, f"{args.name}.json")
    with open(slot_path, "w") as f:
        json.dump(slot, f)
    if _daemon_running():
        return  # daemon will pick up the new slot file on its next tick
    blink_daemon()  # become the daemon (blocks until all slots removed)


def cmd_stop(args):
    name = args.name

    if name:
        # Remove just this one slot file
        slot_path = os.path.join(SLOTS_DIR, f"{name}.json")
        if os.path.exists(slot_path):
            os.unlink(slot_path)
            print(f"Stopped alarm '{name}'")
        else:
            print(f"No alarm named '{name}' is running")
        # If this was the last slot, signal daemon to exit immediately
        try:
            remaining = [f for f in os.listdir(SLOTS_DIR) if f.endswith(".json")]
        except FileNotFoundError:
            remaining = []
        if not remaining:
            try:
                with open(DAEMON_PID_FILE) as f:
                    pid = int(f.read().strip())
                os.kill(pid, signal.SIGUSR1)
            except (FileNotFoundError, ProcessLookupError, ValueError):
                pass
    else:
        # Stop ALL alarms: remove every slot file then signal daemon
        try:
            for fname in os.listdir(SLOTS_DIR):
                if fname.endswith(".json"):
                    os.unlink(os.path.join(SLOTS_DIR, fname))
        except FileNotFoundError:
            pass
        try:
            with open(DAEMON_PID_FILE) as f:
                pid = int(f.read().strip())
            os.kill(pid, signal.SIGUSR1)
            print(f"Sent stop to alarm daemon PID {pid}")
        except FileNotFoundError:
            # No daemon — rescue device if stuck in DIRECT mode
            try:
                h = open_device()
                effect, speed, hue, sat, val = get_mode(h)
                if effect == VIALRGB_EFFECT_DIRECT:
                    label_colors = load_label_colors()
                    if label_colors is not None:
                        set_leds(h, label_colors)
                    else:
                        set_mode(h, 2, speed=128, hue=0, sat=255, val=150)
                    print("No alarm running — rescued device from DIRECT mode")
                else:
                    print("No alarm running (no daemon)")
                h.close()
            except Exception:
                print("No alarm running (no daemon)")
        except ProcessLookupError:
            print("Alarm daemon already gone — cleaning up")
            try:
                os.unlink(DAEMON_PID_FILE)
            except FileNotFoundError:
                pass


def cmd_set(args):
    h = open_device()
    set_mode(h, args.effect, speed=args.speed, hue=args.hue, sat=args.sat, val=args.val)
    print(
        f"Set effect={args.effect} speed={args.speed} hue={args.hue} sat={args.sat} val={args.val}"
    )


def cmd_off(args):
    h = open_device()
    set_mode(h, VIALRGB_EFFECT_OFF)


def cmd_status(args):
    h = open_device()
    effect, speed, hue, sat, val = get_mode(h)
    names = {0: "OFF", 1: "DIRECT", 2: "Solid Color"}
    print(
        f"Effect: {names.get(effect, str(effect))}  Speed: {speed}  "
        f"Hue: {hue}  Sat: {sat}  Val: {val}"
    )


def _resolve_keycode(s: str) -> int:
    """Accept '0x0068', '104', or 'F13' style keycode strings."""
    s = s.upper()
    if s in KC:
        return KC[s]
    return int(s, 0)  # handles hex 0x... and decimal


def cmd_setkey(args):
    keycode = _resolve_keycode(args.keycode)
    h = open_device()
    set_keycode(h, args.layer, args.row, args.col, keycode)
    print(
        f"Set layer={args.layer} row={args.row} col={args.col} → keycode=0x{keycode:04X}"
    )


def cmd_getkey(args):
    h = open_device()
    keycode = get_keycode(h, args.layer, args.row, args.col)
    # Reverse-lookup name
    name = next((k for k, v in KC.items() if v == keycode), f"0x{keycode:04X}")
    print(
        f"layer={args.layer} row={args.row} col={args.col} → {name} (0x{keycode:04X})"
    )


def main():
    parser = argparse.ArgumentParser(
        description="Megalodon RGB control (VIAL firmware)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # blink — meant to be run in background by alarm_blink.sh
    p = sub.add_parser("blink", help="Blink LEDs as alarm (blocks until stopped)")
    p.add_argument(
        "--leds",
        default="0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15",
        help="Comma-separated LED indices (default: all)",
    )
    p.add_argument("--hue", type=int, default=0, help="Hue 0-255 (0=red)")
    p.add_argument("--sat", type=int, default=255, help="Saturation 0-255")
    p.add_argument("--val", type=int, default=200, help="Brightness 0-255")
    p.add_argument(
        "--interval", type=float, default=0.5, help="Blink interval in seconds"
    )
    p.add_argument(
        "--name",
        default="default",
        help="Alarm name — multiple alarms with different names run simultaneously (default: 'default')",
    )
    p.set_defaults(func=cmd_blink)

    # label — set per-key static colors
    p = sub.add_parser(
        "label",
        help="Set per-key static colors (persisted; used as blink background)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Set each key to a different color, saved to ~/.config/megalodon_colors.json.\n"
            "These colors appear as the background while the alarm blinks, and are\n"
            "restored when the alarm stops.\n\n"
            "Color names: red orange yellow green cyan blue purple magenta pink white off\n"
            "Raw numeric:  <hue>  or  <hue>:<sat>  or  <hue>:<sat>:<val>  (0-255 each)\n\n"
            "Examples:\n"
            "  label --preset rows\n"
            "  label --preset rainbow --val 120\n"
            '  label --colors "0:red,1:blue,4:green,8:yellow"\n'
            '  label --preset rows --colors "0:white"   # rows then override key 0'
        ),
    )
    p.add_argument(
        "--preset", metavar="NAME", help="Preset: rows | columns | rainbow | off"
    )
    p.add_argument(
        "--colors",
        metavar="SPEC",
        help='Per-key overrides: "IDX:color,..." e.g. "0:red,1:blue"',
    )
    p.add_argument(
        "--val", type=int, default=150, help="Brightness 0-255 (default 150)"
    )
    p.set_defaults(func=cmd_label)

    # stop
    p = sub.add_parser("stop", help="Stop a running alarm (omit --name to stop all)")
    p.add_argument(
        "--name",
        default=None,
        help="Stop only this named alarm; omit to stop all alarms",
    )
    p.set_defaults(func=cmd_stop)

    # set
    p = sub.add_parser(
        "set", help="Set VIALRGB effect/color (use VIAL app for full control)"
    )
    p.add_argument(
        "--effect", type=int, default=2, help="Effect ID (2=Solid Color, others vary)"
    )
    p.add_argument("--speed", type=int, default=128, help="Speed 0-255")
    p.add_argument(
        "--hue", type=int, default=170, help="Hue 0-255 (0=red 85=green 170=blue)"
    )
    p.add_argument("--sat", type=int, default=255, help="Saturation 0-255")
    p.add_argument("--val", type=int, default=150, help="Brightness 0-255")
    p.set_defaults(func=cmd_set)

    # off
    p = sub.add_parser("off", help="Switch to OFF effect")
    p.set_defaults(func=cmd_off)

    # status
    p = sub.add_parser("status", help="Print current RGB mode")
    p.set_defaults(func=cmd_status)

    # setkey — program a key's keycode via VIA dynamic keymap
    p = sub.add_parser("setkey", help="Assign a keycode to a key position")
    p.add_argument("--layer", type=int, default=0, help="Layer (default 0)")
    p.add_argument("--row", type=int, required=True, help="Matrix row")
    p.add_argument("--col", type=int, required=True, help="Matrix col")
    p.add_argument("keycode", help="Keycode name (F13, F14, ...) or hex (0x0068)")
    p.set_defaults(func=cmd_setkey)

    # getkey — read current keycode at a position
    p = sub.add_parser("getkey", help="Read the keycode assigned to a key")
    p.add_argument("--layer", type=int, default=0)
    p.add_argument("--row", type=int, required=True)
    p.add_argument("--col", type=int, required=True)
    p.set_defaults(func=cmd_getkey)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
