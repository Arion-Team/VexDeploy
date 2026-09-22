#!/usr/bin/env bash
# VexDeploy — auto-setup LXD/Incus for the bot
set -euo pipefail

NETWORK="${LXD_NETWORK:-vexdeploy}"
ENV_FILE="${ENV_FILE:-.env}"

log()  { printf '\n==> %s\n' "$*"; }
ok()   { printf '    OK: %s\n' "$*"; }
warn() { printf '    WARN: %s\n' "$*"; }
die()  { printf '    ERROR: %s\n' "$*" >&2; exit 1; }

detect_cli() {
  if [[ -n "${LXD_CLI:-}" ]]; then
    echo "$LXD_CLI"
    return
  fi
  if command -v incus >/dev/null 2>&1; then
    echo "incus"
  elif command -v lxc >/dev/null 2>&1; then
    echo "lxc"
  elif [[ -x /snap/bin/lxc ]]; then
    echo "/snap/bin/lxc"
  elif [[ -x /usr/bin/incus ]]; then
    echo "/usr/bin/incus"
  else
    echo ""
  fi
}

ensure_root() {
  if [[ "${EUID}" -ne 0 ]]; then
    exec sudo -E bash "$0" "$@"
  fi
}

install_backend() {
  if [[ -n "$(detect_cli)" ]]; then
    ok "CLI already present: $(detect_cli)"
    return
  fi

  log "No lxc/incus found — installing Incus via apt"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq || true

  if apt-cache show incus >/dev/null 2>&1; then
    apt-get install -y -qq incus
  else
    log "Adding Incus apt repository"
    . /etc/os-release
    install -d -m 0755 /etc/apt/keyrings
    curl -fsSL https://pkgs.zabbly.com/incus/stable/key \
      | gpg --dearmor -o /etc/apt/keyrings/incus.gpg
    echo "deb [signed-by=/etc/apt/keyrings/incus.gpg] https://pkgs.zabbly.com/incus/stable ${UBUNTU_CODENAME:-$(. /etc/os-release && echo "$VERSION_CODENAME")} main" \
      > /etc/apt/sources.list.d/incus.list
    apt-get update -qq
    apt-get install -y -qq incus
  fi

  if ! command -v incus >/dev/null 2>&1; then
    log "apt Incus failed — trying snap LXD"
    apt-get install -y -qq snapd apparmor || true
    systemctl enable --now snapd snapd.apparmor snapd.socket 2>/dev/null || true
    sleep 2
    snap install lxd
  fi

  [[ -n "$(detect_cli)" ]] || die "Install failed — set LXD_CLI manually"
  ok "Installed: $(detect_cli)"
}

init_daemon() {
  local cli
  cli="$(detect_cli)"
  log "Initializing $cli (if needed)"

  if ! "$cli" version >/dev/null 2>&1; then
    if command -v systemctl >/dev/null 2>&1; then
      systemctl enable --now "snapd" 2>/dev/null || true
      systemctl enable --now "incus" "incus.socket" 2>/dev/null || true
      systemctl start "incus" 2>/dev/null || true
      systemctl start "incus.socket" 2>/dev/null || true
      sleep 2
    fi
  fi

  if ! "$cli" version >/dev/null 2>&1; then
    # first-use may auto-start for snap lxc; incus needs daemon
    if [[ "$cli" == *incus* ]]; then
      incus admin init --auto >/dev/null 2>&1 || true
    elif [[ "$cli" == *lxc* ]]; then
      lxd init --auto >/dev/null 2>&1 || true
    fi
    sleep 2
  fi

  if ! "$cli" version >/dev/null 2>&1; then
    if command -v incus >/dev/null 2>&1; then
      incus admin init --auto >/dev/null 2>&1 || true
    fi
    if command -v lxd >/dev/null 2>&1; then
      lxd init --auto >/dev/null 2>&1 || true
    fi
    if command -v snap >/dev/null 2>&1; then
      snap start lxd 2>/dev/null || true
    fi
    sleep 3
  fi

  "$cli" version >/dev/null 2>&1 || die "$cli daemon not responding (try: snap start lxd / systemctl start incus)"
  ok "$cli is up ($("$cli" version | head -n1))"
}

ensure_group() {
  local cli group_user
  cli="$(detect_cli)"
  group_user="${SUDO_USER:-${USER:-root}}"

  local grp="lxd"
  if [[ "$cli" == *incus* ]]; then
    grp="incus"
  fi

  if getent group "$grp" >/dev/null 2>&1; then
    if ! id -nG "$group_user" 2>/dev/null | tr ' ' '\n' | grep -qx "$grp"; then
      usermod -aG "$grp" "$group_user"
      warn "Added $group_user to group $grp (re-login may be needed)"
    else
      ok "$group_user already in $grp"
    fi
  fi
}

