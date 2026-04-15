"""
디자인 피드백 협업 툴 v4  (성능 최적화)
────────────────────────────────────────────────────────
핵심 최적화
  1. @st.fragment  → 1번째 클릭 시 이미지 패널만 부분 재실행
                     (전체 앱 재실행 X → 체감 속도 대폭 향상)
  2. MAX_DISPLAY_W → 이미지 전송 크기 제한
  3. JPEG quality  → 75로 줄여 전송 용량 감소
  4. session_state 이미지 캐시 → DB 재조회 없음
────────────────────────────────────────────────────────
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
DB_PATH       = "feedback.db"
MAX_SAVE_W    = 1200   # DB 저장 전 최대 폭
MAX_DISPLAY_W = 860    # 화면 표시 최대 폭 (이 이상이면 축소 → 전송 데이터 ↓)
JPEG_QUALITY  = 75     # 낮을수록 파일 작고 빠름 (75 = 충분한 품질)

STATUS_LIST  = ["수정 필요", "진행 중", "완료"]
STATUS_EMOJI = {"수정 필요": "🔴", "진행 중": "🟡", "완료": "🟢"}
STATUS_HEX   = {"수정 필요": "#DC3545", "진행 중": "#FFA500", "완료": "#28A745"}
STATUS_RGB   = {"수정 필요": (220, 53, 69), "진행 중": (255, 165, 0), "완료": (40, 167, 69)}
STATUS_CLASS = {"수정 필요": "s-red",      "진행 중": "s-amber",       "완료": "s-green"}


# ─────────────────────────────────────────────
# DB
# ─────────────────────────────────────────────
@st.cache_resource
def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH, check_same_thread=False)
    c.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY, name TEXT NOT NULL,
            img_data BLOB NOT NULL, created TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS rects (
            id         TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            x1_pct REAL, y1_pct REAL, x2_pct REAL, y2_pct REAL,
            author  TEXT DEFAULT '',
            comment TEXT DEFAULT '',
            status  TEXT DEFAULT '수정 필요',
            created TEXT,
            FOREIGN KEY(session_id) REFERENCES sessions(id)
        )
    """)
    c.commit()
    return c


def db_create_session(name: str, img: Image.Image) -> str:
    if img.width > MAX_SAVE_W:
        img = img.resize((MAX_SAVE_W, int(img.height * MAX_SAVE_W / img.width)), Image.LANCZOS)
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=85, optimize=True)
    sid = str(uuid.uuid4())
    _conn().execute("INSERT INTO sessions VALUES (?,?,?,?)",
                    (sid, name, buf.getvalue(), datetime.now().isoformat()))
    _conn().commit()
    return sid


def db_load_name(sid: str) -> Optional[str]:
    row = _conn().execute("SELECT name FROM sessions WHERE id=?", (sid,)).fetchone()
    return row[0] if row else None


def _load_img_to_state(sid: str):
    """이미지를 session_state에 한 번만 로드 (이후 DB 조회 없음)."""
    if st.session_state.get("_img_sid") != sid:
        row = _conn().execute("SELECT img_data FROM sessions WHERE id=?", (sid,)).fetchone()
        if row:
            st.session_state["_img_bytes"] = row[0]
            st.session_state["_img_sid"]   = sid


def db_get_rects(sid: str) -> list:
    rows = _conn().execute(
        "SELECT id,x1_pct,y1_pct,x2_pct,y2_pct,author,comment,status,created "
        "FROM rects WHERE session_id=? ORDER BY created", (sid,)
    ).fetchall()
    keys = ["id","x1_pct","y1_pct","x2_pct","y2_pct","author","comment","status","created"]
    return [dict(zip(keys, r)) for r in rows]


def db_add_rect(sid, x1, y1, x2, y2, author, comment, status) -> str:
    rid = str(uuid.uuid4())
    _conn().execute("INSERT INTO rects VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (rid, sid, x1, y1, x2, y2, author, comment, status,
                     datetime.now().isoformat()))
    _conn().commit()
    return rid


def db_update_rect(rid, author, comment, status):
    _conn().execute("UPDATE rects SET author=?,comment=?,status=? WHERE id=?",
                    (author, comment, status, rid))
    _conn().commit()


def db_delete_rect(rid):
    _conn().execute("DELETE FROM rects WHERE id=?", (rid,))
    _conn().commit()


