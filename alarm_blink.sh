#!/usr/bin/env bash
# alarm_blink.sh — blink Megalodon LEDs as a visual alarm.
# Multiple named alarms can run simultaneously (each on different LEDs).
#
# Usage:
#   alarm_blink.sh start  [--name NAME] [--leds N,...] [--hue H] [--interval S]
#   alarm_blink.sh stop   [--name NAME]        # omit --name to stop ALL alarms
#   alarm_blink.sh toggle [--name NAME] [...]  # start if off, stop if running
#
# --name defaults to "default".  Use distinct names for concurrent alarms:
#   alarm_blink.sh start --name vitamins --leds 15 --hue 170   # blue, LED 15
#   alarm_blink.sh start --name chromium --leds 14 --hue 0     # red,  LED 14
#   alarm_blink.sh stop  --name vitamins                        # cancel vitamins only
#   alarm_blink.sh stop                                         # cancel all
#
# LED layout:
#   0  1  2  3    ← top row
#   4  5  6  7
#   8  9 10 11
#  12 13 14 15    ← bottom row

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MEGALODON_LED="$SCRIPT_DIR/megalodon_led.py"
SLOTS_DIR="/tmp/megalodon_slots"

# Parse --name NAME out of the argument list.
# Sets NAME (default "default"), NAME_GIVEN (0|1), and EXTRA_ARGS (remaining args).
_parse_name() {
    NAME="default"
    NAME_GIVEN=0
    EXTRA_ARGS=()
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --name) NAME="$2"; NAME_GIVEN=1; shift 2 ;;
            *)      EXTRA_ARGS+=("$1"); shift ;;
        esac
    done
}

case "${1:-start}" in
    start)
        shift
        _parse_name "$@"
        python3 "$MEGALODON_LED" blink --name "$NAME" "${EXTRA_ARGS[@]}" &
        echo "Alarm '$NAME' started (PID $!)"
        ;;

    stop)
        shift
        _parse_name "$@"
        if [[ "$NAME_GIVEN" -eq 1 ]]; then
            python3 "$MEGALODON_LED" stop --name "$NAME"
        else
            python3 "$MEGALODON_LED" stop   # stop all
        fi
        ;;

    toggle)
        shift
        _parse_name "$@"
        if [[ -f "$SLOTS_DIR/${NAME}.json" ]]; then
            python3 "$MEGALODON_LED" stop --name "$NAME"
        else
            python3 "$MEGALODON_LED" blink --name "$NAME" "${EXTRA_ARGS[@]}" &
            echo "Alarm '$NAME' started (PID $!)"
        fi
        ;;

    *)
        echo "Usage: $0 {start|stop|toggle} [options]"
        echo "  --name NAME     Alarm name (default: 'default'); use different names for"
        echo "                  concurrent alarms on different LEDs"
        echo "  --leds 0,1,...  LED indices to blink (default: all 16)"
        echo "  --hue 0-255     Hue (0=red, 85=green, 170=blue; default: 0)"
        echo "  --sat 0-255     Saturation (default: 255)"
        echo "  --val 0-255     Brightness (default: 200)"
        echo "  --interval N    Blink interval in seconds (default: 0.5)"
        exit 1
        ;;
esac
