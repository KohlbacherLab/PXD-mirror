"""Imported first by every test: tests never read the caller's configuration or point at a real mirror."""
import os, tempfile

for k in list(os.environ):
    if k.startswith("PXD_"):
        del os.environ[k]
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="pxdtest-xdg-")
