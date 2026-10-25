#!/bin/bash
# Privileged helper for the Alienware fan control GUI.
# Installed with a NOPASSWD sudoers rule by install.sh.
set -u

ACPI_CALL=/proc/acpi/call

die() { echo "$*" >&2; exit 1; }

[ -e "$ACPI_CALL" ] || die "acpi_call is not loaded: sudo modprobe acpi_call"

call_acpi() {
    local cmd="$1" result
    printf '%s\n' "$cmd" > "$ACPI_CALL" || die "failed writing to $ACPI_CALL"
    result="$(tr -d '\000' < "$ACPI_CALL" 2>/dev/null)"
    case "$result" in
        *Error*|*error*) die "ACPI call failed: $result" ;;
    esac
    printf '%s\n' "$result"
}

check_speed() {
    case "$1" in
        ''|*[!0-9]*) die "speed must be a number between 0 and 100" ;;
    esac
    [ "$1" -le 100 ] || die "speed must be between 0 and 100"
}

hex_speed() { printf '0x%02X' "$1"; }

case "${1:-}" in
  probe)
    exit 0
    ;;
  cpu)
    check_speed "${2:-}"
    call_acpi "\_SB.AMWW.WMAX 0 0x15 {0x02,0x32,$(hex_speed "$2"),0x00}"
    ;;
  gpu)
    check_speed "${2:-}"
    call_acpi "\_SB.AMWW.WMAX 0 0x15 {0x02,0x33,$(hex_speed "$2"),0x00}"
    ;;
  both)
    check_speed "${2:-}"
    check_speed "${3:-}"
    call_acpi "\_SB.AMWW.WMAX 0 0x15 {0x02,0x32,$(hex_speed "$2"),0x00}"
    call_acpi "\_SB.AMWW.WMAX 0 0x15 {0x02,0x33,$(hex_speed "$3"),0x00}"
    ;;
  profile)
    case "${2:-}" in
      balanced)    call_acpi "\_SB.AMWW.WMAX 0 0x15 {0x01,0xA0,0x00,0x00}" ;;
      performance) call_acpi "\_SB.AMWW.WMAX 0 0x15 {0x01,0xA1,0x00,0x00}" ;;
      quiet)       call_acpi "\_SB.AMWW.WMAX 0 0x15 {0x01,0xA3,0x00,0x00}" ;;
      gameshift)   call_acpi "\_SB.AMWW.WMAX 0 0x15 {0x01,0xAB,0x00,0x00}" ;;
      *) die "unknown profile: ${2:-}" ;;
    esac
    ;;
  *)
    echo "Usage: fan_helper.sh cpu <0-100>" >&2
    echo "       fan_helper.sh gpu <0-100>" >&2
    echo "       fan_helper.sh both <cpu%> <gpu%>" >&2
    echo "       fan_helper.sh profile <balanced|performance|quiet|gameshift>" >&2
    exit 1
    ;;
esac
