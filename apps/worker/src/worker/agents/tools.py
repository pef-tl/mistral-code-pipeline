"""Tools des agents : shell et fichiers bornés au workspace du change."""

from __future__ import annotations

import asyncio
import os


async def _sh(cmd: str, cwd: str) -> dict:
    proc = await asyncio.create_subprocess_shell(
        cmd, cwd=cwd,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await asyncio.wait_for(proc.communicate(), timeout=300)
    return {"code": proc.returncode, "output": out.decode(errors="replace")[:8000]}


def build_dev_tools(worktree: str) -> dict:
    """(schéma, implémentation) pour run_agent — tout est confiné au worktree."""

    def safe_path(rel: str) -> str:
        full = os.path.realpath(os.path.join(worktree, rel))
        root = os.path.realpath(worktree)
        if full != root and not full.startswith(root + os.sep):
            raise PermissionError(f"path escapes workspace: {rel}")
        return full

    async def shell(command: str) -> dict:
        if any(b in command for b in ("sudo ", "rm -rf /", "git push")):
            return {"error": "command not allowed"}
        return await _sh(command, worktree)

    async def read_file(path: str) -> dict:
        with open(safe_path(path), encoding="utf-8") as fh:
            return {"content": fh.read()[:20000]}

    async def write_file(path: str, content: str) -> dict:
        full = safe_path(path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(content)
        return {"written": path}

    async def list_files(pattern: str = "") -> dict:
        proc = await asyncio.create_subprocess_exec(
            "git", "ls-files", cwd=worktree,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
        files = out.decode(errors="replace").splitlines()
        if pattern:
            files = [f for f in files if pattern in f]
        return {"files": files[:1000]}

    return {
        "shell": ({"description": "Run a shell command inside the workspace.",
                   "parameters": {"type": "object",
                                  "properties": {"command": {"type": "string"}},
                                  "required": ["command"]}},
                  shell),
        "read_file": ({"description": "Read a file from the workspace.",
                       "parameters": {"type": "object",
                                      "properties": {"path": {"type": "string"}},
                                      "required": ["path"]}},
                      read_file),
        "write_file": ({"description": "Create or overwrite a file in the workspace.",
                        "parameters": {"type": "object",
                                       "properties": {"path": {"type": "string"},
                                                      "content": {"type": "string"}},
                                       "required": ["path", "content"]}},
                       write_file),
        "list_files": ({"description": "List files tracked by git in the workspace, "
                                       "optionally filtered by a substring.",
                        "parameters": {"type": "object",
                                       "properties": {"pattern": {"type": "string"}},
                                       "required": []}},
                       list_files),
    }
