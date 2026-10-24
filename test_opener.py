#!/usr/bin/env python3
"""Run with python3 test_opener.py; no dotfiles or live Rex needed."""

import importlib.util
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

PACKAGE = Path(__file__).resolve().parent


class StandaloneOpener(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="rex opener's ")
        self.addCleanup(self.temporary.cleanup)
        self.package = Path(self.temporary.name) / "plugin"
        shutil.copytree(PACKAGE, self.package, ignore=shutil.ignore_patterns("__pycache__"))
        helper = self.package / "opener.py"
        self.assertTrue(helper.is_file(), "The exported plugin must contain its own opener")
        spec = importlib.util.spec_from_file_location("rex_opener", helper)
        assert spec and spec.loader
        self.opener = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.opener)

    def test_exported_package_extracts_urls_and_paths_without_dotfiles(self):
        target = self.package / "some file.py"
        target.touch()
        rows = self.opener.text_targets("example.com/help).\n[file](some file.py:17:2)\n", self.package.resolve())
        self.assertIn(("url", "https://example.com/help", "example.com/help", ""), rows)
        self.assertIn(("text", str(target.resolve()), "some file.py:17", "17"), rows)

    def test_capture_uses_utf8_byte_offsets(self):
        rex = Mock()
        rex.call.side_effect = [{"content": "é", "id": 12, "eof": False}, {"data": " next", "eof": True}]
        self.assertEqual(self.opener.capture_text(rex, "s", "b"), "é next")
        self.assertEqual(rex.call.call_args.kwargs["args"]["offset"], 2)

    def test_capture_rejects_a_stalled_blob(self):
        rex = Mock()
        rex.call.side_effect = [{"content": "é", "id": 12, "eof": False}, {"data": "", "eof": False}]
        with self.assertRaisesRegex(RuntimeError, "no progress"):
            self.opener.capture_text(rex, "s", "b")

    def test_text_path_and_line_are_quoted_for_editor(self):
        with patch.object(self.opener, "split_command") as split:
            self.opener.open_target(
                Mock(), "s", "b", "c", self.package, ("text", "/tmp/a file's.py", "source:17", "17")
            )
        self.assertEqual(self.opener.shlex.split(split.call_args.args[-1]), ["nvim", "+17", "--", "/tmp/a file's.py"])

    def test_cancelled_selection_does_not_open_anything(self):
        with (
            patch.object(self.opener, "capture_text", return_value=""),
            patch.object(self.opener, "text_targets", return_value=[("url", "https://a.com", "a", "")] * 2),
            patch.object(self.opener, "choose", return_value=""),
            patch.object(self.opener, "open_target") as opened,
        ):
            self.opener.open_text_picker(Mock(), "s", "b", "c", self.package)
        opened.assert_not_called()

    def test_missing_picker_is_reported_instead_of_treated_as_cancellation(self):
        with patch.object(self.opener, "popup", return_value=127):
            with self.assertRaisesRegex(RuntimeError, "fzf"):
                self.opener.choose(Mock(), "s", "b", "c", self.package, ["0\turl\texample.com"], "open ")

    def test_remote_terminal_is_rejected_before_reading_output(self):
        rex = Mock(identity={"host_id": "local"})
        rex.call.return_value = {"block": {"host_id": "remote"}}
        with self.assertRaisesRegex(RuntimeError, "not on this machine"):
            self.opener.context(rex, "s", "b", "c")
        self.assertEqual(rex.call.call_count, 1)

    def test_another_users_terminal_is_rejected(self):
        rex = Mock(identity={"host_id": "local"})
        rex.call.side_effect = [
            {"block": {"host_id": "local"}},
            {"child": {"user_id": self.opener.os.getuid() + 1, "path": "/bin/fish"}},
        ]
        with self.assertRaisesRegex(RuntimeError, "not owned"):
            self.opener.context(rex, "s", "b", "c")

    def test_editor_split_does_not_redirect_another_app(self):
        rex = Mock()
        rex.call.side_effect = [
            {"columns": 120, "rows": 40},
            {"block_ids": ["new"]},
            {"block": {"window_id": "w"}},
            {"clients": [{"client_id": "peer", "info": {"kind": "app"}, "session_ids": ["s"]}]},
        ]
        with patch.object(self.opener, "layout", return_value={}):
            route = self.opener.split_command(rex, "s", "b", "c", self.package, "nvim file")
        self.assertEqual(route, {"session": "s", "window": "w", "block": "new"})
        self.assertFalse(rex.call.call_args_list[1].kwargs["focus"])
        rex.action.assert_not_called()

    def test_shared_picker_cleans_up_private_session_even_when_select_fails(self):
        rex = Mock()
        rex.call.side_effect = [
            {"block": {"window_id": "w"}},
            {"clients": [{"client_id": "peer", "info": {"kind": "app"}, "session_ids": ["s"]}]},
            {"session_id": "private"},
            {"window_id": "private-window", "block_ids": ["picker"]},
            {"clients": []},
            {},
        ]
        rex.action.side_effect = RuntimeError("client closed")
        with patch.object(self.opener, "layout", return_value={"block": {"options": {}}}):
            with self.assertRaisesRegex(RuntimeError, "client closed"):
                self.opener.popup(rex, "s", "b", "c", self.package, "fzf")
        rex.call.assert_called_with("session.destroy", session_id="private")
        self.assertFalse(any(call.args[0] == "session.new_layer" for call in rex.call.call_args_list))

    def test_detached_failure_is_displayed_in_invoking_client(self):
        with (
            patch.object(self.opener.sys, "argv", ["opener.py", "--session", "s", "--block", "b", "--client", "c"]),
            patch.object(self.opener, "Rex", return_value=Mock()),
            patch.object(self.opener, "context", return_value=({}, self.package, "c")),
            patch.object(self.opener, "open_text_picker", side_effect=RuntimeError("capture failed")),
            patch.object(self.opener, "popup", return_value=0) as popup,
        ):
            with self.assertRaisesRegex(RuntimeError, "capture failed"):
                self.opener.main()
        self.assertEqual(popup.call_args.args[1:4], ("s", "b", "c"))
        self.assertIn("capture failed", popup.call_args.args[-1])

    @unittest.skipUnless(shutil.which("nvim"), "Neovim is needed for the Lua registration check")
    def test_registration_leaves_key_choice_to_the_user(self):
        script = self.package / "check.lua"
        script.write_text("""
local actions, bindings, launched = {}, {}, nil
rex = {
  exec = setmetatable({ start = function(argv, opts) launched = {argv=argv, opts=opts}; return {} end },
    { __call = function(_, argv) return {ok=true, stdout=argv[2] .. "\\n"} end }),
  action = function(action) actions[action.name] = action end,
  bind = function(key, action) bindings[key] = action end,
}
dofile(arg[1])({path="/custom/bin"})
assert(next(bindings) == nil, "Plugin must not bind keys")
local action = assert(actions.rex_opener_open, "Public action missing")
rex.bind("cmd+shift+o", action.name)
assert(bindings["cmd+shift+o"] == "rex_opener_open")
assert(not pcall(action.run, {}), "Must require an exact terminal")
action.run({session_id="session:one", block_id="block:one", client_id="client:one"})
assert(launched.opts.detach and launched.opts.env.PATH == "/custom/bin")
assert(launched.opts.env.TMUX == false)
assert(launched.argv[1] == "python3")
assert(launched.argv[2] == arg[1]:gsub("init.lua$", "opener.py"))
assert(launched.argv[3] == "--session" and launched.argv[4] == "session:one")
assert(launched.argv[7] == "--client" and launched.argv[8] == "client:one")
""")
        result = subprocess.run(
            ["nvim", "--headless", "-u", "NONE", "-i", "NONE", "-l", str(script), str(self.package / "init.lua")],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
