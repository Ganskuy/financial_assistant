# Receipt image normalization evidence

The confirmed defect is transparency handling: converting a transparent black PNG
directly to RGB discards alpha. Black receipt ink and the transparent background
then become the same black pixels before the model receives the image. The fix
composites transparent inputs onto white paper. JPEG/PNG verification, EXIF
orientation, the 20-megapixel safety limit, 1024-pixel output envelope, JPEG quality
90, metadata removal, and the high-detail vision setting remain intact.

The Telegram client also accepts a generic `application/octet-stream` document MIME
declaration and case-insensitive MIME labels. Actual decoded bytes must still be a
valid single-frame JPEG/PNG, with supported paths and the existing download limits.
This closes an ingestion inconsistency; it does not establish how often Telegram
labels real uploads this way.

## Measured comparison

Baseline: commit `34b136acecbbcf6d163fab092f3960fd780d7c91`.
Updated: the alpha-compositing implementation in `app/telegram/client.py`.
Environment: macOS, Python 3.11.14, Pillow 12.3.0. Each cell below comes from 50
normalizations of the same synthetic image in a separate process per implementation
and fixture. p95 uses the nearest-rank 48th observation. No network or model calls
were made. INFO log output was disabled, so file/console logging overhead is excluded.

| Synthetic fixture / metric | Baseline | Updated |
|---|---:|---:|
| 2400 × 3600 JPEG, median wall time | 47.497 ms | 47.532 ms |
| Same JPEG, p95 wall time | 49.064 ms | 49.737 ms |
| Same JPEG, mean process CPU time per call | 47.797 ms | 48.086 ms |
| Same JPEG, peak process RSS | 146.844 MiB | 146.547 MiB |
| Same JPEG, output size | 94,580 bytes | 94,580 bytes |
| 500 × 180 transparent PNG, median wall time | 0.307 ms | 0.416 ms |
| Same PNG, p95 wall time | 1.240 ms | 0.518 ms |
| Same PNG, mean process CPU time per call | 0.404 ms | 0.511 ms |
| Same PNG, peak process RSS | 44.812 MiB | 44.984 MiB |
| Same PNG, grayscale pixel range | 0–0 (all black) | 0–255 (ink and white paper) |
| Same PNG, output size | 2,163 bytes | 5,524 bytes |

The JPEG output remains 683 × 1024; the PNG remains 500 × 180. Small timing/RSS
differences are local benchmark noise or overhead, not evidence of a general speed
improvement. Peak RSS includes the interpreter, imports, and native Pillow
allocations; it is not an incremental memory estimate or a concurrency capacity test.
Preserving previously lost ink requires additional JPEG bytes in the transparent case.

## Reproduction

Run from the repository root after installing the pinned requirements in `envir`.
This creates synthetic fixtures and the historical baseline in a temporary directory,
uses separate subprocesses for RSS measurements, and removes the temporary files on
completion. No credentials, real financial records, or model calls are involved.

```sh
envir/bin/python - <<'PY'
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw

worker = r'''
import importlib.util, io, json, resource, statistics, sys, time
from pathlib import Path
from PIL import Image
module_path, fixture_name = sys.argv[1:]
spec = importlib.util.spec_from_file_location("benchmark_client", module_path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
raw = Path(fixture_name).read_bytes()
latencies = []
cpu_started = time.process_time()
for _ in range(50):
    started = time.perf_counter()
    output = module.normalize_image(raw)
    latencies.append((time.perf_counter() - started) * 1000)
cpu_ms = (time.process_time() - cpu_started) * 1000 / len(latencies)
with Image.open(io.BytesIO(output)) as image:
    print(json.dumps({
        "fixture": Path(fixture_name).name,
        "implementation": Path(module_path).name,
        "runs": len(latencies),
        "p50_ms": round(statistics.median(latencies), 3),
        "p95_ms": round(sorted(latencies)[47], 3),
        "mean_cpu_ms": round(cpu_ms, 3),
        "peak_rss_mib": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            / (1024**2 if sys.platform == "darwin" else 1024), 3),
        "input_bytes": len(raw), "output_bytes": len(output),
        "output_size": image.size,
        "background_rgb": image.getpixel((0, 0)),
        "grayscale_extrema": image.convert("L").getextrema(),
    }))
'''
with tempfile.TemporaryDirectory(prefix="receipt-benchmark-") as temporary:
    folder = Path(temporary)
    baseline = folder / "baseline_client.py"
    baseline.write_bytes(subprocess.check_output([
        "git", "show", "34b136acecbbcf6d163fab092f3960fd780d7c91:app/telegram/client.py"
    ]))
    image = Image.new("RGB", (2400, 3600), "white")
    draw = ImageDraw.Draw(image)
    draw.text((100, 100), "SYNTHETIC RECEIPT - NOT REAL FINANCIAL DATA",
              fill="black", font_size=56)
    for i in range(65):
        draw.text((100, 230 + i * 46), f"{i+1:02}  Example item  x1",
                  fill="black", font_size=36)
        draw.text((1800, 230 + i * 46), f"{(i+1)*1000:,}",
                  fill="black", font_size=36)
    draw.text((100, 3350), "TOTAL          Rp 2.145.000", fill="black", font_size=60)
    image.save(folder / "large_receipt.jpg", quality=95)
    image = Image.new("RGBA", (500, 180), (0, 0, 0, 0))
    ImageDraw.Draw(image).text((20, 40), "TOTAL Rp15.000",
                              fill=(0, 0, 0, 255), font_size=32)
    image.save(folder / "transparent_receipt.png")
    for fixture in ("large_receipt.jpg", "transparent_receipt.png"):
        for implementation in (baseline, Path("app/telegram/client.py").resolve()):
            subprocess.run([sys.executable, "-c", worker, str(implementation),
                            str(folder / fixture)], check=True)
PY
```

## Boundaries of this evidence

- These two synthetic images prove a preprocessing defect and characterize local
  overhead. They do not measure model OCR, amount/merchant accuracy, extraction
  success rate, end-to-end Telegram latency, or API cost.
- Tall/dense receipts may still lose small text when reduced to the existing
  1024-pixel envelope. No labeled live-model/cost comparison supports enlarging the
  envelope or changing image detail, so both remain unchanged.
- Low-resolution or already compressed uploads cannot regain missing detail through
  compositing. The recovery message suggests sending the original JPEG/PNG as a
  Telegram document, a sharper photograph, or transaction text.
- JPEGs continue to be reencoded to remove arbitrary metadata. Lossless passthrough
  would need container sanitization to avoid preserving comments, EXIF, or trailing
  data; that broader change is outside this measured fix.
- Real receipt fixtures and opt-in provider evaluations remain necessary to measure
  recognition and financial extraction improvements.
