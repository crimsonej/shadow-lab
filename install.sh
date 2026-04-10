#!/usr/bin/env bash
# =============================================================================
#  install.sh — Ollama API Provider: Server Agent Installer
#  Supports: Ubuntu 20.04+, Debian 11+, Arch Linux
#  Usage:
#    curl -fsSL https://raw.githubusercontent.com/you/ollama-api-provider/main/install.sh | bash
#    OR: bash install.sh
# =============================================================================

set -euo pipefail

# ── Config ────────────────────────────────────────────────────────────────────
AGENT_PORT="${AGENT_PORT:-8080}"
AGENT_DIR="/opt/ollama-agent"
KEYS_DIR="/etc/ollama-agent"
SERVICE_NAME="ollama-agent"
PYTHON_MIN="3.9"
REPO_URL="${REPO_URL:-}"  # optional: pull code from git

# ── Colors ────────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

info()    { echo -e "${CYAN}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

banner() {
  echo -e "${BOLD}"
  echo "  ╔══════════════════════════════════════════════╗"
  echo "  ║       Ollama API Provider — Installer        ║"
  echo "  ║   Turns your Linux server into an AI API     ║"
  echo "  ╚══════════════════════════════════════════════╝"
  echo -e "${NC}"
}

# ── OS Detection ──────────────────────────────────────────────────────────────
detect_os() {
  if [ -f /etc/os-release ]; then
    . /etc/os-release
    OS=$ID
    OS_LIKE="${ID_LIKE:-}"
  elif [ -f /etc/arch-release ]; then
    OS="arch"
  else
    error "Cannot detect OS. Supported: Ubuntu, Debian, Arch."
  fi
  info "Detected OS: $OS"
}

# ── Package management ────────────────────────────────────────────────────────
pkg_install() {
  case "$OS" in
    ubuntu|debian|linuxmint|pop)
      DEBIAN_FRONTEND=noninteractive apt-get install -yq "$@" ;;
    arch|manjaro|endeavouros)
      pacman -Syu --noconfirm "$@" ;;
    *)
      if echo "$OS_LIKE" | grep -q "debian"; then
        DEBIAN_FRONTEND=noninteractive apt-get install -yq "$@"
      else
        error "Unsupported distro: $OS. Install packages manually: $*"
      fi ;;
  esac
}

pkg_update() {
  case "$OS" in
    ubuntu|debian|linuxmint|pop) apt-get update -yq ;;
    arch|manjaro|endeavouros)    pacman -Sy ;;
    *)
      if echo "$OS_LIKE" | grep -q "debian"; then apt-get update -yq; fi ;;
  esac
}

# ── Check root ────────────────────────────────────────────────────────────────
check_root() {
  if [ "$EUID" -ne 0 ]; then
    error "Please run as root (sudo bash install.sh)"
  fi
}

# ── Python ────────────────────────────────────────────────────────────────────
install_python() {
  info "Checking Python 3..."
  if command -v python3 &>/dev/null; then
    VER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
    info "Found Python $VER"
  else
    info "Installing Python 3..."
    case "$OS" in
      ubuntu|debian|linuxmint|pop) pkg_install python3 python3-pip python3-venv ;;
      arch|manjaro|endeavouros)    pkg_install python python-pip ;;
      *) pkg_install python3 python3-pip ;;
    esac
  fi

  # Ensure pip & venv
  if ! python3 -m pip --version &>/dev/null; then
    case "$OS" in
      ubuntu|debian|linuxmint|pop) pkg_install python3-pip python3-venv ;;
      *) warn "pip not found — install manually if needed" ;;
    esac
  fi
}

# ── Ollama ────────────────────────────────────────────────────────────────────
install_ollama() {
  if command -v ollama &>/dev/null; then
    success "Ollama already installed ($(ollama --version 2>/dev/null || echo 'version unknown'))"
    return
  fi
  info "Installing Ollama..."
  curl -fsSL https://ollama.com/install.sh | sh
  success "Ollama installed"
}

start_ollama() {
  info "Starting Ollama service..."
  if systemctl is-active --quiet ollama 2>/dev/null; then
    success "Ollama already running"
    return
  fi
  # Ollama installer may have set up a service; try starting it
  systemctl enable --now ollama 2>/dev/null || true
  # Wait for Ollama to be ready
  for i in $(seq 1 15); do
    if curl -sf http://127.0.0.1:11434/ &>/dev/null; then
      success "Ollama is running"
      return
    fi
    sleep 2
  done
  warn "Ollama may not be running yet. Check with: systemctl status ollama"
}

