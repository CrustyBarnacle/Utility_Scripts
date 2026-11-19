#!/usr/bin/env python3
# =============================================================================
# bt_speaker.py — Bluetooth speaker connect + PipeWire routing + rmpc launcher
# =============================================================================
# Connects to a specific Bluetooth speaker via BlueZ over D-Bus (async, using
# dbus-next), waits for the real Connected=True property change rather than
# polling, then fixes up PipeWire routing so audio doesn't get stranded on the
# wrong sink or left muted, sets volume, and (re)starts rmpc.
#
#
# Dependencies: dbus-next (pip install dbus-next), wpctl + pw-dump (pipewire),
# pactl (pipewire-pulse), rmpc
#
# Config file (like weather's ~/.config/weather/config): the speaker's MAC
# address is personal/physical-hardware info, so it isn't hardcoded here or
# committed to git. Set it in ~/.config/bt_speaker/config (chmod 600):
#
#   MAC=AA:BB:CC:DD:EE:FF
#   VOLUME=45%
#
# A MAC passed on the command line always overrides the config file.
#
# Usage:
#   ./bt_speaker.py [MAC] [--volume 45%] [--play]
#
# See --help for full usage.
# =============================================================================

import argparse
import asyncio
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

from dbus_next import BusType, Variant
from dbus_next.aio import MessageBus
from dbus_next.errors import DBusError

CONFIG_FILE = Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser() / "bt_speaker" / "config"
ALLOWED_CONFIG_KEYS = {"MAC", "VOLUME"}

DEFAULT_VOLUME = "45%"
MAX_VOLUME_PCT = 150
CONNECT_TIMEOUT = 15
SINK_WAIT_TIMEOUT = 10
SINK_POLL_INTERVAL = 0.5

MAC_RE = re.compile(r"^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$")
VOLUME_RE = re.compile(r"^(\d+(?:\.\d+)?)%?$")


def load_config() -> dict[str, str]:
    config: dict[str, str] = {}
    if not CONFIG_FILE.is_file():
        return config

    # Refuse to load if group/world-accessible (would let other users on the
    # box read or, worse, tamper with values this script trusts).
    mode = CONFIG_FILE.stat().st_mode
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        print(
            f"Warning: config file '{CONFIG_FILE}' is group/world-accessible — skipping for safety.\n"
            f"         Run: chmod 600 {CONFIG_FILE}",
            file=sys.stderr,
        )
        return config

    for line in CONFIG_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if key in ALLOWED_CONFIG_KEYS:
            config[key] = value
        else:
            print(f"Warning: unknown config key '{key}' ignored.", file=sys.stderr)
    return config


def mac_type(value: str) -> str:
    if not MAC_RE.match(value):
        raise argparse.ArgumentTypeError(f"invalid MAC address {value!r} — expected format AA:BB:CC:DD:EE:FF")
    return value.upper()


def volume_type(value: str) -> str:
    # wpctl treats a bare number as *linear* volume (e.g. "55" == 5500%), not
    # a percentage — always normalize to an explicit "%" so that mistake
    # can't blast the speaker.
    match = VOLUME_RE.match(value.strip())
    if not match:
        raise argparse.ArgumentTypeError(f"invalid volume {value!r} — expected a percentage like '45%' or '45'")
    pct = float(match.group(1))
    if not 0 <= pct <= MAX_VOLUME_PCT:
        raise argparse.ArgumentTypeError(f"volume {value!r} out of allowed range (0-{MAX_VOLUME_PCT}%)")
    return f"{pct:g}%"


def device_path(mac: str) -> str:
    return f"/org/bluez/hci0/dev_{mac.replace(':', '_')}"


