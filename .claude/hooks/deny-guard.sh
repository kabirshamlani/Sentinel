#!/bin/bash
# PreToolUse guard for Bash — defense in depth. Reads the tool-call JSON on
# stdin; blocks pushes, PR/network exfil, secret reads, and destructive commands
# even if a prompt tries to talk the agent into them. Exit 2 = block.
#
# Normalizes whitespace so `git   push` / `git\tpush` can't slip past, and uses a
# git-subcommand ALLOWLIST (deny by default) rather than a fragile denylist.
input=$(cat)
cmd=$(printf '%s' "$input" | python3 -c "import sys,json; print(json.load(sys.stdin).get('tool_input',{}).get('command',''))" 2>/dev/null)
# collapse all whitespace runs to single spaces for matching
norm=$(printf '%s' "$cmd" | tr '\t\n' '  ' | tr -s ' ')

block() { echo "BLOCKED by sentinel deny-guard: $1" >&2; exit 2; }

# 1. hard denies (any git invocation form, gh mutations, destructive, secrets, egress)
case "$norm" in
  *"git push"*|*"git -C"*" push"*|*"gh pr create"*|*"gh pr merge"*|*"gh release"*|\
  *"rm -rf /"*|*"rm -rf ~"*|*"rm -rf \$HOME"*|*":(){"*|*"mkfs"*|*"dd if="*|\
  *"npm publish"*|*"pip install"*" upload"*|*"chmod 777"*|*"sudo "*|\
  *"security find-generic-password"*|*"cat "*".env"*|*"cat "*"/.aws/credentials"*|\
  *"cat "*"id_rsa"*|*"printenv"*|*"env |"*|*"env >"*)
    block "matched hard-deny pattern" ;;
esac
# 2. aws mutations
if printf '%s' "$norm" | grep -Eq 'aws .* (delete|terminate|rm |remove-|put-|create-|update-|modify-)'; then
  block "aws mutation (needs approval via broker)"
fi
# 2b. gog / Google Workspace mutations — the thinking chat core reads via gog freely
# but must NEVER send/mutate directly; those go through the broker via a PROPOSE line.
# Reads (search/list/get/read/cat/info/export/attachment) are allowed; verbs that
# change the world are blocked so a broad-Bash agent can't bypass the approval gate.
if printf '%s' "$norm" | grep -Eq '(^| )gog '; then
  if printf '%s' "$norm" | grep -Eq 'gog .*(gmail (send|reply|forward|trash|delete)|drafts create|(chat|messages) .*send|events (create|delete|update|patch)|(docs|sheets|slides) .*(write|create|delete|update|insert|find-replace|append)|contacts .*(create|delete|update)|delete|--force)'; then
    block "gog mutation (send/create/delete needs approval via a PROPOSE line -> broker)"
  fi
fi
# 3. network egress to non-github/anthropic hosts (crude but blocks the obvious exfil)
if printf '%s' "$norm" | grep -Eq '(curl|wget|nc|ncat) '; then
  if ! printf '%s' "$norm" | grep -Eq '(github\.com|githubusercontent|anthropic|api\.telegram|localhost|127\.0\.0\.1)'; then
    block "outbound network to non-allowlisted host"
  fi
fi
exit 0
