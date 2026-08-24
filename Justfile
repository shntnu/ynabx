set dotenv-load
set positional-arguments

session_helper := ".agents/skills/vignette-catalog-compose-notebook/scripts/catalog-session.py"

default:
    @just --list

# Rebuild the private Amazon SQLite database from saved JSON snapshots.
amazon-db source="data/external/raw/amazon" db="data/amazon.sqlite3":
    uv run scripts/import_amazon.py --source "{{ source }}" --db "{{ db }}"

# Run a numbered notebook, wait for its cells, and open it in the browser.
nb number:
    #!/usr/bin/env bash
    set -euo pipefail

    requested="$1"
    if [[ "$requested" == [nN][bB]* ]]; then
        requested="${requested:2}"
    fi
    if [[ ! "$requested" =~ ^[0-9]{1,2}$ ]]; then
        printf 'Expected a notebook number such as 8, 08, or nb08; got %q.\n' "$1" >&2
        exit 2
    fi

    printf -v padded '%02d' "$((10#$requested))"
    shopt -s nullglob
    matches=(notebooks/nb"${padded}"_*.py)
    if (( ${#matches[@]} != 1 )); then
        if (( ${#matches[@]} == 0 )); then
            printf 'No notebook matches nb%s.\n' "$padded" >&2
        else
            printf 'More than one notebook matches nb%s:\n' "$padded" >&2
            printf '  %s\n' "${matches[@]}" >&2
        fi
        printf 'Available notebook numbers:' >&2
        for path in notebooks/nb[0-9][0-9]_*.py; do
            name="${path##*/}"
            printf ' %s' "${name:2:2}" >&2
        done
        printf '\n' >&2
        exit 2
    fi

    helper="{{ session_helper }}"
    if [[ ! -x "$helper" ]]; then
        printf 'Catalog launcher is not installed. Run the skill installation commands in README.md.\n' >&2
        exit 1
    fi

    result="$("$helper" open "${matches[0]}" --run always)"
    printf '%s\n' "$result"
    url="$(awk -F= '$1 == "url" { print substr($0, 5); exit }' <<<"$result")"
    if [[ -z "$url" ]]; then
        printf 'The catalog launcher did not report a browser URL.\n' >&2
        exit 1
    fi

    browser="${YNABX_BROWSER:-/usr/bin/open}"
    if [[ ! -x "$browser" ]] && ! command -v "$browser" >/dev/null 2>&1; then
        printf 'Browser launcher is unavailable: %s\n' "$browser" >&2
        exit 1
    fi
    "$browser" "$url"
