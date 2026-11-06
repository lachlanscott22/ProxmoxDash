#!/bin/sh
# Publish a new version to GitHub. Nodes that have the dashboard installed will then
# show an "Update" button in Settings.
#
#   ./release.sh                  bump patch (1.0.0 -> 1.0.1), commit everything, tag, push
#   ./release.sh minor "Add X"    bump minor, with a commit message
#   ./release.sh major
#   ./release.sh 2.3.0 "Msg"      set an exact version
#   ./release.sh --no-bump "Msg"  just commit and push (nodes won't see an update)
#
# Options: -y  skip the confirmation prompt
set -e
cd "$(dirname "$0")"

YES=0; BUMP=patch; MSG=""
while [ $# -gt 0 ]; do
  case "$1" in
    -y|--yes) YES=1 ;;
    --no-bump) BUMP=none ;;
    patch|minor|major) BUMP="$1" ;;
    [0-9]*.[0-9]*.[0-9]*) BUMP="$1" ;;
    *) MSG="$1" ;;
  esac
  shift
done

git rev-parse --git-dir >/dev/null 2>&1 || { echo "Not a git repository. Run: git init && git remote add origin <url>" >&2; exit 1; }
URL=$(git remote get-url origin 2>/dev/null) || { echo "No 'origin' remote. Run: git remote add origin https://github.com/<owner>/<repo>.git" >&2; exit 1; }
BRANCH=$(git rev-parse --abbrev-ref HEAD)
[ "$BRANCH" = "main" ] || { echo "You're on '$BRANCH'. Releases come from 'main' (that's where nodes look for VERSION)." >&2; exit 1; }
git fetch -q origin 2>/dev/null || true
if git rev-parse -q --verify origin/main >/dev/null 2>&1 && [ -n "$(git rev-list HEAD..origin/main 2>/dev/null)" ]; then
  echo "origin/main has commits you don't have. Run 'git pull --rebase' first." >&2; exit 1
fi

echo "Running tests..."
PY=""
for c in python3 python py; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c "import sys" >/dev/null 2>&1; then PY="$c"; break; fi
done
[ -n "$PY" ] || { echo "Python 3 not found." >&2; exit 1; }
"$PY" -m unittest discover -s tests >/dev/null 2>&1 || { echo "Tests failed. Run: $PY -m unittest discover -s tests" >&2; exit 1; }

OLD=$(tr -d ' \r\n' < VERSION)
case "$BUMP" in
  none) NEW="$OLD" ;;
  patch|minor|major)
    MA=${OLD%%.*}; REST=${OLD#*.}; MI=${REST%%.*}; PA=${REST#*.}
    case "$BUMP" in
      patch) PA=$((PA+1)) ;;
      minor) MI=$((MI+1)); PA=0 ;;
      major) MA=$((MA+1)); MI=0; PA=0 ;;
    esac
    NEW="$MA.$MI.$PA" ;;
  *) NEW="$BUMP" ;;
esac
[ -n "$MSG" ] || { [ "$BUMP" = none ] && MSG="Update" || MSG="Release $NEW"; }

# owner/repo from the remote, so the dashboard knows where to look for updates
SLUG=$(printf '%s' "$URL" | sed -E 's#^(https?://[^/]*/|git@[^:]*:|ssh://git@[^/]*/)##; s#\.git$##')

echo "Remote : $URL"
echo "Version: $OLD -> $NEW"
echo "Message: $MSG"
git status --short
if [ "$YES" -ne 1 ]; then
  printf "Commit all of the above and push to GitHub? [y/N] "
  read -r A
  [ "$A" = y ] || [ "$A" = Y ] || { echo "Cancelled."; exit 1; }
fi

if [ "$BUMP" != none ]; then
  printf '%s\n' "$NEW" > VERSION
  sed -i.bak -E "s/^VERSION = \"[0-9.]+\"/VERSION = \"$NEW\"/" pve-dash.py && rm -f pve-dash.py.bak
fi
sed -i.bak -E "s#or \"OWNER/pve-dash\"#or \"$SLUG\"#" pve-dash.py && rm -f pve-dash.py.bak

git add -A
git commit -q -m "$MSG" || echo "(nothing new to commit)"
if [ "$BUMP" != none ]; then
  git tag -f "v$NEW" >/dev/null
  git push -q origin "v$NEW" --force   # tag first, so the files exist before VERSION advertises them
fi
git push -q origin main
echo "Done. $SLUG is now at $NEW."
[ "$BUMP" = none ] || echo "Nodes will offer the update within 6 hours, or straight away via Settings > Software > Check now."
