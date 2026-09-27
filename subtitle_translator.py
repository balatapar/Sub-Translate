"""SRT subtitle translator using Gemini API.

Design:
- Parse SRT strictly but tolerantly (BOM, CRLF, missing numbers).
- NEVER send timestamps to the model for rewriting. Send only indexed texts,
  get back indexed Persian translations, then rebuild with ORIGINAL timing.
- This guarantees timing preservation and valid SRT output.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


TIMESTAMP_RE = re.compile(
    r"(\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2},\d{3})"
)


@dataclass
class Subtitle:
    index: int
    start: str
    end: str
    text: str  # may contain \n for multi-line


def parse_srt(content: str) -> list[Subtitle]:
    """Parse SRT content into a list of Subtitle. Raises ValueError if empty/invalid."""
    # Normalize: strip BOM, unify newlines
    text = content.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        raise ValueError("فایل خالی است.")

    # Split on blank lines
    blocks = re.split(r"\n\s*\n", text)
    subs: list[Subtitle] = []
    auto_index = 1

    for block in blocks:
        lines = [ln.rstrip() for ln in block.strip().split("\n") if ln.strip() != "" or True]
        lines = [ln for ln in block.strip().split("\n")]
        if not lines:
            continue
        # Find timestamp line (usually line 0 or 1)
        ts_idx = -1
        start = end = ""
        for i, ln in enumerate(lines[:3]):
            m = TIMESTAMP_RE.search(ln.strip())
            if m:
                ts_idx = i
                start, end = m.group(1), m.group(2)
                break
        if ts_idx == -1:
            continue  # skip non-subtitle blocks (e.g. NOTE)
        text_lines = [ln.rstrip() for ln in lines[ts_idx + 1 :]]
        # drop empty trailing lines but keep internal line breaks
        while text_lines and not text_lines[-1].strip():
            text_lines.pop()
        while text_lines and not text_lines[0].strip():
            text_lines.pop(0)
        sub_text = "\n".join(text_lines).strip()
        if not sub_text:
            continue
        subs.append(Subtitle(index=auto_index, start=start, end=end, text=sub_text))
        auto_index += 1

    if not subs:
        raise ValueError("هیچ زیرنویسی در فایل پیدا نشد. فرمت SRT معتبر نیست.")
    return subs


def build_srt(subs: list[Subtitle]) -> str:
    """Build valid SRT string with sequential numbering and original timing."""
    out: list[str] = []
    for i, s in enumerate(subs, start=1):
        out.append(str(i))
        out.append(f"{s.start} --> {s.end}")
        out.append(s.text.strip())
        out.append("")
    return "\n".join(out).strip() + "\n"


def chunk_subtitles(subs: list[Subtitle], batch_size: int = 40) -> list[list[Subtitle]]:
    """Split into batches for API calls. batch_size ~ 30-50 is a good quality/cost balance."""
    batch_size = max(5, min(100, int(batch_size or 40)))
    return [subs[i : i + batch_size] for i in range(0, len(subs), batch_size)]


# ---------- Gemini interaction ----------

TONE_INSTRUCTIONS = {
    "auto": "لحن را از روی دیالوگ تشخیص بده. برای فیلم/سریال و دیالوگ داستانی: فارسی محاوره‌ای (گفتاری) کاملاً روان، طبیعی و زنده؛ از کلمات و ساختارهای کتابی، خشک و اتوکشیده پرهیز کن. برای مستند/آموزشی: رسمی و دقیق.",
    "conversational": "فارسی محاوره‌ای (گفتاری) کاملاً روان، طبیعی و زنده. از به‌کاربردن کلمات و ساختارهای کتابی، خشک و اتوکشیده خودداری کن. جمله‌ها باید مثل حرف‌زدن واقعی آدم‌ها در دوبله/زیرنویس حرفه‌ای باشد.",
    "formal_doc": "لحن رسمی و دقیق مستند و آموزشی. جمله‌ها کامل و درست، بدون اصطلاح عامیانه. (این لحن استثناست: محاوره‌ای ننویس.)",
    "dramatic": "لحن ادبی و دراماتیک ولی همچنان روان و طبیعی؛ حس و وزن احساسی دیالوگ را منتقل کن، بدون اغراق و بدون خشک‌شدن. اصطلاحات را محاوره‌ایِ متناسب با صحنه بومی‌سازی کن.",
    "comedy": "لحن طنز و زنده محاوره‌ای؛ شوخی‌ها را بومی‌سازی کن تا برای مخاطب فارسی خنده‌دار بماند، ولی معنا حفظ شود. ترجمه کلمه‌به‌کلمه ممنوع.",
    "kids": "لحن ساده و کودکانه؛ واژه‌های آسان و جمله‌های کوتاه. (این لحن استثناست: محاوره بزرگسال ننویس.)",
}

SEPARATOR = " ||| "


def build_batch_prompt(
    batch: list[Subtitle],
    batch_no: int,
    total_batches: int,
    tone: str = "auto",
    context_hint: str = "",
    prev_context: list[tuple[int, str, str]] | None = None,
    media_title: str = "",
    media_year: str = "",
) -> str:
    """Build the translation prompt for one batch.

    prev_context: already-translated tail of the previous batch as
    (index, source_text, translated_text). Sent for consistency only —
    the model must NOT re-output these lines.
    media_title/media_year: optional movie/series name + year, used only
    as background so the model keeps names, era and tone consistent.
    """
    tone_line = TONE_INSTRUCTIONS.get(tone, TONE_INSTRUCTIONS["auto"])
    title = (media_title or "").strip()
    year = (media_year or "").strip()
    hint_text = (context_hint or "").strip()
    media_line = ""
    if title or year or hint_text:
        media_desc = title if title else "نامشخص"
        if year:
            media_desc += f" ({year})"
        media_line = f"Media background (use ONLY for tone/era/names consistency, do NOT add plot info or hallucinate): {media_desc}"
        if hint_text:
            media_line += f" — {hint_text}"
        media_line += "\n"
    context_block = ""
    if prev_context:
        ctx_lines = []
        for idx, src, trans in prev_context:
            flat_src = src.replace("\n", " <br> ")
            flat_trans = (trans or "").replace("\n", " <br> ")
            if flat_trans:
                ctx_lines.append(f'[{idx}] "{flat_src}" → "{flat_trans}"')
            else:
                ctx_lines.append(f'[{idx}] "{flat_src}"')
        context_block = (
            "Previously translated lines (CONTEXT ONLY — do NOT re-translate or re-output them,\n"
            "use them only to keep names, pronouns and tone consistent):\n"
            + "\n".join(ctx_lines)
            + "\n\n"
        )
    lines = []
    for s in batch:
        # Flatten internal newlines as <br> so model clearly sees line breaks
        flat = s.text.replace("\n", " <br> ")
        lines.append(f"{s.index}{SEPARATOR}{flat}")
    joined = "\n".join(lines)
    return f"""You are a professional subtitle translator. Translate the following subtitles to fluent, natural Persian (Farsi).

