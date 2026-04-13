#!/usr/bin/env bash
#
# Sets up the test repo (deblasis/stacked-prs-test) with three stacked
# branches and corresponding PRs, plus the stack config in this repo.
#
set -euo pipefail

TEST_REPO="deblasis/stacked-prs-test"
TMPDIR=$(mktemp -d)

echo "=== Creating test repo ==="
gh repo create "$TEST_REPO" --public \
  --description "Test repo for stacked-prs auto-rebase" \
  --clone --add-readme || true

cd "$TMPDIR"
gh repo clone "$TEST_REPO" repo
cd repo

# ── main branch: base content ──────────────────────────────────
echo "# stacked-prs-test" > README.md
echo "Base content" > base.txt
git add -A && git commit -m "Initial commit" --allow-empty || true
git push -u origin main

# ── feature-a (PR #1) ──────────────────────────────────────────
git checkout -b feature-a
cat > feature-a.txt << 'EOF'
Feature A implementation
- adds the first layer of functionality
EOF
git add feature-a.txt && git commit -m "feat: add feature A"
git push -u origin feature-a

# ── feature-b stacked on feature-a (PR #2) ─────────────────────
git checkout -b feature-b
cat > feature-b.txt << 'EOF'
Feature B implementation
- builds on feature A
- adds the second layer
EOF
git add feature-b.txt && git commit -m "feat: add feature B"
git push -u origin feature-b

# ── feature-c stacked on feature-b (PR #3) ─────────────────────
git checkout -b feature-c
cat > feature-c.txt << 'EOF'
Feature C implementation
- builds on feature B
- completes the stack
EOF
git add feature-c.txt && git commit -m "feat: add feature C"
git push -u origin feature-c

# ── Create PRs (stacked targeting) ─────────────────────────────
git checkout main

PR1=$(gh pr create --repo "$TEST_REPO" \
  --head feature-a --base main \
  --title "feat: Feature A" \
  --body "Part 1 of 3 in the stack." \
  --json number -q .number)
echo "Created PR #$PR1 (feature-a → main)"

PR2=$(gh pr create --repo "$TEST_REPO" \
  --head feature-b --base feature-a \
  --title "feat: Feature B" \
  --body "Part 2 of 3 — stacked on Feature A." \
  --json number -q .number)
echo "Created PR #$PR2 (feature-b → feature-a)"

PR3=$(gh pr create --repo "$TEST_REPO" \
  --head feature-c --base feature-b \
  --title "feat: Feature C" \
  --body "Part 3 of 3 — stacked on Feature B." \
  --json number -q .number)
echo "Created PR #$PR3 (feature-c → feature-b)"

# ── Write stack config ──────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
mkdir -p "$SCRIPT_DIR/stacks"

cat > "$SCRIPT_DIR/stacks/test-stack.yml" << EOF
repo: $TEST_REPO
fork: $TEST_REPO
base: main
prs:
  - branch: feature-a
    pr: $PR1
    status: open
  - branch: feature-b
    pr: $PR2
    status: open
  - branch: feature-c
    pr: $PR3
    status: open
EOF

echo ""
echo "=== Done ==="
echo "Test repo: https://github.com/$TEST_REPO"
echo "Stack config written to stacks/test-stack.yml"
echo ""
echo "To test: merge PR #$PR1 on GitHub, then run:"
echo "  python stack_manager.py"
echo ""
echo "Temp clone at: $TMPDIR/repo"

# cleanup
rm -rf "$TMPDIR"
