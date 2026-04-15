"""
디자인 피드백 협업 툴
────────────────────────────────────────────────────────────────────
실행:  streamlit run app.py
의존:  pip install -r requirements.txt
────────────────────────────────────────────────────────────────────
"""

import io
import sqlite3
import uuid
from datetime import datetime
from typing import Optional

import streamlit as st
from PIL import Image, ImageDraw, ImageFont
from streamlit_image_coordinates import streamlit_image_coordinates

# ─────────────────────────────────────────────
# 상수
# ─────────────────────────────────────────────
DB_PATH = "feedback.db"

STATUS_LIST = ["수정 필요", "진행 중", "완료"]
STATUS_EMOJI = {"수정 필요": "🔴", "진행 중": "🟡", "완료": "🟢"}
STATUS_HEX   = {"수정 필요": "#DC3545", "진행 중": "#FFA500", "완료": "#28A745"}
STATUS_RGB   = {"수정 필요": (220, 53, 69), "진행 중": (255, 165, 0), "완료": (40, 167, 69)}

PIN_R = 15          # 핀 반지름 (px)
MAX_IMG_W = 1200    # DB 저장 전 최대 너비 (메모리 절약)


# ─────────────────────────────────────────────
# 데이터베이스
# ─────────────────────────────────────────────
@st.cache_resource
def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id       TEXT PRIMARY KEY,
            name     TEXT NOT NULL,
            img_data BLOB NOT NULL,
            img_w    INTEGER,
            img_h    INTEGER,
            created  TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pins (
            id         TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            x_pct      REAL NOT NULL,
            y_pct      REAL NOT NULL,
            author     TEXT    DEFAULT '',
            comment    TEXT    DEFAULT '',
            status     TEXT    DEFAULT '수정 필요',
            created    TEXT,
            FOREIGN KEY (session_id) REFERENCES sessions(id)
        )
    """)
    conn.commit()
    return conn


def _conn() -> sqlite3.Connection:
    return _get_conn()


# ── Session ──────────────────────────────────
def db_create_session(name: str, img: Image.Image) -> str:
    """이미지를 BLOB으로 저장하고 세션 ID 반환."""
    # 너무 큰 이미지는 폭을 제한 (세로 비율 유지)
    if img.width > MAX_IMG_W:
        ratio = MAX_IMG_W / img.width
        img = img.resize((MAX_IMG_W, int(img.height * ratio)), Image.LANCZOS)

    buf = io.BytesIO()
    img.convert("RGB").save(buf, "PNG", optimize=True)

    sid = str(uuid.uuid4())
    _conn().execute(
        "INSERT INTO sessions VALUES (?,?,?,?,?,?)",
        (sid, name, buf.getvalue(), img.width, img.height, datetime.now().isoformat()),
    )
    _conn().commit()
    return sid


def db_load_session(sid: str) -> tuple[Optional[str], Optional[Image.Image]]:
    row = _conn().execute(
        "SELECT name, img_data FROM sessions WHERE id=?", (sid,)
    ).fetchone()
    if not row:
        return None, None
    name, img_data = row
    img = Image.open(io.BytesIO(img_data)).convert("RGBA")
    return name, img


# ── Pins ─────────────────────────────────────
def db_get_pins(sid: str) -> list:
    rows = _conn().execute(
        "SELECT id,x_pct,y_pct,author,comment,status,created "
        "FROM pins WHERE session_id=? ORDER BY created",
        (sid,),
    ).fetchall()
    keys = ["id", "x_pct", "y_pct", "author", "comment", "status", "created"]
    return [dict(zip(keys, r)) for r in rows]


def db_add_pin(sid: str, x_pct: float, y_pct: float) -> str:
    pid = str(uuid.uuid4())
    _conn().execute(
        "INSERT INTO pins VALUES (?,?,?,?,?,?,?,?)",
        (pid, sid, x_pct, y_pct, "", "", "수정 필요", datetime.now().isoformat()),
    )
    _conn().commit()
    return pid


def db_update_pin(pid: str, author: str, comment: str, status: str):
    _conn().execute(
        "UPDATE pins SET author=?,comment=?,status=? WHERE id=?",
        (author, comment, status, pid),
    )
    _conn().commit()


def db_delete_pin(pid: str):
    _conn().execute("DELETE FROM pins WHERE id=?", (pid,))
    _conn().commit()


# ─────────────────────────────────────────────
# 이미지 렌더링 (핀 오버레이)
# ─────────────────────────────────────────────
def _load_font(size: int) -> ImageFont.FreeTypeFont:
    for face in ("arial.ttf", "Arial.ttf", "DejaVuSans.ttf", "LiberationSans-Regular.ttf"):
        try:
            return ImageFont.truetype(face, size)
        except (IOError, OSError):
            pass
    return ImageFont.load_default()


def render_pins(img: Image.Image, pins: list, active_id: Optional[str], zoom: float) -> Image.Image:
    """줌 적용 후 이미지 위에 번호 핀을 그린다."""
    w, h = img.size
    sw, sh = max(1, int(w * zoom)), max(1, int(h * zoom))
    out = img.resize((sw, sh), Image.LANCZOS).convert("RGBA")
    draw = ImageDraw.Draw(out, "RGBA")
    font = _load_font(max(10, int(PIN_R * 1.1)))

    for i, p in enumerate(pins, 1):
        px = int(p["x_pct"] / 100 * sw)
        py = int(p["y_pct"] / 100 * sh)
        rgb = STATUS_RGB[p.get("status", "수정 필요")]
        is_active = p["id"] == active_id
        r = PIN_R + (4 if is_active else 0)

        # 그림자
        draw.ellipse([px - r - 1, py - r - 1, px + r + 1, py + r + 1], fill=(0, 0, 0, 60))
        # 채우기
        draw.ellipse([px - r, py - r, px + r, py + r], fill=(*rgb, 220))
        # 테두리
        border_col = (255, 220, 0, 255) if is_active else (255, 255, 255, 200)
        draw.ellipse([px - r, py - r, px + r, py + r],
                     outline=border_col, width=3 if is_active else 2)

        # 번호 텍스트
        label = str(i)
        bbox = font.getbbox(label)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        draw.text((px - tw // 2, py - th // 2 - 1), label, fill="white", font=font)

    return out


# ─────────────────────────────────────────────
# CSS
# ─────────────────────────────────────────────
CSS = """
<style>
/* 배경 */
html, body, [data-testid="stApp"] { background: #F4F5F7; }
[data-testid="stHeader"] { background: white; border-bottom: 1px solid #E2E2E2; }

/* 레이아웃 */
.block-container { padding-top: 3.5rem !important; padding-bottom: 1rem !important; }

/* Streamlit 기본 헤더 숨김 */
[data-testid="stHeader"] { display: none !important; }
[data-testid="stToolbar"] { display: none !important; }

/* 핀 카드 */
.pin-card {
    background: white;
    border-left: 4px solid #ccc;
    border-radius: 0 8px 8px 0;
    padding: 9px 12px;
    margin-bottom: 6px;
    box-shadow: 0 1px 3px rgba(0,0,0,.07);
    cursor: pointer;
}
.pin-card:hover { box-shadow: 0 2px 8px rgba(0,0,0,.13); }
.s-red   { border-left-color: #DC3545 !important; }
.s-amber { border-left-color: #FFA500 !important; }
.s-green { border-left-color: #28A745 !important; }
.pin-active { background: #FFFFF0 !important; }

/* 배지 */
.badge {
    display: inline-block;
    border-radius: 10px;
    font-size: 10px; font-weight: 700;
    color: white; padding: 2px 7px; margin-top: 4px;
}

/* 공유 코드 박스 */
.share-box {
    font-family: monospace; font-size: 11px;
    background: #EEF0F3; border-radius: 6px;
    padding: 6px 10px; word-break: break-all;
    color: #333;
}

hr { border-color: #E4E4E4 !important; margin: 10px 0 !important; }
</style>
"""


# ─────────────────────────────────────────────
# 페이지 설정
# ─────────────────────────────────────────────
st.set_page_config(
    page_title="디자인 피드백 툴",
    page_icon="📌",
    layout="wide",
    initial_sidebar_state="collapsed",
)
st.markdown(CSS, unsafe_allow_html=True)


# ─────────────────────────────────────────────
# 세션 상태 초기화
# ─────────────────────────────────────────────
def _init_state():
    defaults = {
        "session_id": st.query_params.get("session"),
        "active_pin": None,
        "zoom": 1.0,
        "my_name": "",
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v

_init_state()


# ═════════════════════════════════════════════
# 랜딩 / 업로드 화면
# ═════════════════════════════════════════════
if not st.session_state.session_id:
    st.markdown("## 📌 디자인 피드백 툴")
    st.markdown(
        "이미지를 업로드하면 **공유 링크**가 생성됩니다.  \n"
        "팀원이 링크(또는 세션 ID)로 접속해 같은 피드백을 확인·작성할 수 있습니다."
    )
    st.markdown("")

    # ── 신규 세션 생성 ───────────────────────
    with st.expander("✨ 새 세션 만들기", expanded=True):
        u1, u2 = st.columns([3, 2])
        with u1:
            uploaded = st.file_uploader(
                "상세페이지 이미지 업로드 (PNG / JPG / WEBP)",
                type=["png", "jpg", "jpeg", "webp"],
            )
        with u2:
            proj_name = st.text_input("프로젝트 이름", placeholder="예: 클라이언트A 메인페이지 v2")
            st.session_state.my_name = st.text_input(
                "내 이름 (선택)", value=st.session_state.my_name,
                placeholder="피드백 작성자로 표시됩니다",
            )

        if uploaded and proj_name:
            if st.button("🚀 세션 생성 →", type="primary"):
                img = Image.open(uploaded)
                sid = db_create_session(proj_name, img)
                st.session_state.session_id = sid
                st.query_params["session"] = sid
                st.rerun()

    # ── 기존 세션 열기 ───────────────────────
    with st.expander("🔗 기존 세션 열기"):
        j1, j2 = st.columns([4, 1])
        with j1:
            join_sid = st.text_input("세션 ID", placeholder="공유받은 세션 ID를 붙여넣으세요")
        with j2:
            st.markdown("<br>", unsafe_allow_html=True)
            if st.button("열기", disabled=not join_sid):
                n, img = db_load_session(join_sid)
                if img:
                    st.session_state.session_id = join_sid
                    st.query_params["session"] = join_sid
                    st.rerun()
                else:
                    st.error("세션을 찾을 수 없습니다.")

    st.stop()


# ═════════════════════════════════════════════
# 메인 피드백 화면
# ═════════════════════════════════════════════
name, base_img = db_load_session(st.session_state.session_id)

if not name:
    st.error("세션이 만료되었거나 존재하지 않습니다.")
    if st.button("홈으로"):
        st.session_state.session_id = None
        st.query_params.clear()
        st.rerun()
    st.stop()

pins = db_get_pins(st.session_state.session_id)

# ─── 헤더 ────────────────────────────────────
hA, hB, hC, hD = st.columns([4, 1.2, 1.5, 2.2])

with hA:
    st.markdown(f"### 📌 {name}")
    n_red  = sum(p["status"] == "수정 필요" for p in pins)
    n_yel  = sum(p["status"] == "진행 중"   for p in pins)
    n_grn  = sum(p["status"] == "완료"      for p in pins)
    st.caption(
        f"핀 {len(pins)}개 · 🔴 수정 필요 {n_red} · "
        f"🟡 진행 중 {n_yel} · 🟢 완료 {n_grn}"
    )

with hB:
    zoom = st.slider(
        "줌", 0.25, 3.0, st.session_state.zoom, 0.25,
        format="×%.2f", label_visibility="collapsed", help="이미지 줌 배율",
    )
    st.session_state.zoom = zoom

with hC:
    st.session_state.my_name = st.text_input(
        "내 이름", value=st.session_state.my_name,
        placeholder="내 이름 입력", label_visibility="collapsed",
        help="핀 편집 시 작성자로 자동 입력됩니다",
    )

with hD:
    sid_short = st.session_state.session_id
    st.markdown(
        f'<div class="share-box">세션 ID<br><b>{sid_short}</b></div>',
        unsafe_allow_html=True,
    )

st.divider()

# ─── 2-컬럼 레이아웃 ─────────────────────────
img_col, fb_col = st.columns([3, 1], gap="medium")


# ════ 왼쪽: 이미지 + 핀 ═════════════════════
with img_col:
    display_img = render_pins(
        base_img.copy(), pins, st.session_state.active_pin, st.session_state.zoom
    )
    display_rgb = display_img.convert("RGB")

    # 핀 개수를 키에 포함 → 새 핀 추가 후 위젯 초기화
    coords = streamlit_image_coordinates(
        display_rgb,
        key=f"canvas_{len(pins)}",
    )

    if coords:
        dw, dh = display_img.size
        x_pct = coords["x"] / dw * 100
        y_pct = coords["y"] / dh * 100
        pid = db_add_pin(st.session_state.session_id, x_pct, y_pct)
        st.session_state.active_pin = pid
        st.rerun()

    st.caption("💡 이미지를 클릭하면 해당 위치에 핀이 생성됩니다.")


# ════ 오른쪽: 피드백 패널 ═══════════════════
with fb_col:

    # ── 선택된 핀 편집 폼 ───────────────────
    if st.session_state.active_pin:
        ap = next((p for p in pins if p["id"] == st.session_state.active_pin), None)
        if ap:
            idx = pins.index(ap) + 1
            st.markdown(f"**✏️ 핀 #{idx} 편집**")

            with st.form("pin_form"):
                f_author = st.text_input(
                    "작성자",
                    value=ap.get("author") or st.session_state.my_name,
                    placeholder="이름",
                )
                f_comment = st.text_area(
                    "피드백 내용",
                    value=ap.get("comment", ""),
                    placeholder="수정 사항이나 의견을 작성하세요...",
                    height=120,
                )
                f_status = st.selectbox(
                    "상태",
                    STATUS_LIST,
                    index=STATUS_LIST.index(ap.get("status", "수정 필요")),
                )
                fc1, fc2, fc3 = st.columns(3)
                with fc1:
                    saved  = st.form_submit_button("저장", type="primary", use_container_width=True)
                with fc2:
                    closed = st.form_submit_button("닫기", use_container_width=True)
                with fc3:
                    removed = st.form_submit_button("삭제", use_container_width=True)

            if saved:
                db_update_pin(st.session_state.active_pin, f_author, f_comment, f_status)
                st.session_state.active_pin = None
                st.rerun()
            elif closed:
                st.session_state.active_pin = None
                st.rerun()
            elif removed:
                db_delete_pin(st.session_state.active_pin)
                st.session_state.active_pin = None
                st.rerun()

            st.divider()

    # ── 전체 피드백 목록 ────────────────────
    st.markdown("**📋 피드백 목록**")

    if not pins:
        st.info("이미지를 클릭해 첫 번째 핀을 추가하세요.")

    STATUS_CLASS = {"수정 필요": "s-red", "진행 중": "s-amber", "완료": "s-green"}

    for i, p in enumerate(pins, 1):
        status  = p.get("status", "수정 필요")
        author  = p.get("author")  or "익명"
        comment = p.get("comment") or ""
        preview = comment[:52] + ("…" if len(comment) > 52 else "")
        is_active = p["id"] == st.session_state.active_pin
        bg = "#FFFFF0" if is_active else "white"
        cls = STATUS_CLASS.get(status, "")

        st.markdown(
            f"""<div class="pin-card {cls}" style="background:{bg}">
                  <b style="font-size:13px;color:#222">
                    #{i} &nbsp; {STATUS_EMOJI[status]} &nbsp; {author}
                  </b>
                  <p style="font-size:12px;color:#555;margin:4px 0 0;min-height:16px">
                    {preview if preview else '<i style="color:#bbb">내용 없음</i>'}
                  </p>
                  <span class="badge" style="background:{STATUS_HEX[status]}">
                    {status}
                  </span>
                </div>""",
            unsafe_allow_html=True,
        )
        if st.button("편집", key=f"edit_{p['id']}", use_container_width=True):
            st.session_state.active_pin = p["id"]
            st.rerun()

    # ── 하단: 세션 관리 ─────────────────────
    st.divider()
    if st.button("🏠 다른 세션 열기", use_container_width=True):
        st.session_state.session_id = None
        st.session_state.active_pin = None
        st.query_params.clear()
        st.rerun()
