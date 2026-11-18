from deephack import device

# periodic sensor frame exactly as the official app sent it
CAPTURED = bytes.fromhex("aa2e01" "460000" "150000" "4c0000" "03001c" "040062" "0c0008" "050032" + "00" * 18 + "48494443" "6803")


def test_sensor_payload_matches_capture():
    payload = device.sensor_payload(cpu_temp=70, field_1=21, field_2=76, v3_3=3.28, v5=4.98, v12=12.08, cpu_freq=5.50)
    assert device.frame(device.CMD_SENSORS, payload) == CAPTURED


def test_sensor_payload_integer_and_fraction():
    p = device.sensor_payload(cpu_temp=51, v3_3=44.87)
    assert p[0:3] == bytes([51, 0, 0]) and p[9:12] == bytes([44, 0, 87])
    assert device.sensor_payload(cpu_temp=1000)[0:3] == bytes([0xE8, 0x03, 0])


def test_sensor_payload_rejects_negative():
    try:
        device.sensor_payload(cpu_temp=-1)
    except ValueError:
        return
    raise AssertionError("expected ValueError")