Rules (very important):
- Source language is auto-detected (usually English). Output ONLY Persian.
- {tone_line}
- Keep meaning, humor and emotion. Never translate slang, idioms, proverbs or jokes word-by-word; use their natural, established equivalent in spoken Persian.
- Preserve each character's register: reconstruct formal, colloquial, serious or humorous tone from the scene and text, and keep it consistent for that speaker.
- Translate each line independently but keep story consistency across lines.
- Use the media background (if any) and the context lines above (if any) only to keep character names, pronouns, era and tone consistent — do NOT invent or add story details.
- Convert "<br>" back to a line break in your output (use a real newline inside the same subtitle).
- Keep <i>, </i>, <b>, </b> tags if present, in the right place.
- Do NOT merge or split subtitles. Output exactly {len(batch)} items with the same numbers as below — never re-output context lines.
- The numbers below are the ORIGINAL subtitle numbers (they do NOT start at 1). Copy each number EXACTLY as given — never restart numbering at 1.
- Sound descriptions like [Music], [Laughing], (sighs) → translate briefly: [موزیک]، [خنده]، (آه).
- Proper names of people/places stay as common Persian transliteration (e.g. John → جان); if media background is known, use the established Persian name for that film/series.
- No explanations, no timestamps, no extra lines. Output format per line exactly:
  <number> ||| <persian translation>
