"""Decode a WAV through the real installed faster-whisper/PyAV pair; no models."""
import tempfile
import wave
from pathlib import Path

from faster_whisper.audio import decode_audio


def main():
    with tempfile.TemporaryDirectory(prefix="meeting-worker-audio-") as folder:
        source = Path(folder) / "synthetic.wav"
        with wave.open(str(source), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(16000)
            audio.writeframes(b"\x00\x00" * 16000)
        decoded = decode_audio(str(source))
        assert len(decoded) == 16000, "Audio decoder changed the WAV duration"
        assert not decoded.any(), "Silence was not preserved"
    print("faster-whisper decoded 16000 WAV samples without a model.")


if __name__ == "__main__":
    main()
