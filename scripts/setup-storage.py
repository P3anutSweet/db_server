#!/usr/bin/env python3
"""Provision the two identified disks on thunderobotdb; default is read-only."""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys

GIB = 1024 ** 3
DB_BYTES = 200 * GIB
VG = "ubuntu-vg"
LV = "database-data"
PV = "/dev/nvme0n1p3"
PV_UUID = "7ZcsLq-CPld-9F2h-U5xd-3hXA-sGf1-0mi5aU"
DB_DEVICE = f"/dev/{VG}/{LV}"
BACKUP_DISK = "/dev/sda"
BACKUP_DEVICE = "/dev/sda1"
BACKUP_SERIAL = "WD-WX92AC2FMJ29"
BACKUP_BYTES = 1000204886016
OLD_BACKUP_UUID = "1571c6eb-4711-4324-977c-e445943356b9"
CONFIG = Path("/etc/db-server/storage.json")
BEGIN = "# BEGIN db_server storage"
END = "# END db_server storage"


def run(*args, change=False, **kwargs):
    if change:
        print("+", " ".join(map(str, args)), flush=True)
    result = subprocess.run(args, text=True, check=True, capture_output=True, **kwargs)
    return result.stdout.strip()


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def flatten(nodes):
    for node in nodes:
        yield node
        yield from flatten(node.get("children", []))


def inventory():
    return json.loads(run("lsblk", "--json", "-b", "-e", "7", "-o",
                          "NAME,PATH,TYPE,SIZE,FSTYPE,LABEL,UUID,SERIAL,MOUNTPOINTS"))


def lvm(command, section, columns, *args):
    data = json.loads(run(command, "--reportformat", "json", "--units", "b",
                          "--nosuffix", "-o", columns, *args))
    return data["report"][0][section]


def mount_uuid(path):
    result = subprocess.run(["findmnt", "-rn", "--mountpoint", str(path), "-o", "UUID"],
                            text=True, capture_output=True)
    return result.stdout.strip() if result.returncode == 0 else None


def check_mount_target(path, expected_uuid):
    path = Path(path)
    require(not path.is_symlink(), f"Refusing symlink mountpoint: {path}")
    actual = mount_uuid(path)
    if actual is not None:
        require(bool(expected_uuid) and actual == expected_uuid,
                f"Unexpected filesystem mounted on {path}")
    elif path.exists():
        require(path.is_dir() and not any(path.iterdir()),
                f"Mountpoint must be empty: {path}")


def render_fstab(original, db_uuid, backup_uuid):
    """Only replace our marked block; reject conflicting unmanaged entries."""
    require(original.count(BEGIN) == original.count(END) <= 1, "Invalid fstab markers")
    if BEGIN in original:
        start, rest = original.split(BEGIN, 1)
        require(END in rest, "Invalid fstab marker order")
        _, tail = rest.split(END, 1)
        original = start + tail.lstrip("\n")
    for line in original.splitlines():
        fields = line.split()
        if fields and not fields[0].startswith("#") and len(fields) > 1:
            require(fields[1] not in ("/srv/db", "/srv/backups"),
                    "An unmanaged fstab entry already uses a target mountpoint")
    return (original.rstrip() + "\n\n" + BEGIN + "\n"
            f"UUID={db_uuid} /srv/db ext4 defaults 0 2\n"
            f"UUID={backup_uuid} /srv/backups ext4 defaults,nofail,x-systemd.device-timeout=10s 0 2\n"
            + END + "\n")


