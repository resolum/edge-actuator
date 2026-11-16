import json

import pytest

from alivia_actuator_edge.devices.domain.model import DeviceKind, InvalidMacAddress, MacAddress, StaticDeviceTable
from alivia_actuator_edge.dispatch.domain.models import InvalidOrder, parse_order
from alivia_actuator_edge.shared.config import DeviceCfg
from tests.conftest import build_table, device_cfgs, make_order


def test_mac_canonica():
    assert str(MacAddress("aa-bb-cc-00-00-10")) == "AA:BB:CC:00:00:10"
    with pytest.raises(InvalidMacAddress):
        MacAddress("AA:BB:CC")


def test_acciones_por_tipo():
    assert DeviceKind.DOOR.actions == {"open", "close"}
    assert DeviceKind.LIGHT.actions == {"on", "off"}


def test_tabla_desde_config_y_repetidos():
    t = StaticDeviceTable.from_config(device_cfgs())
    assert t.get(13).kind is DeviceKind.LIGHT
    assert t.find_by_mac(MacAddress("aa:bb:cc:00:00:11")).device_id == 11
    with pytest.raises(ValueError):
        StaticDeviceTable.from_config(device_cfgs() + [DeviceCfg(99, "AA:BB:CC:00:00:10", "DOOR", "IN_USE")])
    with pytest.raises(ValueError):
        StaticDeviceTable.from_config([DeviceCfg(1, "AA:BB:CC:00:00:01", "TOASTER", "IN_USE")])


def test_parse_ok():
    o = parse_order("10", make_order())
    assert (o.device_id, o.action, o.correlation_id, o.ttl_ms) == (10, "open", "c1", 5000)


@pytest.mark.parametrize("payload,reason", [
    (b"no json", "JSON"),
    (b"[]", "objeto"),
    (json.dumps({"v": 2}).encode(), "correlationId"),
])
def test_parse_invalidos(payload, reason):
    with pytest.raises(InvalidOrder) as e:
        parse_order("10", payload)
    assert reason in e.value.reason


def test_parse_version_topic_y_campos():
    with pytest.raises(InvalidOrder) as e:
        parse_order("10", make_order(v=1))
    assert e.value.correlation_id == "c1"
    with pytest.raises(InvalidOrder):
        parse_order("11", make_order(device_id=10))  # topic distinto del payload
    with pytest.raises(InvalidOrder):
        parse_order("10", make_order(action=""))
