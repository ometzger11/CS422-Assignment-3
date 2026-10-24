#!/usr/bin/env bash
# Load / unload DART for experiments. Use only inside the lab VM.
#
#   sudo scripts/dart_module.sh load    insmod tcp_dart.ko, modprobe tcp_bbr, and
#                                       allow both for unprivileged sockets
#   sudo scripts/dart_module.sh unload  remove dart from the allowed list, rmmod
#   scripts/dart_module.sh status       show what is loaded and allowed (no root)
#
# The default congestion control is never changed, and nothing here survives
# a reboot.
set -euo pipefail
cd "$(dirname "$0")/.."

CC=/proc/sys/net/ipv4

allowed() { cat "$CC/tcp_allowed_congestion_control"; }

status() {
    echo "kernel:    $(uname -r)"
    echo "default:   $(cat "$CC/tcp_congestion_control")"
    echo "available: $(cat "$CC/tcp_available_congestion_control")"
    echo "allowed:   $(allowed)"
    if [ -d /sys/module/tcp_dart ]; then
        echo "tcp_dart:  loaded, srcversion $(cat /sys/module/tcp_dart/srcversion)," \
             "in use by $(cat /sys/module/tcp_dart/refcnt) socket(s)"
        echo "stats:     $(cat /sys/module/tcp_dart/parameters/stats)"
    else
        echo "tcp_dart:  not loaded"
    fi
}

need_root() {
    if [ "$(id -u)" -ne 0 ]; then
        echo "$1 needs root: sudo $0 $1" >&2
        exit 1
    fi
}

# Add or remove one name in net.ipv4.tcp_allowed_congestion_control
allow() {
    case " $(allowed) " in
        *" $1 "*) ;;
        *) sysctl -q -w net.ipv4.tcp_allowed_congestion_control="$(allowed) $1" ;;
    esac
}
disallow() {
    local rest
    rest=$(allowed | tr ' ' '\n' | { grep -vx "$1" || true; } | tr '\n' ' ')
    sysctl -q -w net.ipv4.tcp_allowed_congestion_control="${rest% }"
}

case "${1:-status}" in
    load)
        need_root load
        if [ ! -f tcp_dart.ko ]; then
            echo "tcp_dart.ko is not built; run make first" >&2
            exit 1
        fi
        if [ -d /sys/module/tcp_dart ]; then
            if [ "$(cat /sys/module/tcp_dart/srcversion)" != \
                 "$(modinfo -F srcversion ./tcp_dart.ko)" ]; then
                echo "WARNING: the loaded tcp_dart is not this build;" \
                     "run unload, then load" >&2
            fi
        else
            insmod ./tcp_dart.ko
        fi
        modprobe tcp_bbr
        allow bbr
        allow dart
        status
        ;;
    unload)
        need_root unload
        case " $(allowed) " in *" dart "*) disallow dart ;; esac
        # rmmod refuses while any socket still uses dart
        if [ -d /sys/module/tcp_dart ]; then
            rmmod tcp_dart
        fi
        status
        ;;
    status)
        status
        ;;
    *)
        sed -n '2,10p' "$0"
        exit 1
        ;;
esac
