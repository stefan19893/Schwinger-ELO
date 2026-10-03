#!/usr/bin/env bash
# Publish the site: the owner's one step after everything else is prepared.
#
#   ./scripts/go_live.sh          check the preparation, ask once, opt in, start the first deploy
#   ./scripts/go_live.sh --check  only check the preparation, change nothing
#   ./scripts/go_live.sh --off    stop the automation (the deployed site stays online)
#
# What it changes on GitHub: the repository variable PUBLISH_ENABLED and one manual run of
# "Deploy to GitHub Pages". No pipeline logic lives here - the workflows call the CLI.
# From then on "Scrape and update" runs on its schedule (README, "Hosted setup").
set -euo pipefail

cd "$(dirname "$0")/.."
MODE="${1:-live}"
TAG="pipeline-state"
WORKFLOW="deploy_pages.yml"

command -v gh >/dev/null || { echo "gh (GitHub CLI) not found - see https://cli.github.com" >&2; exit 1; }
if ! gh auth status >/dev/null 2>&1; then
  # fall back to the credential git already uses for github.com
  GH_TOKEN="$(printf 'protocol=https\nhost=github.com\n\n' | git credential fill 2>/dev/null | sed -n 's/^password=//p')"
  export GH_TOKEN
  gh auth status >/dev/null 2>&1 || { echo "not logged in to GitHub: run 'gh auth login'" >&2; exit 1; }
fi
REPO="$(gh repo view --json nameWithOwner --jq .nameWithOwner)"

if [ "$MODE" = "--off" ]; then
  gh variable set PUBLISH_ENABLED --repo "$REPO" --body false
  echo "PUBLISH_ENABLED=false: no further crawl or deployment."
  echo "The deployed site stays online. To take it down: Settings -> Pages -> Unpublish site."
  echo "To remove the pipeline state from GitHub: gh release delete $TAG --repo $REPO"
  exit 0
fi

ok=1
check() { if [ "$2" = "yes" ]; then echo "  ok    $1"; else echo "  MISSING $1"; ok=0; fi; }

echo "Preparation for $REPO:"
draft="$(gh release view "$TAG" --repo "$REPO" --json isDraft --jq .isDraft 2>/dev/null || true)"
check "release '$TAG' exists and is a draft" "$([ "$draft" = "true" ] && echo yes || echo no)"
bundles="$(gh release view "$TAG" --repo "$REPO" --json assets --jq '[.assets[].name | select(startswith("schwingen-state-"))] | length' 2>/dev/null || echo 0)"
check "exactly one state bundle on the release (found $bundles)" "$([ "$bundles" = "1" ] && echo yes || echo no)"
pages="$(gh api "repos/$REPO/pages" --jq .build_type 2>/dev/null || true)"
check "GitHub Pages source is GitHub Actions" "$([ "$pages" = "workflow" ] && echo yes || echo no)"
branch="$(gh repo view "$REPO" --json defaultBranchRef --jq .defaultBranchRef.name)"
wf="$(gh workflow view "$WORKFLOW" --repo "$REPO" --json state --jq .state 2>/dev/null || true)"
check "workflow '$WORKFLOW' is on '$branch' and active" "$([ "$wf" = "active" ] && echo yes || echo no)"
current="$(gh variable get PUBLISH_ENABLED --repo "$REPO" 2>/dev/null || true)"
echo "  PUBLISH_ENABLED is currently: ${current:-unset}"

if [ "$ok" != "1" ]; then
  echo "Not ready - see README, 'Hosted setup'." >&2
  exit 1
fi
[ "$MODE" = "--check" ] && { echo "Ready. Nothing was changed."; exit 0; }

cat <<'EOF'

Going live means (README, "What you accept by opting in"):
  - ratings derived from ESV festival results (via schlussgang.ch) become public;
  - athletes not certainly 18 are withheld by name, but their festival rows can be
    matched against the public result lists;
  - noindex covers the HTML pages only; objections come in through public GitHub issues
    unless contact_email is set;
  - the weekly crawl and deploy then run on their own until you run this script with --off.
EOF
read -r -p "Type 'publish' to go live: " answer
[ "$answer" = "publish" ] || { echo "Aborted. Nothing was changed."; exit 1; }

gh variable set PUBLISH_ENABLED --repo "$REPO" --body true
gh workflow run "$WORKFLOW" --repo "$REPO" --ref "$branch"
echo "Started. Waiting for the run to appear ..."
sleep 8
run="$(gh run list --repo "$REPO" --workflow "$WORKFLOW" --limit 1 --json databaseId --jq '.[0].databaseId')"
gh run watch "$run" --repo "$REPO" --exit-status || {
  echo "The deploy run failed - nothing new was published. Log: gh run view $run --repo $REPO --log-failed" >&2
  echo "The automation is still switched on; './scripts/go_live.sh --off' switches it off." >&2
  exit 1
}
echo "Published: $(gh api "repos/$REPO/pages" --jq .html_url)"
test "$(gh release view "$TAG" --repo "$REPO" --json isDraft --jq .isDraft)" = "true" \
  && echo "The state release is still a draft." \
  || echo "WARNING: release '$TAG' is no longer a draft - make it a draft again now." >&2
