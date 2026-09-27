"""وب‌اپ ترجمه زیرنویس SRT به فارسی روان با Gemini."""
import json
import os
from pathlib import Path

import streamlit as st

from subtitle_translator import (
    build_srt,
    chunk_subtitles,
    friendly_gemini_error,
    make_gemini_caller,
    parse_srt,
    translate_batches,
)

# Local key store (outside the repo, per Windows user — never committed to git)
# ⚠️ On shared hosting (Streamlit Cloud) disk/env are shared between ALL visitors,
# so file/env persistence is disabled there and each key lives only in its own session.
KEY_FILE = Path(os.getenv("LOCALAPPDATA", str(Path.home()))) / "SubtitleTranslator" / "settings.json"


def _is_shared_host() -> bool:
    if os.environ.get("SUBTRNSLT_SHARED", "").strip().lower() in ("1", "true", "yes"):
        return True
    return os.path.exists("/mount/src")  # Streamlit Community Cloud mounts repos here


IS_SHARED_HOST = _is_shared_host()


def cloud_shared_key() -> str:
    """Optional owner-funded key from Streamlit Secrets (Settings → Secrets on Cloud)."""
    try:
        return str(st.secrets.get("GEMINI_API_KEY", "") or "").strip()
    except Exception:
        return ""


def load_saved_key() -> tuple[str, str]:
    """Return (key, source). Source: 'file' | 'env' | ''.

    Disabled on shared hosting: a saved file/env would leak one user's key to everyone.
    """
    if IS_SHARED_HOST:
        return "", ""
    try:
        data = json.loads(KEY_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict) and str(data.get("gemini_api_key", "")).strip():
            return str(data["gemini_api_key"]).strip(), "file"
    except (OSError, ValueError):
        pass
    env_key = os.getenv("GEMINI_API_KEY", "").strip()
    if env_key:
        return env_key, "env"
    return "", ""


def save_key_to_file(key: str) -> None:
    if IS_SHARED_HOST:
        return
    KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    KEY_FILE.write_text(json.dumps({"gemini_api_key": key}, ensure_ascii=False), encoding="utf-8")


def clear_saved_key_file() -> None:
    if IS_SHARED_HOST:
        return
    try:
        KEY_FILE.unlink()
    except OSError:
        pass


def save_key_to_windows_env(key: str) -> None:
    """Persist key in the Windows user environment (HKCU\\Environment)."""
    os.environ["GEMINI_API_KEY"] = key
    if os.name == "nt":
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as reg:
            winreg.SetValueEx(reg, "GEMINI_API_KEY", 0, winreg.REG_SZ, key)