def atomic_write(path, content, mode=0o600):
    path = Path(path)
    require(not path.is_symlink(), f"Refusing symlink: {path}")
    temporary = path.with_name(path.name + ".db-server-tmp")
    require(not temporary.exists() and not temporary.is_symlink(),
            f"Temporary path already exists: {temporary}")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def preflight():
    require(socket.gethostname() == "thunderobotdb", "This procedure is only for thunderobotdb")
    require(os.geteuid() == 0, "Run with sudo; preflight needs read access to LVM metadata")
    for command in ("lsblk", "pvs", "vgs", "lvs", "lvcreate", "vgcfgbackup", "mkfs.ext4",
                    "parted", "partprobe", "wipefs", "findmnt", "mount", "systemctl", "udevadm", "blkid"):
        require(shutil.which(command), f"Missing command: {command}")
    require(not CONFIG.is_symlink(), "Unexpected config symlink")
    config = json.loads(CONFIG.read_text()) if CONFIG.exists() else {}
    disks = inventory()
    nodes = list(flatten(disks["blockdevices"]))
    by_path = {n["path"]: n for n in nodes}
    disk = by_path.get(BACKUP_DISK, {})
    require(disk.get("type") == "disk" and disk.get("serial") == BACKUP_SERIAL
            and int(disk.get("size", 0)) == BACKUP_BYTES, "Backup disk identity mismatch")
    for node in flatten([disk]):
        mounts = [m for m in node.get("mountpoints", []) if m]
        require(not mounts or (config.get("backup_uuid") == node.get("uuid")
                               and mounts == ["/srv/backups"]),
                f"Backup disk is mounted elsewhere: {node['path']}")
        require(node.get("type") in ("disk", "part"), "Backup disk has active mappings/holders")
    backup = by_path.get(BACKUP_DEVICE, {})
    backup_ready = bool(config.get("backup_uuid")) and (
        backup.get("uuid") == config["backup_uuid"] and backup.get("fstype") == "ext4"
        and backup.get("label") == "db_backup")
    if config.get("backup_uuid"):
        require(backup_ready, "Previously configured backup filesystem changed; inspect manually")
    else:
        require(len(disk.get("children", [])) == 1
                and backup.get("uuid") == OLD_BACKUP_UUID
                and backup.get("fstype") == "BitLocker", "Backup partition changed; inspect manually")
    pvs = lvm("pvs", "pv", "pv_name,pv_uuid,vg_name", "--select", f"vg_name={VG}")
    require(len(pvs) == 1 and pvs[0]["pv_name"] == PV and pvs[0]["pv_uuid"] == PV_UUID,
            "Database volume group physical disk identity mismatch")
    vgs = lvm("vgs", "vg", "vg_name,vg_size,vg_free", VG)
    lvs = lvm("lvs", "lv", "lv_name,lv_size,lv_tags,lv_path", VG)
    existing = next((lv for lv in lvs if lv["lv_name"] == LV), None)
    if existing:
        require(int(float(existing["lv_size"])) == DB_BYTES
                and "db_server_data" in existing["lv_tags"].split(","),
                "Existing database LV is not the expected managed 200GiB volume")
        db = next((n for n in nodes if os.path.realpath(n["path"]) == os.path.realpath(DB_DEVICE)), {})
        require(db.get("fstype") == "ext4" and db.get("label") == "db_data",
                "Existing database LV has unexpected filesystem; it will not be formatted")
        require(not config.get("db_uuid") or config["db_uuid"] == db.get("uuid"),
                "Previously configured database UUID changed")
    else:
        require(not config.get("db_uuid"), "Previously configured database LV disappeared")
        require(int(float(vgs[0]["vg_free"])) >= DB_BYTES, "Less than 200GiB free in the SSD volume group")
        db = {}
    check_mount_target("/srv/db", db.get("uuid"))
    check_mount_target("/srv/backups", config.get("backup_uuid"))
    fstab = Path("/etc/fstab").read_text()
    render_fstab(fstab, "CHECK-ONLY", "CHECK-ONLY")
    return disks, vgs, lvs, fstab, existing, backup_ready


