# db_server

Database-server project for an Ubuntu laptop. First milestone: SSH terminal access and a GNOME remote desktop from Windows on the same LAN.

Supported installer targets: **Ubuntu Server 24.04 and 26.04 LTS, initially without a GUI**. The installer adds Ubuntu's minimal GNOME desktop and GNOME Remote Desktop. Confirm the laptop's actual release with `cat /etc/os-release` before installation. Database selection, installation and backups follow after remote access is verified.

## Get the repository onto Ubuntu

At the laptop, run `whoami` and `ip -4 -br addr` to find the username and LAN address. On Windows, run `ipconfig` to find the IPv4 address of the connected Ethernet/Wi-Fi adapter.

This repository is private. Download its ZIP from GitHub while signed in and transfer/extract it using a USB drive, or clone it if GitHub authentication is available:

```bash
sudo apt-get update
sudo apt-get install -y git ca-certificates
git clone https://github.com/P3anutSweet/db_server.git
cd db_server
```

GitHub HTTPS authentication requires a token instead of an account password. Enter it at Git's password prompt, never in the URL or a script. An existing GitHub SSH key also works. See [GitHub authentication](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/about-authentication-to-github).

## Install SSH and GNOME

Run **at the Ubuntu laptop console**, substituting your Windows IPv4 address:

```bash
sudo bash scripts/bootstrap-ubuntu.sh \
  --user "$(whoami)" \
  --client-ip YOUR_WINDOWS_IPV4 \
  --desktop --enable-firewall
```

The script installs OpenSSH, `ubuntu-desktop-minimal`, `gdm3`, and `gnome-remote-desktop`. It enables SSH on boot, selects graphical boot, and enables UFW with inbound traffic denied by default. TCP 22 (SSH) and 3389 (Remote Login) are allowed from the specified Windows address. Existing UFW rules remain; review the printed status for older broad rules. Re-running with another client IP adds another rule. Remove obsolete ones using `sudo ufw status numbered` and `sudo ufw delete RULE_NUMBER`.

GNOME needs several GB of free disk space and installation may take a while. The installer leaves SSH authentication settings unchanged and does not create or store RDP credentials. Keep the laptop console available until both connections work.

Start GNOME after the script finishes:

```bash
sudo systemctl start gdm3
```

Sign in locally. Open **Settings → System → Remote Desktop → Remote Login**, unlock the panel, enable Remote Login, and create the RDP credentials shown there. Note the host, port and certificate details. Then sign out locally before testing remote login.

