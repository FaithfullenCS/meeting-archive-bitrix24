"""Package the SVG's transparent PNG export as a seven-size Windows ICO."""
from pathlib import Path

from PIL import Image


def main():
    assets = Path(__file__).resolve().parents[1] / "meeting_archive/static"
    with Image.open(assets / "icon.png") as source:
        assert source.mode == "RGBA", "The vector export must preserve transparency"
        assert source.width == source.height, "Windows icon artwork must be square"
        source.save(assets / "favicon.ico", format="ICO",
                    sizes=[(size, size) for size in (16, 24, 32, 48, 64, 128, 256)])
    with Image.open(assets / "favicon.ico") as result:
        assert result.ico.sizes() == {(size, size) for size in (16, 24, 32, 48, 64, 128, 256)}
    print("Transparent PNG packaged as seven Windows ICO sizes")


if __name__ == "__main__":
    main()
