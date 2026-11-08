"""Non-blocking playback of the one bundled MP3 through FFmpeg's ffplay."""

import shutil
import subprocess
from pathlib import Path

SOUND_FILE = Path(__file__).resolve().parent / "SFX" / "sfx1.mp3"


class SoundPlayer:
    """Own player processes on the UI thread; wait only during final cleanup."""

    # Construction opens no audio device and creates no subprocess.
    def __init__(self):
        # Currently playing clip; a new event restarts it instead of overlapping.
        self._process: subprocess.Popen | None = None
        # Killed processes are reaped by poll() without waiting on a UI action.
        self._retired: list[subprocess.Popen] = []
        # Ignore late callbacks after the window has completed cleanup.
        self._closed = False

    # Start the fixed local asset and return immediately, without a shell or pipes.
    # The protocol supplies no path/URL; clients always use this same bundled file.
    def play(self) -> None:
        if self._closed:
            return
        self.stop()
        executable = shutil.which("ffplay")
        if executable is None:
            raise RuntimeError("Cannot play sound: install FFmpeg with ffplay available on PATH.")
        if not SOUND_FILE.is_file():
            raise RuntimeError("Cannot play sound: SFX/sfx1.mp3 is missing.")
        try:
            self._process = subprocess.Popen(
                [executable, "-nodisp", "-autoexit", "-loglevel", "error", "-nostats", str(SOUND_FILE)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError:
            raise RuntimeError("Cannot start the sound player.") from None

    # Cancel the current clip when restarting or leaving its room. Reaping a live
    # process is deferred so neither a button press nor room switch waits for it.
    def stop(self) -> None:
        self.poll()
        if self._process is not None:
            try:
                self._process.kill()
            except ProcessLookupError:
                pass
            self._retired.append(self._process)
            self._process = None

    # Run on the existing 50 ms UI tick to reap children and report a failed player.
    # Intentional cancellations are kept separately and do not report playback errors.
    def poll(self) -> str | None:
        self._retired = [process for process in self._retired if process.poll() is None]
        if self._process is not None:
            status = self._process.poll()
            if status is not None:
                self._process = None
                if status != 0:
                    return "Sound playback failed; check the audio device and FFmpeg installation."
        return None

    # After the window is closing, kill and reap every owned process before exit.
    # Repeated cleanup is harmless and no child continues playing after close.
    def close(self) -> None:
        self._closed = True
        self.stop()
        for process in self._retired:
            process.wait()
        self._retired.clear()
