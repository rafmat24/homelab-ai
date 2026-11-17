#!/bin/bash

# ==============================================================================
# AI ORCHESTRATOR - LXC DEPLOYMENT SCRIPT
# ==============================================================================

# Global Configuration
LXC_TEMPLATE="local:vztmpl/debian-13-standard_13.1-2_amd64.tar.zst"
NET_BRIDGE="vmbr0"
NET_GATEWAY="192.168.1.1"
HOMELAB_AI_DIR="${HOMELAB_AI_DIR:-/opt/homelab-ai}"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
HOMELAB_AI_SOURCE_DIR="${HOMELAB_AI_SOURCE_DIR:-$SCRIPT_DIR/runtime}"
LEGACY_RUNTIME_DIR="$SCRIPT_DIR/.lxc-code"
OLDER_RUNTIME_DIR="$SCRIPT_DIR/.lxc-runtime"

CONFIG_FILE=""
usage() {
    cat <<EOF
Usage: $0 [--config FILE]

Without --config, the script prompts for each required setting.
The config file uses plain KEY=value lines (no shell expansion):
  CT_ID=200
  CT_IP=192.168.1.60/24
  RTX_IP=192.168.1.10
  RTX_MAC=AA:BB:CC:DD:EE:FF
  RTX_USER=rafal

Keep the config file private (for example: chmod 600 FILE).
EOF
}