# ─────────────────────────────────────────────
# 이미지 렌더링  (캐시)
# ─────────────────────────────────────────────
def _font(size: int):
    for f in ("arial.ttf", "Arial.ttf", "DejaVuSans.ttf", "LiberationSans-Regular.ttf"):
        try:
            return ImageFont.truetype(f, size)
        except Exception:
            pass
    return ImageFont.load_default()


def _rects_sig(rects: list) -> str:
    return "|".join(f"{r['id'][:8]}{r['status']}" for r in rects)


@st.cache_data(show_spinner=False)
def _render_bg(img_bytes: bytes, rects_sig: str,
               rects_data: tuple, zoom: float) -> bytes:
    """
    저장된 구역이 그려진 배경 이미지 반환 (캐시).
    rects_sig 변경 시에만 재계산.
    MAX_DISPLAY_W 로 축소 → 전송 데이터 감소.
    """
    img = Image.open(io.BytesIO(img_bytes)).convert("RGBA")
    w, h = img.size

    # 줌 적용 후 최대 폭 제한
    target_w = min(int(w * zoom), MAX_DISPLAY_W)
    target_h = int(h * target_w / w)
    img = img.resize((target_w, target_h), Image.LANCZOS)

    if rects_data:
        ov   = Image.new("RGBA", (target_w, target_h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(ov, "RGBA")
        font = _font(max(10, int(12 * (target_w / w))))

        for i, r in enumerate(rects_data, 1):
            x1 = r["x1_pct"] / 100 * target_w
            y1 = r["y1_pct"] / 100 * target_h
            x2 = r["x2_pct"] / 100 * target_w
            y2 = r["y2_pct"] / 100 * target_h
            rgb = STATUS_RGB[r.get("status", "수정 필요")]

            draw.rectangle([x1, y1, x2, y2], fill=(*rgb, 30), outline=(*rgb, 210), width=2)

            label = str(i)
            pad   = 4
            bbox  = font.getbbox(label)
            lw = bbox[2] - bbox[0] + pad * 2
            lh = bbox[3] - bbox[1] + pad * 2
            draw.rectangle([x1, y1, x1 + lw, y1 + lh], fill=(*rgb, 220))
            draw.text((x1 + pad, y1 + pad), label, fill=(255, 255, 255, 255), font=font)

        img = Image.alpha_composite(img, ov)

    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=JPEG_QUALITY)
    return buf.getvalue()


def _add_overlay(base_bytes: bytes, zoom: float,
                 start_pt: Optional[dict],
                 pending: Optional[dict]) -> Image.Image:
    """
    캐시된 배경 위에 십자선 or 미확정 박스만 빠르게 추가.
    무거운 PIL 작업 없음 — 매 클릭마다 호출해도 빠름.
    """
    img = Image.open(io.BytesIO(base_bytes)).convert("RGBA")
    w, h = img.size

    if not start_pt and not pending:
        return img.convert("RGB")

    ov   = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(ov, "RGBA")
    blue = (30, 144, 255)

    if pending:
        x1 = pending["x1_pct"] / 100 * w
        y1 = pending["y1_pct"] / 100 * h
        x2 = pending["x2_pct"] / 100 * w
        y2 = pending["y2_pct"] / 100 * h
        draw.rectangle([x1, y1, x2, y2], fill=(*blue, 25), outline=(*blue, 200), width=2)
        font = _font(max(10, 12))
        draw.text((x1 + 4, y1 + 2), "NEW", fill=(*blue, 220), font=font)

    elif start_pt:
        sx = start_pt["x_pct"] / 100 * w
        sy = start_pt["y_pct"] / 100 * h
        r  = 9
        arm = 18
        draw.ellipse([sx - r, sy - r, sx + r, sy + r], outline=(*blue, 240), width=3)
        draw.line([sx - arm, sy, sx + arm, sy], fill=(*blue, 240), width=2)
        draw.line([sx, sy - arm, sx, sy + arm], fill=(*blue, 240), width=2)

    return Image.alpha_composite(img, ov).convert("RGB")


# ─────────────────────────────────────────────
# CSS
# ─────────────────────────────────────────────
CSS = """
<style>
html, body, [data-testid="stApp"] { background: #F4F5F7; }
[data-testid="stHeader"],
[data-testid="stToolbar"]  { display: none !important; }
.block-container { padding-top: 3.2rem !important; padding-bottom: 1rem !important; }
.rect-card {
    background: white; border-left: 4px solid #ccc;
    border-radius: 0 8px 8px 0; padding: 9px 12px;
    margin-bottom: 6px; box-shadow: 0 1px 3px rgba(0,0,0,.07);
}
.s-red   { border-left-color: #DC3545 !important; }
.s-amber { border-left-color: #FFA500 !important; }
.s-green { border-left-color: #28A745 !important; }
.badge {
    display:inline-block; border-radius:10px; font-size:10px;
    font-weight:700; color:white; padding:2px 7px; margin-top:4px;
}
.share-box {
    font-family:monospace; font-size:11px; background:#EEF0F3;
    border-radius:6px; padding:6px 10px; word-break:break-all; color:#333;
}
.tip-box {
    background:#EAF3FF; border-radius:8px; padding:8px 12px;
    font-size:12px; color:#1E5799; margin-bottom:6px;
}
hr { border-color:#E4E4E4 !important; margin:10px 0 !important; }
</style>
"""

# ─────────────────────────────────────────────
# 앱 초기화
# ─────────────────────────────────────────────
st.set_page_config(
    page_title="디자인 피드백 툴", page_icon="📌",
    layout="wide", initial_sidebar_state="collapsed",
)
st.markdown(CSS, unsafe_allow_html=True)

for k, v in [
    ("session_id",   st.query_params.get("session")),
    ("active_rect",  None),
    ("pending_rect", None),
    ("rect_start",   None),
    ("click_phase",  0),
    ("zoom",         1.0),
    ("my_name",      ""),
]:
    if k not in st.session_state:
        st.session_state[k] = v


# ═════════════════════════════════════════════
# 랜딩 화면
# ═════════════════════════════════════════════
if not st.session_state.session_id:
    st.markdown("## 📌 디자인 피드백 툴")
    st.markdown("이미지를 업로드하면 팀원과 공유 가능한 피드백 세션이 생성됩니다.")

    with st.expander("✨ 새 세션 만들기", expanded=True):
        u1, u2 = st.columns([3, 2])
        with u1:
            uploaded = st.file_uploader("이미지 업로드 (PNG / JPG / WEBP)",
                                        type=["png", "jpg", "jpeg", "webp"])
        with u2:
            proj_name = st.text_input("프로젝트 이름", placeholder="예: 클라이언트A 메인페이지 v2")
            st.session_state.my_name = st.text_input(
                "내 이름 (선택)", value=st.session_state.my_name,
                placeholder="피드백 작성자로 표시됩니다")
        if uploaded and proj_name:
            if st.button("🚀 세션 생성 →", type="primary"):
                img = Image.open(uploaded)
                sid = db_create_session(proj_name, img)
                st.session_state.session_id = sid
                st.query_params["session"] = sid
                st.rerun()

    with st.expander("🔗 기존 세션 열기"):
        j1, j2 = st.columns([4, 1])
        with j1:
            join_sid = st.text_input("세션 ID", placeholder="공유받은 세션 ID를 붙여넣으세요")
        with j2:
            st.markdown("<br>", unsafe_allow_html=True)
            if st.button("열기", disabled=not join_sid):
                if db_load_name(join_sid):
                    st.session_state.session_id = join_sid
                    st.query_params["session"] = join_sid
                    st.rerun()
                else:
                    st.error("세션을 찾을 수 없습니다.")
    st.stop()


# ═════════════════════════════════════════════
# 메인 화면
# ═════════════════════════════════════════════
sid = st.session_state.session_id

name = db_load_name(sid)
if not name:
    st.error("세션이 만료되었거나 존재하지 않습니다.")
    if st.button("홈으로"):
        st.session_state.session_id = None
        st.query_params.clear()
        st.rerun()
    st.stop()

# 이미지를 session_state에 로드 (앱 실행 중 DB 재조회 없음)
_load_img_to_state(sid)
if not st.session_state.get("_img_bytes"):
    st.error("이미지를 불러올 수 없습니다.")
    st.stop()

rects = db_get_rects(sid)

# ─── 헤더 ─────────────────────────────────────
hA, hB, hC, hD = st.columns([4, 1.2, 1.5, 2.2])
with hA:
    st.markdown(f"### 📌 {name}")
    n_r = sum(r["status"] == "수정 필요" for r in rects)
    n_y = sum(r["status"] == "진행 중"   for r in rects)
    n_g = sum(r["status"] == "완료"      for r in rects)
    st.caption(f"구역 {len(rects)}개 · 🔴 {n_r} · 🟡 {n_y} · 🟢 {n_g}")
with hB:
    zoom = st.slider("줌", 0.25, 2.0, st.session_state.zoom, 0.25,
                     format="×%.2f", label_visibility="collapsed", help="이미지 줌")
    if zoom != st.session_state.zoom:
        st.session_state.zoom = zoom
        st.session_state.rect_start  = None
        st.session_state.click_phase = 0
        st.rerun()
with hC:
    st.session_state.my_name = st.text_input(
        "내 이름", value=st.session_state.my_name,
        placeholder="내 이름 입력", label_visibility="collapsed")
with hD:
    st.markdown(f'<div class="share-box">세션 ID<br><b>{sid}</b></div>',
                unsafe_allow_html=True)

st.divider()

# ─── 2-컬럼 ───────────────────────────────────
img_col, fb_col = st.columns([3, 1], gap="medium")


# ════ 왼쪽: 이미지 패널 (@st.fragment) ════════
# fragment = 클릭 시 이 블록만 재실행 → 전체 앱 재실행 없음 → 빠름
@st.fragment
def image_section():
    # 현재 상태 읽기
    _sid   = st.session_state.session_id
    _zoom  = st.session_state.zoom
    _rects = db_get_rects(_sid)

    # 저장된 구역이 그려진 배경 (캐시 히트 시 즉시 반환)
    bg_bytes = _render_bg(
        st.session_state["_img_bytes"],
        _rects_sig(_rects),
        tuple(_rects),   # hashable for cache key
        _zoom,
    )

    # 십자선 / 미확정 박스 오버레이 (매우 가벼운 연산)
    display_img = _add_overlay(
        bg_bytes, _zoom,
        start_pt=st.session_state.rect_start,
        pending=st.session_state.pending_rect,
    )

    # 클릭 단계별 키 분리 (단계 바뀌면 위젯 리셋)
    img_key = f"img_{len(_rects)}_{st.session_state.click_phase}"
    coords  = streamlit_image_coordinates(display_img, key=img_key)

    # ── 클릭 처리 ──────────────────────────
    if coords and not st.session_state.pending_rect:
        iw, ih = display_img.size
        x_pct = coords["x"] / iw * 100
        y_pct = coords["y"] / ih * 100

        if st.session_state.click_phase == 0:
            # 1번째 클릭 → fragment 재실행만 (전체 재실행 X, 빠름)
            st.session_state.rect_start  = {"x_pct": x_pct, "y_pct": y_pct}
            st.session_state.click_phase = 1

        else:
            # 2번째 클릭 → 구역 완성 → 피드백 패널 갱신 필요 → 전체 재실행
            s = st.session_state.rect_start
            x1, y1 = min(s["x_pct"], x_pct), min(s["y_pct"], y_pct)
            x2, y2 = max(s["x_pct"], x_pct), max(s["y_pct"], y_pct)
            if (x2 - x1) > 1 and (y2 - y1) > 1:
                st.session_state.pending_rect = {
                    "x1_pct": x1, "y1_pct": y1,
                    "x2_pct": x2, "y2_pct": y2,
                }
            st.session_state.rect_start  = None
            st.session_state.click_phase = 0
            st.rerun()  # 피드백 폼 표시를 위한 전체 재실행

    # ── 안내 문구 ──────────────────────────
    if st.session_state.pending_rect:
        st.markdown('<div class="tip-box">✏️ 오른쪽 패널에서 피드백을 입력하고 저장하세요.</div>',
                    unsafe_allow_html=True)
    elif st.session_state.click_phase == 1:
        st.markdown('<div class="tip-box">✅ 시작점 완료! 이제 끝점을 클릭하세요.</div>',
                    unsafe_allow_html=True)
    else:
        st.markdown('<div class="tip-box">📌 수정이 필요한 구역의 <b>시작 지점을 클릭</b>하세요.</div>',
                    unsafe_allow_html=True)

    if st.session_state.click_phase == 1 or st.session_state.pending_rect:
        if st.button("↩ 취소", key="cancel_btn"):
            st.session_state.rect_start   = None
            st.session_state.click_phase  = 0
            st.session_state.pending_rect = None
            st.rerun()


with img_col:
    image_section()


# ════ 오른쪽: 피드백 패널 ════════════════════
with fb_col:

    # ── 신규 구역 폼 ─────────────────────────
    if st.session_state.pending_rect:
        st.markdown("**✏️ 새 구역 피드백 입력**")
        with st.form("new_form", clear_on_submit=True):
            f_author  = st.text_input("작성자",
                                      value=st.session_state.my_name, placeholder="이름")
            f_comment = st.text_area("피드백 내용",
                                     placeholder="수정 사항을 작성하세요...", height=110)
            f_status  = st.selectbox("상태", STATUS_LIST)
            nc1, nc2  = st.columns(2)
            with nc1:
                do_save   = st.form_submit_button("저장", type="primary", use_container_width=True)
            with nc2:
                do_cancel = st.form_submit_button("취소", use_container_width=True)

        if do_save:
            pr = st.session_state.pending_rect
            db_add_rect(sid, pr["x1_pct"], pr["y1_pct"],
                        pr["x2_pct"], pr["y2_pct"],
                        f_author, f_comment, f_status)
            st.session_state.pending_rect = None
            st.rerun()
        elif do_cancel:
            st.session_state.pending_rect = None
            st.rerun()

        st.divider()

    # ── 기존 구역 편집 폼 ────────────────────
    elif st.session_state.active_rect:
        ar = next((r for r in rects if r["id"] == st.session_state.active_rect), None)
        if ar:
            idx = rects.index(ar) + 1
            st.markdown(f"**✏️ 구역 #{idx} 편집**")
            with st.form("edit_form", clear_on_submit=False):
                f_author  = st.text_input("작성자",
                                          value=ar.get("author") or st.session_state.my_name)
                f_comment = st.text_area("피드백 내용", value=ar.get("comment", ""), height=110)
                f_status  = st.selectbox("상태", STATUS_LIST,
                                         index=STATUS_LIST.index(ar.get("status", "수정 필요")))
                ec1, ec2, ec3 = st.columns(3)
                with ec1:
                    do_save   = st.form_submit_button("저장", type="primary", use_container_width=True)
                with ec2:
                    do_close  = st.form_submit_button("닫기", use_container_width=True)
                with ec3:
                    do_delete = st.form_submit_button("삭제", use_container_width=True)

            if do_save:
                db_update_rect(st.session_state.active_rect, f_author, f_comment, f_status)
                st.session_state.active_rect = None
                st.rerun()
            elif do_close:
                st.session_state.active_rect = None
                st.rerun()
            elif do_delete:
                db_delete_rect(st.session_state.active_rect)
                st.session_state.active_rect = None
                st.rerun()

            st.divider()

    # ── 피드백 목록 ──────────────────────────
    st.markdown("**📋 피드백 목록**")

    if not rects:
        st.info("이미지 위를 클릭해 구역을 지정하세요.")

    for i, r in enumerate(rects, 1):
        status  = r.get("status", "수정 필요")
        author  = r.get("author")  or "익명"
        comment = r.get("comment") or ""
        preview = comment[:52] + ("…" if len(comment) > 52 else "")
        is_act  = r["id"] == st.session_state.active_rect
        bg      = "#FFFFF0" if is_act else "white"
        cls     = STATUS_CLASS.get(status, "")

        st.markdown(
            f"""<div class="rect-card {cls}" style="background:{bg}">
                  <b style="font-size:13px;color:#222">#{i}&nbsp;{STATUS_EMOJI[status]}&nbsp;{author}</b>
                  <p style="font-size:12px;color:#555;margin:4px 0 0;min-height:16px">
                    {preview if preview else '<i style="color:#bbb">내용 없음</i>'}
                  </p>
                  <span class="badge" style="background:{STATUS_HEX[status]}">{status}</span>
                </div>""",
            unsafe_allow_html=True,
        )
        if st.button("편집", key=f"edit_{r['id']}", use_container_width=True):
            st.session_state.active_rect  = r["id"]
            st.session_state.pending_rect = None
            st.rerun()

    st.divider()
    if st.button("🏠 다른 세션 열기", use_container_width=True):
        for k in ("session_id", "active_rect", "pending_rect", "rect_start"):
            st.session_state[k] = None
        st.session_state.click_phase = 0
        st.query_params.clear()
        st.rerun()
