Run the following git push workflow exactly. Do not deviate from the steps or add extra operations.

## Step 1 — Safety checks

**Check A — branch detection:**
```bash
git branch --show-current
```
- If the result is `main` or `master`, note that we are on main and **skip to Step 3**.
- Otherwise, run Check B.

**Check B — no unpushed commits (only when already on a feature branch):**
```bash
git log @{u}.. --oneline 2>/dev/null || echo "no-upstream"
```
If there are any unpushed commits listed, stop and tell the user:
> "There are already unpushed commits on this branch. Please push or reset them before using /git-push, so every push is intentional and traceable."

If Check B passes, proceed to Step 2.

---

## Step 2 — Confirm there is something to push

```bash
git status --short
```

If the working tree is completely clean (no changes), stop and tell the user:
> "Nothing to commit — working tree is clean."

---

## Step 3 — Auto-generate branch name

Do NOT ask the user for a branch name. Instead, inspect the changed files and diffs to infer both the **prefix** and a short **slug**:

**Prefix rules (pick the first that matches):**

| Prefix | When to use |
|--------|-------------|
| `fix/` | Bug fix, error correction, broken behavior |
| `test/` | Only test files changed (`tests/`, `*.test.*`, `*.spec.*`) |
| `docs/` | Only documentation files changed (`*.md`, `docs/`) |
| `chore/` | Config, tooling, deps, CI, non-functional cleanup |
| `refactor/` | Restructuring without behavior change |
| `feature/` | Everything else — new capability or addition |

**Slug rules:**
- Read `git diff --stat HEAD` (or `git status --short` if on main with no prior commit) to understand what changed.
- Derive a 2–4 word summary of the change. Lowercase, words joined by hyphens, no special chars.
- Example: changed files in `app/edits/` adding a rule extractor → `feature/rule-extractor`

Construct the full branch name as `<prefix>/<slug>` and proceed to Step 4.

---

## Step 4 — Inspect changes and create branch

Run all three in parallel:
```bash
git status
```
```bash
git diff
```
```bash
git log --oneline -5
```

Then create the branch:
```bash
git checkout -b <branch-name>
```

---

## Step 5 — Stage specific files and commit

Stage only the files shown in `git status` — add them by name, never use `git add -A` or `git add .`. Do not stage files that look like secrets (`.env`, credentials, key files).

Show the staged diff summary:
```bash
git diff --cached --stat
```

Draft a concise one-line conventional commit message that summarises the staged changes (e.g. `feat: add rule extractor endpoint`). Then commit using a HEREDOC:

```bash
git commit -m "$(cat <<'EOF'
<message>

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

Run `git status` after the commit to verify it succeeded. If a pre-commit hook fails, fix the underlying issue and create a **new** commit — never use `--amend` or `--no-verify`.

---

## Step 6 — Push

```bash
git push -u origin <branch-name>
```

---

## Step 7 — Done message

Print exactly this (fill in the branch name):

```
Branch pushed: <branch-name>

Next steps:
  1. Open a PR on GitHub to merge <branch-name> → main
  2. After merge, delete the remote branch:
       git push origin --delete <branch-name>
  3. Clean up locally:
       git checkout main && git pull && git branch -d <branch-name>
```

Do not do anything else after printing this message.
