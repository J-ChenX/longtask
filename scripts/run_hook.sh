#!/bin/sh
# platform-adapters: bundled hook consumer; python-path is user-owned install data.
set -eu
LC_ALL=C
export LC_ALL
python=python3
binding=${PLUGIN_DATA:-}/python-path
if [ -n "${PLUGIN_DATA:-}" ] && { [ -e "$binding" ] || [ -L "$binding" ]; }; then
    [ -f "$binding" ] && [ ! -L "$binding" ] || { echo 'Unsafe longtask Python binding' >&2; exit 1; }
    bytes=$(command -p wc -c < "$binding")
    [ "$bytes" -le 4096 ] || exit 1
    extra=
    { IFS= read -r python && ! IFS= read -r extra && [ -z "$extra" ]; } < "$binding" || {
        echo 'Expected one LF-terminated absolute Python path' >&2; exit 1;
    }
    case "$python" in /*) ;; *) echo 'Expected absolute Python path' >&2; exit 1 ;; esac
    [ "$bytes" -eq "$((${#python} + 1))" ] || exit 1
fi
script_dir=$(CDPATH= cd -- "${0%/*}" && pwd -P)
if [ -n "${PLUGIN_DATA:-}" ] && [ ! -e "$binding" ]; then
    "$python" -E -s -B "$script_dir/longtask_hooks.py" && exit 0
    status=$?
    echo "Bind a verified Python 3.14+ executable at: $binding (UTF-8, one absolute path, LF terminated)" >&2
    exit "$status"
fi
exec "$python" -E -s -B "$script_dir/longtask_hooks.py"
