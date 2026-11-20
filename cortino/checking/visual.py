from pathlib import Path

from cortino import config as cortino_config
from cortino.checking import CheckResult, Finding
from cortino.llm import LLM, LLMError, _load_json
from cortino.platform.config_schema import DashboardConfig
from cortino.platform.preview_app import image_files
from cortino.spec.models import DashboardSpec
from cortino.tools.browser import Box, Browser, BrowserError

LAYOUT_MIN = 0.8
_PROMPT = Path(__file__).resolve().parent.parent / "prompts" / "visual_judge.md"


def iou(a: Box, b: Box) -> float:
    """Intersection over union of two (x, y, w, h) boxes."""
    w = min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0])
    h = min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1])
    inter = max(0.0, w) * max(0.0, h)
    union = a[2] * a[3] + b[2] * b[3] - inter
    return inter / union if union > 0 else 0.0


def layout_ious(actual: dict[str, Box], expected: dict[str, Box]) -> dict[str, float]:
    """IoU per expected chart; a chart missing from the page scores 0."""
    return {cid: iou(actual[cid], box) if cid in actual else 0.0 for cid, box in expected.items()}


def parse_judgement(raw: str) -> tuple[float, str]:
    """The judge's {"score": 0..1, "notes": str}; ValueError when the reply is not that."""
    data = _load_json(raw)
    score = data.get("score") if isinstance(data, dict) else None
    if isinstance(score, bool) or not isinstance(score, (int, float)) or not 0 <= score <= 1:
        raise ValueError(f"judge reply has no score between 0 and 1: {raw[:200]!r}")
    return float(score), str(data.get("notes", ""))


def check_visual(
    url: str,
    config: DashboardConfig,
    spec: DashboardSpec,
    browser: Browser,
    screenshot_path: Path,
    reference_dir: Path | None,
    llm: LLM,
) -> CheckResult:
    """Layout: the rendered chart boxes against the spec positions, both as fractions of the page
    (mean IoU >= 0.8). With reference images, the `cheap` role also scores the screenshot."""
    positions = {v.id: v.position for p in spec.pages for v in p.visuals if v.position}
    expected = {
        c.id: (positions[c.id].x / page.width, positions[c.id].y / page.height,
               positions[c.id].w / page.width, positions[c.id].h / page.height)
        for page in config.pages for c in page.charts if c.id in positions
    }  # fmt: skip
    findings: list[Finding] = []
    evidence: dict = {"mode": "layout only", "screenshot": str(screenshot_path)}

    def fail(message, **ev):
        findings.append(Finding(check="visual", owner="ui", visual_id=None, message=message, evidence=ev,
                                fixable=False))  # layout and components are computed in code

    try:
        browser.screenshot(url, screenshot_path)
        ious = layout_ious(browser.boxes(url), expected)
    except BrowserError as e:
        fail(f"preview could not be rendered: {e}")
        return CheckResult(name="visual", passed=False, findings=findings, evidence=evidence)
    mean = sum(ious.values()) / len(ious) if ious else 1.0
    evidence.update(layout_iou=ious, layout_mean=mean)
    if mean < LAYOUT_MIN:
        fail(f"layout differs from the spec: mean IoU {mean:.2f} < {LAYOUT_MIN}",
             low={cid: v for cid, v in ious.items() if v < LAYOUT_MIN})  # fmt: skip

    references = image_files(reference_dir)
    if references:
        evidence["mode"] = "layout and reference"
        minimum = cortino_config.load("thresholds")["visual_score_min"]
        prompt = ("The first image is the rebuilt dashboard; the others are the original. "
                  "Score how well the rebuilt one matches.")  # fmt: skip
        try:
            raw = llm.text("cheap", _PROMPT.read_text(encoding="utf-8"), prompt, images=[screenshot_path, *references])
            score, notes = parse_judgement(raw)
        except (LLMError, ValueError) as e:
            fail(f"visual judge gave no usable score: {e}")
        else:
            evidence.update(visual_score=score, visual_notes=notes)
            if score < minimum:
                fail(f"screenshot scores {score:.2f} against the reference (< {minimum}): {notes}")
    return CheckResult(name="visual", passed=not findings, findings=findings, evidence=evidence)