- This is batch {batch_no} of {total_batches}.
{media_line}{context_block}Subtitles:
{joined}
"""


def parse_batch_response(response_text: str, expected_indices: list[int]) -> dict[int, str]:
    """Parse model output lines 'idx ||| translation' into a dict. Tolerant parser.

    Supports multi-line subtitles: continuation lines without a leading number
    are appended to the previous subtitle.
    """
    result: dict[int, str] = {}
    if not response_text:
        return result
    expected = set(expected_indices)
    last_idx: int | None = None
    for raw_line in response_text.strip().split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        # Accept ||| or | or : as separator fallback
        m = re.match(r"^(\d+)\s*\|\|\|\s*(.*)$", line)
        if not m:
            m = re.match(r"^(\d+)\s*[|:]\s*(.*)$", line)
        if m:
            try:
                idx = int(m.group(1))
            except ValueError:
                continue
            trans = m.group(2).strip().strip('"').strip()
            trans = trans.replace(" <br> ", "\n").replace("<br>", "\n").replace("<br/>", "\n")
            if idx in expected and trans:
                result[idx] = trans
                last_idx = idx
            elif idx in expected:
                last_idx = idx
        elif last_idx is not None:
            # Skip echoed context lines like: [3] "source" → "translation"
            if re.match(r"^\[\d+\]", line):
                continue
            # continuation of previous subtitle's multi-line text
            cont = line.replace(" <br> ", "\n").replace("<br>", "\n").replace("<br/>", "\n")
            result[last_idx] = (result.get(last_idx, "") + "\n" + cont).strip()
    # final cleanup: collapse 3+ newlines to max 2
    for k, v in list(result.items()):
        result[k] = re.sub(r"\n{3,}", "\n\n", v.strip())
    return result


def build_repair_prompt(missing: list[Subtitle], tone: str = "auto") -> str:
    """Prompt asking ONLY for the missing subtitle numbers of a batch."""
    tone_line = TONE_INSTRUCTIONS.get(tone, TONE_INSTRUCTIONS["auto"])
    lines = [f"{s.index}{SEPARATOR}{s.text.replace(chr(10), ' <br> ')}" for s in missing]
    joined = "\n".join(lines)
    return f"""You are a professional subtitle translator. Translate ONLY the following {len(missing)} subtitles to fluent, natural Persian (Farsi).

Rules (very important):
- {tone_line}
- Output one line per subtitle, exactly in this format:
  <number> ||| <persian translation>
