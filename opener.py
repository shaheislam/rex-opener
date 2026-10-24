#!/usr/bin/env python3
"""Standalone local Rex URL/file opener. Python standard library only."""

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from urllib.parse import unquote, urlparse
import uuid

PACKAGE = Path(__file__).resolve().parent


def run(argv, **kwargs):
    result = subprocess.run([str(part) for part in argv], text=True, capture_output=True, **kwargs)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or f"Command failed: {argv[0]}")
    return result.stdout.strip()


class Rex:
    def __init__(self):
        bundled = Path(os.environ.get("REX_BIN_DIR", "/nonexistent")) / "rex"
        self.binary = str(bundled) if bundled.is_file() else shutil.which("rex")
        if not self.binary:
            raise RuntimeError("Put the Rex CLI on PATH or set REX_BIN_DIR to its directory")
        self.identity = json.loads(run([self.binary, "--autostart=false", "whoami", "--json"]))
        endpoint = urlparse(self.identity["endpoint"])
        if endpoint.scheme != "unix" or not stat.S_ISSOCK(os.stat(unquote(endpoint.path)).st_mode):
            raise RuntimeError("The opener supports a local Rex server only")

    def call(self, method, **args):
        return json.loads(run([self.binary, "--autostart=false", "api", "call", method, json.dumps(args)]))

    def action(self, client, action, **args):
        if not client:
            raise RuntimeError("An explicit Rex client is required for focus")
        return run([self.binary, "--autostart=false", "-C", client, "do", action, "--args", json.dumps(args)])


def context(rex, session, block, client=""):
    if not session or not block:
        raise RuntimeError("Exact Rex session and block are required")
    description = rex.call("session.describe_block", session_id=session, block_id=block)["block"]
    if description.get("host_id") != rex.identity["host_id"]:
        raise RuntimeError("The target block is not on this machine")
    process = rex.call("com.superlogical.terminal.process", session_id=session, block_id=block, args={})
    child = process.get("child") or {}
    login = child.get("user_id") == 0 and child.get("path") == "/usr/bin/login" and sys.platform == "darwin"
    if child.get("user_id") != os.getuid() and not login:
        raise RuntimeError("The terminal is not owned by this user")
    if not client:
        candidates = [
            peer["client_id"]
            for peer in rex.call("client.list")["clients"]
            if peer.get("info", {}).get("kind") == "app"
            and session in peer.get("session_ids", [])
            and peer["client_id"] == description.get("owner")
        ]
        client = candidates[0] if len(candidates) == 1 else ""
    cwd = (process.get("foreground") or child).get("cwd")
    if not cwd:
        cwd = rex.call("com.superlogical.terminal.pwd", session_id=session, block_id=block, args={}).get("pwd", "")
        cwd = unquote(urlparse(cwd).path) if cwd.startswith("file://") else cwd
    if not cwd or not Path(cwd).is_dir():
        raise RuntimeError("The terminal's working directory is unavailable")
    return description, Path(cwd), client


def layout(cwd, command):
    fish = shutil.which("fish")
    if not fish:
        raise RuntimeError("Fish is required for picker and editor terminals")
    reported = r"\033]7;" + Path(cwd).resolve().as_uri() + r"\033\134"
    # Fish -c otherwise hides commands in login's foreground process group.
    launch = (
        "status job-control full; "
        + command
        + "; set -l s $status; while jobs -q; "
        + "function _rex_job_exit --on-process-exit (jobs -lp)[-1] --no-scope-shadowing; set s $argv[3]; end; "
        + "fg; end; exit $s"
    )
    return {
        "block": {
            "flavor": "com.superlogical.terminal.shell",
            "label": "opener",
            "options": {
                "cwd": str(cwd),
                "shell": "none",
                "command": [
                    "env",
                    "-u",
                    "TMUX",
                    "-u",
                    "TMUX_PANE",
                    "-u",
                    "NVIM",
                    "-u",
                    "OPENCODE_NVIM_GENERATION",
                    "-u",
                    "CLAUDECODE",
                    fish,
                    "-lic",
                    "printf %b " + shlex.quote(reported) + "; " + launch,
                ],
            },
        }
    }


