#!/usr/bin/env bash
set -Eeuo pipefail

MODEL="${1:-qwen2.5:3b}"
REPOSITORY_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ASSISTANT_ENV_FILE:-$REPOSITORY_DIR/.env}"

set_env_value() {
    local key="$1"
    local value="$2"
    local escaped_value

    escaped_value="$(printf '%s' "$value" | sed 's/[\\/&]/\\\\&/g')"
    if grep -q "^${key}=" "$ENV_FILE"; then
        sed -i "s|^${key}=.*|${key}=${escaped_value}|" "$ENV_FILE"
    else
        printf '\n%s=%s\n' "$key" "$value" >> "$ENV_FILE"
    fi
}

if ! command -v ollama >/dev/null 2>&1; then
    curl -fsSL https://ollama.com/install.sh | sh
fi

if command -v systemctl >/dev/null 2>&1; then
    sudo systemctl enable --now ollama
fi

ollama pull "$MODEL"
curl --fail --silent --show-error http://127.0.0.1:11434/api/tags >/dev/null

touch "$ENV_FILE"
set_env_value "ASSISTANT_LOCAL_AI_ENABLED" "1"
set_env_value "ASSISTANT_LOCAL_AI_URL" "http://127.0.0.1:11434/api/chat"
set_env_value "ASSISTANT_LOCAL_AI_MODEL" "$MODEL"
set_env_value "ASSISTANT_LOCAL_AI_TIMEOUT" "90"
set_env_value "ASSISTANT_LOCAL_AI_CONTEXT_CHARS" "12000"

printf 'Configured Aref local AI with model: %s\n' "$MODEL"
printf 'Restart the Aref application service to load the updated .env file.\n'
