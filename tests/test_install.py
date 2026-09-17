import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from install import prepare_plugin, register_personal


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cachegate package 测试 ")
        self.addCleanup(self.temp.cleanup)
        self.home_dir = Path(self.temp.name)
        self.state = self.home_dir / "data"
        self.market = self.home_dir / ".agents" / "plugins" / "marketplace.json"

    def test_prepared_package_has_absolute_runtime_and_state_paths(self):
        destination = self.home_dir / "plugins" / "cachegate"
        prepare_plugin(destination, self.state)
        config = json.loads((destination / ".mcp.json").read_text())
        server = config["mcpServers"]["cachegate"]
        self.assertTrue(Path(server["command"]).is_absolute())
        self.assertTrue(Path(server["args"][0]).is_file())
        self.assertEqual(server["env"]["CACHEGATE_DATA_DIR"], str(self.state))

    def test_default_personal_marketplace(self):
        name, destination, market = register_personal(self.home_dir, self.state)
        self.assertEqual(name, "personal")
        catalog = json.loads(market.read_text())
        self.assertEqual((self.home_dir / catalog["plugins"][0]["source"]["path"]).resolve(), destination)

    def test_preserves_existing_marketplace(self):
        self.market.parent.mkdir(parents=True)
        existing = {"name": "existing", "interface": {"displayName": "Keep me"}, "custom": 42, "plugins": [{"name": "other"}]}
        self.market.write_text(json.dumps(existing))
        name, _, _ = register_personal(self.home_dir, self.state)
        self.assertEqual(name, "existing")
        actual = json.loads(self.market.read_text())
        self.assertEqual(actual["plugins"][0], {"name": "other"})
        self.assertEqual(actual["interface"], existing["interface"])
        self.assertEqual(actual["custom"], 42)

    def test_existing_plugin_is_not_overwritten(self):
        destination = self.home_dir / "plugins" / "cachegate"
        destination.mkdir(parents=True)
        marker = destination / "user.txt"
        marker.write_text("keep")
        with self.assertRaises(FileExistsError):
            register_personal(self.home_dir, self.state)
        self.assertEqual(marker.read_text(), "keep")
        self.assertFalse(self.market.exists())

    def test_duplicate_entry_is_not_overwritten(self):
        self.market.parent.mkdir(parents=True)
        original = '{"name":"personal","plugins":[{"name":"cachegate","custom":true}]}'
        self.market.write_text(original)
        with self.assertRaises(FileExistsError):
            register_personal(self.home_dir, self.state)
        self.assertEqual(self.market.read_text(), original)
        self.assertFalse((self.home_dir / "plugins" / "cachegate").exists())

    def test_invalid_marketplace_stops_before_writing_plugin(self):
        self.market.parent.mkdir(parents=True)
        self.market.write_text('{"name":"invalid name","plugins":[]}')
        with self.assertRaises(ValueError):
            register_personal(self.home_dir, self.state)
        self.assertFalse((self.home_dir / "plugins" / "cachegate").exists())


if __name__ == "__main__":
    unittest.main()