async def connect_device(bus: MessageBus, mac: str) -> None:
    path = device_path(mac)
    introspection = await bus.introspect("org.bluez", path)
    proxy = bus.get_proxy_object("org.bluez", path, introspection)
    device = proxy.get_interface("org.bluez.Device1")
    props = proxy.get_interface("org.freedesktop.DBus.Properties")

    if await device.get_connected():
        print(f"Already connected to {mac}")
        return

    connected_evt = asyncio.Event()

    def on_properties_changed(interface, changed, invalidated):
        if interface != "org.bluez.Device1":
            return
        value = changed.get("Connected")
        if isinstance(value, Variant) and value.value:
            connected_evt.set()

    props.on_properties_changed(on_properties_changed)

    print(f"Connecting to {mac}...")
    try:
        await device.call_connect()
    except DBusError as exc:
        if exc.type != "br-connection-unknown":
            raise
        # BlueZ's connection-state tracking has drifted out of sync with the
        # kernel's actual ACL link (stale leftover from a prior session) —
        # an explicit Disconnect resets that state before retrying Connect.
        print(f"BlueZ reported {exc.type}; resetting connection state and retrying...")
        await device.call_disconnect()
        await device.call_connect()

    try:
        await asyncio.wait_for(connected_evt.wait(), timeout=CONNECT_TIMEOUT)
    except asyncio.TimeoutError:
        sys.exit(f"Timed out waiting for {mac} to report Connected over D-Bus")

    print(f"Connected to {mac}")
    props.off_properties_changed(on_properties_changed)


def run(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, check=check)


def find_bluez_sink_id(mac: str) -> int | None:
    result = run("pw-dump", check=False)
    if result.returncode != 0:
        return None
    try:
        objects = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None

    wanted_addr = mac.lower()
    for obj in objects:
        info = obj.get("info") or {}
        props = info.get("props") or {}
        if props.get("media.class") != "Audio/Sink":
            continue
        addr = str(props.get("api.bluez5.address", "")).lower()
        if addr == wanted_addr:
            return obj.get("id")
    return None


async def wait_for_sink(mac: str) -> int:
    elapsed = 0.0
    while elapsed < SINK_WAIT_TIMEOUT:
        sink_id = find_bluez_sink_id(mac)
        if sink_id is not None:
            return sink_id
        await asyncio.sleep(SINK_POLL_INTERVAL)
        elapsed += SINK_POLL_INTERVAL
    sys.exit(f"Timed out waiting for PipeWire to expose a sink for {mac}")


def route_audio(sink_id: int, volume: str) -> None:
    run("wpctl", "set-default", str(sink_id))
    run("wpctl", "set-mute", str(sink_id), "0")
    run("wpctl", "set-volume", str(sink_id), volume)

    # Migrate anything already playing onto the new default sink, and clear
    # mute on every sink so built-in speakers don't come back silenced.
    default_sink = run("pactl", "get-default-sink", check=False).stdout.strip()
    if default_sink:
        sink_inputs = run("pactl", "list", "short", "sink-inputs", check=False).stdout
        for line in sink_inputs.splitlines():
            if not line.strip():
                continue
            input_id = line.split()[0]
            run("pactl", "move-sink-input", input_id, default_sink, check=False)

    sinks = run("pactl", "list", "short", "sinks", check=False).stdout
    for line in sinks.splitlines():
        if not line.strip():
            continue
        sink_name = line.split()[1]
        run("pactl", "set-sink-mute", sink_name, "0", check=False)


def start_rmpc() -> None:
    run("rmpc", "stop", check=False)
    run("rmpc", "play", check=False)


async def main() -> None:
    config = load_config()

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "mac", nargs="?", default=config.get("MAC"),
        help=f"Bluetooth MAC address (default: MAC from {CONFIG_FILE} if set)",
    )
    parser.add_argument(
        "--volume", "-vol",
        default=config.get("VOLUME", DEFAULT_VOLUME),
    )
    parser.add_argument("--play", action="store_true", help="Also (re)start rmpc playback after connecting/routing")
    args = parser.parse_args()

    if args.mac is None:
        parser.error(f"no MAC address given and none set in {CONFIG_FILE} — pass one, or add MAC=AA:BB:CC:DD:EE:FF to the config file")
    try:
        args.mac = mac_type(args.mac)
        args.volume = volume_type(args.volume)
    except argparse.ArgumentTypeError as exc:
        parser.error(str(exc))

    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    await connect_device(bus, args.mac)
    bus.disconnect()

    sink_id = await wait_for_sink(args.mac)
    print(f"Routing audio to PipeWire sink {sink_id} at {args.volume}")
    route_audio(sink_id, args.volume)

    if args.play:
        print("Re/starting rmpc playback")
        start_rmpc()


if __name__ == "__main__":
    asyncio.run(main())
