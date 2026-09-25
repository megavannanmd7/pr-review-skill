#!/usr/bin/env bash
# Install the pr-review skill into Antigravity (IDE + CLI) on macOS / Linux.
set -euo pipefail

src="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/skills/pr-review"
[ -f "$src/SKILL.md" ] || { echo "cannot find skills/pr-review/SKILL.md next to this script" >&2; exit 1; }

for dest in "$HOME/.gemini/config/skills/pr-review" "$HOME/.gemini/antigravity-cli/skills/pr-review"; do
  rm -rf "$dest"
  mkdir -p "$(dirname "$dest")"
  cp -R "$src" "$dest"
  echo "installed -> $dest"
done

echo
echo "Checking prerequisites..."
command -v python3 >/dev/null 2>&1 && echo "  python3: $(command -v python3)" || echo "  WARNING: python3 not found"
if command -v gh >/dev/null 2>&1; then
  echo "  gh: $(command -v gh)"
  gh auth status || echo "  WARNING: gh is installed but not authenticated. Run: gh auth login"
else
  echo "  WARNING: GitHub CLI not found. Install it (brew install gh) and run: gh auth login"
fi

echo
echo "Done. Restart Antigravity, then try:  /pr-review 842 paxiai-event-processor"
