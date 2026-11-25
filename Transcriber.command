#!/bin/bash
# Double-click this file to open Transcriber in your web browser (macOS; also runs on Linux).
# The first start downloads what Transcriber needs, which takes a minute.
cd "$(dirname "$0")" || exit 1
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"

if ! command -v uv >/dev/null 2>&1; then
  echo "Transcriber needs a small free helper program called uv, which installs and runs it."
  echo "It comes from https://astral.sh/uv and goes into your home folder; nothing else on the computer changes."
  printf "Install uv now? [y/N] "
  read -r answer
  case "$answer" in
    y|Y|yes|Yes) curl -LsSf https://astral.sh/uv/install.sh | sh || { echo "uv could not be installed."; read -r -p "Press Return to close."; exit 1; } ;;
    *) echo "Nothing was installed."; read -r -p "Press Return to close."; exit 1 ;;
  esac
  export PATH="$HOME/.local/bin:$PATH"
fi

uv run --project . transcriber app
status=$?
if [ $status -ne 0 ]; then
  echo
  read -r -p "Transcriber stopped with an error (shown above). Press Return to close."
fi