st.set_page_config(
    page_title="مترجم زیرنویس فارسی",
    page_icon="🎬",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Minimal RTL for Persian UI
st.markdown(
    """
<style>
@import url('https://fonts.googleapis.com/css2?family=Vazirmatn:wght@300;400;500;700&display=swap');
html, body, [class*="css"] { font-family: 'Vazirmatn', sans-serif; direction: rtl; text-align: right; }
.stSidebar { direction: rtl; }
</style>
""",
    unsafe_allow_html=True,
)

# ---- session state init (single place) ----
_saved_key, _saved_source = load_saved_key()
if not _saved_key:
    _shared = cloud_shared_key()
    if _shared:
        _saved_key, _saved_source = _shared, "shared"
st.session_state.setdefault("api_key", _saved_key)
st.session_state.setdefault("key_source", _saved_source)
st.session_state.setdefault("results", {})  # filename -> translated srt string
st.session_state.setdefault("stats", {})

with st.sidebar:
    st.markdown("### 🎬 مترجم زیرنویس")
    st.caption("ترجمه SRT به فارسی روان با Gemini — تایم‌کدها دقیقاً حفظ می‌شوند.")
    st.divider()

    api_key = st.text_input(
        "کلید Gemini",
        type="password",
        value=st.session_state["api_key"],
        placeholder="AI Studio API key...",
        help="با تیک ذخیره، کلید روی همین سیستم نگه داشته می‌شود و دفعات بعد خودکار پر می‌شود.",
        key="api_key_input",
    )
    if api_key.strip() != st.session_state["api_key"]:
        st.session_state["api_key"] = api_key.strip()
        st.session_state["key_source"] = "typed"
    if st.session_state["api_key"] and st.session_state["key_source"] in ("file", "env", "shared"):
        src = {"file": "حافظه سیستم", "env": "environment ویندوز", "shared": "کلید مشترک سرور"}[
            st.session_state["key_source"]
        ]
        st.caption(f"کلید از {src} بارگذاری شد.")

    if IS_SHARED_HOST:
        st.info("نسخه‌ی ابری: کلید فقط در همین نشست مرورگر تو می‌ماند و به هیچ‌وجه ذخیره نمی‌شود.")
    else:
        remember = st.checkbox(
            "ذخیره کلید روی این سیستم",
            value=bool(st.session_state["api_key"]),
            help="کلید در فایل محلی کاربر ویندوز ذخیره می‌شود (داخل ریپو نیست و در گیت نمی‌رود).",
            key="remember_key",
        )
        if remember and st.session_state["api_key"]:
            saved_now, _ = load_saved_key()
            if saved_now != st.session_state["api_key"]:
                save_key_to_file(st.session_state["api_key"])
        if not remember:
            clear_saved_key_file()

        if st.button("ثبت کلید در environment ویندوز", icon=":material/save:", key="save_env"):
            if not st.session_state["api_key"]:
                st.error("اول کلید را وارد کن.")
            else:
                try:
                    save_key_to_windows_env(st.session_state["api_key"])
                    st.session_state["key_source"] = "env"
                    st.success("کلید در environment ویندوز ثبت شد (از باز شدن بعدی ترمینال اعمال می‌شود).")
                except Exception as e:
                    st.error(f"ثبت نشد: {e}")

    st.caption("[دریافت کلید رایگان از Google AI Studio](https://aistudio.google.com/apikey)")

    if st.button("تست اتصال", icon=":material/wifi:", key="test_conn"):
        if not st.session_state["api_key"]:
            st.error("اول کلید را وارد کن.")
        else:
            with st.spinner("در حال بررسی دسترسی کلید..."):
                try:
                    from google import genai

                    client = genai.Client(api_key=st.session_state["api_key"])
                    try:
                        names = sorted(
                            {
                                getattr(m, "name", "").replace("models/", "")
                                for m in client.models.list()
                            }
                        )
                        names = [n for n in names if n]
                    except Exception as e_list:
                        names = []
                        st.warning(f"فهرست مدل‌ها گرفته نشد: {friendly_gemini_error(e_list)}")
                    if names:
                        st.success(f"{len(names)} مدل در دسترس این کلید است.")
                        st.caption(", ".join(names[:25]))
                    try:
                        r = client.models.generate_content(model=model, contents="Reply with ONLY: ok")
                        st.success(f"مدل «{model}» سالم است: {(r.text or '').strip()[:50]}")
                    except Exception as e_gen:
                        st.error(f"مدل «{model}» خطا داد: {friendly_gemini_error(e_gen)}")
                except Exception as e:
                    st.error(friendly_gemini_error(e))

    model = st.selectbox(
        "مدل",
        [
            "gemini-flash-lite-latest",
            "gemini-flash-latest",
            "gemini-2.5-flash",
            "gemini-2.5-flash-lite",
        ],
        index=0,
        help="مدل lite ارزان‌تر و سریع‌تر است و برای زیرنویس کافی است.",
    )

    tone_label = st.selectbox(
        "لحن ترجمه",
        [
            "خودکار (تشخیص از متن)",
            "محاوره‌ای فیلم و سریال",
            "رسمی مستند و آموزشی",
            "دراماتیک و ادبی",
            "طنز",
            "کودکانه",
        ],
        index=0,
    )
    tone_map = {
        "خودکار (تشخیص از متن)": "auto",
        "محاوره‌ای فیلم و سریال": "conversational",
        "رسمی مستند و آموزشی": "formal_doc",
        "دراماتیک و ادبی": "dramatic",
        "طنز": "comedy",
        "کودکانه": "kids",
    }

    batch_size = st.slider("تعداد زیرنویس در هر درخواست", 10, 80, 40, help="بچ کوچک‌تر دقیق‌تر ولی کندتر و پرهزینه‌تر است.")
    overlap = st.slider("هم‌پوشانی زمینه بین بچ‌ها", 0, 5, 3, help="چند زیرنویس آخر هر بچ فقط برای حفظ انسجام اسم‌ها و لحن، به بچ بعدی داده می‌شود (دوباره ترجمه نمی‌شود).")
    context_hint = st.text_input(
        "زمینه فیلم/سریال (اختیاری)",
        placeholder="مثلاً: سریال جنایی دهه ۹۰ نیویورک",
    )
    media_title = st.text_input(
        "نام فیلم/سریال (اختیاری)",
        placeholder="مثلاً: Breaking Bad",
        help="به انگلیسی وارد کن تا جمینای فضای اثر، اسامی و لحن دوره را بهتر تشخیص بدهد.",
    )
    media_year = st.text_input(
        "سال ساخت (اختیاری)",
        placeholder="مثلاً: 2008",
        help="۴ رقم، مثل 2008. برای تشخیص لحن قدیمی/جدید و اسامی مصطلح کمک می‌کند.",
    )

st.title("مترجم زیرنویس SRT به فارسی")
st.caption("فایل SRT بده، ترجمه روان فارسی با همان زمان‌بندی دقیق تحویل بگیر.")

col_up, col_paste = st.columns([1, 1], gap="medium")

with col_up:
    st.subheader("فایل زیرنویس", divider=True)
    files = st.file_uploader(
        "فایل SRT",
        type=["srt"],
        accept_multiple_files=True,
        label_visibility="collapsed",
        help="می‌توانی چند فایل SRT را همزمان آپلود کنی.",
    )

with col_paste:
    st.subheader("یا متن SRT را بچسبان", divider=True)
    pasted = st.text_area(
        "متن SRT",
        height=170,
        placeholder="1\n00:00:01,000 --> 00:00:03,000\nHello world!\n",
        label_visibility="collapsed",
    )

# Collect inputs as {filename: raw_text}
inputs: dict[str, str] = {}
if files:
    for f in files:
        try:
            raw = f.read().decode("utf-8-sig")
        except UnicodeDecodeError:
            raw = f.read().decode("latin-1")
        inputs[f.name] = raw
if pasted and pasted.strip():
    inputs["pasted.srt"] = pasted.strip()

if inputs:
    st.subheader("پیش‌نمایش ورودی", divider=True)
    for name, raw in inputs.items():
        with st.expander(f"{name}"):
            try:
                subs = parse_srt(raw)
                st.success(f"{len(subs)} زیرنویس پیدا شد — از {subs[0].start} تا {subs[-1].end}")
                st.code("\n\n".join(f"{s.start} --> {s.end}\n{s.text}" for s in subs[:5]), language="text")
                if len(subs) > 5:
                    st.caption(f"... و {len(subs) - 5} زیرنویس دیگر")
            except ValueError as e:
                st.error(str(e))
else:
    st.info("هنوز فایلی آپلود نشده. یک فایل ‎.srt‎ انتخاب کن یا متنش را بچسبان.")

translate_btn = st.button(
    "شروع ترجمه به فارسی",
    type="primary",
    icon=":material/translate:",
    disabled=not inputs,
)

if translate_btn:
    if not st.session_state["api_key"]:
        st.error("اول کلید Gemini را در سایدبار وارد کن.")
        st.stop()
    try:
        caller = make_gemini_caller(st.session_state["api_key"], model)
    except ValueError as e:
        st.error(str(e))
        st.stop()

    st.session_state["results"] = {}
    st.session_state["stats"] = {}
    progress = st.progress(0, text="در حال ترجمه...")
    total_files = len(inputs)

    for fi, (name, raw) in enumerate(inputs.items()):
        try:
            subs = parse_srt(raw)
        except ValueError as e:
            st.error(f"{name}: {e}")
            continue
        batches = chunk_subtitles(subs, batch_size)
        try:
            with st.status(f"در حال ترجمه {name} ({len(subs)} زیرنویس در {len(batches)} بچ)...", expanded=False):
                translated, reports = translate_batches(
                    batches,
                    caller,
                    tone=tone_map[tone_label],
                    context_hint=context_hint,
                    overlap=overlap,
                    progress_cb=lambda r, fi=fi, nb=len(batches): progress.progress(
                        (fi + r) / total_files, text=f"ترجمه {name}..."
                    ),
                    media_title=media_title.strip(),
                    media_year=media_year.strip(),
                )
            out_srt = build_srt(translated)
            st.session_state["results"][name] = out_srt
            failed = sum(1 for a, b in zip(subs, translated) if a.text == b.text)
            st.session_state["stats"][name] = {
                "count": len(subs),
                "fallback": failed,
                "reports": reports,
            }
        except Exception as exc:
            st.error(f"{name}: {friendly_gemini_error(exc)}")

    progress.empty()
    if st.session_state["results"]:
        st.success("ترجمه تمام شد. فایل‌ها را زیر دانلود کن.")
        st.rerun()

# ---- results ----
if st.session_state["results"]:
    st.subheader("نتیجه ترجمه", divider=True)
    for name, out_srt in st.session_state["results"].items():
        stat = st.session_state["stats"].get(name, {})
        total_n = stat.get("count", "?")
        failed_n = stat.get("fallback", 0)
        ok_n = (total_n - failed_n) if isinstance(total_n, int) else "?"
        st.markdown(f"**{name}** — {ok_n} از {total_n} زیرنویس ترجمه شد")
        if failed_n:
            st.warning(f"{failed_n} خط ترجمه نشد و به زبان اصلی باقی ماند.")
            with st.expander("جزئیات بچ‌ها"):
                for rep in stat.get("reports", []):
                    mark = "✅" if not rep["missing"] else "⚠️"
                    line = f"{mark} بچ {rep['batch']}: {rep['got']} از {rep['expected']}"
                    if rep.get("retried"):
                        line += " (تلاش مجدد شد)"
                    if rep["missing"]:
                        line += f" — جامانده‌ها: {rep['missing'][:20]}"
                    st.caption(line)
        tab_prev, tab_code = st.tabs(["پیش‌نمایش", "متن SRT"])
        with tab_prev:
            try:
                subs = parse_srt(out_srt)
                st.code(
                    "\n\n".join(f"{s.start} --> {s.end}\n{s.text}" for s in subs[:10]),
                    language="text",
                )
                if len(subs) > 10:
                    st.caption(f"... و {len(subs) - 10} زیرنویس دیگر")
            except ValueError:
                st.code(out_srt[:2000], language="text")
        with tab_code:
            st.code(out_srt[:4000], language="text")
        dl_name = name.rsplit(".", 1)[0] + ".fa.srt"
        st.download_button(
            "دانلود SRT فارسی",
            data=out_srt.encode("utf-8-sig"),
            file_name=dl_name,
            mime="text/plain",
            icon=":material/download:",
            key=f"dl_{name}",
        )
        st.divider()

if IS_SHARED_HOST:
    st.caption("نسخه‌ی ابری: کلید API فقط در همین نشست استفاده و به Google Gemini ارسال می‌شود؛ ذخیره نمی‌شود.")
else:
    st.caption("کلید API فقط به Google Gemini ارسال می‌شود. با تیک «ذخیره کلید» در فایل محلی همین سیستم نگه داشته می‌شود و در گیت ثبت نمی‌شود.")
