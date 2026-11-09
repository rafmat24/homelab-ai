import os
import time
import json
import subprocess
import requests
from dotenv import load_dotenv
from datetime import datetime

# ==============================================================================
# CONFIGURATION & INITIALIZATION
# ==============================================================================
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_BASE_DIR = (
    os.path.dirname(SCRIPT_DIR)
    if os.path.basename(SCRIPT_DIR) in {"code", "runtime"}
    else SCRIPT_DIR
)
BASE_DIR = os.path.abspath(os.getenv("HOMELAB_AI_DIR", DEFAULT_BASE_DIR))
ENV_PATH = os.path.join(BASE_DIR, ".env")
DATA_FILE = os.path.join(BASE_DIR, "logs", "latest_market_data.json")
REPORT_FILE = os.path.join(BASE_DIR, "logs", f"report_{datetime.now().strftime('%Y%m%d')}.md")
WAKE_SCRIPT_PATH = os.path.join(SCRIPT_DIR, "wake-pc.sh")

load_dotenv(ENV_PATH)

RTX_IP = os.getenv("RTX_IP")
RTX_USER = os.getenv("RTX_SSH_USER")
OPENWRT_IP = os.getenv("OPENWRT_IP")
OLLAMA_URL = os.getenv("OLLAMA_URL")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.1")

# ==============================================================================
# HELPER FUNCTIONS
# ==============================================================================
def is_rtx_online(ip: str) -> bool:
    print(f"[*] Checking if RTX station ({ip}) is online...")
    response = subprocess.run(
        ["ping", "-c", "1", "-W", "2", ip],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )
    return response.returncode == 0

def wake_rtx_station() -> bool:
    """Wakes the RTX station using the external Bash script pointing to OpenWrt."""
    print(f"[*] Executing Wake-on-LAN script via OpenWrt...")
    if not os.path.exists(WAKE_SCRIPT_PATH):
        print(f"[!] Wake script not found at {WAKE_SCRIPT_PATH}")
        return False
        
    try:
        # Execute the Bash script
        result = subprocess.run(["bash", WAKE_SCRIPT_PATH], capture_output=True, text=True, check=True)
        print(f"[+] Output from WOL script:\n{result.stdout}")
        return True
    except subprocess.CalledProcessError as e:
        print(f"[!] WOL script failed. Output:\n{e.stdout}\nError:\n{e.stderr}")
        return False

def wait_for_ollama(ip: str, timeout: int = 300) -> bool:
    print(f"[*] Waiting for Ollama API to become available on {ip}...")
    start_time = time.time()
    url = f"http://{ip}:11434/api/tags"
    
    while (time.time() - start_time) < timeout:
        try:
            response = requests.get(url, timeout=3)
            if response.status_code == 200:
                print(f"[+] Ollama API is UP and responding!")
                return True
        except requests.exceptions.RequestException:
            pass
        
        print(f"[-] Still waiting... (Elapsed: {int(time.time() - start_time)}s)")
        time.sleep(5)
        
    return False

def shutdown_rtx_station(ip: str, user: str):
    print(f"[*] Sending shutdown command to {ip} via SSH...")
    try:
        subprocess.run([
            "ssh", "-o", "StrictHostKeyChecking=no", f"{user}@{ip}", "shutdown /h"
        ], check=True)
        print(f"[+] Shutdown command sent successfully.")
    except subprocess.CalledProcessError as e:
        print(f"[!] Failed to send shutdown command: {e}")

def send_email_notification(subject: str, message: str):
    print(f"[EMAIL MOCK] Subject: {subject}")
    print(f"[EMAIL MOCK] Body: {message}")

# ==============================================================================
# LLM INTERACTION
# ==============================================================================
def generate_ai_report(json_data: dict) -> str:
    print(f"[*] Generating AI report using model: {OLLAMA_MODEL}...")
    
    system_prompt = (
        "You are an expert financial analyst. Your task is to analyze the provided market data. "
        "The portfolio is divided into two categories: "
        "1. 'IKZE' (Retirement account, long-term ETF investments, low risk). "
        "2. 'FREE_TRADE' (Individual stocks, short/medium-term, higher risk). "
        "Review the technical indicators (RSI, SMA200 distance) and recent news. "
        "Write a concise report in POLISH. Provide clear conclusions and potential actions."
    )
    
    user_prompt = f"Here is the market data for {json_data.get('report_date')}:\n{json.dumps(json_data, indent=2)}"

    payload = {
        "model": OLLAMA_MODEL,
        "prompt": f"{system_prompt}\n\n{user_prompt}",
        "stream": False
    }

    try:
        response = requests.post(OLLAMA_URL, json=payload, timeout=120)
        response.raise_for_status()
        result = response.json()
        return result.get("response", "No response generated.")
    except Exception as e:
        print(f"[!] Error generating report from Ollama: {e}")
        return ""

# ==============================================================================
# MAIN ORCHESTRATION LOGIC
# ==============================================================================
def main():
    print("="*60)
    print(f"[*] Starting AI Orchestrator Agent at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("="*60)

    if not os.path.exists(DATA_FILE):
        print(f"[!] Market data file not found at {DATA_FILE}. Run market_data.py first.")
        return

    with open(DATA_FILE, 'r', encoding='utf-8') as f:
        market_data = json.load(f)

    if is_rtx_online(RTX_IP):
        print(f"[!] RTX Station is currently ONLINE (In use).")
        send_email_notification(
            "AI Agent: Authorization Required", 
            "The RTX station is online. Skipping autonomous execution to prevent resource conflicts."
        )
        print(f"[*] Exiting script gracefully.")
        return

    print(f"[+] RTX Station is OFFLINE. Initiating Wake-on-LAN sequence...")
    
    if not wake_rtx_station():
        send_email_notification("AI Agent Error", "Failed to execute WOL script.")
        return
    
    if not wait_for_ollama(RTX_IP, timeout=300):
        print(f"[!] Timeout: Ollama did not come online within 5 minutes. Aborting.")
        send_email_notification("AI Agent Error", "Failed to start Ollama.")
        return

    report_text = generate_ai_report(market_data)
    
    if report_text:
        with open(REPORT_FILE, 'w', encoding='utf-8') as f:
            f.write(report_text)
        print(f"[+] Report generated and saved to {REPORT_FILE}")
        send_email_notification(f"AI Market Report: {market_data.get('report_date')}", report_text)
    
    shutdown_rtx_station(RTX_IP, RTX_USER)
    
    print("[*] AI Orchestrator pipeline completed successfully.")

if __name__ == "__main__":
    main()