#!/bin/sh
# Refuses argv outside the sandbox-safe ps allow-list, then runs the re-signed copy beside
# it as ps-bin (#25368). Only -o with non-argument columns and the selector flags
# -A -a -x -r -w -W -p -q -U -G -g -t reach ps, so `ps aux`, `ps -e` and `ps -o args`
# never list other processes' arguments. The column list must match the
# no-process-environment-listing rule and the Python copy in droid_ps_shim.py.

refuse() {
    echo "ps: refused: only -o with non-argument columns may list processes" >&2
    exit 1
}

# A comma-separated -o list of allowed columns, each with an optional plain header.
valid_columns() {
    list=$1
    while :; do
        item=${list%%,*}
        name=${item%%=*}
        header=
        case $item in *=*) header=${item#*=} ;; esac
        case $header in *[!A-Za-z0-9_]*) return 1 ;; esac
        case $name in
            pid|ppid|pgid|uid|user|gid|group|etime|etimes|time|stat|state|tt|tty|lstart|start|pcpu|pmem|rss|vsz|nice|pri|comm) ;;
            *) return 1 ;;
        esac
        case $list in
            *,*) list=${list#*,} ;;
            *) return 0 ;;
        esac
    done
}

expect=
seen_columns=
for arg; do
    if [ -n "$expect" ]; then
        case $expect in
            columns) valid_columns "$arg" || refuse ;;
            operand)
                case $arg in
                    '' | *[!A-Za-z0-9_.,:]*) refuse ;;
                esac
                ;;
        esac
        expect=
        continue
    fi
    case $arg in
        -?*) ;;
        *) refuse ;;
    esac
    options=${arg#-}
    while [ -n "$options" ]; do
        option=${options%"${options#?}"}
        options=${options#?}
        case $option in
            A | a | x | r | w | W) ;;
            o)
                [ -z "$options" ] || refuse
                expect=columns
                seen_columns=1
                ;;
            p | q | U | G | g | t)
                [ -z "$options" ] || refuse
                expect=operand
                ;;
            *) refuse ;;
        esac
    done
done
[ -z "$expect" ] && [ -n "$seen_columns" ] || refuse
exec "${0%/*}/ps-bin" "$@"
