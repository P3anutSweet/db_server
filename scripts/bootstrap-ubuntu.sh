#!/usr/bin/env bash
# Run on the Ubuntu laptop. Never run this script on the Windows client.
set -euo pipefail

usage() {
    cat <<'EOF'
Usage: sudo bash scripts/bootstrap-ubuntu.sh --user USER --client-ip IPV4 [--desktop] [--enable-firewall]
Installs SSH, optionally Ubuntu GNOME, and adds source-specific UFW rules.
--enable-firewall is allowed only from a local console. Existing UFW rules are preserved.
EOF
}
die() { printf 'Error: %s\n' "$*" >&2; exit 1; }
login_user='' client_ip='' desktop=false enable_firewall=false
while (($#)); do
    case "$1" in
        --user) (($# >= 2)) || die 'Missing --user value'; login_user=$2; shift 2 ;;
        --client-ip) (($# >= 2)) || die 'Missing --client-ip value'; client_ip=$2; shift 2 ;;
        --desktop) desktop=true; shift ;;
        --enable-firewall) enable_firewall=true; shift ;;
        -h|--help) usage; exit 0 ;;
        *) die "Unknown argument: $1" ;;
    esac
done
[[ $login_user =~ ^[a-z_][a-z0-9_-]*[$]?$ && $login_user != root ]] || die 'Specify an existing non-root Ubuntu username'
[[ $client_ip =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] || die 'Specify the Windows LAN IPv4 address'
IFS=. read -r -a octets <<< "$client_ip"
for octet in "${octets[@]}"; do
    ((10#$octet <= 255)) || die 'Invalid IPv4 address'
done
[[ $EUID -eq 0 ]] || die 'Run with sudo on the Ubuntu laptop'
if $enable_firewall && [[ -n ${SSH_CONNECTION:-} || -n ${SSH_CLIENT:-} ]]; then
    die 'Enable the firewall at the laptop console to avoid remote lockout'
fi
# shellcheck source=/dev/null
source /etc/os-release
[[ ${ID:-} == ubuntu ]] || die 'This script supports Ubuntu only'
[[ ${VERSION_ID:-} == 24.04 || ${VERSION_ID:-} == 26.04 ]] || die 'Supported versions: Ubuntu 24.04 and 26.04 LTS'
account=$(getent passwd "$login_user") || die "Unknown user: $login_user"
IFS=: read -r _ _ uid _ _ user_home user_shell <<< "$account"
[[ $user_home == /* && -d $user_home && $uid -ge 1000 ]] || die 'Expected a regular user with an existing home directory'
[[ $user_shell != */nologin && $user_shell != */false ]] || die 'User does not have a login shell'
if $desktop; then
    if systemctl is-active --quiet xrdp.service; then
        die 'xrdp is active; resolve its port conflict before installing GNOME Remote Desktop'
    fi
fi

apt-get update
packages=(openssh-server ufw git ca-certificates)
if $desktop; then
    packages+=(ubuntu-desktop-minimal gnome-remote-desktop gdm3)
fi
apt-get install -y "${packages[@]}"
install -d -m 0755 /run/sshd
/usr/sbin/sshd -t
ssh_settings=$(/usr/sbin/sshd -T)
grep -qx 'port 22' <<< "$ssh_settings" || die 'Existing SSH config uses a custom port; review before proceeding'

# Apply rules before starting network services. This does not remove older rules.
ufw allow from "$client_ip" to any port 22 proto tcp comment 'db_server SSH client'
if $desktop; then
    ufw allow from "$client_ip" to any port 3389 proto tcp comment 'db_server RDP client'
fi
if $enable_firewall; then
    ufw default deny incoming
    ufw default allow outgoing
    ufw --force enable
fi
systemctl enable --now ssh.service

if $desktop; then
    systemctl set-default graphical.target
    printf '\nGNOME installed. Run sudo systemctl start gdm3, then sign in locally.\n'
    printf 'In Settings > System > Remote Desktop > Remote Login, unlock and enable Remote Login.\n'
    printf 'Create RDP credentials in Settings, note the certificate details, then sign out to test remote login.\n'
    printf 'RDP will not accept connections until this Settings step is complete.\n'
fi
systemctl is-active --quiet ssh.service
printf '\nUbuntu user: %s\nServer addresses: ' "$login_user"
hostname -I
printf '\nSSH ED25519 host fingerprint (compare on Windows):\n'
ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub
printf '\nFirewall status (review existing broad rules):\n'
ufw status verbose
printf '\nNext: connect from Windows. SSH authentication settings are unchanged; add and test your SSH key before disabling passwords.\n'
