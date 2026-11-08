#!/bin/bash

# ==============================================================================
# AI ORCHESTRATOR - LXC DEPLOYMENT SCRIPT
# ==============================================================================

# Global Configuration
LXC_TEMPLATE="local:vztmpl/debian-13-standard_13.1-2_amd64.tar.zst"
NET_BRIDGE="vmbr0"
NET_GATEWAY="192.168.1.1"

# Log Colors
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

echo -e "${GREEN}[*] Starting AI Orchestrator deployment...${NC}"

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
        echo -e "${YELLOW}[-] Container $1 already exists. Skipping creation.${NC}"
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
    pct exec "$id" -- mkdir -p /opt/ai-finance/logs
    pct exec "$id" -- python3 -m venv /opt/ai-finance/venv
    
    # 3. Install Python packages inside venv
    echo -e "${YELLOW}[->] Installing Python packages (yfinance, requests, wakeonlan, python-dotenv)...${NC}"
    pct exec "$id" -- /opt/ai-finance/venv/bin/pip install yfinance requests wakeonlan python-dotenv
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
    pct push "$id" /tmp/orchestrator.env /opt/ai-finance/.env
    rm -f /tmp/orchestrator.env
    
    # Ustawienie odpowiednich uprawnień
    pct exec "$id" -- chmod 600 /opt/ai-finance/.env

    echo -e "${GREEN}[+] AI Orchestrator setup completed inside container.${NC}"
}

# ==============================================================================
# MAIN EXECUTION
# ==============================================================================

if ! container_exists "$CT_ID"; then
    echo -e "${GREEN}[+] Creating ai-finance container (${CT_ID})...${NC}"
    
    pct create "$CT_ID" "$LXC_TEMPLATE" \
        -cores 1 -memory 1024 -rootfs local-lvm:8 \
        -net0 name=eth0,bridge="$NET_BRIDGE",ip="$CT_IP",gw="$NET_GATEWAY" \
        -ostype debian -hostname "ai-finance" -onboot 1 -start 1

    if check_step_status "Creating LXC $CT_ID"; then
        setup_ai_orchestrator "$CT_ID"
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