#!/usr/bin/env bash

# Source-only URL and filesystem parser; tmux_url_* names retain tmux compatibility.

tmux_url_trim() {
    local value="$1"
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"
    printf '%s\n' "$value"
}

tmux_url_strip_wrapping() {
    local value
    local opens closes last_char
    value=$(tmux_url_trim "$1")
    value="${value#file://}"
    value="${value#<}"
    value="${value%>}"
    value="${value#\"}"
    value="${value%\"}"
    value="${value#\'}"
    value="${value%\'}"
    value="${value#(}"

    while [ -n "$value" ]; do
        case "${value: -1}" in
        . | , | ';' | ':' | '!' | '?' | ']' | '}') value="${value%?}" ;;
        *) break ;;
        esac
    done

    while [ -n "$value" ]; do
        last_char="${value: -1}"
        [ "$last_char" = ")" ] || break
        opens=$(printf '%s' "$value" | tr -cd '(' | wc -c | tr -d ' ')
        closes=$(printf '%s' "$value" | tr -cd ')' | wc -c | tr -d ' ')
        [ "$closes" -gt "$opens" ] || break
        value="${value%?}"
    done

    printf '%s\n' "$value"
}

tmux_url_normalize_url() {
    local url="$1"
    if [[ "$url" =~ ^https?:// ]]; then
        printf '%s\n' "$url"
    else
        printf 'https://%s\n' "$url"
    fi
}

tmux_url_add_target() {
    local type="$1"
    local value="$2"
    local label="$3"
    local line="${4:-}"

    [ -n "$value" ] || return 0
    printf '%s\t%s\t%s\t%s\n' "$type" "$value" "$label" "$line"
}

tmux_url_extract_urls() {
    local styled_file="$1"

    perl -nle 'while (m{(https?://[a-zA-Z0-9./?=_:%+#&~@!*(),;_%-]+|(?<![a-zA-Z0-9_.@-])[a-zA-Z0-9][a-zA-Z0-9.-]*\.(?:com|org|net|io|dev|co\.uk|gov\.uk|edu|info|me|app|lighting)(?!:[^0-9])(?::[0-9]+)?(?:[/?#][a-zA-Z0-9./?=_:%+#&~@!*(),;_%-]*)?)}ig) { print $1 }' "$styled_file" |
        while IFS= read -r raw_url; do
            local url clean_url
            url=$(tmux_url_strip_wrapping "$raw_url")
            [ -n "$url" ] || continue
            clean_url="${url#*://}"
            tmux_url_add_target "url" "$(tmux_url_normalize_url "$url")" "$clean_url"
        done || true
}

tmux_url_looks_like_file_name() {
    local value="$1"
    [[ "$value" =~ \.(md|markdown|txt|log|json|jsonc|ya?ml|toml|typ|xml|csv|tsv|sh|bash|zsh|fish|js|jsx|ts|tsx|mjs|cjs|py|rb|go|rs|lua|vim|java|kt|kts|c|cc|cpp|h|hpp|cs|php|swift|sql|tf|tfvars|nix|conf|ini|env|pdf|png|jpe?g|gif|webp|svg|heic|mp4|mov|m4v|webm|mkv|avi|mp3|wav|zip|tar|tgz|gz|bz2|xz|7z|rar)$ ]]
}

tmux_url_resolve_path() {
    local raw="$1"
    local pane_cwd="$2"
    local home_dir="$3"
    local value line candidate resolved
    value=$(tmux_url_strip_wrapping "$raw")

    [ -n "$value" ] || return 1
    [[ "$value" =~ ^https?:// ]] && return 1
    [[ "$value" =~ ^mailto: ]] && return 1
    value="${value%%\?*}"
    value="${value%%#*}"

    line=""
    if [[ "$value" =~ ^(.+):([0-9]+):[0-9]+$ ]]; then
        line="${BASH_REMATCH[2]}"
        value="${BASH_REMATCH[1]}"
    elif [[ "$value" =~ ^(.+):([0-9]+)$ ]]; then
        line="${BASH_REMATCH[2]}"
        value="${BASH_REMATCH[1]}"
    fi

    case "$value" in
    \~/*) candidate="$home_dir/${value#\~/}" ;;
    /*) candidate="$value" ;;
    ./* | ../*) candidate="$pane_cwd/$value" ;;
    *)
        if [[ "$value" == */* ]] || tmux_url_looks_like_file_name "$value"; then
            candidate="$pane_cwd/$value"
        else
            return 1
        fi
        ;;
    esac

    if [ -e "$candidate" ]; then
        resolved=$(cd "$(dirname "$candidate")" && pwd -P)/$(basename "$candidate")
        printf '%s\t%s\n' "$resolved" "$line"
        return 0
    fi
    return 1
}

tmux_url_add_continuation_blocker() {
    local candidate="$1"
    local output_file="$2"
    local pane_cwd="$3"
    local home_dir="$4"
    candidate=$(tmux_url_trim "$candidate")

    [ -n "$candidate" ] || return 0
    tmux_url_resolve_path "$candidate" "$pane_cwd" "$home_dir" >/dev/null || return 0
    printf '%s\n' "$candidate" >>"$output_file"
}

tmux_url_collect_continuation_blockers() {
    local source_file="$1"
    local output_file="$2"
    local pane_cwd="$3"
    local home_dir="$4"
    local line remainder without_pipes

    : >"$output_file"
    while IFS= read -r line || [ -n "$line" ]; do
        tmux_url_add_continuation_blocker "$line" "$output_file" "$pane_cwd" "$home_dir"

        remainder="$line"
        while [[ "$remainder" == *'│'* ]]; do
            remainder="${remainder#*│}"
            tmux_url_add_continuation_blocker "$remainder" "$output_file" "$pane_cwd" "$home_dir"
        done

        without_pipes="${line//|/}"
        if [ "$((${#line} - ${#without_pipes}))" -eq 1 ]; then
            tmux_url_add_continuation_blocker "${line#*|}" "$output_file" "$pane_cwd" "$home_dir"
        fi
    done <"$source_file"
}

tmux_url_target_type_for_path() {
    local path="$1"
    local lower
    lower=$(printf '%s\n' "$path" | tr '[:upper:]' '[:lower:]')

    if [ -d "$path" ]; then
        printf 'dir\n'
    elif [[ "$lower" =~ \.(md|markdown)$ ]]; then
        printf 'md\n'
    elif [[ "$lower" =~ \.(txt|log|json|jsonc|ya?ml|toml|typ|xml|csv|tsv|sh|bash|zsh|fish|js|jsx|ts|tsx|mjs|cjs|py|rb|go|rs|lua|vim|java|kt|kts|c|cc|cpp|h|hpp|cs|php|swift|sql|tf|tfvars|nix|conf|ini|env)$ ]]; then
        printf 'text\n'
    elif [[ "$lower" =~ \.pdf$ ]]; then
        printf 'pdf\n'
    elif [[ "$lower" =~ \.(mp4|mov|m4v|webm|mkv|avi)$ ]]; then
        printf 'video\n'
    elif [[ "$lower" =~ \.(png|jpe?g|gif|webp|svg|heic|mp3|wav)$ ]]; then
        printf 'media\n'
    else
        printf 'file\n'
    fi
}

tmux_url_add_file_target_from_raw() {
    local raw="$1"
    local pane_cwd="$2"
    local home_dir="$3"
    local resolved path line type label
    resolved=$(tmux_url_resolve_path "$raw" "$pane_cwd" "$home_dir") || return 0
    path="${resolved%%$'\t'*}"
    line="${resolved#*$'\t'}"
    [ "$line" != "$resolved" ] || line=""
    [ "$path" != "$pane_cwd" ] || return 0
    type=$(tmux_url_target_type_for_path "$path")
    label="${path#"$pane_cwd"/}"
    [ "$label" != "$path" ] || label="$path"
    [ -z "$line" ] || label="$label:$line"
    tmux_url_add_target "$type" "$path" "$label" "$line"
}

tmux_url_extract_markdown_targets() {
    local plain_file="$1"
    local pane_cwd="$2"
    local home_dir="$3"

    perl -nle 'while (/!?\[[^\]]*\]\(([^)]+)\)/g) { print $1 }' "$plain_file" |
        while IFS= read -r raw_path; do
            tmux_url_add_file_target_from_raw "$raw_path" "$pane_cwd" "$home_dir"
        done || true
}

tmux_url_extract_patch_targets() {
    local plain_file="$1"
    local pane_cwd="$2"
    local home_dir="$3"

    grep -E '^\*\*\* (Add|Update) File: |^\*\*\* Move to: ' "$plain_file" |
        sed -E 's/^\*\*\* (Add|Update) File: //; s/^\*\*\* Move to: //' |
        while IFS= read -r raw_path; do
            tmux_url_add_file_target_from_raw "$raw_path" "$pane_cwd" "$home_dir"
        done || true
}

tmux_url_extract_plain_path_targets() {
    local plain_file="$1"
    local pane_cwd="$2"
    local home_dir="$3"

    {
        perl -nle 'while (m{(?:^|[\s"'"'"'`(<])((?:~|/|\.\.?/|[A-Za-z0-9_.-]+/)[^"'"'"'`<>\[\]()|\t\r\n]*?\.(?:md|markdown|txt|log|json|jsonc|ya?ml|toml|typ|xml|csv|tsv|sh|bash|zsh|fish|js|jsx|ts|tsx|mjs|cjs|py|rb|go|rs|lua|vim|java|kt|kts|c|cc|cpp|h|hpp|cs|php|swift|sql|tf|tfvars|nix|conf|ini|env|pdf|png|jpe?g|gif|webp|svg|heic|mp4|mov|m4v|webm|mkv|avi|mp3|wav|zip|tar|tgz|gz|bz2|xz|7z|rar)(?::[0-9]+(?::[0-9]+)?)?)(?=$|[\s"'"'"'`<>|)\]\},;!?])}ig) { print $1 }' "$plain_file"
        grep -Eo '(^|[[:space:]"'"'"'`(<])(~|/|\.\.?/|[A-Za-z0-9_.-]+/)[^[:space:]"'"'"'`<>|]+' "$plain_file" |
            sed -E 's/^[[:space:]"'"'"'`(<]+//'
        grep -Eo '(^|[[:space:]"'"'"'`(<])\.[A-Za-z0-9_.-]+\.(md|markdown|txt|log|json|jsonc|ya?ml|toml|typ|xml|csv|tsv|sh|bash|zsh|fish|js|jsx|ts|tsx|mjs|cjs|py|rb|go|rs|lua|vim|java|kt|kts|c|cc|cpp|h|hpp|cs|php|swift|sql|tf|tfvars|nix|conf|ini|env|pdf|png|jpe?g|gif|webp|svg|heic|mp4|mov|m4v|webm|mkv|avi|mp3|wav|zip|tar|tgz|gz|bz2|xz|7z|rar)(:[0-9]+(:[0-9]+)?)?' "$plain_file" |
            sed -E 's/^[[:space:]"'"'"'`(<]+//'
        grep -Eo '\b[A-Za-z0-9_.-]+\.(md|markdown|txt|log|json|jsonc|ya?ml|toml|typ|xml|csv|tsv|sh|bash|zsh|fish|js|jsx|ts|tsx|mjs|cjs|py|rb|go|rs|lua|vim|java|kt|kts|c|cc|cpp|h|hpp|cs|php|swift|sql|tf|tfvars|nix|conf|ini|env|pdf|png|jpe?g|gif|webp|svg|heic|mp4|mov|m4v|webm|mkv|avi|mp3|wav|zip|tar|tgz|gz|bz2|xz|7z|rar)(:[0-9]+(:[0-9]+)?)?' "$plain_file"
    } |
        while IFS= read -r raw_path; do
            tmux_url_add_file_target_from_raw "$raw_path" "$pane_cwd" "$home_dir"
        done || true
}

tmux_url_extract_file_targets() {
    local plain_file="$1"
    local pane_cwd="$2"
    local home_dir="$3"

    {
        tmux_url_extract_markdown_targets "$plain_file" "$pane_cwd" "$home_dir"
        tmux_url_extract_patch_targets "$plain_file" "$pane_cwd" "$home_dir"
        tmux_url_extract_plain_path_targets "$plain_file" "$pane_cwd" "$home_dir"
    } | awk -F '\t' '!seen[$1 FS $2 FS $4]++'
}

tmux_url_extract_targets() {
    local plain_file="$1"
    local styled_file="$2"
    local pane_cwd="$3"
    local home_dir="$4"

    {
        tmux_url_extract_urls "$styled_file"
        tmux_url_extract_file_targets "$plain_file" "$pane_cwd" "$home_dir"
    } | awk -F '\t' '!seen[$1 FS $2 FS $4]++'
}