ensure_remote() {
  local cli name url proto
  cli="$(detect_cli)"
  name="$1"
  url="$2"
  proto="$3"

  if "$cli" remote get-url "$name" >/dev/null 2>&1; then
    ok "remote $name exists ($("$cli" remote get-url "$name"))"
    return
  fi

  log "Adding remote $name → $url"
  if "$cli" remote add "$name" "$url" --protocol "$proto" --force-remote >/dev/null 2>&1 \
    || "$cli" remote add "$name" "$url" --protocol "$proto" >/dev/null 2>&1; then
    ok "remote $name added"
  else
    warn "Could not add remote $name — trying image pull fallback later"
  fi
}

ensure_remotes() {
  local cli
  cli="$(detect_cli)"

  # images (debian/alpine in IMAGE_MAP)
  ensure_remote "images" "https://images.linuxcontainers.org" "simplestreams"

  # ubuntu cloud images (ubuntu:22.04 / ubuntu:24.04 in IMAGE_MAP)
  if "$cli" remote get-url "ubuntu" >/dev/null 2>&1; then
    ok "remote ubuntu exists"
  else
    ensure_remote "ubuntu" "https://cloud-images.ubuntu.com/releases" "simplestreams" || true
    if ! "$cli" remote get-url "ubuntu" >/dev/null 2>&1; then
      ensure_remote "ubuntu" "https://cloud-images.ubuntu.com/daily" "simplestreams" || true
    fi
    # some builds use ubuntu-daily name
    if ! "$cli" remote get-url "ubuntu" >/dev/null 2>&1; then
      warn "ubuntu remote missing — bot will fall back to images:ubuntu/* if you switch DEFAULT_OS_IMAGE"
    fi
  fi
}

ensure_network() {
  local cli
  cli="$(detect_cli)"

  if "$cli" network show "$NETWORK" >/dev/null 2>&1; then
    ok "network $NETWORK exists"
    return
  fi

  log "Creating managed bridge network: $NETWORK"
  if "$cli" network create "$NETWORK" ipv4.address=auto ipv4.nat=true ipv6.address=none >/dev/null 2>&1; then
    ok "network $NETWORK created"
  else
    # maybe created without ipv6 flag on older builds
    if "$cli" network create "$NETWORK" ipv4.address=auto ipv4.nat=true >/dev/null 2>&1; then
      ok "network $NETWORK created"
    else
      warn "network create failed (may already exist): $("$cli" network show "$NETWORK" 2>&1 | head -n3)"
    fi
  fi
}

warm_images() {
  local cli
  cli="$(detect_cli)"
  log "Prefetching default image ubuntu:22.04 (best-effort)"

  if "$cli" remote get-url "ubuntu" >/dev/null 2>&1; then
    "$cli" image copy "ubuntu:22.04" "local:" --auto-update=false >/dev/null 2>&1 \
      || "$cli" image list "ubuntu:22.04" >/dev/null 2>&1 \
      || true
  fi

  # smoke: resolve image listing
  if "$cli" image list "ubuntu:22.04" >/dev/null 2>&1 || "$cli" image list "images:ubuntu/22.04" >/dev/null 2>&1; then
    ok "ubuntu image resolvable"
  else
    warn "Could not list ubuntu:22.04 — check network / remotes after install"
  fi
}

update_env() {
  local cli env_path
  cli="$(detect_cli)"
  env_path="$ENV_FILE"

  [[ -f "$env_path" ]] || touch "$env_path"

  if grep -q '^LXD_CLI=' "$env_path"; then
    sed -i "s|^LXD_CLI=.*|LXD_CLI=$cli|" "$env_path"
  else
    echo "LXD_CLI=$cli" >> "$env_path"
  fi

  if grep -q '^LXD_NETWORK=' "$env_path"; then
    sed -i "s|^LXD_NETWORK=.*|LXD_NETWORK=$NETWORK|" "$env_path"
  else
    echo "LXD_NETWORK=$NETWORK" >> "$env_path"
  fi

  ok "Updated $env_path (LXD_CLI=$cli, LXD_NETWORK=$NETWORK)"
}

verify() {
  local cli
  cli="$(detect_cli)"
  log "Verification"
  "$cli" version >/dev/null || die "CLI failed"
  ok "CLI: $cli"

  "$cli" remote list >/dev/null 2>&1 || true
  echo "    Remotes:"
  "$cli" remote list 2>/dev/null | sed 's/^/      /' || true

  if "$cli" network show "$NETWORK" >/dev/null 2>&1; then
    ok "network $NETWORK"
  else
    warn "network $NETWORK missing"
  fi

  echo ""
  echo "Done. Next:"
  echo "  cd $(pwd)"
  echo "  git pull"
  echo "  # ensure .env has LXD_CLI=$cli"
  echo "  systemctl restart vexdeploy   # or: python bot.py"
  echo "  In Discord: /admin_stats  → LXD should be online"
}

main() {
  ensure_root "$@"
  cd "$(dirname "$0")"

  log "VexDeploy LXD/Incus setup"
  install_backend
  init_daemon
  ensure_group
  ensure_remotes
  ensure_network
  warm_images
  update_env
  verify
}

main "$@"
