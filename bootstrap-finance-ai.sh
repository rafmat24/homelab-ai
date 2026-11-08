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
HOMELAB_AI_SOURCE_DIR="${HOMELAB_AI_SOURCE_DIR:-$SCRIPT_DIR}"
HOMELAB_AI_RUNTIME_DIR="${HOMELAB_AI_RUNTIME_DIR:-$SCRIPT_DIR/.lxc-runtime}"

if [[ "$HOMELAB_AI_DIR" != /* ]]; then
    echo "[X] HOMELAB_AI_DIR must be an absolute path: $HOMELAB_AI_DIR" >&2
    exit 1
fi

if [[ "$HOMELAB_AI_SOURCE_DIR" != /* || ! -d "$HOMELAB_AI_SOURCE_DIR" ]]; then
    echo "[X] HOMELAB_AI_SOURCE_DIR must be an existing absolute directory: $HOMELAB_AI_SOURCE_DIR" >&2
    exit 1
fi

if [[ "$HOMELAB_AI_SOURCE_DIR" == *,* || "$HOMELAB_AI_DIR" == *,* || "$HOMELAB_AI_RUNTIME_DIR" == *,* ]]; then
    echo "[X] Mount paths cannot contain commas." >&2
    exit 1
fi

HOMELAB_AI_SOURCE_DIR="$(cd -- "$HOMELAB_AI_SOURCE_DIR" && pwd)"

if [[ "$HOMELAB_AI_RUNTIME_DIR" != /* ]]; then
    echo "[X] HOMELAB_AI_RUNTIME_DIR must be an absolute path: $HOMELAB_AI_RUNTIME_DIR" >&2
    exit 1
fi

mkdir -p "$HOMELAB_AI_RUNTIME_DIR" || exit 1
HOMELAB_AI_RUNTIME_DIR="$(cd -- "$HOMELAB_AI_RUNTIME_DIR" && pwd)"

if [[ "$HOMELAB_AI_RUNTIME_DIR" == "$HOMELAB_AI_SOURCE_DIR" || "$HOMELAB_AI_SOURCE_DIR" == "$HOMELAB_AI_RUNTIME_DIR/"* ]]; then
    echo "[X] HOMELAB_AI_RUNTIME_DIR must not contain the project source directory." >&2
    exit 1
fi

# Log Colors
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

echo -e "${GREEN}[*] Starting AI Orchestrator deployment...${NC}"
echo -e "${CYAN}[*] Application directory in container: ${HOMELAB_AI_DIR}${NC}"
echo -e "${CYAN}[*] Staging runtime files in: ${HOMELAB_AI_RUNTIME_DIR}${NC}"
echo -e "${CYAN}[*] Bootstrap is excluded; runtime files, configuration, and runtime state will be mounted.${NC}"

# ==============================================================================
# HELPER FUNCTIONS
# ==============================================================================

check_step_status() {
    if [ $? -ne 0 ]; then
        echo -e "${RED}[X] ERROR: Step '$1' failed. Proceeding...${NC}"
        return 1
    fi
    return 0
}

container_exists() {
    pct status "$1" >/dev/null 2>&1
    if [ $? -eq 0 ]; then
        echo -e "${YELLOW}[-] Container $1 already exists.${NC}"
        return 0
    fi
    return 1
}

stage_runtime_files() {
    local file
    local -a runtime_files=(
        agent.py
        market_data.py
        compile_weekly_context.py
        run-daily.sh
        wake-pc.sh
        portfolio.json
    )

    if [[ -e "$HOMELAB_AI_RUNTIME_DIR/bootstrap-finance-ai.sh" ]]; then
        echo -e "${RED}[X] Runtime mount directory contains bootstrap-finance-ai.sh; choose a clean HOMELAB_AI_RUNTIME_DIR.${NC}" >&2
        return 1
    fi

    for file in "${runtime_files[@]}"; do
        if [[ ! -f "$HOMELAB_AI_SOURCE_DIR/$file" ]]; then
            echo -e "${RED}[X] Required runtime file not found: $HOMELAB_AI_SOURCE_DIR/$file${NC}" >&2
            return 1
        fi
    done

    mkdir -p "$HOMELAB_AI_RUNTIME_DIR/configs" || return 1
    for file in "${runtime_files[@]}"; do
        if [[ "$file" == "portfolio.json" ]]; then
            cp "$HOMELAB_AI_SOURCE_DIR/$file" "$HOMELAB_AI_RUNTIME_DIR/configs/portfolio.json" || return 1
        else
            cp "$HOMELAB_AI_SOURCE_DIR/$file" "$HOMELAB_AI_RUNTIME_DIR/$file" || return 1
        fi
    done
    chmod +x "$HOMELAB_AI_RUNTIME_DIR/run-daily.sh" "$HOMELAB_AI_RUNTIME_DIR/wake-pc.sh"
}

RUNTIME_MOUNT_ADDED=0

configure_runtime_mount() {
    local id="$1"
    local current_mount
    local expected_mount="${HOMELAB_AI_RUNTIME_DIR},mp=${HOMELAB_AI_DIR}"

    current_mount="$(pct config "$id" | sed -n 's/^mp0: //p')"
    if [[ "$current_mount" == "$expected_mount" ]]; then
        return 0
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
        RUNTIME_MOUNT_ADDED=1
        return 0
    fi

    return 1
}

# ==============================================================================
# INTERACTIVE STEP: Prompt for Variables
# ==============================================================================
echo -e "${CYAN}------------------------------------------------------------${NC}"
echo -e "${CYAN}[?] CONFIGURATION STEP${NC}"
echo -e "${CYAN}------------------------------------------------------------${NC}"
read -p "1. Enter Container ID (e.g., 200): " CT_ID
read -p "2. Enter Container IP with CIDR (e.g., 192.168.1.60/24): " CT_IP
echo -e "${CYAN}--- RTX Station Details ---${NC}"
read -p "3. Enter RTX Station IP Address (e.g., 192.168.1.10): " RTX_IP
read -p "4. Enter RTX Station MAC Address (e.g., AA:BB:CC:DD:EE:FF): " RTX_MAC
read -p "5. Enter RTX Station SSH User (e.g., rafal): " RTX_USER
echo -e ""

# ==============================================================================
# SETUP FUNCTION: Python Environment & Configuration
# ==============================================================================
setup_ai_orchestrator() {
    local id="$1"
    echo -e "${YELLOW}[->] Preparing Python environment for AI Orchestrator...${NC}"
    
    # 1. Update and install system dependencies
    pct exec "$id" -- apt-get update -y
    pct exec "$id" -- apt-get install -y python3 python3-pip python3-venv git cron openssh-client iputils-ping sqlite3 curl
    check_step_status "Install system dependencies"

    # 2. Create directory structure and virtual environment
    pct exec "$id" -- mkdir -p "$HOMELAB_AI_DIR/logs"
    pct exec "$id" -- python3 -m venv "$HOMELAB_AI_DIR/venv"
    
    # 3. Install Python packages inside venv
    echo -e "${YELLOW}[->] Installing Python packages (yfinance, requests, wakeonlan, python-dotenv)...${NC}"
    pct exec "$id" -- "$HOMELAB_AI_DIR/venv/bin/pip" install yfinance requests wakeonlan python-dotenv
    check_step_status "Python PIP packages installation"

    # 4. Generate SSH key for passwordless RTX shutdown
    echo -e "${YELLOW}[->] Generating SSH keys for passwordless RTX shutdown...${NC}"
    pct exec "$id" -- bash -c 'if [ ! -f /root/.ssh/id_ed25519 ]; then ssh-keygen -t ed25519 -f /root/.ssh/id_ed25519 -N ""; fi'
    
    PUB_KEY=$(pct exec "$id" -- cat /root/.ssh/id_ed25519.pub)

    # 5. Generate .env configuration file
    echo -e "${YELLOW}[->] Injecting .env configuration...${NC}"
    cat <<EOF > /tmp/orchestrator.env
# RTX Station Configuration
RTX_IP=${RTX_IP}
RTX_MAC=${RTX_MAC}
RTX_SSH_USER=${RTX_USER}

# Ollama Endpoint
OLLAMA_URL=http://${RTX_IP}:11434/api/generate
EOF
    pct push "$id" /tmp/orchestrator.env "$HOMELAB_AI_DIR/.env"
    rm -f /tmp/orchestrator.env
    
    # Ustawienie odpowiednich uprawnień
    pct exec "$id" -- chmod 600 "$HOMELAB_AI_DIR/.env"

    echo -e "${GREEN}[+] AI Orchestrator setup completed inside container.${NC}"
}

# ==============================================================================
# MAIN EXECUTION
# ==============================================================================

if ! stage_runtime_files; then
    echo -e "${RED}[X] Failed to stage runtime files.${NC}" >&2
    exit 1
fi

if ! container_exists "$CT_ID"; then
    echo -e "${GREEN}[+] Creating ai-finance container (${CT_ID})...${NC}"
    
    pct create "$CT_ID" "$LXC_TEMPLATE" \
        -cores 1 -memory 1024 -rootfs local-lvm:8 \
        -net0 name=eth0,bridge="$NET_BRIDGE",ip="$CT_IP",gw="$NET_GATEWAY" \
        -ostype debian -hostname "ai-finance" -onboot 1

    if check_step_status "Creating LXC $CT_ID"; then
        if configure_runtime_mount "$CT_ID" && pct start "$CT_ID"; then
            setup_ai_orchestrator "$CT_ID"
        else
            echo -e "${RED}[X] Failed to configure and start LXC $CT_ID.${NC}" >&2
            exit 1
        fi
    fi
else
    if configure_runtime_mount "$CT_ID"; then
        if ! pct status "$CT_ID" | grep -q 'status: running'; then
            pct start "$CT_ID" || exit 1
        fi
        if [ "$RUNTIME_MOUNT_ADDED" -eq 1 ]; then
            setup_ai_orchestrator "$CT_ID"
        fi
    else
        exit 1
    fi
fi

echo -e "${GREEN}[*] Script execution finished.${NC}"

# Display the SSH key for RTX authorization
if [ -n "$PUB_KEY" ]; then
    echo -e "${CYAN}============================================================${NC}"
    echo -e "${CYAN}[!] IMPORTANT: SSH KEY FOR RTX SHUTDOWN${NC}"
    echo -e "${CYAN}To allow Proxmox to shut down your RTX machine automatically,${NC}"
    echo -e "${CYAN}copy the following public key to your RTX machine's authorized_keys file:${NC}"
    echo -e "${CYAN}(Windows path: C:\\Users\\${RTX_USER}\\.ssh\\authorized_keys)${NC}"
    echo -e "${CYAN}(Linux path: /home/${RTX_USER}/.ssh/authorized_keys)${NC}"
    echo -e "${CYAN}------------------------------------------------------------${NC}"
    echo -e "${YELLOW}$PUB_KEY${NC}"
    echo -e "${CYAN}============================================================${NC}"
    
    # Zapisz klucz do pliku tekstowego na serwerze Proxmox w razie czego
    echo "$PUB_KEY" > "/root/ai_orchestrator_${CT_ID}_pubkey.txt"
    echo -e "${CYAN}[i] A backup of this key has been saved to /root/ai_orchestrator_${CT_ID}_pubkey.txt${NC}"
fi