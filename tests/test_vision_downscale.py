from io import BytesIO
import sys
import base64
import pytest
from evi.vision import _fit_size, _downscale, build_image_content


def test_fit_size():
    assert _fit_size(3000, 2000, 1568) == (1568, 1045)
    assert _fit_size(100, 50, 1568) == (100, 50)
    assert _fit_size(1000, 1, 100) == (100, 1)
    assert _fit_size(2000, 3000, 0) == (2000, 3000)
    assert _fit_size(2000, 3000, -1) == (2000, 3000)


def test_downscale_pillow_missing(monkeypatch):
    monkeypatch.setitem(sys.modules, "PIL", None)
    assert _downscale(b"fake data", 1568) is None


def test_data_url_for_without_pillow(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "PIL", None)
    p = tmp_path / "test.png"
    p.write_bytes(b"fake png bytes")
    from evi.vision import _data_url_for
    url = _data_url_for(p)
    assert url == "data:image/png;base64," + base64.b64encode(b"fake png bytes").decode("ascii")


def test_downscale_unsupported_format(tmp_path):
    Image = pytest.importorskip("PIL.Image")
    p = tmp_path / "test.bmp"
    img = Image.new("RGB", (100, 100))
    img.save(p, "BMP")
    data = p.read_bytes()
    # BMP is not in ('JPEG', 'PNG', 'WEBP')
    assert _downscale(data, 1568) is None


def test_downscale_supported_formats(tmp_path):
    Image = pytest.importorskip("PIL.Image")
    for fmt in ("JPEG", "PNG", "WEBP"):
        p = tmp_path / f"test.{fmt.lower()}"
        img = Image.new("RGB", (2000, 1000))
        img.save(p, fmt)
        data = p.read_bytes()
        downscaled = _downscale(data, 1000)
        assert downscaled is not None
        res = Image.open(BytesIO(downscaled))
        assert res.size == (1000, 500)
        assert res.format == fmt


def test_downscale_exif_strip(tmp_path):
    Image = pytest.importorskip("PIL.Image")
    p = tmp_path / "exif.jpg"
    img = Image.new("RGB", (3000, 2000))
    exif = Image.Exif()
    exif[0x010F] = "TestCam"
    img.save(p, "JPEG", exif=exif.tobytes())

    data = p.read_bytes()
    downscaled = _downscale(data, 1568)
    assert downscaled is not None
    res = Image.open(BytesIO(downscaled))
    assert res.size == (1568, 1045)
    assert len(res.getexif()) == 0


def test_downscale_no_resize_strips_exif(tmp_path):
    Image = pytest.importorskip("PIL.Image")
    p = tmp_path / "exif.jpg"
    img = Image.new("RGB", (100, 100))
    exif = Image.Exif()
    exif[0x010F] = "TestCam"
    img.save(p, "JPEG", exif=exif.tobytes())

    data = p.read_bytes()
    downscaled = _downscale(data, 0) # max_px=0 means no resize
    assert downscaled is not None
    res = Image.open(BytesIO(downscaled))
    assert res.size == (100, 100)
    assert len(res.getexif()) == 0


def test_build_image_content_max_px(tmp_path, monkeypatch):
    Image = pytest.importorskip("PIL.Image")
    # Create a large image
    p = tmp_path / "large.jpg"
    img = Image.new("RGB", (3000, 2000))
    img.save(p, "JPEG")

    # Test with explicit max_px
    content = build_image_content("text", [p], max_px=1000)
    url = content[1]["image_url"]["url"]
    # Decode base64 to check size
    data = base64.b64decode(url.split(",")[1])
    res = Image.open(BytesIO(data))
    assert res.size == (1000, 666)


def test_build_image_content_default_max_px(tmp_path, monkeypatch):
    Image = pytest.importorskip("PIL.Image")
    # Create a large image
    p = tmp_path / "large.jpg"
    img = Image.new("RGB", (3000, 2000))
    img.save(p, "JPEG")

    # Test with default max_px (should use Config.load().vision.max_px = 1568)
    content = build_image_content("text", [p])
    url = content[1]["image_url"]["url"]
    data = base64.b64decode(url.split(",")[1])
    res = Image.open(BytesIO(data))
    assert res.size == (1568, 1045)


def test_build_image_content_small_png_keeps_size(tmp_path):
    Image = pytest.importorskip("PIL.Image")
    p = tmp_path / "small.png"
    img = Image.new("RGB", (200, 100))
    img.save(p, "PNG")

    content = build_image_content("text", [p])
    url = content[1]["image_url"]["url"]
    data = base64.b64decode(url.split(",")[1])
    res = Image.open(BytesIO(data))
    assert res.size == (200, 100)


def test_build_image_content_config_load_once(tmp_path, monkeypatch):
    """Check that build_image_content calls Config.load() exactly once for multiple images."""
    from types import SimpleNamespace

    from evi.config import Config

    calls = []
    monkeypatch.setattr(Config, "load", lambda: calls.append(1) or SimpleNamespace(
        vision=SimpleNamespace(max_px=1568)))

    # Create 3 dummy images
    for i in range(3):
        p = tmp_path / f"img{i}.jpg"
        p.write_bytes(b"fake image data")

    # build_image_content should resolve max_px once and pass it to _data_url_for
    build_image_content("text", [str(p) for p in tmp_path.glob("*.jpg")])

    assert len(calls) == 1