# ── Agent files ───────────────────────────────────────────────────────────────
install_agent_files() {
  info "Setting up agent directory at $AGENT_DIR ..."
  mkdir -p "$AGENT_DIR" "$KEYS_DIR"

  # If running from a cloned repo, copy files; otherwise download
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  if [ -f "$SCRIPT_DIR/server-agent/main.py" ]; then
    info "Copying agent files from $SCRIPT_DIR/server-agent ..."
    cp -r "$SCRIPT_DIR"/server-agent/* "$AGENT_DIR/"
  elif [ -n "$REPO_URL" ]; then
    info "Cloning from $REPO_URL ..."
    pkg_install git
    git clone "$REPO_URL" /tmp/ollama-api-provider-install
    cp -r /tmp/ollama-api-provider-install/server-agent/* "$AGENT_DIR/"
    rm -rf /tmp/ollama-api-provider-install
  else
    error "Cannot find agent files. Run from the project root or set REPO_URL."
  fi

  # Set up virtual environment
  info "Creating Python virtual environment..."
  python3 -m venv "$AGENT_DIR/venv"
  "$AGENT_DIR/venv/bin/pip" install --upgrade pip -q
  "$AGENT_DIR/venv/bin/pip" install -r "$AGENT_DIR/requirements.txt" -q
  success "Python dependencies installed"
}

# ── Admin token ───────────────────────────────────────────────────────────────
generate_admin_token() {
  ENV_FILE="$AGENT_DIR/.env"
  if [ -f "$ENV_FILE" ] && grep -q "ADMIN_TOKEN=" "$ENV_FILE" && [ -n "$(grep ADMIN_TOKEN= "$ENV_FILE" | cut -d= -f2)" ]; then
    info "Admin token already set in .env"
    return
  fi
  ADMIN_TOKEN=$(python3 -c "import secrets; print(secrets.token_hex(32))")
  cat > "$ENV_FILE" <<EOF
OLLAMA_BASE_URL=http://127.0.0.1:11434
AGENT_PORT=${AGENT_PORT}
AGENT_HOST=0.0.0.0
ADMIN_TOKEN=${ADMIN_TOKEN}
KEYS_FILE=${KEYS_DIR}/keys.json
MAX_CONCURRENT=4
GPU_MONITORING=true
EOF
  chmod 600 "$ENV_FILE"
  success "Admin token generated and saved to $ENV_FILE"
}

# ── Systemd service ───────────────────────────────────────────────────────────
install_systemd_service() {
  info "Installing systemd service: $SERVICE_NAME ..."
  cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<EOF
[Unit]
Description=Ollama API Provider Agent
After=network.target ollama.service
Wants=ollama.service

[Service]
Type=simple
User=root
WorkingDirectory=${AGENT_DIR}
EnvironmentFile=${AGENT_DIR}/.env
ExecStart=${AGENT_DIR}/venv/bin/python main.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

  systemctl daemon-reload
  systemctl enable "$SERVICE_NAME"
  systemctl restart "$SERVICE_NAME"
  sleep 2

  if systemctl is-active --quiet "$SERVICE_NAME"; then
    success "Agent service is running"
  else
    warn "Agent service may have failed. Check with: journalctl -u $SERVICE_NAME -n 40"
  fi
}

# ── Firewall ──────────────────────────────────────────────────────────────────
configure_firewall() {
  info "Configuring firewall for port $AGENT_PORT ..."
  if command -v ufw &>/dev/null; then
    ufw allow "$AGENT_PORT/tcp" &>/dev/null && success "UFW: port $AGENT_PORT allowed"
  elif command -v firewall-cmd &>/dev/null; then
    firewall-cmd --permanent --add-port="$AGENT_PORT/tcp" &>/dev/null && firewall-cmd --reload &>/dev/null
    success "firewalld: port $AGENT_PORT allowed"
  else
    warn "No firewall manager found. Manually ensure port $AGENT_PORT is open."
  fi
}

# ── Summary ───────────────────────────────────────────────────────────────────
print_summary() {
  ADMIN_TOKEN=$(grep ADMIN_TOKEN= "$AGENT_DIR/.env" | cut -d= -f2)
  PUBLIC_IP=$(curl -sf https://api.ipify.org 2>/dev/null || hostname -I | awk '{print $1}')

  echo ""
  echo -e "${BOLD}${GREEN}═══════════════════════════════════════════════════${NC}"
  echo -e "${BOLD}  ✅  Ollama API Provider Agent Installed!${NC}"
  echo -e "${GREEN}═══════════════════════════════════════════════════${NC}"
  echo ""
  echo -e "  ${BOLD}Agent URL:${NC}    http://${PUBLIC_IP}:${AGENT_PORT}"
  echo -e "  ${BOLD}Admin Token:${NC}  ${ADMIN_TOKEN}"
  echo -e "  ${BOLD}Health:${NC}       http://${PUBLIC_IP}:${AGENT_PORT}/v1/health"
  echo -e "  ${BOLD}API Docs:${NC}     http://${PUBLIC_IP}:${AGENT_PORT}/docs"
  echo ""
  echo -e "  ${CYAN}Next steps:${NC}"
  echo -e "  1. Pull a model:  ollama pull llama3:8b"
  echo -e "  2. Open the dashboard on your local machine"
  echo -e "  3. Add this server with the URL and token above"
  echo ""
  echo -e "  ${YELLOW}Service commands:${NC}"
  echo -e "  systemctl status $SERVICE_NAME"
  echo -e "  journalctl -u $SERVICE_NAME -f"
  echo -e "  systemctl restart $SERVICE_NAME"
  echo ""
}

# ── Main ──────────────────────────────────────────────────────────────────────
main() {
  banner
  check_root
  detect_os
  pkg_update
  install_python
  install_ollama
  start_ollama
  install_agent_files
  generate_admin_token
  install_systemd_service
  configure_firewall
  print_summary
}

main "$@"