def process_info(pid):
    fields = run(["ps", "-p", str(pid), "-o", "ppid=", "-o", "uid=", "-o", "tty="]).split()
    if len(fields) != 3:
        raise RuntimeError("Process ownership unavailable")
    return int(fields[0]), int(fields[1]), fields[2]


def owned_tty(process, pid):
    child = process.get("child") or {}
    owner = child.get("pid")
    login = child.get("user_id") == 0 and child.get("path") == "/usr/bin/login" and sys.platform == "darwin"
    if not owner or (child.get("user_id") != os.getuid() and not login):
        raise RuntimeError("Terminal ownership unavailable")
    current = pid
    for _ in range(64):
        parent, uid, tty = process_info(current)
        if current == owner:
            if uid != os.getuid() and not (login and uid == 0):
                break
            if tty in ("?", "??", "-"):
                break
            return tty if tty.startswith("/dev/") else "/dev/" + tty
        if uid != os.getuid() or parent == current or parent <= 1:
            break
        current = parent
    raise RuntimeError("Terminal ownership not proven by process ancestry")


def foreground_program(rex, session, block):
    process = rex.call("com.superlogical.terminal.process", session_id=session, block_id=block, args={})
    terminal = owned_tty(process, (process.get("child") or {}).get("pid", 0))
    rows = run(["ps", "-t", terminal, "-o", "pid=", "-o", "pgid=", "-o", "tpgid=", "-o", "uid=", "-o", "comm="])
    programs = []
    for row in rows.splitlines():
        fields = row.split(None, 4)
        if len(fields) == 5 and fields[1] == fields[2] and int(fields[3]) == os.getuid():
            programs.append((int(fields[0]), Path(fields[4]).name))
    if not programs:
        raise RuntimeError("The terminal's foreground process is unavailable")
    pid, name = next(
        (item for item in programs if re.fullmatch(r"n?vim|view|fzf", item[1])),
        next((item for item in programs if item[1] in ("fish", "bash", "zsh", "sh")), programs[-1]),
    )
    if name in ("nvim", "vim", "view", "fzf"):
        owned_tty(process, pid)
    return pid, name


def neovim_target(pid):
    processes = [row.split(None, 3) for row in run(["ps", "-axo", "pid=,ppid=,uid=,comm="], timeout=1).splitlines()]
    owners = {pid}
    while True:
        children = {
            int(row[0])
            for row in processes
            if len(row) == 4 and int(row[1]) in owners and int(row[2]) == os.getuid() and Path(row[3]).name == "nvim"
        }
        if children <= owners:
            break
        owners.update(children)
    candidates = run(["lsof", "-a", "-p", ",".join(map(str, sorted(owners))), "-U", "-Fpn"], timeout=1)
    socket_owner = 0
    for row in candidates.splitlines():
        if row.startswith("p"):
            socket_owner = int(row[1:])
        if not row.startswith("n/"):
            continue
        socket = Path(row[1:])
        try:
            metadata = socket.stat()
        except FileNotFoundError:
            continue
        if not stat.S_ISSOCK(metadata.st_mode) or metadata.st_uid != os.getuid():
            continue
        try:
            owner = run(["nvim", "--server", socket, "--remote-expr", "getpid()"], timeout=1)
        except (RuntimeError, subprocess.TimeoutExpired):
            continue
        if socket_owner in owners and owner == str(socket_owner):
            return {"socket": str(socket)}
    raise RuntimeError("No owned Neovim socket; refusing to steal editor focus")


