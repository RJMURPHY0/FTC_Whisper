"""
Runtime hook: ensure sounddevice can locate its PortAudio DLL when running
inside a PyInstaller one-file bundle.

sounddevice finds the PortAudio DLL through the _sounddevice_data package.
In a frozen build the package root is sys._MEIPASS, so we add it to PATH so
the DLL loader can find libportaudio64bit.dll (and its 32-bit variant).
"""
import os
import sys

if getattr(sys, "frozen", False):
    base = sys._MEIPASS
    # The PortAudio binary lives inside _sounddevice_data/portaudio-binaries/
    portaudio_dir = os.path.join(base, "_sounddevice_data", "portaudio-binaries")
    if os.path.isdir(portaudio_dir):
        os.environ["PATH"] = portaudio_dir + os.pathsep + os.environ.get("PATH", "")