def apply(plan):
    import pwd
    disks, vgs, lvs, fstab, existing, backup_ready = plan
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    audit = Path("/var/backups/db-server-setup") / stamp
    audit.mkdir(parents=True, mode=0o700)
    atomic_write(audit / "before.json", json.dumps({"disks": disks, "vgs": vgs, "lvs": lvs}, indent=2))
    atomic_write(audit / "fstab.before", fstab)
    atomic_write(audit / "wd-partitions.before.txt", run("parted", "--script", BACKUP_DISK, "unit", "s", "print") + "\n")
    run("vgcfgbackup", "-f", str(audit / "ubuntu-vg.before.conf"), VG, change=True)
    if not existing:
        run("lvcreate", "--yes", "--size", "200G", "--name", LV, "--addtag", "db_server_data", VG, PV, change=True)
        run("mkfs.ext4", "-m", "1", "-L", "db_data", DB_DEVICE, change=True)
    if not backup_ready:
        run("wipefs", "--all", BACKUP_DEVICE, change=True)
        run("parted", "--script", BACKUP_DISK, "mklabel", "gpt", "mkpart", "primary", "ext4", "1MiB", "100%", change=True)
        run("partprobe", BACKUP_DISK, change=True)
        run("udevadm", "settle", change=True)
        run("mkfs.ext4", "-m", "0", "-L", "db_backup", BACKUP_DEVICE, change=True)
    db_uuid = run("blkid", "-s", "UUID", "-o", "value", DB_DEVICE)
    backup_uuid = run("blkid", "-s", "UUID", "-o", "value", BACKUP_DEVICE)
    require(db_uuid and backup_uuid and db_uuid != backup_uuid, "Invalid resulting UUIDs")
    CONFIG.parent.mkdir(mode=0o700, exist_ok=True)
    config = {"db_uuid": db_uuid, "backup_uuid": backup_uuid, "backup_serial": BACKUP_SERIAL,
              "database_size_gib": 200, "retain_backups": 7, "created_at": stamp,
              "database_engine": "not_configured"}
    atomic_write(CONFIG, json.dumps(config, indent=2) + "\n")
    atomic_write("/etc/fstab", render_fstab(fstab, db_uuid, backup_uuid), 0o644)
    for path, uuid in (("/srv/db", db_uuid), ("/srv/backups", backup_uuid)):
        Path(path).mkdir(mode=0o755, exist_ok=True)
    run("systemctl", "daemon-reload", change=True)
    for path, uuid in (("/srv/db", db_uuid), ("/srv/backups", backup_uuid)):
        if mount_uuid(path) is None:
            run("mount", path, change=True)
        require(mount_uuid(path) == uuid, f"Wrong filesystem on {path}")
    Path("/srv/db/data").mkdir(mode=0o700, exist_ok=True)
    exports = Path("/srv/db/exports")
    exports.mkdir(mode=0o700, exist_ok=True)
    account = pwd.getpwnam("jjj")
    os.chown(exports, account.pw_uid, account.pw_gid)
    os.chmod("/srv/backups", 0o700)
    backup_root = Path("/srv/backups/db-server")
    backup_root.mkdir(mode=0o700, exist_ok=True)
    root = Path(__file__).resolve().parent.parent
    shutil.copyfile(root / "scripts/backup-server.py", "/usr/local/sbin/db-server-backup")
    os.chmod("/usr/local/sbin/db-server-backup", 0o750)
    for name in ("db-server-backup.service", "db-server-backup.timer"):
        destination = Path("/etc/systemd/system") / name
        shutil.copyfile(root / "systemd" / name, destination)
        os.chmod(destination, 0o644)
    run("systemctl", "daemon-reload", change=True)
    run("systemctl", "start", "db-server-backup.service", change=True)
    run("systemctl", "enable", "--now", "db-server-backup.timer", change=True)
    report = {"completed_at": dt.datetime.now(dt.timezone.utc).isoformat(), "storage": config,
              "filesystems": run("df", "-hT", "/", "/srv/db", "/srv/backups"),
              "volumes": lvm("vgs", "vg", "vg_name,vg_size,vg_free", VG),
              "timer": run("systemctl", "list-timers", "db-server-backup.timer", "--no-pager"),
              "audit_directory": str(audit)}
    atomic_write(audit / "after.json", json.dumps(report, indent=2) + "\n")
    # A non-secret report is readable over SSH so the actual resulting state can be verified.
    Path("/var/lib/db-server").mkdir(mode=0o755, exist_ok=True)
    atomic_write("/var/lib/db-server/storage-status.json", json.dumps(report, indent=2) + "\n", 0o644)
    print(json.dumps(report, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Apply storage changes and run the first backup")
    parser.add_argument("--erase-backup", metavar="SERIAL", help="Explicitly authorize clearing the identified WDC disk")
    args = parser.parse_args()
    require(not args.apply or args.erase_backup == BACKUP_SERIAL,
            f"Apply requires --erase-backup {BACKUP_SERIAL}; all old WDC data will be lost")
    require(args.erase_backup is None or args.erase_backup == BACKUP_SERIAL, "Wrong backup disk serial")
    plan = preflight()
    print(json.dumps({"mode": "APPLY" if args.apply else "READ-ONLY PREFLIGHT", "database": DB_DEVICE,
                      "database_size_gib": 200, "mounts": ["/srv/db", "/srv/backups"],
                      "backup_disk_to_clear": BACKUP_DISK, "backup_serial": BACKUP_SERIAL,
                      "vgs_before": plan[1]}, indent=2), flush=True)
    if args.apply:
        import fcntl
        with open("/run/lock/db-server-setup.lock", "w", encoding="utf-8") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            apply(preflight())


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, subprocess.CalledProcessError, ValueError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        if isinstance(error, subprocess.CalledProcessError):
            print(error.stderr, file=sys.stderr)
        sys.exit(1)
