"""Internal data contracts for vector-PDF intake."""

from __future__ import annotations

from dataclasses import dataclass, field

Point = tuple[float, float]
BBox = tuple[float, float, float, float]


@dataclass(frozen=True)
class PageMap:
    """One PDF page and the business identity of its floor plan."""

    page_number: int
    floor: str
    number_prefix: str

    @property
    def block_name(self) -> str:
        return f"FLOOR_{self.floor}"


@dataclass(frozen=True)
class PdfWord:
    text: str
    bbox: BBox
    center: Point


@dataclass(frozen=True)
class BoardLabel:
    number: int
    width_mm: float
    height_mm: float
    number_bbox: BBox
    size_bbox: BBox
    center: Point

    @property
    def size_text(self) -> str:
        return f"{self.width_mm:g}x{self.height_mm:g}"


@dataclass(frozen=True)
class ArcCurve:
    """A PDF cubic or flattened R450 boundary, still in PDF coordinates."""

    points: tuple[Point, ...]
    bbox: BBox
    side: str

    @property
    def start(self) -> Point:
        return self.points[0]

    @property
    def end(self) -> Point:
        return self.points[-1]

    @property
    def middle(self) -> Point:
        return self.points[len(self.points) // 2]


@dataclass
class BoardCell:
    label: BoardLabel
    row: int
    column: int
    rect_pt: BBox
    material: str
    arc: ArcCurve | None = None
    issues: list[str] = field(default_factory=list)


@dataclass
class PageModel:
    page_map: PageMap
    width_pt: float
    height_pt: float
    mm_per_pt: float
    plan_rect_pt: BBox
    cells: list[BoardCell]
    words: list[PdfWord]
    drawing_count: int
    vector_item_count: int


@dataclass
class PdfIntakeModel:
    source_path: str
    source_sha256: str
    pages: list[PageModel]

    @property
    def board_label_count(self) -> int:
        return sum(len(page.cells) for page in self.pages)

    @property
    def r450_cell_count(self) -> int:
        return sum(
            1 for page in self.pages for cell in page.cells if cell.arc is not None
        )
