#!/usr/bin/env python3
"""Edit a Syncthing config.xml for intranet-only (Tailscale) aggregation.

Idempotent: re-running with the same args does not duplicate devices/folders.
"""
import argparse
import sys
import xml.etree.ElementTree as ET

# options that must be OFF so Syncthing has no public code path
ISOLATE_FALSE = {
    "globalAnnounceEnabled": "false",   # never report to public discovery servers
    "localAnnounceEnabled": "false",    # no local broadcast (a tailnet is not L2)
    "relaysEnabled": "false",           # never route through public relays
    "natEnabled": "false",              # no UPnP hole punching
    "crashReportingEnabled": "false",   # no crash reporting
    "startBrowser": "false",
    "announceLANAddresses": "false",
}
ISOLATE_SET = {
    "autoUpgradeIntervalH": "0",        # no auto-upgrade (it phones home)
    "urAccepted": "-1",                 # decline anonymous usage reporting
}


def set_child_text(parent, tag, text):
    el = parent.find(tag)
    if el is None:
        el = ET.SubElement(parent, tag)
    el.text = text


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--listen", help="e.g. tcp://<tailnet-ip>:22000")
    ap.add_argument("--isolate", action="store_true")
    ap.add_argument("--add-device", action="append", default=[],
                    help="ID|NAME|ADDR")
    ap.add_argument("--add-folder", action="append", default=[],
                    help="ID|PATH|TYPE|DEVID,DEVID")
    args = ap.parse_args()

    tree = ET.parse(args.config)
    root = tree.getroot()
    opts = root.find("options")

    if args.isolate:
        for k, v in ISOLATE_FALSE.items():
            set_child_text(opts, k, v)
        for k, v in ISOLATE_SET.items():
            set_child_text(opts, k, v)

    if args.listen:
        # collapse any existing listenAddress entries to the single tailnet one
        for el in opts.findall("listenAddress"):
            opts.remove(el)
        la = ET.SubElement(opts, "listenAddress")
        la.text = args.listen

    existing_devs = {d.get("id") for d in root.findall("device")}
    for spec in args.add_device:
        did, name, addr = spec.split("|", 2)
        if did in existing_devs:
            continue
        d = ET.SubElement(root, "device")
        d.set("id", did); d.set("name", name); d.set("compression", "metadata")
        d.set("introducer", "false")
        a = ET.SubElement(d, "address"); a.text = addr
        p = ET.SubElement(d, "paused"); p.text = "false"

    existing_folders = {f.get("id"): f for f in root.findall("folder")}
    paths_in_use = {f.get("path"): f.get("id") for f in root.findall("folder")}
    for spec in args.add_folder:
        fid, path, ftype, devids = spec.split("|", 3)
        want = [d for d in devids.split(",") if d]
        old = existing_folders.get(fid)
        if old is not None:
            # A second hub for an already-onboarded source lands here. Skipping
            # would leave the new hub unshared while every script still printed
            # READY — so union the devices in instead of returning silently.
            if old.get("path") != path:
                sys.exit(f"ERROR: folder {fid} already exists at {old.get('path')}, "
                         f"refusing to repoint it at {path}")
            have = {d.get("id") for d in old.findall("device")}
            added = [d for d in want if d not in have]
            for dv in added:
                ET.SubElement(old, "device").set("id", dv)
            print(f"OK  folder {fid} exists; "
                  + (f"shared with {len(added)} new device(s)" if added else "no change"))
            continue
        if path in paths_in_use:
            # Syncthing refuses two folders on one path; catch it here where the
            # message can say which folder already owns it.
            sys.exit(f"ERROR: {path} is already shared as folder "
                     f"'{paths_in_use[path]}' — reuse that folder id, do not add {fid}")
        f = ET.SubElement(root, "folder")
        f.set("id", fid); f.set("label", fid); f.set("path", path)
        f.set("type", ftype); f.set("rescanIntervalS", "3600")
        f.set("fsWatcherEnabled", "true")
        for dv in devids.split(","):
            fd = ET.SubElement(f, "device"); fd.set("id", dv)

    tree.write(args.config, encoding="utf-8", xml_declaration=False)
    print(f"OK  wrote {args.config}")


if __name__ == "__main__":
    main()
