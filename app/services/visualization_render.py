"""In-memory Pillow fallback; no frontend/browser runtime is present in this project.

This is explicitly not the requested Recharts implementation. It reuses the existing
image dependency so the isolated command can deliver a useful artifact meanwhile.
"""

import io
import textwrap
from decimal import Decimal

from PIL import Image, ImageDraw, ImageFont

from app.schemas.visualization import ChartSpec, VisualizationSpec
from app.services.validation import idr
from app.services.visualization import report_lines, resolve_metric, validate_spec

COLORS = ["#1d4ed8", "#0f766e", "#b45309", "#7c3aed", "#be123c", "#0369a1", "#4d7c0f", "#6b7280"]
TITLES = {
    "expense_by_category": "Expense distribution",
    "income_by_category": "Income sources",
    "daily_expense": "Daily spending (day of month)",
    "weekly_expense": "Spending by 7-day month block",
    "income_vs_expense": "Income vs expenses",
    "net_cashflow": "Monthly net cashflow",
    "monthly_comparison": "Previous vs current month",
}


def compact_idr(value: int) -> str:
    for divisor, suffix in [(10**12, "T"), (10**9, "B"), (10**6, "M"), (10**3, "K")]:
        if abs(value) >= divisor:
            return f"Rp{Decimal(value) / divisor:.1f}{suffix}"
    return idr(value)


class DynamicChart:
    """Strict metric dispatch shared by all periods; coordinates alone use floats."""

    def __init__(self, draw: ImageDraw.ImageDraw):
        self.draw = draw
        self.font = ImageFont.load_default(size=20)

    def render(self, spec: ChartSpec | dict, data: dict, top: int) -> None:
        # Defense in depth, including callers bypassing the graph.
        spec = ChartSpec.model_validate(spec.model_dump() if isinstance(spec, ChartSpec) else spec)
        points = resolve_metric(spec, data)
        self.draw.text(
            (40, top), TITLES[spec.metric], font=ImageFont.load_default(size=26), fill="#0f172a"
        )
        if not points or all(value == 0 for _, value in points):
            self.draw.text(
                (40, top + 60), "No activity for this metric.", font=self.font, fill="#475569"
            )
            return
        if spec.type == "pie":
            self.pie(points, top + 45)
        elif spec.type == "line":
            self.line(points, top + 45)
        else:
            self.bar(points, top + 45)

    def distributions(self, points: list[tuple[str, int]]) -> list[tuple[str, int]]:
        if len(points) <= 8:
            return points
        return points[:7] + [("Remaining categories", sum(value for _, value in points[7:]))]

    def pie(self, points: list[tuple[str, int]], top: int) -> None:
        points = self.distributions(points)
        total = sum(value for _, value in points)
        angle = -90.0
        for index, (label, value) in enumerate(points):
            next_angle = angle + value / total * 360
            color = COLORS[index]
            if value:
                self.draw.pieslice((50, top, 350, top + 300), angle, next_angle, fill=color)
            angle = next_angle
            y = top + index * 36
            self.draw.rectangle((390, y + 4, 407, y + 21), fill=color)
            self.draw.text((420, y), f"{label[:32]}: {idr(value)}", font=self.font, fill="#0f172a")

    def bar(self, points: list[tuple[str, int]], top: int) -> None:
        # Time series remain intact; category tails are summed into one labeled bar.
        if len(points) > 8 and not all(label.isdigit() for label, _ in points):
            points = self.distributions(points)
        low, high = min(0, *(v for _, v in points)), max(0, *(v for _, v in points))
        span = high - low or 1
        left, right = 320, 745
        zero = left + (0 - low) / span * (right - left)
        step = min(36, 295 / len(points))
        self.draw.line((zero, top, zero, top + len(points) * step), fill="#94a3b8", width=1)
        font = self.font if step >= 25 else ImageFont.load_default(size=10)
        for index, (label, value) in enumerate(points):
            y = top + index * step
            x = left + (value - low) / span * (right - left)
            self.draw.text((45, y), label[:32], font=font, fill="#0f172a")
            self.draw.rectangle(
                (min(zero, x), y + 3, max(zero, x), y + step - 5), fill=COLORS[index % len(COLORS)]
            )
            self.draw.text((765, y), compact_idr(value), font=font, fill="#0f172a")
        self.draw.text(
            (320, top + len(points) * step + 15),
            "IDR (zero baseline)",
            font=self.font,
            fill="#475569",
        )

    def line(self, points: list[tuple[str, int]], top: int) -> None:
        maximum = max(value for _, value in points) or 1
        left, right, bottom = 160, 925, top + 275
        for index in range(5):
            value = maximum * index // 4
            y = bottom - index * 60
            self.draw.line((left, y, right, y), fill="#e2e8f0")
            self.draw.text((40, y - 10), compact_idr(value), font=self.font, fill="#475569")
        coords = [
            (left + i / max(1, len(points) - 1) * (right - left), bottom - value / maximum * 240)
            for i, (_, value) in enumerate(points)
        ]
        if len(coords) > 1:
            self.draw.line(coords, fill=COLORS[0], width=4)
        for index, (x, y) in enumerate(coords):
            self.draw.ellipse((x - 3, y - 3, x + 3, y + 3), fill=COLORS[0])
            if index % max(1, len(points) // 6) == 0 or index == len(points) - 1:
                self.draw.text(
                    (x - 12, bottom + 12), points[index][0], font=self.font, fill="#475569"
                )


def render_report(data: dict, spec: VisualizationSpec) -> bytes:
    spec = validate_spec(spec, data)
    lines = report_lines(data, spec)
    wrapped = [piece for line in lines[1:] for piece in textwrap.wrap(line, width=76)]
    header_height = 125 + len(wrapped) * 29
    heights = [
        max(180, 130 + min(295, len(resolve_metric(chart, data)) * 36))
        if chart.type == "bar"
        else 420
        for chart in spec.charts
    ]
    with Image.new("RGB", (1000, header_height + sum(heights) + 55), "#f8fafc") as image:
        draw = ImageDraw.Draw(image)
        draw.text((40, 30), lines[0], font=ImageFont.load_default(size=34), fill="#0f172a")
        for index, line in enumerate(wrapped):
            draw.text(
                (40, 85 + index * 29), line, font=ImageFont.load_default(size=22), fill="#334155"
            )
        renderer = DynamicChart(draw)
        top = header_height
        for chart, height in zip(spec.charts, heights, strict=True):
            renderer.render(chart, data, top)
            top += height
        draw.text(
            (40, image.height - 40),
            "Verified ledger data | Standard charts | IDR",
            font=ImageFont.load_default(size=18),
            fill="#475569",
        )
        with io.BytesIO() as output:
            image.save(output, format="PNG")
            result = output.getvalue()
    if len(result) > 2_000_000:
        raise ValueError("Report exceeds bounded delivery size")
    return result