def popup_neovim(target, cwd, command):
    token = uuid.uuid4().hex

    def request(action, **args):
        code = "assert(loadfile(" + json.dumps(str(PACKAGE / "popup.lua")) + "))(vim.json.decode(_A))"
        data = json.dumps({"action": action, "token": token, **args})
        expression = "luaeval(" + json.dumps(code) + "," + json.dumps(data) + ")"
        return int(run(["nvim", "--server", target["socket"], "--remote-expr", expression], timeout=3))

    colors = "bg:-1,gutter:-1,bg+:-1,preview-bg:-1,list-bg:-1,input-bg:-1,header-bg:-1"
    transparent = 'set -gx FZF_DEFAULT_OPTS "$FZF_DEFAULT_OPTS --height=100% --color=' + colors + '"; '
    argv = layout(cwd, transparent + command)["block"]["options"]["command"]
    failure = None
    try:
        request("open", argv=argv, cwd=str(cwd))
        deadline = time.monotonic() + 3600
        while time.monotonic() < deadline:
            status = request("status")
            if status != -1:
                return status
            time.sleep(0.2)
        raise RuntimeError("Picker timed out")
    except BaseException as error:
        failure = error
        raise
    finally:
        try:
            deadline = time.monotonic() + 3
            while request("close") == -1:
                if time.monotonic() >= deadline:
                    raise RuntimeError("Neovim picker cleanup timed out")
                time.sleep(0.2)
        except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as error:
            if failure is None:
                raise
            print(f"rex-opener: picker cleanup also failed: {error}", file=sys.stderr)


def popup(rex, session, block, client, cwd, command):
    if not client:
        raise RuntimeError("Picker needs an unambiguous app client; pass --client CLIENT_ID")
    original = rex.call("session.describe_block", session_id=session, block_id=block)["block"]
    shared = any(
        peer.get("info", {}).get("kind") == "app"
        and peer["client_id"] != client
        and session in peer.get("session_ids", [])
        for peer in rex.call("client.list")["clients"]
    )
    if not shared:
        pid, program = foreground_program(rex, session, block)
        if program == "nvim":
            return popup_neovim(neovim_target(pid), cwd, command)
    # Opaque cells keep the terminal underneath a native layer from showing through.
    terminal = layout(cwd, "printf '\\033[40m\\033[2J\\033[H'; " + command)
    terminal["block"]["options"]["exit"] = {"on_completion": False}
    private, created = "", None
    try:
        if shared:
            private = rex.call("session.create", label="opener")["session_id"]
            created = rex.call(
                "session.new_window", session_id=private, window_label="opener", layout=terminal, focus=False
            )
            target_session = private
        else:
            target_session = session
            created = rex.call(
                "session.new_layer",
                session_id=session,
                window_id=original["window_id"],
                bounds=[0.1, 0.15, 0.8, 0.7],
                layout=terminal,
                focus=False,
            )
        target_block = created["block_ids"][0]
        rex.action(
            client,
            "session.select",
            session_id=target_session,
            window_id=created.get("window_id", original["window_id"]),
        )
        rex.call("session.focus_block", session_id=target_session, block_id=target_block)
        deadline = time.monotonic() + 3600
        while time.monotonic() < deadline:
            process = rex.call(
                "com.superlogical.terminal.process", session_id=target_session, block_id=target_block, args={}
            )
            if process.get("last_exit") is not None:
                return process["last_exit"]["exit_code"]
            time.sleep(0.2)
        raise RuntimeError("Picker timed out")
    finally:
        if private:
            try:
                attached = rex.call("client.list")["clients"]
                if any(peer["client_id"] == client and private in peer.get("session_ids", []) for peer in attached):
                    rex.action(client, "session.select", session_id=session, window_id=original["window_id"])
            finally:
                rex.call("session.destroy", session_id=private)
        elif created:
            rex.call("session.close_layer", session_id=session, layer_id=created["layer_id"])


def choose(rex, session, block, client, cwd, rows, prompt):
    with tempfile.TemporaryDirectory(prefix="rex-select-") as temporary:
        source, selected = [Path(temporary) / name for name in ("rows", "selected")]
        source.write_text("\n".join(rows) + "\n")
        command = (
            f"fzf --delimiter='\\t' --with-nth=2.. --prompt={shlex.quote(prompt)}"
            f" < {shlex.quote(str(source))} > {shlex.quote(str(selected))}"
        )
        status = popup(rex, session, block, client, cwd, command)
        if status not in (0, 1, 130):
            raise RuntimeError(f"Picker exited with status {status}; check that fzf is on PATH")
        return selected.read_text().split("\t", 1)[0].strip() if status == 0 and selected.exists() else ""