Remote Login starts a remote GNOME session while the local user is logged out. Desktop Sharing controls an existing local desktop. Start with **Remote Login only**, on TCP 3389. If both modes are enabled, Desktop Sharing normally moves to 3390; the installer only opens 3389. See [Ubuntu's remote desktop guide](https://ubuntu.com/desktop/docs/en/latest/how-to/share-your-desktop-remotely/).

## Connect from Windows

In PowerShell, replace the placeholders:

```powershell
ssh YOUR_UBUNTU_USERNAME@UBUNTU_LAN_IP
mstsc /v:UBUNTU_LAN_IP:3389
```

Compare SSH's first-connection host fingerprint with the fingerprint printed at the laptop. For RDP, verify the presented certificate against the laptop's Remote Login certificate details before trusting it. Enter the RDP credentials from Settings. If GNOME then presents a login screen, use your regular Ubuntu account there. Sign out through GNOME when finished; closing the RDP window may leave the session running.

Optional repository helper:

```powershell
.\scripts\connect.ps1 -Server UBUNTU_LAN_IP -User YOUR_UBUNTU_USERNAME
.\scripts\connect.ps1 -Server UBUNTU_LAN_IP -Mode Desktop
.\scripts\connect.ps1 -Server UBUNTU_LAN_IP -Mode Check
```

## Add an SSH key

On Windows, generate a dedicated key and choose a passphrase. If this filename already exists, reuse it or choose another name instead of overwriting it:

```powershell
New-Item -ItemType Directory -Force "$HOME\.ssh" | Out-Null
ssh-keygen -t ed25519 -f "$HOME\.ssh\db_server_ed25519"
Get-Content "$HOME\.ssh\db_server_ed25519.pub" | ssh YOUR_UBUNTU_USERNAME@UBUNTU_LAN_IP 'umask 077; mkdir -p ~/.ssh; cat >> ~/.ssh/authorized_keys; chmod 700 ~/.ssh; chmod 600 ~/.ssh/authorized_keys'
ssh -o PasswordAuthentication=no -o KbdInteractiveAuthentication=no -i "$HOME\.ssh\db_server_ed25519" YOUR_UBUNTU_USERNAME@UBUNTU_LAN_IP
```

After that final command succeeds, add an entry to Windows `~/.ssh/config`:

```sshconfig
Host db-server
    HostName UBUNTU_LAN_IP
    User YOUR_UBUNTU_USERNAME
    IdentityFile ~/.ssh/db_server_ed25519
    IdentitiesOnly yes
    ServerAliveInterval 30
    ServerAliveCountMax 3
```

Then connect using `ssh db-server`. Keep SSH passwords enabled until key login is verified. For later hardening, use `/etc/ssh/sshd_config.d/`, validate with `sudo sshd -t`, inspect `sudo sshd -T`, and test a new connection while retaining the current session. Disabling SSH password authentication does not disable GNOME RDP authentication.

## Availability and troubleshooting

Keep the laptop on AC power and initially leave its lid open. Confirm it stays awake before relying on unattended access. Sleep, network loss, and a disk-encryption unlock prompt after reboot can interrupt access. Reserve both devices' addresses in the router's DHCP settings, or update connection details and firewall rules after address changes.

On Windows, check `Test-NetConnection UBUNTU_LAN_IP -Port 22` and `Test-NetConnection UBUNTU_LAN_IP -Port 3389`. If both time out, verify IP addresses, UFW and Wi-Fi guest/client isolation.

At the laptop:

```bash
systemctl status ssh gdm3 gnome-remote-desktop --no-pager
sudo ss -ltnp | grep -E ':(22|3389)\b'
sudo ufw status verbose
sudo journalctl -u ssh -u gdm3 -u gnome-remote-desktop -n 80 --no-pager
sudo grdctl --system status
```

For RDP failures, verify Remote Login is enabled, sign out of local GNOME, and inspect the journal. Install available Ubuntu updates before troubleshooting protocol issues. Avoid running xrdp on the same port.

### Ubuntu 26.04: credentials are not set

If the service log says `[RDP] Credentials are not set, denying client`, the server is rejecting connections because its system-level RDP credentials are missing. An enabled service and an open port do not prove that authentication is configured.

Run this command **by itself** at the Ubuntu console, then answer its username and password prompts:

```bash
sudo grdctl --system rdp set-credentials
```

Do not paste subsequent commands until the prompts finish. Do not put the password in command arguments, shell history, screenshots, or Git. Choose a dedicated RDP password; the username can be the same as the Ubuntu username.

After the command finishes, run:

```bash
sudo systemctl restart gnome-remote-desktop.service
sudo grdctl --system status
```

Reconnect from Windows using the RDP credentials just saved, without a `MicrosoftAccount\\` prefix. If GDM then asks you to log in, use your regular Ubuntu account. The TPM warning saying `using GKeyFile as fallback` reports the alternative credential backend; the missing-credentials message is the relevant rejection in this case. See [Canonical's Ubuntu 26.04 RDP configuration instructions](https://ubuntu.com/aws/docs/aws-how-to/instances/launch-ubuntu-desktop/).

For later access outside the LAN, use a private VPN such as Tailscale. Do not forward SSH, RDP or database ports through the router. Keep passwords, tokens, private keys, live data and backups out of Git.

## Validation

GitHub Actions checks Bash syntax, ShellCheck, invalid argument rejection, and PowerShell parsing. It does not install GNOME or prove the physical laptop accepts connections. Verify SSH login, RDP login and availability on the real machines before marking the milestone complete.

## References

- [Ubuntu OpenSSH server](https://ubuntu.com/server/docs/how-to/security/openssh-server/)
- [Ubuntu GNOME Remote Desktop](https://ubuntu.com/desktop/docs/en/latest/how-to/share-your-desktop-remotely/)
- [Ubuntu 26.04 minimal desktop package](https://packages.ubuntu.com/resolute/ubuntu-desktop-minimal)

