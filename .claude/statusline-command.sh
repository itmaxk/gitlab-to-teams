#!/usr/bin/env bash
# Claude Code status line — mirrors Git Bash PS1 from git-prompt.sh
input=$(cat)
cwd=$(echo "$input" | jq -r '.cwd // .workspace.current_dir // empty')
[ -z "$cwd" ] && cwd=$(pwd)

user=$(whoami)
host=$(hostname -s)
msystem="${MSYSTEM:-MINGW64}"

# Git branch (skip optional locks)
git_branch=""
if git_branch_raw=$(GIT_OPTIONAL_LOCKS=0 git -C "$cwd" symbolic-ref --short HEAD 2>/dev/null); then
  git_branch=" ($git_branch_raw)"
fi

printf "\033[32m%s@%s \033[35m%s \033[33m%s\033[36m%s\033[0m" \
  "$user" "$host" "$msystem" "$cwd" "$git_branch"