if [[ $# -gt 0 ]]; then
    case "$1" in
        -h|--help)
            usage
            exit 0
            ;;
        -c|--config)
            if [[ $# -ne 2 || -z "$2" ]]; then
                usage >&2
                exit 2
            fi
            CONFIG_FILE="$2"
            ;;
        *)
            echo "[X] Unknown argument: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
fi

if [[ "$HOMELAB_AI_DIR" != /* ]]; then
    echo "[X] HOMELAB_AI_DIR must be an absolute path: $HOMELAB_AI_DIR" >&2
    exit 1
fi

if [[ "$HOMELAB_AI_SOURCE_DIR" != /* || ! -d "$HOMELAB_AI_SOURCE_DIR" ]]; then
    echo "[X] HOMELAB_AI_SOURCE_DIR must be an existing absolute directory: $HOMELAB_AI_SOURCE_DIR" >&2
    exit 1
fi

if [[ "$HOMELAB_AI_SOURCE_DIR" == *,* || "$HOMELAB_AI_DIR" == *,* ]]; then
    echo "[X] Mount paths cannot contain commas." >&2
    exit 1
fi

HOMELAB_AI_SOURCE_DIR="$(cd -- "$HOMELAB_AI_SOURCE_DIR" && pwd)"

# Log Colors
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

echo -e "${GREEN}[*] Starting AI Orchestrator deployment...${NC}"
echo -e "${CYAN}[*] Writable runtime state directory in container: ${HOMELAB_AI_DIR}${NC}"
echo -e "${CYAN}[*] Mounting runtime code directly from: ${HOMELAB_AI_SOURCE_DIR}${NC}"
echo -e "${CYAN}[*] Runtime code will be mounted read-only at ${HOMELAB_AI_DIR}/code.${NC}"

# ==============================================================================
# HELPER FUNCTIONS
# ==============================================================================

container_exists() {
    pct status "$1" >/dev/null 2>&1
    if [ $? -eq 0 ]; then
        echo -e "${YELLOW}[-] Container $1 already exists.${NC}"
        return 0
    fi
    return 1
}

validate_runtime_files() {
    local file
    local -a runtime_files=(
        agent.py
        market_data.py
        compile_weekly_context.py
        run-daily.sh
        wake-pc.sh
        portfolio.json
    )

    for file in "${runtime_files[@]}"; do
        if [[ ! -f "$HOMELAB_AI_SOURCE_DIR/$file" ]]; then
            echo -e "${RED}[X] Required runtime file not found: $HOMELAB_AI_SOURCE_DIR/$file${NC}" >&2
            return 1
        fi
    done

    return 0
}

configure_runtime_mount() {
    local id="$1"
    local current_mount
    local expected_mount="${HOMELAB_AI_SOURCE_DIR},mp=${HOMELAB_AI_DIR}/code,ro=1"

    current_mount="$(pct config "$id" | sed -n 's/^mp0: //p')"
    if [[ "$current_mount" == "$expected_mount" ]]; then
        return 0
    fi

    if [[ "$current_mount" == "${LEGACY_RUNTIME_DIR},mp=${HOMELAB_AI_DIR}/code,ro=1" ||
          "$current_mount" == "${OLDER_RUNTIME_DIR},mp=${HOMELAB_AI_DIR}/code,ro=1" ||
          "$current_mount" == "${HOMELAB_AI_SOURCE_DIR},mp=${HOMELAB_AI_DIR}" ||
          "$current_mount" == "${SCRIPT_DIR},mp=${HOMELAB_AI_DIR}/code,ro=1" ||
          "$current_mount" == "${LEGACY_RUNTIME_DIR},mp=${HOMELAB_AI_DIR}" ||
          "$current_mount" == "${OLDER_RUNTIME_DIR},mp=${HOMELAB_AI_DIR}" ||
          "$current_mount" == "${SCRIPT_DIR},mp=${HOMELAB_AI_DIR}" ]]; then
        if pct status "$id" | grep -q 'status: running'; then
            echo -e "${RED}[X] Stop container $id before moving its old project mount to the read-only code directory.${NC}" >&2
            return 1
        fi
        pct set "$id" --delete mp0 || return 1
        current_mount=""
    fi

    if [[ -n "$current_mount" ]]; then
        echo -e "${RED}[X] Container $id already has a different mp0 mount: $current_mount${NC}" >&2
        return 1
    fi

    if pct status "$id" | grep -q 'status: running'; then
        echo -e "${RED}[X] Container $id is running without the project mount. Stop it, then rerun bootstrap.${NC}" >&2
        return 1
    fi

    if pct set "$id" --mp0 "$expected_mount"; then
        return 0
    fi

    return 1
}

# ==============================================================================
# INTERACTIVE STEP: Prompt for Variables
# ==============================================================================
if [[ -n "$CONFIG_FILE" ]]; then
    if [[ ! -f "$CONFIG_FILE" || ! -r "$CONFIG_FILE" ]]; then
        echo "[X] Config file is not a readable regular file: $CONFIG_FILE" >&2
        exit 1
    fi

    CT_ID=""
    CT_IP=""
    RTX_IP=""
    RTX_MAC=""
    RTX_USER=""
    seen_keys=" "
    while IFS= read -r line || [[ -n "$line" ]]; do
        line="${line%$'\r'}"
        line="${line#"${line%%[![:space:]]*}"}"
        [[ -z "$line" || "$line" == \#* ]] && continue
        if [[ "$line" != *=* ]]; then
            echo "[X] Invalid config line; expected KEY=value." >&2
            exit 1
        fi

        key="${line%%=*}"
        value="${line#*=}"
        key="${key%"${key##*[![:space:]]}"}"
        value="${value#"${value%%[![:space:]]*}"}"
        value="${value%"${value##*[![:space:]]}"}"

        case "$key" in
            CT_ID|CT_IP|RTX_IP|RTX_MAC|RTX_USER)
                ;;
            ALPHAVANTAGE_API_KEY)
                ;;
            *)
                echo "[X] Unsupported config key: $key" >&2
                exit 1
                ;;
        esac
        case "$seen_keys" in
            *" $key "*)
                echo "[X] Duplicate config key: $key" >&2
                exit 1
                ;;
        esac
        seen_keys+="$key "
        printf -v "$key" '%s' "$value"
    done < "$CONFIG_FILE"
    case "$seen_keys" in
        *" ALPHAVANTAGE_API_KEY "*)
            echo "[!] Ignoring legacy ALPHAVANTAGE_API_KEY; Alpha Vantage is archived."
            ;;
    esac
else
    echo -e "${CYAN}------------------------------------------------------------${NC}"
    echo -e "${CYAN}[?] CONFIGURATION STEP${NC}"
    echo -e "${CYAN}------------------------------------------------------------${NC}"
    read -r -p "1. Enter Container ID (e.g., 200): " CT_ID
    read -r -p "2. Enter Container IP with CIDR (e.g., 192.168.1.60/24): " CT_IP
    echo -e "${CYAN}--- RTX Station Details ---${NC}"
    read -r -p "3. Enter RTX Station IP Address (e.g., 192.168.1.10): " RTX_IP
    read -r -p "4. Enter RTX Station MAC Address (e.g., AA:BB:CC:DD:EE:FF): " RTX_MAC
    read -r -p "5. Enter RTX Station SSH User (e.g., rafal): " RTX_USER
fi

for key in CT_ID CT_IP RTX_IP RTX_MAC RTX_USER; do
    if [[ -z "${!key:-}" ]]; then
        echo "[X] Missing required config value: $key" >&2
        exit 1
    fi
done

if [[ "$CT_ID" == *[!0-9]* ]]; then
    echo "[X] CT_ID must contain only digits." >&2
    exit 1
fi
echo -e ""

# ==============================================================================
# SETUP FUNCTION: Python Environment & Configuration
# ==============================================================================
setup_ai_orchestrator() {
    local id="$1"
    echo -e "${YELLOW}[->] Preparing Python environment for AI Orchestrator...${NC}"

    # Keep the generated configuration in the Proxmox project directory and in the container.
    echo -e "${YELLOW}[->] Writing .env configuration...${NC}"
    umask 077
    if ! cat <<EOF > "$SCRIPT_DIR/.env"
# RTX Station Configuration
RTX_IP=${RTX_IP}
RTX_MAC=${RTX_MAC}
RTX_SSH_USER=${RTX_USER}

# Ollama Endpoint
OLLAMA_URL=http://${RTX_IP}:11434/api/generate
EOF
    then
        echo -e "${RED}[X] Failed to write $SCRIPT_DIR/.env.${NC}" >&2
        return 1
    fi
    if ! chmod 600 "$SCRIPT_DIR/.env"; then
        echo -e "${RED}[X] Failed to secure $SCRIPT_DIR/.env.${NC}" >&2
        return 1
    fi
    if ! pct exec "$id" -- mkdir -p "$HOMELAB_AI_DIR" ||
       ! pct push "$id" "$SCRIPT_DIR/.env" "$HOMELAB_AI_DIR/.env" ||
       ! pct exec "$id" -- chmod 600 "$HOMELAB_AI_DIR/.env"; then
        echo -e "${RED}[X] Failed to install the protected .env file in the container.${NC}" >&2
        return 1
    fi
    
    # 1. Update and install system dependencies
    if ! pct exec "$id" -- apt-get update -y ||
       ! pct exec "$id" -- apt-get install -y python3 python3-pip python3-venv git cron openssh-client iputils-ping sqlite3 curl; then
        echo -e "${RED}[X] Failed to install system dependencies.${NC}" >&2
        return 1
    fi

    # 2. Create directory structure and virtual environment
    if ! pct exec "$id" -- mkdir -p "$HOMELAB_AI_DIR/logs" ||
       ! pct exec "$id" -- python3 -m venv "$HOMELAB_AI_DIR/venv"; then
        echo -e "${RED}[X] Failed to create the writable runtime directory or virtual environment.${NC}" >&2
        return 1
    fi
    
    # 3. Install Python packages inside venv
    echo -e "${YELLOW}[->] Installing Python packages from requirements.txt...${NC}"
    if ! pct push "$id" "$SCRIPT_DIR/requirements.txt" "$HOMELAB_AI_DIR/requirements.txt" ||
       ! pct exec "$id" -- "$HOMELAB_AI_DIR/venv/bin/pip" install -r "$HOMELAB_AI_DIR/requirements.txt"; then
        echo -e "${RED}[X] Failed to install Python packages.${NC}" >&2
        return 1
    fi

    # 4. Generate SSH key for passwordless RTX shutdown
    echo -e "${YELLOW}[->] Generating SSH keys for passwordless RTX shutdown...${NC}"
    if ! pct exec "$id" -- bash -c 'if [ ! -f /root/.ssh/id_ed25519 ]; then ssh-keygen -t ed25519 -f /root/.ssh/id_ed25519 -N ""; fi'; then
        echo -e "${RED}[X] Failed to generate the SSH key.${NC}" >&2
        return 1
    fi
    
    PUB_KEY=$(pct exec "$id" -- cat /root/.ssh/id_ed25519.pub)

    echo -e "${GREEN}[+] AI Orchestrator setup completed inside container.${NC}"
}

# ==============================================================================
# MAIN EXECUTION
# ==============================================================================

if ! validate_runtime_files; then
    echo -e "${RED}[X] Runtime code directory is incomplete.${NC}" >&2
    exit 1
fi

if ! container_exists "$CT_ID"; then
    echo -e "${GREEN}[+] Creating ai-finance container (${CT_ID})...${NC}"
    
    if ! pct create "$CT_ID" "$LXC_TEMPLATE" \
        -cores 1 -memory 1024 -rootfs local-lvm:8 \
        -net0 name=eth0,bridge="$NET_BRIDGE",ip="$CT_IP",gw="$NET_GATEWAY" \
        -ostype debian -hostname "ai-finance" -onboot 1; then
        echo -e "${RED}[X] Failed to create LXC $CT_ID.${NC}" >&2
        exit 1
    fi

    if configure_runtime_mount "$CT_ID" && pct start "$CT_ID"; then
        setup_ai_orchestrator "$CT_ID" || exit 1
    else
        echo -e "${RED}[X] Failed to configure and start LXC $CT_ID.${NC}" >&2
        exit 1
    fi
else
    if configure_runtime_mount "$CT_ID"; then
        if ! pct status "$CT_ID" | grep -q 'status: running'; then
            pct start "$CT_ID" || exit 1
        fi
        setup_ai_orchestrator "$CT_ID" || exit 1
    else
        exit 1
    fi
fi

echo -e "${GREEN}[*] Script execution finished.${NC}"

# Display the SSH key for RTX and OpenWRT authorization
if [ -n "$PUB_KEY" ]; then
    echo -e "${CYAN}============================================================${NC}"
    echo -e "${CYAN}[!] IMPORTANT: AUTHORIZE THIS SSH KEY ON BOTH TARGETS${NC}"
    echo -e "${CYAN}Append this public key to the RTX user's authorized_keys file${NC}"
    echo -e "${CYAN}to allow shutdown commands, and to OpenWRT's /etc/dropbear/authorized_keys${NC}"
    echo -e "${CYAN}to allow Wake-on-LAN commands. Do not overwrite existing keys.${NC}"
    echo -e "${CYAN}(Windows path: C:\\Users\\${RTX_USER}\\.ssh\\authorized_keys)${NC}"
    echo -e "${CYAN}(Linux path: /home/${RTX_USER}/.ssh/authorized_keys)${NC}"
    echo -e "${CYAN}(OpenWRT path: /etc/dropbear/authorized_keys for the root account)${NC}"
    echo -e "${CYAN}------------------------------------------------------------${NC}"
    echo -e "${YELLOW}$PUB_KEY${NC}"
    echo -e "${CYAN}============================================================${NC}"
    
    # Save a backup of the public key on the Proxmox host.
    echo "$PUB_KEY" > "/root/ai_orchestrator_${CT_ID}_pubkey.txt"
    echo -e "${CYAN}[i] A backup of this key has been saved to /root/ai_orchestrator_${CT_ID}_pubkey.txt${NC}"
fi