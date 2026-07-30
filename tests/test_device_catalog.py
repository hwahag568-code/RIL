from pathlib import Path
import unittest
import xml.etree.ElementTree as ET

import RIL_client
import ril_devices


EXPECTED_DEVICE_IDS = (
    "D1",
    "D2",
    "Cobas",
    "Al1",
    "Al3",
    "AU1",
    "AU2",
    "AU3",
    "pHox",
    "Presep",
    "UA",
    "OC",
    "Nova",
)


class DeviceCatalogTests(unittest.TestCase):
    def test_current_device_order_and_addresses_are_preserved(self):
        self.assertEqual(ril_devices.DEVICE_IDS, EXPECTED_DEVICE_IDS)
        self.assertEqual(ril_devices.DEVICE_IPS["Al1"], "10.2.151.52")
        self.assertEqual(ril_devices.DEVICE_IPS["Al3"], "10.2.151.53")
        self.assertEqual(ril_devices.DEVICE_IPS["AU3"], "10.2.151.219")

    def test_current_groups_are_derived_from_catalog(self):
        self.assertEqual(
            ril_devices.devices_in_group("AU"),
            ("AU1", "AU2", "AU3"),
        )
        self.assertEqual(
            ril_devices.devices_in_group("DxI"),
            ("D1", "D2"),
        )
        self.assertEqual(
            ril_devices.devices_in_group("Alinity"),
            ("Al1", "Al3"),
        )
        self.assertEqual(
            ril_devices.devices_in_group("jochul"),
            ("pHox", "Presep", "UA", "OC", "Nova"),
        )
        self.assertEqual(
            ril_devices.devices_in_group("dangjik"),
            (
                "D1",
                "D2",
                "Cobas",
                "Al1",
                "Al3",
                "AU1",
                "AU2",
                "pHox",
                "Presep",
                "UA",
                "OC",
                "Nova",
            ),
        )

    def test_duty_preset_unchecks_au3(self):
        class CheckBox:
            def __init__(self):
                self.checked = True

            def setChecked(self, checked):
                self.checked = checked

        class Window:
            _set_group_checked = RIL_client.WindowClass._set_group_checked

        window = Window()
        window.interface_list = ril_devices.DEVICE_IDS
        window.interface_list_dangjik = ril_devices.devices_in_group(
            "dangjik"
        )
        for device_id in ril_devices.DEVICE_IDS:
            setattr(window, f"checkBox_{device_id}", CheckBox())

        RIL_client.WindowClass.select_dangjik(window)

        self.assertFalse(window.checkBox_AU3.checked)
        for device_id in window.interface_list_dangjik:
            with self.subTest(device_id=device_id):
                self.assertTrue(
                    getattr(window, f"checkBox_{device_id}").checked
                )

    def test_manual_checkbox_change_clears_selection_preset(self):
        class RadioButton:
            def __init__(self, checked=False):
                self.checked = checked
                self.exclusive = True

            def autoExclusive(self):
                return self.exclusive

            def setAutoExclusive(self, enabled):
                self.exclusive = enabled

            def setChecked(self, checked):
                self.checked = checked

        window = type("Window", (), {})()
        for index, name in enumerate(
            RIL_client.WindowClass._SELECTION_PRESET_NAMES
        ):
            setattr(window, name, RadioButton(index == 2))
        window._SELECTION_PRESET_NAMES = (
            RIL_client.WindowClass._SELECTION_PRESET_NAMES
        )

        RIL_client.WindowClass._clear_selection_preset(window)

        for name in window._SELECTION_PRESET_NAMES:
            button = getattr(window, name)
            self.assertFalse(button.checked)
            self.assertTrue(button.exclusive)

    def test_every_device_command_is_supported(self):
        self.assertTrue(
            set(ril_devices.DEVICE_COMMANDS.values())
            <= ril_devices.SUPPORTED_LOGIN_COMMANDS
        )
        self.assertEqual(
            ril_devices.LEGACY_CLIENT_COMMANDS,
            {"AU1": "AU22", "AU2": "AU32"},
        )
        self.assertEqual(
            ril_devices.AU_COMMAND_NUMBERS,
            {
                "AU1": 1,
                "AU2": 2,
                "AU3": 3,
                "AU22": 1,
                "AU32": 2,
            },
        )

    def test_static_ui_has_one_checkbox_and_label_per_device(self):
        ui_path = Path(__file__).resolve().parents[1] / "RIL.ui"
        root = ET.parse(ui_path).getroot()
        names = {
            widget.attrib["name"]
            for widget in root.iter("widget")
            if "name" in widget.attrib
        }
        expected_checkboxes = {
            f"checkBox_{device_id}"
            for device_id in EXPECTED_DEVICE_IDS
        }
        actual_checkboxes = {
            name
            for name in names
            if name.startswith("checkBox_")
        }

        self.assertEqual(actual_checkboxes, expected_checkboxes)
        for device_id in EXPECTED_DEVICE_IDS:
            self.assertIn(f"label_{device_id}", names)

    def test_st2_is_not_part_of_active_catalog(self):
        self.assertNotIn("ST2", ril_devices.DEVICE_IDS)
        self.assertNotIn("ST2", ril_devices.DEVICE_IPS)
        self.assertNotIn("ST2", ril_devices.SUPPORTED_LOGIN_COMMANDS)


if __name__ == "__main__":
    unittest.main()
