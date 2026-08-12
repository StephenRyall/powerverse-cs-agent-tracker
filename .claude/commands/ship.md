Commit the current changes and open a PR.

1. Run `git status` and `git diff` and summarise what actually changed.
2. If we're on `main`, create a branch with a sensible name first.
3. Commit with a conventional-commit message. One commit per logical change, not one big one.
4. Push and open a PR with `gh pr create`, describing what changed and how to verify it.
5. Print the PR URL.

Never push to `main` directly. Never force-push.
