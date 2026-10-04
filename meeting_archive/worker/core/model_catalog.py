"""Dependency-free catalogue shared by the desktop and standalone worker."""

WHISPER_MODELS = {
    "tiny": (.08, 1, 2), "tiny.en": (.08, 1, 2),
    "base": (.15, 1.2, 3), "base.en": (.15, 1.2, 3),
    "small": (.5, 2, 4), "small.en": (.5, 2, 4),
    "medium": (1.6, 5, 8), "medium.en": (1.6, 5, 8),
    "large-v1": (3.1, 10, 12), "large-v2": (3.1, 10, 12), "large-v3": (3.1, 10, 12),
    "large-v3-turbo": (1.62, 6, 8),
}
PARAKEET_MODEL = "parakeet-tdt-0.6b-v3-q8"
GIGAAM_MODEL = "gigaam-v3-e2e-rnnt"


def whisper_repo(model):
    if model not in WHISPER_MODELS:
        raise ValueError("Неизвестная модель Whisper")
    return "dropbox-dash/faster-whisper-large-v3-turbo" if model == "large-v3-turbo" else "Systran/faster-whisper-" + model
