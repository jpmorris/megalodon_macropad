# Megalodon Macropad (KEEBMONKEY DOIO KB16 rev2)

RGB LED control and alarm system for the KEEBMONKEY Megalodon macropad running VIAL firmware. Communicates over VIA Raw HID protocol.

## Hardware

- **Device**: KEEBMONKEY DOIO KB16 rev2 (4x4 macropad + 3 rotary encoders)
- **Firmware**: VIAL (QMK fork) with `ENABLE_RGB_MATRIX_VIALRGB_DIRECT = yes`
- **USB IDs**: VID `0xD010`, PID `0x1601`
- **HID interface**: Usage page `0xFF60`, usage `0x0061`
- **LEDs**: 16 per-key RGB LEDs (one per key, row-major order)

```
LED layout:
 0  1  2  3    <- top row
 4  5  6  7
 8  9 10 11
12 13 14 15    <- bottom row
```

## Files

| File | Purpose |
|------|---------|
| `megalodon_led.py` | Main Python library and CLI for RGB control, keymap editing, and alarm blink daemon |
| `alarm_blink.sh` | Shell wrapper for starting/stopping/toggling named alarm blinks |

## Dependencies

- Python 3
- [`hidapi`](https://pypi.org/project/hidapi/) (`pip install hidapi`)
- udev rule for non-root access (deployed via Ansible):
  ```
  # /etc/udev/rules.d/99-via-doio.rules
  SUBSYSTEM=="hidraw", ATTRS{idVendor}=="d010", ATTRS{idProduct}=="1601", MODE="0660", GROUP="input"
  ```

## CLI Usage (`megalodon_led.py`)

### RGB Effects

```bash
# Check current mode
python3 megalodon_led.py status

# Set all LEDs to a solid color (effect 2 = Solid Color)
python3 megalodon_led.py set --hue 170 --sat 255 --val 150

# Turn LEDs off
python3 megalodon_led.py off
```

### Per-Key Label Colors

Static per-key colors saved to `~/.config/megalodon_colors.json`. These persist across reboots and are used as the background during alarm blinks.

```bash
# Preset patterns
python3 megalodon_led.py label --preset rows
python3 megalodon_led.py label --preset rainbow --val 120
python3 megalodon_led.py label --preset columns
python3 megalodon_led.py label --preset off

# Individual key colors (by name or HSV)
python3 megalodon_led.py label --colors "0:red,1:blue,4:green,8:yellow"

# Combine preset + overrides
python3 megalodon_led.py label --preset rows --colors "0:white"
```

Color names: `red` `orange` `yellow` `green` `cyan` `blue` `purple` `magenta` `pink` `white` `off`

### Alarm Blink

Blinks specified LEDs as a visual reminder. Multiple named alarms can run simultaneously on different keys. A background daemon manages all active alarms and auto-exits when the last alarm is dismissed.

```bash
# Start an alarm (blocks as daemon if first, otherwise registers and returns)
python3 megalodon_led.py blink --name vitamins --leds 15 --hue 170

# Stop a specific alarm
python3 megalodon_led.py stop --name vitamins

# Stop all alarms
python3 megalodon_led.py stop
```

Alarm state is stored as JSON files in `/tmp/megalodon_slots/`. The daemon PID is tracked at `/tmp/megalodon_alarm_daemon.pid`.

### Keymap Editing

Read or write keycodes via VIA dynamic keymap protocol:

```bash
# Read keycode at layer 0, row 3, col 2
python3 megalodon_led.py getkey --row 3 --col 2

# Assign F13 to layer 0, row 0, col 0
python3 megalodon_led.py setkey --row 0 --col 0 F13
```

## Shell Wrapper (`alarm_blink.sh`)

Convenience wrapper that handles backgrounding:

```bash
alarm_blink.sh start  --name vitamins --leds 15 --hue 170
alarm_blink.sh stop   --name vitamins
alarm_blink.sh stop                      # stop all
alarm_blink.sh toggle --name vitamins    # start if off, stop if running
```

## Key Layout & Bindings (Hyprland / julia)

Physical layout with key functions and XF86 keysyms:

```
┌─────────────────────────────────────────────────────┐
│  [knob L]        [knob M]          [knob R]          │
│  press=gammastep  (unused)          press=cycle-sink  │
│  rotate=temp↑↓                      rotate=vol↑↓     │
├──────────────┬──────────────┬──────────────┬─────────┤
│  0           │  1           │  2           │  3      │
│  XF86Tools   │  XF86Launch5 │  XF86TouchpadOn│XF86Search│
│  play/pause  │  pause all   │  kill chrome │cycle sink│
│              │  (not vlc)   │              │         │
├──────────────┼──────────────┼──────────────┼─────────┤
│  4           │  5           │  6           │  7      │
│  XF86Launch9 │  (unused)    │  (unused)    │(unused) │
│  workspace 1 │              │              │         │
├──────────────┼──────────────┼──────────────┼─────────┤
│  8           │  9           │  10          │  11     │
│  XF86Launch8 │  (unused)    │  (unused)    │(unused) │
│  gammastep   │              │              │         │
│  toggle      │              │              │         │
├──────────────┼──────────────┼──────────────┼─────────┤
│  12          │  13          │  14          │  15     │
│  XF86Calculator│(unused)    │  XF86TouchpadOff│code:202│
│  JupyterLab  │              │  spanish alarm│vitamins │
│              │              │  toggle🟠    │ toggle🔵│
└──────────────┴──────────────┴──────────────┴─────────┘
```

### Cron Jobs (`/etc/cron.d/jmorris`)

Scheduled alarms trigger LED blinks as visual reminders:

```cron
# Spanish session reminder - every day at 9PM (orange blink on LED 14)
0 21 * * * jmorris /mnt/bebop_jmorris/code/megalodon_macropad/alarm_blink.sh start --name spanish --leds 14 --hue 21

# Vitamins reminder - every day at noon (blue blink on LED 15)
0 12 * * * jmorris /mnt/bebop_jmorris/code/megalodon_macropad/alarm_blink.sh start --name vitamins --leds 15 --hue 170
```

### Hyprland Keybindings (`~/.config/hypr/hyprland.conf`)

```
XF86Tools          → playerctl play-pause
XF86Launch5        → playerctl pause all players (except vlc)
XF86TouchpadOn     → kill_chrome.sh
XF86Search         → cycle-sink.sh (cycle audio output)
XF86Launch9        → switch to workspace 1
XF86Launch8        → gammastep toggle (screen color temperature)
XF86Calculator     → jlab (JupyterLab)
XF86TouchpadOff    → toggle spanish alarm (LED 14, orange)
code:202           → toggle vitamins alarm (LED 15, blue)
XF86MonBrightnessUp   → gammastep decrease (warmer)
XF86MonBrightnessDown → gammastep increase (cooler)
XF86AudioRaiseVolume  → volume +5%
XF86AudioLowerVolume  → volume -5%
XF86AudioMute         → mute toggle
```

## Architecture

The system uses a slot-based daemon architecture:

1. `alarm_blink.sh` (or cron) calls `megalodon_led.py blink --name <name>` in the background
2. The blink command writes a slot JSON file to `/tmp/megalodon_slots/<name>.json`
3. If no daemon is running, the process becomes the daemon; otherwise it exits (the existing daemon picks up the new slot)
4. The daemon renders all active slots at ~20 FPS, overlaying alarm blinks on top of saved label colors
5. When a slot file is removed (via `stop`), that alarm stops blinking
6. When all slots are gone, the daemon restores label colors (or the previous effect) and exits