- The numbers are the ORIGINAL subtitle numbers — copy each number EXACTLY as given, never restart numbering at 1.
- Output NOTHING else: no explanations, no timestamps, no extra lines.
Subtitles:
{joined}
"""


def translate_batches(
    batches: list[list[Subtitle]],
    call_model,
    tone: str = "auto",
    context_hint: str = "",
    overlap: int = 3,
    progress_cb=None,
    max_retries: int = 1,
    media_title: str = "",
    media_year: str = "",
) -> tuple[list[Subtitle], list[dict]]:
    """Translate all batches with the injected call_model(prompt)->str.

    call_model is injected so UI can wire google-genai client + key.
    overlap: how many already-translated lines from the previous batch are
    sent along as read-only context (never re-output) for consistency.
    Lines still missing after a batch get up to max_retries repair calls
    asking ONLY for the missing numbers.
    Returns (subtitles with original timing, per-batch reports).
    Missing lines fall back to original text (never dropped).
    """
    overlap = max(0, min(10, int(overlap or 0)))
    translated: dict[int, str] = {}
    reports: list[dict] = []
    total = len(batches)
    for bi, batch in enumerate(batches, start=1):
        prev_context: list[tuple[int, str, str]] = []
        if overlap and bi > 1:
            for s in batches[bi - 2][-overlap:]:
                prev_context.append((s.index, s.text, translated.get(s.index, "")))
        prompt = build_batch_prompt(
            batch,
            bi,
            total,
            tone=tone,
            context_hint=context_hint,
            prev_context=prev_context,
            media_title=media_title,
            media_year=media_year,
        )
        resp_text = call_model(prompt)
        parsed = parse_batch_response(resp_text, [s.index for s in batch])
        translated.update(parsed)
        missing = [s for s in batch if s.index not in translated]
        attempts = 0
        while missing and attempts < max_retries:
            attempts += 1
            try:
                repair_resp = call_model(build_repair_prompt(missing, tone))
            except Exception:
                break
            translated.update(parse_batch_response(repair_resp, [s.index for s in missing]))
            missing = [s for s in batch if s.index not in translated]
        reports.append(
            {
                "batch": bi,
                "expected": len(batch),
                "got": len(batch) - len(missing),
                "missing": [s.index for s in missing],
                "retried": attempts > 0,
            }
        )
        if progress_cb:
            progress_cb(bi / total)
    # Rebuild preserving order + timing; fallback to original if a line failed
    out: list[Subtitle] = []
    for batch in batches:
        for s in batch:
            new_text = translated.get(s.index, s.text)
            out.append(Subtitle(index=s.index, start=s.start, end=s.end, text=new_text))
    out.sort(key=lambda x: x.index)
    return out, reports


def make_gemini_caller(api_key: str, model: str):
    """Create a call_model(prompt)->str function bound to user's key. Lazy import."""
    from google import genai

    key = (api_key or "").strip()
    if not key:
        raise ValueError("کلید Gemini وارد نشده است.")
    client = genai.Client(api_key=key)
    model_name = (model or "gemini-flash-lite-latest").strip()

    def _call(prompt: str) -> str:
        resp = client.models.generate_content(model=model_name, contents=prompt)
        return (getattr(resp, "text", "") or "").strip()

    return _call


def friendly_gemini_error(exc: Exception) -> str:
    t = str(exc).upper()
    if "401" in t or "UNAUTHENTICATED" in t or "API KEY NOT VALID" in t or "INVALID API KEY" in t:
        return "کلید Gemini معتبر نیست (خطای 401). کلید را از AI Studio بررسی و دوباره وارد کنید."
    if "429" in t or "RESOURCE_EXHAUSTED" in t or "QUOTA" in t:
        return "سهمیه Gemini تمام شده یا محدودیت نرخ (429). کمی صبر کنید یا مدل دیگری را امتحان کنید."
    if "403" in t or "FORBIDDEN" in t:
        return (
            "خطای 403: کلید معتبر است ولی سرور اجازه دسترسی نداد. علت‌های رایج:\n"
            "۱. کلید محدود (Restricted) شده — در Google Cloud Console بخش Credentials محدودیت‌های IP/Referrer/API را بررسی کن "
            "(باید Generative Language API مجاز باشد).\n"
            "۲. نام مدل اشتباه یا منسوخ است — با دکمه «تست اتصال» مدل‌های در دسترس کلیدت را ببین و مدل دیگری انتخاب کن.\n"
            "۳. محدودیت منطقه‌ای IP — با IP دیگری امتحان کن."
        )
    if "400" in t or "INVALID_ARGUMENT" in t:
        return f"درخواست نامعتبر بود (400): {exc}"
    if "NOT_FOUND" in t or "MODEL" in t and "NOT FOUND" in t:
        return f"مدل پیدا نشد. نام مدل را بررسی کنید: {exc}"
    return f"خطا در ترجمه: {exc}"
