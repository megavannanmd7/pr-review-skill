#!/usr/bin/env bash
# Install the pr-review skill for one or more AI coding tools (macOS / Linux).
#
#   ./install.sh --all
#   ./install.sh --platform claude-code,cursor
#
# Copies core/ to ~/.pr-review-skill/core and drops a small adapter into each
# selected tool's own skill/command directory. Every adapter points back at that one
# core, so there is never more than one copy of the rubric or the scripts.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
core_dest="$HOME/.pr-review-skill/core"
platforms=""

while [ $# -gt 0 ]; do
  case "$1" in
    --all) platforms="antigravity,claude-code,cursor,gemini-cli"; shift ;;
    --platform) platforms="$2"; shift 2 ;;
    --platform=*) platforms="${1#*=}"; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

[ -f "$root/core/REVIEW.md" ] || { echo "cannot find core/REVIEW.md next to this script" >&2; exit 1; }
if [ -z "$platforms" ]; then
  echo "Usage: ./install.sh --all"
  echo "       ./install.sh --platform antigravity,claude-code,cursor,gemini-cli"
  exit 1
fi

install_file() {  # src dest
  mkdir -p "$(dirname "$2")"
  cp -f "$1" "$2"
  echo "  -> $2"
}

echo "Installing shared core..."
rm -rf "$core_dest"
mkdir -p "$(dirname "$core_dest")"
cp -R "$root/core" "$core_dest"
find "$core_dest" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
echo "  -> $core_dest"

IFS=',' read -ra selected <<< "$platforms"
for p in "${selected[@]}"; do
  echo "Installing adapter: $p"
  case "$p" in
    antigravity)
      for d in "$HOME/.gemini/config/skills/pr-review" "$HOME/.gemini/antigravity-cli/skills/pr-review"; do
        rm -rf "$d"
        install_file "$root/adapters/antigravity/SKILL.md" "$d/SKILL.md"
      done
      ;;
    claude-code)
      rm -rf "$HOME/.claude/skills/pr-review"
      install_file "$root/adapters/claude-code/SKILL.md" "$HOME/.claude/skills/pr-review/SKILL.md"
      ;;
    cursor)
      install_file "$root/adapters/cursor/pr-review.md" "$HOME/.cursor/commands/pr-review.md"
      ;;
    gemini-cli)
      install_file "$root/adapters/gemini-cli/pr-review.toml" "$HOME/.gemini/commands/pr-review.toml"
      ;;
    *)
      echo "  unknown platform: $p (expected antigravity, claude-code, cursor or gemini-cli)" >&2
      exit 2
      ;;
  esac
done

echo
echo "Checking prerequisites..."
if command -v python3 >/dev/null 2>&1; then echo "  python3: $(command -v python3)"; else echo "  WARNING: python3 not found"; fi
if command -v gh >/dev/null 2>&1; then
  echo "  gh: $(command -v gh)"
  gh auth status || echo "  WARNING: gh is installed but not authenticated. Run: gh auth login"
else
  echo "  WARNING: GitHub CLI not found. Install it (brew install gh) and run: gh auth login"
fi

echo
echo "Done. Restart the tool, then run:  /pr-review 842 paxiai-event-processor"