def capture_text(rex, session, block):
    args = {"session_id": session, "block_id": block}
    result = rex.call(
        "com.superlogical.terminal.format", **args, args={"format": "text", "unwrap": True, "max_bytes": 983040}
    )
    parts = [result["content"]]
    offset = len(parts[0].encode("utf-8"))
    while "id" in result and not result.get("eof", False):
        result = rex.call(
            "com.superlogical.terminal.blob_read",
            **args,
            args={"id": result["id"], "offset": offset, "max_bytes": 983040},
        )
        if not result["data"] and not result["eof"]:
            raise RuntimeError("Scrollback read made no progress")
        parts.append(result["data"])
        offset += len(parts[-1].encode("utf-8"))
    return "".join(parts)


def text_targets(text, cwd):
    with tempfile.TemporaryDirectory(prefix="rex-targets-") as temporary:
        capture = Path(temporary) / "capture"
        capture.write_text(text)
        output = run(
            [
                "bash",
                "-c",
                'source "$1"; tmux_url_extract_targets "$2" "$2" "$3" "$4"',
                "rex-opener",
                PACKAGE / "targets.sh",
                capture,
                cwd,
                Path.home(),
            ]
        )
    return [tuple((row.split("\t") + [""] * 4)[:4]) for row in output.splitlines()]


def split_command(rex, session, block, client, cwd, command):
    size = rex.call("com.superlogical.terminal.size", session_id=session, block_id=block, args={})
    direction = "horizontal" if size["columns"] * 8 >= size["rows"] * 20 else "vertical"
    created = rex.call(
        "session.new_split",
        session_id=session,
        anchor_id=block,
        direction=direction,
        side="after",
        ratio=0.5,
        layout=layout(cwd, command),
        focus=False,
    )
    window = rex.call("session.describe_block", session_id=session, block_id=block)["block"]["window_id"]
    route = {"session": session, "window": window, "block": created["block_ids"][0]}
    shared = any(
        peer.get("info", {}).get("kind") == "app"
        and peer["client_id"] != client
        and session in peer.get("session_ids", [])
        for peer in rex.call("client.list")["clients"]
    )
    if client and not shared:
        rex.action(client, "session.select", session_id=session, window_id=window)
        rex.call("session.focus_block", session_id=session, block_id=route["block"])
    return route


def open_target(rex, session, block, client, cwd, target):
    kind, value, _, line = target
    if kind in ("md", "text"):
        args = ["nvim"]
        if line:
            args.append("+" + str(max(1, int(line))))
        return split_command(rex, session, block, client, cwd, shlex.join([*args, "--", value]))
    if kind == "dir":
        return popup(rex, session, block, client, Path(value), shlex.join(["yazi", value]))
    command = ["open"]
    if kind == "video" and Path("/Applications/IINA.app").is_dir():
        command += ["-a", "IINA"]
    run([*command, value])


def open_text_picker(rex, session, block, client, cwd):
    targets = text_targets(capture_text(rex, session, block), cwd)
    if not targets:
        raise RuntimeError("No URL or existing file found in this block's output")
    if len(targets) == 1:
        selected = "0"
    else:
        rows = [f"{index}\t{kind}\t{label}" for index, (kind, _, label, _) in enumerate(targets)]
        selected = choose(rex, session, block, client, cwd, rows, "open URL / file ❯ ")
    if selected:
        open_target(rex, session, block, client, cwd, targets[int(selected)])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", required=True)
    parser.add_argument("--block", required=True)
    parser.add_argument("--client", default="")
    args = parser.parse_args()
    rex = Rex()
    _, cwd, client = context(rex, args.session, args.block, args.client)
    try:
        open_text_picker(rex, args.session, args.block, client, cwd)
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as error:
        if client:
            message = re.sub(r"[\x00-\x1f\x7f]", " ", str(error))
            command = (
                "printf '%s\\n' "
                + shlex.quote("Rex opener: " + message)
                + "; read --prompt-str='Press Enter to close: '"
            )
            try:
                popup(rex, args.session, args.block, client, cwd, command)
            except (RuntimeError, OSError, ValueError, subprocess.SubprocessError):
                pass  # Preserve the original failure if the invoking client has closed.
        raise


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"rex-opener: {error}", file=sys.stderr)
        sys.exit(1)
