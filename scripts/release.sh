#!/usr/bin/env bash
#
# release.sh X.Y.Z - bump pyproject.toml's version, commit "Release X.Y.Z" and
# create the annotated tag X.Y.Z (no "v" prefix). The backend's equivalent of
# `npm version`. Write the CHANGELOG.md section first; see README "Releasing".
#
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

v="${1:-}"
[[ "$v" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "usage: scripts/release.sh X.Y.Z" >&2; exit 1; }
[[ -z "$(git status --porcelain)" ]] || { echo "working tree not clean" >&2; exit 1; }
if git rev-parse -q --verify "refs/tags/$v" >/dev/null; then
  echo "tag $v already exists" >&2; exit 1
fi
grep -q "^## \[$v\]" CHANGELOG.md || { echo "CHANGELOG.md has no '## [$v]' section" >&2; exit 1; }

python3 - "$v" <<'PY'
import pathlib, re, sys
p = pathlib.Path("pyproject.toml")
s, n = re.subn(r'(?m)^version = "[^"]*"', f'version = "{sys.argv[1]}"', p.read_text(), count=1)
if n != 1:
    sys.exit("pyproject.toml: no [project] version line")
p.write_text(s)
PY

git commit -q -am "Release $v"
git tag -a "$v" -m "Release $v"
echo "Tagged $v. Push with: git push origin main --follow-tags"
