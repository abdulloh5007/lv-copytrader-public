#!/bin/sh
# LVCT user installer. No privilege escalation; Docker is a separate explicit step.
set -eu
umask 077
install_python=0
source_dir=
previous=
for argument in "$@"; do
    case "$argument" in
        --help|-h)
            printf '%s\n' 'LVCT installer — Linux/macOS' 'Options: --ref REV --home DIR --bin-dir DIR --source CHECKOUT --no-path --install-python' 'Installs only lvct and public source. Next: lvct deps; lvct init.'
            exit 0 ;;
        --install-python) install_python=1 ;;
        --source=*) source_dir=${argument#--source=} ;;
    esac
    if [ "$previous" = --source ]; then source_dir=$argument; fi
    previous=$argument
done
python_cmd=
for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
        python_cmd=$candidate
        break
    fi
done
if [ -z "$python_cmd" ] && [ "$install_python" = 1 ]; then
    case "$(uname -s)" in
        Darwin)
            if ! command -v brew >/dev/null 2>&1; then
                printf '%s\n' 'Install Python 3.10+ from https://www.python.org/downloads/macos/ or Homebrew, then retry.' >&2
                exit 1
            fi
            brew install python
            python_cmd="$(brew --prefix)/bin/python3" ;;
        Linux)
            if ! command -v apt-get >/dev/null 2>&1; then
                printf '%s\n' 'Install Python 3.10+ with your OS package manager, then retry.' >&2
                exit 1
            fi
            if [ "$(id -u)" = 0 ]; then
                apt-get update
                apt-get install -y python3 ca-certificates
            else
                sudo apt-get update
                sudo apt-get install -y python3 ca-certificates
            fi
            python_cmd=python3 ;;
        *) printf '%s\n' 'Use install.ps1 on Windows.' >&2; exit 1 ;;
    esac
fi
if [ -z "$python_cmd" ]; then
    printf '%s\n' 'Python 3.10+ is required. Retry with --install-python, or install Python using your OS package manager.' >&2
    exit 1
fi
"$python_cmd" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' || { printf '%s\n' 'Python 3.10+ is required.' >&2; exit 1; }
if [ -n "$source_dir" ]; then
    exec "$python_cmd" "$source_dir/cli/bootstrap.py" "$@"
fi
command -v curl >/dev/null 2>&1 || { printf '%s\n' 'curl is required.' >&2; exit 1; }
work_dir=$(mktemp -d)
trap 'rm -rf "$work_dir"' EXIT HUP INT TERM
curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 \
    https://raw.githubusercontent.com/abdulloh5007/lv-copytrader-public/main/cli/bootstrap.py \
    --output "$work_dir/bootstrap.py"
"$python_cmd" "$work_dir/bootstrap.py" "$@"
