
import contextlib
import os
import socket
import shutil
import subprocess
import tempfile


__all__ = [
    "BuildRoot",
]


class BuildRoot:
    def __init__(self, root, path="/run/osbuild"):
        self.root = tempfile.mkdtemp(prefix="osbuild-buildroot-", dir=path)
        self.api = tempfile.mkdtemp(prefix="osbuild-api-", dir=path)
        self.mounts = []
        for p in ["usr", "bin", "sbin", "lib", "lib64"]:
            source = os.path.join(root, p)
            target = os.path.join(self.root, p)
            if not os.path.isdir(source) or os.path.islink(source):
                continue # only bind-mount real dirs
            os.mkdir(target)
            try:
                subprocess.run(["mount", "-o", "bind,ro", source, target], check=True)
            except subprocess.CalledProcessError:
                self.unmount()
                raise
            self.mounts.append(target)

    def unmount(self):
        for path in self.mounts:
            subprocess.run(["umount", "--lazy", path], check=True)
            os.rmdir(path)
        self.mounts = []
        if self.root:
            shutil.rmtree(self.root)
            self.root = None
        if self.api:
            shutil.rmtree(self.api)
            self.api = None

    def run(self, argv, binds=None, readonly_binds=None, **kwargs):
        """Runs a command in the buildroot.

        Its arguments mean the same as those for subprocess.run().
        """

        return subprocess.run([
            "systemd-nspawn",
            "--quiet",
            "--register=no",
            "--as-pid2",
            "--link-journal=no",
            "--property=DeviceAllow=block-loop rw",
            f"--directory={self.root}",
            *[f"--bind={b}" for b in (binds or [])],
            *[f"--bind-ro={b}" for b in [f"{self.api}:/run/osbuild/api"] + (readonly_binds or [])],
            ] + argv, **kwargs)

    @contextlib.contextmanager
    def bound_socket(self, name):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        sock_path = os.path.join(self.api, name)
        sock.bind(os.path.join(self.api, name))
        try:
            yield sock
        finally:
            os.unlink(sock_path)
            sock.close()

    def __del__(self):
        self.unmount()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, exc_tb):
        self.unmount()
