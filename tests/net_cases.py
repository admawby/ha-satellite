"""Network throughput: interface filtering, rates between polls, counter resets, HA entities."""

from __future__ import annotations

import json
import os
import tempfile
from collections import namedtuple
from typing import Callable

Counters = namedtuple("Counters", "bytes_sent bytes_recv packets_sent packets_recv errin errout dropin dropout")
Stats = namedtuple("Stats", "isup duplex speed mtu")
Addr = namedtuple("Addr", "family address netmask broadcast ptp")


def run(check: Callable[[bool, str], None], sat) -> None:
    from hasat import ha_bridge
    from hasat_agent import net

    sysnet = tempfile.mkdtemp(prefix="hasat-net-")
    for iface, physical, wireless in (("enp3s0", True, False), ("wlan0", True, True), ("docker0", False, False),
                                      ("veth1a2b", False, False), ("tailscale0", False, False), ("lo", False, False)):
        os.makedirs(os.path.join(sysnet, iface))
        if physical:
            os.makedirs(os.path.join(sysnet, iface, "device"))
        if wireless:
            os.makedirs(os.path.join(sysnet, iface, "wireless"))
    counters = {
        "enp3s0": Counters(1_000_000, 50_000_000, 0, 0, 0, 0, 0, 0),
        "wlan0": Counters(0, 0, 0, 0, 0, 0, 0, 0),
        "docker0": Counters(9_999_999, 9_999_999, 0, 0, 0, 0, 0, 0),
        "veth1a2b": Counters(9_999_999, 9_999_999, 0, 0, 0, 0, 0, 0),
        "tailscale0": Counters(5, 5, 0, 0, 0, 0, 0, 0),
        "lo": Counters(1, 1, 0, 0, 0, 0, 0, 0),
    }
    clock = [100.0]
    saved = (net.SYS_NET, net.psutil.net_io_counters, net.psutil.net_if_stats, net.psutil.net_if_addrs, net.time.monotonic)
    net.SYS_NET = sysnet
    net.psutil.net_io_counters = lambda pernic=False: dict(counters)
    net.psutil.net_if_stats = lambda: {k: Stats(True, 2, 1000 if k == "enp3s0" else 0, 1500) for k in counters}
    net.psutil.net_if_addrs = lambda: {"enp3s0": [Addr(2, "192.168.0.8", "255.255.255.0", None, None)]}
    net.time.monotonic = lambda: clock[0]
    net._LAST.clear()
    try:
        first = net.collect()
        check([i["name"] for i in first["interfaces"]] == ["enp3s0", "wlan0"],
              "net: only physical interfaces counted (Docker, veth, VPN, loopback skipped)")
        check(first["rx_bps"] is None, "net: no rate until a second sample")
        clock[0] += 2.0
        c = counters["enp3s0"]
        counters["enp3s0"] = c._replace(bytes_recv=c.bytes_recv + 10_000_000, bytes_sent=c.bytes_sent + 500_000)
        counters["wlan0"] = Counters(250_000, 1_000_000, 0, 0, 0, 0, 0, 0)
        r = net.collect()
        enp = r["interfaces"][0]
        check(enp["rx_bps"] == 5_000_000 and enp["tx_bps"] == 250_000 and enp["speed_mbps"] == 1000
              and enp["ipv4"] == "192.168.0.8", "net: per-interface rates (40 Mbit/s down, 2 Mbit/s up)")
        check(r["rx_bps"] == 5_500_000 and r["tx_bps"] == 375_000 and r["interfaces"][1]["wireless"],
              "net: totals across interfaces, Wi-Fi recognised")
        clock[0] += 2.0
        counters["enp3s0"] = Counters(10, 10, 0, 0, 0, 0, 0, 0)  # reboot / driver reload
        r2 = net.collect()
        check(r2["interfaces"][0]["rx_bps"] is None, "net: counter reset does not produce a bogus rate")
    finally:
        net.SYS_NET, net.psutil.net_io_counters, net.psutil.net_if_stats, net.psutil.net_if_addrs, net.time.monotonic = saved

    metrics = sat.metrics
    try:
        sat.metrics = dict(metrics, net=r)
        values = ha_bridge.flatten(sat)
        check(values["net_rx"] == 44.0 and values["net_tx"] == 3.0 and values["net_enp3s0_rx"] == 40.0,
              f"net: HA values in Mbit/s ({values['net_rx']} / {values['net_tx']})")
        check(values["net_rx_total"] == round(r["rx_total"] / 1e9, 3), "net: data received in GB")
        keys = [e.key for e in ha_bridge.net_entities(sat)]
        check(keys == ["net_enp3s0_rx", "net_enp3s0_tx", "net_wlan0_rx", "net_wlan0_tx"],
              "net: per-interface entities when a device has several interfaces")
        published: dict = {}

        class FakeMqtt:
            def publish(self, topic, payload, retain=False):
                published[topic] = payload

        b = ha_bridge.HABridge(lambda *_: None)
        b._mqtt, b._mqtt_connected = FakeMqtt(), True
        b._publish_mqtt(sat)
        cfg = json.loads(published[f"homeassistant/sensor/hasat_{sat.id}/net_rx/config"])
        tot = json.loads(published[f"homeassistant/sensor/hasat_{sat.id}/net_rx_total/config"])
        check(cfg["device_class"] == "data_rate" and cfg["unit_of_measurement"] == "Mbit/s"
              and tot["state_class"] == "total_increasing" and tot["unit_of_measurement"] == "GB",
              "net: HA download sensor and data counter announced")
        sat.metrics = dict(metrics, net=dict(r, interfaces=r["interfaces"][:1]))  # Wi-Fi gone
        b._publish_mqtt(sat)
        check(published[f"homeassistant/sensor/hasat_{sat.id}/net_wlan0_rx/config"] == "" and not ha_bridge.net_entities(sat),
              "net: single interface -> only the totals; per-interface entities removed")
    finally:
        sat.metrics = metrics
